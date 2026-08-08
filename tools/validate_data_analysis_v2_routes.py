#!/usr/bin/env python3
"""Validate representative V2 routes against an independent manifest.

The shared dummy retrieval data remains reusable, but opaque legacy pandas
steps are replaced with validation-only Typed IR from the manifest. This makes
Fast, deterministic Complex, and LLM-backed Complex expectations executable
without deriving the oracle from the Resolver output being tested.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
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
MANIFEST_PATH = ROOT / "validation_questions_v2_manifest.json"
LIVE_ONLY_CASE = {
    "id": 31,
    "question": "7/6 생산 계획 데이터 알려줘",
    "min_rows": 1,
}


def load_route_manifest(path: Path = MANIFEST_PATH) -> dict[int, dict[str, Any]]:
    """Load independent route expectations instead of deriving them from output."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if int(document.get("version") or 0) != 1:
        raise ValueError("V2 route manifest version must be 1")
    route_defaults = document.get("route_defaults")
    if not isinstance(route_defaults, dict):
        route_defaults = {}
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("V2 route manifest must contain a non-empty cases list")
    result: dict[int, dict[str, Any]] = {}
    for raw in cases:
        if not isinstance(raw, dict):
            raise ValueError("V2 route manifest case must be an object")
        case_id = int(raw.get("id") or 0)
        if case_id <= 0 or case_id in result:
            raise ValueError(f"V2 route manifest case id is invalid or duplicated: {case_id}")
        expected = deepcopy(raw)
        route = str(expected.get("expected_route") or "").strip().lower()
        if route not in {"fast", "complex", "blocked"}:
            raise ValueError(f"V2 route manifest case {case_id} has invalid route: {route!r}")
        defaults = route_defaults.get(route)
        if isinstance(defaults, dict):
            for key, value in defaults.items():
                expected.setdefault(str(key), deepcopy(value))
        calls = expected.get("expected_model_calls")
        if not isinstance(calls, dict):
            raise ValueError(f"V2 route manifest case {case_id} is missing expected_model_calls")
        for key in ("intent", "pandas_generation", "repair", "answer_generation"):
            if key not in calls or int(calls[key]) < 0:
                raise ValueError(f"V2 route manifest case {case_id} has invalid call count: {key}")
        if route == "fast" and case_id != int(LIVE_ONLY_CASE["id"]):
            if not isinstance(expected.get("fixture_plan"), dict):
                raise ValueError(f"V2 route manifest Fast case {case_id} is missing fixture_plan")
        result[case_id] = expected
    return result


def _fixture_output_contract(
    fixture_plan: dict[str, Any],
    *,
    recipe: str,
    source_alias: str,
    dataset_key: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compile a small validation-only recipe into the same Typed IR used live."""

    group_by = [str(item) for item in fixture_plan.get("group_by", []) if str(item).strip()]
    projection = [str(item) for item in fixture_plan.get("projection", []) if str(item).strip()]
    metrics = [deepcopy(item) for item in fixture_plan.get("metrics", []) if isinstance(item, dict)]
    ordering = [deepcopy(item) for item in fixture_plan.get("ordering", []) if isinstance(item, dict)]
    normalized_metrics: list[dict[str, Any]] = []
    aggregations: list[dict[str, Any]] = []
    for metric in metrics:
        source_column = str(metric.get("source_column") or metric.get("column") or "").strip()
        output_column = str(metric.get("output_column") or source_column).strip()
        aggregation = str(metric.get("aggregation") or metric.get("method") or "").strip().lower()
        if not source_column or not output_column or not aggregation:
            raise ValueError(f"invalid validation metric contract: {metric!r}")
        normalized_metrics.append(
            {
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "source_column": source_column,
                "aggregation": aggregation,
                "output_column": output_column,
            }
        )
        aggregations.append(
            {
                "column": source_column,
                "method": aggregation,
                "output_column": output_column,
            }
        )

    steps: list[dict[str, Any]] = []
    if projection:
        steps.append(
            {
                "operation": "select_columns",
                "source_alias": source_alias,
                "columns": projection,
            }
        )
    if aggregations:
        steps.append(
            {
                "operation": "groupby_and_aggregate",
                "source_alias": source_alias,
                "group_by": group_by,
                "aggregations": aggregations,
            }
        )
    if recipe == "distinct_summary":
        steps.append({"operation": "distinct_summary", "source_alias": source_alias})
    for item in ordering:
        column = str(item.get("column") or item.get("sort_by") or "").strip()
        if column:
            steps.append(
                {
                    "operation": "sort_and_top_n",
                    "source_alias": source_alias,
                    "sort_by": column,
                    "order": str(item.get("direction") or item.get("order") or "asc"),
                    "limit": int(item.get("limit") or 0),
                }
            )
    result_columns = list(dict.fromkeys([*projection, *group_by, *[item["output_column"] for item in normalized_metrics]]))
    output_contract = {
        "result_mode": "detail" if recipe in {"detail_query", "distinct_summary"} else "aggregate",
        "fast_path_recipe": recipe,
        "required_columns": result_columns,
        "result_columns": result_columns,
        "grain_columns": group_by,
        "metric_columns": [item["output_column"] for item in normalized_metrics],
        "metric_bindings": normalized_metrics,
        "ordering": ordering,
        "strict_result_columns": True,
        "null_group_policy": "preserve_as_blank",
        "metric_null_policy": "display_zero",
    }
    return steps, output_contract


def apply_manifest_fixture_plan(payload: dict[str, Any], expectation: dict[str, Any]) -> dict[str, Any]:
    """Replace opaque legacy fixture steps with an executable manifest plan."""

    next_payload = deepcopy(payload)
    fixture_plan = expectation.get("fixture_plan")
    if isinstance(fixture_plan, dict) and fixture_plan.get("deterministic_operation"):
        return _apply_deterministic_complex_fixture(next_payload, fixture_plan)
    if not isinstance(fixture_plan, dict):
        return next_payload
    plan = next_payload.get("intent_plan") if isinstance(next_payload.get("intent_plan"), dict) else {}
    jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(jobs) != 1:
        raise ValueError(f"Fast case {expectation.get('id')} must have one retrieval job")
    source_alias = str(jobs[0].get("source_alias") or "").strip()
    dataset_key = str(jobs[0].get("dataset_key") or "").strip()
    recipe = str(expectation.get("expected_recipe") or "").strip().lower()
    steps, output_contract = _fixture_output_contract(
        fixture_plan,
        recipe=recipe,
        source_alias=source_alias,
        dataset_key=dataset_key,
    )
    plan["pandas_execution_plan"] = steps
    plan["output_contract"] = output_contract
    plan.pop("pandas_function_cases", None)
    for key in (
        "resolved_empty_result_plan",
        "resolved_grain_plan",
        "resolved_metric_comparison_plan",
        "resolved_metric_merge_plan",
        "resolved_output_grain_plan",
        "resolved_presence_comparison_plan",
        "resolved_reference_join_plan",
    ):
        plan.pop(key, None)
    next_payload["intent_plan"] = plan
    return next_payload


def _apply_deterministic_complex_fixture(
    payload: dict[str, Any],
    fixture_plan: dict[str, Any],
) -> dict[str, Any]:
    """Attach a strict generic comparison/join contract for Complex fixtures."""

    operation = str(fixture_plan.get("deterministic_operation") or "").strip().lower()
    if operation not in {"merge_metric_sources", "compare_metrics", "compare_presence"}:
        return payload
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    grain_columns = [str(item) for item in fixture_plan.get("grain_columns", []) if str(item).strip()]
    metrics = [deepcopy(item) for item in fixture_plan.get("metrics", []) if isinstance(item, dict)]
    aliases = [str(item.get("source_alias") or "").strip() for item in metrics]
    if not grain_columns or len(metrics) != 2 or any(not alias for alias in aliases):
        raise ValueError(f"invalid deterministic Complex fixture: {fixture_plan!r}")
    grain_mappings = [
        {
            "canonical_column": column,
            "output_column": column,
            "source_candidates": {alias: [column] for alias in aliases},
        }
        for column in grain_columns
    ]
    normalized_metrics: list[dict[str, Any]] = []
    metric_bindings: list[dict[str, Any]] = []
    jobs = {
        str(item.get("source_alias") or "").strip(): item
        for item in plan.get("retrieval_jobs", [])
        if isinstance(item, dict)
    }
    for metric in metrics:
        alias = str(metric.get("source_alias") or "").strip()
        source_column = str(metric.get("source_column") or "").strip()
        output_column = str(metric.get("output_column") or "").strip()
        aggregation = str(metric.get("aggregation") or "sum").strip().lower()
        if not source_column or not output_column:
            raise ValueError(f"invalid deterministic Complex metric: {metric!r}")
        normalized_metrics.append(
            {
                "source_alias": alias,
                "source_column": source_column,
                "source_candidates": [source_column],
                "aggregation": aggregation,
                "output_column": output_column,
                "fill_value": 0,
            }
        )
        metric_bindings.append(
            {
                "source_alias": alias,
                "dataset_key": str(jobs.get(alias, {}).get("dataset_key") or ""),
                "source_column": source_column,
                "aggregation": aggregation,
                "output_column": output_column,
            }
        )
    merge_plan = {
        "operation": "merge_metric_sources",
        "strict": True,
        "grain_mappings": grain_mappings,
        "metrics": normalized_metrics,
        "join_type": str(fixture_plan.get("join_type") or "outer").strip().lower(),
        "fill_zero_on_success": True,
    }
    if operation == "merge_metric_sources":
        plan["resolved_metric_merge_plan"] = merge_plan
    elif operation == "compare_presence":
        plan["resolved_presence_comparison_plan"] = {
            "operation": "compare_presence",
            "strict": True,
            "presence_rule": "left_positive_right_missing_or_zero",
            "grain_mappings": grain_mappings,
            "left_metric": normalized_metrics[0],
            "right_metric": normalized_metrics[1],
        }
    else:
        plan["resolved_metric_comparison_plan"] = {
            "operation": "compare_metrics",
            "strict": True,
            "merge_plan": merge_plan,
            "lhs_metric_column": str(fixture_plan.get("lhs_metric_column") or ""),
            "operator": str(fixture_plan.get("comparison_operator") or "gt").strip().lower(),
            "rhs_metric_column": str(fixture_plan.get("rhs_metric_column") or ""),
        }
    metric_columns = [item["output_column"] for item in normalized_metrics]
    result_columns = [*grain_columns, *metric_columns]
    plan["output_contract"] = {
        "result_mode": "aggregate",
        "required_columns": result_columns,
        "result_columns": result_columns,
        "grain_columns": grain_columns,
        "metric_columns": metric_columns,
        "metric_bindings": metric_bindings,
        "ordering": {
            "sort_by": str(fixture_plan.get("sort_by") or ""),
            "order": str(fixture_plan.get("sort_order") or "desc"),
        }
        if fixture_plan.get("sort_by")
        else {},
        "strict_result_columns": True,
        "null_group_policy": "preserve_as_blank",
        "metric_null_policy": "display_zero",
    }
    payload["intent_plan"] = plan
    return payload


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
    expectation: dict[str, Any],
    modules: dict[str, Any],
    v2: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    payload, _ = _prepared_payload(case, modules, reference_date)
    payload = apply_manifest_fixture_plan(payload, expectation)
    pandas_vars = base.with_selected_helper_code(modules, modules["pandas_vars"].build_variables(payload))
    resolved = v2["resolver"].resolve_simple_analysis_contract(payload)
    expected_route = str(expectation.get("expected_route") or "").strip().lower()
    expected_recipe = str(expectation.get("expected_recipe") or "").strip().lower()

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
    analysis_execution_mode = str(executed.get("analysis", {}).get("analysis_execution_mode") or "")
    executor_mode = str(executed.get("analysis", {}).get("execution_mode") or "")
    actual_recipe = str(resolved.get("simple_analysis_contract", {}).get("recipe") or "").strip().lower()
    llm_call_trace = (
        executed.get("trace", {})
        .get("inspection", {})
        .get("fast_path", {})
        .get("llm_calls", {})
    )
    actual_calls = {
        "intent": 0,
        "pandas_generation": len(calls),
        "repair": int(llm_call_trace.get("repair") or 0),
        "answer_generation": 0,
    }
    errors: list[str] = []
    if route != expected_route:
        errors.append(f"expected route {expected_route}, got {route}")
    if expected_recipe and actual_recipe != expected_recipe:
        errors.append(f"expected recipe {expected_recipe}, got {actual_recipe or '<empty>'}")
    expected_executor_mode = str(expectation.get("expected_execution_mode") or "").strip()
    if expected_executor_mode and executor_mode != expected_executor_mode:
        errors.append(f"expected executor mode {expected_executor_mode}, got {executor_mode or '<empty>'}")
    expected_analysis_mode = str(expectation.get("expected_analysis_execution_mode") or "").strip()
    if expected_analysis_mode and analysis_execution_mode != expected_analysis_mode:
        errors.append(
            f"expected analysis execution mode {expected_analysis_mode}, got {analysis_execution_mode or '<empty>'}"
        )
    expected_calls = expectation.get("expected_model_calls")
    if isinstance(expected_calls, dict):
        for name, expected_count in expected_calls.items():
            actual_count = int(actual_calls.get(str(name)) or 0)
            if actual_count != int(expected_count):
                errors.append(f"expected {name} model calls {expected_count}, got {actual_count}")
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
    try:
        answered = v2["answer"].build_answer_response(executed, "분석 결과를 확인했습니다.")
    except Exception as exc:
        answered = executed
        errors.append(f"answer builder raised {type(exc).__name__}: {exc}")
    else:
        if not str(answered.get("answer_message") or "").strip():
            errors.append("missing answer_message")
    return {
        "id": case["id"],
        "question": case["question"],
        "status": "ok" if not errors else "error",
        "expected_route": expected_route,
        "actual_route": route,
        "expected_recipe": expected_recipe,
        "recipe": actual_recipe,
        "expected_execution_mode": expected_executor_mode,
        "execution_mode": executor_mode,
        "expected_analysis_execution_mode": expected_analysis_mode,
        "analysis_execution_mode": analysis_execution_mode,
        "route_reason_codes": resolved.get("simple_analysis_contract", {}).get("eligibility", {}).get("reason_codes", []),
        "model_calls": actual_calls,
        "pandas_model_calls": actual_calls["pandas_generation"],
        "row_count": executed.get("analysis", {}).get("row_count", 0),
        "errors": errors,
    }


def validate_live_case(
    case: dict[str, Any],
    expectation: dict[str, Any],
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
    answer_builder_error = ""
    try:
        answered = v2["answer"].build_answer_response(executed, answer_text)
    except Exception as exc:
        answered = executed
        answer_builder_error = f"answer builder raised {type(exc).__name__}: {exc}"

    expected_route = str(expectation.get("expected_route") or "").strip().lower()
    expected_recipe = str(expectation.get("expected_recipe") or "").strip().lower()
    route = str(answered.get("analysis", {}).get("execution_route") or "")
    status = str(answered.get("analysis", {}).get("status") or "")
    analysis_execution_mode = str(answered.get("analysis", {}).get("analysis_execution_mode") or "")
    executor_mode = str(answered.get("analysis", {}).get("execution_mode") or "")
    actual_recipe = str(resolved.get("simple_analysis_contract", {}).get("recipe") or "").strip().lower()
    call_counts = (
        answered.get("trace", {})
        .get("inspection", {})
        .get("fast_path", {})
        .get("llm_calls", {})
    )
    errors: list[str] = []
    if answer_builder_error:
        errors.append(answer_builder_error)
    if route != expected_route:
        errors.append(f"expected route {expected_route}, got {route}")
    if expected_recipe and actual_recipe != expected_recipe:
        errors.append(f"expected recipe {expected_recipe}, got {actual_recipe or '<empty>'}")
    expected_executor_mode = str(expectation.get("expected_execution_mode") or "").strip()
    if expected_executor_mode and executor_mode != expected_executor_mode:
        errors.append(f"expected executor mode {expected_executor_mode}, got {executor_mode or '<empty>'}")
    expected_analysis_mode = str(expectation.get("expected_analysis_execution_mode") or "").strip()
    if expected_analysis_mode and analysis_execution_mode != expected_analysis_mode:
        errors.append(
            f"expected analysis execution mode {expected_analysis_mode}, got {analysis_execution_mode or '<empty>'}"
        )
    if status != "ok":
        errors.append(f"analysis status={status}: {answered.get('trace', {}).get('errors', [])[-1:]}")
    actual_answer_calls = 1 if answer_prompt else 0
    actual_calls = {
        "intent": 1,
        "pandas_generation": int(call_counts.get("pandas_generation") or 0),
        "repair": int(call_counts.get("repair") or 0),
        "answer_generation": actual_answer_calls,
    }
    expected_calls = expectation.get("expected_live_model_calls")
    if isinstance(expected_calls, dict):
        for name, expected_count in expected_calls.items():
            actual_count = int(actual_calls.get(str(name)) or 0)
            if not _call_count_matches(actual_count, expected_count):
                errors.append(f"expected live {name} model calls {expected_count}, got {actual_count}")
    if not str(answered.get("answer_message") or "").strip():
        errors.append("missing answer_message")
    return {
        "id": case["id"],
        "question": case["question"],
        "status": "ok" if not errors else "error",
        "expected_route": expected_route,
        "actual_route": route,
        "expected_recipe": expected_recipe,
        "recipe": actual_recipe,
        "expected_execution_mode": expected_executor_mode,
        "execution_mode": executor_mode,
        "expected_analysis_execution_mode": expected_analysis_mode,
        "analysis_execution_mode": analysis_execution_mode,
        "route_reason_codes": resolved.get("simple_analysis_contract", {}).get("eligibility", {}).get("reason_codes", []),
        "model_calls": actual_calls,
        "pandas_model_calls": actual_calls["pandas_generation"],
        "answer_model_calls": actual_answer_calls,
        "row_count": answered.get("analysis", {}).get("row_count", 0),
        "errors": errors,
    }


def _call_count_matches(actual: int, expected: Any) -> bool:
    if isinstance(expected, dict):
        minimum = int(expected.get("min") or 0)
        maximum = int(expected.get("max") if expected.get("max") is not None else minimum)
        return minimum <= actual <= maximum
    return actual == int(expected)


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
    manifest = load_route_manifest()
    cases = list(base.representative_cases())
    if args.use_llm:
        cases.append(LIVE_ONLY_CASE)
    if args.ids.strip():
        selected = {int(value.strip()) for value in args.ids.split(",") if value.strip()}
        cases = [case for case in cases if int(case["id"]) in selected]
    if args.limit > 0:
        cases = cases[: args.limit]
    missing_expectations = [int(case["id"]) for case in cases if int(case["id"]) not in manifest]
    if missing_expectations:
        raise ValueError(f"V2 route manifest is missing case ids: {missing_expectations}")
    if args.use_llm:
        metadata_context = base.load_metadata_context(modules)
        llm_config = base.resolve_llm_config()
        results = [
            validate_live_case(
                case,
                manifest[int(case["id"])],
                modules,
                v2,
                metadata_context,
                llm_config,
                args.reference_date,
            )
            for case in cases
        ]
    else:
        results = [
            validate_case(
                case,
                manifest[int(case["id"])],
                modules,
                v2,
                args.reference_date,
            )
            for case in cases
        ]
    failures = [item for item in results if item["status"] != "ok"]
    report = {
        "status": "ok" if not failures else "error",
        "summary": {
            "total": len(results),
            "passed": len(results) - len(failures),
            "failed": len(failures),
            "fast": sum(item["actual_route"] == "fast" for item in results),
            "complex": sum(item["actual_route"] == "complex" for item in results),
            "deterministic_fast": sum(item.get("analysis_execution_mode") == "deterministic_fast" for item in results),
            "deterministic_contract": sum(item.get("analysis_execution_mode") == "deterministic_contract" for item in results),
            "llm_pandas": sum(item.get("analysis_execution_mode") == "llm_pandas" for item in results),
            "pandas_generation_calls": sum(int(item.get("pandas_model_calls") or 0) for item in results),
            "validation_mode": "live_llm_manifest" if args.use_llm else "executable_manifest_fixture",
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
                f"mode={item.get('analysis_execution_mode') or '-':<22} "
                f"calls={item['pandas_model_calls']} rows={item['row_count']} "
                f"reason={item['route_reason_codes']} {item['question']}"
            )
            for error in item["errors"]:
                print(f"     {error}")
        print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
