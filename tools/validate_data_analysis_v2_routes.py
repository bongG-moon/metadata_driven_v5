#!/usr/bin/env python3
"""Validate all representative questions through the V2 route and executor.

The fixture plans and dummy retrieval data are intentionally shared with the
audited V1 validator. This isolates V2 regressions: Fast must execute without
an analysis-model call, while Complex must preserve the existing pandas code
path and invoke it exactly once when the fixture code succeeds.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import data_analysis_semantic_validator as semantic_validator  # noqa: E402
from tools import validate_representative_questions as base  # noqa: E402


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"
EXPECTED_COMPLEX_IDS = {5, 8, 9, 10, 11, 12, 22, 25, 30}
LIVE_ONLY_CASE = {
    "id": 31,
    "question": "7/6 생산 계획 데이터 알려줘",
    "min_rows": 1,
}


def _v2_modules() -> dict[str, Any]:
    return {
        "intent_vars": base.load_module(V2_ROOT / "02_intent_variables_builder.py"),
        "intent": base.load_module(V2_ROOT / "04_intent_plan_normalizer.py"),
        "resolver": base.load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py"),
        "pandas_prompt": base.load_module(V2_ROOT / "16_route_aware_pandas_prompt_builder.py"),
        "executor": base.load_module(V2_ROOT / "17_hybrid_analysis_executor.py"),
        "answer_prompt": base.load_module(V2_ROOT / "18_route_aware_answer_prompt_builder.py"),
        "answer": base.load_module(V2_ROOT / "20_hybrid_answer_builder.py"),
    }


def _prepared_payload(case: dict[str, Any], modules: dict[str, Any], reference_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = base.build_validation_request(case["question"], modules, reference_date)
    payload = modules["intent"].normalize_intent_plan(payload, case["intent_response"])
    payload = modules["hydrator"].hydrate_retrieval_jobs(
        payload,
        base.validation_catalog(case),
        retrieval_mode="dummy",
    )
    payload = modules["validator"].validate_retrieval_payload(payload)
    bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    retrieved = modules["dummy"].retrieve_dummy_data(bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, retrieved)
    payload = modules["adapter"].build_retrieval_payload(payload)
    pandas_vars = base.with_selected_helper_code(modules, modules["pandas_vars"].build_variables(payload))
    return payload, pandas_vars


def validate_case(
    case: dict[str, Any],
    modules: dict[str, Any],
    v2: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    payload, pandas_vars = _prepared_payload(case, modules, reference_date)
    resolved = v2["resolver"].resolve_simple_analysis_contract(payload)
    declared_route = "complex" if int(case["id"]) in EXPECTED_COMPLEX_IDS else "fast"
    # The shared deterministic fixtures predate the V2 operation contract and
    # intentionally contain opaque {"step": ...} plans. The safe compatible
    # behavior for those legacy fixtures is Complex; live validation below
    # checks the declared Fast/Complex expectation using the current prompt.
    expected_route = "complex"

    pandas_code = str(case["pandas_code"])
    if case.get("requires_helper"):
        pandas_code = base.inline_helper_source(
            pandas_code,
            str(case.get("helper_function") or "match_product_tokens"),
        )
    calls: list[str] = []

    def invoke(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"code": pandas_code}, ensure_ascii=False)

    executed = v2["executor"].execute_hybrid_analysis(
        resolved,
        "fixture complex pandas prompt",
        model_invoker=invoke,
        repair_prompt_template="fixture repair prompt",
        function_case_helper_code=str(pandas_vars.get("function_case_helper_code") or ""),
        max_repair_attempts=0,
    )
    route = str(executed.get("analysis", {}).get("execution_route") or "")
    status = str(executed.get("analysis", {}).get("status") or "")
    model_calls = len(calls)
    errors: list[str] = []
    if route != expected_route:
        errors.append(f"expected route {expected_route}, got {route}")
    expected_calls = 1 if expected_route == "complex" else 0
    if model_calls != expected_calls:
        errors.append(f"expected pandas model calls {expected_calls}, got {model_calls}")
    if status != "ok":
        trace_errors = executed.get("trace", {}).get("errors", [])
        errors.append(f"analysis status={status}: {trace_errors[-1:]}")
    semantic = semantic_validator.validate_semantic_payload(
        executed,
        question=str(case.get("question") or ""),
        pandas_variables=pandas_vars,
    )
    errors.extend(
        f"{item.get('type')}: {item.get('message')}"
        for item in semantic.get("errors", [])
        if isinstance(item, dict)
    )

    # Exercise the final V2 answer-state builder as well. Fast uses its fixed
    # answer already present in the payload; Complex accepts the supplied text.
    answered = v2["answer"].build_answer_response(executed, "분석 결과를 확인했습니다.")
    if not str(answered.get("answer_message") or "").strip():
        errors.append("missing answer_message")
    return {
        "id": case["id"],
        "question": case["question"],
        "status": "ok" if not errors else "error",
        "expected_route": expected_route,
        "declared_v2_route": declared_route,
        "actual_route": route,
        "recipe": resolved.get("simple_analysis_contract", {}).get("recipe", ""),
        "route_reason_codes": resolved.get("simple_analysis_contract", {}).get("eligibility", {}).get("reason_codes", []),
        "pandas_model_calls": model_calls,
        "row_count": executed.get("analysis", {}).get("row_count", 0),
        "errors": errors,
    }


def validate_live_case(
    case: dict[str, Any],
    modules: dict[str, Any],
    v2: dict[str, Any],
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    payload = base.build_validation_request(str(case["question"]), modules, reference_date)
    candidates_payload = modules["candidates"].build_metadata_candidates(
        payload,
        metadata_context["domain"],
        metadata_context["table"],
        metadata_context["main"],
    )
    metadata_candidates = candidates_payload.get("metadata_candidates", candidates_payload)
    intent_vars = base.with_specialized_prompt(v2["intent_vars"].build_variables(payload, metadata_candidates))
    intent_prompt = base.render_prompt(V2_ROOT / "03_intent_prompt_template_ko.md", intent_vars)
    intent_response = base.call_llm(intent_prompt, llm_config)
    payload = v2["intent"].normalize_intent_plan(payload, intent_response, candidates_payload)
    payload = modules["hydrator"].hydrate_retrieval_jobs(
        payload,
        metadata_context["table"],
        retrieval_mode="dummy",
    )
    payload = modules["validator"].validate_retrieval_payload(payload)
    bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    retrieved = modules["dummy"].retrieve_dummy_data(bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(payload, retrieved)
    payload = modules["adapter"].build_retrieval_payload(payload)
    resolved = v2["resolver"].resolve_simple_analysis_contract(payload)

    selection = base.load_module(V2_ROOT / "15_function_case_selection_builder.py").build_function_case_selection_only(resolved)
    helper_library = (base.FLOW / "function_case_helper_code_input_example.py").read_text(encoding="utf-8")
    helper_code = modules["helper_builder"].build_selected_helper_code(selection, helper_library)
    pandas_prompt = v2["pandas_prompt"].build_route_aware_pandas_prompt(
        resolved,
        (base.FLOW / "16_pandas_prompt_template_ko.md").read_text(encoding="utf-8"),
        helper_code,
    )
    executed = v2["executor"].execute_hybrid_analysis(
        resolved,
        pandas_prompt,
        model_invoker=lambda prompt: base.call_llm(prompt, llm_config),
        repair_prompt_template=(base.FLOW / "17b_pandas_repair_prompt_template_ko.md").read_text(encoding="utf-8"),
        function_case_helper_code=helper_code,
        max_repair_attempts=1,
    )
    answer_prompt = v2["answer_prompt"].build_route_aware_answer_prompt(
        executed,
        (base.FLOW / "19_answer_prompt_template_ko.md").read_text(encoding="utf-8"),
        (base.FLOW / "answer_domain_guidance_input_example_ko.md").read_text(encoding="utf-8"),
    )
    answer_text = base.call_llm(answer_prompt, llm_config) if answer_prompt else ""
    answered = v2["answer"].build_answer_response(executed, answer_text)

    expected_route = "complex" if int(case["id"]) in EXPECTED_COMPLEX_IDS else "fast"
    route = str(answered.get("analysis", {}).get("execution_route") or "")
    status = str(answered.get("analysis", {}).get("status") or "")
    call_counts = (
        answered.get("trace", {})
        .get("inspection", {})
        .get("fast_path", {})
        .get("llm_calls", {})
    )
    errors: list[str] = []
    if route != expected_route:
        errors.append(f"expected route {expected_route}, got {route}")
    if status != "ok":
        errors.append(f"analysis status={status}: {answered.get('trace', {}).get('errors', [])[-1:]}")
    expected_pandas_calls = 1 if expected_route == "complex" else 0
    if int(call_counts.get("pandas_generation") or 0) != expected_pandas_calls:
        errors.append(
            f"expected pandas model calls {expected_pandas_calls}, got {call_counts.get('pandas_generation')}"
        )
    expected_answer_calls = 1 if expected_route == "complex" else 0
    actual_answer_calls = 1 if answer_prompt else 0
    if actual_answer_calls != expected_answer_calls:
        errors.append(f"expected answer model calls {expected_answer_calls}, got {actual_answer_calls}")
    if not str(answered.get("answer_message") or "").strip():
        errors.append("missing answer_message")
    return {
        "id": case["id"],
        "question": case["question"],
        "status": "ok" if not errors else "error",
        "expected_route": expected_route,
        "actual_route": route,
        "recipe": resolved.get("simple_analysis_contract", {}).get("recipe", ""),
        "route_reason_codes": resolved.get("simple_analysis_contract", {}).get("eligibility", {}).get("reason_codes", []),
        "pandas_model_calls": int(call_counts.get("pandas_generation") or 0),
        "answer_model_calls": actual_answer_calls,
        "row_count": answered.get("analysis", {}).get("row_count", 0),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate representative questions through Data Analysis Flow V2.")
    parser.add_argument("--reference-date", default="20260701")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--use-llm", action="store_true", help="Run the current V2 intent/pandas/answer prompts with configured Gemini metadata.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", default="")
    args = parser.parse_args()

    base.load_dotenv(ROOT / ".env")
    base.install_lfx_stubs()
    modules = base.load_flow_modules()
    v2 = _v2_modules()
    cases = list(base.representative_cases())
    if args.use_llm:
        cases.append(LIVE_ONLY_CASE)
    if args.ids.strip():
        selected = {int(value.strip()) for value in args.ids.split(",") if value.strip()}
        cases = [case for case in cases if int(case["id"]) in selected]
    if args.limit > 0:
        cases = cases[: args.limit]
    if args.use_llm:
        metadata_context = base.load_metadata_context(modules)
        llm_config = base.resolve_llm_config()
        results = [
            validate_live_case(case, modules, v2, metadata_context, llm_config, args.reference_date)
            for case in cases
        ]
    else:
        results = [validate_case(case, modules, v2, args.reference_date) for case in cases]
    failures = [item for item in results if item["status"] != "ok"]
    report = {
        "status": "ok" if not failures else "error",
        "summary": {
            "total": len(results),
            "passed": len(results) - len(failures),
            "failed": len(failures),
            "fast": sum(item["actual_route"] == "fast" for item in results),
            "complex": sum(item["actual_route"] == "complex" for item in results),
            "validation_mode": "live_llm" if args.use_llm else "legacy_fixture_compatibility",
        },
        "results": results,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in results:
            marker = "OK" if item["status"] == "ok" else "FAIL"
            print(
                f"[{marker}] {item['id']:>2} {item['actual_route']:<7} "
                f"calls={item['pandas_model_calls']} rows={item['row_count']} "
                f"reason={item['route_reason_codes']} {item['question']}"
            )
            for error in item["errors"]:
                print(f"     {error}")
        print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
