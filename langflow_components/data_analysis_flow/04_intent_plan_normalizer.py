# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 의도 계획 정규화기
# 역할: Langflow 에이전트/LLM의 의도 JSON을 표준 의도 계획으로 정규화합니다.
# 주요 입력: 페이로드 (payload) · 필수, 의도 LLM 응답 (llm_response) · 필수, 메타데이터 후보 (metadata_candidates)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: LLM JSON을 추출해 분석 범위, 조건 변경 내역, 조회 작업, pandas 단계와 후속 질문 전략을 표준 형태로 정규화합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

RETIRED_JOB_DETAIL_KEYS = {"row_identity_columns", "context_columns"}
PREVIOUS_RESULT_ALIAS = "previous_result"


# 주요 함수: LLM 의도 결과를 신뢰 가능한 실행 계획 계약으로 정규화합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def normalize_intent_plan(
    payload_value: Any,
    llm_response: Any,
    metadata_candidates_value: Any = None,
) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    plan = parsed.get("intent_plan") if isinstance(parsed.get("intent_plan"), dict) else parsed
    retrieval_jobs = _retrieval_jobs(plan)
    retrieval_jobs, context_date_guard = _apply_context_date_guard(payload, retrieval_jobs)
    pandas_plan = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    function_cases = _function_case_items(plan, retrieval_jobs)
    pandas_plan = _ensure_function_case_steps(function_cases, pandas_plan, retrieval_jobs)
    request_scope = _request_scope(plan, payload)
    reuse_strategy = _reuse_strategy(plan, payload, request_scope)
    if reuse_strategy == "previous_result":
        pandas_plan = _bind_previous_result_alias(pandas_plan)
        function_cases = _bind_previous_result_alias(function_cases)

    metadata_candidates = _metadata_candidates(metadata_candidates_value, payload)
    metadata_refs = _metadata_refs(parsed, plan)
    resolved_grain_plan = _resolve_grain_plan(
        plan,
        metadata_refs,
        metadata_candidates,
        retrieval_jobs,
    )
    resolved_join_plan = _resolve_join_plan(
        plan,
        metadata_refs,
        metadata_candidates,
        retrieval_jobs,
        pandas_plan,
    )

    normalized_plan = deepcopy(plan)
    normalized_plan.pop("pandas_function_case", None)
    normalized_plan.pop("selected_function_cases", None)
    normalized_plan["request_scope"] = request_scope
    normalized_plan["reuse_strategy"] = reuse_strategy
    normalized_plan["condition_resolution"] = _condition_resolution(plan)
    normalized_plan["retrieval_jobs"] = retrieval_jobs
    normalized_plan["pandas_execution_plan"] = pandas_plan
    normalized_plan["output_contract"] = _output_contract(
        plan,
        payload,
        retrieval_jobs,
        metadata_candidates,
        resolved_grain_plan,
        resolved_join_plan,
    )
    if resolved_grain_plan:
        normalized_plan["resolved_grain_plan"] = resolved_grain_plan
    else:
        normalized_plan.pop("resolved_grain_plan", None)
    if resolved_join_plan:
        normalized_plan["resolved_join_plan"] = resolved_join_plan
    else:
        normalized_plan.pop("resolved_join_plan", None)
    if function_cases:
        normalized_plan["pandas_function_cases"] = function_cases
    else:
        normalized_plan.pop("pandas_function_cases", None)

    next_payload = payload
    next_payload["intent_plan"] = normalized_plan
    next_payload["metadata_refs"] = _merge_output_metadata_refs(
        parsed,
        plan,
        _plan_metadata_refs(normalized_plan),
    )
    previous_data_reuse = _uses_previous_data_without_new_retrieval(normalized_plan)
    next_payload.setdefault("trace", {}).setdefault("inspection", {})["intent"] = {
        "stage": "04_intent_plan_normalizer",
        "status": "ok" if retrieval_jobs or previous_data_reuse else "warning",
        "analysis_kind": next_payload["intent_plan"].get("analysis_kind", ""),
        "request_scope": normalized_plan["request_scope"],
        "reuse_strategy": normalized_plan["reuse_strategy"],
        "retrieval_job_count": len(retrieval_jobs),
        "pandas_step_count": len(pandas_plan),
        "previous_data_reuse": previous_data_reuse,
        "decision_reason": parsed.get("trace", {}).get("decision_reason", []) if isinstance(parsed.get("trace"), dict) else [],
        "context_date_guard": context_date_guard,
        "resolved_grain_columns": resolved_grain_plan.get("grain_columns", []) if resolved_grain_plan else [],
        "resolved_join_count": len(resolved_join_plan),
    }
    if not retrieval_jobs and not previous_data_reuse:
        next_payload.setdefault("trace", {}).setdefault("warnings", []).append({"type": "missing_retrieval_jobs", "message": "intent_plan.retrieval_jobs가 비어 있습니다."})
    return next_payload


# 함수 설명: `_request_scope()`는 분석 범위에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _request_scope(plan: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    value = str(plan.get("request_scope") or "").strip()
    allowed = {
        "new_analysis",
        "followup_requery",
        "followup_transform",
        "followup_expand_source",
        "followup_explain",
        "clarification",
    }
    normalized = value if value in allowed else "new_analysis"
    date_hint = _context_date_hint(payload)
    if date_hint.get("requires_clarification") is True:
        return "clarification"
    if normalized == "new_analysis" and date_hint.get("source") == "previous_context":
        return "followup_requery"
    return normalized


# 함수 설명: `_reuse_strategy()`는 의도 계획의 이전 결과 재사용 전략을 허용된 값으로 정규화합니다.
def _reuse_strategy(
    plan: dict[str, Any],
    payload: dict[str, Any] | None = None,
    request_scope: str = "",
) -> str:
    value = str(plan.get("reuse_strategy") or "").strip()
    allowed = {
        "none",
        "previous_result",
        "previous_source",
        "previous_intent_with_new_retrieval",
        "trace_only",
    }
    normalized = value if value in allowed else "none"
    if request_scope == "clarification":
        return "none"
    date_hint = _context_date_hint(payload)
    if (
        normalized == "none"
        and request_scope == "followup_requery"
        and date_hint.get("source") == "previous_context"
    ):
        return "previous_intent_with_new_retrieval"
    return normalized


# 함수 설명: `_context_date_hint()`는 01E가 만든 직전 날짜 상속 힌트만 안전하게 꺼냅니다.
def _context_date_hint(payload: dict[str, Any] | None) -> dict[str, Any]:
    value = payload if isinstance(payload, dict) else {}
    followup_hint = value.get("followup_hint") if isinstance(value.get("followup_hint"), dict) else {}
    changed = followup_hint.get("changed_conditions_hint") if isinstance(followup_hint.get("changed_conditions_hint"), dict) else {}
    date_hint = changed.get("date") if isinstance(changed.get("date"), dict) else {}
    if followup_hint.get("followup_candidate") is not True:
        return {}
    return date_hint


# 함수 설명: `_apply_context_date_guard()`는 `이날/이 일자`를 오늘로 바꾼 LLM DATE 값을 직전 분석의 단일 DATE로 교정합니다.
def _apply_context_date_guard(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
) -> tuple[list[Any], dict[str, Any]]:
    date_hint = _context_date_hint(payload)
    inherited_date = str(date_hint.get("resolved_value") or "").strip()
    if date_hint.get("source") != "previous_context" or not re.fullmatch(r"20\d{6}", inherited_date):
        return retrieval_jobs, {}

    result: list[Any] = []
    corrected_aliases: list[str] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        job = deepcopy(item)
        required_params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
        if "DATE" in required_params and str(required_params.get("DATE") or "").strip() != inherited_date:
            required_params["DATE"] = inherited_date
            job["required_params"] = required_params
            alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            if alias and alias not in corrected_aliases:
                corrected_aliases.append(alias)
        result.append(job)
    return result, {
        "status": "applied" if corrected_aliases else "not_needed",
        "expression": date_hint.get("expression"),
        "resolved_value": inherited_date,
        "corrected_source_aliases": corrected_aliases,
    }


# 함수 설명: `_bind_previous_result_alias()`는 이전 최종 결과 재분석 계획이 MongoDB 로더의 단일 예약 alias를 사용하도록 정규화합니다.
def _bind_previous_result_alias(items: list[Any]) -> list[Any]:
    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        normalized = deepcopy(item)
        normalized["source_alias"] = PREVIOUS_RESULT_ALIAS
        for key in ("left_source_alias", "right_source_alias"):
            if key in normalized:
                normalized[key] = PREVIOUS_RESULT_ALIAS
        result.append(normalized)
    return result


# 함수 설명: `_condition_resolution()`는 이전 조건의 inherited·changed·dropped·new 내역을 표준 구조로 정리합니다.
def _condition_resolution(plan: dict[str, Any]) -> dict[str, Any]:
    value = plan.get("condition_resolution")
    if not isinstance(value, dict):
        return {}
    return {
        key: deepcopy(value.get(key))
        for key in ("inherited", "changed", "dropped", "new")
        if value.get(key) not in (None, "", [], {})
    }


# 함수 설명: `_retrieval_jobs()`는 조회 job을 복사하면서 폐기된 상세 컬럼 계약을 runtime payload에서 제거합니다.
def _retrieval_jobs(plan: dict[str, Any]) -> list[Any]:
    items = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        result.append(
            {
                str(key): deepcopy(value)
                for key, value in item.items()
                if str(key) not in RETIRED_JOB_DETAIL_KEYS
            }
        )
    return result


# 함수 설명: `_output_contract()`는 LLM의 출력 의도를 작은 표준 계약으로 정리하고 상세 조회에만 카탈로그 기본 컬럼을 보완합니다.
def _output_contract(
    plan: dict[str, Any],
    payload: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    metadata_candidates: dict[str, Any] | None = None,
    resolved_grain_plan: dict[str, Any] | None = None,
    resolved_join_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result_mode = str(raw.get("result_mode") or raw.get("mode") or "").strip().lower()
    allowed_modes = {"aggregate", "detail", "entity_list", "scalar", "explanation"}
    if result_mode not in allowed_modes:
        result_mode = ""

    contract = {
        "result_mode": result_mode,
        "required_columns": _string_list(raw.get("required_columns") or raw.get("columns")),
        "grain_columns": _string_list(raw.get("grain_columns") or raw.get("group_by")),
        "metric_columns": _string_list(raw.get("metric_columns") or raw.get("metrics")),
        "null_group_policy": str(raw.get("null_group_policy") or "preserve_as_blank").strip(),
        "metric_null_policy": str(raw.get("metric_null_policy") or "display_zero").strip(),
    }

    # 상세/entity 목록에만 table catalog의 기본 상세 컬럼을 사용합니다.
    # 집계 결과에 LOT_ID 같은 row key가 강제로 추가되지 않도록 result_mode로 범위를 제한합니다.
    if result_mode in {"detail", "entity_list"}:
        contract["required_columns"] = _merge_strings(
            contract["required_columns"],
            _catalog_default_detail_columns(
                payload,
                retrieval_jobs,
                metadata_candidates,
            ),
        )
    elif result_mode == "aggregate" and resolved_grain_plan:
        # 제품별 집계처럼 metadata grain이 선택된 경우 LLM이 DEVICE 같은 추가 차원을
        # source schema에서 임의로 끼워 넣지 못하도록 정확한 물리 컬럼 목록을 계약에 고정합니다.
        contract["grain_columns"] = _string_list(resolved_grain_plan.get("grain_columns"))
        join_value_columns = _merge_strings(
            *[
                _string_list(item.get("right_value_columns"))
                for item in (resolved_join_plan or [])
                if isinstance(item, dict)
            ]
        )
        contract["required_columns"] = _merge_strings(
            contract["grain_columns"],
            contract["metric_columns"],
            join_value_columns,
        )

    return {
        key: value
        for key, value in contract.items()
        if value not in (None, "", [], {})
    }


# 함수 설명: `_catalog_default_detail_columns()`는 선택된 데이터셋의 기본 상세 표시 컬럼 metadata만 모읍니다.
def _catalog_default_detail_columns(
    payload: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    metadata_candidates: dict[str, Any] | None = None,
) -> list[str]:
    candidates = metadata_candidates if isinstance(metadata_candidates, dict) else {}
    if not candidates:
        candidates = payload.get("metadata_candidates") if isinstance(payload.get("metadata_candidates"), dict) else {}
    items = candidates.get("table_catalog_items") if isinstance(candidates.get("table_catalog_items"), list) else []
    selected_keys = {
        str(job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict) and str(job.get("dataset_key") or "").strip()
    }
    if not selected_keys:
        return []
    result: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("payload") if isinstance(item.get("payload"), dict) else item
        dataset_key = str(
            item.get("dataset_key")
            or item.get("key")
            or metadata.get("dataset_key")
            or metadata.get("key")
            or ""
        ).strip()
        if dataset_key not in selected_keys:
            continue
        result = _merge_strings(result, _string_list(metadata.get("default_detail_columns")))
    return result


# 함수 설명: 01D 출력 또는 기존 payload에서 실제 후보 묶음을 꺼내 정규화 단계에서만 사용합니다.
def _metadata_candidates(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    candidate_payload = _payload(value)
    nested = candidate_payload.get("metadata_candidates")
    if isinstance(nested, dict):
        return nested
    if any(
        isinstance(candidate_payload.get(key), list)
        for key in ("domain_items", "table_catalog_items", "main_flow_filters")
    ):
        return candidate_payload
    existing = payload.get("metadata_candidates")
    return deepcopy(existing) if isinstance(existing, dict) else {}


# 함수 설명: LLM이 선택한 메타데이터 참조 목록을 section/key 계약으로만 정리합니다.
def _metadata_refs(parsed: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, str]]:
    raw = parsed.get("metadata_refs", plan.get("metadata_refs", []))
    if not isinstance(raw, list):
        return []
    result: list[dict[str, str]] = []
    for item in raw:
        ref = _metadata_ref(item)
        if ref and ref not in result:
            result.append(ref)
    return result


# 함수 설명: 다양한 참조 표기를 section/key 두 필드로 통일합니다.
def _metadata_ref(value: Any) -> dict[str, str]:
    if isinstance(value, str) and ":" in value:
        section, key = value.split(":", 1)
        value = {"section": section, "key": key}
    if not isinstance(value, dict):
        return {}
    section = str(value.get("section") or value.get("type") or "").strip()
    key = str(value.get("key") or value.get("dataset_key") or "").strip()
    if not section or not key:
        return {}
    if section in {"table_catalog_items", "dataset", "data_catalog"}:
        section = "table_catalog"
    return {"section": section, "key": key}


# 함수 설명: grain/join 계획에 포함된 참조도 trace용 metadata_refs에 빠짐없이 합칩니다.
def _plan_metadata_refs(plan: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    grain = plan.get("grain_plan") if isinstance(plan.get("grain_plan"), dict) else {}
    grain_ref = _metadata_ref(grain.get("metadata_ref"))
    if grain_ref:
        result.append(grain_ref)
    joins = plan.get("join_plan")
    join_items = joins if isinstance(joins, list) else [joins] if isinstance(joins, dict) else []
    for item in join_items:
        ref = _metadata_ref(item.get("metadata_ref")) if isinstance(item, dict) else {}
        if ref and ref not in result:
            result.append(ref)
    return result


# 함수 설명: 기존 trace 소비자가 사용하는 type/section 표기는 보존하면서 새 grain/join 참조만 보충합니다.
def _merge_output_metadata_refs(
    parsed: dict[str, Any],
    plan: dict[str, Any],
    additional_refs: list[dict[str, str]],
) -> list[dict[str, Any]]:
    raw = parsed.get("metadata_refs", plan.get("metadata_refs", []))
    result = deepcopy(raw) if isinstance(raw, list) else []
    existing = {
        (ref.get("section", ""), ref.get("key", ""))
        for ref in (_metadata_ref(item) for item in result)
        if ref
    }
    for item in additional_refs:
        ref = _metadata_ref(item)
        marker = (ref.get("section", ""), ref.get("key", ""))
        if ref and marker not in existing:
            result.append(ref)
            existing.add(marker)
    return result


# 함수 설명: 선택된 metadata grain을 source별 실제 컬럼으로 결정합니다.
def _resolve_grain_plan(
    plan: dict[str, Any],
    metadata_refs: list[dict[str, str]],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = plan.get("grain_plan") if isinstance(plan.get("grain_plan"), dict) else {}
    metadata_ref = _metadata_ref(raw.get("metadata_ref"))
    if not metadata_ref:
        product_refs = [
            ref
            for ref in metadata_refs
            if ref.get("section") in {"product_key_columns", "analysis_recipes"}
        ]
        if len(product_refs) == 1:
            metadata_ref = product_refs[0]
    if not metadata_ref:
        return {}

    metadata_item = _find_metadata_item(candidates, metadata_ref)
    canonical_columns = _metadata_key_columns(metadata_item, candidates)
    if not canonical_columns:
        return {}

    source_alias = str(raw.get("source_alias") or "").strip()
    if not source_alias and retrieval_jobs:
        source_alias = str(
            retrieval_jobs[0].get("source_alias")
            or retrieval_jobs[0].get("dataset_key")
            or ""
        ).strip()
    dataset_key = _dataset_key_for_alias(source_alias, retrieval_jobs)
    table_item = _table_catalog_item(candidates, dataset_key)
    mappings = [
        {
            "canonical_key": column,
            "source_candidates": _mapped_column_candidates(table_item, column),
        }
        for column in canonical_columns
    ]
    grain_columns = [
        mapping["source_candidates"][0]
        for mapping in mappings
        if mapping.get("source_candidates")
    ]
    if not source_alias or not grain_columns:
        return {}
    return {
        "metadata_ref": metadata_ref,
        "source_alias": source_alias,
        "dataset_key": dataset_key,
        "canonical_columns": canonical_columns,
        "column_mappings": mappings,
        "grain_columns": grain_columns,
        "strict": True,
    }


# 함수 설명: 선택된 metadata join 계약을 좌우 source의 실제 key 쌍으로 변환합니다.
def _resolve_join_plan(
    plan: dict[str, Any],
    metadata_refs: list[dict[str, str]],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    pandas_plan: list[Any],
) -> list[dict[str, Any]]:
    raw_joins = plan.get("join_plan")
    join_items = raw_joins if isinstance(raw_joins, list) else [raw_joins] if isinstance(raw_joins, dict) else []
    if not join_items:
        join_steps = [
            item
            for item in pandas_plan
            if isinstance(item, dict)
            and "join" in str(item.get("operation") or item.get("step") or "").lower()
        ]
        product_refs = [
            ref
            for ref in metadata_refs
            if ref.get("section") in {"product_key_columns", "analysis_recipes"}
        ]
        if len(join_steps) == 1 and len(product_refs) == 1:
            step = join_steps[0]
            join_items = [
                {
                    "metadata_ref": product_refs[0],
                    "left_source_alias": step.get("left_source_alias"),
                    "right_source_alias": step.get("right_source_alias"),
                    "join_type": step.get("join_type"),
                    "right_value_columns": step.get("right_value_columns"),
                    "multi_match_policy": step.get("multi_match_policy"),
                }
            ]

    result: list[dict[str, Any]] = []
    for raw in join_items:
        if not isinstance(raw, dict):
            continue
        metadata_ref = _metadata_ref(raw.get("metadata_ref"))
        metadata_item = _find_metadata_item(candidates, metadata_ref)
        metadata_payload = _metadata_payload(metadata_item)
        canonical_keys = _metadata_key_columns(metadata_item, candidates)
        left_alias = str(raw.get("left_source_alias") or "").strip()
        right_alias = str(raw.get("right_source_alias") or "").strip()
        if not left_alias or not right_alias or not canonical_keys:
            continue
        left_dataset = _dataset_key_for_alias(left_alias, retrieval_jobs)
        right_dataset = _dataset_key_for_alias(right_alias, retrieval_jobs)
        left_table = _table_catalog_item(candidates, left_dataset)
        right_table = _table_catalog_item(candidates, right_dataset)
        key_mappings: list[dict[str, Any]] = []
        for key in canonical_keys:
            left_candidates = _mapped_column_candidates(left_table, key)
            right_candidates = _mapped_column_candidates(right_table, key)
            if left_candidates and right_candidates:
                key_mappings.append(
                    {
                        "canonical_key": key,
                        "left_candidates": left_candidates,
                        "right_candidates": right_candidates,
                    }
                )
        if not key_mappings:
            continue
        join_type = str(metadata_payload.get("join_type") or raw.get("join_type") or "left").strip().lower()
        if join_type not in {"left", "inner"}:
            join_type = "left"
        multi_match_policy = str(
            metadata_payload.get("multi_match_policy")
            or raw.get("multi_match_policy")
            or "preserve_rows"
        ).strip()
        if multi_match_policy not in {"collect_unique", "preserve_rows", "first"}:
            multi_match_policy = "preserve_rows"
        canonical_right_value_columns = _string_list(
            raw.get("right_value_columns")
            or metadata_payload.get("right_value_columns")
        )
        right_value_mappings = [
            {
                "canonical_key": column,
                "source_candidates": _mapped_column_candidates(right_table, column),
            }
            for column in canonical_right_value_columns
        ]
        right_value_columns = [
            mapping["source_candidates"][0]
            for mapping in right_value_mappings
            if mapping.get("source_candidates")
        ]
        result.append(
            {
                "metadata_ref": metadata_ref,
                "left_source_alias": left_alias,
                "right_source_alias": right_alias,
                "left_dataset_key": left_dataset,
                "right_dataset_key": right_dataset,
                "join_type": join_type,
                "canonical_keys": [item["canonical_key"] for item in key_mappings],
                "key_mappings": key_mappings,
                "left_keys": [item["left_candidates"][0] for item in key_mappings],
                "right_keys": [item["right_candidates"][0] for item in key_mappings],
                "canonical_right_value_columns": canonical_right_value_columns,
                "right_value_mappings": right_value_mappings,
                "right_value_columns": right_value_columns,
                "null_key_policy": str(
                    metadata_payload.get("null_key_policy")
                    or raw.get("null_key_policy")
                    or "normalize_blank"
                ).strip(),
                "multi_match_policy": multi_match_policy,
                "strict": True,
            }
        )
    return result


# 함수 설명: 참조 section/key와 정확히 일치하는 후보 metadata 문서를 찾습니다.
def _find_metadata_item(
    candidates: dict[str, Any],
    metadata_ref: dict[str, str],
) -> dict[str, Any]:
    if not metadata_ref:
        return {}
    target_section = str(metadata_ref.get("section") or "").strip()
    target_key = str(metadata_ref.get("key") or "").strip()
    collections = (
        ("domain_items", ""),
        ("table_catalog_items", "table_catalog"),
        ("main_flow_filters", "main_flow_filter"),
    )
    for collection_key, default_section in collections:
        items = candidates.get(collection_key)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            payload = _metadata_payload(item)
            section = str(item.get("section") or item.get("type") or default_section).strip()
            key = str(
                item.get("key")
                or item.get("dataset_key")
                or payload.get("key")
                or payload.get("dataset_key")
                or ""
            ).strip()
            if section == target_section and key == target_key:
                return item
    return {}


# 함수 설명: metadata 문서의 업무 payload를 안전하게 꺼냅니다.
def _metadata_payload(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else item


# 함수 설명: product key 또는 recipe metadata에서 canonical grain/join key 목록을 읽습니다.
def _metadata_key_columns(
    item: dict[str, Any],
    candidates: dict[str, Any],
    visited: set[tuple[str, str]] | None = None,
) -> list[str]:
    payload = _metadata_payload(item)
    for key in ("columns", "group_by", "join_keys", "product_key_columns", "grain_columns"):
        values = _string_list(payload.get(key))
        if values:
            return values
    grain_policy = payload.get("grain_policy")
    if isinstance(grain_policy, dict):
        for key in ("columns", "group_by", "join_keys"):
            values = _string_list(grain_policy.get(key))
            if values:
                return values
    reference = _metadata_ref(
        payload.get("join_key_ref")
        or payload.get("product_key_ref")
        or payload.get("grain_ref")
    )
    marker = (reference.get("section", ""), reference.get("key", ""))
    seen = visited or set()
    if reference and marker not in seen:
        return _metadata_key_columns(
            _find_metadata_item(candidates, reference),
            candidates,
            {*seen, marker},
        )
    return []


# 함수 설명: retrieval source alias에 대응하는 catalog dataset_key를 찾습니다.
def _dataset_key_for_alias(
    source_alias: str,
    retrieval_jobs: list[dict[str, Any]],
) -> str:
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias == source_alias:
            return str(job.get("dataset_key") or "").strip()
    return ""


# 함수 설명: 선택된 dataset의 table catalog 후보 문서를 찾습니다.
def _table_catalog_item(candidates: dict[str, Any], dataset_key: str) -> dict[str, Any]:
    if not dataset_key:
        return {}
    return _find_metadata_item(
        candidates,
        {"section": "table_catalog", "key": dataset_key},
    )


# 함수 설명: canonical key를 table catalog의 실제 source column 후보로 변환합니다.
def _mapped_column_candidates(item: dict[str, Any], canonical_key: str) -> list[str]:
    payload = _metadata_payload(item)
    normalized_key = _normalized_column_key(canonical_key)
    result: list[str] = []
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        mapping = payload.get(mapping_name)
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            if _normalized_column_key(key) != normalized_key:
                continue
            result = _merge_strings(result, _string_list(value))
    if not result:
        result.append(str(canonical_key).strip())
    return result


# 함수 설명: MODE/Mode, MCP_NO/MCP NO 같은 canonical 표기 차이를 metadata 매핑 비교용으로 통일합니다.
def _normalized_column_key(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "")).upper()


# 함수 설명: `_string_list()`는 문자열 또는 목록 입력을 순서가 유지되는 중복 없는 컬럼 목록으로 정규화합니다.
def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= 40:
            break
    return result


# 함수 설명: `_merge_strings()`는 여러 컬럼 목록을 첫 등장 순서로 합쳐 작은 출력 계약을 유지합니다.
def _merge_strings(*values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in value:
            if item not in result:
                result.append(item)
            if len(result) >= 40:
                return result
    return result


# 함수 설명: `_uses_previous_data_without_new_retrieval()`는 04 의도 계획 정규화기 처리 중 이전 값·데이터·without·NEW·데이터 조회 관련 값을 계산·변환하는
#        내부 helper입니다.
def _uses_previous_data_without_new_retrieval(plan: dict[str, Any]) -> bool:
    request_scope = str(plan.get("request_scope") or "").strip()
    reuse_strategy = str(plan.get("reuse_strategy") or "").strip()
    if request_scope == "clarification":
        return True
    if request_scope == "followup_explain" and reuse_strategy == "trace_only":
        return True
    return request_scope in {"followup_transform", "followup_expand_source"} and reuse_strategy in {"previous_result", "previous_source", "trace_only"}


# 함수 설명: `_function_case_items()`는 Function Case·항목 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _function_case_items(plan: dict[str, Any], retrieval_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    single = plan.get("pandas_function_case")
    if isinstance(single, dict) and single:
        items.append(deepcopy(single))
    elif isinstance(single, list):
        items.extend(deepcopy(item) for item in single if isinstance(item, dict) and item)
    multiple = plan.get("pandas_function_cases")
    if isinstance(multiple, dict) and multiple:
        items.append(deepcopy(multiple))
    elif isinstance(multiple, list):
        items.extend(deepcopy(item) for item in multiple if isinstance(item, dict) and item)
    return _dedupe_cases([_normalize_case(item, retrieval_jobs) for item in items])


# 함수 설명: `_normalize_case()`는 Function Case의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다.
def _normalize_case(item: dict[str, Any], retrieval_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    case = deepcopy(item)
    if case.get("function_case_key") and not case.get("key"):
        case["key"] = case.get("function_case_key")
    if case.get("case_key") and not case.get("key"):
        case["key"] = case.get("case_key")
    case.pop("case_key", None)
    case.pop("function_case_key", None)
    source_alias = str(case.get("source_alias") or "").strip()
    if not source_alias and retrieval_jobs:
        source_alias = str(retrieval_jobs[0].get("source_alias") or retrieval_jobs[0].get("dataset_key") or "").strip()
    if source_alias:
        case["source_alias"] = source_alias
    if "input_text" in case:
        case["input_text"] = str(case.get("input_text") or "")
    return case


# 함수 설명: `_dedupe_cases()`는 cases의 중복을 제거하고 최초 등장 순서를 유지합니다.
def _dedupe_cases(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in items:
        function_name = str(item.get("function_name") or "").strip()
        case_key = str(item.get("key") or "").strip()
        input_text = str(item.get("input_text") or "").strip()
        source_alias = str(item.get("source_alias") or "").strip()
        if not function_name and not case_key:
            continue
        marker = (function_name, case_key, input_text, source_alias)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


# 함수 설명: `_ensure_function_case_steps()`는 함수·Function Case·steps이 실행·저장 계약을 만족하는지 검사하고 위반 내용을 명시적으로 반환합니다.
def _ensure_function_case_steps(function_cases: list[dict[str, Any]], pandas_plan: list[Any], retrieval_jobs: list[dict[str, Any]]) -> list[Any]:
    if not function_cases:
        return pandas_plan
    existing_steps = [step for step in pandas_plan if isinstance(step, dict) and str(step.get("operation") or "") == "apply_pandas_function_case"]
    steps_to_add = []
    for case in function_cases:
        function_name = str(case.get("function_name") or "").strip()
        case_key = str(case.get("key") or "").strip()
        if not function_name and not case_key:
            continue
        source_alias = str(case.get("source_alias") or "").strip()
        if not source_alias and retrieval_jobs:
            source_alias = str(retrieval_jobs[0].get("source_alias") or retrieval_jobs[0].get("dataset_key") or "").strip()
        input_text = str(case.get("input_text") or "")
        if _has_function_case_step(existing_steps + steps_to_add, function_name, case_key, input_text, source_alias):
            continue
        steps_to_add.append(
            {
                "step": "특화 함수 적용",
                "operation": "apply_pandas_function_case",
                "function_case_key": case_key,
                "function_name": function_name,
                "input_text": input_text,
                "source_alias": source_alias,
            }
        )
    return [*steps_to_add, *pandas_plan]


# 함수 설명: `_has_function_case_step()`는 입력값이 함수·Function Case·STEP 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _has_function_case_step(steps: list[Any], function_name: str, case_key: str, input_text: str, source_alias: str) -> bool:
    for step in steps:
        if not isinstance(step, dict) or str(step.get("operation") or "") != "apply_pandas_function_case":
            continue
        if function_name and str(step.get("function_name") or "") != function_name:
            continue
        if case_key and str(step.get("function_case_key") or step.get("key") or "") != case_key:
            continue
        if input_text and str(step.get("input_text") or "") != input_text:
            continue
        if source_alias and str(step.get("source_alias") or "") != source_alias:
            continue
        return True
    return False


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_json()`는 Message·dict·JSON 문자열에서 Markdown fence를 제거하고 JSON object를 안전하게 추출합니다.
def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = _text_value(value)
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    elif "{" in text and "}" in text:
        text = text[text.find("{") : text.rfind("}") + 1]
    try:
        parsed = json.loads(text)
    except Exception:
        try:
            parsed = json.loads(text, strict=False)
        except Exception:
            parsed = _partial_intent_plan(text)
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: `_partial_intent_plan()`는 LLM 응답이 완전하지 않아도 복구 가능한 의도 계획 필드만 우선 추출합니다.
def _partial_intent_plan(text: str) -> dict[str, Any]:
    plan_text = _extract_json_value(text, "intent_plan")
    if not plan_text:
        return {}
    try:
        plan = json.loads(plan_text)
    except Exception:
        try:
            plan = json.loads(plan_text, strict=False)
        except Exception:
            return {}
    return {"intent_plan": plan} if isinstance(plan, dict) else {}


# 함수 설명: `_extract_json_value()`는 복합 입력이나 응답에서 JSON·값을 찾아 검증 가능한 기본 Python 값으로 변환합니다.
def _extract_json_value(text: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:', text)
    if not match:
        return ""
    start = match.end()
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] not in "{[":
        return ""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


# 함수 설명: `_text_value()`는 Langflow Message/Data에서 실제 문자열 값을 꺼내 공통 텍스트 형식으로 맞춥니다.
def _text_value(value: Any) -> str:
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        for key in ("text", "content", "message", "output"):
            if isinstance(data.get(key), str):
                return data[key]
    return str(value or "")


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class IntentPlanNormalizer(Component):
    display_name = "04 의도 계획 정규화기"
    description = "Langflow 에이전트/LLM의 의도 JSON을 표준 의도 계획으로 정규화합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="의도 LLM 응답", required=True),
        DataInput(
            name="metadata_candidates",
            display_name="메타데이터 후보",
            required=False,
        ),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=normalize_intent_plan(
                getattr(self, "payload", None),
                getattr(self, "llm_response", ""),
                getattr(self, "metadata_candidates", None),
            )
        )
