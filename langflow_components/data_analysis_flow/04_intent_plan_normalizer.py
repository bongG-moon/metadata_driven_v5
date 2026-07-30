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
from datetime import datetime, timedelta
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

RETIRED_JOB_DETAIL_KEYS = {"row_identity_columns", "context_columns"}
PREVIOUS_RESULT_ALIAS = "previous_result"
REFERENCE_MODE_TO_REUSE_STRATEGY = {
    "none": "none",
    "previous_result_rows": "previous_result",
    "previous_result_transform": "previous_result",
    "previous_source": "previous_source",
    "previous_filters": "previous_intent_with_new_retrieval",
    "previous_trace": "trace_only",
}
PANDAS_COLUMN_SCALAR_KEYS = {
    "field",
    "column",
    "agg_column",
    "aggregate_column",
    "aggregation_column",
    "value_column",
    "metric_column",
    "sort_by",
    "order_by",
    "rank_by",
    "rank_column",
    "label_column",
    "order_column",
    "date_column",
    "id_column",
}
PANDAS_COLUMN_LIST_KEYS = {
    "columns",
    "group_by",
    "group_by_columns",
    "group_columns",
    "group_cols",
    "dimension_columns",
    "compare_columns",
    "comparison_columns",
    "select_columns",
    "selected_columns",
    "display_columns",
    "output_columns",
    "required_columns",
    "metric_columns",
    "value_columns",
    "sort_columns",
    "order_columns",
    "partition_by",
}
PANDAS_LEFT_COLUMN_KEYS = {
    "left_key",
    "left_keys",
    "left_columns",
    "left_on",
    "left_metric_column",
}
PANDAS_RIGHT_COLUMN_KEYS = {
    "right_key",
    "right_keys",
    "right_columns",
    "right_on",
    "right_value_columns",
    "right_metric_column",
}
PANDAS_CANONICAL_COLUMN_KEYS = {
    "match_columns",
    "canonical_columns",
    "canonical_keys",
}
FILTER_OPERATOR_ALIASES = {
    "=": "eq",
    "==": "eq",
    "!=": "ne",
    ">": "gt",
    ">=": "ge",
    "gte": "ge",
    "greater_than": "gt",
    "greater_than_or_equal": "ge",
    "<": "lt",
    "<=": "le",
    "lte": "le",
    "less_than": "lt",
    "less_than_or_equal": "le",
    "notin": "not_in",
    "not in": "not_in",
    "startswith": "starts_with",
    "prefix": "starts_with",
    "endswith": "ends_with",
    "suffix": "ends_with",
    "isnull": "is_null",
    "isempty": "is_empty",
    "is_null_or_empty": "null_or_empty",
    "notnull": "not_null",
    "notempty": "not_empty",
    "notblank": "not_blank",
    "is_not_blank": "not_blank",
    "is_not_null_or_empty": "not_blank",
    "is_not_null_and_not_empty": "not_blank",
    "not_null_or_empty": "not_blank",
    "not_null_and_not_empty": "not_blank",
}


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
    metadata_candidates = _metadata_candidates(metadata_candidates_value, payload)
    retrieval_jobs = _retrieval_jobs(plan)
    question = str(
        (payload.get("request") if isinstance(payload.get("request"), dict) else {}).get("question")
        or ""
    ).strip()
    retrieval_jobs, process_group_field_guard = _apply_process_group_filter_fields(
        retrieval_jobs,
        metadata_candidates,
        question,
        align_explicit_scope=not _has_ordered_process_range_case(plan),
    )
    retrieval_jobs, filter_operator_normalization = _normalize_retrieval_filter_operators(
        retrieval_jobs
    )
    retrieval_jobs, context_date_guard = _apply_context_date_guard(
        payload,
        retrieval_jobs,
        metadata_candidates,
    )
    retrieval_jobs, business_time_guard = _apply_business_time_contracts(
        payload,
        retrieval_jobs,
        metadata_candidates,
        question,
    )
    pandas_plan = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    pandas_plan = _rewrite_process_group_plan_descriptions(
        pandas_plan,
        process_group_field_guard,
    )
    function_cases = _function_case_items(
        plan,
        retrieval_jobs,
        metadata_candidates,
    )
    retrieval_jobs, function_owned_filter_normalization = (
        _remove_function_owned_retrieval_filters(
            retrieval_jobs,
            function_cases,
        )
    )
    pandas_plan = _ensure_function_case_steps(function_cases, pandas_plan, retrieval_jobs)
    request_scope = _request_scope(plan, payload)
    reference_mode_resolution = _reference_mode_resolution(plan, payload, request_scope)
    reference_mode = str(reference_mode_resolution.get("mode") or "none")
    request_scope, reference_scope_normalization = _normalize_reference_request_scope(
        request_scope,
        reference_mode,
        retrieval_jobs,
        payload,
    )
    reuse_strategy = _reuse_strategy(reference_mode)
    pandas_plan = _ensure_previous_result_row_match_step(
        pandas_plan,
        retrieval_jobs,
        reference_mode,
        payload,
    )
    if reuse_strategy == "previous_result":
        pandas_plan = _bind_previous_result_alias(pandas_plan, retrieval_jobs)
        function_cases = _bind_previous_result_alias(function_cases, retrieval_jobs)
    metadata_refs = _metadata_refs(parsed, plan)
    resolved_grain_plan = _resolve_grain_plan(
        plan,
        metadata_refs,
        metadata_candidates,
        retrieval_jobs,
    )
    pandas_plan, row_match_guard = _normalize_row_match_steps(
        pandas_plan,
        retrieval_jobs,
        reference_mode,
        payload,
    )
    reference_mode_guard = _validate_reference_mode(
        reference_mode_resolution,
        request_scope,
        retrieval_jobs,
        row_match_guard,
        payload,
    )
    validation_errors = _reference_mode_validation_errors(reference_mode_guard)
    resolved_join_plan = _resolve_join_plan(
        plan,
        metadata_refs,
        metadata_candidates,
        retrieval_jobs,
        pandas_plan,
    )
    condition_resolution = _condition_resolution(
        plan,
        payload,
        metadata_candidates,
        retrieval_jobs,
    )
    (
        retrieval_jobs,
        pandas_plan,
        condition_resolution,
        function_case_execution_contracts,
    ) = _apply_function_case_execution_contracts(
        retrieval_jobs,
        pandas_plan,
        function_cases,
        condition_resolution,
    )
    pandas_plan, pandas_column_normalization = _normalize_pandas_plan_columns(
        pandas_plan,
        metadata_candidates,
        retrieval_jobs,
        resolved_grain_plan,
        resolved_join_plan,
    )
    resolved_reference_join_plan = _resolve_reference_join_plan(
        payload,
        metadata_candidates,
        retrieval_jobs,
        pandas_plan,
        reference_mode,
    )
    resolved_metric_merge_plan = _resolve_metric_merge_plan(
        plan,
        metadata_candidates,
        retrieval_jobs,
        pandas_plan,
        resolved_grain_plan,
        business_time_guard,
    )
    contract_plan = deepcopy(plan)
    contract_plan["pandas_execution_plan"] = pandas_plan
    output_contract = _output_contract(
        contract_plan,
        payload,
        retrieval_jobs,
        metadata_candidates,
        resolved_grain_plan,
        resolved_join_plan,
        resolved_reference_join_plan,
        resolved_metric_merge_plan,
    )
    metric_source_errors = _metric_source_validation_errors(
        output_contract,
        retrieval_jobs,
        business_time_guard,
    )
    if metric_source_errors:
        validation_errors.extend(metric_source_errors)

    normalized_plan = deepcopy(plan)
    normalized_plan.pop("pandas_function_case", None)
    normalized_plan.pop("selected_function_cases", None)
    normalized_plan["request_scope"] = request_scope
    normalized_plan["reference_mode"] = reference_mode
    normalized_plan["reuse_strategy"] = reuse_strategy
    if validation_errors:
        normalized_plan["validation_errors"] = validation_errors
    else:
        normalized_plan.pop("validation_errors", None)
    if reference_scope_normalization.get("reason") == "complete_independent_question":
        condition_resolution.pop("inherited", None)
        condition_resolution.pop("dropped", None)
    normalized_plan["condition_resolution"] = condition_resolution
    normalized_plan["retrieval_jobs"] = retrieval_jobs
    normalized_plan["pandas_execution_plan"] = pandas_plan
    normalized_plan["output_contract"] = output_contract
    if business_time_guard.get("temporal_semantics"):
        normalized_plan["temporal_semantics"] = deepcopy(
            business_time_guard["temporal_semantics"]
        )
    else:
        normalized_plan.pop("temporal_semantics", None)
    if resolved_grain_plan:
        normalized_plan["resolved_grain_plan"] = resolved_grain_plan
    else:
        normalized_plan.pop("resolved_grain_plan", None)
    if resolved_join_plan:
        normalized_plan["resolved_join_plan"] = resolved_join_plan
    else:
        normalized_plan.pop("resolved_join_plan", None)
    if resolved_reference_join_plan:
        normalized_plan["resolved_reference_join_plan"] = resolved_reference_join_plan
    else:
        normalized_plan.pop("resolved_reference_join_plan", None)
    if resolved_metric_merge_plan:
        normalized_plan["resolved_metric_merge_plan"] = resolved_metric_merge_plan
    else:
        normalized_plan.pop("resolved_metric_merge_plan", None)
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
        "status": (
            "error"
            if validation_errors
            else ("ok" if retrieval_jobs or previous_data_reuse else "warning")
        ),
        "analysis_kind": next_payload["intent_plan"].get("analysis_kind", ""),
        "request_scope": normalized_plan["request_scope"],
        "reference_mode": normalized_plan["reference_mode"],
        "reuse_strategy": normalized_plan["reuse_strategy"],
        "retrieval_job_count": len(retrieval_jobs),
        "pandas_step_count": len(pandas_plan),
        "previous_data_reuse": previous_data_reuse,
        "decision_reason": parsed.get("trace", {}).get("decision_reason", []) if isinstance(parsed.get("trace"), dict) else [],
        "context_date_guard": context_date_guard,
        "business_time_guard": business_time_guard,
        "process_group_field_guard": process_group_field_guard,
        "filter_operator_normalization": filter_operator_normalization,
        "function_owned_filter_normalization": function_owned_filter_normalization,
        "function_case_execution_contracts": function_case_execution_contracts,
        "reference_scope_normalization": reference_scope_normalization,
        "reference_mode_guard": reference_mode_guard,
        "row_match_guard": row_match_guard,
        "pandas_column_normalization": pandas_column_normalization,
        "resolved_grain_columns": resolved_grain_plan.get("grain_columns", []) if resolved_grain_plan else [],
        "resolved_join_count": len(resolved_join_plan),
        "resolved_reference_join": bool(resolved_reference_join_plan),
        "resolved_metric_merge": bool(resolved_metric_merge_plan),
        "metric_source_validation_errors": metric_source_errors,
    }
    if not retrieval_jobs and not previous_data_reuse and not validation_errors:
        next_payload.setdefault("trace", {}).setdefault("warnings", []).append({"type": "missing_retrieval_jobs", "message": "intent_plan.retrieval_jobs가 비어 있습니다."})
    return next_payload


# 함수 설명: 후속 계획에 실제 신규 retrieval job이 있으면 requery로, 이전 source만 재사용하면 transform으로 scope를 교정합니다.
def _normalize_reference_request_scope(
    request_scope: str,
    reference_mode: str,
    retrieval_jobs: list[Any],
    payload: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    normalized = request_scope
    reason = ""
    payload = payload if isinstance(payload, dict) else {}
    followup_hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    complete_independent = (
        followup_hint.get("complete_independent_request") is True
        or (
            followup_hint.get("followup_candidate") is False
            and str(followup_hint.get("request_scope_hint") or "") == "new_analysis"
        )
    )
    if (
        retrieval_jobs
        and request_scope
        in {
            "followup_transform",
            "followup_expand_source",
            "followup_explain",
        }
    ):
        normalized = "followup_requery"
        reason = "followup_with_new_retrieval"
    elif (
        retrieval_jobs
        and request_scope == "followup_requery"
        and reference_mode == "none"
        and complete_independent
    ):
        normalized = "new_analysis"
        reason = "complete_independent_question"
    elif (
        not retrieval_jobs
        and request_scope == "followup_requery"
        and reference_mode == "previous_source"
    ):
        normalized = "followup_transform"
        reason = "followup_reuses_previous_source_without_retrieval"
    return normalized, {
        "status": "adjusted" if normalized != request_scope else "unchanged",
        "input_request_scope": request_scope,
        "request_scope": normalized,
        "reference_mode": reference_mode,
        "retrieval_job_count": len(retrieval_jobs),
        "reason": reason,
    }


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


# 함수 설명: `_reference_mode_resolution()`은 LLM이 판단한 이전 상태 사용 의미를 표준 mode로 정규화합니다.
def _reference_mode_resolution(
    plan: dict[str, Any],
    payload: dict[str, Any] | None = None,
    request_scope: str = "",
) -> dict[str, Any]:
    raw_mode = str(plan.get("reference_mode") or "").strip()
    payload = payload if isinstance(payload, dict) else {}
    followup_hint = (
        payload.get("followup_hint")
        if isinstance(payload.get("followup_hint"), dict)
        else {}
    )
    if (
        raw_mode == "none"
        and request_scope
        in {
            "followup_requery",
            "followup_transform",
            "followup_expand_source",
        }
        and followup_hint.get("followup_candidate") is True
        and str(followup_hint.get("reuse_strategy_hint") or "").strip()
        == "previous_source"
        and not _retrieval_jobs(plan)
    ):
        reusable_aliases = {
            str(alias).strip()
            for alias in _string_list(
                followup_hint.get("reusable_previous_source_aliases")
            )
            if str(alias).strip()
        }
        planned_aliases: set[str] = set()
        for step in (
            plan.get("pandas_execution_plan")
            if isinstance(plan.get("pandas_execution_plan"), list)
            else []
        ):
            if not isinstance(step, dict):
                continue
            for key in ("source_alias", "left_source_alias", "right_source_alias"):
                alias = str(step.get(key) or "").strip()
                if alias:
                    planned_aliases.add(alias)
        if reusable_aliases.intersection(planned_aliases):
            return {
                "mode": "previous_source",
                "source": "followup_contract_completion",
                "input": raw_mode,
                "issues": [],
            }
    if raw_mode in REFERENCE_MODE_TO_REUSE_STRATEGY:
        return {
            "mode": raw_mode,
            "source": "intent_plan.reference_mode",
            "input": raw_mode,
            "issues": [],
        }
    if raw_mode:
        return {
            "mode": "none",
            "source": "intent_plan.reference_mode",
            "input": raw_mode,
            "issues": ["unsupported_reference_mode"],
        }

    legacy_strategy = str(plan.get("reuse_strategy") or "").strip()
    date_hint = _context_date_hint(payload)
    if (
        legacy_strategy in {"", "none"}
        and request_scope == "followup_requery"
        and date_hint.get("source") == "previous_context"
    ):
        return {
            "mode": "previous_filters",
            "source": "context_date_guard",
            "input": legacy_strategy,
            "issues": [],
        }
    legacy_modes = {
        "none": "none",
        "previous_source": "previous_source",
        "previous_intent_with_new_retrieval": "previous_filters",
        "trace_only": "previous_trace",
    }
    if legacy_strategy == "previous_result":
        legacy_mode = (
            "previous_result_rows"
            if request_scope == "followup_requery"
            else "previous_result_transform"
        )
        return {
            "mode": legacy_mode,
            "source": "legacy_intent_plan.reuse_strategy",
            "input": legacy_strategy,
            "issues": [],
        }
    if legacy_strategy in legacy_modes:
        return {
            "mode": legacy_modes[legacy_strategy],
            "source": "legacy_intent_plan.reuse_strategy",
            "input": legacy_strategy,
            "issues": [],
        }

    return {
        "mode": "none",
        "source": "default",
        "input": "",
        "issues": [],
    }


# 함수 설명: `_reuse_strategy()`는 의미 판단이 끝난 reference mode를 기존 런타임 재사용 전략으로 변환합니다.
def _reuse_strategy(reference_mode: str) -> str:
    return REFERENCE_MODE_TO_REUSE_STRATEGY.get(reference_mode, "none")


# 함수 설명: `_validate_reference_mode()`는 의도 분석의 reference mode와 실행 계획이 서로 같은 의미인지 검증합니다.
def _validate_reference_mode(
    resolution: dict[str, Any],
    request_scope: str,
    retrieval_jobs: list[Any],
    row_match_guard: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    mode = str(resolution.get("mode") or "none")
    issues = _string_list(resolution.get("issues"))
    allowed_scopes = {
        "none": {"new_analysis", "clarification"},
        "previous_result_rows": {"followup_requery"},
        "previous_result_transform": {"followup_transform"},
        "previous_source": {"followup_transform", "followup_expand_source", "followup_requery"},
        "previous_filters": {"followup_requery"},
        "previous_trace": {"followup_explain"},
    }
    if request_scope not in allowed_scopes.get(mode, set()):
        issues.append("reference_mode_scope_mismatch")

    if mode == "previous_result_rows":
        if not retrieval_jobs:
            issues.append("missing_new_retrieval_for_previous_result_rows")
        previous_contract = _previous_result_match_contract(payload)
        if not _string_list(previous_contract.get("match_columns")):
            issues.append("missing_previous_result_grain")
        row_status = str(row_match_guard.get("status") or "").strip()
        if row_status != "applied":
            issues.append("missing_valid_previous_result_row_match")
    elif mode in {"previous_result_transform", "previous_trace"} and retrieval_jobs:
        issues.append("unexpected_new_retrieval_for_reference_mode")
    elif mode == "previous_filters" and not retrieval_jobs:
        issues.append("missing_new_retrieval_for_previous_filters")

    unique_issues: list[str] = []
    for issue in issues:
        if issue and issue not in unique_issues:
            unique_issues.append(issue)
    return {
        "status": "invalid" if unique_issues else "valid",
        "reference_mode": mode,
        "source": str(resolution.get("source") or ""),
        "input": str(resolution.get("input") or ""),
        "request_scope": request_scope,
        "reuse_strategy": _reuse_strategy(mode),
        "issues": unique_issues,
    }


# 함수 설명: `_reference_mode_validation_errors()`는 mode 정합성 오류를 조회 실행 전에 차단할 표준 오류로 변환합니다.
def _reference_mode_validation_errors(guard: dict[str, Any]) -> list[dict[str, Any]]:
    issues = _string_list(guard.get("issues"))
    if not issues:
        return []
    return [
        {
            "type": "invalid_reference_mode_contract",
            "message": "이전 결과 참조 방식과 의도 실행 계획이 일치하지 않습니다.",
            "reference_mode": str(guard.get("reference_mode") or "none"),
            "request_scope": str(guard.get("request_scope") or ""),
            "issues": issues,
        }
    ]


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
    metadata_candidates: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    date_hint = _context_date_hint(payload)
    inherited_date = str(date_hint.get("resolved_value") or "").strip()
    result: list[Any] = []
    corrected_aliases: list[str] = []
    populated_aliases: list[str] = []
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or "")
    reference_date = str(request.get("reference_date") or "").strip()
    current_date_requested = bool(
        re.search(r"(?<![가-힣A-Za-z0-9])(오늘|금일|현재)(?![가-힣A-Za-z0-9])", question)
    )
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        job = deepcopy(item)
        required_params = (
            deepcopy(job.get("required_params"))
            if isinstance(job.get("required_params"), dict)
            else {}
        )
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if (
            date_hint.get("source") == "previous_context"
            and re.fullmatch(r"20\d{6}", inherited_date)
            and "DATE" in required_params
            and str(required_params.get("DATE") or "").strip() != inherited_date
        ):
            required_params["DATE"] = inherited_date
            job["required_params"] = required_params
            if alias and alias not in corrected_aliases:
                corrected_aliases.append(alias)
        elif (
            current_date_requested
            and re.fullmatch(r"20\d{6}", reference_date)
            and _catalog_requires_param(
                metadata_candidates or {},
                str(job.get("dataset_key") or "").strip(),
                "DATE",
            )
            and not any(
                _normalized_column_key(name) == "DATE"
                and str(value or "").strip()
                for name, value in required_params.items()
            )
        ):
            required_params["DATE"] = reference_date
            job["required_params"] = required_params
            if alias and alias not in populated_aliases:
                populated_aliases.append(alias)
        result.append(job)
    guard = {
        "status": "applied" if corrected_aliases or populated_aliases else "not_needed",
        "expression": date_hint.get("expression"),
        "resolved_value": inherited_date or (reference_date if populated_aliases else ""),
        "corrected_source_aliases": corrected_aliases,
    }
    if populated_aliases:
        guard["populated_required_date_aliases"] = populated_aliases
    return result, guard


# 함수 설명: 선택된 Domain의 temporal_semantics를 공통 실행 계약으로 해석해 질문 기준일과 실제 조회일을 분리합니다.
def _apply_business_time_contracts(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
    metadata_candidates: dict[str, Any],
    question: str,
) -> tuple[list[Any], dict[str, Any]]:
    contracts = _selected_temporal_contracts(metadata_candidates)
    if not contracts:
        return retrieval_jobs, {"status": "not_needed"}

    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    reference_date = str(request.get("reference_date") or "").strip()
    requested_date = _requested_question_date(question, reference_date)
    if not re.fullmatch(r"20\d{6}", requested_date):
        return retrieval_jobs, {
            "status": "error",
            "issues": ["unresolved_requested_date"],
            "temporal_semantics": [
                {
                    **deepcopy(contract),
                    "requested_date": requested_date,
                    "query_date": "",
                }
                for contract in contracts
            ],
        }

    jobs = [deepcopy(item) for item in retrieval_jobs if isinstance(item, dict)]
    shared_filters = _shared_retrieval_filters(jobs)
    resolved_semantics: list[dict[str, Any]] = []
    temporal_aliases: list[str] = []
    for contract in contracts:
        dataset_key = str(contract.get("dataset_key") or "").strip()
        date_param = str(contract.get("date_param") or "DATE").strip()
        offset_days = int(contract.get("requested_date_offset_days") or 0)
        query_date = _offset_yyyymmdd(requested_date, offset_days)
        if not dataset_key or not date_param or not query_date:
            return retrieval_jobs, {
                "status": "error",
                "issues": ["invalid_temporal_semantics"],
                "temporal_semantics": [
                    {
                        **deepcopy(contract),
                        "requested_date": requested_date,
                        "query_date": query_date,
                    }
                ],
            }

        disallowed = set(_string_list(contract.get("disallowed_dataset_keys")))
        source_alias_hint = str(contract.get("source_alias") or "").strip()
        existing_index = next(
            (
                index
                for index, item in enumerate(jobs)
                if source_alias_hint
                and str(item.get("source_alias") or "").strip() == source_alias_hint
            ),
            -1,
        )
        if existing_index < 0:
            existing_index = next(
                (
                    index
                    for index, item in enumerate(jobs)
                    if str(item.get("dataset_key") or "").strip() == dataset_key
                ),
                -1,
            )
        if existing_index < 0 and disallowed:
            existing_index = next(
                (
                    index
                    for index, item in enumerate(jobs)
                    if str(item.get("dataset_key") or "").strip() in disallowed
                ),
                -1,
            )

        existing = jobs[existing_index] if existing_index >= 0 else None
        used_aliases = {
            str(item.get("source_alias") or item.get("dataset_key") or "").strip()
            for index, item in enumerate(jobs)
            if index != existing_index
            and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        }
        job = _temporal_contract_job(
            existing,
            dataset_key=dataset_key,
            source_alias_hint=source_alias_hint,
            date_param=date_param,
            query_date=query_date,
            inherited_filters=(
                shared_filters if contract.get("inherit_filters") is True else {}
            ),
            used_aliases=used_aliases,
        )
        if existing_index >= 0:
            jobs[existing_index] = job
        else:
            jobs.append(job)
        selected_alias = str(job.get("source_alias") or "").strip()
        temporal_aliases.append(selected_alias)

        conflict_keys = disallowed | {dataset_key}
        jobs = [
            item
            for item in jobs
            if (
                str(item.get("source_alias") or "").strip() == selected_alias
                or str(item.get("dataset_key") or "").strip() not in conflict_keys
            )
        ]
        resolved_semantics.append(
            {
                **deepcopy(contract),
                "source_alias": selected_alias,
                "requested_date": requested_date,
                "query_date": query_date,
            }
        )

    aligned_aliases: list[str] = []
    temporal_alias_set = set(temporal_aliases)
    temporal_date_params = {
        _normalized_column_key(item.get("date_param"))
        for item in resolved_semantics
        if str(item.get("date_param") or "").strip()
    }
    for index, item in enumerate(jobs):
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        dataset_key = str(item.get("dataset_key") or "").strip()
        aligned_date_param = next(
            (
                param
                for param in _catalog_required_params(
                    metadata_candidates,
                    dataset_key,
                )
                if _normalized_column_key(param) in temporal_date_params
            ),
            "",
        )
        if alias in temporal_alias_set or not aligned_date_param:
            continue
        required_params = (
            deepcopy(item.get("required_params"))
            if isinstance(item.get("required_params"), dict)
            else {}
        )
        if any(
            _normalized_column_key(key)
            == _normalized_column_key(aligned_date_param)
            and str(value or "").strip()
            for key, value in required_params.items()
        ):
            continue
        required_params[aligned_date_param] = requested_date
        jobs[index] = {**item, "required_params": required_params}
        if alias:
            aligned_aliases.append(alias)

    return jobs, {
        "status": "applied",
        "requested_date": requested_date,
        "temporal_source_aliases": temporal_aliases,
        "aligned_requested_date_aliases": aligned_aliases,
        "temporal_semantics": resolved_semantics,
    }


# 함수 설명: 선택된 Domain 후보에 명시된 temporal_semantics만 실행 가능한 공통 시간 계약으로 정규화합니다.
def _selected_temporal_contracts(
    metadata_candidates: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    domain_items = metadata_candidates.get("domain_items")
    for item in domain_items if isinstance(domain_items, list) else []:
        if not isinstance(item, dict):
            continue
        payload = _metadata_payload(item)
        raw_semantics = payload.get("temporal_semantics")
        semantics_items = (
            raw_semantics
            if isinstance(raw_semantics, list)
            else [raw_semantics]
            if isinstance(raw_semantics, dict)
            else []
        )
        for raw in semantics_items:
            if not isinstance(raw, dict):
                continue
            dataset_key = str(
                raw.get("dataset_key")
                or payload.get("dataset_key")
                or payload.get("data_source")
                or ""
            ).strip()
            date_param = str(raw.get("date_param") or "DATE").strip()
            try:
                offset_days = int(raw.get("requested_date_offset_days") or 0)
            except (TypeError, ValueError):
                continue
            if not dataset_key or not date_param or abs(offset_days) > 3660:
                continue
            source_column = str(
                raw.get("source_column")
                or payload.get("metric_column")
                or payload.get("quantity_column")
                or payload.get("column")
                or ""
            ).strip()
            metric = str(raw.get("metric") or source_column).strip()
            result.append(
                {
                    **deepcopy(raw),
                    "domain_ref": {
                        "section": str(item.get("section") or "").strip(),
                        "key": str(item.get("key") or "").strip(),
                    },
                    "dataset_key": dataset_key,
                    "date_param": date_param,
                    "requested_date_offset_days": offset_days,
                    "metric": metric,
                    "source_column": source_column,
                    "aggregation": str(
                        raw.get("aggregation")
                        or payload.get("aggregation_method")
                        or payload.get("aggregation")
                        or "sum"
                    ).strip().lower(),
                    "output_column": str(
                        raw.get("output_column")
                        or payload.get("output_column")
                        or ""
                    ).strip(),
                    "metric_aliases": _merge_strings(
                        _string_list(raw.get("metric_aliases")),
                        _string_list(payload.get("aliases")),
                        _string_list(payload.get("display_name")),
                    ),
                }
            )
    return result


# 함수 설명: 질문에 명시된 날짜를 reference date의 연도와 결합해 YYYYMMDD로 해석합니다.
def _requested_question_date(question: str, reference_date: str) -> str:
    text = str(question or "").strip()
    reference = _parse_yyyymmdd(reference_date)
    compact_match = re.search(r"(?<!\d)(20\d{6})(?!\d)", text)
    if compact_match:
        return compact_match.group(1)
    full_match = re.search(
        r"(?<!\d)(20\d{2})\s*(?:[-/.]|년)\s*(\d{1,2})\s*(?:[-/.]|월)\s*(\d{1,2})(?:\s*일)?",
        text,
    )
    if full_match:
        return _valid_yyyymmdd(
            int(full_match.group(1)),
            int(full_match.group(2)),
            int(full_match.group(3)),
        )
    short_match = re.search(r"(?<!\d)(\d{1,2})\s*(?:/|월)\s*(\d{1,2})\s*일?", text)
    if short_match:
        year = reference.year if reference else datetime.now().year
        return _valid_yyyymmdd(year, int(short_match.group(1)), int(short_match.group(2)))
    if re.search(r"(?<![가-힣A-Za-z0-9])(어제|전일)(?![가-힣A-Za-z0-9])", text):
        return (
            (reference - timedelta(days=1)).strftime("%Y%m%d")
            if reference
            else ""
        )
    if re.search(r"(?<![가-힣A-Za-z0-9])(오늘|금일|현재|현시간)(?![가-힣A-Za-z0-9])", text):
        return reference.strftime("%Y%m%d") if reference else ""
    return reference.strftime("%Y%m%d") if reference else ""


# 함수 설명: 유효한 날짜 구성 요소만 YYYYMMDD로 반환합니다.
def _valid_yyyymmdd(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).strftime("%Y%m%d")
    except ValueError:
        return ""


# 함수 설명: YYYYMMDD 문자열을 datetime으로 안전하게 변환합니다.
def _parse_yyyymmdd(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y%m%d")
    except ValueError:
        return None


# 함수 설명: 요청 기준일에 metadata가 지정한 일수 offset을 적용해 실제 조회일을 계산합니다.
def _offset_yyyymmdd(value: Any, offset_days: int) -> str:
    parsed = _parse_yyyymmdd(value)
    return (
        (parsed + timedelta(days=int(offset_days))).strftime("%Y%m%d")
        if parsed
        else ""
    )


# 함수 설명: temporal 계약이 필터 상속을 명시했을 때 기존 조회 계획의 공통 필터를 보존합니다.
def _shared_retrieval_filters(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    for item in jobs:
        filters = item.get("filters")
        if isinstance(filters, dict) and filters:
            return deepcopy(filters)
    return {}


# 함수 설명: 하나의 temporal 계약에 지정된 dataset, 날짜 파라미터와 고유 alias를 조회 작업에 적용합니다.
def _temporal_contract_job(
    existing: dict[str, Any] | None,
    *,
    dataset_key: str,
    source_alias_hint: str,
    date_param: str,
    query_date: str,
    inherited_filters: dict[str, Any],
    used_aliases: set[str],
) -> dict[str, Any]:
    job = deepcopy(existing) if isinstance(existing, dict) else {}
    old_alias = str(job.get("source_alias") or "").strip()
    default_alias = source_alias_hint or f"{_normalized_alias(dataset_key)}_data"
    alias = old_alias or default_alias
    if not old_alias or alias in used_aliases:
        alias = default_alias
        suffix = 2
        while alias in used_aliases:
            alias = f"{default_alias}_{suffix}"
            suffix += 1
    used_aliases.add(alias)
    required_params = (
        deepcopy(job.get("required_params"))
        if isinstance(job.get("required_params"), dict)
        else {}
    )
    for key in list(required_params):
        if _normalized_column_key(key) == _normalized_column_key(date_param):
            required_params.pop(key, None)
    required_params[date_param] = query_date
    job.update(
        {
            "dataset_key": dataset_key,
            "source_alias": alias,
            "required_params": required_params,
            "filters": (
                deepcopy(job.get("filters"))
                if isinstance(job.get("filters"), dict) and job.get("filters")
                else deepcopy(inherited_filters)
            ),
            "required": True,
        }
    )
    return job


# 함수 설명: dataset key를 새 source alias의 안전한 기본 stem으로 변환합니다.
def _normalized_alias(value: Any) -> str:
    alias = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip()).strip("_").lower()
    return alias or "temporal"


# 함수 설명: `_bind_previous_result_alias()`는 이전 결과만 재분석할 때는 예약 alias로 통일하고 신규 조회가 함께 있으면 명시한 source alias를 보존합니다.
def _bind_previous_result_alias(
    items: list[Any],
    retrieval_jobs: list[Any] | None = None,
) -> list[Any]:
    retrieval_aliases = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in (retrieval_jobs or [])
        if isinstance(item, dict)
        and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    }
    mixed_sources = bool(retrieval_aliases)
    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        normalized = deepcopy(item)
        if not mixed_sources:
            normalized["source_alias"] = PREVIOUS_RESULT_ALIAS
        elif not str(normalized.get("source_alias") or "").strip():
            normalized["source_alias"] = PREVIOUS_RESULT_ALIAS
        for key in ("left_source_alias", "right_source_alias"):
            if key in normalized:
                if not mixed_sources or not str(normalized.get(key) or "").strip():
                    normalized[key] = PREVIOUS_RESULT_ALIAS
        result.append(normalized)
    return result


# 함수 설명: `_ensure_previous_result_row_match_step()`는 직전 결과 행과 단일 신규 source를 연결하는 최소 전처리 단계를 LLM 출력과 무관하게 보장합니다.
def _ensure_previous_result_row_match_step(
    items: list[Any],
    retrieval_jobs: list[Any],
    reference_mode: str,
    payload: dict[str, Any] | None = None,
) -> list[Any]:
    if reference_mode != "previous_result_rows":
        return items
    if any(
        isinstance(item, dict)
        and str(item.get("operation") or "").strip().lower() == "apply_row_match_groups"
        for item in items
    ):
        return items
    aliases = [
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in retrieval_jobs
        if isinstance(item, dict)
        and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    ]
    if len(aliases) != 1:
        return items
    contract = _previous_result_match_contract(payload or {})
    if not _string_list(contract.get("match_columns")):
        return items
    return [
        {
            "operation": "apply_row_match_groups",
            "source_alias": aliases[0],
            "reference_source_alias": PREVIOUS_RESULT_ALIAS,
        },
        *items,
    ]


# 함수 설명: `_normalize_row_match_steps()`는 참조 source의 여러 행을 행 내부 AND·행 사이 OR로 적용할 범용 실행 단계를 표준화합니다.
def _normalize_row_match_steps(
    items: list[Any],
    retrieval_jobs: list[Any],
    reference_mode: str,
    payload: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    retrieval_aliases = [
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in retrieval_jobs
        if isinstance(item, dict)
        and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    ]
    previous_result_contract = _previous_result_match_contract(payload or {})
    normalized_items: list[Any] = []
    normalized_steps: list[dict[str, Any]] = []
    invalid_steps: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            normalized_items.append(deepcopy(item))
            continue
        operation = str(item.get("operation") or "").strip().lower()
        if operation != "apply_row_match_groups":
            normalized_items.append(deepcopy(item))
            continue

        normalized = deepcopy(item)
        source_alias = str(normalized.get("source_alias") or "").strip()
        reference_alias = str(normalized.get("reference_source_alias") or "").strip()
        if not source_alias and len(retrieval_aliases) == 1:
            source_alias = retrieval_aliases[0]
        if not reference_alias and reference_mode == "previous_result_rows":
            reference_alias = PREVIOUS_RESULT_ALIAS
        plan_match_columns = _string_list(
            normalized.get("match_columns")
            or normalized.get("condition_columns")
            or normalized.get("columns")
        )
        if reference_alias == PREVIOUS_RESULT_ALIAS:
            if reference_mode == "previous_result_rows":
                match_columns = _string_list(previous_result_contract.get("match_columns"))
                match_columns_source = str(previous_result_contract.get("source") or "previous_result_grain")
            else:
                match_columns = []
                match_columns_source = "invalid_reference_mode"
        else:
            match_columns = plan_match_columns
            match_columns_source = "plan"
        normalized.update(
            {
                "operation": "apply_row_match_groups",
                "source_alias": source_alias,
                "reference_source_alias": reference_alias,
                "match_columns": match_columns,
                "blank_policy": "normalize_blank",
            }
        )
        for retired_key in (
            "match_key_ref",
            "condition_columns",
            "columns",
            "row_match_groups",
            "key_metadata_ref",
            "metadata_ref",
        ):
            normalized.pop(retired_key, None)

        issue_types: list[str] = []
        if not source_alias:
            issue_types.append("missing_source_alias")
        if not reference_alias:
            issue_types.append("missing_reference_source_alias")
        if source_alias and source_alias == reference_alias:
            issue_types.append("same_source_and_reference_alias")
        if reference_alias == PREVIOUS_RESULT_ALIAS and reference_mode != "previous_result_rows":
            issue_types.append("previous_result_rows_mode_required")
        minimum_columns = 1 if reference_alias == PREVIOUS_RESULT_ALIAS else 2
        if len(match_columns) < minimum_columns:
            issue_types.append(
                "missing_previous_result_grain"
                if reference_alias == PREVIOUS_RESULT_ALIAS
                else "insufficient_match_columns"
            )
        if issue_types:
            invalid_steps.append({"index": index, "issues": issue_types})
        else:
            normalized_steps.append(
                {
                    "index": index,
                    "source_alias": source_alias,
                    "reference_source_alias": reference_alias,
                    "match_columns": match_columns,
                    "match_columns_source": match_columns_source,
                }
            )
        normalized_items.append(normalized)

    status = "not_needed"
    if normalized_steps:
        status = "applied"
    if invalid_steps:
        status = "invalid"
    return normalized_items, {
        "status": status,
        "step_count": len(normalized_steps),
        "steps": normalized_steps,
        "invalid_steps": invalid_steps,
        "blank_policy": "normalize_blank" if normalized_steps or invalid_steps else "",
        "previous_result_match_contract": previous_result_contract,
    }


# 함수 설명: `_previous_result_match_contract()`는 직전 결과를 만든 grain 계약을 후속 row match의 유일한 identity로 재사용합니다.
def _previous_result_match_contract(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    previous_plan = (
        state.get("last_intent_plan")
        if isinstance(state.get("last_intent_plan"), dict)
        else {}
    )
    resolved_grain = (
        previous_plan.get("resolved_grain_plan")
        if isinstance(previous_plan.get("resolved_grain_plan"), dict)
        else {}
    )
    candidates = (
        (
            "previous_result_resolved_grain",
            resolved_grain.get("canonical_columns"),
        ),
        (
            "previous_result_resolved_source_grain",
            resolved_grain.get("grain_columns"),
        ),
        (
            "previous_result_output_contract",
            (
                previous_plan.get("output_contract")
                if isinstance(previous_plan.get("output_contract"), dict)
                else {}
            ).get("grain_columns"),
        ),
    )
    for source, raw_columns in candidates:
        columns = _string_list(raw_columns)
        if columns:
            return {
                "source": source,
                "match_columns": columns,
            }
    followup_hint = (
        payload.get("followup_hint")
        if isinstance(payload.get("followup_hint"), dict)
        else {}
    )
    matched_cues = (
        followup_hint.get("matched_cues")
        if isinstance(followup_hint.get("matched_cues"), dict)
        else {}
    )
    hinted_identifiers = _string_list(
        matched_cues.get("previous_entity_identifiers")
    )
    state_current_data = (
        state.get("current_data")
        if isinstance(state.get("current_data"), dict)
        else {}
    )
    previous_columns = {
        str(column).strip().casefold()
        for column in (
            _string_list(state_current_data.get("columns"))
            or _string_list(state_current_data.get("result_columns"))
        )
        if str(column).strip()
    }
    verified_identifiers = [
        column
        for column in hinted_identifiers
        if not previous_columns or column.casefold() in previous_columns
    ]
    if verified_identifiers:
        return {
            "source": "followup_hint_previous_entity_identifiers",
            "match_columns": verified_identifiers,
        }
    return {
        "source": "previous_result_grain_missing",
        "match_columns": [],
    }


# 함수 설명: `_condition_resolution()`는 이전 조건의 inherited·changed·dropped·new 내역을 표준 구조로 정리합니다.
def _condition_resolution(
    plan: dict[str, Any],
    payload: dict[str, Any] | None = None,
    metadata_candidates: dict[str, Any] | None = None,
    retrieval_jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value = plan.get("condition_resolution")
    if not isinstance(value, dict):
        return {}
    result = {
        key: deepcopy(value.get(key))
        for key in ("inherited", "changed", "dropped", "new")
        if value.get(key) not in (None, "", [], {})
    }
    raw_effective_filters = value.get("effective_filters")
    if str(plan.get("reference_mode") or "").strip() == "previous_source":
        raw_effective_filters = _compile_previous_source_effective_filters(value)
    effective_filters = _effective_filter_contract(
        raw_effective_filters,
        payload or {},
        metadata_candidates or {},
        retrieval_jobs or [],
    )
    if effective_filters:
        result["effective_filters"] = effective_filters
    return result


# 함수 설명: LLM이 inherited/changed/new/dropped에 나눠 둔 previous_source 조건을 최종 실행 filter로 합칩니다.
def _compile_previous_source_effective_filters(
    condition_resolution: dict[str, Any],
) -> dict[str, Any]:
    compiled = deepcopy(
        condition_resolution.get("effective_filters")
        if isinstance(condition_resolution.get("effective_filters"), dict)
        else {}
    )
    inherited = (
        condition_resolution.get("inherited")
        if isinstance(condition_resolution.get("inherited"), dict)
        else {}
    )
    inherited_effective = inherited.get("effective_filters")
    if isinstance(inherited_effective, dict):
        for alias, raw_item in inherited_effective.items():
            if not isinstance(raw_item, dict):
                continue
            existing = compiled.get(alias)
            if not isinstance(existing, dict):
                compiled[alias] = deepcopy(raw_item)
                continue
            merged = deepcopy(raw_item)
            merged.update(existing)
            inherited_filters = (
                raw_item.get("filters")
                if isinstance(raw_item.get("filters"), dict)
                else {}
            )
            existing_filters = (
                existing.get("filters")
                if isinstance(existing.get("filters"), dict)
                else {}
            )
            if inherited_filters or existing_filters:
                merged["filters"] = {
                    **deepcopy(inherited_filters),
                    **deepcopy(existing_filters),
                }
            compiled[alias] = merged

    dropped = (
        condition_resolution.get("dropped")
        if isinstance(condition_resolution.get("dropped"), dict)
        else {}
    )
    dropped_by_alias = dropped.get("filters")
    if isinstance(dropped_by_alias, dict):
        for alias, raw_fields in dropped_by_alias.items():
            current = compiled.get(alias)
            if not isinstance(current, dict):
                continue
            current_filters = (
                current.get("filters")
                if isinstance(current.get("filters"), dict)
                else {}
            )
            if isinstance(raw_fields, dict):
                fields = [str(field) for field in raw_fields]
            elif isinstance(raw_fields, (list, tuple, set)):
                fields = [str(field) for field in raw_fields]
            else:
                fields = [str(raw_fields or "")]
            dropped_markers = {field.casefold() for field in fields if field}
            current["filters"] = {
                key: item
                for key, item in current_filters.items()
                if str(key).casefold() not in dropped_markers
            }
            compiled[alias] = current

    for section_name in ("changed", "new"):
        section = (
            condition_resolution.get(section_name)
            if isinstance(condition_resolution.get(section_name), dict)
            else {}
        )
        filters_by_alias = section.get("filters")
        if not isinstance(filters_by_alias, dict):
            continue
        for alias, raw_filters in filters_by_alias.items():
            if not isinstance(raw_filters, dict):
                continue
            current = compiled.get(alias)
            current = deepcopy(current) if isinstance(current, dict) else {}
            current_filters = (
                current.get("filters")
                if isinstance(current.get("filters"), dict)
                else {}
            )
            current["filters"] = {
                **deepcopy(current_filters),
                **deepcopy(raw_filters),
            }
            compiled[alias] = current
    return compiled


# 함수 설명: LLM이 선택한 previous_source 조건을 alias별 실행 filter와 trusted catalog mapping으로 정규화합니다.
def _effective_filter_contract(
    value: Any,
    payload: dict[str, Any],
    metadata_candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    previous_plan = state.get("last_intent_plan") if isinstance(state.get("last_intent_plan"), dict) else {}
    previous_jobs = previous_plan.get("retrieval_jobs") if isinstance(previous_plan.get("retrieval_jobs"), list) else []
    all_jobs = [*retrieval_jobs, *[item for item in previous_jobs if isinstance(item, dict)]]
    result: dict[str, dict[str, Any]] = {}
    for raw_alias, raw_item in value.items():
        alias = str(raw_alias or "").strip()
        if not alias or not isinstance(raw_item, dict):
            continue
        filters = (
            raw_item.get("filters")
            if isinstance(raw_item.get("filters"), (dict, list))
            else {
                key: item
                for key, item in raw_item.items()
                if str(key) not in {"dataset_key", "filter_mappings", "standard_column_aliases"}
            }
        )
        changes: list[dict[str, Any]] = []
        normalized_filters = _normalize_filter_operator_value(
            filters,
            f"condition_resolution.effective_filters.{alias}",
            changes,
        )
        if normalized_filters in (None, "", [], {}):
            continue
        dataset_key = str(raw_item.get("dataset_key") or "").strip()
        if not dataset_key:
            dataset_key = _dataset_key_for_alias(alias, all_jobs)
        catalog_payload = _metadata_payload(
            _table_catalog_item(metadata_candidates, dataset_key)
        )
        item: dict[str, Any] = {
            "filters": normalized_filters,
        }
        if dataset_key:
            item["dataset_key"] = dataset_key
        for mapping_key in ("filter_mappings", "standard_column_aliases"):
            mapping = catalog_payload.get(mapping_key)
            if isinstance(mapping, dict) and mapping:
                item[mapping_key] = deepcopy(mapping)
        result[alias] = item
    return result


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


# 함수 설명: 공정 그룹 metadata의 canonical field와 processes 값을 기준으로 LLM filter field 오선택을 교정합니다.
# 함수 설명: 모든 retrieval filter operator를 동일한 canonical vocabulary로 정규화합니다.
def _normalize_retrieval_filter_operators(
    retrieval_jobs: list[Any],
) -> tuple[list[Any], dict[str, Any]]:
    """모든 retrieval filter operator를 동일한 canonical vocabulary로 정규화합니다."""
    normalized_jobs: list[Any] = []
    changes: list[dict[str, Any]] = []
    for index, item in enumerate(retrieval_jobs):
        if not isinstance(item, dict):
            normalized_jobs.append(deepcopy(item))
            continue
        job = deepcopy(item)
        if isinstance(job.get("filters"), (dict, list)):
            job["filters"] = _normalize_filter_operator_value(
                job["filters"],
                f"retrieval_jobs[{index}].filters",
                changes,
            )
        normalized_jobs.append(job)
    return normalized_jobs, {
        "status": "applied" if changes else "not_needed",
        "change_count": len(changes),
        "changes": changes,
    }


# 함수 설명: 중첩 filter 조건의 operator만 바꾸고 field/value는 그대로 보존합니다.
def _normalize_filter_operator_value(
    value: Any,
    path: str,
    changes: list[dict[str, Any]],
) -> Any:
    """중첩 filter 조건의 operator만 바꾸고 field/value는 그대로 보존합니다."""
    if isinstance(value, list):
        return [
            _normalize_filter_operator_value(item, f"{path}[{index}]", changes)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)

    normalized = deepcopy(value)
    operator_key = "operator" if "operator" in normalized else ("op" if "op" in normalized else "")
    if operator_key:
        raw_operator = str(normalized.get(operator_key) or "eq").strip()
        canonical = _canonical_filter_operator(raw_operator)
        if canonical != raw_operator or operator_key == "op":
            changes.append(
                {
                    "path": f"{path}.{operator_key}",
                    "from": raw_operator,
                    "to": canonical,
                }
            )
        normalized["operator"] = canonical
        normalized.pop("op", None)
        if canonical == "not_blank":
            normalized.pop("value", None)
            normalized.pop("values", None)

    for key, item in list(normalized.items()):
        if key in {"operator", "op", "value", "values"}:
            continue
        if isinstance(item, (dict, list)):
            normalized[key] = _normalize_filter_operator_value(
                item,
                f"{path}.{key}",
                changes,
            )
    return normalized


# 함수 설명: 여러 filter operator 표기를 Executor가 이해하는 canonical 이름으로 바꿉니다.
def _canonical_filter_operator(value: Any) -> str:
    """여러 filter operator 표기를 Executor가 이해하는 canonical 이름으로 바꿉니다."""
    text = re.sub(r"[\s-]+", "_", str(value or "eq").strip()).lower()
    return FILTER_OPERATOR_ALIASES.get(text, text)


# 함수 설명: 공정 그룹 metadata의 canonical field와 processes 값으로 LLM의 filter field 오선택을 교정합니다.
def _apply_process_group_filter_fields(
    retrieval_jobs: list[Any],
    metadata_candidates: dict[str, Any],
    question: str = "",
    align_explicit_scope: bool = True,
) -> tuple[list[Any], dict[str, Any]]:
    contracts = _process_group_contracts(metadata_candidates)
    if not contracts:
        return retrieval_jobs, {"status": "not_available", "corrections": []}
    requested_processes = _requested_process_scope(question, contracts) if align_explicit_scope else []
    mentioned_group_indexes = (
        _mentioned_process_group_indexes(question, contracts)
        if align_explicit_scope
        else set()
    )
    alignment_scope = _process_filter_alignment_scope(retrieval_jobs)
    preserve_distinct_job_scopes = alignment_scope["has_disjoint_scopes"]

    normalized_jobs: list[Any] = []
    corrections: list[dict[str, Any]] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            normalized_jobs.append(deepcopy(item))
            continue
        job = deepcopy(item)
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        filters = job.get("filters")
        if isinstance(filters, dict):
            normalized_filters = deepcopy(filters)
            for raw_field, condition in filters.items():
                narrowed_condition = (
                    _expand_requested_process_groups_within_condition(
                        condition,
                        contracts,
                        mentioned_group_indexes,
                    )
                    if preserve_distinct_job_scopes
                    else _align_requested_process_condition(
                        condition,
                        contracts,
                        requested_processes,
                    )
                )
                if narrowed_condition != condition:
                    normalized_filters[str(raw_field)] = narrowed_condition
                    corrections.append(
                        {
                            "source_alias": alias,
                            "field": str(raw_field),
                            "correction_type": (
                                "source_process_group_expansion"
                                if preserve_distinct_job_scopes
                                else "specific_process_scope"
                            ),
                            "from_values": _condition_scalar_values(condition),
                            "to_values": _condition_scalar_values(narrowed_condition),
                        }
                    )
                condition = narrowed_condition
                canonical_field, group_keys = _process_group_field_for_condition(
                    raw_field,
                    condition,
                    contracts,
                )
                if not canonical_field or _normalized_column_key(raw_field) == _normalized_column_key(canonical_field):
                    continue
                original_key = str(raw_field)
                original_value = normalized_filters.pop(original_key, deepcopy(condition))
                existing_key = next(
                    (
                        key
                        for key in normalized_filters
                        if _normalized_column_key(key) == _normalized_column_key(canonical_field)
                    ),
                    "",
                )
                if not existing_key:
                    normalized_filters[canonical_field] = original_value
                corrections.append(
                    {
                        "source_alias": alias,
                        "from_field": original_key,
                        "to_field": canonical_field,
                        "process_group_keys": group_keys,
                    }
                )
            job["filters"] = normalized_filters
        elif isinstance(filters, list):
            normalized_filters = []
            for condition in filters:
                normalized = deepcopy(condition)
                if isinstance(condition, dict):
                    narrowed_condition = (
                        _expand_requested_process_groups_within_condition(
                            condition,
                            contracts,
                            mentioned_group_indexes,
                        )
                        if preserve_distinct_job_scopes
                        else _align_requested_process_condition(
                            condition,
                            contracts,
                            requested_processes,
                        )
                    )
                    if narrowed_condition != condition:
                        normalized = narrowed_condition
                        corrections.append(
                            {
                                "source_alias": alias,
                                "field": str(
                                    condition.get("field")
                                    or condition.get("column")
                                    or ""
                                ),
                                "correction_type": (
                                    "source_process_group_expansion"
                                    if preserve_distinct_job_scopes
                                    else "specific_process_scope"
                                ),
                                "from_values": _condition_scalar_values(condition),
                                "to_values": _condition_scalar_values(narrowed_condition),
                            }
                        )
                    condition = normalized
                    raw_field = condition.get("field") or condition.get("column")
                    canonical_field, group_keys = _process_group_field_for_condition(
                        raw_field,
                        condition,
                        contracts,
                    )
                    if canonical_field and _normalized_column_key(raw_field) != _normalized_column_key(canonical_field):
                        normalized["field"] = canonical_field
                        normalized.pop("column", None)
                        corrections.append(
                            {
                                "source_alias": alias,
                                "from_field": str(raw_field or ""),
                                "to_field": canonical_field,
                                "process_group_keys": group_keys,
                            }
                        )
                normalized_filters.append(normalized)
            job["filters"] = normalized_filters
        normalized_jobs.append(job)

    return normalized_jobs, {
        "status": "applied" if corrections else "not_needed",
        "corrections": corrections,
        "value_alignment_mode": (
            "preserve_distinct_job_scopes"
            if preserve_distinct_job_scopes
            else "question_scope_alignment"
        ),
        "job_process_scopes": alignment_scope["job_process_scopes"],
    }


# 함수 설명: 여러 retrieval job에 서로 다른 공정 조건이 있으면 질문 전체 공정 합집합으로 덮어쓰지 않도록 source별 범위를 식별합니다.
def _process_filter_alignment_scope(retrieval_jobs: list[Any]) -> dict[str, Any]:
    job_process_scopes: list[dict[str, Any]] = []
    normalized_scopes: list[set[str]] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            continue
        filters = item.get("filters")
        if isinstance(filters, dict):
            conditions = list(filters.items())
        elif isinstance(filters, list):
            conditions = [
                (
                    condition.get("field") or condition.get("column"),
                    condition,
                )
                for condition in filters
                if isinstance(condition, dict)
            ]
        else:
            conditions = []

        values: list[str] = []
        for raw_field, condition in conditions:
            if _normalized_column_key(raw_field) not in {
                "OPER",
                "OPERNUM",
                "OPERNAME",
                "OPERNM",
            }:
                continue
            values = _merge_strings(values, _condition_scalar_values(condition))
        if not values:
            continue

        normalized_scope = {value.casefold() for value in values}
        if normalized_scope not in normalized_scopes:
            normalized_scopes.append(normalized_scope)
        job_process_scopes.append(
            {
                "source_alias": str(
                    item.get("source_alias") or item.get("dataset_key") or ""
                ).strip(),
                "values": values,
            }
        )
    return {
        "distinct_scope_count": len(normalized_scopes),
        "has_disjoint_scopes": any(
            left.isdisjoint(right)
            for index, left in enumerate(normalized_scopes)
            for right in normalized_scopes[index + 1 :]
        ),
        "job_process_scopes": job_process_scopes,
    }


# 함수 설명: ordered process range helper가 선택된 계획인지 판별해 양 끝 공정 filter로 잘못 축소되는 것을 막습니다.
def _has_ordered_process_range_case(plan: dict[str, Any]) -> bool:
    candidates: list[Any] = []
    for key in ("pandas_function_case", "pandas_function_cases", "selected_function_cases"):
        value = plan.get(key)
        candidates.extend(value if isinstance(value, list) else [value])
    pandas_plan = plan.get("pandas_execution_plan")
    candidates.extend(pandas_plan if isinstance(pandas_plan, list) else [])
    return any(
        isinstance(item, dict)
        and (
            str(item.get("function_name") or "").strip() == "filter_ordered_range"
            or str(item.get("key") or item.get("function_case_key") or "").strip() == "ordered_process_range"
        )
        for item in candidates
    )


# 함수 설명: 후보 Domain에서 공정 그룹별 canonical field와 실제 process 값 계약을 추출합니다.
def _process_group_contracts(metadata_candidates: dict[str, Any]) -> list[dict[str, Any]]:
    items = metadata_candidates.get("domain_items")
    contracts: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or str(item.get("section") or "").strip() != "process_groups":
            continue
        payload = _metadata_payload(item)
        processes = _string_list(payload.get("processes"))
        if not processes:
            continue
        aliases = _merge_strings(
            _string_list(payload.get("aliases")),
            _string_list(
                [
                    payload.get("display_name"),
                    item.get("display_name"),
                    item.get("key"),
                ]
            ),
        )
        contracts.append(
            {
                "key": str(item.get("key") or "").strip(),
                # 기존 운영 문서의 process_groups는 모두 OPER_NAME 계약이므로
                # field 재등록 전 문서도 같은 의미로 안전하게 호환합니다.
                "field": str(payload.get("field") or "OPER_NAME").strip(),
                "aliases": aliases,
                "process_values": processes,
                "processes": {value.casefold() for value in processes},
            }
        )
    return contracts


# 함수 설명: 질문에 명시된 세부 공정명을 공정 그룹 metadata에서 찾고 코드형 공정명 뒤의 한글 표현을 허용합니다.
def _explicit_process_mentions(
    question: str,
    contracts: list[dict[str, Any]],
) -> list[str]:
    text = str(question or "")
    result: list[str] = []
    for contract in contracts:
        for process in contract.get("process_values", []):
            value = str(process or "").strip()
            if not value or value in result:
                continue
            right_boundary = r"(?![0-9A-Za-z])" if re.search(r"[0-9A-Za-z]$", value) else r"(?![0-9A-Za-z가-힣])"
            pattern = rf"(?<![0-9A-Za-z가-힣]){re.escape(value)}{right_boundary}"
            if re.search(pattern, text, flags=re.IGNORECASE):
                result.append(value)
    return result


# 함수 설명: 질문에 직접 명시된 세부 공정과 명시·공유 `공정` 접미사가 적용된 그룹 별칭을 하나의 실행 범위로 합칩니다.
def _requested_process_scope(
    question: str,
    contracts: list[dict[str, Any]],
) -> list[str]:
    result = _explicit_process_mentions(question, contracts)
    text = str(question or "")
    mentioned_group_indexes = _mentioned_process_group_indexes(text, contracts)
    for index, contract in enumerate(contracts):
        if index not in mentioned_group_indexes:
            continue
        result = _merge_strings(result, _string_list(contract.get("process_values")))
    return result


# 함수 설명: metadata group alias의 직접 `공정` 표현과 연결 목록 마지막의 공유 `공정` 접미사를 안전하게 찾습니다.
def _mentioned_process_group_indexes(
    question: str,
    contracts: list[dict[str, Any]],
) -> set[int]:
    text = str(question or "")
    mentioned: set[int] = set()
    alias_to_indexes: dict[str, list[int]] = {}
    alias_values: list[str] = []
    for index, contract in enumerate(contracts):
        for alias in contract.get("aliases", []):
            base_alias = re.sub(r"\s*공정\s*$", "", str(alias or "").strip(), flags=re.IGNORECASE)
            if not base_alias:
                continue
            normalized_alias = base_alias.casefold()
            alias_to_indexes.setdefault(normalized_alias, [])
            if index not in alias_to_indexes[normalized_alias]:
                alias_to_indexes[normalized_alias].append(index)
            if base_alias not in alias_values:
                alias_values.append(base_alias)
            pattern = rf"(?<![0-9A-Za-z가-힣]){re.escape(base_alias)}\s*공정"
            if re.search(pattern, text, flags=re.IGNORECASE):
                mentioned.add(index)

    if len(alias_values) < 2:
        return mentioned
    alias_pattern = "(?:" + "|".join(
        re.escape(alias)
        for alias in sorted(alias_values, key=lambda value: (-len(value), value.casefold()))
    ) + ")"
    connector_pattern = r"\s*(?:,|&|및|와|과)\s*"
    shared_suffix_pattern = re.compile(
        rf"(?<![0-9A-Za-z가-힣])(?P<aliases>{alias_pattern}(?:{connector_pattern}{alias_pattern})+)\s*공정",
        flags=re.IGNORECASE,
    )
    alias_matcher = re.compile(alias_pattern, flags=re.IGNORECASE)
    for sequence_match in shared_suffix_pattern.finditer(text):
        sequence = sequence_match.group("aliases")
        for alias_match in alias_matcher.finditer(sequence):
            for index in alias_to_indexes.get(alias_match.group(0).casefold(), []):
                mentioned.add(index)
    return mentioned


# 함수 설명: 공정 관련 LLM filter를 질문에서 요청한 세부 공정과 공정 그룹의 합집합에 일치시킵니다.
def _align_requested_process_condition(
    condition: Any,
    contracts: list[dict[str, Any]],
    requested_processes: list[str],
) -> Any:
    if not requested_processes or not isinstance(condition, dict):
        return deepcopy(condition)
    operator = str(condition.get("operator") or condition.get("op") or "eq").strip().lower()
    if operator not in {"eq", "in", "=", "=="}:
        return deepcopy(condition)
    values = _condition_scalar_values(condition)
    if not values:
        return deepcopy(condition)
    normalized_values = {value.casefold() for value in values}
    known_processes = set().union(*(contract.get("processes", set()) for contract in contracts))
    known_aliases = {
        str(alias).strip().casefold()
        for contract in contracts
        for alias in contract.get("aliases", [])
        if str(alias or "").strip()
    }
    # 질문에서 실제로 언급된 공정 그룹 범위가 requested_processes로 해석된 경우,
    # LLM이 canonical 세부 공정 대신 등록 alias 자체를 filter 값으로 써도 같은 metadata
    # processes 계약으로 정규화한다. 등록되지 않은 일반 값은 그대로 유지한다.
    if not normalized_values.issubset(known_processes | known_aliases):
        return deepcopy(condition)
    narrowed = deepcopy(condition)
    narrowed["operator"] = "eq" if len(requested_processes) == 1 else "in"
    narrowed.pop("op", None)
    if "values" in narrowed:
        narrowed["values"] = list(requested_processes)
        narrowed.pop("value", None)
    else:
        narrowed["value"] = requested_processes[0] if len(requested_processes) == 1 else list(requested_processes)
    return narrowed


# 함수 설명: dict/list filter 조건에서 비교 가능한 scalar 값 목록을 추출합니다.
def _expand_requested_process_groups_within_condition(
    condition: Any,
    contracts: list[dict[str, Any]],
    mentioned_group_indexes: set[int],
) -> Any:
    """Expand only the process groups that belong to the current source condition."""
    if not mentioned_group_indexes or not isinstance(condition, dict):
        return deepcopy(condition)
    operator = str(condition.get("operator") or condition.get("op") or "eq").strip().lower()
    if operator not in {"eq", "in", "=", "=="}:
        return deepcopy(condition)
    values = _condition_scalar_values(condition)
    if not values:
        return deepcopy(condition)

    expanded_values = list(values)
    changed = False
    for index, contract in enumerate(contracts):
        if index not in mentioned_group_indexes:
            continue
        group_processes = {
            str(value).strip().casefold()
            for value in contract.get("process_values", [])
            if str(value or "").strip()
        }
        group_aliases = {
            str(value).strip().casefold()
            for value in contract.get("aliases", [])
            if str(value or "").strip()
        }
        if not group_processes:
            continue
        current_values = {value.casefold() for value in expanded_values}
        if not current_values.intersection(group_processes | group_aliases):
            continue
        retained_values = [
            value
            for value in expanded_values
            if value.casefold() not in group_processes | group_aliases
        ]
        expanded_values = _merge_strings(
            retained_values,
            _string_list(contract.get("process_values")),
        )
        changed = True

    if not changed or expanded_values == values:
        return deepcopy(condition)
    expanded = deepcopy(condition)
    expanded["operator"] = "eq" if len(expanded_values) == 1 else "in"
    expanded.pop("op", None)
    if "values" in expanded:
        expanded["values"] = expanded_values
        expanded.pop("value", None)
    else:
        expanded["value"] = (
            expanded_values[0] if len(expanded_values) == 1 else expanded_values
        )
    return expanded


# 함수 설명: 단일 filter 조건의 value/values를 중복 없는 문자열 목록으로 정규화합니다.
def _condition_scalar_values(condition: Any) -> list[str]:
    raw_values = (
        condition.get("values", condition.get("value", []))
        if isinstance(condition, dict)
        else condition
    )
    values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
    return [
        str(value).strip()
        for value in values
        if not isinstance(value, (dict, list, tuple, set))
        and str(value or "").strip()
    ]


# 함수 설명: 한 filter 조건의 값이 공정 그룹 processes와 일치할 때 유일한 canonical field를 반환합니다.
def _process_group_field_for_condition(
    raw_field: Any,
    condition: Any,
    contracts: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    if _normalized_column_key(raw_field) not in {"OPER", "OPERNUM", "OPERNAME", "OPERNM"}:
        return "", []
    operator = str(condition.get("operator") or condition.get("op") or "eq").strip().lower() if isinstance(condition, dict) else "eq"
    if operator not in {"eq", "in", "=", "=="}:
        return "", []
    raw_values = (
        condition.get("values", condition.get("value", []))
        if isinstance(condition, dict)
        else condition
    )
    values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
    normalized_values = {
        str(value).strip().casefold()
        for value in values
        if not isinstance(value, (dict, list, tuple, set)) and str(value or "").strip()
    }
    if not normalized_values:
        return "", []

    matches = [
        contract
        for contract in contracts
        if normalized_values.intersection(contract["processes"])
    ]
    covered_values = set().union(*(contract["processes"] for contract in matches))
    if not normalized_values.issubset(covered_values):
        return "", []
    fields = {
        str(contract.get("field") or "").strip()
        for contract in matches
        if str(contract.get("field") or "").strip()
    }
    if len(fields) != 1:
        return "", []
    return next(iter(fields)), [
        str(contract.get("key") or "")
        for contract in matches
        if str(contract.get("key") or "")
    ]


# 함수 설명: filter field 교정 결과를 같은 source의 pandas 계획 설명에도 반영해 표시와 실행 계약을 맞춥니다.
def _rewrite_process_group_plan_descriptions(
    pandas_plan: list[Any],
    guard: dict[str, Any],
) -> list[Any]:
    corrections = guard.get("corrections") if isinstance(guard.get("corrections"), list) else []
    if not corrections:
        return deepcopy(pandas_plan)
    result: list[Any] = []
    for item in pandas_plan:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        normalized = deepcopy(item)
        alias = str(normalized.get("source_alias") or "").strip()
        description = str(normalized.get("description") or "")
        for correction in corrections:
            if alias != str(correction.get("source_alias") or "").strip():
                continue
            from_field = str(correction.get("from_field") or "").strip()
            to_field = str(correction.get("to_field") or "").strip()
            if from_field and to_field:
                description = re.sub(
                    rf"(?<![\w]){re.escape(from_field)}(?![\w])",
                    to_field,
                    description,
                )
        if description:
            normalized["description"] = description
        result.append(normalized)
    return result


# 함수 설명: metric binding과 선택된 Domain 시간 계약이 실제 retrieval job으로 충족되는지 pandas 생성 전에 검증합니다.
def _metric_source_validation_errors(
    output_contract: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    business_time_guard: dict[str, Any],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    aliases = [
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in retrieval_jobs
        if isinstance(item, dict)
    ]
    duplicate_aliases = sorted(
        {
            alias
            for alias in aliases
            if alias and aliases.count(alias) > 1
        }
    )
    if duplicate_aliases:
        errors.append(
            {
                "type": "duplicate_source_alias",
                "message": "retrieval job의 source_alias는 질문 안에서 고유해야 합니다.",
                "source_aliases": duplicate_aliases,
            }
        )

    jobs_by_alias = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in retrieval_jobs
        if isinstance(item, dict)
    }
    binding_issues: list[dict[str, Any]] = []
    for binding in output_contract.get("metric_bindings", []):
        if not isinstance(binding, dict):
            continue
        alias = str(binding.get("source_alias") or "").strip()
        if alias in {PREVIOUS_RESULT_ALIAS, "upstream_result"}:
            continue
        job = jobs_by_alias.get(alias)
        if not isinstance(job, dict):
            binding_issues.append(
                {
                    "output_column": binding.get("output_column"),
                    "source_alias": alias,
                    "issue": "missing_retrieval_job",
                }
            )
            continue
        expected_dataset = str(binding.get("dataset_key") or "").strip()
        actual_dataset = str(job.get("dataset_key") or "").strip()
        if expected_dataset and expected_dataset != actual_dataset:
            binding_issues.append(
                {
                    "output_column": binding.get("output_column"),
                    "source_alias": alias,
                    "issue": "dataset_key_mismatch",
                    "expected_dataset_key": expected_dataset,
                    "actual_dataset_key": actual_dataset,
                }
            )
    if binding_issues:
        errors.append(
            {
                "type": "invalid_metric_source_contract",
                "message": "출력 metric과 조회 source 계약이 일치하지 않습니다.",
                "issues": binding_issues,
            }
        )

    if business_time_guard.get("status") == "error":
        errors.append(
            {
                "type": "invalid_business_time_contract",
                "message": "선택된 도메인의 시간 기준 조회일을 확정하지 못했습니다.",
                "issues": _string_list(business_time_guard.get("issues")),
            }
        )
        return errors
    if business_time_guard.get("status") != "applied":
        return errors

    temporal_issues: list[dict[str, Any]] = []
    for semantics in business_time_guard.get("temporal_semantics", []):
        if not isinstance(semantics, dict):
            continue
        alias = str(semantics.get("source_alias") or "").strip()
        job = jobs_by_alias.get(alias)
        if not isinstance(job, dict):
            temporal_issues.append(
                {
                    "metric": semantics.get("metric"),
                    "source_alias": alias,
                    "issue": "missing_retrieval_job",
                }
            )
            continue
        expected_dataset = str(semantics.get("dataset_key") or "").strip()
        actual_dataset = str(job.get("dataset_key") or "").strip()
        required_params = (
            job.get("required_params")
            if isinstance(job.get("required_params"), dict)
            else {}
        )
        date_param = str(semantics.get("date_param") or "DATE").strip()
        actual_date = next(
            (
                str(value or "").strip()
                for key, value in required_params.items()
                if _normalized_column_key(key)
                == _normalized_column_key(date_param)
            ),
            "",
        )
        expected_date = str(semantics.get("query_date") or "").strip()
        disallowed = set(_string_list(semantics.get("disallowed_dataset_keys")))
        if actual_dataset != expected_dataset or actual_dataset in disallowed:
            temporal_issues.append(
                {
                    "metric": semantics.get("metric"),
                    "source_alias": alias,
                    "issue": "dataset_key_mismatch",
                    "expected_dataset_key": expected_dataset,
                    "actual_dataset_key": actual_dataset,
                }
            )
        if not expected_date or actual_date != expected_date:
            temporal_issues.append(
                {
                    "metric": semantics.get("metric"),
                    "source_alias": alias,
                    "issue": "query_date_mismatch",
                    "expected_query_date": expected_date,
                    "actual_query_date": actual_date,
                }
            )
    if temporal_issues:
        errors.append(
            {
                "type": "invalid_business_time_contract",
                "message": "질문 기준일과 선택된 도메인의 조회일 계약이 일치하지 않습니다.",
                "issues": temporal_issues,
            }
        )
    return errors


# 함수 설명: `_output_contract()`는 LLM의 출력 의도를 작은 표준 계약으로 정리하고 상세 조회에만 카탈로그 기본 컬럼을 보완합니다.
def _output_contract(
    plan: dict[str, Any],
    payload: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    metadata_candidates: dict[str, Any] | None = None,
    resolved_grain_plan: dict[str, Any] | None = None,
    resolved_join_plan: list[dict[str, Any]] | None = None,
    resolved_reference_join_plan: dict[str, Any] | None = None,
    resolved_metric_merge_plan: dict[str, Any] | None = None,
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
    aggregation_outputs = _aggregation_output_contract(plan)
    metric_bindings = _metric_bindings(
        plan,
        retrieval_jobs,
        resolved_reference_join_plan or {},
        resolved_metric_merge_plan or {},
    )
    metric_bindings, suppressed_metric_aliases = _deduplicate_metric_bindings(
        metric_bindings
    )
    contract["required_columns"] = _merge_strings(
        contract["required_columns"],
        aggregation_outputs["all"],
    )
    contract["metric_columns"] = _merge_strings(
        contract["metric_columns"],
        aggregation_outputs["numeric"],
    )
    if metric_bindings:
        bound_outputs = _string_list(
            [item.get("output_column") for item in metric_bindings]
        )
        numeric_aggregation_methods = {
            "sum",
            "mean",
            "avg",
            "average",
            "min",
            "max",
            "count",
            "nunique",
            "median",
            "std",
            "var",
        }
        bound_metric_outputs = _string_list(
            [
                item.get("output_column")
                for item in metric_bindings
                if str(item.get("aggregation") or "").strip().lower()
                in numeric_aggregation_methods
            ]
        )
        bound_source_keys = {
            _normalized_column_key(item.get("source_column"))
            for item in metric_bindings
            if str(item.get("source_column") or "").strip()
        }
        suppressed_keys = {
            _normalized_column_key(item)
            for item in suppressed_metric_aliases
            if str(item or "").strip()
        }
        contract["metric_columns"] = [
            column
            for column in contract["metric_columns"]
            if (
                _normalized_column_key(column) not in suppressed_keys
                and (
                    _normalized_column_key(column) not in bound_source_keys
                    or column in bound_outputs
                )
            )
        ]
        contract["metric_columns"] = _merge_strings(
            contract["metric_columns"],
            bound_metric_outputs,
        )
        contract["required_columns"] = [
            column
            for column in contract["required_columns"]
            if (
                _normalized_column_key(column) not in suppressed_keys
                and (
                    _normalized_column_key(column) not in bound_source_keys
                    or column in bound_outputs
                )
            )
        ]
        contract["metric_bindings"] = metric_bindings
        if suppressed_metric_aliases:
            contract["suppressed_metric_aliases"] = suppressed_metric_aliases
    ordering = _ordering_contract(raw, plan)
    primary_metric = str(raw.get("primary_metric") or "").strip()
    if not primary_metric and ordering:
        primary_metric = str(ordering.get("sort_by") or "").strip()
    if primary_metric:
        contract["primary_metric"] = primary_metric
    if ordering:
        contract["ordering"] = ordering
    column_labels = _column_labels(raw.get("column_labels"))
    if suppressed_metric_aliases:
        suppressed_label_keys = {
            _normalized_column_key(item) for item in suppressed_metric_aliases
        }
        column_labels = {
            key: value
            for key, value in column_labels.items()
            if _normalized_column_key(key) not in suppressed_label_keys
        }
    if column_labels:
        contract["column_labels"] = column_labels
    result_segments = _result_segments(raw.get("result_segments"))
    if len(result_segments) >= 2:
        contract["result_segments"] = result_segments
        contract["segment_column"] = "RESULT_GROUP"
        if any(item.get("operation") in {"top_n", "bottom_n"} for item in result_segments):
            contract["rank_column"] = "RESULT_RANK"
        # 서로 다른 방향의 구간을 합친 결과에는 표 전체 기준의 단일 ordering 계약을 적용할 수 없습니다.
        contract.pop("ordering", None)

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
            aggregation_outputs["all"],
        )

    if resolved_reference_join_plan:
        contract["required_columns"] = _merge_strings(
            _string_list(resolved_reference_join_plan.get("left_columns")),
            contract["required_columns"],
            _string_list(
                [
                    item.get("output_column")
                    for item in resolved_reference_join_plan.get(
                        "aggregations",
                        [],
                    )
                    if isinstance(item, dict)
                ]
            ),
        )

    result_columns = _merge_strings(
        contract["required_columns"],
        contract["grain_columns"],
        contract["metric_columns"],
        _string_list(contract.get("segment_column")),
        _string_list(contract.get("rank_column")),
    )
    if (
        result_mode in {"aggregate", "detail", "entity_list"}
        and result_columns
    ):
        contract["result_columns"] = result_columns
        contract["strict_result_columns"] = True
        labels = (
            contract.get("column_labels")
            if isinstance(contract.get("column_labels"), dict)
            else {}
        )
        allowed_label_keys = {
            _normalized_column_key(column) for column in result_columns
        }
        if labels:
            contract["column_labels"] = {
                key: value
                for key, value in labels.items()
                if _normalized_column_key(key) in allowed_label_keys
            }

    return {
        key: value
        for key, value in contract.items()
        if value not in (None, "", [], {})
    }


# 함수 설명: pandas 집계와 정규화기가 확정한 병합 계획에서 각 출력 metric의 원천 alias·dataset·컬럼을 구성합니다.
def _metric_bindings(
    plan: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    resolved_reference_join_plan: dict[str, Any],
    resolved_metric_merge_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    job_datasets = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): str(
            item.get("dataset_key") or ""
        ).strip()
        for item in retrieval_jobs
        if isinstance(item, dict)
    }
    result: list[dict[str, Any]] = []
    for item in resolved_metric_merge_plan.get("metrics", []):
        if not isinstance(item, dict):
            continue
        binding = _normalized_metric_binding(item, job_datasets)
        if binding:
            result.append(binding)
    for item in resolved_reference_join_plan.get("aggregations", []):
        if not isinstance(item, dict):
            continue
        binding = _normalized_metric_binding(
            {
                **item,
                "source_alias": resolved_reference_join_plan.get(
                    "right_source_alias"
                ),
            },
            job_datasets,
        )
        if binding:
            result.append(binding)

    steps = (
        plan.get("pandas_execution_plan")
        if isinstance(plan.get("pandas_execution_plan"), list)
        else []
    )
    for step in steps:
        if not isinstance(step, dict):
            continue
        source_alias = str(step.get("source_alias") or "").strip()
        aggregations = (
            step.get("aggregations")
            if isinstance(step.get("aggregations"), list)
            else []
        )
        if not aggregations and (
            step.get("agg_column") or step.get("aggregate_column")
        ):
            aggregations = [
                {
                    "source_column": (
                        step.get("agg_column")
                        or step.get("aggregate_column")
                        or step.get("column")
                    ),
                    "aggregation": (
                        step.get("agg_method")
                        or step.get("aggregation")
                        or step.get("method")
                    ),
                    "output_column": (
                        step.get("output_column")
                        or step.get("result_column")
                    ),
                }
            ]
        for raw in aggregations:
            if not isinstance(raw, dict):
                continue
            binding = _normalized_metric_binding(
                {
                    **raw,
                    "source_alias": (
                        raw.get("source_alias") or source_alias
                    ),
                },
                job_datasets,
            )
            if binding:
                result.append(binding)
    return result


# 함수 설명: 다양한 집계 필드명을 하나의 metric binding 계약으로 정규화합니다.
def _normalized_metric_binding(
    value: dict[str, Any],
    job_datasets: dict[str, str],
) -> dict[str, Any]:
    source_alias = str(value.get("source_alias") or "").strip()
    source_column = str(
        value.get("source_column")
        or value.get("column")
        or value.get("agg_column")
        or value.get("aggregate_column")
        or ""
    ).strip()
    aggregation = str(
        value.get("aggregation")
        or value.get("method")
        or value.get("agg_method")
        or "sum"
    ).strip().lower()
    output_column = str(
        value.get("output_column")
        or value.get("result_column")
        or ""
    ).strip()
    if not source_alias or not source_column or not output_column:
        return {}
    return {
        "source_alias": source_alias,
        "dataset_key": str(
            value.get("dataset_key") or job_datasets.get(source_alias) or ""
        ).strip(),
        "source_column": source_column,
        "aggregation": aggregation,
        "output_column": output_column,
    }


# 함수 설명: 동일한 원천 metric에 여러 표시명이 지정되면 최초 출력명만 보존하고 나머지는 제거 대상으로 기록합니다.
def _deduplicate_metric_bindings(
    bindings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    suppressed: list[str] = []
    seen: dict[tuple[str, str, str, str], str] = {}
    for item in bindings:
        marker = (
            str(item.get("source_alias") or "").casefold(),
            str(item.get("dataset_key") or "").casefold(),
            _normalized_column_key(item.get("source_column")),
            str(item.get("aggregation") or "").casefold(),
        )
        output = str(item.get("output_column") or "").strip()
        existing = seen.get(marker)
        if existing:
            if output and output != existing and output not in suppressed:
                suppressed.append(output)
            continue
        seen[marker] = output
        result.append(item)
    return result, suppressed


# 함수 설명: pandas 다중 집계의 명시적 output_column을 최종 결과 계약에 보존합니다.
def _aggregation_output_contract(plan: dict[str, Any]) -> dict[str, list[str]]:
    all_outputs: list[str] = []
    numeric_outputs: list[str] = []
    numeric_methods = {
        "sum",
        "mean",
        "avg",
        "average",
        "min",
        "max",
        "count",
        "nunique",
        "median",
        "std",
        "var",
    }
    steps = (
        plan.get("pandas_execution_plan")
        if isinstance(plan.get("pandas_execution_plan"), list)
        else []
    )
    for step in steps:
        if not isinstance(step, dict):
            continue
        aggregations = (
            step.get("aggregations")
            if isinstance(step.get("aggregations"), list)
            else []
        )
        for aggregation in aggregations:
            if not isinstance(aggregation, dict):
                continue
            output_column = str(
                aggregation.get("output_column")
                or aggregation.get("result_column")
                or ""
            ).strip()
            if not output_column:
                continue
            if output_column not in all_outputs:
                all_outputs.append(output_column)
            method = str(
                aggregation.get("method")
                or aggregation.get("agg_method")
                or ""
            ).strip().lower()
            if method in numeric_methods and output_column not in numeric_outputs:
                numeric_outputs.append(output_column)
    return {
        "all": all_outputs,
        "numeric": numeric_outputs,
    }


# 함수 설명: 단일 정렬 요청을 output contract로 정규화하며, 명시 계약이 없으면 구조화 pandas 정렬 단계만 사용합니다.
def _ordering_contract(raw: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("ordering") if isinstance(raw.get("ordering"), dict) else {}
    if not value:
        steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            operation = str(step.get("operation") or "").strip().lower()
            if operation not in {"sort", "sort_and_top_n", "top_n", "bottom_n"} and not step.get("sort_by"):
                continue
            value = step
            break
    sort_by = str(value.get("sort_by") or value.get("order_by") or value.get("rank_by") or "").strip()
    if not sort_by:
        return {}
    order = str(value.get("order") or value.get("direction") or "").strip().lower()
    if order not in {"asc", "desc"}:
        operation = str(value.get("operation") or "").strip().lower()
        order = "asc" if operation == "bottom_n" else "desc"
    result: dict[str, Any] = {"sort_by": sort_by, "order": order}
    limit = _positive_int(value.get("limit") or value.get("top_n") or value.get("bottom_n"))
    if limit:
        result["limit"] = limit
    return result


# 함수 설명: LLM이 선언한 결과 컬럼 표시명 중 유효한 문자열 매핑만 보존합니다.
def _column_labels(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for column, label in value.items():
        column_text = str(column or "").strip()
        label_text = str(label or "").strip()
        if column_text and label_text:
            result[column_text[:120]] = label_text[:160]
    return result


# 함수 설명: 서로 다른 조건 결과를 한 표에 합칠 때 사용할 구간 표시 계약을 안전한 작은 구조로 정규화합니다.
def _result_segments(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    allowed_operations = {"top_n", "bottom_n", "filter", "comparison"}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        operation = str(item.get("operation") or "").strip().lower()
        if not label or operation not in allowed_operations:
            continue
        normalized: dict[str, Any] = {
            "label": label[:80],
            "operation": operation,
        }
        limit = _positive_int(item.get("limit"))
        if limit:
            normalized["limit"] = limit
        sort_by = str(item.get("sort_by") or "").strip()
        if sort_by:
            normalized["sort_by"] = sort_by
        order = str(item.get("order") or "").strip().lower()
        if order not in {"asc", "desc"}:
            order = "asc" if operation == "bottom_n" else "desc" if operation == "top_n" else ""
        if order:
            normalized["order"] = order
        result.append(normalized)
    return result


# 함수 설명: LLM이 반환한 구간별 요청 건수를 양의 정수로만 보존합니다.
def _positive_int(value: Any) -> int:
    try:
        number = int(value)
    except Exception:
        return 0
    return number if number > 0 else 0


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
                    "left_source_alias": (
                        step.get("left_source_alias")
                        or step.get("source_alias")
                    ),
                    "right_source_alias": (
                        step.get("right_source_alias")
                        or step.get("reference_source_alias")
                    ),
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


# 함수 설명: 직전 결과의 실제 컬럼과 신규 source의 catalog 컬럼을 pandas 생성 전에 확정해 후속 조회의 left join을 결정론적으로 만듭니다.
def _resolve_reference_join_plan(
    payload: dict[str, Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    pandas_plan: list[Any],
    reference_mode: str,
) -> dict[str, Any]:
    if reference_mode != "previous_result_rows" or len(retrieval_jobs) != 1:
        return {}
    right_job = retrieval_jobs[0] if isinstance(retrieval_jobs[0], dict) else {}
    right_alias = str(
        right_job.get("source_alias") or right_job.get("dataset_key") or ""
    ).strip()
    if not right_alias:
        return {}
    row_match_step = next(
        (
            item
            for item in pandas_plan
            if isinstance(item, dict)
            and str(item.get("operation") or "").strip()
            == "apply_row_match_groups"
            and str(item.get("source_alias") or "").strip() == right_alias
            and str(item.get("reference_source_alias") or "").strip()
            == PREVIOUS_RESULT_ALIAS
        ),
        None,
    )
    if not isinstance(row_match_step, dict):
        return {}
    canonical_keys = _string_list(row_match_step.get("match_columns"))
    previous_columns = _previous_result_columns(payload)
    if not canonical_keys or not previous_columns:
        return {}

    right_dataset = str(right_job.get("dataset_key") or "").strip()
    right_table = _table_catalog_item(candidates, right_dataset)
    previous_alias_groups = _previous_result_alias_groups(payload)
    key_mappings: list[dict[str, Any]] = []
    for canonical_key in canonical_keys:
        left_candidates = _merge_strings(
            [canonical_key],
            previous_alias_groups.get(
                _normalized_column_key(canonical_key),
                [],
            ),
        )
        left_column = _first_existing_column(previous_columns, left_candidates)
        right_candidates = _merge_strings(
            _mapped_column_candidates(right_table, canonical_key),
            [canonical_key],
        )
        if not left_column or not right_candidates:
            continue
        key_mappings.append(
            {
                "canonical_key": canonical_key,
                "left_column": left_column,
                "right_candidates": right_candidates,
            }
        )
    if len(key_mappings) != len(canonical_keys):
        return {}

    aggregations = _aggregation_contracts_for_alias(pandas_plan, right_alias)
    if not aggregations:
        return {}
    aggregations = [
        {
            **item,
            "source_candidates": _merge_strings(
                _mapped_column_candidates(
                    right_table,
                    str(item.get("source_column") or "").strip(),
                ),
                _string_list(item.get("source_column")),
            ),
        }
        for item in aggregations
    ]
    return {
        "operation": "enrich_previous_result",
        "left_source_alias": PREVIOUS_RESULT_ALIAS,
        "right_source_alias": right_alias,
        "right_dataset_key": right_dataset,
        "left_columns": previous_columns,
        "key_mappings": key_mappings,
        "aggregations": aggregations,
        "join_type": "left",
        "blank_policy": "normalize_blank",
        "strict": True,
    }


# 함수 설명: temporal 계약이 포함된 다중 metric 조회를 source별로 독립 집계한 뒤 공통 grain으로 병합할 계획을 확정합니다.
def _resolve_metric_merge_plan(
    plan: dict[str, Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    pandas_plan: list[Any],
    resolved_grain_plan: dict[str, Any],
    business_time_guard: dict[str, Any],
) -> dict[str, Any]:
    if business_time_guard.get("status") != "applied":
        return {}
    jobs_by_alias = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in retrieval_jobs
        if isinstance(item, dict)
    }
    raw_output = (
        plan.get("output_contract")
        if isinstance(plan.get("output_contract"), dict)
        else {}
    )
    raw_metrics = _string_list(
        raw_output.get("metric_columns") or raw_output.get("metrics")
    )
    if not raw_metrics:
        grain_keys = {
            _normalized_column_key(value)
            for value in _string_list(raw_output.get("grain_columns"))
        }
        raw_metrics = [
            value
            for value in _string_list(raw_output.get("required_columns"))
            if _normalized_column_key(value) not in grain_keys
        ]
    metrics = _resolved_metric_specs(
        pandas_plan,
        candidates,
        jobs_by_alias,
        business_time_guard,
        raw_metrics,
    )
    metric_aliases = {
        str(item.get("source_alias") or "").strip()
        for item in metrics
        if str(item.get("source_alias") or "").strip()
    }
    temporal_aliases = set(
        _string_list(business_time_guard.get("temporal_source_aliases"))
    )
    if len(metrics) < 2 or len(metric_aliases) < 2 or not metric_aliases.intersection(
        temporal_aliases
    ):
        return {}

    canonical_grain = _string_list(resolved_grain_plan.get("canonical_columns"))
    if not canonical_grain:
        canonical_grain = _string_list(raw_output.get("grain_columns"))
    if not canonical_grain:
        return {}

    grain_mappings: list[dict[str, Any]] = []
    for canonical_column in canonical_grain:
        source_candidates: dict[str, list[str]] = {}
        for alias in sorted(metric_aliases):
            dataset_key = str(jobs_by_alias[alias].get("dataset_key") or "").strip()
            table_item = _table_catalog_item(candidates, dataset_key)
            source_candidates[alias] = _merge_strings(
                _mapped_column_candidates(table_item, canonical_column),
                [canonical_column],
            )
        grain_mappings.append(
            {
                "canonical_column": canonical_column,
                "output_column": canonical_column,
                "source_candidates": source_candidates,
            }
        )
    return {
        "operation": "merge_metric_sources",
        "join_type": "outer",
        "grain_mappings": grain_mappings,
        "metrics": metrics,
        "fill_zero_on_success": True,
        "strict": True,
    }


# 함수 설명: pandas 계획, 선택 Domain metric, temporal 계약, Table Catalog 순으로 source별 metric 계약을 공통 해석합니다.
def _resolved_metric_specs(
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    jobs_by_alias: dict[str, dict[str, Any]],
    business_time_guard: dict[str, Any],
    raw_metrics: list[str],
) -> list[dict[str, Any]]:
    temporal_by_alias = {
        str(item.get("source_alias") or "").strip(): item
        for item in business_time_guard.get("temporal_semantics", [])
        if isinstance(item, dict)
        and str(item.get("source_alias") or "").strip()
    }
    used_outputs: set[str] = set()
    result: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str]] = set()
    for alias, job in jobs_by_alias.items():
        dataset_key = str(job.get("dataset_key") or "").strip()
        table_item = _table_catalog_item(candidates, dataset_key)
        candidates_for_source = _aggregation_contracts_for_alias(pandas_plan, alias)
        candidates_for_source.extend(
            _domain_metric_contracts_for_dataset(candidates, dataset_key)
        )
        temporal = temporal_by_alias.get(alias)
        if isinstance(temporal, dict) and str(
            temporal.get("source_column") or ""
        ).strip():
            candidates_for_source.append(
                {
                    "source_column": temporal.get("source_column"),
                    "aggregation": temporal.get("aggregation") or "sum",
                    "output_column": temporal.get("output_column"),
                    "metric_aliases": temporal.get("metric_aliases"),
                }
            )
        candidates_for_source.extend(
            _schema_metric_contracts(table_item, raw_metrics)
        )

        for contract in candidates_for_source:
            if not isinstance(contract, dict):
                continue
            source_column = str(contract.get("source_column") or "").strip()
            if not source_column:
                continue
            marker = (alias.casefold(), _normalized_column_key(source_column))
            if marker in seen_sources:
                continue
            output_column = _metric_output_column(
                source_column,
                raw_metrics,
                explicit_output=str(contract.get("output_column") or "").strip(),
                aliases=_string_list(contract.get("metric_aliases")),
                used_outputs=used_outputs,
            )
            if not output_column:
                continue
            seen_sources.add(marker)
            used_outputs.add(_normalized_column_key(output_column))
            result.append(
                {
                    "source_alias": alias,
                    "dataset_key": dataset_key,
                    "source_column": source_column,
                    "aggregation": str(
                        contract.get("aggregation") or "sum"
                    ).strip().lower(),
                    "output_column": output_column,
                    "source_candidates": _merge_strings(
                        _mapped_column_candidates(table_item, source_column),
                        [source_column],
                    ),
                }
            )
    return result


# 함수 설명: 선택 Domain 중 현재 dataset을 의미하는 수량·metric 항목을 source 집계 후보로 변환합니다.
def _domain_metric_contracts_for_dataset(
    candidates: dict[str, Any],
    dataset_key: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    domain_items = candidates.get("domain_items")
    for item in domain_items if isinstance(domain_items, list) else []:
        if not isinstance(item, dict):
            continue
        payload = _metadata_payload(item)
        source_dataset = str(
            payload.get("dataset_key") or payload.get("data_source") or ""
        ).strip()
        source_column = str(
            payload.get("metric_column")
            or payload.get("quantity_column")
            or payload.get("column")
            or ""
        ).strip()
        if source_dataset != dataset_key or not source_column:
            continue
        result.append(
            {
                "source_column": source_column,
                "aggregation": (
                    payload.get("aggregation_method")
                    or payload.get("aggregation")
                    or "sum"
                ),
                "output_column": payload.get("output_column"),
                "metric_aliases": _merge_strings(
                    _string_list(payload.get("aliases")),
                    _string_list(payload.get("display_name")),
                    _string_list(item.get("key")),
                ),
            }
        )
    return result


# 함수 설명: 명시적 집계 계약이 부족할 때 Table Catalog 실제 컬럼과 요청 출력 metric의 이름 관계만으로 보수적 후보를 만듭니다.
def _schema_metric_contracts(
    table_item: dict[str, Any],
    raw_metrics: list[str],
) -> list[dict[str, Any]]:
    declared_columns = _catalog_declared_columns(_metadata_payload(table_item))
    result: list[dict[str, Any]] = []
    for output_column in raw_metrics:
        output_key = _normalized_column_key(output_column)
        matches = [
            column
            for column in declared_columns
            if len(_normalized_column_key(column)) >= 3
            and _normalized_column_key(column) in output_key
        ]
        if len(matches) == 1:
            result.append(
                {
                    "source_column": matches[0],
                    "aggregation": "sum",
                    "output_column": output_column,
                }
            )
    return result


# 함수 설명: source column, Domain alias와 LLM 출력 계약을 비교해 source마다 사용할 표시 metric 이름을 하나만 고릅니다.
def _metric_output_column(
    source_column: str,
    raw_metrics: list[str],
    *,
    explicit_output: str,
    aliases: list[str],
    used_outputs: set[str],
) -> str:
    source_key = _normalized_column_key(source_column)
    explicit_key = _normalized_column_key(explicit_output)
    alias_keys = [
        _normalized_column_key(alias)
        for alias in aliases
        if _normalized_column_key(alias)
    ]
    ranked: list[tuple[int, int, str]] = []
    for index, metric in enumerate(raw_metrics):
        metric_key = _normalized_column_key(metric)
        if not metric_key or metric_key in used_outputs:
            continue
        score = 0
        if explicit_key and metric_key == explicit_key:
            score = 100
        elif source_key and source_key in metric_key:
            score = 80 + len(source_key)
        elif any(key in metric_key or metric_key in key for key in alias_keys):
            score = 40
        if score:
            ranked.append((score, -index, metric))
    if ranked:
        ranked.sort(reverse=True)
        return ranked[0][2]
    fallback = explicit_output or source_column
    return (
        fallback
        if fallback and _normalized_column_key(fallback) not in used_outputs
        else ""
    )


# 함수 설명: 특정 source의 집계 단계에서 source column, 방식, output column을 표준 목록으로 추출합니다.
def _aggregation_contracts_for_alias(
    pandas_plan: list[Any],
    source_alias: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for step in pandas_plan:
        if (
            not isinstance(step, dict)
            or str(step.get("source_alias") or "").strip() != source_alias
        ):
            continue
        aggregations = (
            step.get("aggregations")
            if isinstance(step.get("aggregations"), list)
            else []
        )
        if not aggregations and (
            step.get("agg_column") or step.get("aggregate_column")
        ):
            aggregations = [
                {
                    "source_column": (
                        step.get("agg_column")
                        or step.get("aggregate_column")
                    ),
                    "aggregation": (
                        step.get("agg_method")
                        or step.get("method")
                        or step.get("aggregation")
                    ),
                    "output_column": (
                        step.get("output_column")
                        or step.get("result_column")
                    ),
                }
            ]
        for item in aggregations:
            if not isinstance(item, dict):
                continue
            source_column = str(
                item.get("source_column")
                or item.get("column")
                or item.get("agg_column")
                or item.get("aggregate_column")
                or ""
            ).strip()
            aggregation = str(
                item.get("aggregation")
                or item.get("method")
                or item.get("agg_method")
                or "sum"
            ).strip().lower()
            output_column = str(
                item.get("output_column")
                or item.get("result_column")
                or ""
            ).strip()
            if source_column and output_column:
                result.append(
                    {
                        "source_column": source_column,
                        "aggregation": aggregation,
                        "output_column": output_column,
                    }
                )
    return result


# 함수 설명: 직전 결과 payload에서 실제 표시 컬럼 순서를 가져옵니다.
def _previous_result_columns(payload: dict[str, Any]) -> list[str]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current_data = (
        state.get("current_data")
        if isinstance(state.get("current_data"), dict)
        else {}
    )
    columns = _string_list(current_data.get("columns")) or _string_list(
        current_data.get("result_columns")
    )
    if columns:
        return columns
    rows = current_data.get("rows") if isinstance(current_data.get("rows"), list) else []
    return _merge_strings(
        *[
            _string_list(list(item))
            for item in rows[:20]
            if isinstance(item, dict)
        ]
    )


# 함수 설명: 직전 분석의 resolved grain에 기록된 canonical→physical 후보를 후속 left key 선택에 재사용합니다.
def _previous_result_alias_groups(
    payload: dict[str, Any],
) -> dict[str, list[str]]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    previous_plan = (
        state.get("last_intent_plan")
        if isinstance(state.get("last_intent_plan"), dict)
        else {}
    )
    resolved_grain = (
        previous_plan.get("resolved_grain_plan")
        if isinstance(previous_plan.get("resolved_grain_plan"), dict)
        else {}
    )
    result: dict[str, list[str]] = {}
    for item in resolved_grain.get("column_mappings", []):
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical_key") or "").strip()
        if canonical:
            result[_normalized_column_key(canonical)] = _merge_strings(
                [canonical],
                _string_list(item.get("source_candidates")),
            )
    return result


# 함수 설명: 후보 목록 중 실제 컬럼에 존재하는 첫 컬럼을 대소문자 무시 방식으로 선택합니다.
def _first_existing_column(
    available_columns: list[str],
    candidates: list[str],
) -> str:
    available = {
        str(item).strip().casefold(): str(item)
        for item in available_columns
        if str(item).strip()
    }
    for candidate in candidates:
        matched = available.get(str(candidate).strip().casefold())
        if matched:
            return matched
    return ""


# 함수 설명: pandas 실행 계획의 컬럼 참조를 source별 metadata 계약에 등록된 실제 물리 컬럼명으로 정규화합니다.
def _normalize_pandas_plan_columns(
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    resolved_grain_plan: dict[str, Any] | None = None,
    resolved_join_plan: list[dict[str, Any]] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    alias_maps = _pandas_column_alias_maps(
        candidates,
        retrieval_jobs,
        resolved_grain_plan or {},
        resolved_join_plan or [],
    )
    changes: list[dict[str, Any]] = []
    normalized: list[Any] = []
    for index, raw_step in enumerate(pandas_plan):
        if not isinstance(raw_step, dict):
            normalized.append(deepcopy(raw_step))
            continue
        normalized.append(
            _normalize_pandas_plan_value(
                raw_step,
                alias_maps,
                {},
                f"pandas_execution_plan[{index}]",
                changes,
            )
        )
    return normalized, {
        "status": "applied" if changes else "not_needed",
        "change_count": len(changes),
        "changes": changes,
    }


# 함수 설명: 중첩된 pandas 단계에서 source alias 문맥을 유지하며 명시적인 컬럼 필드만 실제 컬럼명으로 바꿉니다.
def _normalize_pandas_plan_value(
    value: Any,
    alias_maps: dict[str, dict[str, str]],
    inherited_context: dict[str, str],
    path: str,
    changes: list[dict[str, Any]],
) -> Any:
    if isinstance(value, list):
        return [
            _normalize_pandas_plan_value(
                item,
                alias_maps,
                inherited_context,
                f"{path}[{index}]",
                changes,
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)

    context = dict(inherited_context)
    for context_key in ("source_alias", "left_source_alias", "right_source_alias"):
        text = str(value.get(context_key) or "").strip()
        if text:
            context[context_key] = text
    source_mapping = alias_maps.get(context.get("source_alias", ""), {})
    left_mapping = alias_maps.get(context.get("left_source_alias", ""), {})
    right_mapping = alias_maps.get(context.get("right_source_alias", ""), {})

    result: dict[str, Any] = {}
    for raw_key, raw_item in value.items():
        key = str(raw_key)
        item_path = f"{path}.{key}"
        if key in PANDAS_CANONICAL_COLUMN_KEYS:
            result[raw_key] = deepcopy(raw_item)
            continue
        if key in PANDAS_LEFT_COLUMN_KEYS:
            result[raw_key] = _normalize_column_field_value(
                raw_item,
                left_mapping,
                context.get("left_source_alias", ""),
                item_path,
                changes,
            )
            continue
        if key in PANDAS_RIGHT_COLUMN_KEYS:
            result[raw_key] = _normalize_column_field_value(
                raw_item,
                right_mapping,
                context.get("right_source_alias", ""),
                item_path,
                changes,
            )
            continue
        if key in PANDAS_COLUMN_SCALAR_KEYS or key in PANDAS_COLUMN_LIST_KEYS:
            result[raw_key] = _normalize_column_field_value(
                raw_item,
                source_mapping,
                context.get("source_alias", ""),
                item_path,
                changes,
            )
            continue
        result[raw_key] = _normalize_pandas_plan_value(
            raw_item,
            alias_maps,
            context,
            item_path,
            changes,
        )
    return result


# 함수 설명: 문자열 또는 문자열 목록에 metadata로 확정된 alias 매핑만 적용하고 변경 근거를 trace에 기록합니다.
def _normalize_column_field_value(
    value: Any,
    mapping: dict[str, str],
    source_alias: str,
    path: str,
    changes: list[dict[str, Any]],
) -> Any:
    if isinstance(value, list):
        normalized_items = [
            _normalize_column_field_value(
                item,
                mapping,
                source_alias,
                f"{path}[{index}]",
                changes,
            )
            for index, item in enumerate(value)
        ]
        result: list[Any] = []
        seen_columns: set[str] = set()
        for item in normalized_items:
            if isinstance(item, str):
                key = _normalized_column_key(item)
                if key and key in seen_columns:
                    continue
                if key:
                    seen_columns.add(key)
            result.append(item)
        return result
    if not isinstance(value, str) or not mapping:
        return deepcopy(value)
    normalized_key = _normalized_column_key(value)
    target = mapping.get(normalized_key)
    if not target or target == value:
        return value
    changes.append(
        {
            "path": path,
            "source_alias": source_alias,
            "from": value,
            "to": target,
        }
    )
    return target


# 함수 설명: retrieval source별 Table Catalog와 resolved grain/join 계약을 하나의 비모호한 alias→물리 컬럼 맵으로 합칩니다.
def _pandas_column_alias_maps(
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    resolved_grain_plan: dict[str, Any],
    resolved_join_plan: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    groups_by_alias: dict[str, list[tuple[str, list[str]]]] = {}
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        if not alias or not dataset_key:
            continue
        table_item = _table_catalog_item(candidates, dataset_key)
        groups_by_alias.setdefault(alias, []).extend(_table_column_alias_groups(table_item))

    grain_alias = str(resolved_grain_plan.get("source_alias") or "").strip()
    if grain_alias:
        for item in resolved_grain_plan.get("column_mappings", []):
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("canonical_key") or "").strip()
            physical = _string_list(item.get("source_candidates"))
            if canonical and physical:
                groups_by_alias.setdefault(grain_alias, []).append((physical[0], [canonical, *physical]))

    for join in resolved_join_plan:
        if not isinstance(join, dict):
            continue
        left_alias = str(join.get("left_source_alias") or "").strip()
        right_alias = str(join.get("right_source_alias") or "").strip()
        for item in join.get("key_mappings", []):
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("canonical_key") or "").strip()
            left_candidates = _string_list(item.get("left_candidates"))
            right_candidates = _string_list(item.get("right_candidates"))
            if left_alias and canonical and left_candidates:
                groups_by_alias.setdefault(left_alias, []).append(
                    (left_candidates[0], [canonical, *left_candidates])
                )
            if right_alias and canonical and right_candidates:
                groups_by_alias.setdefault(right_alias, []).append(
                    (right_candidates[0], [canonical, *right_candidates])
                )

    return {
        alias: _unambiguous_column_alias_map(groups)
        for alias, groups in groups_by_alias.items()
        if groups
    }


# 함수 설명: Table Catalog의 표준 alias 그룹에서 선언된 source column을 우선해 물리 컬럼 대상을 결정합니다.
def _table_column_alias_groups(item: dict[str, Any]) -> list[tuple[str, list[str]]]:
    payload = _metadata_payload(item)
    declared_columns = _catalog_declared_columns(payload)
    declared_index = {
        _normalized_column_key(column): column
        for column in declared_columns
    }
    result: list[tuple[str, list[str]]] = []
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        mapping = payload.get(mapping_name)
        if not isinstance(mapping, dict):
            continue
        for standard, raw_aliases in mapping.items():
            standard_name = str(standard or "").strip()
            aliases = _string_list(raw_aliases)
            if not standard_name or not aliases:
                continue
            physical = next(
                (
                    declared_index[_normalized_column_key(alias)]
                    for alias in aliases
                    if _normalized_column_key(alias) in declared_index
                ),
                declared_index.get(_normalized_column_key(standard_name), aliases[0]),
            )
            result.append((physical, [standard_name, *aliases]))
    return result


# 함수 설명: Table Catalog가 선언한 실제 source column 목록을 다양한 schema 표기에서 추출합니다.
def _catalog_declared_columns(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("columns") or payload.get("schema") or payload.get("column_names") or []
    if isinstance(raw, dict):
        raw = list(raw)
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("column") or item.get("key")
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: 같은 alias가 서로 다른 물리 컬럼을 가리키면 추측하지 않고 해당 alias의 자동 정규화를 비활성화합니다.
def _unambiguous_column_alias_map(
    groups: list[tuple[str, list[str]]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    ambiguous: set[str] = set()
    for raw_target, raw_aliases in groups:
        target = str(raw_target or "").strip()
        if not target:
            continue
        for alias in _merge_strings([target], _string_list(raw_aliases)):
            key = _normalized_column_key(alias)
            if not key or key in ambiguous:
                continue
            existing = result.get(key)
            if existing and _normalized_column_key(existing) != _normalized_column_key(target):
                result.pop(key, None)
                ambiguous.add(key)
                continue
            result[key] = target
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


# 함수 설명: 선택 dataset의 catalog가 특정 조회 필수 파라미터를 선언했는지 정확한 이름 기준으로 확인합니다.
def _catalog_requires_param(
    candidates: dict[str, Any],
    dataset_key: str,
    param_name: str,
) -> bool:
    target = _normalized_column_key(param_name)
    return any(
        _normalized_column_key(value) == target
        for value in _catalog_required_params(candidates, dataset_key)
    )


# 함수 설명: Table Catalog의 여러 schema 표기에서 실제 required parameter 이름을 중복 없이 추출합니다.
def _catalog_required_params(
    candidates: dict[str, Any],
    dataset_key: str,
) -> list[str]:
    item = _table_catalog_item(candidates, dataset_key)
    payload = _metadata_payload(item)
    values = [
        payload,
        payload.get("source_config")
        if isinstance(payload.get("source_config"), dict)
        else {},
        item,
    ]
    result: list[str] = []
    for value in values:
        raw = value.get("required_params") or value.get("required_param_names") or []
        if isinstance(raw, dict):
            raw = list(raw)
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        for entry in raw:
            if isinstance(entry, dict):
                entry = entry.get("name") or entry.get("key")
            text = str(entry or "").strip()
            if text and text not in result:
                result.append(text)
    return result


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


# 함수 설명: `_function_case_items()`는 선택된 Function Case에 신뢰 가능한 Domain 실행 계약을 결합해 전달합니다.
def _function_case_items(
    plan: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    metadata_candidates: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
    normalized = _dedupe_cases(
        [_normalize_case(item, retrieval_jobs) for item in items]
    )
    return _attach_function_case_execution_contracts(
        normalized,
        metadata_candidates or {},
    )


# 함수 설명: 선택된 case의 key 또는 function_name과 일치하는 Domain metadata에서 공통 실행 계약만 복원합니다.
def _attach_function_case_execution_contracts(
    function_cases: list[dict[str, Any]],
    metadata_candidates: dict[str, Any],
) -> list[dict[str, Any]]:
    domain_items = (
        metadata_candidates.get("domain_items")
        if isinstance(metadata_candidates.get("domain_items"), list)
        else []
    )
    contracts: list[dict[str, Any]] = []
    for item in domain_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("section") or "").strip() != "pandas_function_cases":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        execution_contract = (
            payload.get("execution_contract")
            if isinstance(payload.get("execution_contract"), dict)
            else {}
        )
        source_filter_order = str(
            execution_contract.get("source_filter_order") or ""
        ).strip()
        if source_filter_order not in {"before_helper", "after_helper"}:
            continue
        contracts.append(
            {
                "key": str(item.get("key") or "").strip(),
                "function_name": str(
                    item.get("function_name")
                    or payload.get("function_name")
                    or ""
                ).strip(),
                "execution_contract": {
                    "source_filter_order": source_filter_order,
                },
            }
        )

    result: list[dict[str, Any]] = []
    for case in function_cases:
        next_case = deepcopy(case)
        case_key = str(next_case.get("key") or "").strip()
        function_name = str(next_case.get("function_name") or "").strip()
        trusted = next(
            (
                item
                for item in contracts
                if (
                    case_key
                    and case_key == str(item.get("key") or "").strip()
                )
                or (
                    function_name
                    and function_name
                    == str(item.get("function_name") or "").strip()
                )
            ),
            None,
        )
        if trusted is not None:
            next_case["execution_contract"] = deepcopy(
                trusted["execution_contract"]
            )
        else:
            next_case.pop("execution_contract", None)
        result.append(next_case)
    return result


# 함수 설명: 제품 token helper가 해석할 원문 token을 같은 source의 단순 조회 filter로 중복 적용하지 않도록 실행 책임을 한 곳으로 모읍니다.
def _remove_function_owned_retrieval_filters(
    retrieval_jobs: list[dict[str, Any]],
    function_cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    product_filter_fields = {
        "TECH",
        "DEN",
        "DENSITY",
        "MODE",
        "PKGTYPE1",
        "PKG1",
        "PKGTYP1",
        "PKGTYPE2",
        "PKG2",
        "PKGTYP2",
        "LEAD",
        "MCPNO",
        "MCPSALESNO",
        "MCPSALECD",
        "DEVICE",
        "DEVICEDESC",
        "ORG",
        "ORGANIZCD",
    }
    owned_cases = [
        item
        for item in function_cases
        if str(item.get("function_name") or "").strip() == "match_product_tokens"
        and str(item.get("input_text") or "").strip()
    ]
    if not owned_cases:
        return retrieval_jobs, {"removed": []}

    normalized_jobs = deepcopy(retrieval_jobs)
    removed: list[dict[str, Any]] = []
    for job in normalized_jobs:
        if not isinstance(job, dict):
            continue
        source_alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        matching_cases = [
            item
            for item in owned_cases
            if not str(item.get("source_alias") or "").strip()
            or str(item.get("source_alias") or "").strip() == source_alias
        ]
        filters = job.get("filters")
        if not matching_cases or not isinstance(filters, dict):
            continue
        retained_filters: dict[str, Any] = {}
        for field, condition in filters.items():
            normalized_field = _normalized_column_key(field)
            owner = next(
                (
                    item
                    for item in matching_cases
                    if normalized_field in product_filter_fields
                    and _function_case_owns_filter_value(
                        normalized_field,
                        condition,
                        str(item.get("input_text") or ""),
                    )
                ),
                None,
            )
            if owner is None:
                retained_filters[field] = condition
                continue
            removed.append(
                {
                    "source_alias": source_alias,
                    "field": str(field),
                    "function_name": "match_product_tokens",
                    "function_case_key": str(owner.get("key") or ""),
                }
            )
        job["filters"] = retained_filters
    return normalized_jobs, {"removed": removed}


# 함수 설명: filter 값이 helper input에 직접 포함된 제품 token인지 판정하며, domain이 소유한 별도 조건은 유지합니다.
def _function_case_owns_filter_value(
    normalized_field: str,
    condition: Any,
    input_text: str,
) -> bool:
    if isinstance(condition, dict):
        operator = str(condition.get("operator") or "eq").strip().casefold()
        raw_value = (
            condition.get("values")
            if condition.get("values") is not None
            else condition.get("value")
        )
    else:
        operator = "eq"
        raw_value = condition
    if operator not in {"eq", "in", "contains", "starts_with", "startswith"}:
        return False
    raw_values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
    values = [_function_token(item) for item in raw_values]
    values = [item for item in values if item]
    if not values:
        return False
    input_tokens = {
        _function_token(item)
        for item in re.findall(r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*", input_text)
    }
    input_tokens.discard("")
    if not input_tokens:
        return False

    for value in values:
        candidates = {value}
        if normalized_field == "LEAD":
            candidates.update({f"F{value}", f"FC{value}", f"{value}LEAD", f"{value}BALL"})
        elif normalized_field in {"ORG", "ORGANIZCD"}:
            candidates.add(f"X{value}")
        if not candidates.intersection(input_tokens):
            return False
    return True


# 함수 설명: 제품 token과 filter 값을 공백·구분자·대소문자 차이 없이 비교할 최소 형태로 정규화합니다.
def _function_token(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").strip().upper())


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


# 함수 설명: Domain Function Case가 선언한 공통 실행 순서에 따라 source filter를 helper 뒤 단계로 이동합니다.
def _apply_function_case_execution_contracts(
    retrieval_jobs: list[dict[str, Any]],
    pandas_plan: list[Any],
    function_cases: list[dict[str, Any]],
    condition_resolution: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any], dict[str, Any]]:
    deferred_cases = [
        item
        for item in function_cases
        if isinstance(item, dict)
        and str(item.get("source_alias") or "").strip()
        and isinstance(item.get("execution_contract"), dict)
        and str(
            item["execution_contract"].get("source_filter_order") or ""
        ).strip()
        == "after_helper"
    ]
    if not deferred_cases:
        return (
            retrieval_jobs,
            pandas_plan,
            condition_resolution,
            {"applied": []},
        )

    aliases = {
        str(item.get("source_alias") or "").strip()
        for item in deferred_cases
    }
    normalized_jobs = deepcopy(retrieval_jobs)
    filter_contracts: dict[str, list[Any]] = {alias: [] for alias in aliases}
    for job in normalized_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(
            job.get("source_alias") or job.get("dataset_key") or ""
        ).strip()
        filters = job.get("filters")
        if alias in aliases and filters not in (None, "", [], {}):
            filter_contracts[alias].append(deepcopy(filters))
            job["filters"] = {}

    normalized_condition = deepcopy(condition_resolution)
    effective_filters = (
        normalized_condition.get("effective_filters")
        if isinstance(normalized_condition.get("effective_filters"), dict)
        else {}
    )
    retained_effective_filters: dict[str, Any] = {}
    for alias, item in effective_filters.items():
        alias_text = str(alias or "").strip()
        filters = (
            item.get("filters")
            if isinstance(item, dict)
            else None
        )
        if alias_text in aliases:
            if filters not in (None, "", [], {}):
                filter_contracts[alias_text].append(deepcopy(filters))
            continue
        retained_effective_filters[alias] = deepcopy(item)
    if retained_effective_filters:
        normalized_condition["effective_filters"] = retained_effective_filters
    else:
        normalized_condition.pop("effective_filters", None)

    deferred_steps = {
        alias: _function_contract_filter_steps(alias, contracts)
        for alias, contracts in filter_contracts.items()
    }
    reordered_plan = _place_function_contract_filters(
        pandas_plan,
        deferred_cases,
        deferred_steps,
    )
    applied = []
    for case in deferred_cases:
        alias = str(case.get("source_alias") or "").strip()
        fields = [
            str(step.get("field") or "").strip()
            for step in deferred_steps.get(alias, [])
            if isinstance(step, dict) and str(step.get("field") or "").strip()
        ]
        applied.append(
            {
                "source_alias": alias,
                "function_case_key": str(case.get("key") or "").strip(),
                "function_name": str(case.get("function_name") or "").strip(),
                "source_filter_order": "after_helper",
                "deferred_filter_fields": list(dict.fromkeys(fields)),
            }
        )
    return (
        normalized_jobs,
        reordered_plan,
        normalized_condition,
        {"applied": applied},
    )


# 함수 설명: 조회·effective filter 구조를 helper 뒤에서 실행할 명시적 apply_filters 단계로 바꿉니다.
def _function_contract_filter_steps(
    source_alias: str,
    filter_contracts: list[Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for filters in filter_contracts:
        if isinstance(filters, dict):
            raw_steps = [
                _function_contract_filter_step(
                    source_alias,
                    str(field),
                    condition,
                )
                for field, condition in filters.items()
            ]
        elif isinstance(filters, list):
            raw_steps = [
                {
                    "step": "Function Case 이후 필터 적용",
                    "operation": "apply_filters",
                    "source_alias": source_alias,
                    **deepcopy(item),
                }
                for item in filters
                if isinstance(item, dict)
            ]
        else:
            raw_steps = []
        for step in raw_steps:
            marker = json.dumps(
                {
                    key: value
                    for key, value in step.items()
                    if key != "step"
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if marker in seen:
                continue
            seen.add(marker)
            result.append(step)
    return result


# 함수 설명: field별 filter condition을 pandas plan의 표준 apply_filters 한 단계로 변환합니다.
def _function_contract_filter_step(
    source_alias: str,
    field: str,
    condition: Any,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step": "Function Case 이후 필터 적용",
        "operation": "apply_filters",
        "source_alias": source_alias,
        "field": field,
    }
    if not isinstance(condition, dict):
        step.update({"operator": "eq", "value": deepcopy(condition)})
        return step
    step["operator"] = str(condition.get("operator") or "eq")
    if "values" in condition:
        step["values"] = deepcopy(condition.get("values"))
    elif "value" in condition:
        step["value"] = deepcopy(condition.get("value"))
    else:
        step["condition"] = deepcopy(condition)
    return step


# 함수 설명: 계약 대상 source의 apply_filters를 모아 해당 Function Case helper 직후에 배치합니다.
def _place_function_contract_filters(
    pandas_plan: list[Any],
    deferred_cases: list[dict[str, Any]],
    deferred_steps: dict[str, list[dict[str, Any]]],
) -> list[Any]:
    filters_by_alias: dict[str, list[dict[str, Any]]] = {
        alias: list(steps)
        for alias, steps in deferred_steps.items()
    }
    remaining: list[Any] = []
    for raw_step in deepcopy(pandas_plan):
        if not isinstance(raw_step, dict):
            remaining.append(raw_step)
            continue
        operation = str(raw_step.get("operation") or "").strip()
        alias = str(raw_step.get("source_alias") or "").strip()
        if operation == "apply_filters" and alias in filters_by_alias:
            if any(
                key in raw_step
                for key in ("field", "filters", "condition")
            ):
                filters_by_alias[alias].append(raw_step)
            continue
        remaining.append(raw_step)

    result: list[Any] = []
    inserted: set[str] = set()
    for raw_step in remaining:
        result.append(raw_step)
        if not isinstance(raw_step, dict):
            continue
        if str(raw_step.get("operation") or "").strip() != "apply_pandas_function_case":
            continue
        for case in deferred_cases:
            alias = str(case.get("source_alias") or "").strip()
            if alias in inserted or not _function_step_matches_case(raw_step, case):
                continue
            result.extend(filters_by_alias.get(alias, []))
            inserted.add(alias)
    for alias, steps in filters_by_alias.items():
        if alias not in inserted:
            result.extend(steps)
    return result


# 함수 설명: pandas helper 단계가 metadata 실행 계약을 가진 선택 case와 같은지 확인합니다.
def _function_step_matches_case(
    step: dict[str, Any],
    case: dict[str, Any],
) -> bool:
    if str(step.get("source_alias") or "").strip() != str(
        case.get("source_alias") or ""
    ).strip():
        return False
    case_key = str(case.get("key") or "").strip()
    function_name = str(case.get("function_name") or "").strip()
    if case_key and str(
        step.get("function_case_key") or step.get("key") or ""
    ).strip() == case_key:
        return True
    return bool(
        function_name
        and str(step.get("function_name") or "").strip() == function_name
    )


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
