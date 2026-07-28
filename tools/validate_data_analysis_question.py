# -*- coding: utf-8 -*-
"""Langflow 없이 v5 Data Analysis Flow의 실제 프롬프트 경로를 검증합니다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "langflow_components" / "data_analysis_flow"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import validate_representative_questions as flow_validator  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MongoDB 메타데이터와 v5 Flow 프롬프트를 그대로 사용해 "
            "의도 분석, dummy 조회, pandas 생성/실행, 답변 생성을 검증합니다."
        )
    )
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="검증할 질문입니다. 여러 번 지정하면 입력 순서대로 각각 실행합니다.",
    )
    parser.add_argument(
        "--reference-date",
        default="",
        help="YYYYMMDD 기준일입니다. 비우면 VALIDATION_REFERENCE_DATE 또는 20260701을 사용합니다.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="전체 JSON 결과를 저장할 UTF-8 파일 경로입니다.",
    )
    parser.add_argument(
        "--include-raw-responses",
        action="store_true",
        help="의도·pandas·답변 LLM 원문도 결과 JSON에 포함합니다.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="MongoDB 메타데이터와 LLM 설정만 확인하고 질문은 실행하지 않습니다.",
    )
    args = parser.parse_args()

    if not args.check_only and not args.question:
        parser.error("--question을 한 번 이상 지정하거나 --check-only를 사용하세요.")

    flow_validator.load_dotenv(ROOT / ".env")
    flow_validator.install_lfx_stubs()
    modules = flow_validator.load_flow_modules()
    metadata_context = flow_validator.load_metadata_context(modules)
    llm_config = flow_validator.resolve_llm_config()
    reference_date = (
        args.reference_date.strip()
        or os.getenv("VALIDATION_REFERENCE_DATE", "").strip()
        or "20260701"
    )

    environment = build_environment_summary(metadata_context, llm_config, reference_date)
    results: list[dict[str, Any]] = []
    top_errors: list[dict[str, str]] = []
    if not args.check_only:
        for question in args.question:
            try:
                results.append(
                    validate_question(
                        str(question or "").strip(),
                        modules,
                        metadata_context,
                        llm_config,
                        reference_date,
                        include_raw_responses=bool(args.include_raw_responses),
                    )
                )
            except Exception as exc:
                top_errors.append(
                    {
                        "type": "validation_runtime_error",
                        "question": str(question or "").strip(),
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )

    failed = [item for item in results if item.get("status") != "ok"]
    status = "ok" if not failed and not top_errors else "error"
    report = {
        "status": status,
        "mode": "check_only" if args.check_only else "full_pipeline",
        "environment": environment,
        "results": results,
        "errors": top_errors,
    }
    report = flow_validator.json_safe(report)

    output_text = json.dumps(report, ensure_ascii=False, indent=2)
    print(output_text)
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text + "\n", encoding="utf-8")
    return 0 if status == "ok" else 1


def build_environment_summary(
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    reference_date: str,
) -> dict[str, Any]:
    """비밀값을 제외하고 현재 검증 환경과 읽힌 메타데이터 건수만 보여줍니다."""

    domain_items = _items(metadata_context.get("domain"), "domain_items")
    table_items = _items(metadata_context.get("table"), "table_catalog_items")
    main_items = _items(metadata_context.get("main"), "main_flow_filters")
    return {
        "repository": str(ROOT),
        "reference_date": reference_date,
        "retrieval_mode": "dummy",
        "llm_provider": "gemini",
        "llm_model": str(llm_config.get("model") or ""),
        "llm_temperature": llm_config.get("temperature"),
        "llm_timeout_seconds": llm_config.get("timeout"),
        "secrets": {
            "mongodb_uri_configured": bool(os.getenv("MONGODB_URI", "").strip()),
            "google_api_key_configured": bool(
                os.getenv("LLM_API_KEY", "").strip()
                or os.getenv("GEMINI_API_KEY", "").strip()
                or os.getenv("GOOGLE_API_KEY", "").strip()
            ),
        },
        "mongodb": {
            "database": os.getenv("MONGODB_DATABASE", "datagov").strip() or "datagov",
            "collections": {
                "domain": os.getenv(
                    "MONGODB_DOMAIN_COLLECTION",
                    "agent_v4_domain_items",
                ).strip()
                or "agent_v4_domain_items",
                "table_catalog": os.getenv(
                    "MONGODB_TABLE_CATALOG_COLLECTION",
                    "agent_v4_table_catalog_items",
                ).strip()
                or "agent_v4_table_catalog_items",
                "main_flow_filter": os.getenv(
                    "MONGODB_MAIN_FLOW_FILTER_COLLECTION",
                    "agent_v4_main_flow_filters",
                ).strip()
                or "agent_v4_main_flow_filters",
            },
            "loaded_counts": {
                "domain": len(domain_items),
                "table_catalog": len(table_items),
                "main_flow_filter": len(main_items),
            },
        },
        "prompt_sources": {
            "intent": _prompt_info(FLOW / "03_intent_prompt_template_ko.md"),
            "specialized_intent": _prompt_info(
                FLOW / "specialized_prompt_input_example_ko.md"
            ),
            "pandas": _prompt_info(FLOW / "16_pandas_prompt_template_ko.md"),
            "pandas_repair": _prompt_info(
                FLOW / "17b_pandas_repair_prompt_template_ko.md"
            ),
            "answer": _prompt_info(FLOW / "19_answer_prompt_template_ko.md"),
        },
    }


def validate_question(
    question: str,
    modules: dict[str, Any],
    metadata_context: dict[str, Any],
    llm_config: dict[str, Any],
    reference_date: str,
    *,
    include_raw_responses: bool = False,
) -> dict[str, Any]:
    """실제 v5 컴포넌트 함수와 프롬프트를 Flow 순서대로 실행합니다."""

    if not question:
        raise ValueError("질문이 비어 있습니다.")
    started_at = time.perf_counter()
    payload = flow_validator.build_validation_request(question, modules, reference_date)
    candidates_payload = modules["candidates"].build_metadata_candidates(
        payload,
        metadata_context["domain"],
        metadata_context["table"],
        metadata_context["main"],
    )
    metadata_candidates = candidates_payload.get(
        "metadata_candidates",
        candidates_payload,
    )

    intent_variables = flow_validator.with_specialized_prompt(
        modules["intent_vars"].build_variables(payload, metadata_candidates)
    )
    intent_prompt = flow_validator.render_prompt(
        FLOW / "03_intent_prompt_template_ko.md",
        intent_variables,
    )
    intent_response = flow_validator.call_llm(intent_prompt, llm_config)
    payload = modules["intent"].normalize_intent_plan(
        payload,
        intent_response,
        candidates_payload,
    )
    payload = modules["hydrator"].hydrate_retrieval_jobs(
        payload,
        metadata_context["table"],
        retrieval_mode="dummy",
    )
    payload = modules["validator"].validate_retrieval_payload(payload)
    dummy_bundle = modules["router"].route_retrieval_jobs(payload, "dummy")
    dummy_result = modules["dummy"].retrieve_dummy_data(dummy_bundle)
    payload = modules["merger"].merge_source_retrieval_payloads(
        payload,
        dummy_result,
    )
    payload = modules["adapter"].build_retrieval_payload(payload)

    pandas_variables = modules["pandas_vars"].build_variables(payload)
    pandas_variables = flow_validator.with_selected_helper_code(
        modules,
        pandas_variables,
    )
    pandas_prompt = flow_validator.render_prompt(
        FLOW / "16_pandas_prompt_template_ko.md",
        pandas_variables,
    )
    pandas_response = flow_validator.call_llm(pandas_prompt, llm_config)
    payload = modules["executor"].execute_pandas_with_repair(
        payload,
        pandas_response,
        repair_invoker=lambda prompt: flow_validator.call_llm(prompt, llm_config),
        repair_prompt_template=(
            FLOW / "17b_pandas_repair_prompt_template_ko.md"
        ).read_text(encoding="utf-8"),
        function_case_helper_code=str(
            pandas_variables.get("function_case_helper_code") or ""
        ),
    )

    answer_variables = modules["answer_vars"].build_variables(payload)
    answer_variables["domain_answer_guidance"] = (
        FLOW / "answer_domain_guidance_input_example_ko.md"
    ).read_text(encoding="utf-8")
    answer_prompt = flow_validator.render_prompt(
        FLOW / "19_answer_prompt_template_ko.md",
        answer_variables,
    )
    answer_response = flow_validator.call_llm(answer_prompt, llm_config)
    payload = modules["answer_builder"].build_answer_response(
        payload,
        answer_response,
    )
    display_message = modules["message_adapter"].build_message(payload)
    api_response = modules["api_builder"].build_api_response(
        payload,
        display_message,
    )

    semantic_errors = _semantic_plan_errors(
        question,
        payload.get("intent_plan", {}),
        metadata_candidates,
        metadata_context.get("domain"),
    )
    errors = _pipeline_errors(payload) + semantic_errors
    warnings = [
        item
        for item in payload.get("trace", {}).get("warnings", [])
        if isinstance(item, dict)
    ]
    trace_inspection = payload.get("trace", {}).get("inspection", {})
    pandas_inspection = trace_inspection.get("pandas_execution", {})
    pandas_repair_inspection = trace_inspection.get("pandas_repair", {})
    source_results = [
        {
            "dataset_key": item.get("dataset_key"),
            "source_alias": item.get("source_alias"),
            "status": item.get("status"),
            "row_count": item.get("row_count"),
            "applied_params": item.get("applied_params"),
            "pandas_filters": item.get("pandas_filters"),
        }
        for item in payload.get("source_results", [])
        if isinstance(item, dict)
    ]
    result = {
        "question": question,
        "status": "ok" if not errors else "error",
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "metadata_candidates": {
            "domain_count": len(_items(metadata_candidates, "domain_items")),
            "table_catalog_count": len(
                _items(metadata_candidates, "table_catalog_items")
            ),
            "main_flow_filter_count": len(
                _items(metadata_candidates, "main_flow_filters")
            ),
            "domain_keys": _candidate_keys(
                metadata_candidates,
                "domain_items",
                "section",
                "key",
            ),
            "table_catalog_keys": _candidate_keys(
                metadata_candidates,
                "table_catalog_items",
                "",
                "dataset_key",
            ),
            "main_flow_filter_keys": _candidate_keys(
                metadata_candidates,
                "main_flow_filters",
                "",
                "key",
            ),
        },
        "intent": {
            "prompt_chars": len(intent_prompt),
            "analysis_kind": payload.get("intent_plan", {}).get(
                "analysis_kind",
                "",
            ),
            "request_scope": payload.get("intent_plan", {}).get(
                "request_scope",
                "",
            ),
            "reuse_strategy": payload.get("intent_plan", {}).get(
                "reuse_strategy",
                "",
            ),
            "plan": payload.get("intent_plan", {}),
            "metadata_refs": payload.get("metadata_refs", []),
        },
        "retrieval": {
            "mode": "dummy",
            "source_results": source_results,
        },
        "pandas": {
            "prompt_chars": len(pandas_prompt),
            "status": payload.get("analysis", {}).get("status"),
            "repair_attempted": bool(pandas_repair_inspection.get("attempted")),
            "repair_selected": pandas_repair_inspection.get("selected", ""),
            "generated_code": pandas_inspection.get("generated_code", ""),
            "used_helpers": pandas_inspection.get("used_helpers", []),
            "row_count": payload.get("analysis", {}).get("row_count", 0),
            "columns": payload.get("analysis", {}).get("columns", []),
            "preview_rows": payload.get("data", {}).get("rows", [])[:20],
        },
        "answer": {
            "prompt_chars": len(answer_prompt),
            "message": api_response.get("message", ""),
            "data_mode": api_response.get("data_mode", ""),
        },
        "errors": errors,
        "warnings": warnings,
        "semantic_checks": {
            "status": "ok" if not semantic_errors else "error",
            "errors": semantic_errors,
        },
    }
    if include_raw_responses:
        result["raw_llm_responses"] = {
            "intent": intent_response,
            "pandas": pandas_response,
            "answer": answer_response,
        }
    return flow_validator.json_safe(result)


def _pipeline_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    trace_errors = payload.get("trace", {}).get("errors", [])
    errors.extend(item for item in trace_errors if isinstance(item, dict))
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    if analysis.get("status") != "ok":
        analysis_error = analysis.get("error")
        if isinstance(analysis_error, dict):
            errors.append(analysis_error)
        else:
            errors.append(
                {
                    "type": "pandas_execution_failed",
                    "message": str(analysis_error or "pandas 실행 상태가 ok가 아닙니다."),
                }
            )
    if not payload.get("intent_plan", {}).get("retrieval_jobs"):
        errors.append(
            {
                "type": "missing_retrieval_jobs",
                "message": "의도 분석 결과에 retrieval_jobs가 없습니다.",
            }
        )
    return _unique_dicts(errors)


def _semantic_plan_errors(
    question: str,
    intent_plan: Any,
    metadata_candidates: Any,
    domain_metadata: Any = None,
) -> list[dict[str, Any]]:
    """정상 실행만으로 잡히지 않는 대표적인 의도 계획 의미 오류를 검출합니다."""

    plan = intent_plan if isinstance(intent_plan, dict) else {}
    candidates = metadata_candidates if isinstance(metadata_candidates, dict) else {}
    full_domain = domain_metadata if isinstance(domain_metadata, dict) else candidates
    errors: list[dict[str, Any]] = []
    errors.extend(
        _process_group_expansion_errors(
            question,
            plan,
            candidates,
            full_domain,
        )
    )
    ranking_error = _product_ranking_grain_error(question, plan)
    if ranking_error:
        errors.append(ranking_error)
    uph_selection_error = _unrequested_uph_selection_error(question, plan)
    if uph_selection_error:
        errors.append(uph_selection_error)
    return _unique_dicts(errors)


def _process_group_expansion_errors(
    question: str,
    plan: dict[str, Any],
    candidates: dict[str, Any],
    domain_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    question_upper = str(question or "").upper()
    jobs = [
        item
        for item in plan.get("retrieval_jobs", [])
        if isinstance(item, dict)
    ]
    selected_keys = {
        (
            str(item.get("section") or "").strip(),
            str(item.get("key") or "").strip(),
        )
        for item in _items(candidates, "domain_items")
        if isinstance(item, dict)
    }
    errors: list[dict[str, Any]] = []
    process_group_items = _preferred_process_group_items(
        _items(domain_metadata, "domain_items")
    )
    for item in process_group_items:
        if not isinstance(item, dict) or str(item.get("section") or "") != "process_groups":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        processes = [
            str(value).strip()
            for value in payload.get("processes", [])
            if str(value or "").strip()
        ]
        if not processes:
            continue
        aliases = _unique_text(
            [
                item.get("key"),
                payload.get("display_name"),
                *(payload.get("aliases", []) if isinstance(payload.get("aliases"), list) else []),
            ]
        )
        matched_alias = next(
            (
                alias
                for alias in sorted(aliases, key=len, reverse=True)
                if _alias_in_question(alias, question_upper)
            ),
            "",
        )
        if not matched_alias:
            continue
        explicit_processes = [
            process
            for process in processes
            if _alias_in_question(process, question_upper)
        ]
        if explicit_processes:
            expected_specific = {
                _normalized_text(process)
                for process in explicit_processes
            }
            for job in jobs:
                filters = job.get("filters") if isinstance(job.get("filters"), dict) else {}
                condition = filters.get("OPER_NAME")
                actual_values = _condition_values(condition)
                actual = {_normalized_text(value) for value in actual_values}
                if actual != expected_specific:
                    errors.append(
                        {
                            "type": "specific_process_overexpanded",
                            "message": "단일 세부 공정 질문이 공정 그룹 전체 조건으로 확장됐습니다.",
                            "dataset_key": job.get("dataset_key"),
                            "source_alias": job.get("source_alias"),
                            "expected_oper_names": explicit_processes,
                            "actual_oper_names": actual_values,
                        }
                    )
            continue
        metadata_identity = ("process_groups", str(item.get("key") or "").strip())
        metadata_key = f"{metadata_identity[0]}:{metadata_identity[1]}"
        if metadata_identity not in selected_keys:
            errors.append(
                {
                    "type": "missing_process_group_candidate",
                    "message": (
                        f"질문에 필요한 {metadata_key}가 의도 분석 metadata 후보에 포함되지 않았습니다."
                    ),
                    "matched_alias": matched_alias,
                    "expected_oper_names": processes,
                    "metadata_key": metadata_key,
                }
            )
        expected = {_normalized_text(process) for process in processes}
        for job in jobs:
            filters = job.get("filters") if isinstance(job.get("filters"), dict) else {}
            condition = filters.get("OPER_NAME")
            actual_values = _condition_values(condition)
            actual = {_normalized_text(value) for value in actual_values}
            if not actual or not expected.issubset(actual):
                errors.append(
                    {
                        "type": "unexpanded_process_group",
                        "message": (
                            f"{matched_alias} 공정 그룹이 등록된 세부 공정으로 펼쳐지지 않았습니다."
                        ),
                        "dataset_key": job.get("dataset_key"),
                        "source_alias": job.get("source_alias"),
                        "expected_oper_names": processes,
                        "actual_oper_names": actual_values,
                        "metadata_key": metadata_key,
                    }
                )
    return errors


def _product_ranking_grain_error(
    question: str,
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    question_upper = str(question or "").upper()
    is_product_ranking = (
        "제품별" in question
        or (
            "제품" in question
            and any(
                phrase in question
                for phrase in ("상위", "하위", "가장 많은", "가장 적은")
            )
        )
    )
    explicit_device = any(
        term in question_upper
        for term in ("DEVICE", "DEVICE CODE", "디바이스")
    )
    if not is_product_ranking or explicit_device:
        return None
    resolved = (
        plan.get("resolved_grain_plan")
        if isinstance(plan.get("resolved_grain_plan"), dict)
        else {}
    )
    grain_columns = _unique_text(
        resolved.get("grain_columns")
        or plan.get("output_contract", {}).get("grain_columns")
        or []
    )
    forbidden = [
        column
        for column in grain_columns
        if _normalized_text(column) in {"DEVICE", "DEVICEDESC"}
    ]
    if not forbidden:
        return None
    return {
        "type": "device_in_default_product_grain",
        "message": (
            "DEVICE를 명시하지 않은 제품 집계 질문의 group_by에 DEVICE 계열 컬럼이 포함됐습니다."
        ),
        "forbidden_columns": forbidden,
        "grain_columns": grain_columns,
    }


def _unrequested_uph_selection_error(
    question: str,
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    """UPH를 묻지 않은 장비/Recipe 질문이 UPH dataset과 join recipe를 과선택했는지 검출합니다."""

    if "UPH" in str(question or "").upper():
        return None
    dataset_keys = [
        str(item.get("dataset_key") or "").strip()
        for item in plan.get("retrieval_jobs", [])
        if isinstance(item, dict)
    ]
    metadata_refs = [
        item
        for item in plan.get("metadata_refs", [])
        if isinstance(item, dict)
    ]
    selected_recipe = any(
        str(item.get("section") or "").strip() == "analysis_recipes"
        and str(item.get("key") or "").strip() == "equipment_assignment_uph_join"
        for item in metadata_refs
    )
    if "eqp_uph" not in dataset_keys and not selected_recipe:
        return None
    return {
        "type": "unrequested_uph_join",
        "message": "UPH를 요청하지 않은 질문에 eqp_uph 또는 장비-UPH join recipe가 선택됐습니다.",
        "dataset_keys": dataset_keys,
        "selected_recipe": selected_recipe,
    }


def _condition_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        raw = value.get("value")
    else:
        raw = value
    items = raw if isinstance(raw, list) else [raw]
    return [str(item).strip() for item in items if str(item or "").strip()]


def _preferred_process_group_items(values: list[Any]) -> list[dict[str, Any]]:
    """같은 별칭을 가진 과거 중복 문서 중 canonical key를 우선합니다."""

    items = [
        item
        for item in values
        if isinstance(item, dict)
        and str(item.get("section") or "") == "process_groups"
    ]
    canonical_aliases: set[str] = set()
    for item in items:
        key = str(item.get("key") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        aliases = _unique_text(
            [
                key,
                payload.get("display_name"),
                *(payload.get("aliases", []) if isinstance(payload.get("aliases"), list) else []),
            ]
        )
        if key and not key.upper().endswith(("_PROCESS_GROUP", "_GROUP")):
            canonical_aliases.update(_normalized_text(alias) for alias in aliases)

    result: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("key") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        aliases = _unique_text(
            [
                key,
                payload.get("display_name"),
                *(payload.get("aliases", []) if isinstance(payload.get("aliases"), list) else []),
            ]
        )
        is_legacy_suffix = key.upper().endswith(("_PROCESS_GROUP", "_GROUP"))
        overlaps_canonical = any(
            _normalized_text(alias) in canonical_aliases
            for alias in aliases
        )
        if is_legacy_suffix and overlaps_canonical:
            continue
        result.append(item)
    return result


def _alias_in_question(alias: str, question_upper: str) -> bool:
    alias_upper = str(alias or "").strip().upper()
    if not alias_upper:
        return False
    if re.fullmatch(r"[A-Z0-9]+", alias_upper):
        pattern = rf"(?<![A-Z0-9]){re.escape(alias_upper)}(?![A-Z0-9])"
        return re.search(pattern, question_upper) is not None
    return alias_upper in question_upper


def _candidate_keys(
    value: Any,
    list_key: str,
    section_key: str,
    item_key: str,
) -> list[str]:
    result: list[str] = []
    for item in _items(value, list_key):
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        key = str(item.get(item_key) or payload.get(item_key) or "").strip()
        section = str(item.get(section_key) or "").strip() if section_key else ""
        label = f"{section}:{key}" if section and key else key
        if label and label not in result:
            result.append(label)
    return result


def _normalized_text(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _unique_text(values: Any) -> list[str]:
    result: list[str] = []
    iterable = values if isinstance(values, (list, tuple, set)) else [values]
    for value in iterable:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _items(value: Any, key: str) -> list[Any]:
    if not isinstance(value, dict):
        return []
    items = value.get(key)
    return items if isinstance(items, list) else []


def _prompt_info(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return {
        "path": str(path.relative_to(ROOT)),
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
