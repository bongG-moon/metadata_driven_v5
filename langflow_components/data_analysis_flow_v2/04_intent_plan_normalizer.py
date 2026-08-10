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
    "lhs_metric_column",
    "rhs_metric_column",
    "comparison_metric_column",
    "baseline_metric_column",
    "sort_by",
    "order_by",
    "rank_by",
    "rank_column",
    "label_column",
    "order_column",
    "date_column",
    "time_column",
    "id_column",
    "primary_metric",
    "segment_column",
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
    "grain_columns",
    "result_columns",
    "pivot_index",
    "pivot_columns",
    "pivot_values",
    "quality_columns",
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
VALUELESS_FILTER_OPERATORS = {
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
}
FILTER_LOGICAL_KEYS = {"and", "or", "any", "all", "$and", "$or"}
V2_FAST_RECIPES = {
    "detail_query",
    "scalar_summary",
    "group_summary",
    "ranked_summary",
    "frequency_summary",
    "distinct_summary",
    "list_summary",
    "existence_summary",
    "quality_summary",
    "latest_earliest",
    "percent_of_total",
    "rank_within_group",
    "threshold_after_aggregate",
    "time_bucket_summary",
    "period_change",
    "running_total",
    "moving_aggregate",
    "percentile_summary",
    "pivot_summary",
}

# 제품 Function Case 입력 보정에 사용하는 범용 구조 토큰 패턴입니다.
# 업무별 의미(TECH/LEAD/MCP_NO 등)는 Domain metadata가 소유하고, 여기서는
# 질문에서 누락되기 쉬운 고신뢰 형태만 찾습니다. 공정·날짜·지표 단어는
# 이 패턴에 매칭되지 않으므로 helper 입력에 자동으로 섞이지 않습니다.
GENERIC_FUNCTION_TOKEN_PATTERNS = (
    r"(?<![A-Za-z0-9])(?:FC|F|X)\d+(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])[A-Z]-\d+(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])\d+G(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])(?:DDR|GDDR|HBM)\d+[A-Z0-9-]*(?![A-Za-z0-9])",
    # Do not match the numeric suffix of a canonical column such as
    # ``PKG_TYPE2`` as a standalone product token.
    r"(?<![A-Za-z0-9_])[A-Z]{2,}\d+(?![A-Za-z0-9_])",
)
FUNCTION_CASE_DETAIL_CUES = (
    "이력",
    "내역",
    "상세",
    "사유",
    "원인",
    "시각",
    "시간",
    "코드",
    "history",
    "detail",
)


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
    plan = deepcopy(plan) if isinstance(plan, dict) else {}
    metadata_envelope = _metadata_candidate_envelope(metadata_candidates_value, payload)
    metadata_candidates = _metadata_candidates(metadata_candidates_value, payload)
    catalog_error = _catalog_metadata_error(metadata_envelope, metadata_candidates)
    if not catalog_error:
        catalog_error = _catalog_error_from_plan(plan)
    if catalog_error:
        return _blocked_catalog_metadata_payload(payload, catalog_error)
    retrieval_jobs = _retrieval_jobs(plan)
    (
        plan,
        retrieval_jobs,
        previous_result_pseudo_job_guard,
    ) = _remove_previous_result_pseudo_jobs(plan, retrieval_jobs)
    (
        metadata_candidates,
        retrieval_jobs,
        execution_catalog_resolution,
    ) = _hydrate_execution_catalog_candidates(
        metadata_candidates,
        metadata_envelope,
        retrieval_jobs,
    )
    unknown_dataset_error = _unregistered_dataset_error(
        retrieval_jobs,
        metadata_envelope,
        metadata_candidates,
    )
    if unknown_dataset_error:
        return _blocked_catalog_metadata_payload(payload, unknown_dataset_error)
    metadata_refs = _metadata_refs(parsed, plan)
    metadata_refs = _merge_metadata_ref_lists(
        metadata_refs,
        execution_catalog_resolution.get("metadata_refs", []),
    )
    metadata_refs, metadata_ref_guard = _known_metadata_refs(
        metadata_refs,
        metadata_candidates,
    )
    question = str(
        (payload.get("request") if isinstance(payload.get("request"), dict) else {}).get("question")
        or ""
    ).strip()
    raw_pandas_plan = (
        plan.get("pandas_execution_plan")
        if isinstance(plan.get("pandas_execution_plan"), list)
        else []
    )
    raw_pandas_plan, typed_input_binding = _bind_typed_external_source_aliases(
        raw_pandas_plan
    )
    # Retrieval jobs own the runtime source alias.  A model may use a harmless
    # spelling variant such as ``dataset_src`` in a typed step while declaring
    # ``dataset_source`` on the job.  Normalize only a unique dataset-stem
    # match, so a true second source is never silently redirected.
    plan, raw_pandas_plan, source_alias_reconciliation = (
        _reconcile_execution_source_aliases(
            plan,
            retrieval_jobs,
            raw_pandas_plan,
        )
    )
    (
        plan,
        retrieval_jobs,
        raw_pandas_plan,
        followup_contract_guard,
    ) = _reconcile_followup_execution_contract(
        payload,
        plan,
        retrieval_jobs,
        raw_pandas_plan,
        metadata_candidates,
        question,
    )
    if followup_contract_guard.get("metadata_ref"):
        followup_ref = _metadata_ref(followup_contract_guard["metadata_ref"])
        if followup_ref and followup_ref not in metadata_refs:
            metadata_refs.append(followup_ref)
    (
        retrieval_jobs,
        external_source_catalog_binding,
    ) = _bind_missing_external_sources_from_catalog_contracts(
        payload,
        plan,
        retrieval_jobs,
        raw_pandas_plan,
        metadata_candidates,
    )
    domain_selection = _resolve_execution_domain_selection(
        question,
        metadata_candidates,
        metadata_refs,
    )
    metadata_refs, removed_unmatched_execution_refs = _execution_compatible_metadata_refs(
        metadata_refs,
        metadata_candidates,
        domain_selection.get("locked_metadata_refs", []),
    )
    metadata_ref_guard["removed_unmatched_execution_refs"] = (
        removed_unmatched_execution_refs
    )
    metadata_refs = _merge_metadata_ref_lists(
        metadata_refs,
        domain_selection.get("locked_metadata_refs", []),
    )
    (
        retrieval_jobs,
        raw_pandas_plan,
        domain_metric_source_guard,
    ) = _ensure_selected_metric_sources(
        payload,
        retrieval_jobs,
        raw_pandas_plan,
        metadata_candidates,
        domain_selection.get("locked_metadata_refs", []),
    )
    retrieval_jobs, domain_condition_guard = _apply_selected_domain_conditions(
        retrieval_jobs,
        metadata_candidates,
        domain_selection.get("locked_metadata_refs", []),
    )
    retrieval_jobs, domain_filter_contract_guard = _enforce_selected_domain_filter_contracts(
        retrieval_jobs,
        metadata_candidates,
        domain_selection.get("locked_metadata_refs", []),
    )
    retrieval_jobs, process_group_field_guard = _apply_process_group_filter_fields(
        retrieval_jobs,
        metadata_candidates,
        question,
        declared_processes=_declared_process_scope_from_plan(
            plan, metadata_candidates
        ),
        align_explicit_scope=not _has_ordered_process_range_case(plan),
    )
    retrieval_jobs, filter_operator_normalization = _normalize_retrieval_filter_operators(
        retrieval_jobs,
        question=question,
    )
    retrieval_jobs, context_date_guard = _apply_context_date_guard(
        payload,
        retrieval_jobs,
        metadata_candidates,
    )
    retrieval_jobs, optional_date_filter_guard = (
        _apply_unrequested_optional_date_filter_guard(
            payload,
            retrieval_jobs,
            metadata_candidates,
        )
    )
    retrieval_jobs, business_time_guard = _apply_business_time_contracts(
        payload,
        retrieval_jobs,
        metadata_candidates,
        question,
        domain_selection.get("locked_metadata_refs", []),
        raw_pandas_plan,
    )
    # A follow-up may legitimately carry an earlier condition into a different
    # Catalog dataset.  Keep that condition only when the *new* trusted
    # Catalog can execute it.  This prevents a prior source's date/process
    # column from becoming an impossible schema requirement on a current
    # snapshot or other independently-shaped source.
    retrieval_jobs, inherited_filter_compatibility_guard = (
        _drop_unsupported_inherited_filters(
            plan,
            retrieval_jobs,
            metadata_candidates,
            question,
        )
    )
    retrieval_jobs, post_business_process_group_guard = _apply_process_group_filter_fields(
        retrieval_jobs,
        metadata_candidates,
        question,
        declared_processes=_declared_process_scope_from_plan(
            plan, metadata_candidates
        ),
        align_explicit_scope=not _has_ordered_process_range_case(plan),
    )
    if post_business_process_group_guard.get("corrections"):
        process_group_field_guard = {
            **process_group_field_guard,
            "status": "applied",
            "corrections": [
                *(process_group_field_guard.get("corrections") or []),
                *(post_business_process_group_guard.get("corrections") or []),
            ],
            "value_alignment_mode": post_business_process_group_guard.get(
                "value_alignment_mode",
                process_group_field_guard.get("value_alignment_mode"),
            ),
            "job_process_scopes": post_business_process_group_guard.get(
                "job_process_scopes",
                process_group_field_guard.get("job_process_scopes", []),
            ),
        }
    if (
        business_time_guard.get("status") == "applied"
        and domain_selection.get("temporal_alias_lock") is True
    ):
        business_time_guard["selection_source"] = "metadata_alias_lock"
    raw_pandas_plan, temporal_metric_alignment = _align_temporal_metric_columns(
        raw_pandas_plan,
        business_time_guard,
        metadata_candidates,
    )
    external_source_catalog_binding = _resolve_late_external_source_bindings(
        external_source_catalog_binding,
        retrieval_jobs,
    )
    pandas_plan = raw_pandas_plan
    retrieval_jobs, metric_dataset_selection = _reconcile_metric_dataset_selection(
        payload,
        retrieval_jobs,
        pandas_plan,
        metadata_candidates,
    )
    retrieval_jobs, source_dataset_selection = _reconcile_source_dataset_selection(
        payload,
        retrieval_jobs,
        pandas_plan,
        metadata_candidates,
        domain_selection.get("locked_metadata_refs", []),
        locked_source_aliases={
            str(item.get("source_alias") or item.get("dataset_key") or "").strip()
            for item in retrieval_jobs
            if isinstance(item, dict)
            and plan.get("_continuation_stage_active") is True
            and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        },
    )
    # Dataset replacement can change the late catalog binding even though the
    # source alias remains stable.  Re-run the generic binding resolver after
    # both time-scope and schema-fit reconciliation.
    external_source_catalog_binding = _resolve_late_external_source_bindings(
        external_source_catalog_binding,
        retrieval_jobs,
    )
    (
        metadata_refs,
        compatible_join_plan,
        recipe_source_compatibility,
    ) = _filter_incompatible_recipe_contracts(
        metadata_refs,
        plan.get("join_plan"),
        retrieval_jobs,
        metadata_candidates,
        question=question,
    )
    plan = deepcopy(plan)
    plan["metadata_refs"] = deepcopy(metadata_refs)
    if compatible_join_plan:
        plan["join_plan"] = compatible_join_plan
    else:
        plan.pop("join_plan", None)
    pandas_plan = _rewrite_process_group_plan_descriptions(
        pandas_plan,
        process_group_field_guard,
    )
    plan, auto_function_case_selection = _auto_select_metadata_function_case(
        plan,
        retrieval_jobs,
        metadata_candidates,
        question,
    )
    function_cases = _function_case_items(
        plan,
        retrieval_jobs,
        metadata_candidates,
    )
    function_cases, function_case_input_reconciliation = (
        _reconcile_function_case_inputs(
            function_cases,
            question,
            metadata_candidates,
        )
    )
    (
        function_cases,
        pandas_plan,
        function_case_source_sufficiency,
    ) = _remove_source_filter_sufficient_function_cases(
        function_cases,
        pandas_plan,
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
    (
        plan,
        retrieval_jobs,
        pandas_plan,
        source_sufficiency_pruning,
    ) = _prune_source_sufficient_left_joins(
        plan,
        retrieval_jobs,
        pandas_plan,
        metadata_candidates,
        function_cases,
        question,
    )
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
        pandas_plan = _normalize_reserved_previous_result_references(
            pandas_plan,
            reference_mode,
        )
        function_cases = _bind_previous_result_alias(function_cases, retrieval_jobs)
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
    # Validate process scope only after the normalizer has materialized the
    # trusted previous/upstream row-match step. A dependent history source
    # may inherit the parent's scope through those rows rather than repeat
    # OPER_NAME filters, while ordinary unscoped sources remain blocked.
    process_scope_guard = _validate_process_scope_contract(
        retrieval_jobs,
        metadata_candidates,
        question,
        pandas_plan=pandas_plan,
        declared_processes=_declared_process_scope_from_plan(
            plan, metadata_candidates
        ),
        skip=_has_ordered_process_range_case(plan),
    )
    reference_mode_guard = _validate_reference_mode(
        reference_mode_resolution,
        request_scope,
        retrieval_jobs,
        row_match_guard,
        payload,
    )
    validation_errors = _reference_mode_validation_errors(reference_mode_guard)
    validation_errors.extend(followup_contract_guard.get("validation_errors", []))
    validation_errors.extend(
        function_case_input_reconciliation.get("validation_errors", [])
    )
    if metric_dataset_selection.get("unresolved"):
        validation_errors.append(
            {
                "type": "metric_dataset_selection_unresolved",
                "message": "metric과 요청 시점에 맞는 Table Catalog dataset을 하나로 확정할 수 없습니다.",
                "issues": deepcopy(metric_dataset_selection["unresolved"]),
            }
        )
    if external_source_catalog_binding.get("unresolved"):
        validation_errors.append(
            {
                "type": "external_source_catalog_binding_unresolved",
                "message": "typed external source를 Table Catalog 계약으로 하나로 확정할 수 없습니다.",
                "issues": deepcopy(external_source_catalog_binding["unresolved"]),
            }
        )
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
    condition_resolution = _synchronize_effective_filters_with_retrieval_jobs(
        condition_resolution,
        retrieval_jobs,
    )
    condition_resolution = _strip_removed_optional_date_conditions(
        condition_resolution,
        optional_date_filter_guard,
    )
    condition_resolution = _strip_dropped_inherited_filter_conditions(
        condition_resolution,
        inherited_filter_compatibility_guard,
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
    pandas_plan, domain_execution_contracts = _apply_selected_domain_execution_contracts(
        pandas_plan,
        metadata_candidates,
        retrieval_jobs,
        resolved_grain_plan,
        domain_selection.get("locked_metadata_refs", []),
    )
    pandas_plan, aggregate_grain_alignment = (
        _align_aggregate_steps_with_resolved_grain(
            pandas_plan,
            metadata_candidates,
            retrieval_jobs,
            resolved_grain_plan,
        )
    )
    validation_errors.extend(
        _pandas_catalog_column_validation_errors(
            pandas_plan,
            metadata_candidates,
            retrieval_jobs,
        )
    )
    resolved_output_grain_plan = _resolve_output_grain_plan(
        pandas_plan,
        resolved_grain_plan,
        metadata_candidates,
        retrieval_jobs,
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
        resolved_output_grain_plan or resolved_grain_plan,
        business_time_guard,
        function_cases,
    )
    validation_errors.extend(
        _function_case_metric_lineage_validation_errors(
            function_cases,
            resolved_metric_merge_plan,
        )
    )
    resolved_execution_graph = _compile_execution_graph(
        pandas_plan,
        retrieval_jobs,
        payload,
        reuse_strategy,
    )
    contract_plan = deepcopy(plan)
    contract_plan["pandas_execution_plan"] = pandas_plan
    normalized_raw_contract, output_contract_column_normalization = (
        _normalize_raw_output_contract_columns(
            contract_plan.get("output_contract"),
            metadata_candidates,
            retrieval_jobs,
            resolved_grain_plan,
            resolved_join_plan,
        )
    )
    if normalized_raw_contract:
        contract_plan["output_contract"] = normalized_raw_contract
    if domain_execution_contracts.get("status") == "applied":
        contract_plan["output_contract"] = _reconcile_aggregate_output_contract(
            contract_plan.get("output_contract"),
            pandas_plan,
            resolved_output_grain_plan or resolved_grain_plan,
        )
    output_contract = _output_contract(
        contract_plan,
        payload,
        retrieval_jobs,
        metadata_candidates,
        resolved_output_grain_plan or resolved_grain_plan,
        resolved_join_plan,
        resolved_reference_join_plan,
        resolved_metric_merge_plan,
    )
    output_contract = _preserve_v2_fast_output_contract(
        output_contract,
        contract_plan.get("output_contract"),
    )
    resolved_presence_comparison_plan = _resolve_presence_comparison_plan(
        pandas_plan,
        output_contract,
        metadata_candidates,
        retrieval_jobs,
    )
    resolved_metric_comparison_plan = _resolve_metric_comparison_plan(
        pandas_plan,
        output_contract,
        resolved_metric_merge_plan,
        question,
    )
    declared_metric_comparisons = [
        item
        for item in pandas_plan
        if isinstance(item, dict)
        and str(item.get("operation") or item.get("step") or "").strip().lower()
        == "compare_metrics"
    ]
    if declared_metric_comparisons and not resolved_metric_comparison_plan:
        validation_errors.append(
            {
                "type": "metric_comparison_contract_invalid",
                "message": "수치 비교 단계의 metric·operator·병합 계약을 확정할 수 없습니다.",
            }
        )
    if resolved_presence_comparison_plan:
        output_contract = _presence_output_contract(
            output_contract,
            resolved_presence_comparison_plan,
        )
    metric_source_errors = _metric_source_validation_errors(
        output_contract,
        retrieval_jobs,
        business_time_guard,
        resolved_execution_graph,
        metadata_candidates,
    )
    validation_errors.extend(resolved_execution_graph.get("validation_errors", []))
    if metric_source_errors:
        validation_errors.extend(metric_source_errors)
    validation_errors.extend(
        domain_filter_contract_guard.get("validation_errors", [])
    )
    validation_errors.extend(process_scope_guard.get("validation_errors", []))

    intent_ir = _build_intent_ir(
        plan,
        question,
        retrieval_jobs,
        pandas_plan,
        output_contract,
        resolved_output_grain_plan or resolved_grain_plan,
        business_time_guard,
        validation_errors,
    )

    decision_reasons, decision_reason_normalization = _normalize_decision_reasons(
        plan,
        parsed,
        business_time_guard,
        optional_date_filter_guard,
    )
    normalized_plan = deepcopy(plan)
    normalized_plan.pop("pandas_function_case", None)
    normalized_plan.pop("selected_function_cases", None)
    if decision_reasons:
        normalized_plan["decision_reason"] = decision_reasons
    else:
        normalized_plan.pop("decision_reason", None)
    normalized_plan["request_scope"] = request_scope
    normalized_plan["reference_mode"] = reference_mode
    normalized_plan["reuse_strategy"] = reuse_strategy
    if followup_contract_guard.get("reference_contract"):
        normalized_plan["reference_contract"] = deepcopy(
            followup_contract_guard["reference_contract"]
        )
    if validation_errors:
        normalized_plan["validation_errors"] = validation_errors
    else:
        normalized_plan.pop("validation_errors", None)
    if reference_scope_normalization.get("reason") == "complete_independent_question":
        condition_resolution.pop("inherited", None)
        condition_resolution.pop("dropped", None)
    normalized_plan["condition_resolution"] = condition_resolution
    normalized_plan["intent_ir"] = intent_ir
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
    if resolved_output_grain_plan:
        normalized_plan["resolved_output_grain_plan"] = resolved_output_grain_plan
    else:
        normalized_plan.pop("resolved_output_grain_plan", None)
    normalized_plan["resolved_execution_graph"] = resolved_execution_graph
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
    if resolved_presence_comparison_plan:
        normalized_plan["resolved_presence_comparison_plan"] = (
            resolved_presence_comparison_plan
        )
    else:
        normalized_plan.pop("resolved_presence_comparison_plan", None)
    if resolved_metric_comparison_plan:
        normalized_plan["resolved_metric_comparison_plan"] = (
            resolved_metric_comparison_plan
        )
    else:
        normalized_plan.pop("resolved_metric_comparison_plan", None)
    if function_cases:
        normalized_plan["pandas_function_cases"] = function_cases
    else:
        normalized_plan.pop("pandas_function_cases", None)

    # References from an LLM response are evidence only after they match the
    # current candidate set.  In particular, an empty/failed metadata load
    # must never reintroduce an invented domain or table reference into trace.
    metadata_output_parsed = {}
    metadata_output_plan = {"metadata_refs": metadata_refs}
    next_payload = payload
    next_payload["intent_plan"] = normalized_plan
    next_payload["metadata_refs"] = _merge_output_metadata_refs(
        metadata_output_parsed,
        metadata_output_plan,
        _merge_metadata_ref_lists(
            metadata_refs,
            _plan_metadata_refs(normalized_plan),
        ),
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
        "previous_result_pseudo_job_guard": previous_result_pseudo_job_guard,
        "decision_reason": decision_reasons,
        "decision_reason_normalization": decision_reason_normalization,
        "context_date_guard": context_date_guard,
        "optional_date_filter_guard": optional_date_filter_guard,
        "inherited_filter_compatibility_guard": inherited_filter_compatibility_guard,
        "business_time_guard": business_time_guard,
        "metadata_ref_guard": metadata_ref_guard,
        "domain_selection": domain_selection,
        "domain_metric_source_guard": domain_metric_source_guard,
        "domain_condition_guard": domain_condition_guard,
        "domain_filter_contract_guard": domain_filter_contract_guard,
        "process_group_field_guard": process_group_field_guard,
        "process_scope_guard": process_scope_guard,
        "filter_operator_normalization": filter_operator_normalization,
        "function_owned_filter_normalization": function_owned_filter_normalization,
        "followup_contract_guard": followup_contract_guard,
        "function_case_input_reconciliation": function_case_input_reconciliation,
        "function_case_source_sufficiency": function_case_source_sufficiency,
        "source_sufficiency_pruning": source_sufficiency_pruning,
        "auto_function_case_selection": auto_function_case_selection,
        "function_case_execution_contracts": function_case_execution_contracts,
        "reference_scope_normalization": reference_scope_normalization,
        "reference_mode_guard": reference_mode_guard,
        "row_match_guard": row_match_guard,
        "pandas_column_normalization": pandas_column_normalization,
        "domain_execution_contracts": domain_execution_contracts,
        "aggregate_grain_alignment": aggregate_grain_alignment,
        "output_contract_column_normalization": output_contract_column_normalization,
        "typed_input_binding": typed_input_binding,
        "source_alias_reconciliation": source_alias_reconciliation,
        "external_source_catalog_binding": external_source_catalog_binding,
        "execution_catalog_resolution": execution_catalog_resolution,
        "temporal_metric_alignment": temporal_metric_alignment,
        "metric_dataset_selection": metric_dataset_selection,
        "source_dataset_selection": source_dataset_selection,
        "recipe_source_compatibility": recipe_source_compatibility,
        "resolved_grain_columns": (resolved_output_grain_plan or resolved_grain_plan).get("grain_columns", []) if (resolved_output_grain_plan or resolved_grain_plan) else [],
        "execution_graph_external_source_count": len(resolved_execution_graph.get("external_source_requirements", [])),
        "resolved_join_count": len(resolved_join_plan),
        "resolved_reference_join": bool(resolved_reference_join_plan),
        "resolved_metric_merge": bool(resolved_metric_merge_plan),
        "resolved_presence_comparison": bool(resolved_presence_comparison_plan),
        "resolved_metric_comparison": bool(resolved_metric_comparison_plan),
        "metric_source_validation_errors": metric_source_errors,
        "intent_ir": deepcopy(intent_ir),
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
        and reference_mode == "previous_source"
        and request_scope == "followup_expand_source"
    ):
        # 원본 source를 재사용해 새 grain을 계산하는 경로는 retrieval job이
        # 남아 있어도 expand_source 의미를 보존합니다. 기존처럼 무조건
        # requery로 바꾸면 previous_result와 source 재사용을 구분할 수 없습니다.
        normalized = "followup_expand_source"
        reason = "followup_reuses_previous_source_with_expansion"
    elif (
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


# 함수 설명: 후속 질문을 LLM 출력 문자열이 아니라 이전 schema·source·Catalog 계약으로 보정합니다.
def _reconcile_followup_execution_contract(
    payload: dict[str, Any],
    plan: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    pandas_plan: list[Any],
    metadata_candidates: dict[str, Any],
    question: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Any], dict[str, Any]]:
    next_plan = deepcopy(plan)
    jobs = deepcopy(retrieval_jobs)
    steps = deepcopy(pandas_plan)
    hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    if hint.get("followup_candidate") is not True:
        return next_plan, jobs, steps, {"status": "not_needed", "reason": "not_followup"}

    # An upstream binding is a fallback for a required parameter that is not
    # present in the current question.  If the intent already supplies every
    # catalog-required parameter, keep this as an independent retrieval even
    # when the conversation state makes it look like a follow-up.
    direct_required_jobs = _direct_required_parameter_evidence(
        jobs,
        metadata_candidates,
        question,
    )
    if direct_required_jobs:
        next_plan["request_scope"] = "new_analysis"
        next_plan["reference_mode"] = "none"
        next_plan["reuse_strategy"] = "none"
        return next_plan, jobs, steps, {
            "status": "applied",
            "kind": "direct_required_parameter",
            "reason": "explicit_required_parameter_precedes_previous_result_binding",
            "direct_jobs": direct_required_jobs,
            "llm_request_scope": str(plan.get("request_scope") or ""),
            "llm_reference_mode": str(plan.get("reference_mode") or ""),
        }

    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    previous_result_columns = _merge_strings(
        _string_list(current_data.get("columns")),
        _string_list(current_data.get("result_columns")),
    )
    source_columns_by_alias = (
        current_data.get("source_columns_by_alias")
        if isinstance(current_data.get("source_columns_by_alias"), dict)
        else {}
    )
    reusable_aliases = set(
        _string_list(hint.get("reusable_previous_source_aliases"))
    )
    if not reusable_aliases:
        reusable_aliases = {
            str(alias).strip()
            for alias, columns in source_columns_by_alias.items()
            if str(alias).strip() and _string_list(columns)
        }

    previous_ids = _followup_previous_identifier_columns(payload)
    detail_requested = _followup_has_detail_cue(question, hint)

    # 1) 이전 결과의 식별자로 연결할 수 있는 dependent Catalog가 하나뿐이면
    #    history/detail 조회로 확정합니다. dataset 이름(HOLD 등)은 사용하지 않습니다.
    dependent_matches = _find_dependent_catalog_matches(
        question,
        metadata_candidates,
        previous_ids,
        detail_requested,
        {
            str(job.get("dataset_key") or "").strip()
            for job in jobs
            if isinstance(job, dict)
        },
    )
    dependent = dependent_matches[0] if len(dependent_matches) == 1 else {}
    if dependent:
        target_item = dependent["item"]
        target_payload = _metadata_payload(target_item)
        dataset_key = dependent["dataset_key"]
        old_aliases = {
            str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            for job in jobs
            if isinstance(job, dict)
        }
        target_job = _build_dependent_retrieval_job(
            target_item,
            metadata_candidates,
            jobs,
            dependent["binding"],
        )
        target_alias = str(target_job.get("source_alias") or dataset_key).strip()
        steps = _rewrite_followup_source_steps(steps, old_aliases, target_alias)
        detail_columns = _string_list(target_payload.get("default_detail_columns"))
        if detail_columns:
            next_plan["output_contract"] = _detail_output_contract(
                detail_columns,
                next_plan.get("output_contract"),
            )
        next_plan["retrieval_jobs"] = [target_job]
        next_plan["pandas_execution_plan"] = steps
        next_plan["request_scope"] = "followup_requery"
        next_plan["reference_mode"] = "previous_result_rows"
        next_plan["reuse_strategy"] = "previous_result"
        next_plan["metadata_refs"] = _merge_metadata_ref_lists(
            _metadata_refs({}, next_plan),
            [{"section": "table_catalog", "key": dataset_key}],
        )
        reference_contract = {
            "mode": "previous_result_rows",
            "scope": "followup_requery",
            "target_dataset": dataset_key,
            "target_source_alias": target_alias,
            "previous_columns": previous_ids,
            "binding": deepcopy(dependent["binding"]),
            "selection_source": "catalog_upstream_binding",
        }
        return next_plan, [target_job], steps, {
            "status": "applied",
            "kind": "dependent_retrieval",
            "reason": "detail_request_with_catalog_upstream_binding",
            "metadata_ref": {"section": "table_catalog", "key": dataset_key},
            "reference_contract": reference_contract,
            "llm_request_scope": str(plan.get("request_scope") or ""),
            "llm_reference_mode": str(plan.get("reference_mode") or ""),
        }

    # 2) 제품별·공정별 등 새 grain이 이전 결과에 없으면 결과 재변환이 아닙니다.
    requested_grain = _followup_requested_grain(plan, steps)
    missing_from_result = [
        column
        for column in requested_grain
        if _normalized_column_key(column)
        not in {_normalized_column_key(value) for value in previous_result_columns}
    ]
    if missing_from_result and (requested_grain or detail_requested):
        has_reusable_source = bool(
            reusable_aliases.intersection(
                {
                    str(job.get("source_alias") or job.get("dataset_key") or "").strip()
                    for job in jobs
                    if isinstance(job, dict)
                }
            )
            or reusable_aliases
        )
        if jobs:
            mode = "previous_source" if has_reusable_source else "previous_filters"
            scope = "followup_expand_source" if has_reusable_source else "followup_requery"
            next_plan["reference_mode"] = mode
            next_plan["request_scope"] = scope
            next_plan["reuse_strategy"] = _reuse_strategy(mode)
            return next_plan, jobs, steps, {
                "status": "applied",
                "kind": "source_grain_expansion",
                "reason": "requested_grain_missing_from_previous_result",
                "missing_result_columns": missing_from_result,
                "available_result_columns": previous_result_columns,
                "source_aliases": sorted(reusable_aliases),
                "reference_contract": {
                    "mode": mode,
                    "scope": scope,
                    "requested_grain": requested_grain,
                    "missing_from_previous_result": missing_from_result,
                    "source_requery": True,
                },
                "llm_request_scope": str(plan.get("request_scope") or ""),
                "llm_reference_mode": str(plan.get("reference_mode") or ""),
            }
        return next_plan, [], [], {
            "status": "blocked",
            "kind": "unavailable_grain",
            "reason": "requested_grain_missing_from_result_and_source",
            "missing_result_columns": missing_from_result,
            "available_result_columns": previous_result_columns,
            "validation_errors": [
                {
                    "type": "followup_result_grain_unavailable",
                    "message": "이전 결과와 재사용 가능한 원본 source에 요청한 분석 기준 컬럼이 없습니다.",
                    "missing_columns": missing_from_result,
                }
            ],
        }

    # A detail/history follow-up with an entity identifier requires a
    # metadata-proven dependent source. If the catalog is absent or ambiguous,
    # fail closed instead of reusing an unrelated aggregate/source.
    if detail_requested and previous_ids and jobs and str(plan.get("reference_mode") or "").strip() == "none":
        reason = "ambiguous" if len(dependent_matches) > 1 else "unavailable"
        return next_plan, jobs, steps, {
            "status": "blocked",
            "kind": "dependent_catalog_unresolved",
            "reason": f"dependent_catalog_{reason}",
            "validation_errors": [
                {
                    "type": "followup_dependent_catalog_unresolved",
                    "message": "후속 상세 조회에 필요한 dependent Catalog 계약을 하나로 확정하지 못했습니다.",
                    "candidate_count": len(dependent_matches),
                    "previous_identifier_columns": previous_ids,
                }
            ],
        }

    # A follow-up that really asks for a new retrieval must never retain the
    # ambiguous ``reference_mode=none`` emitted by a weak intent model. The
    # decision is source/capability based: reuse an explicitly available source
    # when one exists; otherwise inherit only the previous filters. This keeps
    # the rule generic and independent of a dataset or business term.
    if jobs and str(plan.get("request_scope") or "").strip() == "followup_requery" and str(
        plan.get("reference_mode") or ""
    ).strip() == "none":
        has_reusable_source = bool(reusable_aliases)
        mode = "previous_source" if has_reusable_source else "previous_filters"
        scope = "followup_expand_source" if has_reusable_source else "followup_requery"
        next_plan["reference_mode"] = mode
        next_plan["request_scope"] = scope
        next_plan["reuse_strategy"] = _reuse_strategy(mode)
        return next_plan, jobs, steps, {
            "status": "applied",
            "kind": "generic_followup_reference_completion",
            "reason": "followup_requery_requires_explicit_previous_reference",
            "reference_contract": {
                "mode": mode,
                "scope": scope,
                "source_aliases": sorted(reusable_aliases),
                "filter_inheritance": mode == "previous_filters",
            },
            "llm_request_scope": str(plan.get("request_scope") or ""),
            "llm_reference_mode": str(plan.get("reference_mode") or ""),
        }

    return next_plan, jobs, steps, {
        "status": "not_needed",
        "reason": "no_catalog_proven_dependent_or_grain_conflict",
    }


# 함수 설명: 후속 상태에 남은 식별 컬럼 중 Catalog upstream binding과 연결 가능한 컬럼만 반환합니다.
# 주요 함수: 현재 질문의 직접 필수 조건이 완결되었는지 Catalog 기준으로 판별합니다.
# dataset 또는 업무 용어에 의존하지 않으며, 값 자체는 trace에 남기지 않습니다.
def _direct_required_parameter_evidence(
    retrieval_jobs: list[dict[str, Any]],
    candidates: dict[str, Any],
    question: str = "",
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        dataset_key = str(job.get("dataset_key") or "").strip()
        required_params = _catalog_required_params(candidates, dataset_key)
        if not dataset_key or not required_params:
            continue
        supplied_params = job.get("required_params")
        if not isinstance(supplied_params, dict):
            supplied_params = (
                job.get("params") if isinstance(job.get("params"), dict) else {}
            )
        filters = job.get("filters") if isinstance(job.get("filters"), dict) else {}
        complete = True
        for param_name in required_params:
            value = _normalized_mapping_value(supplied_params, param_name)
            if _is_nonblank_direct_parameter(value) and _question_confirms_required_parameter(
                question,
                value,
            ):
                continue
            value = _normalized_mapping_value(filters, param_name)
            if _is_nonblank_direct_parameter(value) and _question_confirms_required_parameter(
                question,
                value,
            ):
                continue
            complete = False
            break
        if complete:
            evidence.append(
                {
                    "dataset_key": dataset_key,
                    "source_alias": str(
                        job.get("source_alias") or dataset_key
                    ).strip(),
                    "required_params": required_params,
                }
            )
    return evidence


def _question_confirms_required_parameter(question: str, value: Any) -> bool:
    """Prove a nonblank required value came from the current user turn.

    Models can copy a value from the compact previous-result preview into a
    follow-up plan.  A populated field alone is therefore not evidence of an
    independent query.  Treat it as direct only when one of its scalar values
    is actually present in the new question.  This remains generic for any
    catalog-required identifier and does not infer dataset-specific terms.
    """

    question_compact = re.sub(r"[^0-9A-Za-z가-힣]+", "", str(question or "")).upper()
    if not question_compact:
        return False
    values = value if isinstance(value, (list, tuple, set)) else [value]
    for raw in values:
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("values")
        if isinstance(raw, (list, tuple, set)):
            if _question_confirms_required_parameter(question, raw):
                return True
            continue
        candidate = re.sub(r"[^0-9A-Za-z가-힣]+", "", str(raw or "")).upper()
        if len(candidate) >= 3 and candidate in question_compact:
            return True
    return False


# 주요 함수: 표기 차이를 무시하고 mapping의 계약 필드 값을 찾습니다.
def _normalized_mapping_value(mapping: dict[str, Any], target_name: str) -> Any:
    target = _normalized_column_key(target_name)
    for raw_name, value in mapping.items():
        if _normalized_column_key(raw_name) == target:
            return value
    return None


# 주요 함수: 현재 요청에서 직접 전달된 필수 조건 값을 빈 placeholder와 구분합니다.
def _is_nonblank_direct_parameter(value: Any) -> bool:
    if isinstance(value, dict):
        if "values" in value:
            return _is_nonblank_direct_parameter(value.get("values"))
        if "value" in value:
            return _is_nonblank_direct_parameter(value.get("value"))
        return False
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_is_nonblank_direct_parameter(item) for item in value)
    return True


# 주요 함수: 이전 결과에서 Catalog upstream binding에 사용할 수 있는 식별자 컬럼을 찾습니다.
def _followup_previous_identifier_columns(payload: dict[str, Any]) -> list[str]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    columns = _merge_strings(
        _string_list(current.get("columns")),
        _string_list(current.get("result_columns")),
    )
    hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    matched = hint.get("matched_cues") if isinstance(hint.get("matched_cues"), dict) else {}
    hinted = _string_list(matched.get("previous_entity_identifiers"))
    result: list[str] = []
    for column in [*hinted, *columns]:
        upper = str(column).strip().upper()
        if not upper.endswith(("_ID", "_NO")):
            continue
        if any(_normalized_column_key(column) == _normalized_column_key(item) for item in result):
            continue
        result.append(str(column).strip())
    return result


# 함수 설명: 이력·상세·사유 요청을 공통 cue와 followup hint로 판정합니다.
def _followup_has_detail_cue(question: str, hint: dict[str, Any]) -> bool:
    matched = hint.get("matched_cues") if isinstance(hint.get("matched_cues"), dict) else {}
    if _string_list(matched.get("previous_entity_identifiers")):
        return True
    compact = str(question or "").casefold().replace(" ", "")
    return any(str(cue).casefold() in compact for cue in FUNCTION_CASE_DETAIL_CUES)


# 함수 설명: 이전 결과 식별자와 Catalog upstream binding을 모두 만족하는 dependent dataset을 하나만 선택합니다.
def _find_dependent_catalog_matches(
    question: str,
    candidates: dict[str, Any],
    previous_columns: list[str],
    detail_requested: bool,
    current_dataset_keys: set[str],
) -> list[dict[str, Any]]:
    if not detail_requested or not previous_columns:
        return []
    matches: list[dict[str, Any]] = []
    for raw_item in candidates.get("table_catalog_items", []) if isinstance(candidates.get("table_catalog_items"), list) else []:
        if not isinstance(raw_item, dict):
            continue
        item = _metadata_payload(raw_item)
        dataset_key = str(raw_item.get("dataset_key") or item.get("dataset_key") or "").strip()
        if not dataset_key or dataset_key in current_dataset_keys:
            continue
        bindings = _catalog_upstream_bindings(raw_item)
        for binding in bindings:
            source_column = str(
                binding.get("source_column") or binding.get("source_column_name") or ""
            ).strip()
            target_param = str(
                binding.get("target_param") or binding.get("required_param") or binding.get("param") or ""
            ).strip()
            source_alias = str(binding.get("source_alias") or binding.get("source") or "").strip()
            if not source_column or not target_param:
                continue
            if source_alias and source_alias not in {"previous_result", "upstream_result", PREVIOUS_RESULT_ALIAS}:
                continue
            if not any(_normalized_column_key(source_column) == _normalized_column_key(column) for column in previous_columns):
                continue
            required = _catalog_required_params(candidates, dataset_key)
            if required and not any(
                _normalized_column_key(target_param) == _normalized_column_key(value)
                for value in required
            ):
                continue
            criteria = item.get("selection_criteria") if isinstance(item.get("selection_criteria"), dict) else {}
            if criteria:
                required_previous = _string_list(
                    criteria.get("required_previous_columns")
                    or criteria.get("required_upstream_columns")
                )
                if required_previous and not all(
                    any(_normalized_column_key(value) == _normalized_column_key(column) for column in previous_columns)
                    for value in required_previous
                ):
                    continue
                required_aliases = _string_list(
                    criteria.get("required_any_aliases")
                    or criteria.get("required_terms_any")
                )
                if required_aliases and not any(_domain_alias_matches(question, alias) for alias in required_aliases):
                    continue
            matches.append(
                {
                    "item": raw_item,
                    "dataset_key": dataset_key,
                    "binding": deepcopy(binding),
                }
            )
            break
    return matches


# 함수 설명: 후속 조회 질문과 이전 결과 컬럼에 맞는 dependent Catalog 후보를 찾습니다.
def _find_dependent_catalog_candidate(
    question: str,
    candidates: dict[str, Any],
    previous_columns: list[str],
    detail_requested: bool,
    current_dataset_keys: set[str],
) -> dict[str, Any]:
    """Backward-compatible single-candidate wrapper for existing callers."""
    matches = _find_dependent_catalog_matches(
        question,
        candidates,
        previous_columns,
        detail_requested,
        current_dataset_keys,
    )
    return matches[0] if len(matches) == 1 else {}


# 함수 설명: Catalog의 upstream_bindings를 여러 저장 위치에서 읽어 실행용 단일 목록으로 만듭니다.
def _catalog_upstream_bindings(item: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _metadata_payload(item)
    source_config = payload.get("source_config") if isinstance(payload.get("source_config"), dict) else {}
    raw = source_config.get("upstream_bindings") or payload.get("upstream_bindings") or item.get("upstream_bindings") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [deepcopy(value) for value in raw if isinstance(value, dict)] if isinstance(raw, list) else []


# 함수 설명: dependent Catalog의 required parameter·source 설정을 blank placeholder와 binding으로 구성합니다.
def _build_dependent_retrieval_job(
    item: dict[str, Any],
    candidates: dict[str, Any],
    existing_jobs: list[dict[str, Any]],
    binding: dict[str, Any],
) -> dict[str, Any]:
    payload = _metadata_payload(item)
    dataset_key = str(item.get("dataset_key") or payload.get("dataset_key") or "").strip()
    existing = next(
        (
            deepcopy(job)
            for job in existing_jobs
            if isinstance(job, dict) and str(job.get("dataset_key") or "").strip() == dataset_key
        ),
        {},
    )
    alias = str(existing.get("source_alias") or payload.get("source_alias") or f"{dataset_key}_src").strip()
    job = existing or {}
    job.update({"dataset_key": dataset_key, "source_alias": alias})
    source_type = str(payload.get("source_type") or item.get("source_type") or "").strip()
    if source_type:
        job["source_type"] = source_type
    required_params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
    if not required_params:
        required_params = {
            str(param): ""
            for param in _catalog_required_params(candidates, dataset_key)
        }
    target_param = str(binding.get("target_param") or binding.get("required_param") or "").strip()
    if target_param and not any(
        _normalized_column_key(key) == _normalized_column_key(target_param)
        for key in required_params
    ):
        required_params[target_param] = ""
    if target_param:
        for key in list(required_params):
            if _normalized_column_key(key) == _normalized_column_key(target_param) and not str(required_params[key] or "").strip():
                required_params[key] = ""
    job["required_params"] = required_params
    job.setdefault("filters", {})
    source_config = payload.get("source_config") if isinstance(payload.get("source_config"), dict) else {}
    if source_config:
        job["source_config"] = deepcopy(source_config)
    if _catalog_upstream_bindings(item):
        job.setdefault("source_config", {})["upstream_bindings"] = _catalog_upstream_bindings(item)
    return job


# 함수 설명: 이전 parent source의 일반 필터를 dependent source에 복사하지 않고 typed source alias만 교체합니다.
def _rewrite_followup_source_steps(
    steps: list[Any],
    old_aliases: set[str],
    new_alias: str,
) -> list[Any]:
    result: list[Any] = []
    dropped_node_ids: set[str] = set()
    for raw in steps:
        if not isinstance(raw, dict):
            result.append(deepcopy(raw))
            continue
        operation = str(raw.get("operation") or raw.get("step") or "").strip().lower()
        aliases = {
            str(raw.get(key) or "").strip()
            for key in ("source_alias", "left_source_alias", "right_source_alias")
            if str(raw.get(key) or "").strip()
        }
        external_refs = {
            str(item.get("ref") or "").strip()
            for item in raw.get("inputs", [])
            if isinstance(item, dict) and str(item.get("kind") or "") == "external_source"
        } if isinstance(raw.get("inputs"), list) else set()
        if old_aliases.intersection(aliases | external_refs) and operation in {"apply_filters", "filter", "where"}:
            node_id = str(raw.get("node_id") or raw.get("output_alias") or "").strip()
            if node_id:
                dropped_node_ids.add(node_id)
            continue
        step = deepcopy(raw)
        for key in ("source_alias", "left_source_alias", "right_source_alias"):
            if str(step.get(key) or "").strip() in old_aliases:
                step[key] = new_alias
        if isinstance(step.get("inputs"), list):
            for input_item in step["inputs"]:
                if not isinstance(input_item, dict):
                    continue
                if str(input_item.get("kind") or "") == "external_source" and str(input_item.get("ref") or "").strip() in old_aliases:
                    input_item["ref"] = new_alias
                elif (
                    str(input_item.get("kind") or "") == "node_output"
                    and str(input_item.get("ref") or "").strip() in dropped_node_ids
                ):
                    input_item["kind"] = "external_source"
                    input_item["ref"] = new_alias
        result.append(step)
    if not any(isinstance(item, dict) for item in result):
        result.append(
            {
                "node_id": "select_followup_detail",
                "operation": "select_columns",
                "inputs": [{"kind": "external_source", "ref": new_alias}],
                "output_alias": "followup_detail_result",
                "source_alias": new_alias,
            }
        )
    return result


# 함수 설명: dependent Catalog의 default_detail_columns를 후속 detail 결과 계약으로 적용합니다.
def _detail_output_contract(columns: list[str], existing: Any) -> dict[str, Any]:
    contract = deepcopy(existing) if isinstance(existing, dict) else {}
    contract.update(
        {
            "result_mode": "detail",
            "required_columns": columns,
            "result_columns": columns,
            "strict_result_columns": True,
            "grain_columns": [columns[0]] if columns else [],
        }
    )
    metric_columns = [
        column
        for column in columns
        if str(column).upper().endswith(("_QTY", "_TAT", "_COUNT", "_SUM"))
    ]
    if metric_columns:
        contract["metric_columns"] = metric_columns
    return contract


# 함수 설명: 후속 질문의 group_by가 이전 결과에 존재하는지 확인해 transform/source 재조회 경로를 분리합니다.
def _followup_requested_grain(plan: dict[str, Any], steps: list[Any]) -> list[str]:
    output = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result: list[str] = []
    result = _merge_strings(
        _string_list(output.get("grain_columns")),
        _string_list(output.get("group_by")),
    )
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or "").strip().lower()
        if operation in {"groupby_and_aggregate", "group_by", "aggregate"}:
            result = _merge_strings(
                result,
                _string_list(raw.get("group_by") or raw.get("group_by_columns")),
            )
    return result


# 함수 설명: metadata Function Case 입력 계약과 질문 원문을 비교해 LLM이 빠뜨린 구조 token을 보정합니다.
def _reconcile_function_case_inputs(
    function_cases: list[dict[str, Any]],
    question: str,
    metadata_candidates: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not function_cases:
        return function_cases, {"status": "not_needed", "cases": []}
    result: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []
    for case in function_cases:
        next_case = deepcopy(case)
        function_name = str(next_case.get("function_name") or "").strip()
        if function_name != "match_product_tokens":
            result.append(next_case)
            continue
        original = str(next_case.get("input_text") or "").strip()
        policy = _function_case_token_policy(next_case, metadata_candidates)
        question_tokens = _extract_function_case_tokens(question, policy)
        existing_tokens = {
            _function_token(value)
            for value in _extract_function_case_tokens(original, policy)
        }
        missing = [
            value
            for value in question_tokens
            if _function_token(value) not in existing_tokens
        ]
        if missing:
            canonical = " ".join([*missing, original]).strip() if original else " ".join(question_tokens)
            next_case["input_text"] = canonical
            changes.append(
                {
                    "function_case_key": str(next_case.get("key") or "").strip(),
                    "function_name": function_name,
                    "original_input_text": original,
                    "canonical_input_text": canonical,
                    "question_tokens": question_tokens,
                    "missing_tokens": missing,
                    "source": "question_metadata_reconciliation",
                }
            )
        elif not original and not question_tokens:
            validation_errors.append(
                {
                    "type": "function_case_input_unresolved",
                    "message": "제품 token Function Case의 입력 token을 질문과 metadata에서 확정하지 못했습니다.",
                    "function_case_key": str(next_case.get("key") or "").strip(),
                }
            )
        result.append(next_case)
    return result, {
        "status": "blocked" if validation_errors else ("applied" if changes else "not_needed"),
        "cases": changes,
        "validation_errors": validation_errors,
    }


# 함수 설명: Domain Function Case에 선언된 token pattern을 우선 사용하고 없으면 고신뢰 공통 패턴을 적용합니다.
def _function_case_token_policy(case: dict[str, Any], candidates: dict[str, Any]) -> dict[str, Any]:
    case_key = str(case.get("key") or case.get("function_case_key") or "").strip()
    function_name = str(case.get("function_name") or "").strip()
    for item in candidates.get("domain_items", []) if isinstance(candidates.get("domain_items"), list) else []:
        if not isinstance(item, dict) or str(item.get("section") or "") != "pandas_function_cases":
            continue
        payload = _metadata_payload(item)
        if case_key and case_key != str(item.get("key") or payload.get("key") or "").strip() and function_name != str(item.get("function_name") or payload.get("function_name") or "").strip():
            continue
        policy = payload.get("token_policy")
        if isinstance(policy, dict):
            return deepcopy(policy)
    return {}


# 함수 설명: 질문 또는 Function Case 입력에서 metadata pattern과 공통 구조 token을 원문 순서대로 추출합니다.
def _extract_function_case_tokens(value: Any, policy: dict[str, Any]) -> list[str]:
    text = str(value or "")
    patterns = list(GENERIC_FUNCTION_TOKEN_PATTERNS)
    custom = policy.get("include_patterns") or policy.get("token_patterns") or []
    for pattern in _string_list(custom):
        if pattern not in patterns:
            patterns.append(pattern)
    matches: list[tuple[int, str]] = []
    for pattern in patterns:
        try:
            matches.extend((match.start(), match.group(0)) for match in re.finditer(pattern, text))
        except re.error:
            continue
    excluded = {
        _function_token(value)
        for value in _string_list(policy.get("exclude_tokens"))
    }
    ordered: list[str] = []
    for _, token in sorted(matches, key=lambda item: item[0]):
        if _function_token(token) in excluded:
            continue
        if _function_token(token) not in {_function_token(value) for value in ordered}:
            ordered.append(token)
    return ordered


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
    # ``previous_result_rows`` is meaningful only when it limits a fresh
    # retrieval.  If a model emits it for a retrieval-free follow-up transform,
    # the structural plan itself proves that the intended operation is over
    # the already materialized result.  Normalize that one reversible shape
    # rather than rejecting a valid sort/filter of the prior result.
    if (
        raw_mode == "previous_result_rows"
        and not _retrieval_jobs(plan)
        and request_scope == "followup_transform"
    ):
        return {
            "mode": "previous_result_transform",
            "source": "reference_mode_shape_reconciliation",
            "input": raw_mode,
            "issues": [],
        }
    # A transform runs over the already materialized result only.  When a
    # fresh Catalog retrieval is also present, the model is actually asking to
    # enrich the prior result with rows from that source.  Use the row-match
    # mode so the generic Typed-IR normalizer can preserve the left result
    # grain instead of treating the new source as an unrelated transform.
    if (
        raw_mode == "previous_result_transform"
        and _retrieval_jobs(plan)
        and followup_hint.get("followup_candidate") is True
        and str(followup_hint.get("reuse_strategy_hint") or "").strip()
        == "previous_result"
    ):
        return {
            "mode": "previous_result_rows",
            "source": "reference_mode_shape_reconciliation",
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
    # Some otherwise valid model plans use the legacy reuse-strategy spelling
    # in ``reference_mode`` (for example ``previous_result``) rather than the
    # Typed-IR mode name.  It is safe to normalize that spelling only from the
    # shape of the plan: a new retrieval needs previous result *rows* for
    # matching, while a retrieval-free transform operates on the prior result
    # itself.  This is intentionally independent of any particular dataset or
    # business term.
    if raw_mode == "previous_result":
        mode = (
            "previous_result_rows"
            if _retrieval_jobs(plan)
            else "previous_result_transform"
        )
        return {
            "mode": mode,
            "source": "legacy_intent_plan.reference_mode",
            "input": raw_mode,
            "issues": [],
        }
    legacy_mode_aliases = {
        "previous_intent_with_new_retrieval": "previous_filters",
        "trace_only": "previous_trace",
    }
    if raw_mode in legacy_mode_aliases:
        return {
            "mode": legacy_mode_aliases[raw_mode],
            "source": "legacy_intent_plan.reference_mode",
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


# 함수 설명: 질문이나 후속 문맥에 날짜 요청이 없을 때 catalog 필수가 아닌 LLM 임의 날짜 필터를 제거합니다.
def _apply_unrequested_optional_date_filter_guard(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
    metadata_candidates: dict[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or "").strip()
    if _question_has_date_scope(question) or _context_date_hint(payload):
        return retrieval_jobs, {"status": "not_needed", "removed_filters": []}

    result: list[Any] = []
    removed_filters: list[dict[str, Any]] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        job = deepcopy(item)
        dataset_key = str(job.get("dataset_key") or "").strip()
        source_alias = str(job.get("source_alias") or dataset_key).strip()
        required_params = {
            _normalized_column_key(value)
            for value in _catalog_required_params(metadata_candidates, dataset_key)
        }
        has_filter_object = isinstance(job.get("filters"), dict)
        filters = deepcopy(job.get("filters")) if has_filter_object else {}
        for field in list(filters):
            if (
                _is_date_filter_field(field)
                and _normalized_column_key(field) not in required_params
            ):
                removed_filters.append(
                    {
                        "dataset_key": dataset_key,
                        "source_alias": source_alias,
                        "field": str(field),
                        "condition": deepcopy(filters.pop(field)),
                    }
                )
        if has_filter_object:
            job["filters"] = filters
        result.append(job)
    return result, {
        "status": "applied" if removed_filters else "not_needed",
        "removed_filters": removed_filters,
    }


# 함수 설명: 명시 날짜, 상대 날짜, 현재 시점 표현이 질문에 실제로 있는지 판별합니다.
def _drop_unsupported_inherited_filters(
    plan: dict[str, Any],
    retrieval_jobs: list[Any],
    metadata_candidates: dict[str, Any],
    question: str = "",
) -> tuple[list[Any], dict[str, Any]]:
    """Remove inherited or implicit snapshot filters the target Catalog cannot execute.

    A condition stated in the current question remains visible and therefore
    fails closed if the selected Catalog cannot support it.  This narrowing is
    limited to fields the model explicitly marked as inherited from a prior
    turn, where a new Catalog has a different physical schema.  A relative
    temporal phrase such as ``current`` may also be projected by the model as
    a physical DATE filter even when the selected Catalog is a current
    snapshot without a date column.  That implicit projection is removed only
    when the user did not provide an explicit calendar date.
    """

    resolution = (
        plan.get("condition_resolution")
        if isinstance(plan.get("condition_resolution"), dict)
        else {}
    )
    inherited = (
        resolution.get("inherited")
        if isinstance(resolution.get("inherited"), dict)
        else {}
    )
    inherited_fields = {
        _normalized_column_key(field)
        for field in inherited
        if str(field or "").strip()
        and str(field) not in {"effective_filters", "filters", "conditions"}
    }
    has_explicit_calendar_date = _question_has_explicit_calendar_date(question)
    if not inherited_fields and has_explicit_calendar_date:
        return retrieval_jobs, {"status": "not_needed", "dropped_filters": []}

    effective_filters = (
        resolution.get("effective_filters")
        if isinstance(resolution.get("effective_filters"), dict)
        else {}
    )

    result: list[Any] = []
    dropped: list[dict[str, Any]] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        job = deepcopy(item)
        dataset_key = str(job.get("dataset_key") or "").strip()
        source_alias = str(job.get("source_alias") or dataset_key).strip()
        has_filters = isinstance(job.get("filters"), dict)
        filters = deepcopy(job.get("filters")) if has_filters else {}
        considered_fields: set[str] = set()
        has_params = isinstance(job.get("required_params"), dict)
        params = deepcopy(job.get("required_params")) if has_params else {}
        required_params = {
            _normalized_column_key(value)
            for value in _catalog_required_params(metadata_candidates, dataset_key)
        }
        for field in list(params):
            field_key = _normalized_column_key(field)
            inherited = field_key in inherited_fields
            implicit_snapshot_date = (
                _is_date_filter_field(field)
                and bool(str(question or "").strip())
                and not has_explicit_calendar_date
            )
            if not inherited and not implicit_snapshot_date:
                continue
            if field_key in required_params or _catalog_supports_domain_column(
                metadata_candidates,
                dataset_key,
                field,
            ):
                continue
            considered_fields.add(field_key)
            dropped.append(
                {
                    "dataset_key": dataset_key,
                    "source_alias": source_alias,
                    "field": str(field),
                    "condition": deepcopy(params.pop(field)),
                    "reason": (
                        "inherited_field_not_supported_by_target_catalog"
                        if inherited
                        else "implicit_date_scope_not_supported_by_target_catalog"
                    ),
                }
            )
        for field in list(filters):
            field_key = _normalized_column_key(field)
            considered_fields.add(field_key)
            inherited = field_key in inherited_fields
            implicit_snapshot_date = (
                _is_date_filter_field(field)
                and bool(str(question or "").strip())
                and not has_explicit_calendar_date
            )
            if not inherited and not implicit_snapshot_date:
                continue
            if field_key in required_params or _catalog_supports_domain_column(
                metadata_candidates,
                dataset_key,
                field,
            ):
                continue
            dropped.append(
                {
                    "dataset_key": dataset_key,
                    "source_alias": source_alias,
                    "field": str(field),
                    "condition": deepcopy(filters.pop(field)),
                    "reason": (
                        "inherited_field_not_supported_by_target_catalog"
                        if inherited
                        else "implicit_date_scope_not_supported_by_target_catalog"
                    ),
                }
            )

        # Some model responses place a temporal condition only in
        # ``effective_filters`` while leaving it out of the retrieval job.
        # The deterministic executor still consumes that map, so validate the
        # same condition against the selected Catalog instead of allowing a
        # hidden, non-executable DATE filter to reach pandas.
        for raw_alias, raw_effective in effective_filters.items():
            if not isinstance(raw_effective, dict):
                continue
            effective_dataset = str(raw_effective.get("dataset_key") or "").strip()
            if str(raw_alias).strip() not in {source_alias, dataset_key} and effective_dataset != dataset_key:
                continue
            effective_map = (
                raw_effective.get("filters")
                if isinstance(raw_effective.get("filters"), dict)
                else raw_effective
            )
            for field, condition in effective_map.items():
                if field in {"dataset_key", "filters", "source_alias"}:
                    continue
                field_key = _normalized_column_key(field)
                if field_key in considered_fields:
                    continue
                inherited = field_key in inherited_fields
                implicit_snapshot_date = (
                    _is_date_filter_field(field)
                    and bool(str(question or "").strip())
                    and not has_explicit_calendar_date
                )
                if not inherited and not implicit_snapshot_date:
                    continue
                if field_key in required_params or _catalog_supports_domain_column(
                    metadata_candidates,
                    dataset_key,
                    field,
                ):
                    continue
                considered_fields.add(field_key)
                dropped.append(
                    {
                        "dataset_key": dataset_key,
                        "source_alias": source_alias,
                        "field": str(field),
                        "condition": deepcopy(condition),
                        "reason": (
                            "inherited_field_not_supported_by_target_catalog"
                            if inherited
                            else "implicit_date_scope_not_supported_by_target_catalog"
                        ),
                    }
                )
        if has_filters:
            job["filters"] = filters
        if has_params:
            job["required_params"] = params
        result.append(job)
    return result, {
        "status": "applied" if dropped else "not_needed",
        "dropped_filters": dropped,
    }


def _question_has_explicit_calendar_date(question: str) -> bool:
    """Return true only for a concrete date, never for relative time words."""

    text = str(question or "")
    return bool(
        re.search(r"(?<!\d)20\d{6}(?!\d)", text)
        or re.search(r"(?<!\d)20\d{2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}(?!\d)", text)
        or re.search(r"(?<!\d)\d{1,2}\s*/\s*\d{1,2}(?!\d)", text)
    )


def _question_has_date_scope(question: Any) -> bool:
    text = str(question or "").strip()
    if not text:
        return False
    patterns = (
        r"(?<!\d)20\d{6}(?!\d)",
        r"(?<!\d)20\d{2}\s*(?:[-/.]|년)\s*\d{1,2}\s*(?:[-/.]|월)\s*\d{1,2}",
        r"(?<!\d)\d{1,2}\s*(?:/|월)\s*\d{1,2}\s*일?",
        r"(?<![가-힣A-Za-z0-9])(오늘|금일|현재|현시간|어제|전일)(?![가-힣A-Za-z0-9])",
        r"(이날|이 일자|그날|그 일자|해당 일자|같은 날)",
    )
    return any(re.search(pattern, text) for pattern in patterns)


# 함수 설명: metadata에 별도 역할 표기가 없어도 일반적인 날짜 컬럼 명명 규칙을 판별합니다.
def _is_date_filter_field(value: Any) -> bool:
    text = str(value or "").strip().upper()
    normalized = _normalized_column_key(text)
    if normalized in {"DATE", "기준일", "일자", "날짜"}:
        return True
    return bool(
        "DATE" in text
        or text.endswith("_DT")
        or text.startswith("DT_")
        or any(token in text for token in ("일자", "날짜", "기준일"))
    )


# 함수 설명: 제거된 임의 날짜 필터를 LLM condition_resolution 설명에서도 함께 제거합니다.
def _strip_removed_optional_date_conditions(
    condition_resolution: dict[str, Any],
    guard: dict[str, Any],
) -> dict[str, Any]:
    removed_fields = {
        _normalized_column_key(item.get("field"))
        for item in guard.get("removed_filters", [])
        if isinstance(item, dict)
    }
    if not removed_fields:
        return condition_resolution
    result = deepcopy(condition_resolution)
    for scope_name in ("new", "changed", "inherited"):
        scope = result.get(scope_name)
        if not isinstance(scope, dict):
            continue
        for field in list(scope):
            if _normalized_column_key(field) in removed_fields:
                scope.pop(field, None)
        if not scope:
            result.pop(scope_name, None)
    return result


def _strip_dropped_inherited_filter_conditions(
    condition_resolution: dict[str, Any],
    guard: dict[str, Any],
) -> dict[str, Any]:
    """Keep displayed inherited criteria consistent with executable filters."""

    dropped = [
        item for item in guard.get("dropped_filters", []) if isinstance(item, dict)
    ]
    if not dropped:
        return condition_resolution
    result = deepcopy(condition_resolution)
    dropped_keys = {_normalized_column_key(item.get("field")) for item in dropped}
    # The model can label an implicit current-snapshot DATE either as
    # ``changed`` or ``new``.  Remove it from the displayed contract together
    # with the executable job filter, while preserving all unrelated criteria.
    for scope_name in ("inherited", "changed", "new"):
        scope = result.get(scope_name)
        if not isinstance(scope, dict):
            continue
        for field in list(scope):
            if _normalized_column_key(field) in dropped_keys:
                scope.pop(field, None)
        if not scope:
            result.pop(scope_name, None)

    by_alias: dict[str, set[str]] = {}
    by_dataset: dict[str, set[str]] = {}
    for item in dropped:
        field_key = _normalized_column_key(item.get("field"))
        alias = str(item.get("source_alias") or "").strip()
        dataset_key = str(item.get("dataset_key") or "").strip()
        if alias:
            by_alias.setdefault(alias, set()).add(field_key)
        if dataset_key:
            by_dataset.setdefault(dataset_key, set()).add(field_key)
    effective = result.get("effective_filters")
    if isinstance(effective, dict):
        for alias, raw_item in effective.items():
            if not isinstance(raw_item, dict):
                continue
            dataset_key = str(raw_item.get("dataset_key") or "").strip()
            fields = set(by_alias.get(str(alias), set())) | set(
                by_dataset.get(dataset_key, set())
            )
            filters = raw_item.get("filters")
            if not fields or not isinstance(filters, dict):
                continue
            for field in list(filters):
                if _normalized_column_key(field) in fields:
                    filters.pop(field, None)
    return result


# 함수 설명: 선택된 Domain의 temporal_semantics를 공통 실행 계약으로 해석해 질문 기준일과 실제 조회일을 분리합니다.
def _typed_external_input_alias(step: dict[str, Any]) -> str:
    inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
    aliases = _string_list(
        [
            item.get("ref")
            for item in inputs
            if isinstance(item, dict)
            and str(item.get("kind") or "").strip() == "external_source"
        ]
    )
    return aliases[0] if len(aliases) == 1 else ""


# 함수 설명: `_bind_typed_external_source_aliases()`는 04 의도 계획 정규화기 처리 중 typed·external·데이터 소스·aliases 관련 값을 계산·변환하는 내부
#        helper입니다.
def _bind_typed_external_source_aliases(
    pandas_plan: list[Any],
) -> tuple[list[Any], dict[str, Any]]:
    result: list[Any] = []
    bindings: list[dict[str, Any]] = []
    for index, raw in enumerate(pandas_plan):
        if not isinstance(raw, dict):
            result.append(deepcopy(raw))
            continue
        step = deepcopy(raw)
        if not str(step.get("source_alias") or "").strip():
            alias = _typed_external_input_alias(step)
            if alias:
                step["source_alias"] = alias
                bindings.append(
                    {
                        "step_index": index,
                        "node_id": str(step.get("node_id") or "").strip(),
                        "source_alias": alias,
                        "binding_source": "direct_external_input",
                    }
                )
        result.append(step)
    # A transform/aggregate node commonly consumes the output of a helper node
    # rather than an external source directly. If its typed lineage resolves to
    # exactly one external leaf, bind that alias before column normalization so
    # the correct Table Catalog mapping is still applied.
    for index, raw in enumerate(list(result)):
        if not isinstance(raw, dict) or str(raw.get("source_alias") or "").strip():
            continue
        lineage_aliases = _step_external_source_aliases(raw, result)
        if len(lineage_aliases) != 1:
            continue
        step = deepcopy(raw)
        step["source_alias"] = lineage_aliases[0]
        result[index] = step
        bindings.append(
            {
                "step_index": index,
                "node_id": str(step.get("node_id") or "").strip(),
                "source_alias": lineage_aliases[0],
                "binding_source": "single_external_leaf_lineage",
            }
        )
    return result, {
        "status": "applied" if bindings else "not_needed",
        "bindings": bindings,
    }


# 함수 설명: `_pandas_plan_lineage()`는 PLAN·lineage 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
# Function description: retrieval job aliases own runtime source identity.
# This helper repairs only a unique lexical alias variant and leaves ambiguous
# references unchanged for the ordinary Catalog validation path.
def _reconcile_execution_source_aliases(
    plan_value: dict[str, Any],
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
) -> tuple[dict[str, Any], list[Any], dict[str, Any]]:
    """Align structural source references with unique retrieval-job aliases."""

    candidates: list[dict[str, str]] = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        if alias and dataset_key:
            candidates.append({"alias": alias, "dataset_key": dataset_key})

    def stem(value: Any) -> str:
        normalized = _normalized_alias(value)
        for suffix in ("_source", "_src", "_data", "_dataset"):
            if normalized.endswith(suffix):
                return normalized[: -len(suffix)]
        return normalized

    def canonical_alias(reference: Any) -> str:
        value = str(reference or "").strip()
        if not value:
            return ""
        exact = list(
            dict.fromkeys(
                item["alias"]
                for item in candidates
                if value.casefold()
                in {item["alias"].casefold(), item["dataset_key"].casefold()}
            )
        )
        if len(exact) == 1:
            return exact[0]
        value_stem = stem(value)
        matches = list(
            dict.fromkeys(
                item["alias"]
                for item in candidates
                if value_stem
                and value_stem in {stem(item["alias"]), stem(item["dataset_key"])}
            )
        )
        return matches[0] if len(matches) == 1 else ""

    rewrites: list[dict[str, str]] = []

    def rewrite(value: Any, field: str) -> Any:
        original = str(value or "").strip()
        replacement = canonical_alias(original)
        if replacement and original and replacement != original:
            rewrites.append({"field": field, "from": original, "to": replacement})
            return replacement
        return value

    rewritten_plan = deepcopy(plan_value)
    rewritten_steps: list[Any] = []
    alias_fields = (
        "source_alias",
        "left_source_alias",
        "right_source_alias",
        "reference_source_alias",
        "target_source_alias",
    )
    for raw_step in pandas_plan:
        if not isinstance(raw_step, dict):
            rewritten_steps.append(deepcopy(raw_step))
            continue
        step = deepcopy(raw_step)
        for field in alias_fields:
            if field in step:
                step[field] = rewrite(step.get(field), f"pandas_execution_plan.{field}")
        if isinstance(step.get("inputs"), list):
            inputs: list[Any] = []
            for raw_input in step["inputs"]:
                if not isinstance(raw_input, dict):
                    inputs.append(deepcopy(raw_input))
                    continue
                item = deepcopy(raw_input)
                if str(item.get("kind") or "").strip() == "external_source":
                    item["ref"] = rewrite(item.get("ref"), "pandas_execution_plan.inputs.ref")
                inputs.append(item)
            step["inputs"] = inputs
        rewritten_steps.append(step)

    def rewrite_nested(value: Any, field: str) -> Any:
        if isinstance(value, list):
            return [rewrite_nested(item, field) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = deepcopy(value)
        for alias_field in alias_fields:
            if alias_field in result:
                result[alias_field] = rewrite(result.get(alias_field), field)
        return result

    for key in (
        "pandas_function_case",
        "pandas_function_cases",
        "selected_function_cases",
        "grain_plan",
        "join_plan",
        "resolved_grain_plan",
        "resolved_join_plan",
    ):
        if key in rewritten_plan:
            rewritten_plan[key] = rewrite_nested(rewritten_plan.get(key), key)
    contract = rewritten_plan.get("output_contract")
    if isinstance(contract, dict) and isinstance(contract.get("metric_bindings"), list):
        rewritten_contract = deepcopy(contract)
        rewritten_contract["metric_bindings"] = rewrite_nested(
            contract["metric_bindings"],
            "output_contract.metric_bindings",
        )
        rewritten_plan["output_contract"] = rewritten_contract
    unique_rewrites = list(
        dict.fromkeys((item["field"], item["from"], item["to"]) for item in rewrites)
    )
    return rewritten_plan, rewritten_steps, {
        "status": "applied" if unique_rewrites else "not_needed",
        "rewrites": [
            {"field": field, "from": before, "to": after}
            for field, before, after in unique_rewrites
        ],
    }


# 함수 설명: 왼쪽 원본만으로 질문의 필터·표시 계약을 만족하는 단순 left join은 보조 source를 제거합니다.
def _prune_source_sufficient_left_joins(
    plan: dict[str, Any],
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    function_cases: list[Any],
    question: str,
) -> tuple[dict[str, Any], list[Any], list[Any], dict[str, Any]]:
    """Remove an unconsumed auxiliary source from a simple preserve-left join.

    This is deliberately a data-contract rule, not a question-pattern rule.
    It applies only when a single left join declares population preservation,
    every requested output column is available from the left catalog, and no
    selected helper or metric is owned by the right source.  Any ambiguity
    leaves the original multi-source plan untouched.
    """

    next_plan = deepcopy(plan)
    jobs = [deepcopy(item) for item in retrieval_jobs if isinstance(item, dict)]
    steps = [deepcopy(item) for item in pandas_plan if isinstance(item, dict)]
    join_indexes = [
        index
        for index, step in enumerate(steps)
        if str(step.get("operation") or step.get("step") or "").strip().lower()
        == "join"
    ]
    if len(join_indexes) != 1 or len(jobs) < 2:
        return next_plan, jobs, steps, {"status": "not_needed"}

    join_index = join_indexes[0]
    join_step = steps[join_index]
    join_type = str(join_step.get("join_type") or "").strip().lower()
    population_policy = str(join_step.get("population_policy") or "").strip().lower()
    if join_type != "left" or population_policy not in {
        "preserve_left_rows",
        "preserve_left",
    }:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "join_does_not_declare_preserve_left_population",
        }

    inputs = join_step.get("inputs") if isinstance(join_step.get("inputs"), list) else []
    if len(inputs) != 2 or not all(isinstance(item, dict) for item in inputs):
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "join_inputs_not_two_typed_sources",
        }
    nodes_by_id, output_aliases = _pandas_plan_lineage(steps)
    known_aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in jobs
        if str(job.get("source_alias") or job.get("dataset_key") or "").strip()
    }
    left_aliases = _input_external_source_aliases(
        inputs[0], nodes_by_id, output_aliases, steps, known_aliases
    )
    right_aliases = _input_external_source_aliases(
        inputs[1], nodes_by_id, output_aliases, steps, known_aliases
    )
    if len(left_aliases) != 1 or len(right_aliases) != 1:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "join_source_lineage_not_unique",
            "left_aliases": left_aliases,
            "right_aliases": right_aliases,
        }
    left_alias, right_alias = left_aliases[0], right_aliases[0]
    job_by_alias = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip(): job
        for job in jobs
    }
    left_job = job_by_alias.get(left_alias)
    right_job = job_by_alias.get(right_alias)
    if not left_job or not right_job or left_alias == right_alias:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "join_jobs_not_resolved",
        }

    required_columns = _source_sufficiency_required_columns(next_plan, steps[join_index:])
    if not required_columns:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "output_columns_not_explicit",
        }
    left_dataset_key = str(left_job.get("dataset_key") or "").strip()
    if not left_dataset_key:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "left_dataset_key_missing",
        }
    unsupported_columns = [
        column
        for column in required_columns
        if not _explicit_catalog_column_contract(candidates, left_dataset_key, column)
    ]
    next_plan, steps, dropped_columns = _drop_unrequested_auxiliary_output_columns(
        next_plan,
        steps,
        unsupported_columns,
        question,
        candidates,
    )
    if dropped_columns:
        required_columns = _source_sufficiency_required_columns(
            next_plan, steps[join_index:]
        )
    if not all(
        _explicit_catalog_column_contract(candidates, left_dataset_key, column)
        for column in required_columns
    ):
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "left_catalog_does_not_cover_output_contract",
            "left_dataset_key": left_dataset_key,
            "required_columns": required_columns,
            "dropped_unrequested_auxiliary_columns": dropped_columns,
        }
    if _source_is_semantically_consumed(
        next_plan, steps, function_cases, right_alias, join_index
    ):
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "right_source_is_consumed_by_metric_or_transform",
            "right_source_alias": right_alias,
        }

    join_ids = {
        str(join_step.get("node_id") or "").strip(),
        str(join_step.get("output_alias") or "").strip(),
    }
    join_ids.discard("")
    if not join_ids:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "join_has_no_rewritable_identity",
        }

    rewritten_steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        if index == join_index:
            continue
        step = deepcopy(raw_step)
        step_inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        rewritten_inputs: list[Any] = []
        for raw_input in step_inputs:
            if not isinstance(raw_input, dict):
                rewritten_inputs.append(raw_input)
                continue
            input_value = deepcopy(raw_input)
            if (
                str(input_value.get("kind") or "").strip() == "node_output"
                and str(input_value.get("ref") or "").strip() in join_ids
            ):
                input_value = deepcopy(inputs[0])
            rewritten_inputs.append(input_value)
        if rewritten_inputs:
            step["inputs"] = rewritten_inputs
        for field in ("source_alias", "left_source_alias", "right_source_alias"):
            if str(step.get(field) or "").strip() in join_ids:
                step[field] = left_alias
        rewritten_steps.append(step)

    reachable_steps = _reachable_terminal_steps(rewritten_steps)
    if not reachable_steps:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "rewritten_plan_has_no_terminal_lineage",
        }
    used_aliases: set[str] = set()
    for step in reachable_steps:
        used_aliases.update(
            _step_external_source_aliases(step, reachable_steps, known_aliases)
        )
    if right_alias in used_aliases:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "right_source_remains_reachable_after_rewrite",
        }
    pruned_jobs = [
        job
        for job in jobs
        if str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        != right_alias
    ]
    if len(pruned_jobs) != len(jobs) - 1:
        return next_plan, jobs, steps, {
            "status": "not_needed",
            "reason": "right_source_job_not_uniquely_prunable",
        }
    # An LLM-provided join_plan would otherwise be resolved again after the
    # typed join step is removed.  Drop only join metadata tied to this exact
    # simple plan; unrelated multi-join plans never enter this branch.
    next_plan.pop("join_plan", None)
    return next_plan, pruned_jobs, reachable_steps, {
        "status": "pruned",
        "reason": "left_catalog_covers_output_contract_and_right_source_is_unconsumed",
        "kept_source_alias": left_alias,
        "kept_dataset_key": left_dataset_key,
        "pruned_source_alias": right_alias,
        "pruned_dataset_key": str(right_job.get("dataset_key") or "").strip(),
        "required_columns": required_columns,
        "dropped_unrequested_auxiliary_columns": dropped_columns,
    }


# 함수 설명: 질문에 근거가 없는 보조 source 전용 출력 컬럼은 left join pruning 전에 계약에서 제거합니다.
def _drop_unrequested_auxiliary_output_columns(
    plan: dict[str, Any],
    steps: list[dict[str, Any]],
    unsupported_columns: list[str],
    question: str,
    candidates: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Remove only explicitly unrequested, right-only display columns.

    A model can add a familiar metric merely because it selected a second
    catalog.  We never infer that a metric is absent: it is removed only when
    the left catalog cannot supply it *and* neither its column/label nor a
    registered metric alias appears in the question.
    """

    dropped = [
        column
        for column in _merge_strings(unsupported_columns)
        if not _question_requests_output_column(question, plan, candidates, column)
    ]
    if not dropped:
        return plan, steps, []
    next_plan = deepcopy(plan)
    contract = next_plan.get("output_contract") if isinstance(next_plan.get("output_contract"), dict) else {}
    if not contract:
        return plan, steps, []
    for key in ("required_columns", "result_columns", "grain_columns", "metric_columns"):
        if isinstance(contract.get(key), list):
            contract[key] = [
                value
                for value in contract[key]
                if str(value).strip() not in set(dropped)
            ]
    if isinstance(contract.get("metric_bindings"), list):
        contract["metric_bindings"] = [
            value
            for value in contract["metric_bindings"]
            if not isinstance(value, dict)
            or str(value.get("source_column") or value.get("output_column") or "").strip()
            not in set(dropped)
        ]
    if str(contract.get("primary_metric") or "").strip() in set(dropped):
        contract.pop("primary_metric", None)
    labels = contract.get("column_labels") if isinstance(contract.get("column_labels"), dict) else {}
    if labels:
        contract["column_labels"] = {
            key: value for key, value in labels.items() if str(key).strip() not in set(dropped)
        }
    # A detail/aggregate contract without any display column is ambiguous; do
    # not remove all columns merely to make a join disappear.
    if not _string_list(contract.get("result_columns") or contract.get("required_columns")):
        return plan, steps, []
    next_plan["output_contract"] = contract
    cleaned_steps: list[dict[str, Any]] = []
    for raw_step in steps:
        step = deepcopy(raw_step)
        for key in ("columns", "selected_columns", "result_columns", "output_columns", "comparison_columns", "projection"):
            if isinstance(step.get(key), list):
                step[key] = [
                    value for value in step[key] if str(value).strip() not in set(dropped)
                ]
        if isinstance(step.get("aggregations"), list):
            step["aggregations"] = [
                item
                for item in step["aggregations"]
                if not isinstance(item, dict)
                or str(item.get("column") or item.get("output_column") or "").strip()
                not in set(dropped)
            ]
        cleaned_steps.append(step)
    return next_plan, cleaned_steps, dropped


# 함수 설명: 컬럼명·표시명·등록 metric alias 중 질문에 실제 등장한 근거가 있는지 확인합니다.
def _question_requests_output_column(
    question: str,
    plan: dict[str, Any],
    candidates: dict[str, Any],
    column: str,
) -> bool:
    if _domain_alias_matches(question, column):
        return True
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    labels = contract.get("column_labels") if isinstance(contract.get("column_labels"), dict) else {}
    label = str(labels.get(column) or "").strip()
    if label and _domain_alias_matches(question, label):
        return True
    target = _normalized_column_key(column)
    for item in candidates.get("domain_items", []) if isinstance(candidates.get("domain_items"), list) else []:
        if not isinstance(item, dict) or str(item.get("section") or "").strip() not in {"metric_terms", "quantity_terms"}:
            continue
        payload = _metadata_payload(item)
        owned_columns = _merge_strings(
            _string_list(payload.get("columns")),
            _string_list(payload.get("column")),
            _string_list(payload.get("metric_columns")),
            _string_list(payload.get("source_column")),
        )
        if target not in {_normalized_column_key(value) for value in owned_columns}:
            continue
        aliases = _merge_strings(
            _string_list(payload.get("aliases")),
            _string_list(payload.get("display_name")),
        )
        if any(_domain_alias_matches(question, alias) for alias in aliases):
            return True
    return False


# 함수 설명: typed input이 가리키는 외부 retrieval source alias를 lineage로 추적합니다.
def _input_external_source_aliases(
    value: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    output_aliases: dict[str, str],
    pandas_plan: list[Any],
    known_aliases: set[str],
) -> list[str]:
    kind = str(value.get("kind") or "").strip()
    ref = str(value.get("ref") or "").strip()
    if kind == "external_source" and ref:
        return [ref]
    if kind != "node_output" or not ref:
        return []
    node_id = ref if ref in nodes_by_id else output_aliases.get(ref, "")
    step = nodes_by_id.get(node_id)
    if not isinstance(step, dict):
        return []
    return _step_external_source_aliases(step, pandas_plan, known_aliases)


# 함수 설명: output 계약과 join 이후의 명시 컬럼만 모아 source sufficiency를 보수적으로 판정합니다.
def _source_sufficiency_required_columns(
    plan: dict[str, Any],
    downstream_steps: list[Any],
) -> list[str]:
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    columns = _merge_strings(
        _string_list(contract.get("result_columns")),
        _string_list(contract.get("required_columns")),
        _string_list(contract.get("grain_columns")),
        _string_list(contract.get("metric_columns")),
    )
    for raw_step in downstream_steps:
        if not isinstance(raw_step, dict):
            continue
        operation = str(raw_step.get("operation") or raw_step.get("step") or "").strip().lower()
        if operation in {"groupby_and_aggregate", "group_by_and_aggregate", "aggregate"}:
            columns = _merge_strings(
                columns,
                _string_list(raw_step.get("group_by")),
                _string_list(raw_step.get("group_by_columns")),
            )
            for aggregation in raw_step.get("aggregations", []) if isinstance(raw_step.get("aggregations"), list) else []:
                if isinstance(aggregation, dict):
                    columns = _merge_strings(columns, _string_list(aggregation.get("column")))
        if operation in {"select_columns", "select", "project"}:
            for key in ("columns", "selected_columns", "result_columns", "output_columns", "projection"):
                columns = _merge_strings(columns, _string_list(raw_step.get(key)))
        if operation in {"sort_and_top_n", "sort", "order_by"}:
            columns = _merge_strings(columns, _string_list(raw_step.get("sort_by")))
    return columns


# 함수 설명: 보조 source가 metric·helper·후속 외부입력에 쓰이면 source pruning을 하지 않습니다.
def _source_is_semantically_consumed(
    plan: dict[str, Any],
    steps: list[Any],
    function_cases: list[Any],
    source_alias: str,
    join_index: int,
) -> bool:
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    for binding in contract.get("metric_bindings", []) if isinstance(contract.get("metric_bindings"), list) else []:
        if isinstance(binding, dict) and str(binding.get("source_alias") or "").strip() == source_alias:
            return True
    for item in function_cases:
        if isinstance(item, dict) and str(item.get("source_alias") or "").strip() == source_alias:
            return True
    for index, raw_step in enumerate(steps):
        if index <= join_index or not isinstance(raw_step, dict):
            continue
        inputs = raw_step.get("inputs") if isinstance(raw_step.get("inputs"), list) else []
        if any(
            isinstance(item, dict)
            and str(item.get("kind") or "").strip() == "external_source"
            and str(item.get("ref") or "").strip() == source_alias
            for item in inputs
        ):
            return True
        if str(raw_step.get("source_alias") or "").strip() == source_alias:
            return True
    return False


# 함수 설명: 재작성한 typed plan의 마지막 결과에 실제로 연결된 노드만 남깁니다.
def _reachable_terminal_steps(pandas_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not pandas_plan:
        return []
    nodes_by_id, output_aliases = _pandas_plan_lineage(pandas_plan)
    terminal_index = len(pandas_plan) - 1
    terminal_id = str(pandas_plan[terminal_index].get("node_id") or f"__step_{terminal_index + 1}").strip()
    pending = [terminal_id]
    kept_ids: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in kept_ids:
            continue
        step = nodes_by_id.get(node_id)
        if not isinstance(step, dict):
            continue
        kept_ids.add(node_id)
        for value in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
            if not isinstance(value, dict) or str(value.get("kind") or "").strip() != "node_output":
                continue
            ref = str(value.get("ref") or "").strip()
            parent = ref if ref in nodes_by_id else output_aliases.get(ref, "")
            if parent and parent not in kept_ids:
                pending.append(parent)
    result: list[dict[str, Any]] = []
    for index, step in enumerate(pandas_plan):
        node_id = str(step.get("node_id") or f"__step_{index + 1}").strip()
        if node_id in kept_ids:
            result.append(step)
    return result


def _pandas_plan_lineage(
    pandas_plan: list[Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Index typed pandas nodes and their declared output aliases."""
    nodes_by_id: dict[str, dict[str, Any]] = {}
    output_aliases: dict[str, str] = {}
    for index, raw in enumerate(pandas_plan):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id") or f"__step_{index + 1}").strip()
        nodes_by_id[node_id] = raw
        output_alias = str(
            raw.get("output_alias") or raw.get("result_alias") or ""
        ).strip()
        if output_alias:
            output_aliases[output_alias] = node_id
    return nodes_by_id, output_aliases


# 함수 설명: `_step_external_source_aliases()`는 04 의도 계획 정규화기 처리 중 external·데이터 소스·aliases 관련 값을 계산·변환하는 내부 helper입니다.
def _step_external_source_aliases(
    step: dict[str, Any],
    pandas_plan: list[Any],
    known_external_aliases: set[str] | None = None,
) -> list[str]:
    """Trace a pandas step through node_output inputs to external source leaves."""
    nodes_by_id, output_aliases = _pandas_plan_lineage(pandas_plan)
    known = set(known_external_aliases or set())

    # 함수 설명: `visit()`는 04 의도 계획 정규화기 처리 중 visit 관련 값을 계산·변환하는 내부 helper입니다.
    def visit(current: dict[str, Any], visited: set[str]) -> list[str]:
        result: list[str] = []
        inputs = current.get("inputs") if isinstance(current.get("inputs"), list) else []
        for item in inputs:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            ref = str(item.get("ref") or "").strip()
            if not ref:
                continue
            if kind == "external_source":
                if ref not in result:
                    result.append(ref)
                continue
            if kind == "node_output" and ref in known:
                if ref not in result:
                    result.append(ref)
                continue
            if kind != "node_output" or ref in visited:
                continue
            parent_id = ref if ref in nodes_by_id else output_aliases.get(ref, "")
            parent = nodes_by_id.get(parent_id)
            if not isinstance(parent, dict):
                continue
            for alias in visit(parent, {*visited, ref, parent_id}):
                if alias not in result:
                    result.append(alias)
        if result:
            return result

        raw_alias = str(current.get("source_alias") or "").strip()
        if raw_alias in output_aliases:
            parent_id = output_aliases[raw_alias]
            if parent_id not in visited and parent_id in nodes_by_id:
                return visit(nodes_by_id[parent_id], {*visited, parent_id})
        if raw_alias and (not known or raw_alias in known):
            return [raw_alias]
        return []

    return visit(step, set())


# 함수 설명: `_explicit_catalog_column_contract()`는 04 의도 계획 정규화기 처리 중 catalog·컬럼·contract 관련 값을 계산·변환하는 내부 helper입니다.
def _explicit_catalog_column_contract(
    candidates: dict[str, Any],
    dataset_key: str,
    column: str,
) -> bool:
    """Require an explicit catalog column or alias declaration for recovery."""
    item = _table_catalog_item(candidates, dataset_key)
    if not item:
        return False
    payload = _metadata_payload(item)
    has_schema = bool(_catalog_declared_columns(payload))
    has_aliases = any(
        isinstance(payload.get(name), dict) and bool(payload.get(name))
        for name in ("filter_mappings", "standard_column_aliases")
    )
    return bool(has_schema or has_aliases) and _catalog_supports_domain_column(
        candidates,
        dataset_key,
        column,
    )


# 함수 설명: `_bind_missing_external_sources_from_catalog_contracts()`는 04 의도 계획 정규화기 처리 중
#        missing·external·sources·원본·catalog·contracts 관련 값을 계산·변환하는 내부 helper입니다.
def _bind_missing_external_sources_from_catalog_contracts(
    payload: dict[str, Any],
    plan: dict[str, Any],
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
    candidates: dict[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Complete missing retrieval leaves only from unique schema/time contracts."""
    jobs = [deepcopy(item) for item in retrieval_jobs]
    request_scope = str(plan.get("request_scope") or "new_analysis").strip()
    reference_mode = str(plan.get("reference_mode") or "none").strip()
    if request_scope not in {"new_analysis", "followup_requery"} or reference_mode not in {
        "",
        "none",
        "previous_filters",
    }:
        return jobs, {"status": "not_needed", "bindings": [], "unresolved": []}

    existing_aliases = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in jobs
        if isinstance(item, dict)
    }
    external_aliases: list[str] = []
    metric_columns_by_alias: dict[str, list[str]] = {}
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        for item in inputs:
            if (
                isinstance(item, dict)
                and str(item.get("kind") or "").strip() == "external_source"
            ):
                alias = str(item.get("ref") or "").strip()
                if alias and alias not in external_aliases:
                    external_aliases.append(alias)

        aggregations = (
            step.get("aggregations")
            if isinstance(step.get("aggregations"), list)
            else []
        )
        if not aggregations and (step.get("agg_column") or step.get("aggregate_column")):
            aggregations = [
                {"column": step.get("agg_column") or step.get("aggregate_column")}
            ]
        source_columns = _string_list(
            [
                item.get("source_column")
                or item.get("column")
                or item.get("agg_column")
                for item in aggregations
                if isinstance(item, dict)
            ]
        )
        if not source_columns:
            continue
        lineage_aliases = _step_external_source_aliases(step, pandas_plan)
        if len(lineage_aliases) != 1:
            continue
        alias = lineage_aliases[0]
        metric_columns_by_alias[alias] = _merge_strings(
            metric_columns_by_alias.get(alias, []),
            source_columns,
        )

    missing_aliases = [alias for alias in external_aliases if alias not in existing_aliases]
    if not missing_aliases:
        return jobs, {"status": "not_needed", "bindings": [], "unresolved": []}

    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or "").strip()
    reference_date = str(request.get("reference_date") or "").strip()
    requested_date = _requested_question_date(question, reference_date)
    desired_scope = (
        "current_day"
        if requested_date and requested_date == reference_date
        else "history"
        if requested_date
        else ""
    )
    catalog_items = [
        item
        for item in candidates.get("table_catalog_items", [])
        if isinstance(item, dict) and str(item.get("dataset_key") or "").strip()
    ]
    bindings: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for alias in missing_aliases:
        metric_columns = metric_columns_by_alias.get(alias, [])
        eligible: list[dict[str, Any]] = []
        for item in catalog_items:
            dataset_key = str(item.get("dataset_key") or "").strip()
            if not metric_columns or _catalog_time_scope(item) != desired_scope:
                continue
            if all(
                _explicit_catalog_column_contract(candidates, dataset_key, column)
                for column in metric_columns
            ):
                eligible.append(item)
        if len(eligible) != 1:
            unresolved.append(
                {
                    "source_alias": alias,
                    "metric_columns": metric_columns,
                    "requested_time_scope": desired_scope,
                    "candidate_dataset_keys": [
                        str(item.get("dataset_key") or "").strip()
                        for item in eligible
                    ],
                    "issue": "external_source_catalog_contract_not_unique",
                }
            )
            continue

        selected = eligible[0]
        dataset_key = str(selected.get("dataset_key") or "").strip()
        required_param_names = _catalog_required_params(candidates, dataset_key)
        unsupported_params = [
            value
            for value in required_param_names
            if _normalized_column_key(value) != _normalized_column_key("DATE")
        ]
        if unsupported_params or (
            any(
                _normalized_column_key(value) == _normalized_column_key("DATE")
                for value in required_param_names
            )
            and not requested_date
        ):
            unresolved.append(
                {
                    "source_alias": alias,
                    "metric_columns": metric_columns,
                    "dataset_key": dataset_key,
                    "unresolved_required_params": unsupported_params
                    or required_param_names,
                    "issue": "external_source_required_params_unresolved",
                }
            )
            continue
        required_params = {
            value: requested_date
            for value in required_param_names
            if _normalized_column_key(value) == _normalized_column_key("DATE")
        }
        job: dict[str, Any] = {
            "dataset_key": dataset_key,
            "source_alias": alias,
        }
        if required_params:
            job["required_params"] = required_params
        selected_payload = _metadata_payload(selected)
        source_type = str(selected_payload.get("source_type") or "").strip()
        if source_type:
            job["source_type"] = source_type
        jobs.append(job)
        bindings.append(
            {
                "source_alias": alias,
                "dataset_key": dataset_key,
                "metric_columns": metric_columns,
                "requested_time_scope": desired_scope,
                "selection_source": "table_catalog.columns+selection_criteria",
            }
        )

    return jobs, {
        "status": "applied" if bindings else "unresolved",
        "bindings": bindings,
        "unresolved": unresolved,
    }


# 함수 설명: `_metric_source_alias_from_pandas_plan()`는 04 의도 계획 정규화기 처리 중 데이터 소스·alias·원본·pandas 실행·PLAN 관련 값을 계산·변환하는
#        내부 helper입니다.
def _metric_source_alias_from_pandas_plan(
    pandas_plan: list[Any],
    source_column: str,
    metric_aliases: Any = None,
) -> str:
    targets = {
        _normalized_column_key(value)
        for value in _merge_strings(
            [source_column],
            _string_list(metric_aliases),
        )
        if _normalized_column_key(value)
    }
    if not targets:
        return ""
    aliases: list[str] = []
    for raw in pandas_plan:
        if not isinstance(raw, dict):
            continue
        aggregation_columns = _string_list(
            [
                item.get("column") or item.get("source_column")
                for item in raw.get("aggregations", [])
                if isinstance(item, dict)
            ]
        )
        aggregation_columns = _merge_strings(
            aggregation_columns,
            _string_list(raw.get("agg_column") or raw.get("aggregate_column")),
        )
        if not targets.intersection(
            {_normalized_column_key(value) for value in aggregation_columns}
        ):
            continue
        lineage_aliases = _step_external_source_aliases(raw, pandas_plan)
        alias = (
            lineage_aliases[0]
            if len(lineage_aliases) == 1
            else str(raw.get("source_alias") or "").strip()
            or _typed_external_input_alias(raw)
        )
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases[0] if len(aliases) == 1 else ""


# 함수 설명: 시간 Domain이 뒤늦게 외부 source를 복원한 경우 초기 catalog binding의 미해결 항목을 실제 job 기준으로 정리합니다.
def _resolve_late_external_source_bindings(
    resolution: dict[str, Any],
    retrieval_jobs: list[Any],
) -> dict[str, Any]:
    result = deepcopy(resolution) if isinstance(resolution, dict) else {}
    aliases = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in retrieval_jobs
        if isinstance(item, dict)
        and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    }
    unresolved = [
        deepcopy(item)
        for item in result.get("unresolved", [])
        if isinstance(item, dict)
        and str(item.get("source_alias") or "").strip() not in aliases
    ]
    resolved_late = [
        deepcopy(item)
        for item in result.get("unresolved", [])
        if isinstance(item, dict)
        and str(item.get("source_alias") or "").strip() in aliases
    ]
    result["unresolved"] = unresolved
    if resolved_late:
        result["resolved_late"] = resolved_late
    if unresolved:
        result["status"] = "unresolved"
    elif result.get("bindings") or resolved_late:
        result["status"] = "applied"
    else:
        result["status"] = "not_needed"
    return result


# 함수 설명: 선택된 시간 Domain의 metric alias를 원본 source column으로 바꿔 business label이 pandas 컬럼으로 실행되지 않게 합니다.
def _align_temporal_metric_columns(
    pandas_plan: list[Any],
    business_time_guard: dict[str, Any],
    metadata_candidates: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    semantics = [
        item
        for item in business_time_guard.get("temporal_semantics", [])
        if isinstance(item, dict)
        and str(item.get("source_alias") or "").strip()
        and str(item.get("source_column") or "").strip()
    ]
    if not semantics:
        return [deepcopy(item) for item in pandas_plan], {
            "status": "not_needed",
            "corrections": [],
        }

    result = [deepcopy(item) for item in pandas_plan]
    corrections: list[dict[str, Any]] = []
    for index, step in enumerate(result):
        if not isinstance(step, dict):
            continue
        lineage_aliases = _step_external_source_aliases(step, result)
        for semantic in semantics:
            source_alias = str(semantic.get("source_alias") or "").strip()
            if source_alias not in lineage_aliases:
                continue
            source_column = str(semantic.get("source_column") or "").strip()
            dataset_key = str(semantic.get("dataset_key") or "").strip()
            registered_aliases = {
                _normalized_column_key(value)
                for value in _merge_strings(
                    [
                        source_column,
                        semantic.get("metric"),
                        semantic.get("output_column"),
                        semantic.get("business_timepoint"),
                    ],
                    _string_list(semantic.get("metric_aliases")),
                )
                if _normalized_column_key(value)
            }

            # LLM은 표시용 output 이름을 source column으로 재사용할 수 있습니다.
            # 등록된 temporal source column과 이름상 관련되지만 Table Catalog에
            # 존재하지 않는 값만 교정해, 실제로 선언된 다른 metric은 보존합니다.
            # 함수 설명: temporal Domain 원본 컬럼과 LLM 집계 컬럼의 정합성을 Table Catalog 선언 기준으로 판정합니다.
            def should_align(raw_column: str) -> bool:
                raw_key = _normalized_column_key(raw_column)
                source_key = _normalized_column_key(source_column)
                if not raw_key or raw_key == source_key:
                    return False
                exact_alias = raw_key in registered_aliases
                related_alias = any(
                    len(alias_key) >= 3
                    and (alias_key in raw_key or raw_key in alias_key)
                    for alias_key in registered_aliases
                )
                if not exact_alias and not related_alias:
                    return False
                if exact_alias:
                    return True
                candidates = (
                    metadata_candidates
                    if isinstance(metadata_candidates, dict)
                    else {}
                )
                return bool(
                    dataset_key
                    and candidates
                    and not _catalog_supports_domain_column(
                        candidates,
                        dataset_key,
                        raw_column,
                    )
                )

            aggregations = (
                step.get("aggregations")
                if isinstance(step.get("aggregations"), list)
                else []
            )
            for aggregation_index, raw in enumerate(aggregations):
                if not isinstance(raw, dict):
                    continue
                raw_column = str(
                    raw.get("source_column")
                    or raw.get("column")
                    or raw.get("agg_column")
                    or ""
                ).strip()
                if (
                    not raw_column
                    or not should_align(raw_column)
                ):
                    continue
                aggregation = deepcopy(raw)
                field_name = (
                    "source_column"
                    if "source_column" in aggregation
                    else "column"
                    if "column" in aggregation
                    else "agg_column"
                )
                aggregation[field_name] = source_column
                aggregations[aggregation_index] = aggregation
                corrections.append(
                    {
                        "step_index": index,
                        "source_alias": source_alias,
                        "from_column": raw_column,
                        "to_column": source_column,
                        "reason": "temporal_source_column_contract",
                    }
                )
            if aggregations:
                step["aggregations"] = aggregations
            for field_name in ("agg_column", "aggregate_column"):
                raw_column = str(step.get(field_name) or "").strip()
                if raw_column and should_align(raw_column):
                    step[field_name] = source_column
                    corrections.append(
                        {
                            "step_index": index,
                            "source_alias": source_alias,
                            "from_column": raw_column,
                            "to_column": source_column,
                            "reason": "temporal_source_column_contract",
                        }
                    )
            result[index] = step
    return result, {
        "status": "applied" if corrections else "not_needed",
        "corrections": corrections,
    }


# 함수 설명: `_apply_business_time_contracts()`는 04 의도 계획 정규화기 처리 중 business·TIME·contracts 관련 값을 계산·변환하는 내부 helper입니다.
def _apply_business_time_contracts(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
    metadata_candidates: dict[str, Any],
    question: str,
    metadata_refs: list[dict[str, str]],
    pandas_plan: list[Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    contracts = _selected_temporal_contracts(metadata_candidates, metadata_refs)
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
        if not source_alias_hint:
            source_alias_hint = _metric_source_alias_from_pandas_plan(
                pandas_plan or [],
                str(contract.get("source_column") or "").strip(),
                _merge_strings(
                    _string_list(contract.get("metric_aliases")),
                    [
                        contract.get("metric"),
                        contract.get("output_column"),
                        contract.get("business_timepoint"),
                    ],
                ),
            )
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


# 함수 설명: LLM의 시간 설명을 실행에 확정된 Domain 시간 계약과 일치시키고 요청 기준일과 실제 조회일을 분리해 기록합니다.
def _normalize_decision_reasons(
    plan: dict[str, Any],
    parsed: dict[str, Any],
    business_time_guard: dict[str, Any],
    optional_date_filter_guard: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    parsed_trace = parsed.get("trace") if isinstance(parsed.get("trace"), dict) else {}
    raw_reasons = _string_list(parsed_trace.get("decision_reason"))
    if not raw_reasons:
        raw_reasons = _string_list(plan.get("decision_reason"))
    removed_optional_date_reasons: list[str] = []
    removed_date_fields = [
        str(item.get("field") or "").strip()
        for item in optional_date_filter_guard.get("removed_filters", [])
        if isinstance(item, dict)
    ]
    if removed_date_fields:
        retained_reasons: list[str] = []
        for reason in raw_reasons:
            reason_upper = reason.upper()
            if any(field.upper() in reason_upper for field in removed_date_fields):
                removed_optional_date_reasons.append(reason)
            else:
                retained_reasons.append(reason)
        raw_reasons = retained_reasons

    temporal_reasons = _business_time_decision_reasons(business_time_guard)
    if not temporal_reasons:
        return raw_reasons, {
            "status": (
                "normalized" if removed_optional_date_reasons else "not_needed"
            ),
            "removed_optional_date_reasons": removed_optional_date_reasons,
            "removed_temporal_reasons": [],
        }

    retained: list[str] = []
    removed: list[str] = []
    semantics = (
        business_time_guard.get("temporal_semantics")
        if isinstance(business_time_guard.get("temporal_semantics"), list)
        else []
    )
    for reason in raw_reasons:
        if _looks_like_temporal_decision_reason(reason, semantics):
            removed.append(reason)
        else:
            retained.append(reason)

    insert_at = 1 if retained else 0
    normalized = _merge_strings(
        retained[:insert_at],
        temporal_reasons,
        retained[insert_at:],
    )
    return normalized, {
        "status": "normalized",
        "removed_optional_date_reasons": removed_optional_date_reasons,
        "removed_temporal_reasons": removed,
        "canonical_temporal_reasons": temporal_reasons,
    }


# 함수 설명: 실행에 확정된 requested_date, offset, query_date를 사람이 오해하지 않는 공통 시간 근거 문장으로 변환합니다.
def _business_time_decision_reasons(
    business_time_guard: dict[str, Any],
) -> list[str]:
    result: list[str] = []
    semantics = (
        business_time_guard.get("temporal_semantics")
        if isinstance(business_time_guard.get("temporal_semantics"), list)
        else []
    )
    for item in semantics:
        if not isinstance(item, dict):
            continue
        requested_date = str(item.get("requested_date") or "").strip()
        query_date = str(item.get("query_date") or "").strip()
        date_param = str(item.get("date_param") or "DATE").strip() or "DATE"
        try:
            offset_days = int(item.get("requested_date_offset_days") or 0)
        except (TypeError, ValueError):
            continue
        if not requested_date or not query_date:
            continue
        domain_ref = item.get("domain_ref") if isinstance(item.get("domain_ref"), dict) else {}
        domain_section = str(domain_ref.get("section") or "").strip()
        domain_key = str(domain_ref.get("key") or "").strip()
        domain_identity = ":".join(
            value for value in (domain_section, domain_key) if value
        )
        domain_label = (
            f"선택된 Domain({domain_identity})의 시간 계약"
            if domain_identity
            else "선택된 Domain의 시간 계약"
        )
        offset_label = f"{offset_days:+d}" if offset_days else "0"
        result.append(
            f"질문의 요청 기준일 {requested_date}에 {domain_label} "
            f"offset({offset_label}일)을 적용하여 실제 {date_param} 조회일을 "
            f"{query_date}로 설정했습니다."
        )
    return _merge_strings(result)


# 함수 설명: 구조화된 시간 계약과 충돌할 수 있는 LLM 날짜·offset 설명만 판별해 다른 의도 근거는 보존합니다.
def _looks_like_temporal_decision_reason(
    reason: Any,
    semantics: list[Any],
) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if "offset" in lowered or "requested_date_offset_days" in lowered:
        return True
    temporal_markers = ("기준일", "조회일", "조회 일자", "조회 날짜", "date 파라미터")
    if not any(marker.casefold() in lowered for marker in temporal_markers):
        return False
    tokens: list[str] = []
    for item in semantics:
        if not isinstance(item, dict):
            continue
        tokens.extend(
            str(item.get(key) or "").strip()
            for key in ("requested_date", "query_date", "date_param")
        )
    return any(token and token.casefold() in lowered for token in tokens)


# 함수 설명: 선택된 Domain 후보에 명시된 temporal_semantics만 실행 가능한 공통 시간 계약으로 정규화합니다.
def _compact_domain_text(value: Any) -> str:
    """Normalize metadata aliases for exact compact phrase matching."""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(value or "")).casefold()


# 함수 설명: `_domain_alias_matches()`는 alias·matches 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _domain_alias_matches(question: str, alias: str) -> bool:
    compact_alias = _compact_domain_text(alias)
    if len(compact_alias) < 2:
        return False
    compact_question = _compact_domain_text(question)
    if compact_alias not in compact_question:
        return False
    if re.fullmatch(r"[0-9A-Za-z _./&+-]+", str(alias or "")):
        escaped = re.escape(str(alias).strip())
        return bool(
            re.search(
                rf"(?<![0-9A-Za-z]){escaped}(?![0-9A-Za-z])",
                question,
                flags=re.IGNORECASE,
            )
        )
    return True


# 함수 설명: `_merge_metadata_ref_lists()`는 여러 메타데이터·참조·lists 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다.
def _merge_metadata_ref_lists(*values: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for value in values:
        items = value if isinstance(value, list) else []
        for item in items:
            ref = _metadata_ref(item)
            if ref and ref not in result:
                result.append(ref)
    return result


# 함수 설명: `_filter_incompatible_recipe_contracts()`는 조건과 우선순위에 맞는 incompatible·recipe·contracts만 골라 원래 순서를 유지해 반환합니다.
def _filter_incompatible_recipe_contracts(
    metadata_refs: list[dict[str, str]],
    raw_join_plan: Any,
    retrieval_jobs: list[Any],
    candidates: dict[str, Any],
    question: str = "",
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    """Drop recipes whose source or selection contract is not satisfied.

    Recipe selection criteria are metadata-owned.  The normalizer only
    evaluates the generic structured contract and never embeds a
    question-specific phrase or dataset rule.
    """
    selected_datasets = {
        str(item.get("dataset_key") or "").strip()
        for item in retrieval_jobs
        if isinstance(item, dict) and str(item.get("dataset_key") or "").strip()
    }
    join_items = (
        raw_join_plan
        if isinstance(raw_join_plan, list)
        else [raw_join_plan]
        if isinstance(raw_join_plan, dict)
        else []
    )
    if not selected_datasets:
        return deepcopy(metadata_refs), deepcopy(join_items), {
            "status": "not_needed",
            "removed_refs": [],
        }

    removed: list[dict[str, Any]] = []

    # 함수 설명: `compatible()`는 04 의도 계획 정규화기 처리 중 compatible 관련 값을 계산·변환하는 내부 helper입니다.
    def compatible(ref: dict[str, str]) -> bool:
        if str(ref.get("section") or "").strip() != "analysis_recipes":
            return True
        item = _find_metadata_item(candidates, ref)
        payload = _metadata_payload(item)
        selection_ok, selection_detail = _recipe_selection_criteria_match(
            question,
            payload,
        )
        if not selection_ok:
            removed.append(
                {
                    "metadata_ref": deepcopy(ref),
                    "issue": "recipe_selection_criteria_not_satisfied",
                    "selection_detail": selection_detail,
                }
            )
            return False
        required_sources = set(_string_list(payload.get("source_datasets")))
        if not required_sources or required_sources.issubset(selected_datasets):
            return True
        removed.append(
            {
                "metadata_ref": deepcopy(ref),
                "required_source_datasets": sorted(required_sources),
                "selected_dataset_keys": sorted(selected_datasets),
                "issue": "recipe_source_datasets_not_satisfied",
            }
        )
        return False

    compatible_refs = [ref for ref in metadata_refs if compatible(ref)]
    removed_identities = {
        (
            str(item.get("metadata_ref", {}).get("section") or "").strip(),
            str(item.get("metadata_ref", {}).get("key") or "").strip(),
        )
        for item in removed
    }
    compatible_joins: list[dict[str, Any]] = []
    for raw in join_items:
        if not isinstance(raw, dict):
            continue
        ref = _metadata_ref(raw.get("metadata_ref"))
        identity = (
            str(ref.get("section") or "").strip(),
            str(ref.get("key") or "").strip(),
        )
        if identity in removed_identities:
            continue
        compatible_joins.append(deepcopy(raw))
    return compatible_refs, compatible_joins, {
        "status": "applied" if removed else "not_needed",
        "removed_refs": removed,
    }


# 함수 설명: `_recipe_selection_criteria_match()`는 04 의도 계획 정규화기 처리 중 selection·적용 기준·match 관련 값을 계산·변환하는 내부 helper입니다.
def _recipe_selection_criteria_match(
    question: str,
    payload: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Evaluate a structured metadata recipe-selection contract.

    Legacy prose/list criteria are intentionally left permissive.  New or
    migrated metadata can use alias lists to make recipe applicability
    deterministic across LLMs.
    """

    criteria = payload.get("selection_criteria")
    if not isinstance(criteria, dict):
        return True, {"status": "not_declared"}

    # 함수 설명: `values()`는 04 의도 계획 정규화기 처리 중 값 관련 값을 계산·변환하는 내부 helper입니다.
    def values(*keys: str) -> list[str]:
        result: list[str] = []
        for key in keys:
            raw = criteria.get(key)
            if isinstance(raw, dict):
                raw = raw.get("aliases") or raw.get("terms") or raw.get("values")
            for value in _string_list(raw):
                if value not in result:
                    result.append(value)
        return result

    required_any = values(
        "required_any_aliases",
        "required_terms_any",
        "required_aliases_any",
    )
    required_all = values(
        "required_all_aliases",
        "required_terms_all",
        "required_aliases_all",
    )
    excluded_any = values(
        "excluded_any_aliases",
        "excluded_terms_any",
        "excluded_aliases_any",
    )

    matched_any = [alias for alias in required_any if _domain_alias_matches(question, alias)]
    missing_all = [alias for alias in required_all if not _domain_alias_matches(question, alias)]
    matched_excluded = [alias for alias in excluded_any if _domain_alias_matches(question, alias)]
    detail = {
        "status": "matched",
        "required_any_aliases": required_any,
        "matched_any_aliases": matched_any,
        "required_all_aliases": required_all,
        "missing_all_aliases": missing_all,
        "excluded_any_aliases": excluded_any,
        "matched_excluded_aliases": matched_excluded,
    }
    if required_any and not matched_any:
        detail["status"] = "rejected"
        detail["reason"] = "required_any_aliases_not_matched"
        return False, detail
    if missing_all:
        detail["status"] = "rejected"
        detail["reason"] = "required_all_aliases_not_matched"
        return False, detail
    if matched_excluded:
        detail["status"] = "rejected"
        detail["reason"] = "excluded_alias_matched"
        return False, detail
    return True, detail


# 함수 설명: `_resolve_execution_domain_selection()`는 여러 execution·도메인·selection 후보와 우선순위를 검토해 실제 사용할 값을 확정합니다.
# 함수 설명: `_ensure_selected_metric_sources()`는 질문에 직접 선택된 수량 Domain의 metric source가 누락되면 Catalog 시간·컬럼 계약으로 조회와 집계를 보완합니다.
def _ensure_selected_metric_sources(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    locked_metadata_refs: list[dict[str, str]],
) -> tuple[list[Any], list[Any], dict[str, Any]]:
    jobs = [deepcopy(item) for item in retrieval_jobs]
    steps = [deepcopy(item) for item in pandas_plan]
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or "").strip()
    reference_date = str(request.get("reference_date") or "").strip()
    requested_date = _requested_question_date(question, reference_date)
    desired_scope = (
        "current_day"
        if requested_date and requested_date == reference_date
        else "history"
        if requested_date
        else ""
    )

    contracts: list[dict[str, Any]] = []
    for reference in locked_metadata_refs:
        item = _find_metadata_item(candidates, reference)
        payload_value = _metadata_payload(item)
        # Temporal metric domains own dataset/date selection through the
        # business-time contract. Do not pre-empt that resolver with the
        # generic missing-metric source completion path.
        if isinstance(payload_value.get("temporal_semantics"), (dict, list)):
            continue
        metrics = _string_list(payload_value.get("metric_columns"))
        if not metrics:
            metrics = _string_list(payload_value.get("column"))
        if not metrics and str(item.get("section") or "") == "quantity_terms":
            metrics = _string_list(payload_value.get("columns"))
        if not metrics:
            continue
        contracts.append(
            {
                "metadata_ref": deepcopy(reference),
                "dataset_hint": str(
                    payload_value.get("data_source")
                    or payload_value.get("dataset_key")
                    or ""
                ).strip(),
                "metrics": metrics,
                "aggregation_method": str(
                    payload_value.get("aggregation_method") or "sum"
                ).strip(),
            }
        )

    catalog_items = [
        item
        for item in candidates.get("table_catalog_items", [])
        if isinstance(item, dict) and str(item.get("dataset_key") or "").strip()
    ]
    additions: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for contract in contracts:
        metrics = contract["metrics"]
        if any(
            all(
                _catalog_supports_domain_column(
                    candidates,
                    str(job.get("dataset_key") or "").strip(),
                    metric,
                )
                for metric in metrics
            )
            for job in jobs
            if isinstance(job, dict)
        ):
            continue

        # ``data_source``/``dataset_key`` declared by the matched domain is an
        # explicit ownership hint.  A catalog without a ``time_scope`` is still
        # valid for that exact hint: time scope only disambiguates alternatives,
        # it must not discard the domain's registered metric source.
        dataset_hint = str(contract.get("dataset_hint") or "").strip()
        normalized_hint = dataset_hint.casefold()
        eligible = [
            item
            for item in catalog_items
            if all(
                _explicit_catalog_column_contract(
                    candidates,
                    str(item.get("dataset_key") or "").strip(),
                    metric,
                )
                for metric in metrics
            )
            and (
                bool(normalized_hint)
                and str(item.get("dataset_key") or "").strip().casefold()
                == normalized_hint
                or not normalized_hint
                and (
                not desired_scope
                or _catalog_time_scope(item) == desired_scope
                )
            )
        ]
        if len(eligible) != 1:
            unresolved.append(
                {
                    "metadata_ref": contract["metadata_ref"],
                    "metrics": metrics,
                    "requested_time_scope": desired_scope,
                    "candidate_dataset_keys": [
                        str(item.get("dataset_key") or "").strip()
                        for item in eligible
                    ],
                    "issue": "metric_source_catalog_contract_not_unique",
                }
            )
            continue
        selected = eligible[0]
        dataset_key = str(selected.get("dataset_key") or "").strip()
        required_names = _catalog_required_params(candidates, dataset_key)
        if any(
            _normalized_column_key(name) != _normalized_column_key("DATE")
            for name in required_names
        ) or (
            any(
                _normalized_column_key(name) == _normalized_column_key("DATE")
                for name in required_names
            )
            and not requested_date
        ):
            unresolved.append(
                {
                    "metadata_ref": contract["metadata_ref"],
                    "metrics": metrics,
                    "dataset_key": dataset_key,
                    "issue": "metric_source_required_params_unresolved",
                }
            )
            continue

        # When a single planned source cannot supply the selected metric but
        # the matched domain names one catalog explicitly, retain the source
        # alias and replace only its catalog binding.  This keeps typed pandas
        # inputs, filters, and downstream contracts stable while avoiding an
        # invented second source/join.  Multi-source plans remain additive
        # below because their semantics cannot be inferred safely here.
        incompatible_jobs = [
            job
            for job in jobs
            if isinstance(job, dict)
            and not all(
                _catalog_supports_domain_column(
                    candidates,
                    str(job.get("dataset_key") or "").strip(),
                    metric,
                )
                for metric in metrics
            )
        ]
        selected_is_explicit_hint = bool(normalized_hint) and dataset_key.casefold() == normalized_hint
        if selected_is_explicit_hint and len(jobs) == 1 and len(incompatible_jobs) == 1:
            job = incompatible_jobs[0]
            previous_dataset_key = str(job.get("dataset_key") or "").strip()
            if previous_dataset_key.casefold() != dataset_key.casefold():
                previous_required = (
                    job.get("required_params")
                    if isinstance(job.get("required_params"), dict)
                    else {}
                )
                rebound_required: dict[str, Any] = {}
                for name in required_names:
                    normalized_name = _normalized_column_key(name)
                    existing_value = next(
                        (
                            value
                            for key, value in previous_required.items()
                            if _normalized_column_key(key) == normalized_name
                            and value not in (None, "", [], {})
                        ),
                        None,
                    )
                    if existing_value is not None:
                        rebound_required[name] = deepcopy(existing_value)
                    elif normalized_name == _normalized_column_key("DATE") and requested_date:
                        rebound_required[name] = requested_date
                job["dataset_key"] = dataset_key
                source_type = str(_metadata_payload(selected).get("source_type") or "").strip()
                if source_type:
                    job["source_type"] = source_type
                if rebound_required:
                    job["required_params"] = rebound_required
                else:
                    job.pop("required_params", None)
                replacements.append(
                    {
                        "metadata_ref": contract["metadata_ref"],
                        "source_alias": str(job.get("source_alias") or previous_dataset_key).strip(),
                        "from_dataset_key": previous_dataset_key,
                        "to_dataset_key": dataset_key,
                        "metrics": metrics,
                        "requested_time_scope": desired_scope,
                        "selection_source": "domain_explicit_metric_dataset",
                    }
                )
                continue

        used_aliases = {
            str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            for job in jobs
            if isinstance(job, dict)
        }
        source_alias = dataset_key
        suffix = 2
        while source_alias in used_aliases:
            source_alias = f"{dataset_key}_{suffix}"
            suffix += 1
        job: dict[str, Any] = {
            "dataset_key": dataset_key,
            "source_alias": source_alias,
        }
        required_params = {
            name: requested_date
            for name in required_names
            if _normalized_column_key(name) == _normalized_column_key("DATE")
        }
        if required_params:
            job["required_params"] = required_params
        jobs.append(job)

        existing_terminal = next(
            (
                str(step.get("node_id") or step.get("output_alias") or "").strip()
                for step in reversed(steps)
                if isinstance(step, dict)
                and str(step.get("node_id") or step.get("output_alias") or "").strip()
            ),
            "",
        )
        existing_grain = next(
            (
                _string_list(
                    step.get("group_by")
                    or step.get("group_by_columns")
                    or step.get("group_columns")
                    or step.get("group_cols")
                )
                for step in reversed(steps)
                if isinstance(step, dict)
                and str(step.get("operation") or step.get("step") or "").strip().lower()
                in {"groupby_and_aggregate", "group_by_and_aggregate", "aggregate"}
                and _string_list(
                    step.get("group_by")
                    or step.get("group_by_columns")
                    or step.get("group_columns")
                    or step.get("group_cols")
                )
            ),
            [],
        )
        aggregate_node_id = f"metadata_metric_{len(additions) + 1}"
        catalog_payload = _metadata_payload(selected)
        semantics = (
            catalog_payload.get("metric_semantics")
            if isinstance(catalog_payload.get("metric_semantics"), dict)
            else {}
        )
        aggregations = []
        for metric in metrics:
            semantic = semantics.get(metric) if isinstance(semantics.get(metric), dict) else {}
            aggregations.append(
                {
                    "column": metric,
                    "method": str(
                        semantic.get("default_rollup")
                        or contract["aggregation_method"]
                        or "sum"
                    ).strip(),
                    "output_column": metric,
                }
            )
        steps.append(
            {
                "node_id": aggregate_node_id,
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "external_source", "ref": source_alias}],
                "output_alias": f"{source_alias}_by_grain",
                "source_alias": source_alias,
                "group_by": existing_grain,
                "aggregations": aggregations,
            }
        )
        if existing_terminal:
            steps.append(
                {
                    "node_id": f"metadata_metric_join_{len(additions) + 1}",
                    "operation": "join",
                    "inputs": [
                        {"kind": "node_output", "ref": existing_terminal},
                        {"kind": "node_output", "ref": aggregate_node_id},
                    ],
                    "output_alias": f"metric_join_{len(additions) + 1}",
                    "left_source_alias": existing_terminal,
                    "right_source_alias": f"{source_alias}_by_grain",
                    "join_type": "outer",
                    "population_policy": "preserve_all_metric_source_keys",
                }
            )
        additions.append(
            {
                "metadata_ref": contract["metadata_ref"],
                "dataset_key": dataset_key,
                "source_alias": source_alias,
                "metrics": metrics,
                "requested_time_scope": desired_scope,
            }
        )
    return jobs, steps, {
        "status": "applied" if additions or replacements else ("unresolved" if unresolved else "not_needed"),
        "additions": additions,
        "replacements": replacements,
        "unresolved": unresolved,
    }


# 함수 설명: `_resolve_execution_domain_selection()`는 질문과 등록 alias가 일치하는 실행 Domain을 잠그고 모호성을 기록합니다.
def _resolve_execution_domain_selection(
    question: str,
    candidates: dict[str, Any],
    metadata_refs: list[dict[str, str]],
) -> dict[str, Any]:
    """Lock execution-bearing domain metadata when its registered alias is unique."""
    selected = {
        (str(item.get("section") or ""), str(item.get("key") or ""))
        for item in metadata_refs
        if isinstance(item, dict)
    }
    matches: list[dict[str, Any]] = []
    for item in candidates.get("domain_items", []) if isinstance(candidates.get("domain_items"), list) else []:
        if not isinstance(item, dict):
            continue
        payload = _metadata_payload(item)
        kinds: list[str] = []
        if isinstance(payload.get("temporal_semantics"), (dict, list)):
            kinds.append("temporal")
        if _metadata_filter_contracts(payload):
            kinds.append("conditions")
        if _string_list(payload.get("metric_columns")) or str(
            payload.get("column") or ""
        ).strip():
            kinds.append("metrics")
        if not kinds:
            continue
        aliases = _merge_strings(
            _string_list(payload.get("aliases")),
            _string_list(payload.get("display_name")),
        )
        matched_alias = next(
            (alias for alias in aliases if _domain_alias_matches(question, alias)),
            "",
        )
        if not matched_alias:
            continue
        ref = _metadata_ref(item)
        if ref:
            matches.append({"metadata_ref": ref, "alias": matched_alias, "kinds": kinds})

    locked: list[dict[str, str]] = []
    ambiguities: list[dict[str, Any]] = []
    for kind in ("temporal", "conditions", "metrics"):
        kind_matches = [item for item in matches if kind in item.get("kinds", [])]
        identities = {
            (item["metadata_ref"]["section"], item["metadata_ref"]["key"])
            for item in kind_matches
        }
        explicitly_selected = [
            item for item in kind_matches
            if (
                item["metadata_ref"]["section"],
                item["metadata_ref"]["key"],
            ) in selected
        ]
        if kind == "metrics":
            # A question may legitimately request several registered metrics
            # (for example plan and actual). Lock every exact alias match;
            # this is not the exclusive choice used for temporal contracts.
            for item in kind_matches:
                if item["metadata_ref"] not in locked:
                    locked.append(item["metadata_ref"])
        elif explicitly_selected:
            for item in explicitly_selected:
                if item["metadata_ref"] not in locked:
                    locked.append(item["metadata_ref"])
        elif len(identities) == 1 and kind_matches:
            ref = kind_matches[0]["metadata_ref"]
            if ref not in locked:
                locked.append(ref)
        elif len(identities) > 1:
            ambiguities.append(
                {
                    "kind": kind,
                    "matches": deepcopy(kind_matches),
                    "issue": "ambiguous_registered_domain_alias",
                }
            )
    temporal_refs = {
        (item["metadata_ref"]["section"], item["metadata_ref"]["key"])
        for item in matches
        if "temporal" in item.get("kinds", [])
    }
    return {
        "status": "ambiguous" if ambiguities else ("locked" if locked else "not_needed"),
        "selection_source": "metadata_alias_lock" if locked else "none",
        "locked_metadata_refs": locked,
        "matched_aliases": matches,
        "ambiguities": ambiguities,
        "temporal_alias_lock": any(
            (ref.get("section"), ref.get("key")) in temporal_refs for ref in locked
        ),
    }


# 함수 설명: `_catalog_supports_domain_column()`는 04 의도 계획 정규화기 처리 중 supports·도메인·컬럼 관련 값을 계산·변환하는 내부 helper입니다.
def _catalog_supports_domain_column(
    candidates: dict[str, Any],
    dataset_key: str,
    column: str,
) -> bool:
    item = _table_catalog_item(candidates, dataset_key)
    if not item:
        return True
    payload = _metadata_payload(item)
    declared = _catalog_declared_columns(payload)
    mapped = _mapped_column_candidates(item, column)
    declared_keys = {_normalized_column_key(value) for value in declared}
    mapping_groups: list[tuple[str, list[str]]] = []
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        mapping = payload.get(mapping_name)
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            mapping_groups.append((str(key), _string_list(value)))
    target = _normalized_column_key(column)
    mapped_keys = {
        _normalized_column_key(value)
        for key, values in mapping_groups
        for value in [key, *values]
    }
    if not declared_keys and not mapped_keys:
        return True
    return (
        target in mapped_keys
        or any(_normalized_column_key(value) in declared_keys for value in mapped)
        or target in declared_keys
    )


# 함수 설명: `_catalog_time_scope()`는 04 의도 계획 정규화기 처리 중 TIME·분석 범위 관련 값을 계산·변환하는 내부 helper입니다.
def _catalog_time_scope(item: dict[str, Any]) -> str:
    payload = _metadata_payload(item)
    criteria = (
        payload.get("selection_criteria")
        if isinstance(payload.get("selection_criteria"), dict)
        else {}
    )
    raw = str(criteria.get("time_scope") or payload.get("time_scope") or "").strip().lower()
    if raw in {"current", "current_day", "today", "realtime", "current_time"}:
        return "current_day"
    if raw in {"history", "historical", "past", "as_of_date"}:
        return "history"
    return raw


# 함수 설명: `_job_requested_time_scope()`는 04 의도 계획 정규화기 처리 중 requested·TIME·분석 범위 관련 값을 계산·변환하는 내부 helper입니다.
def _catalog_dataset_family(item: dict[str, Any]) -> str:
    """Return the catalog-declared dataset family for scope reconciliation."""
    payload = _metadata_payload(item)
    criteria = (
        payload.get("selection_criteria")
        if isinstance(payload.get("selection_criteria"), dict)
        else {}
    )
    return str(
        payload.get("dataset_family") or criteria.get("dataset_family") or ""
    ).strip().casefold()


# 함수 설명: `_job_requested_time_scope()`는 조회 기준일과 요청 기준일을 비교해 현재·이력 시간 범위를 계산합니다.
def _job_requested_time_scope(job: dict[str, Any], payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    reference_date = str(request.get("reference_date") or "").strip()
    required_params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
    requested_date = next(
        (
            str(value or "").strip()
            for key, value in required_params.items()
            if _normalized_column_key(key) == _normalized_column_key("DATE")
        ),
        "",
    )
    if not re.fullmatch(r"20\d{6}", requested_date) or not re.fullmatch(r"20\d{6}", reference_date):
        return ""
    return "current_day" if requested_date == reference_date else "history"


# 함수 설명: `_aggregation_source_columns_by_alias()`는 04 의도 계획 정규화기 처리 중 데이터 소스·컬럼·BY·alias 관련 값을 계산·변환하는 내부 helper입니다.
def _aggregation_source_columns_by_alias(
    pandas_plan: list[Any],
    known_external_aliases: set[str] | None = None,
) -> dict[str, list[str]]:
    """Collect source columns used by every executable analysis operation.

    The helper name is retained for compatibility, but dataset time-scope
    reconciliation must also cover detail/comparison plans that have no
    ``aggregations`` entry (for example ``compare_group_attributes``).
    """
    result: dict[str, list[str]] = {}
    known = set(known_external_aliases or set())
    # ``sort_by`` and result projections often name a column produced by an
    # earlier aggregation.  That is an execution-graph value, not a physical
    # source column which a Table Catalog must expose.  Treating it as physical
    # made an otherwise valid current-day catalog look schema-incompatible
    # (for example ``PRODUCTION_SUM`` after ``sum(PRODUCTION)``).  Collect only
    # explicitly derived aggregate outputs here; raw columns with the same
    # spelling are still collected from their actual aggregation/group input.
    derived_output_keys = {
        _normalized_column_key(
            aggregation.get("output_column")
            or aggregation.get("alias")
            or aggregation.get("name")
        )
        for step in pandas_plan
        if isinstance(step, dict)
        for aggregation in (
            step.get("aggregations")
            if isinstance(step.get("aggregations"), list)
            else []
        )
        if isinstance(aggregation, dict)
        and _normalized_column_key(
            aggregation.get("output_column")
            or aggregation.get("alias")
            or aggregation.get("name")
        )
    }
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        alias = str(step.get("source_alias") or "").strip()
        if known and alias not in known:
            lineage_aliases = _step_external_source_aliases(
                step,
                pandas_plan,
                known,
            )
            alias = lineage_aliases[0] if len(lineage_aliases) == 1 else ""
        if not alias:
            continue
        direct_source_columns: list[str] = []
        derived_reference_columns: list[str] = []
        aggregations = step.get("aggregations") if isinstance(step.get("aggregations"), list) else []
        if not aggregations and (step.get("agg_column") or step.get("aggregate_column")):
            aggregations = [
                {
                    "column": step.get("agg_column") or step.get("aggregate_column")
                }
            ]
        for aggregation in aggregations:
            if isinstance(aggregation, dict):
                direct_source_columns.append(
                    str(
                        aggregation.get("source_column")
                        or aggregation.get("column")
                        or aggregation.get("agg_column")
                        or ""
                    ).strip()
                )
        # Non-aggregate operations expose their direct source columns through
        # these common contract keys.  This remains metadata/IR driven and
        # does not encode a dataset or business-specific question.
        for key in (
            "group_by",
            "group_by_columns",
            "group_columns",
            "comparison_columns",
            "compare_columns",
            "columns",
            "projection",
            "keys",
            "join_keys",
            "on",
            "subset",
            "source_column",
            "column",
        ):
            direct_source_columns.extend(_string_list(step.get(key)))
        # These fields are often attached to an operation which consumes the
        # output of a previous aggregation.  They are not automatically raw
        # source requirements when they match a known aggregate output.
        for key in ("result_columns", "output_columns", "sort_by", "order_by", "rank_by", "rank_column"):
            derived_reference_columns.extend(_string_list(step.get(key)))
        for column in _merge_strings(direct_source_columns):
            if column and column not in result.setdefault(alias, []):
                result[alias].append(column)
        for column in _merge_strings(derived_reference_columns):
            if _normalized_column_key(column) in derived_output_keys:
                continue
            if column and column not in result.setdefault(alias, []):
                result[alias].append(column)
    return result


# 함수 설명: `_reconcile_metric_dataset_selection()`는 04 의도 계획 정규화기 처리 중 metric·데이터셋·selection 관련 값을 계산·변환하는 내부
#        helper입니다.
def _reconcile_metric_dataset_selection(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
    candidates: dict[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Select a dataset from catalog time-scope and executable column contracts.

    Detail and comparison plans often have no aggregation list.  Their
    group/comparison/projection columns still identify the source schema and
    must participate in current-day versus history dataset reconciliation.
    """
    jobs = [deepcopy(item) for item in retrieval_jobs]
    source_columns = _aggregation_source_columns_by_alias(
        pandas_plan,
        {
            str(item.get("source_alias") or item.get("dataset_key") or "").strip()
            for item in jobs
            if isinstance(item, dict)
            and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        },
    )
    catalog_items = [
        item
        for item in candidates.get("table_catalog_items", [])
        if isinstance(item, dict) and str(item.get("dataset_key") or "").strip()
    ]
    corrections: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        metric_columns = source_columns.get(alias, [])
        desired_scope = _job_requested_time_scope(job, payload)
        if not metric_columns or not desired_scope:
            continue
        current_key = str(job.get("dataset_key") or "").strip()
        current_item = _table_catalog_item(candidates, current_key)
        current_supports = all(
            _catalog_supports_domain_column(candidates, current_key, column)
            for column in metric_columns
        )
        current_scope = _catalog_time_scope(current_item)
        current_family = _catalog_dataset_family(current_item)
        if current_supports and current_scope == desired_scope:
            continue

        eligible: list[dict[str, Any]] = []
        for item in catalog_items:
            candidate_key = str(item.get("dataset_key") or "").strip()
            if _catalog_time_scope(item) != desired_scope:
                continue
            candidate_family = _catalog_dataset_family(item)
            # When the current catalog item is available, a time-scope
            # companion must belong to the same dataset family.  Column
            # overlap alone is insufficient because status/WIP catalogs can
            # expose common product keys while representing a different
            # source population.
            if current_family and candidate_family != current_family:
                continue
            if not all(
                _catalog_supports_domain_column(candidates, candidate_key, column)
                for column in metric_columns
            ):
                continue
            eligible.append(item)
        if len(eligible) != 1:
            if not current_supports or (current_scope and current_scope != desired_scope):
                unresolved.append(
                    {
                        "source_alias": alias,
                        "dataset_key": current_key,
                        "metric_columns": metric_columns,
                        "requested_time_scope": desired_scope,
                        "candidate_dataset_keys": [
                            str(item.get("dataset_key") or "").strip() for item in eligible
                        ],
                        "issue": "metric_time_scope_dataset_not_unique",
                    }
                )
            continue
        selected = eligible[0]
        selected_key = str(selected.get("dataset_key") or "").strip()
        if selected_key == current_key:
            continue
        selected_payload = _metadata_payload(selected)
        job["dataset_key"] = selected_key
        source_type = str(selected_payload.get("source_type") or "").strip()
        if source_type:
            job["source_type"] = source_type
        corrections.append(
            {
                "source_alias": alias,
                "from_dataset_key": current_key,
                "to_dataset_key": selected_key,
                "metric_columns": metric_columns,
                "requested_time_scope": desired_scope,
                "selection_source": "table_catalog.selection_criteria",
            }
        )
    return jobs, {
        "status": "applied" if corrections else ("unresolved" if unresolved else "not_needed"),
        "corrections": corrections,
        "unresolved": unresolved,
    }


# 함수 설명: `_reconcile_source_dataset_selection()`은 실행 불가능한 source만 Catalog schema로 보정하고, 의미상 후보 비교는 Intent LLM 판단으로 남깁니다.
def _reconcile_source_dataset_selection(
    payload: dict[str, Any],
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    locked_metadata_refs: list[dict[str, str]] | None = None,
    locked_source_aliases: set[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    """Keep the model's semantic dataset choice unless its schema is impossible.

    ``use_when``/``exclude_when`` are candidate guidance for the Intent LLM.
    Replacing a schema-capable source merely because another candidate has a
    higher phrase score makes otherwise valid questions non-deterministic and
    hides why a source was selected.  This reconciler therefore only repairs a
    source when the selected catalog cannot provide an executable column and a
    single schema-capable alternative exists.  Better semantic candidates are
    retained as trace-only advisories for diagnosis.
    """

    jobs = [deepcopy(item) for item in retrieval_jobs]
    catalog_items = [
        item
        for item in candidates.get("table_catalog_items", [])
        if isinstance(item, dict) and str(item.get("dataset_key") or "").strip()
    ]
    if len(jobs) == 0 or len(catalog_items) < 2:
        return jobs, {
            "status": "not_needed",
            "corrections": [],
            "advisories": [],
            "skipped": "insufficient_catalog",
        }

    known_aliases = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in jobs
        if isinstance(item, dict)
    }
    source_columns = _aggregation_source_columns_by_alias(pandas_plan, known_aliases)
    question = str(
        (payload.get("request") if isinstance(payload.get("request"), dict) else {}).get("question")
        or ""
    ).strip()
    locked = locked_metadata_refs if isinstance(locked_metadata_refs, list) else []
    stage_locked = {
        str(alias).strip()
        for alias in (locked_source_aliases or set())
        if str(alias).strip()
    }
    corrections: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        current_key = str(job.get("dataset_key") or "").strip()
        if alias in stage_locked or job.get("source_dataset_locked") is True:
            skipped.append(
                {
                    "source_alias": alias,
                    "dataset_key": current_key,
                    "reason": "validated_continuation_stage_source",
                }
            )
            continue
        required_columns = source_columns.get(alias, [])
        if not alias or not current_key or not required_columns:
            continue
        if _dataset_locked_by_recipe(current_key, locked, candidates):
            skipped.append({"source_alias": alias, "dataset_key": current_key, "reason": "explicit_recipe_source"})
            continue
        current_item = _table_catalog_item(candidates, current_key)
        current_scope = _catalog_time_scope(current_item)
        current_fit = _catalog_selection_fit(current_item, question)
        current_supports = all(
            _catalog_supports_domain_column(candidates, current_key, column)
            for column in required_columns
        )
        current_identity_penalty = _catalog_unrequested_identity_penalty(
            current_item,
            required_columns,
        )
        alternatives: list[tuple[int, int, int, dict[str, Any]]] = []
        for index, item in enumerate(catalog_items):
            candidate_key = str(item.get("dataset_key") or "").strip()
            if not candidate_key or candidate_key == current_key:
                continue
            candidate_scope = _catalog_time_scope(item)
            if current_scope and candidate_scope and candidate_scope != current_scope:
                continue
            if not all(
                _catalog_supports_domain_column(candidates, candidate_key, column)
                for column in required_columns
            ):
                continue
            fit = _catalog_selection_fit(item, question)
            identity_penalty = _catalog_unrequested_identity_penalty(
                item,
                required_columns,
            )
            alternatives.append((fit, -identity_penalty, -index, item))
        if not alternatives:
            continue
        best_fit, neg_best_penalty, _, best_item = max(
            alternatives,
            key=lambda value: (value[0], value[1], value[2]),
        )
        best_penalty = -neg_best_penalty
        schema_capable = [item for _, _, _, item in alternatives]
        should_switch = not current_supports and len(schema_capable) == 1
        if not should_switch:
            candidate_is_better = (
                best_fit > current_fit
                or (
                    best_fit == current_fit
                    and best_penalty < current_identity_penalty
                )
            )
            if candidate_is_better or not current_supports:
                advisories.append(
                    {
                        "source_alias": alias,
                        "selected_dataset_key": current_key,
                        "candidate_dataset_key": str(best_item.get("dataset_key") or "").strip(),
                        "source_columns": required_columns,
                        "current_schema_capable": current_supports,
                        "candidate_schema_capable_count": len(schema_capable),
                        "current_fit": current_fit,
                        "candidate_fit": best_fit,
                        "reason": (
                            "semantic_candidate_not_forced"
                            if current_supports
                            else "schema_capable_candidate_not_unique"
                        ),
                    }
                )
            continue
        selected_key = str(best_item.get("dataset_key") or "").strip()
        selected_payload = _metadata_payload(best_item)
        job["dataset_key"] = selected_key
        source_type = str(selected_payload.get("source_type") or "").strip()
        if source_type:
            job["source_type"] = source_type
        corrections.append(
            {
                "source_alias": alias,
                "from_dataset_key": current_key,
                "to_dataset_key": selected_key,
                "source_columns": required_columns,
                "current_fit": current_fit,
                "selected_fit": best_fit,
                "selected_identity_penalty": best_penalty,
                "current_identity_penalty": current_identity_penalty,
                "selection_source": "table_catalog.unique_schema_contract",
            }
        )
    return jobs, {
        "status": "applied" if corrections else ("advisory" if advisories else "not_needed"),
        "corrections": corrections,
        "advisories": advisories,
        "skipped": skipped,
    }


# 함수 설명: `_dataset_locked_by_recipe()`는 Domain recipe가 명시한 source를 일반 후보 보정에서 보호합니다.
def _dataset_locked_by_recipe(
    dataset_key: str,
    locked_metadata_refs: list[dict[str, str]],
    candidates: dict[str, Any],
) -> bool:
    """Return whether a selected dataset belongs to an explicit recipe source contract."""

    for ref in locked_metadata_refs:
        if str(ref.get("section") or "").strip() != "analysis_recipes":
            continue
        item = _find_metadata_item(candidates, ref)
        recipe = _metadata_payload(item)
        declared = {
            str(value).strip()
            for value in _string_list(
                recipe.get("source_datasets")
                or recipe.get("source_dataset_keys")
                or recipe.get("dependency_dataset_keys")
            )
            if str(value).strip()
        }
        if dataset_key in declared:
            return True
    return False


# 함수 설명: `_catalog_selection_fit()`은 Catalog의 사용·제외 문구와 질문의 의미 겹침 정도를 점수화합니다.
def _catalog_selection_fit(item: dict[str, Any], question: str) -> int:
    """Score catalog use/exclude phrases against the user question."""

    payload = _metadata_payload(item)
    criteria = payload.get("selection_criteria") if isinstance(payload.get("selection_criteria"), dict) else {}
    question_text = _compact_selection_text(question)
    if not question_text:
        return 0
    score = 0
    for phrase in _string_list(criteria.get("use_when")):
        score += _selection_phrase_overlap(phrase, question_text)
    for phrase in _string_list(criteria.get("exclude_when")):
        score -= _selection_phrase_overlap(phrase, question_text)
    return score


# 함수 설명: `_catalog_unrequested_identity_penalty()`는 필요한 결과 컬럼에
# 포함되지 않은 ID 계열 컬럼 수를 계산해 동률인 source의 인구/상태 스키마를
# 일반적인 실행 컬럼 계약만으로 구분합니다.
def _catalog_unrequested_identity_penalty(
    item: dict[str, Any],
    requested_columns: list[str],
) -> int:
    payload = _metadata_payload(item)
    available = _catalog_column_names(payload)
    requested = {_normalized_column_key(column) for column in requested_columns if str(column).strip()}
    identity_columns = {
        column
        for column in available
        if re.search(r"(?:ID|NO)$", column)
    }
    return len(identity_columns - requested)


# 함수 설명: `_catalog_column_names()`는 catalog의 columns 계약을 대소문자
# 구분 없이 비교할 수 있는 canonical 이름 집합으로 정리합니다.
def _catalog_column_names(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("columns") if isinstance(payload.get("columns"), list) else []
    return {
        _normalized_column_key(value)
        for value in raw
        if str(value or "").strip()
    }


# 함수 설명: `_selection_phrase_overlap()`은 하나의 Catalog 선택 문구가 질문에 얼마나 나타나는지 계산합니다.
def _selection_phrase_overlap(phrase: Any, question_text: str) -> int:
    compact_phrase = _compact_selection_text(phrase)
    if not compact_phrase:
        return 0
    if compact_phrase in question_text:
        return max(3, len(compact_phrase))
    tokens = [
        token
        for token in re.findall(r"\w+", str(phrase or "").casefold(), flags=re.UNICODE)
        if len(token) >= 2
    ]
    matched = sum(1 for token in tokens if _compact_selection_text(token) in question_text)
    return matched * 3


# 함수 설명: `_compact_selection_text()`는 선택 기준 비교를 위해 공백과 대소문자를 정규화합니다.
def _compact_selection_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


# 함수 설명: `_apply_selected_domain_conditions()`는 04 의도 계획 정규화기 처리 중 selected·도메인·conditions 관련 값을 계산·변환하는 내부
#        helper입니다.
def _metadata_filter_contracts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize metadata-owned ``conditions`` or ``filters`` contracts.

    Domain authors have historically used both shapes. Runtime execution
    consumes one canonical list without embedding a business-specific rule.
    """

    raw_values: list[Any] = []
    for field_name in ("conditions", "filters"):
        raw = payload.get(field_name)
        if raw is None:
            continue
        if isinstance(raw, list):
            raw_values.extend(raw)
        elif isinstance(raw, dict):
            if any(key in raw for key in ("column", "field", "operator", "op")):
                raw_values.append(raw)
            else:
                for column, condition in raw.items():
                    if isinstance(condition, dict):
                        item = deepcopy(condition)
                    else:
                        item = {"operator": "eq", "value": deepcopy(condition)}
                    item.setdefault("column", str(column))
                    raw_values.append(item)
    result: list[dict[str, Any]] = []
    for raw in raw_values:
        if not isinstance(raw, dict):
            continue
        column = str(raw.get("column") or raw.get("field") or "").strip()
        if not column:
            continue
        item = deepcopy(raw)
        item["column"] = column
        item["operator"] = _canonical_filter_operator(
            raw.get("operator") or raw.get("op") or "eq"
        )
        result.append(item)
    return result


# 함수 설명: `_apply_selected_domain_conditions()`는 04 의도 계획 정규화기 처리 중 selected·도메인·conditions 관련 값을 계산·변환하는 내부
#        helper입니다.
def _apply_selected_domain_conditions(
    retrieval_jobs: list[Any],
    candidates: dict[str, Any],
    locked_refs: list[dict[str, str]],
) -> tuple[list[Any], dict[str, Any]]:
    jobs = [deepcopy(item) for item in retrieval_jobs]
    applied: list[dict[str, Any]] = []
    for ref in locked_refs:
        item = _find_metadata_item(candidates, ref)
        payload = _metadata_payload(item)
        conditions = _metadata_filter_contracts(payload)
        for raw in conditions:
            if not isinstance(raw, dict):
                continue
            column = str(raw.get("column") or raw.get("field") or "").strip()
            if not column:
                continue
            operator = _canonical_filter_operator(
                str(raw.get("operator") or raw.get("op") or "eq")
            )
            scoped_datasets = set(_string_list(raw.get("dataset_keys") or raw.get("dataset_key")))
            scoped_aliases = set(_string_list(raw.get("source_aliases") or raw.get("source_alias")))
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                dataset_key = str(job.get("dataset_key") or "").strip()
                source_alias = str(job.get("source_alias") or dataset_key).strip()
                if scoped_datasets and dataset_key not in scoped_datasets:
                    continue
                if scoped_aliases and source_alias not in scoped_aliases:
                    continue
                if not _catalog_supports_domain_column(candidates, dataset_key, column):
                    continue
                filters = deepcopy(job.get("filters")) if isinstance(job.get("filters"), dict) else {}
                if any(_normalized_column_key(key) == _normalized_column_key(column) for key in filters):
                    continue
                condition: dict[str, Any] = {"operator": operator}
                if "value" in raw:
                    condition["value"] = deepcopy(raw.get("value"))
                elif "values" in raw:
                    condition["value"] = deepcopy(raw.get("values"))
                filters[column] = condition
                job["filters"] = filters
                applied.append(
                    {
                        "metadata_ref": deepcopy(ref),
                        "source_alias": source_alias,
                        "dataset_key": dataset_key,
                        "column": column,
                        "operator": operator,
                    }
                )
    return jobs, {
        "status": "applied" if applied else "not_needed",
        "applied_conditions": applied,
    }


# 함수 설명: `_enforce_selected_domain_filter_contracts()`는 04 의도 계획 정규화기 처리 중 selected·도메인·필터·contracts 관련 값을 계산·변환하는
#        내부 helper입니다.
def _enforce_selected_domain_filter_contracts(
    retrieval_jobs: list[Any],
    candidates: dict[str, Any],
    locked_refs: list[dict[str, str]],
) -> tuple[list[Any], dict[str, Any]]:
    """Use selected Domain conditions to repair incomplete LLM filter specs."""

    jobs = [deepcopy(item) for item in retrieval_jobs]
    corrections: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []
    for ref in locked_refs:
        item = _find_metadata_item(candidates, ref)
        payload = _metadata_payload(item)
        conditions = _metadata_filter_contracts(payload)
        for raw in conditions:
            if not isinstance(raw, dict):
                continue
            field = str(raw.get("column") or raw.get("field") or "").strip()
            if not field:
                continue
            operator = _canonical_filter_operator(
                raw.get("operator") or raw.get("op") or "eq"
            )
            domain_condition: dict[str, Any] = {"operator": operator}
            if "value" in raw:
                domain_condition["value"] = deepcopy(raw.get("value"))
            elif "values" in raw:
                domain_condition["value"] = deepcopy(raw.get("values"))
            scoped_datasets = set(
                _string_list(raw.get("dataset_keys") or raw.get("dataset_key"))
            )
            scoped_aliases = set(
                _string_list(raw.get("source_aliases") or raw.get("source_alias"))
            )
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                dataset_key = str(job.get("dataset_key") or "").strip()
                source_alias = str(job.get("source_alias") or dataset_key).strip()
                if scoped_datasets and dataset_key not in scoped_datasets:
                    continue
                if scoped_aliases and source_alias not in scoped_aliases:
                    continue
                if not _catalog_supports_domain_column(candidates, dataset_key, field):
                    continue
                filters = (
                    deepcopy(job.get("filters"))
                    if isinstance(job.get("filters"), dict)
                    else {}
                )
                existing_key = next(
                    (
                        key
                        for key in filters
                        if _normalized_column_key(key)
                        == _normalized_column_key(field)
                    ),
                    "",
                )
                if not existing_key:
                    filters[field] = deepcopy(domain_condition)
                    job["filters"] = filters
                    corrections.append(
                        {
                            "source_alias": source_alias,
                            "dataset_key": dataset_key,
                            "field": field,
                            "action": "domain_condition_added",
                            "operator": operator,
                        }
                    )
                    continue
                existing = filters.get(existing_key)
                if not _filter_condition_is_incomplete(existing):
                    continue
                filters[existing_key] = deepcopy(domain_condition)
                job["filters"] = filters
                corrections.append(
                    {
                        "source_alias": source_alias,
                        "dataset_key": dataset_key,
                        "field": field,
                        "action": "incomplete_llm_condition_replaced",
                        "from": deepcopy(existing),
                        "to": deepcopy(domain_condition),
                    }
                )
                if _filter_condition_is_incomplete(filters[existing_key]):
                    validation_errors.append(
                        {
                            "type": "domain_filter_contract_unresolved",
                            "message": "선택된 Domain의 필터 조건을 실행 가능한 값으로 확정하지 못했습니다.",
                            "source_alias": source_alias,
                            "field": field,
                        }
                    )
    return jobs, {
        "status": "applied" if corrections else "not_needed",
        "corrections": corrections,
        "validation_errors": validation_errors,
    }


# 함수 설명: `_filter_condition_is_incomplete()`는 조건과 우선순위에 맞는 조건·IS·incomplete만 골라 원래 순서를 유지해 반환합니다.
def _filter_condition_is_incomplete(condition: Any) -> bool:
    if not isinstance(condition, dict):
        return True
    operator = _canonical_filter_operator(
        condition.get("operator") or condition.get("op") or "eq"
    )
    if operator in VALUELESS_FILTER_OPERATORS:
        return False
    values = condition.get("values")
    if not isinstance(values, list):
        values = condition.get("value")
        values = values if isinstance(values, list) else [values]
    return not any(_filter_value_is_present(value) for value in values)


# 함수 설명: `_filter_value_is_present()`는 조건과 우선순위에 맞는 값·IS·present만 골라 원래 순서를 유지해 반환합니다.
def _filter_value_is_present(value: Any) -> bool:
    """Treat numeric zero/False as valid filter values, not as blank text."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# 함수 설명: `_declared_process_scope_from_plan()`는 04 의도 계획 정규화기 처리 중 process·분석 범위·원본·PLAN 관련 값을 계산·변환하는 내부 helper입니다.
def _declared_process_scope_from_plan(
    plan: dict[str, Any],
    metadata_candidates: dict[str, Any],
) -> list[str]:
    """Extract canonical process values declared by the normalized LLM IR inputs."""

    contracts = _process_group_contracts(metadata_candidates)
    process_fields = {
        str(contract.get("field") or "OPER_NAME").strip().casefold()
        for contract in contracts
    }
    canonical_by_key = {
        str(process).strip().casefold(): str(process).strip()
        for contract in contracts
        for process in contract.get("process_values", [])
        if str(process).strip()
    }
    declared: list[str] = []

    # 함수 설명: `visit()`는 04 의도 계획 정규화기 처리 중 visit 관련 값을 계산·변환하는 내부 helper입니다.
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            field = str(value.get("field") or value.get("column") or "").strip()
            if field.casefold() in process_fields:
                for raw in _condition_scalar_values(value):
                    canonical = canonical_by_key.get(str(raw).strip().casefold())
                    if canonical and canonical not in declared:
                        declared.append(canonical)
            for key, child in value.items():
                if str(key).strip().casefold() in process_fields:
                    for raw in _condition_scalar_values(child):
                        canonical = canonical_by_key.get(str(raw).strip().casefold())
                        if canonical and canonical not in declared:
                            declared.append(canonical)
                elif key not in {"field", "column", "value", "values"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan.get("condition_resolution"))
    return declared


# 함수 설명: `_validate_process_scope_contract()`는 process·분석 범위·contract이 실행·저장 계약을 만족하는지 검사하고 위반 내용을 명시적으로 반환합니다.
def _validate_process_scope_contract(
    retrieval_jobs: list[Any],
    candidates: dict[str, Any],
    question: str,
    *,
    pandas_plan: list[Any] | None = None,
    declared_processes: list[str] | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    """Reject partial process scopes instead of returning misleading subsets."""

    if skip:
        return {"status": "skipped", "validation_errors": []}
    contracts = _process_group_contracts(candidates)
    requested = _merge_strings(
        _requested_process_scope(question, contracts),
        declared_processes or [],
    )
    if not requested:
        return {"status": "not_requested", "validation_errors": []}
    alignment = _process_filter_alignment_scope(retrieval_jobs)
    requested_keys = {str(value).strip().casefold() for value in requested}
    covered: set[str] = set()
    matched_filter = False
    scoped_aliases: set[str] = set()
    process_fields = {
        str(contract.get("field") or "OPER_NAME").strip().casefold()
        for contract in contracts
    }
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        for field, condition in _filter_field_entries(job.get("filters")):
            if str(field).strip().casefold() not in process_fields:
                continue
            values = _condition_scalar_values(condition)
            if not values:
                continue
            matched_filter = True
            covered.update(value.casefold() for value in values)
            scoped_aliases.add(
                str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            )
    missing = sorted(requested_keys - covered)
    dependent_scope_aliases = _dependent_process_scope_aliases(pandas_plan or [])
    unscoped_aliases = [
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        not in scoped_aliases
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        not in dependent_scope_aliases
    ]
    if unscoped_aliases:
        error = {
            "type": "process_scope_incomplete",
            "message": "A requested process scope is missing from one or more source filters; execution is blocked.",
            "requested_processes": requested,
            "covered_processes": sorted(covered),
            "missing_processes": missing,
            "unscoped_sources": unscoped_aliases,
        }
        return {
            "status": "error",
            "requested_processes": requested,
            "covered_processes": sorted(covered),
            "missing_processes": missing,
            "unscoped_sources": unscoped_aliases,
            "validation_errors": [error],
        }
    # A dependent history/detail source can be scoped by the previous result's
    # trusted entity rows instead of repeating the parent's process filter.
    # Do not treat that source as an unscoped broad query: the row-match step
    # is executed before the history filter/aggregation and is the effective
    # process boundary for this job.  Keep ordinary unscoped sources blocked.
    if missing and dependent_scope_aliases:
        job_aliases = {
            str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            for job in retrieval_jobs
            if isinstance(job, dict)
        }
        if job_aliases and job_aliases.issubset(dependent_scope_aliases):
            return {
                "status": "dependent_scope_allowed",
                "requested_processes": requested,
                "covered_processes": sorted(covered),
                "missing_processes": missing,
                "dependent_scope_sources": sorted(dependent_scope_aliases & job_aliases),
                "validation_errors": [],
            }
    if alignment.get("has_disjoint_scopes"):
        return {
            "status": "disjoint_scopes_allowed",
            "requested_processes": requested,
            "validation_errors": [],
        }
    if not missing:
        return {
            "status": "complete",
            "requested_processes": requested,
            "covered_processes": sorted(covered),
            "validation_errors": [],
        }
    error = {
        "type": "process_scope_incomplete",
        "message": "질문에 명시된 공정 범위가 조회 필터에 모두 반영되지 않아 실행을 차단했습니다.",
        "requested_processes": requested,
        "covered_processes": sorted(covered),
        "missing_processes": missing,
    }
    return {
        "status": "error",
        "requested_processes": requested,
        "covered_processes": sorted(covered),
        "missing_processes": missing,
        "validation_errors": [error],
    }


# 함수 설명: `_dependent_process_scope_aliases()`는 이전 결과/상위 결과의
# 식별 행으로 범위가 확정되는 source alias를 찾습니다. 이 source들은 부모
# 질문의 공정 필터를 반복하지 않아도 되지만, 일반 무범위 source와 섞이면
# 여전히 process scope 검증에서 차단됩니다.
def _dependent_process_scope_aliases(pandas_plan: list[Any]) -> set[str]:
    aliases: set[str] = set()
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation != "apply_row_match_groups":
            continue
        source_alias = str(step.get("source_alias") or "").strip()
        reference_alias = str(
            step.get("reference_source_alias")
            or step.get("reference_alias")
            or ""
        ).strip().casefold()
        if source_alias and reference_alias in {"previous_result", "upstream_result"}:
            aliases.add(source_alias)
    return aliases


# 함수 설명: `_build_intent_ir()`는 의도 계획·IR 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def _build_intent_ir(
    plan: dict[str, Any],
    question: str,
    retrieval_jobs: list[Any],
    pandas_plan: list[Any],
    output_contract: dict[str, Any],
    resolved_grain_plan: dict[str, Any],
    business_time_guard: dict[str, Any],
    validation_errors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compile the normalized plan into a compact, typed execution IR."""

    jobs = [item for item in retrieval_jobs if isinstance(item, dict)]
    aliases = [
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in jobs
    ]
    bindings = [
        item
        for item in output_contract.get("metric_bindings", [])
        if isinstance(item, dict)
    ]
    binding_aliases = _merge_strings(
        [str(item.get("source_alias") or "").strip() for item in bindings]
    )
    operations = _merge_strings(
        [
            str(item.get("operation") or item.get("step") or "").strip().lower()
            for item in pandas_plan
            if isinstance(item, dict)
        ]
    )
    complex_operations = {
        "join",
        "merge",
        "compare_presence",
        "compare_metrics",
        "compare_group_attributes",
        "find_duplicate_groups",
        "apply_row_match_groups",
        "apply_pandas_function_case",
    }
    route_aliases = (
        [alias for alias in binding_aliases if alias in aliases]
        if binding_aliases and not any(op in complex_operations for op in operations)
        else aliases
    )
    source_requirements = [
        {
            "source_alias": alias,
            "dataset_key": str(job.get("dataset_key") or "").strip(),
            "required": job.get("required") is not False,
        }
        for alias, job in zip(aliases, jobs)
        if alias
    ]
    compact_filters = {
        alias: deepcopy(job.get("filters"))
        for alias, job in zip(aliases, jobs)
        if alias and isinstance(job.get("filters"), (dict, list))
    }
    return {
        "version": 1,
        "status": "blocked" if validation_errors else "complete",
        "analysis_kind": str(plan.get("analysis_kind") or "").strip(),
        "question": str(question or "").strip(),
        "request_scope": str(plan.get("request_scope") or "new_analysis").strip(),
        "route_source_aliases": route_aliases,
        "source_requirements": source_requirements,
        "filters": compact_filters,
        "operations": operations,
        "metric_bindings": [
            {
                "source_alias": str(item.get("source_alias") or "").strip(),
                "source_column": str(item.get("source_column") or "").strip(),
                "aggregation": str(item.get("aggregation") or "").strip().lower(),
                "output_column": str(item.get("output_column") or "").strip(),
            }
            for item in bindings
        ],
        "grain_columns": _string_list(
            resolved_grain_plan.get("grain_columns")
            or resolved_grain_plan.get("entity_grain_columns")
        ),
        "result_columns": _string_list(output_contract.get("result_columns")),
        "temporal_semantics": deepcopy(
            business_time_guard.get("temporal_semantics") or []
        ),
        "validation_errors": deepcopy(validation_errors),
    }


# 함수 설명: `_selected_temporal_contracts()`는 04 의도 계획 정규화기 처리 중 temporal·contracts 관련 값을 계산·변환하는 내부 helper입니다.
def _selected_temporal_contracts(
    metadata_candidates: dict[str, Any],
    metadata_refs: list[dict[str, str]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    selected_domain_refs = {
        (
            str(item.get("section") or "").strip(),
            str(item.get("key") or "").strip(),
        )
        for item in metadata_refs
        if isinstance(item, dict)
        and str(item.get("section") or "").strip() != "table_catalog"
        and str(item.get("key") or "").strip()
    }
    if not selected_domain_refs:
        return result
    domain_items = metadata_candidates.get("domain_items")
    for item in domain_items if isinstance(domain_items, list) else []:
        if not isinstance(item, dict):
            continue
        identity = (
            str(item.get("section") or "").strip(),
            str(item.get("key") or "").strip(),
        )
        if identity not in selected_domain_refs:
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


def _normalize_reserved_previous_result_references(
    items: list[Any],
    reference_mode: str,
) -> list[Any]:
    """Convert model spellings of the session result into one graph provider.

    ``previous_result_rows`` is a Typed-IR reference mode, not a node output.
    A model can nevertheless use it as a typed ``node_output`` input in a
    join.  Normalize that reserved spelling to the executable external
    ``previous_result`` provider before graph validation.  No user-defined
    aliases or ordinary node outputs are changed.
    """

    if reference_mode not in {"previous_result_rows", "previous_result_transform"}:
        return [deepcopy(item) for item in items]
    result: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        normalized = deepcopy(item)
        for key in ("source_alias", "left_source_alias", "right_source_alias", "reference_source_alias"):
            if str(normalized.get(key) or "").strip() == "previous_result_rows":
                normalized[key] = PREVIOUS_RESULT_ALIAS
        inputs = normalized.get("inputs")
        if isinstance(inputs, list):
            normalized_inputs: list[Any] = []
            for raw_input in inputs:
                if not isinstance(raw_input, dict):
                    normalized_inputs.append(deepcopy(raw_input))
                    continue
                typed_input = deepcopy(raw_input)
                if str(typed_input.get("ref") or "").strip() == "previous_result_rows":
                    typed_input["kind"] = "external_source"
                    typed_input["ref"] = PREVIOUS_RESULT_ALIAS
                normalized_inputs.append(typed_input)
            normalized["inputs"] = normalized_inputs
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
        # Models use both the typed reference mode name (``previous_result_rows``)
        # and the runtime source alias (``previous_result``) in row-match
        # steps. They designate the same trusted session rows, so normalize the
        # former before graph construction. Unknown aliases are deliberately
        # left untouched and fail the ordinary provider validation.
        if reference_alias in {"previous_result_rows", PREVIOUS_RESULT_ALIAS}:
            reference_alias = PREVIOUS_RESULT_ALIAS
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


# 함수 설명: metadata/domain 정규화가 끝난 retrieval filter를 동일 source·field의 effective filter에도 반영합니다.
# retrieval_jobs는 실제 조회 계약이고 effective_filters는 후속 분석용 보존 계약이므로, 같은 field가 양쪽에
# 존재하면 정규화가 끝난 retrieval 조건을 우선해 원문 alias와 확장된 값이 pandas에서 중복 적용되지 않게 합니다.
def _synchronize_effective_filters_with_retrieval_jobs(
    condition_resolution: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(condition_resolution)
    effective_by_alias = (
        result.get("effective_filters")
        if isinstance(result.get("effective_filters"), dict)
        else {}
    )
    if not effective_by_alias:
        return result

    jobs_by_alias = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in retrieval_jobs
        if isinstance(item, dict)
        and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    }
    jobs_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for job in jobs_by_alias.values():
        dataset_key = str(job.get("dataset_key") or "").strip().casefold()
        if dataset_key:
            jobs_by_dataset.setdefault(dataset_key, []).append(job)

    synchronized_by_alias: dict[str, Any] = {}
    for alias, effective_item in list(effective_by_alias.items()):
        raw_alias = str(alias or "").strip()
        job = jobs_by_alias.get(raw_alias)
        # LLM plans frequently name ``effective_filters`` by dataset_key while
        # retrieval jobs use a deliberately distinct source_alias.  Resolve
        # that only when the dataset maps to one job; multiple same-dataset
        # jobs remain untouched because choosing one would lose source scope.
        if not isinstance(job, dict):
            effective_dataset_key = (
                str(effective_item.get("dataset_key") or raw_alias).strip().casefold()
                if isinstance(effective_item, dict)
                else raw_alias.casefold()
            )
            candidates = jobs_by_dataset.get(effective_dataset_key, [])
            if len(candidates) == 1:
                job = candidates[0]
        if not isinstance(effective_item, dict) or not isinstance(job, dict):
            synchronized_by_alias[raw_alias] = effective_item
            continue
        target_alias = str(job.get("source_alias") or job.get("dataset_key") or raw_alias).strip()
        effective_filters = effective_item.get("filters")
        job_filters = job.get("filters")
        dataset_key = str(job.get("dataset_key") or "").strip()
        synchronized_item = deepcopy(effective_item)
        if dataset_key:
            synchronized_item["dataset_key"] = dataset_key
        synchronized = _replace_matching_filter_fields(effective_filters, job_filters)
        if synchronized is not None:
            synchronized_item["filters"] = synchronized
        # The execution alias is authoritative.  This prevents an unchanged
        # dataset-key entry from being interpreted as a second independent
        # filter source after the retrieval job has already been normalized.
        existing = synchronized_by_alias.get(target_alias)
        if isinstance(existing, dict) and isinstance(synchronized_item, dict):
            existing_filters = existing.get("filters")
            next_filters = synchronized_item.get("filters")
            if isinstance(existing_filters, dict) and isinstance(next_filters, dict):
                merged = deepcopy(existing)
                merged["filters"] = {**existing_filters, **next_filters}
                synchronized_by_alias[target_alias] = merged
                continue
        synchronized_by_alias[target_alias or raw_alias] = synchronized_item
    result["effective_filters"] = synchronized_by_alias
    return result


# 함수 설명: 두 filter 표현(dict/list)에서 같은 canonical field만 authoritative 조건으로 교체합니다.
def _replace_matching_filter_fields(effective_filters: Any, job_filters: Any) -> Any:
    job_entries = _filter_field_entries(job_filters)
    if not job_entries or not isinstance(effective_filters, (dict, list)):
        return deepcopy(effective_filters)
    replacements = {
        _normalized_column_key(field): (field, deepcopy(condition))
        for field, condition in job_entries
        if _normalized_column_key(field)
    }
    if isinstance(effective_filters, dict):
        result = deepcopy(effective_filters)
        for field in list(result):
            replacement = replacements.get(_normalized_column_key(field))
            if replacement is None:
                continue
            replacement_field, replacement_condition = replacement
            result.pop(field, None)
            result[replacement_field] = replacement_condition
        return result

    result_list: list[Any] = []
    for item in effective_filters:
        if not isinstance(item, dict):
            result_list.append(deepcopy(item))
            continue
        field = str(item.get("field") or item.get("column") or "").strip()
        field_key = _normalized_column_key(field)
        replacement = replacements.get(field_key)
        if replacement is None:
            result_list.append(deepcopy(item))
            continue
        replacement_field, replacement_condition = replacement
        result_list.append(_filter_list_condition(replacement_field, replacement_condition))
    return result_list


# 함수 설명: dict/list filter 계약을 canonical field와 조건 쌍으로 읽습니다.
def _filter_field_entries(filters: Any) -> list[tuple[str, Any]]:
    if isinstance(filters, dict):
        return [(str(field), condition) for field, condition in filters.items()]
    if isinstance(filters, list):
        result: list[tuple[str, Any]] = []
        for item in filters:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or item.get("column") or "").strip()
            if field:
                result.append((field, item))
        return result
    return []


# 함수 설명: dict형 field 조건을 list형 filter condition으로 안전하게 변환합니다.
def _filter_list_condition(field: str, condition: Any) -> dict[str, Any]:
    if isinstance(condition, dict):
        result = deepcopy(condition)
        result["field"] = str(result.get("field") or result.get("column") or field)
        result.pop("column", None)
        return result
    return {"field": field, "operator": "eq", "value": deepcopy(condition)}


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


def _remove_previous_result_pseudo_jobs(
    plan: dict[str, Any],
    retrieval_jobs: list[Any],
) -> tuple[dict[str, Any], list[Any], dict[str, Any]]:
    """Keep reserved prior-result providers out of the retrieval-job contract.

    ``previous_result`` is restored from the same-session result store, not
    fetched from a Table Catalog.  Some model responses represent it as a
    retrieval job during a pure follow-up transform.  Removing only the
    exact reserved alias/key pair lets the ordinary reference-mode rules turn
    that shape into a deterministic previous-result transform while leaving
    all real Catalog jobs untouched.
    """

    reserved = {PREVIOUS_RESULT_ALIAS, "previous_result_rows", "upstream_result"}
    filtered: list[Any] = []
    removed: list[dict[str, str]] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            filtered.append(deepcopy(item))
            continue
        dataset_key = str(item.get("dataset_key") or "").strip()
        source_alias = str(item.get("source_alias") or dataset_key).strip()
        if dataset_key in reserved and source_alias in reserved:
            removed.append(
                {
                    "dataset_key": dataset_key,
                    "source_alias": source_alias,
                }
            )
            continue
        filtered.append(deepcopy(item))
    next_plan = deepcopy(plan)
    if removed:
        next_plan["retrieval_jobs"] = deepcopy(filtered)
    return next_plan, filtered, {
        "status": "applied" if removed else "not_needed",
        "removed_jobs": removed,
    }


# 함수 설명: 공정 그룹 metadata의 canonical field와 processes 값을 기준으로 LLM filter field 오선택을 교정합니다.
# 함수 설명: 모든 retrieval filter operator를 동일한 canonical vocabulary로 정규화합니다.
def _normalize_retrieval_filter_operators(
    retrieval_jobs: list[Any],
    question: str = "",
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
            job["filters"] = _promote_explicit_prefix_filter_conditions(
                job["filters"],
                question,
                f"retrieval_jobs[{index}].filters",
                changes,
            )
        normalized_jobs.append(job)
    return normalized_jobs, {
        "status": "applied" if changes else "not_needed",
        "change_count": len(changes),
        "changes": changes,
    }


# Function description: preserve an explicit user prefix expression when a
# model flattened it to an equality predicate.  The value itself must appear
# immediately beside a prefix cue in the question, so a prefix request for one
# condition never broadens an unrelated date, process, or status predicate.
def _promote_explicit_prefix_filter_conditions(
    value: Any,
    question: str,
    path: str,
    changes: list[dict[str, Any]],
) -> Any:
    if not question or not isinstance(value, (dict, list)):
        return value
    if isinstance(value, list):
        return [
            _promote_explicit_prefix_filter_conditions(
                item,
                question,
                f"{path}[{index}]",
                changes,
            )
            for index, item in enumerate(value)
        ]
    result = deepcopy(value)
    operator = str(result.get("operator") or "").strip().lower()
    literal = result.get("value")
    if (
        operator in {"eq", "contains"}
        and isinstance(literal, str)
        and _is_compact_identifier_literal(literal)
        and _value_has_explicit_prefix_cue(question, literal)
    ):
        result["operator"] = "starts_with"
        changes.append(
            {
                "path": path,
                "from": operator,
                "to": "starts_with",
                "reason": "explicit_prefix_cue_for_same_literal",
            }
        )
        return result
    # Some models fold a product qualifier and an explicitly prefixed
    # identifier into one source-filter value (for example ``F315 L-116``).
    # Do not guess the field or discard the qualifier: when the question
    # unambiguously marks one embedded identifier as a prefix, repair only
    # that selected source condition.  The remaining qualifier stays in the
    # function-case input and is evaluated by its trusted helper.
    if operator in {"eq", "contains"} and isinstance(literal, str):
        literal_key = _filter_literal_key(literal)
        for prefix_literal in _explicit_prefix_literals(question):
            prefix_key = _filter_literal_key(prefix_literal)
            if (
                prefix_key
                and prefix_key != literal_key
                and prefix_key in literal_key
            ):
                result["operator"] = "starts_with"
                result["value"] = prefix_literal
                changes.append(
                    {
                        "path": path,
                        "from": operator,
                        "to": "starts_with",
                        "reason": "explicit_prefix_literal_extracted_from_compound_filter",
                    }
                )
                return result
    for key, item in list(result.items()):
        if key in {"operator", "op", "value", "values"}:
            continue
        if isinstance(item, (dict, list)):
            result[key] = _promote_explicit_prefix_filter_conditions(
                item,
                question,
                f"{path}.{key}",
                changes,
            )
    return result


# Function description: test the local natural-language context around a
# literal value rather than relying on a particular business field name.
def _value_has_explicit_prefix_cue(question: str, value: str) -> bool:
    literal = str(value or "").strip()
    if not literal:
        return False
    text = str(question or "")
    escaped = re.escape(literal)
    suffix = r"(?:\s*(?:로|으로|부터)?\s*(?:시작(?:하는|하|값)?|접두(?:어)?|prefix|starts?\s*with))"
    prefix = r"(?:(?:prefix|starts?\s*with|시작(?:값)?|접두(?:어)?)\s*(?:값)?\s*)"
    return bool(
        re.search(escaped + suffix, text, flags=re.IGNORECASE)
        or re.search(prefix + escaped, text, flags=re.IGNORECASE)
    )


def _is_compact_identifier_literal(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*",
            str(value or "").strip(),
        )
    )


# Function description: extract only identifier-shaped literals whose local
# wording explicitly asks for a prefix.  This deliberately does not infer a
# catalog field; it is used solely to correct a malformed value on a field the
# intent model has already selected.
def _explicit_prefix_literals(question: str) -> list[str]:
    text = str(question or "")
    if not text:
        return []
    suffix = r"(?:\s*(?:로|으로|부터)?\s*(?:시작(?:하는|하|값)?|접두(?:어)?|prefix|starts?\s*with))"
    prefix = r"(?:(?:prefix|starts?\s*with|시작(?:값)?|접두(?:어)?)\s*(?:값)?\s*)"
    identifier = r"([A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*)"
    candidates = [
        match.group(1).strip()
        for match in re.finditer(identifier + suffix, text, flags=re.IGNORECASE)
    ]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(prefix + identifier, text, flags=re.IGNORECASE)
    )
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _filter_literal_key(candidate)
        # Prefix repair is intentionally restricted to compact identifiers;
        # ordinary words such as a process name must not be reinterpreted.
        if len(key) < 3 or not any(char.isdigit() for char in key) or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _filter_literal_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").strip().upper())


# 함수 설명: 중첩 filter 조건을 canonical operator/value 형식으로 정규화합니다.
def _normalize_filter_operator_value(
    value: Any,
    path: str,
    changes: list[dict[str, Any]],
) -> Any:
    """Normalize explicit and compact field filters without inventing values.

    Intent models occasionally emit compact forms such as ``{"OPER_NAME":
    ["D/A1", "D/A2"]}`` or ``{"DATE": "20260701"}``.  The executor
    contract is operator-based, so those unambiguous forms are transformed to
    ``in`` and ``eq`` respectively.  Existing explicit operators and logical
    groups remain untouched apart from canonical operator spelling.
    """
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
        normalized_key = str(key or "").strip().casefold()
        if normalized_key in FILTER_LOGICAL_KEYS:
            if isinstance(item, list):
                normalized[key] = [
                    _normalize_filter_operator_value(
                        nested,
                        f"{path}.{key}[{index}]",
                        changes,
                    )
                    for index, nested in enumerate(item)
                ]
            elif isinstance(item, dict):
                normalized[key] = _normalize_filter_operator_value(
                    item,
                    f"{path}.{key}",
                    changes,
                )
            continue
        if isinstance(item, dict):
            normalized[key] = _normalize_filter_operator_value(
                item,
                f"{path}.{key}",
                changes,
            )
        elif isinstance(item, list):
            normalized[key] = {"operator": "in", "value": deepcopy(item)}
            changes.append(
                {
                    "path": f"{path}.{key}",
                    "from": "list",
                    "to": "in",
                }
            )
        elif item is not None:
            normalized[key] = {"operator": "eq", "value": deepcopy(item)}
            changes.append(
                {
                    "path": f"{path}.{key}",
                    "from": "scalar",
                    "to": "eq",
                }
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
    declared_processes: list[str] | None = None,
    align_explicit_scope: bool = True,
) -> tuple[list[Any], dict[str, Any]]:
    contracts = _process_group_contracts(metadata_candidates)
    if not contracts:
        return retrieval_jobs, {"status": "not_available", "corrections": []}
    requested_processes = (
        _merge_strings(
            _requested_process_scope(question, contracts),
            declared_processes or [],
        )
        if align_explicit_scope
        else []
    )
    mentioned_group_indexes = (
        _mentioned_process_group_indexes(question, contracts)
        if align_explicit_scope
        else set()
    )
    alignment_scope = _process_filter_alignment_scope(retrieval_jobs)
    preserve_distinct_job_scopes = alignment_scope["has_disjoint_scopes"]
    job_count = sum(1 for item in retrieval_jobs if isinstance(item, dict))
    process_fields = {
        str(contract.get("field") or "OPER_NAME").strip().casefold()
        for contract in contracts
    }
    scope_fields = {
        str(contracts[index].get("field") or "OPER_NAME").strip()
        for index in mentioned_group_indexes
        if 0 <= index < len(contracts)
    }
    single_source_scope_field = (
        next(iter(scope_fields)) if job_count == 1 and len(scope_fields) == 1 else ""
    )
    single_source_scope_condition = (
        {
            "operator": "eq" if len(requested_processes) == 1 else "in",
            "value": (
                requested_processes[0]
                if len(requested_processes) == 1
                else list(requested_processes)
            ),
        }
        if single_source_scope_field and requested_processes
        else None
    )

    normalized_jobs: list[Any] = []
    corrections: list[dict[str, Any]] = []
    for item in retrieval_jobs:
        if not isinstance(item, dict):
            normalized_jobs.append(deepcopy(item))
            continue
        job = deepcopy(item)
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        filters = job.get("filters")
        has_process_filter = any(
            str(field).strip().casefold() in process_fields
            for field, _condition in _filter_field_entries(filters)
        )
        if single_source_scope_condition and not has_process_filter:
            if isinstance(filters, dict):
                normalized_filters = deepcopy(filters)
                normalized_filters[single_source_scope_field] = deepcopy(
                    single_source_scope_condition
                )
                job["filters"] = normalized_filters
            elif isinstance(filters, list):
                normalized_filters = deepcopy(filters)
                normalized_filters.append(
                    _filter_list_condition(
                        single_source_scope_field,
                        single_source_scope_condition,
                    )
                )
                job["filters"] = normalized_filters
            else:
                job["filters"] = {
                    single_source_scope_field: deepcopy(single_source_scope_condition)
                }
            corrections.append(
                {
                    "source_alias": alias,
                    "field": single_source_scope_field,
                    "correction_type": "single_source_process_scope_completion",
                    "to_values": list(requested_processes),
                }
            )
            filters = job["filters"]
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
    resolved_execution_graph: dict[str, Any] | None = None,
    metadata_candidates: dict[str, Any] | None = None,
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
    external_requirements = {
        str(item.get("source_alias") or "").strip(): item
        for item in (resolved_execution_graph or {}).get("external_source_requirements", [])
        if isinstance(item, dict) and str(item.get("source_alias") or "").strip()
    }
    binding_issues: list[dict[str, Any]] = []
    metadata_candidates = metadata_candidates if isinstance(metadata_candidates, dict) else {}
    for binding in output_contract.get("metric_bindings", []):
        if not isinstance(binding, dict):
            continue
        alias = str(binding.get("source_alias") or "").strip()
        if alias in {PREVIOUS_RESULT_ALIAS, "upstream_result"}:
            continue
        job = jobs_by_alias.get(alias)
        if not isinstance(job, dict):
            provider = external_requirements.get(alias)
            if isinstance(provider, dict) and provider.get("provider") in {
                "previous_source",
                "previous_result",
            }:
                expected_dataset = str(binding.get("dataset_key") or "").strip()
                provider_dataset = str(provider.get("dataset_key") or "").strip()
                if expected_dataset and provider_dataset and expected_dataset != provider_dataset:
                    binding_issues.append(
                        {
                            "output_column": binding.get("output_column"),
                            "source_alias": alias,
                            "issue": "dataset_key_mismatch",
                            "expected_dataset_key": expected_dataset,
                            "actual_dataset_key": provider_dataset,
                        }
                    )
                source_column = str(binding.get("source_column") or "").strip()
                if (
                    provider_dataset
                    and source_column
                    and not _catalog_supports_domain_column(
                        metadata_candidates,
                        provider_dataset,
                        source_column,
                    )
                ):
                    binding_issues.append(
                        {
                            "output_column": binding.get("output_column"),
                            "source_alias": alias,
                            "issue": "source_column_not_in_table_catalog",
                            "source_column": source_column,
                            "actual_dataset_key": provider_dataset,
                        }
                    )
                continue
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
        source_column = str(binding.get("source_column") or "").strip()
        if (
            actual_dataset
            and source_column
            and not _catalog_supports_domain_column(
                metadata_candidates,
                actual_dataset,
                source_column,
            )
        ):
            binding_issues.append(
                {
                    "output_column": binding.get("output_column"),
                    "source_alias": alias,
                    "issue": "source_column_not_in_table_catalog",
                    "source_column": source_column,
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

    # A metric binding may be mechanically schema-valid while still relabeling
    # a quantity from one dataset as a metric owned by another catalog.  Do not
    # substitute a different dataset here; fail closed so the intent model can
    # choose again using the catalog candidate guidance.
    ownership_issues = _catalog_metric_ownership_issues(
        output_contract,
        jobs_by_alias,
        metadata_candidates,
    )
    if ownership_issues:
        errors.append(
            {
                "type": "catalog_metric_ownership_mismatch",
                "message": "선택한 Table Catalog가 보유하지 않은 metric을 다른 source 컬럼으로 바꾸려 했습니다.",
                "issues": ownership_issues,
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


# 함수 설명: `_catalog_metric_ownership_issues()`는 서로 다른 Catalog metric 이름으로 단순 재라벨링하는 계획을 차단합니다.
def _catalog_metric_ownership_issues(
    output_contract: dict[str, Any],
    jobs_by_alias: dict[str, dict[str, Any]],
    metadata_candidates: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return only unambiguous catalog-owned metric relabeling conflicts.

    This intentionally does not infer a replacement dataset or reject normal
    presentation aliases such as ``TOTAL_PRODUCTION``.  It applies only when a
    result metric exactly matches a metric explicitly declared by some catalog
    candidate and the selected source neither declares nor exposes that metric.
    """

    candidate_items = (
        metadata_candidates.get("table_catalog_items")
        if isinstance(metadata_candidates, dict)
        else []
    )
    if not isinstance(candidate_items, list):
        return []
    owners_by_metric: dict[str, set[str]] = {}
    for item in candidate_items:
        if not isinstance(item, dict):
            continue
        dataset_key = str(item.get("dataset_key") or "").strip()
        payload = _metadata_payload(item)
        semantics = payload.get("metric_semantics")
        if not dataset_key or not isinstance(semantics, dict):
            continue
        for metric in semantics:
            metric_key = _normalized_column_key(metric)
            if metric_key:
                owners_by_metric.setdefault(metric_key, set()).add(dataset_key)
    if not owners_by_metric:
        return []

    issues: list[dict[str, Any]] = []
    for binding in output_contract.get("metric_bindings", []):
        if not isinstance(binding, dict):
            continue
        alias = str(binding.get("source_alias") or "").strip()
        job = jobs_by_alias.get(alias)
        if not isinstance(job, dict):
            continue
        dataset_key = str(job.get("dataset_key") or "").strip()
        output_column = str(binding.get("output_column") or "").strip()
        source_column = str(binding.get("source_column") or "").strip()
        output_key = _normalized_column_key(output_column)
        source_key = _normalized_column_key(source_column)
        owners = owners_by_metric.get(output_key, set())
        if (
            not dataset_key
            or not output_key
            or not source_key
            or output_key == source_key
            or not owners
            or dataset_key in owners
            or _catalog_supports_domain_column(
                metadata_candidates,
                dataset_key,
                output_column,
            )
        ):
            continue
        issues.append(
            {
                "source_alias": alias,
                "dataset_key": dataset_key,
                "source_column": source_column,
                "output_column": output_column,
                "metric_owner_dataset_keys": sorted(owners),
                "issue": "selected_dataset_does_not_own_output_metric",
            }
        )
    return issues


# 함수 설명: V2 Fast Path에 필요한 범용 계산 계약과 파생 결과 컬럼을 기존 정규화 결과에 안전하게 보존합니다.
def _preserve_v2_fast_output_contract(
    normalized_value: Any,
    raw_value: Any,
) -> dict[str, Any]:
    normalized = deepcopy(normalized_value) if isinstance(normalized_value, dict) else {}
    raw = raw_value if isinstance(raw_value, dict) else {}
    recipe = str(raw.get("fast_path_recipe") or "").strip().lower()
    if recipe not in V2_FAST_RECIPES:
        recipe = ""
    calculation = deepcopy(raw.get("calculation")) if isinstance(raw.get("calculation"), dict) else {}
    if recipe:
        normalized["fast_path_recipe"] = recipe
    if calculation:
        normalized["calculation"] = calculation

    for key in ("limit", "tie_policy", "result_schema_mode"):
        if raw.get(key) not in (None, "", [], {}):
            normalized[key] = deepcopy(raw[key])

    declared_columns = _string_list(raw.get("result_columns"))
    derived_columns = _merge_strings(
        _string_list(calculation.get("output_column")),
        _string_list(calculation.get("output_columns")),
        _string_list(calculation.get("time_bucket_column")),
    )
    if declared_columns or derived_columns:
        normalized["result_columns"] = _merge_strings(
            _string_list(normalized.get("result_columns")),
            declared_columns,
            derived_columns,
        )
        normalized["required_columns"] = _merge_strings(
            _string_list(normalized.get("required_columns")),
            declared_columns,
            derived_columns,
        )
        normalized["strict_result_columns"] = True

    labels = raw.get("column_labels") if isinstance(raw.get("column_labels"), dict) else {}
    if labels:
        allowed = set(_string_list(normalized.get("result_columns")))
        normalized["column_labels"] = {
            **(
                normalized.get("column_labels")
                if isinstance(normalized.get("column_labels"), dict)
                else {}
            ),
            **{
                str(key): str(value)
                for key, value in labels.items()
                if str(key) in allowed and str(value or "").strip()
            },
        }
    return {
        key: value
        for key, value in normalized.items()
        if value not in (None, "", [], {})
    }


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
    # Keep the model's explicit output shape as the single owner of required
    # columns.  Catalog defaults fill only an unshaped detail/entity request;
    # they must not become mandatory columns after retrieval has already
    # resolved the real source schema.
    explicit_result_columns = _string_list(
        raw.get("result_columns") or raw.get("columns")
    )
    explicit_required_columns = _string_list(raw.get("required_columns"))
    explicit_output_shape = bool(
        explicit_result_columns
        or explicit_required_columns
        or raw.get("strict_result_columns") is True
    )
    if explicit_result_columns:
        contract["result_columns"] = explicit_result_columns
        contract["required_columns"] = explicit_result_columns
        contract["strict_result_columns"] = True
    elif explicit_required_columns:
        contract["required_columns"] = explicit_required_columns
    aggregation_outputs = _aggregation_output_contract(plan)
    metric_bindings = _metric_bindings(
        plan,
        retrieval_jobs,
        resolved_reference_join_plan or {},
        resolved_metric_merge_plan or {},
        metadata_candidates or {},
    )
    # A retrieval-free follow-up operates on the already restored
    # ``previous_result`` frame.  There is no new aggregation step from which
    # to rediscover its metrics, so retain only the explicit bindings that
    # unambiguously point at that runtime frame.  This keeps a generic
    # sort/top-N follow-up from losing the metric contract solely because it
    # has no retrieval job.  Bindings for any other alias remain untrusted and
    # are deliberately not copied through.
    if not metric_bindings:
        metric_bindings = _previous_result_transform_metric_bindings(
            raw,
            plan,
            retrieval_jobs,
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
    question = str(
        (payload.get("request") if isinstance(payload.get("request"), dict) else {}).get("question")
        or ""
    ).strip()
    ordering = _ordering_contract(raw, plan, question)
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
        requested_metrics = _merge_strings(
            contract["metric_columns"],
            _string_list(contract.get("primary_metric")),
        )
        default_detail_columns = _catalog_default_detail_columns(
            payload,
            retrieval_jobs,
            metadata_candidates,
            requested_metrics,
        )
        if explicit_output_shape:
            # Preserve the requested shape.  In particular, do not merge the
            # table catalog's default detail list into a strict projection.
            contract["required_columns"] = _merge_strings(
                contract["required_columns"],
                contract["metric_columns"],
            )
        elif requested_metrics and default_detail_columns:
            # When a detail question requests a concrete metric, the selected
            # metric provider and resolved grain are the trusted output shape.
            # Keep extra model-proposed columns only when the question names
            # their canonical key or display label explicitly.
            explicit_columns = _explicitly_requested_contract_columns(
                question,
                contract["required_columns"],
                column_labels,
            )
            contract["required_columns"] = _merge_strings(
                _string_list(resolved_grain_plan.get("grain_columns"))
                if isinstance(resolved_grain_plan, dict)
                else [],
                default_detail_columns if len(retrieval_jobs) > 1 else [],
                requested_metrics,
                explicit_columns,
            )
        else:
            contract["required_columns"] = _merge_strings(
                contract["required_columns"],
                default_detail_columns,
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
        # A raw right-side column is an execution input, not a result column,
        # when the Typed join already consumes it to produce an aggregate such
        # as a count or a unique-list.  Requiring both turns a correct grouped
        # enrichment into a false output-contract failure.  Explicit user
        # projections remain in ``contract[required_columns]``; this filters
        # only catalog/plan-derived join value defaults.
        aggregate_input_keys = {
            _normalized_column_key(binding.get("source_column"))
            for binding in metric_bindings
            if isinstance(binding, dict)
            and str(binding.get("source_column") or "").strip()
        }
        join_value_columns = [
            column
            for column in join_value_columns
            if _normalized_column_key(column) not in aggregate_input_keys
        ]
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
    metadata_candidates: dict[str, Any],
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
    known_external_aliases = set(job_datasets)
    for step in steps:
        if not isinstance(step, dict):
            continue
        source_alias = str(step.get("source_alias") or "").strip()
        if source_alias not in known_external_aliases:
            lineage_aliases = _step_external_source_aliases(
                step,
                steps,
                known_external_aliases,
            )
            if len(lineage_aliases) == 1:
                source_alias = lineage_aliases[0]
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
            source_column = str(
                raw.get("source_column")
                or raw.get("column")
                or raw.get("agg_column")
                or raw.get("aggregate_column")
                or ""
            ).strip()
            binding_source_alias = str(raw.get("source_alias") or source_alias).strip()
            if binding_source_alias not in known_external_aliases:
                lineage_aliases = _step_external_source_aliases(
                    step,
                    steps,
                    known_external_aliases,
                )
                compatible_aliases = [
                    alias
                    for alias in lineage_aliases
                    if job_datasets.get(alias)
                    and _catalog_supports_domain_column(
                        metadata_candidates,
                        job_datasets[alias],
                        source_column,
                    )
                ]
                if len(compatible_aliases) == 1:
                    binding_source_alias = compatible_aliases[0]
            binding = _normalized_metric_binding(
                {
                    **raw,
                    "source_alias": binding_source_alias,
                },
                job_datasets,
            )
            if binding:
                result.append(binding)
    return result


# 함수 설명: 이전 결과만 정렬·순위화하는 후속 분석은 원본 metric binding을 안전하게 유지합니다.
def _previous_result_transform_metric_bindings(
    raw_contract: dict[str, Any],
    plan: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep explicit metrics only for a strict retrieval-free prior-result transform.

    The model may describe a top-N follow-up without another aggregation
    operation.  In that case the source column already exists in the restored
    prior-result frame, while normal metric discovery intentionally finds no
    retrieval-backed binding.  This narrow fallback never accepts an unknown
    source alias or a plan that still performs a data retrieval.
    """

    if retrieval_jobs or not _is_retrieval_free_previous_result_transform_candidate(plan):
        return []
    raw_bindings = raw_contract.get("metric_bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_bindings:
        if not isinstance(item, dict):
            return []
        source_alias = str(item.get("source_alias") or "").strip()
        source_column = str(item.get("source_column") or item.get("column") or "").strip()
        output_column = str(item.get("output_column") or item.get("result_column") or "").strip()
        aggregation = str(item.get("aggregation") or item.get("method") or "").strip().lower()
        if (
            source_alias != PREVIOUS_RESULT_ALIAS
            or not source_column
            or not output_column
            or not aggregation
        ):
            return []
        normalized.append(
            {
                "source_alias": PREVIOUS_RESULT_ALIAS,
                "dataset_key": PREVIOUS_RESULT_ALIAS,
                "source_column": source_column,
                "aggregation": aggregation,
                "output_column": output_column,
                "semantic_scope": deepcopy(
                    item.get("semantic_scope")
                    or item.get("filter_scope")
                    or item.get("filters")
                    or item.get("temporal_scope")
                    or {}
                ),
            }
        )
    return normalized


# 함수 설명: 정규화 전 spelling 차이에도 이전 결과만 사용하는 후속 변환을 식별합니다.
def _is_retrieval_free_previous_result_transform_candidate(plan: dict[str, Any]) -> bool:
    """Recognize only an explicit, retrieval-free prior-result transform.

    ``_output_contract`` runs before the reference-mode repair that converts
    legacy ``previous_result`` / ``previous_result_rows`` spellings to the
    canonical transform mode.  Check the Typed operation shape as well, so a
    bare model claim cannot preserve bindings for an unrelated source.
    """

    if str(plan.get("request_scope") or "").strip() != "followup_transform":
        return False
    if str(plan.get("reference_mode") or "").strip() not in {
        "previous_result_transform",
        "previous_result_rows",
        "previous_result",
    }:
        return False
    steps = plan.get("pandas_execution_plan")
    if not isinstance(steps, list) or not steps:
        return False
    reserved_aliases = {PREVIOUS_RESULT_ALIAS, "previous_result_rows"}
    external_refs: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            return False
        inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        for input_item in inputs:
            if not isinstance(input_item, dict):
                return False
            if str(input_item.get("kind") or "").strip() == "external_source":
                reference = str(input_item.get("ref") or "").strip()
                if reference:
                    external_refs.append(reference)
        source_alias = str(step.get("source_alias") or "").strip()
        if source_alias and source_alias not in reserved_aliases:
            # A node-output alias is allowed only when it is produced by a
            # preceding step; source aliases on raw sort/filter steps must not
            # introduce an external data source.
            produced = {
                str(item.get("node_id") or item.get("output_alias") or "").strip()
                for item in steps
                if isinstance(item, dict)
            }
            if source_alias not in produced:
                return False
    return bool(external_refs) and all(reference in reserved_aliases for reference in external_refs)


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
        "semantic_scope": deepcopy(
            value.get("semantic_scope")
            or value.get("filter_scope")
            or value.get("filters")
            or value.get("temporal_scope")
            or {}
        ),
    }


# 함수 설명: 동일한 원천 metric에 여러 표시명이 지정되면 최초 출력명만 보존하고 나머지는 제거 대상으로 기록합니다.
def _deduplicate_metric_bindings(
    bindings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    suppressed: list[str] = []
    seen: dict[tuple[str, str, str, str, str], str] = {}
    for item in bindings:
        marker = (
            str(item.get("source_alias") or "").casefold(),
            str(item.get("dataset_key") or "").casefold(),
            _normalized_column_key(item.get("source_column")),
            str(item.get("aggregation") or "").casefold(),
            json.dumps(
                item.get("semantic_scope") or {},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ),
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


# 함수 설명: 단일 정렬 요청을 output contract로 정규화하며, 질문이나 pandas 단계에 정렬 근거가 없는 LLM 임의 계약은 제거합니다.
def _ordering_contract(
    raw: dict[str, Any],
    plan: dict[str, Any],
    question: str = "",
) -> dict[str, Any]:
    value = raw.get("ordering") if isinstance(raw.get("ordering"), dict) else {}
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    ordering_step: dict[str, Any] = {}
    for step in reversed(steps):
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or "").strip().lower()
        if operation not in {"sort", "sort_and_top_n", "top_n", "bottom_n"} and not step.get("sort_by"):
            continue
        ordering_step = step
        break
    if not value:
        value = ordering_step
    elif not ordering_step and not _question_requests_ordering(question):
        return {}
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


# 함수 설명: 사용자가 정렬·순위·극값을 실제로 요청했는지 한국어와 일반 영문 표현으로 판정합니다.
def _question_requests_ordering(question: Any) -> bool:
    text = str(question or "").strip().casefold()
    if not text:
        return False
    markers = (
        "상위",
        "하위",
        "가장",
        "최대",
        "최소",
        "순위",
        "랭킹",
        "정렬",
        "내림차순",
        "오름차순",
        "top ",
        "bottom ",
        "highest",
        "lowest",
    )
    if any(marker in text for marker in markers):
        return True
    return bool(re.search(r"(?<![가-힣])(많은|적은|높은|낮은)", text))


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


# 함수 설명: 질문에 canonical 컬럼명 또는 표시 라벨이 직접 등장한 결과 컬럼만 반환합니다.
def _explicitly_requested_contract_columns(
    question: str,
    columns: list[str],
    labels: dict[str, str],
) -> list[str]:
    result: list[str] = []
    for column in _string_list(columns):
        label = str(labels.get(column) or "").strip()
        if _domain_alias_matches(question, column) or (
            label and _domain_alias_matches(question, label)
        ):
            result.append(column)
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
    requested_metrics: list[str] | None = None,
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
    requested_metric_keys = {
        _normalized_column_key(value)
        for value in _string_list(requested_metrics)
    }
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
        semantics = (
            metadata.get("metric_semantics")
            if isinstance(metadata.get("metric_semantics"), dict)
            else {}
        )
        semantic_keys = {
            _normalized_column_key(value) for value in semantics
        }
        if (
            requested_metric_keys
            and semantic_keys
            and not requested_metric_keys.intersection(semantic_keys)
        ):
            continue
        result = _merge_strings(result, _string_list(metadata.get("default_detail_columns")))
    return result


# 함수 설명: 01D 출력 또는 기존 payload에서 실제 후보 묶음을 꺼내 정규화 단계에서만 사용합니다.
def _metadata_candidates(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    candidate_payload = _payload(value)
    nested = candidate_payload.get("metadata_candidates")
    if isinstance(nested, dict):
        result = deepcopy(nested)
        if isinstance(candidate_payload.get("metadata_load"), dict):
            result["metadata_load"] = deepcopy(candidate_payload["metadata_load"])
        return result
    if any(
        isinstance(candidate_payload.get(key), list)
        for key in ("domain_items", "table_catalog_items", "main_flow_filters")
    ):
        return candidate_payload
    existing = payload.get("metadata_candidates")
    return deepcopy(existing) if isinstance(existing, dict) else {}


# 함수 설명: LLM metadata_refs 중 현재 후보에 실제 존재하는 참조만 실행 가능한 신뢰 목록으로 보존합니다.
def _metadata_candidate_envelope(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep 01D loader status beside the compact candidate lists."""

    candidate_payload = _payload(value)
    if candidate_payload:
        return candidate_payload
    existing = payload.get("metadata_candidate_envelope")
    return deepcopy(existing) if isinstance(existing, dict) else {}


# 함수 설명: `_execution_catalog_registry_items()`는 LLM에 보인 상위 후보와 별도로,
# 현재 활성 Table Catalog 전체를 실행 권한 확인과 선택 문서 보강에만 제공합니다.
def _execution_catalog_registry_items(
    envelope: dict[str, Any],
    candidates: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the authoritative Catalog registry without expanding the model prompt."""

    registry = envelope.get("table_catalog_registry") if isinstance(envelope, dict) else {}
    raw_items = registry.get("items") if isinstance(registry, dict) else registry
    if not isinstance(raw_items, list):
        raw_items = []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        dataset_key = _catalog_dataset_key(item)
        normalized = dataset_key.casefold()
        if not dataset_key or normalized in seen:
            continue
        seen.add(normalized)
        result.append(deepcopy(item))
    if result:
        return result

    # 기존 export/direct unit input은 registry가 없을 수 있다. 그 경우에는
    # 기존 후보 목록을 authoritative snapshot으로 사용하는 하위 호환을 유지한다.
    raw_candidates = (
        candidates.get("table_catalog_items")
        if isinstance(candidates.get("table_catalog_items"), list)
        else []
    )
    return [deepcopy(item) for item in raw_candidates if isinstance(item, dict)]


# 함수 설명: 실행 계획이 후보 밖의 정상 등록 dataset을 선택하면 그 한 건의 Catalog 문서만
# 후보에 보강합니다. 전체 registry를 LLM prompt에 다시 넣거나 dataset을 강제 선택하지 않습니다.
def _hydrate_execution_catalog_candidates(
    metadata_candidates: dict[str, Any],
    metadata_envelope: dict[str, Any],
    retrieval_jobs: list[Any],
) -> tuple[dict[str, Any], list[Any], dict[str, Any]]:
    """Materialize only trusted planned datasets omitted by bounded candidate selection."""

    candidates = deepcopy(metadata_candidates) if isinstance(metadata_candidates, dict) else {}
    selected = (
        [deepcopy(item) for item in candidates.get("table_catalog_items", []) if isinstance(item, dict)]
        if isinstance(candidates.get("table_catalog_items"), list)
        else []
    )
    registry_items = _execution_catalog_registry_items(metadata_envelope, candidates)
    registry_index = {
        _catalog_dataset_key(item).casefold(): item
        for item in registry_items
        if _catalog_dataset_key(item)
    }
    selected_keys = {
        _catalog_dataset_key(item).casefold()
        for item in selected
        if _catalog_dataset_key(item)
    }
    jobs: list[Any] = []
    hydrated_keys: list[str] = []
    unknown_keys: list[str] = []
    for raw_job in retrieval_jobs:
        if not isinstance(raw_job, dict):
            jobs.append(deepcopy(raw_job))
            continue
        job = deepcopy(raw_job)
        requested_key = str(job.get("dataset_key") or "").strip()
        canonical_item = registry_index.get(requested_key.casefold()) if requested_key else None
        if not canonical_item:
            if requested_key:
                unknown_keys.append(requested_key)
            jobs.append(job)
            continue
        canonical_key = _catalog_dataset_key(canonical_item)
        # dataset key의 대소문자 표기만 다른 경우 Catalog의 canonical key를 사용한다.
        job["dataset_key"] = canonical_key
        if canonical_key.casefold() not in selected_keys:
            selected.append(deepcopy(canonical_item))
            selected_keys.add(canonical_key.casefold())
            hydrated_keys.append(canonical_key)
        jobs.append(job)
    candidates["table_catalog_items"] = selected
    return candidates, jobs, {
        "status": "hydrated" if hydrated_keys else ("unknown" if unknown_keys else "not_needed"),
        "registry_dataset_count": len(registry_index),
        "candidate_dataset_count": len(selected),
        "hydrated_dataset_keys": hydrated_keys,
        "unknown_dataset_keys": list(dict.fromkeys(unknown_keys)),
        "metadata_refs": [
            {"section": "table_catalog", "key": key}
            for key in hydrated_keys
        ],
    }


# 함수 설명: Catalog wrapper/payload의 dataset_key를 실행 비교에 사용할 canonical 문자열로 읽습니다.
def _catalog_dataset_key(item: dict[str, Any]) -> str:
    payload = _metadata_payload(item)
    return str(
        item.get("dataset_key")
        or item.get("key")
        or payload.get("dataset_key")
        or payload.get("key")
        or ""
    ).strip()


# 함수 설명: 실제 메타데이터 로더가 Table Catalog를 읽지 못한 상태를 정규화 단계에서도 fail-close로 유지합니다.
def _catalog_metadata_error(
    envelope: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed when the real graph reports that Table Catalog is unavailable."""

    load = envelope.get("metadata_load") if isinstance(envelope.get("metadata_load"), dict) else {}
    if not load and isinstance(candidates.get("metadata_load"), dict):
        load = candidates.get("metadata_load")
    # Older direct unit callers can supply bare lists. The actual V2 graph
    # always supplies loader evidence, so it is the production fail-close path.
    if not isinstance(load, dict) or not load:
        return {}
    loads = load.get("loads") if isinstance(load.get("loads"), dict) else {}
    table_load = loads.get("table_catalog_items") if isinstance(loads.get("table_catalog_items"), dict) else {}
    table_status = str(table_load.get("status") or "").strip().lower()
    overall_status = str(load.get("status") or "").strip().lower()
    table_items = _execution_catalog_registry_items(envelope, candidates)
    registered = [
        str(item.get("dataset_key") or _metadata_payload(item).get("dataset_key") or "").strip()
        for item in table_items
        if isinstance(item, dict)
    ]
    registered = [item for item in dict.fromkeys(registered) if item]
    failed_loads = _metadata_load_failures(loads)
    if failed_loads or overall_status in {"error", "failed", "failure", "invalid"}:
        return _metadata_load_error(
            failed_loads or [{"metadata_kind": "metadata", "status": overall_status or "error"}],
            registered_dataset_count=len(registered),
        )
    if not registered:
        return {
            "type": "table_catalog_metadata_unavailable",
            "reason": "no_active_table_catalog",
            "message": "MongoDB 메타데이터 연결은 되었지만 활성 Table Catalog에 등록된 데이터셋이 없습니다. 데이터셋 등록 상태와 collection의 status=active 설정을 확인해 주세요.",
            "table_catalog_load_status": table_status or overall_status or "not_available",
            "registered_dataset_count": 0,
        }
    return {}


# 함수 설명: 메타데이터 로더의 실패 사유를 비밀 정보 없이 후속 오류 응답으로 전달할 형태로 정리합니다.
def _metadata_load_failures(loads: dict[str, Any]) -> list[dict[str, str]]:
    """Keep the metadata loader failure reason through normalization without secrets."""

    failed: list[dict[str, str]] = []
    for metadata_kind, raw_load in loads.items():
        if not isinstance(raw_load, dict):
            continue
        status = str(raw_load.get("status") or "").strip().lower()
        if status not in {"error", "failed", "failure", "invalid", "skipped"}:
            continue
        raw_errors = raw_load.get("errors") if isinstance(raw_load.get("errors"), list) else []
        first_error = next((item for item in raw_errors if isinstance(item, dict)), {})
        error_type = str(first_error.get("type") or status or "metadata_load_error").strip()
        detail = _safe_metadata_error_detail(first_error.get("message") or raw_load.get("message") or "")
        failed.append(
            {
                "metadata_kind": str(metadata_kind or "metadata"),
                "status": status,
                "error_type": error_type,
                "detail": detail,
            }
        )
    return failed


# 함수 설명: stale 라우터 응답이 들어와도 동일한 메타데이터 로드 실패 계약을 반환합니다.
def _metadata_load_error(
    failed_loads: list[dict[str, str]],
    *,
    registered_dataset_count: int,
) -> dict[str, Any]:
    """Return the same typed failure even if a stale router reached normalizer."""

    first = _primary_metadata_load_failure(failed_loads)
    kind = str(first.get("metadata_kind") or "metadata")
    error_type = str(first.get("error_type") or first.get("status") or "metadata_load_error")
    detail = str(first.get("detail") or "상세 오류 정보가 없습니다.")
    return {
        "type": "table_catalog_metadata_unavailable",
        "reason": "metadata_connection_or_loader_failed",
        "message": (
            "메타데이터 연결 정보를 확인해 주세요. 분석에 필요한 메타데이터를 읽지 못해 분석을 시작하지 않았습니다. "
            f"상세 사유: {kind} 조회 실패 ({error_type}) - {detail}"
        ),
        "table_catalog_load_status": str(first.get("status") or "error"),
        "registered_dataset_count": registered_dataset_count,
        "metadata_failures": deepcopy(failed_loads[:3]),
    }


# 함수 설명: 조회 가능한 데이터셋의 신뢰 기준인 Table Catalog 실패를 우선 원인으로 선택합니다.
def _primary_metadata_load_failure(failed_loads: list[dict[str, str]]) -> dict[str, str]:
    """Prefer Table Catalog because it is the trusted execution allowlist."""

    return next(
        (item for item in failed_loads if str(item.get("metadata_kind") or "") == "table_catalog_items"),
        failed_loads[0] if failed_loads else {},
    )


# 함수 설명: MongoDB 연결 오류의 조치 정보는 남기되 URI 자격 증명과 과도한 길이를 제거합니다.
def _safe_metadata_error_detail(value: Any) -> str:
    """Redact MongoDB credentials while preserving the actionable network error."""

    text = " ".join(str(value or "").split())
    text = re.sub(r"mongodb(?:\+srv)?://[^\s@/]+@", "mongodb://***@", text, flags=re.IGNORECASE)
    return text[:500] if text else "상세 오류 정보가 없습니다."


# 함수 설명: LLM이 현재 Table Catalog에 없는 데이터셋을 계획에 넣었는지 확인해 실행 전 차단합니다.
def _unregistered_dataset_error(
    retrieval_jobs: list[dict[str, Any]],
    envelope: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    """Reject LLM dataset keys that are not in the current Table Catalog."""

    load = envelope.get("metadata_load") if isinstance(envelope.get("metadata_load"), dict) else {}
    if not load and isinstance(candidates.get("metadata_load"), dict):
        load = candidates.get("metadata_load")
    # This strict check is for the actual graph, which supplies loader evidence.
    # Bare unit callers still exercise the downstream trusted hydrator guard.
    if not isinstance(load, dict) or not load:
        return {}
    table_items = _execution_catalog_registry_items(envelope, candidates)
    registered = {
        str(item.get("dataset_key") or _metadata_payload(item).get("dataset_key") or "").strip()
        for item in table_items
        if isinstance(item, dict)
    }
    registered.discard("")
    unknown = [
        str(job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict)
        and str(job.get("dataset_key") or "").strip()
        and str(job.get("dataset_key") or "").strip() not in registered
    ]
    unknown = list(dict.fromkeys(unknown))
    if not unknown:
        return {}
    return {
        "type": "unregistered_dataset_key",
        "message": "등록된 Table Catalog에서 확인되지 않는 데이터셋이 요청 계획에 포함되어 분석을 시작하지 않았습니다: "
        + ", ".join(unknown),
        "dataset_keys": unknown,
        "registered_dataset_count": len(registered),
    }


# 함수 설명: 앞 단계에서 이미 확정한 메타데이터 차단 원인을 변경 없이 보존합니다.
def _catalog_error_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Preserve the pre-LLM router's deterministic metadata failure verbatim."""

    validation_errors = plan.get("validation_errors")
    for raw in validation_errors if isinstance(validation_errors, list) else []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("type") or "").strip() == "table_catalog_metadata_unavailable":
            return deepcopy(raw)
    return {}


# 함수 설명: 메타데이터 오류 상황에서 데이터셋·컬럼을 추측하지 않는 최종 차단 페이로드를 생성합니다.
def _blocked_catalog_metadata_payload(
    payload: dict[str, Any],
    error: dict[str, Any],
) -> dict[str, Any]:
    """Return a typed terminal plan without inventing sources or columns."""

    next_payload = deepcopy(payload)
    plan = {
        "analysis_kind": "metadata_catalog_unavailable",
        "request_scope": "new_analysis",
        "reference_mode": "none",
        "reuse_strategy": "none",
        "metadata_refs": [],
        "retrieval_jobs": [],
        "pandas_execution_plan": [],
        "output_contract": {
            "result_mode": "detail",
            "required_columns": [],
            "grain_columns": [],
            "metric_columns": [],
            "result_columns": [],
            "strict_result_columns": True,
        },
        "validation_errors": [deepcopy(error)],
        "intent_ir": {
            "version": "intent.ir.v1",
            "status": "blocked",
            "route_source_aliases": [],
        },
    }
    next_payload["intent_plan"] = plan
    next_payload["metadata_refs"] = []
    next_payload["execution_gate"] = {
        "stage": "04_intent_plan_normalizer",
        "status": "blocked",
        "reason": str(error.get("type") or "metadata_contract_blocked"),
        "critical_failures": [deepcopy(error)],
        "pandas_execution_allowed": False,
        "model_response_policy": "ignore",
    }
    trace = next_payload.setdefault("trace", {})
    errors = trace.setdefault("errors", [])
    if not any(
        isinstance(item, dict)
        and str(item.get("type") or "") == str(error.get("type") or "")
        for item in errors
    ):
        errors.append(deepcopy(error))
    trace.setdefault("inspection", {})["intent"] = {
        "stage": "04_intent_plan_normalizer",
        "status": "error",
        "metadata_ref_guard": {"status": "unavailable", "removed_unknown_refs": []},
        "metadata_catalog_guard": deepcopy(error),
        "llm_plan_accepted": False,
    }
    next_payload["analysis"] = {
        "status": "error",
        "row_count": 0,
        "columns": [],
        "error": deepcopy(error),
        "errors": [str(error.get("message") or "")],
        "repairable_errors": [],
        "step_outputs": [],
        "function_case_results": [],
    }
    next_payload["data"] = {"columns": [], "rows": [], "row_count": 0, "data_ref": ""}
    next_payload["answer_message"] = str(error.get("message") or "")
    return next_payload


# 함수 설명: 모델이 언급한 메타데이터 참조 중 현재 후보 목록에 실제 존재하는 항목만 남깁니다.
def _known_metadata_refs(
    refs: list[dict[str, str]],
    candidates: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    candidate_items = [
        item
        for key in ("domain_items", "table_catalog_items", "main_flow_filters")
        for item in (
            candidates.get(key) if isinstance(candidates.get(key), list) else []
        )
        if isinstance(item, dict)
    ]
    if not candidate_items:
        return [], {
            "status": "unavailable",
            "removed_unknown_refs": deepcopy(refs),
        }
    known: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    for raw in refs:
        ref = _metadata_ref(raw)
        if not ref:
            continue
        if _find_metadata_item(candidates, ref):
            if ref not in known:
                known.append(ref)
        elif ref not in removed:
            removed.append(ref)
    return known, {
        "status": "filtered" if removed else "not_needed",
        "removed_unknown_refs": removed,
    }


# 함수 설명: 시간·조건처럼 실행에 영향을 주는 Domain은 질문 alias로 잠긴 참조만 남겨 모델의 과잉 선택을 차단합니다.
def _execution_compatible_metadata_refs(
    refs: list[dict[str, str]],
    candidates: dict[str, Any],
    locked_refs: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    locked = {
        (str(item.get("section") or "").strip(), str(item.get("key") or "").strip())
        for item in locked_refs
        if isinstance(item, dict)
    }
    result: list[dict[str, str]] = []
    removed: list[dict[str, str]] = []
    for raw in refs:
        ref = _metadata_ref(raw)
        if not ref:
            continue
        item = _find_metadata_item(candidates, ref)
        payload = _metadata_payload(item)
        execution_bearing = isinstance(payload.get("temporal_semantics"), (dict, list)) or bool(
            _metadata_filter_contracts(payload)
        )
        marker = (ref.get("section", ""), ref.get("key", ""))
        if execution_bearing and marker not in locked:
            if ref not in removed:
                removed.append(ref)
            continue
        if ref not in result:
            result.append(ref)
    return result, removed


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
            "source_candidates": _merge_strings(
                [column],
                _mapped_column_candidates(table_item, column),
            ),
        }
        for column in canonical_columns
    ]
    # Pandas receives catalog-normalized rows, so executable grain columns use
    # the canonical product keys. Physical candidates remain available only as
    # source-lineage metadata for retrieval and validation.
    grain_columns = [
        mapping["canonical_key"]
        for mapping in mappings
        if mapping.get("canonical_key") and mapping.get("source_candidates")
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


# 함수 설명: `_resolve_output_grain_plan()`는 여러 output·grain·PLAN 후보와 우선순위를 검토해 실제 사용할 값을 확정합니다.
def _resolve_output_grain_plan(
    pandas_plan: list[Any],
    entity_grain_plan: dict[str, Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Combine validated breakdown dimensions with the entity identity grain."""
    explicit_columns: list[str] = []
    source_aliases: list[str] = []
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation not in {"groupby_and_aggregate", "group_by_and_aggregate", "aggregate"}:
            continue
        alias = str(step.get("source_alias") or "").strip()
        if alias and alias not in source_aliases:
            source_aliases.append(alias)
        explicit_columns = _merge_strings(
            explicit_columns,
            _string_list(
                step.get("group_by")
                or step.get("group_by_columns")
                or step.get("group_columns")
                or step.get("group_cols")
            ),
        )
    entity_columns = _string_list(entity_grain_plan.get("grain_columns"))
    aliases_to_check = source_aliases or _string_list(entity_grain_plan.get("source_alias"))
    combined: list[str] = []
    seen_identities: set[str] = set()
    for column in [*explicit_columns, *entity_columns]:
        identity = _grain_column_identity(
            column,
            aliases_to_check,
            candidates,
            retrieval_jobs,
        )
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        combined.append(column)
    if not combined:
        return {}

    validated: list[str] = []
    rejected: list[dict[str, Any]] = []
    for column in combined:
        supported_aliases: list[str] = []
        for alias in aliases_to_check:
            dataset_key = _dataset_key_for_alias(alias, retrieval_jobs)
            if _catalog_supports_domain_column(candidates, dataset_key, column):
                supported_aliases.append(alias)
        if supported_aliases or not aliases_to_check:
            validated.append(column)
        else:
            rejected.append({"column": column, "source_aliases": aliases_to_check})
    return {
        "entity_grain_columns": entity_columns,
        "breakdown_columns": [column for column in validated if column not in entity_columns],
        "grain_columns": validated,
        "source_aliases": aliases_to_check,
        "rejected_columns": rejected,
        "strict": True,
    }


# 함수 설명: `_grain_column_identity()`는 04 의도 계획 정규화기 처리 중 컬럼·식별자 관련 값을 계산·변환하는 내부 helper입니다.
def _grain_column_identity(
    column: str,
    source_aliases: list[str],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> str:
    target = _normalized_column_key(column)
    canonical_matches: set[str] = set()
    for alias in source_aliases:
        dataset_key = _dataset_key_for_alias(alias, retrieval_jobs)
        item = _table_catalog_item(candidates, dataset_key)
        payload = _metadata_payload(item)
        for mapping_name in ("filter_mappings", "standard_column_aliases"):
            mapping = payload.get(mapping_name)
            if not isinstance(mapping, dict):
                continue
            for canonical, raw_values in mapping.items():
                values = _merge_strings(
                    _string_list(canonical),
                    _string_list(raw_values),
                )
                if target in {_normalized_column_key(value) for value in values}:
                    canonical_matches.add(_normalized_column_key(canonical))
    return next(iter(canonical_matches)) if len(canonical_matches) == 1 else target


# 함수 설명: `_previous_source_refs()`는 04 의도 계획 정규화기 처리 중 데이터 소스·참조 관련 값을 계산·변환하는 내부 helper입니다.
def _previous_source_refs(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    raw = state.get("runtime_source_refs") if isinstance(state.get("runtime_source_refs"), dict) else {}
    return {
        str(alias): deepcopy(value)
        for alias, value in raw.items()
        if str(alias).strip() and isinstance(value, dict)
    }


# 함수 설명: `_compile_execution_graph()`는 04 의도 계획 정규화기 처리 중 execution·graph 관련 값을 계산·변환하는 내부 helper입니다.
def _compile_execution_graph(
    pandas_plan: list[Any],
    retrieval_jobs: list[dict[str, Any]],
    payload: dict[str, Any],
    reuse_strategy: str,
) -> dict[str, Any]:
    """Compile plan steps into typed leaf and node-output references."""
    jobs = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in retrieval_jobs
        if isinstance(item, dict)
        and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    }
    previous_refs = _previous_source_refs(payload)
    requirements: list[dict[str, Any]] = []
    requirement_aliases: set[str] = set()
    nodes: list[dict[str, Any]] = []
    output_aliases: dict[str, str] = {}
    node_external_aliases: dict[str, set[str]] = {}
    declared_node_ids = {
        str(raw.get("node_id") or f"step_{index + 1}_{re.sub(r'[^0-9a-z]+', '_', str(raw.get('operation') or raw.get('step') or 'operation').strip().lower()).strip('_') or 'operation'}").strip()
        for index, raw in enumerate(pandas_plan)
        if isinstance(raw, dict)
    }
    validation_errors: list[dict[str, Any]] = []
    unary_previous_operations = {
        "sort_and_top_n",
        "sort",
        "top_n",
        "bottom_n",
        "filter_result",
        "select_columns",
        "rename_columns",
    }

    # 함수 설명: `leaf_ref()`는 04 의도 계획 정규화기 처리 중 참조 관련 값을 계산·변환하는 내부 helper입니다.
    def leaf_ref(alias: str) -> dict[str, str] | None:
        if alias in jobs:
            job = jobs[alias]
            if alias not in requirement_aliases:
                requirements.append(
                    {
                        "source_alias": alias,
                        "provider": "retrieval_job",
                        "dataset_key": str(job.get("dataset_key") or "").strip(),
                        "required": True,
                    }
                )
                requirement_aliases.add(alias)
            return {"kind": "external_source", "ref": alias}
        if alias in previous_refs and reuse_strategy == "previous_source":
            ref = previous_refs[alias]
            if alias not in requirement_aliases:
                requirements.append(
                    {
                        "source_alias": alias,
                        "provider": "previous_source",
                        "dataset_key": str(ref.get("dataset_key") or "").strip(),
                        "required": True,
                    }
                )
                requirement_aliases.add(alias)
            return {"kind": "external_source", "ref": alias}
        if alias in {PREVIOUS_RESULT_ALIAS, "upstream_result"}:
            if alias not in requirement_aliases:
                requirements.append(
                    {
                        "source_alias": alias,
                        "provider": "previous_result",
                        "dataset_key": "",
                        "required": True,
                    }
                )
                requirement_aliases.add(alias)
            return {"kind": "external_source", "ref": alias}
        if reuse_strategy == "previous_source":
            ref = previous_refs.get(alias, {})
            if alias not in requirement_aliases:
                requirements.append(
                    {
                        "source_alias": alias,
                        "provider": "previous_source",
                        "dataset_key": str(ref.get("dataset_key") or "").strip(),
                        "required": True,
                    }
                )
                requirement_aliases.add(alias)
            return {"kind": "external_source", "ref": alias}
        return None

    for index, raw in enumerate(pandas_plan):
        if not isinstance(raw, dict):
            continue
        operation = str(raw.get("operation") or raw.get("step") or "operation").strip().lower()
        safe_operation = re.sub(r"[^0-9a-z]+", "_", operation).strip("_") or "operation"
        node_id = str(raw.get("node_id") or f"step_{index + 1}_{safe_operation}").strip()
        inputs: list[dict[str, str]] = []
        explicit_inputs = raw.get("inputs") if isinstance(raw.get("inputs"), list) else []
        for value in explicit_inputs:
            if isinstance(value, dict) and value.get("kind") in {"external_source", "node_output"} and value.get("ref"):
                kind = str(value["kind"])
                ref = str(value["ref"])
                if kind == "external_source":
                    resolved_leaf = leaf_ref(ref)
                    if resolved_leaf:
                        inputs.append(resolved_leaf)
                    else:
                        validation_errors.append(
                            {
                                "type": "unresolved_execution_input",
                                "message": "typed external source provider could not be resolved.",
                                "node_id": node_id,
                                "source_alias": ref,
                            }
                        )
                else:
                    if ref in jobs or ref in previous_refs or ref in {
                        PREVIOUS_RESULT_ALIAS,
                        "upstream_result",
                    }:
                        upstream_node = next(
                            (
                                str(item.get("node_id") or "").strip()
                                for item in reversed(nodes)
                                if ref
                                in node_external_aliases.get(
                                    str(item.get("node_id") or "").strip(),
                                    set(),
                                )
                            ),
                            "",
                        )
                        if upstream_node:
                            inputs.append({"kind": "node_output", "ref": upstream_node})
                        else:
                            resolved_leaf = leaf_ref(ref)
                            if resolved_leaf:
                                inputs.append(resolved_leaf)
                        continue
                    resolved_ref = ref if ref in declared_node_ids else output_aliases.get(ref, "")
                    if resolved_ref:
                        inputs.append({"kind": kind, "ref": resolved_ref})
                    else:
                        validation_errors.append(
                            {
                                "type": "unresolved_execution_input",
                                "message": "typed node output provider could not be resolved.",
                                "node_id": node_id,
                                "node_output_ref": ref,
                            }
                        )
        if not inputs:
            aliases: list[str] = []
            if operation in {"join", "merge", "outer_join", "left_join", "compare_presence"}:
                aliases = _string_list(
                    [
                        raw.get("left_source_alias") or raw.get("source_alias"),
                        raw.get("right_source_alias") or raw.get("reference_source_alias"),
                    ]
                )
            else:
                aliases = _string_list(raw.get("source_alias"))
            for alias in aliases:
                if alias in output_aliases:
                    inputs.append({"kind": "node_output", "ref": output_aliases[alias]})
                    continue
                resolved_leaf = leaf_ref(alias)
                if resolved_leaf:
                    inputs.append(resolved_leaf)
                    continue
                if nodes and operation in unary_previous_operations:
                    inputs.append({"kind": "node_output", "ref": nodes[-1]["node_id"]})
                    continue
                validation_errors.append(
                    {
                        "type": "unresolved_execution_input",
                        "message": "pandas step input provider could not be resolved.",
                        "node_id": node_id,
                        "source_alias": alias,
                    }
                )
        node = {
            "node_id": node_id,
            "operation": operation,
            "inputs": inputs,
        }
        output_alias = str(raw.get("output_alias") or raw.get("result_alias") or "").strip()
        if output_alias:
            node["output_alias"] = output_alias
            output_aliases[output_alias] = node_id
        nodes.append(node)
        leaf_aliases: set[str] = set()
        for item in inputs:
            if item.get("kind") == "external_source":
                leaf_aliases.add(str(item.get("ref") or "").strip())
            elif item.get("kind") == "node_output":
                leaf_aliases.update(
                    node_external_aliases.get(str(item.get("ref") or "").strip(), set())
                )
        node_external_aliases[node_id] = {value for value in leaf_aliases if value}
    return {
        "version": 1,
        "nodes": nodes,
        "external_source_requirements": requirements,
        "validation_errors": validation_errors,
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
        # Retrieval rows are standardized before pandas execution. Executable
        # join keys and selected value columns therefore stay on the canonical
        # contract; physical candidates are lineage metadata only.
        right_value_columns = [
            mapping["canonical_key"]
            for mapping in right_value_mappings
            if mapping.get("canonical_key") and mapping.get("source_candidates")
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
                "left_keys": [item["canonical_key"] for item in key_mappings],
                "right_keys": [item["canonical_key"] for item in key_mappings],
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
            [canonical_key],
            _mapped_column_candidates(right_table, canonical_key),
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

    # The aggregation can be downstream of an explicit join.  In that shape
    # ``source_alias`` names the joined frame rather than the right retrieval
    # source, so require both lineage and the right catalog's column ownership
    # instead of relying on a literal source-alias match.
    aggregations = _aggregation_contracts_for_alias(
        pandas_plan,
        right_alias,
        candidates=candidates,
        dataset_key=right_dataset,
    )
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
def _resolve_presence_comparison_plan(
    pandas_plan: list[Any],
    output_contract: dict[str, Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve an explicit presence comparison into physical source contracts."""
    compare_steps = [
        item
        for item in pandas_plan
        if isinstance(item, dict)
        and str(item.get("operation") or item.get("step") or "").strip().lower()
        == "compare_presence"
    ]
    if len(compare_steps) != 1:
        return {}
    step = compare_steps[0]
    bindings = [
        item for item in output_contract.get("metric_bindings", []) if isinstance(item, dict)
    ]
    jobs_by_alias = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in retrieval_jobs
        if isinstance(item, dict)
    }
    nodes_by_id = {
        str(item.get("node_id") or "").strip(): item
        for item in pandas_plan
        if isinstance(item, dict) and str(item.get("node_id") or "").strip()
    }
    compare_inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []

    # 함수 설명: `source_step_for_side()`는 STEP·대상·SIDE 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
    def source_step_for_side(index: int) -> dict[str, Any]:
        if index >= len(compare_inputs) or not isinstance(compare_inputs[index], dict):
            return {}
        item = compare_inputs[index]
        if str(item.get("kind") or "").strip() == "node_output":
            return nodes_by_id.get(str(item.get("ref") or "").strip(), {})
        return {}

    # 함수 설명: `external_alias_for_side()`는 04 의도 계획 정규화기 처리 중 alias·대상·SIDE 관련 값을 계산·변환하는 내부 helper입니다.
    def external_alias_for_side(index: int, raw_alias: str) -> str:
        if raw_alias in jobs_by_alias:
            return raw_alias
        source_step = source_step_for_side(index)
        alias = str(source_step.get("source_alias") or "").strip()
        if alias in jobs_by_alias:
            return alias
        typed_alias = _typed_external_input_alias(source_step)
        if typed_alias in jobs_by_alias:
            return typed_alias
        if index < len(compare_inputs) and isinstance(compare_inputs[index], dict):
            item = compare_inputs[index]
            if str(item.get("kind") or "").strip() == "external_source":
                alias = str(item.get("ref") or "").strip()
                if alias in jobs_by_alias:
                    return alias
        return ""

    raw_left_alias = str(step.get("left_source_alias") or step.get("source_alias") or "").strip()
    raw_right_alias = str(
        step.get("right_source_alias") or step.get("reference_source_alias") or ""
    ).strip()
    left_alias = external_alias_for_side(0, raw_left_alias)
    right_alias = external_alias_for_side(1, raw_right_alias)
    binding_aliases = _string_list([item.get("source_alias") for item in bindings])
    if not left_alias and binding_aliases:
        left_alias = binding_aliases[0]
    if not right_alias:
        right_alias = next((alias for alias in binding_aliases if alias != left_alias), "")
    if not left_alias or not right_alias or left_alias == right_alias:
        return {}

    # 함수 설명: `select_binding()`는 조건과 우선순위에 맞는 binding만 골라 원래 순서를 유지해 반환합니다.
    def select_binding(alias: str, side: str, index: int) -> dict[str, Any]:
        explicit_output = str(step.get(f"{side}_metric_column") or "").strip()
        for binding in bindings:
            if str(binding.get("source_alias") or "").strip() != alias:
                continue
            if explicit_output and explicit_output not in {
                str(binding.get("output_column") or "").strip(),
                str(binding.get("source_column") or "").strip(),
            }:
                continue
            return binding
        source_step = source_step_for_side(index)
        aggregations = (
            source_step.get("aggregations")
            if isinstance(source_step.get("aggregations"), list)
            else []
        )
        candidates_for_binding = [item for item in aggregations if isinstance(item, dict)]
        if not candidates_for_binding and (
            source_step.get("agg_column") or source_step.get("aggregate_column")
        ):
            candidates_for_binding = [
                {
                    "column": source_step.get("agg_column")
                    or source_step.get("aggregate_column"),
                    "method": source_step.get("agg_method")
                    or source_step.get("aggregation")
                    or "sum",
                    "output_column": explicit_output,
                }
            ]
        for raw in candidates_for_binding:
            source_column = str(raw.get("column") or raw.get("source_column") or "").strip()
            output_column = str(raw.get("output_column") or explicit_output or source_column).strip()
            if explicit_output and explicit_output not in {source_column, output_column}:
                continue
            if source_column and output_column:
                return {
                    "source_alias": alias,
                    "dataset_key": str(jobs_by_alias.get(alias, {}).get("dataset_key") or "").strip(),
                    "source_column": source_column,
                    "aggregation": str(raw.get("method") or raw.get("aggregation") or "sum").strip().lower(),
                    "output_column": output_column,
                }
        return {}

    left_binding = select_binding(left_alias, "left", 0)
    right_binding = select_binding(right_alias, "right", 1)
    if not left_binding or not right_binding:
        return {}
    grain_columns = _string_list(output_contract.get("grain_columns"))
    if not grain_columns:
        grain_columns = _string_list(
            step.get("group_by") or step.get("group_by_columns") or step.get("join_keys")
        )
    if not grain_columns:
        return {}
    grain_mappings: list[dict[str, Any]] = []
    for column in grain_columns:
        source_candidates: dict[str, list[str]] = {}
        for alias in (left_alias, right_alias):
            dataset_key = str(jobs_by_alias.get(alias, {}).get("dataset_key") or "").strip()
            table_item = _table_catalog_item(candidates, dataset_key)
            source_candidates[alias] = _merge_strings(
                _mapped_column_candidates(table_item, column),
                [column],
            )
        grain_mappings.append(
            {
                "canonical_column": column,
                "output_column": column,
                "source_candidates": source_candidates,
            }
        )

    # 함수 설명: `metric_contract()`는 04 의도 계획 정규화기 처리 중 contract 관련 값을 계산·변환하는 내부 helper입니다.
    def metric_contract(binding: dict[str, Any]) -> dict[str, Any]:
        alias = str(binding.get("source_alias") or "").strip()
        dataset_key = str(jobs_by_alias.get(alias, {}).get("dataset_key") or "").strip()
        source_column = str(binding.get("source_column") or "").strip()
        table_item = _table_catalog_item(candidates, dataset_key)
        return {
            "source_alias": alias,
            "dataset_key": dataset_key,
            "source_column": source_column,
            "source_candidates": _merge_strings(
                _mapped_column_candidates(table_item, source_column),
                [source_column],
            ),
            "aggregation": str(binding.get("aggregation") or "sum").strip().lower(),
            "output_column": str(binding.get("output_column") or "").strip(),
        }

    rule = str(
        step.get("presence_rule") or "left_positive_right_missing_or_zero"
    ).strip()
    if rule != "left_positive_right_missing_or_zero":
        return {}
    return {
        "operation": "compare_presence",
        "presence_rule": rule,
        "grain_mappings": grain_mappings,
        "left_metric": metric_contract(left_binding),
        "right_metric": metric_contract(right_binding),
        "strict": True,
    }


# 함수 설명: 두 source에서 병합된 수치 metric 사이의 명시적 비교 조건을 결정론적 실행 계약으로 확정합니다.
def _resolve_metric_comparison_plan(
    pandas_plan: list[Any],
    output_contract: dict[str, Any],
    metric_merge_plan: dict[str, Any],
    question: str = "",
) -> dict[str, Any]:
    compare_steps = [
        item
        for item in pandas_plan
        if isinstance(item, dict)
        and str(item.get("operation") or item.get("step") or "").strip().lower()
        == "compare_metrics"
    ]
    if len(compare_steps) > 1 or not isinstance(metric_merge_plan, dict):
        return {}
    if metric_merge_plan.get("strict") is not True:
        return {}

    selection_source = "typed_compare_metrics_step"
    if compare_steps:
        step = compare_steps[0]
    else:
        step = _question_metric_comparison_contract(question, output_contract)
        selection_source = "question_metric_comparison_guard"
        if not step:
            return {}
    lhs_raw = str(
        step.get("lhs_metric_column")
        or step.get("comparison_metric_column")
        or step.get("left_metric_column")
        or ""
    ).strip()
    rhs_raw = str(
        step.get("rhs_metric_column")
        or step.get("baseline_metric_column")
        or step.get("right_metric_column")
        or ""
    ).strip()
    operator = _canonical_filter_operator(
        step.get("operator") or step.get("comparison_operator") or ""
    )
    if not lhs_raw or not rhs_raw or operator not in {"gt", "ge", "lt", "le", "eq", "ne"}:
        return {}

    available_metrics = _merge_strings(
        _string_list(output_contract.get("metric_columns")),
        _string_list(
            [
                item.get("output_column")
                for item in output_contract.get("metric_bindings", [])
                if isinstance(item, dict)
            ]
        ),
    )
    metric_index: dict[str, list[str]] = {}
    for column in available_metrics:
        metric_index.setdefault(_normalized_column_key(column), []).append(column)

    # 함수 설명: `resolve_metric()`은 비교 단계의 metric 표기를 output contract의 유일한 실제 결과 컬럼으로 확정합니다.
    def resolve_metric(value: str) -> str:
        matches = metric_index.get(_normalized_column_key(value), [])
        return matches[0] if len(matches) == 1 else ""

    lhs_column = resolve_metric(lhs_raw)
    rhs_column = resolve_metric(rhs_raw)
    if not lhs_column or not rhs_column or lhs_column == rhs_column:
        return {}

    comparison_merge_plan = deepcopy(metric_merge_plan)
    comparison_merge_plan["join_type"] = "inner"
    comparison_merge_plan["fill_zero_on_success"] = False
    return {
        "operation": "compare_metrics",
        "merge_plan": comparison_merge_plan,
        "source_transforms": deepcopy(metric_merge_plan.get("source_transforms", [])),
        "lhs_metric_column": lhs_column,
        "rhs_metric_column": rhs_column,
        "operator": operator,
        "null_numeric_policy": "exclude_missing_operand",
        "selection_source": selection_source,
        "strict": True,
    }


# 함수 설명: 두 metric의 `대비/보다` 비교 표현을 primary metric 기준의 typed 수치 비교 계약으로 보완합니다.
# 특정 dataset·공정·metric 이름은 사용하지 않으며, 명시적 순위/정렬 요청은 비교 조건으로 바꾸지 않습니다.
def _question_metric_comparison_contract(
    question: str,
    output_contract: dict[str, Any],
) -> dict[str, Any]:
    text = str(question or "").strip().casefold()
    if not text:
        return {}
    connectors = ("대비", "보다", "compared to", "versus", " vs ")
    if not any(marker in text for marker in connectors):
        return {}
    ranking_markers = (
        "순으로",
        "순서",
        "상위",
        "하위",
        "내림차순",
        "오름차순",
        "랭킹",
        "ranking",
        "top ",
        "bottom ",
    )
    if any(marker in text for marker in ranking_markers):
        return {}
    positive_markers = ("많은", "크거나", "큰", "높은", "초과", "greater", "higher", "more than")
    negative_markers = ("적은", "작거나", "작은", "낮은", "미만", "less", "lower", "fewer than")
    has_positive = any(marker in text for marker in positive_markers)
    has_negative = any(marker in text for marker in negative_markers)
    if has_positive == has_negative:
        return {}

    metrics = _string_list(output_contract.get("metric_columns"))
    if len(metrics) != 2:
        return {}
    primary_metric = str(output_contract.get("primary_metric") or "").strip()
    primary_matches = [
        metric
        for metric in metrics
        if _normalized_column_key(metric) == _normalized_column_key(primary_metric)
    ]
    if len(primary_matches) != 1:
        return {}
    lhs_metric = primary_matches[0]
    rhs_metrics = [metric for metric in metrics if metric != lhs_metric]
    if len(rhs_metrics) != 1:
        return {}
    return {
        "operation": "compare_metrics",
        "lhs_metric_column": lhs_metric,
        "operator": "gt" if has_positive else "lt",
        "rhs_metric_column": rhs_metrics[0],
    }


# 함수 설명: `_presence_output_contract()`는 04 의도 계획 정규화기 처리 중 output·contract 관련 값을 계산·변환하는 내부 helper입니다.
def _presence_output_contract(
    output_contract: dict[str, Any],
    presence_plan: dict[str, Any],
) -> dict[str, Any]:
    contract = deepcopy(output_contract)
    grain_columns = _string_list(
        [
            item.get("output_column")
            for item in presence_plan.get("grain_mappings", [])
            if isinstance(item, dict)
        ]
    )
    metric_columns = _string_list(
        [
            presence_plan.get("left_metric", {}).get("output_column"),
            presence_plan.get("right_metric", {}).get("output_column"),
        ]
    )
    result_columns = _merge_strings(grain_columns, metric_columns)
    contract["result_mode"] = "aggregate"
    contract["grain_columns"] = grain_columns
    contract["metric_columns"] = metric_columns
    contract["metric_bindings"] = [
        {
            "source_alias": str(item.get("source_alias") or "").strip(),
            "dataset_key": str(item.get("dataset_key") or "").strip(),
            "source_column": str(item.get("source_column") or "").strip(),
            "aggregation": str(item.get("aggregation") or "sum").strip(),
            "output_column": str(item.get("output_column") or "").strip(),
        }
        for item in (
            presence_plan.get("left_metric", {}),
            presence_plan.get("right_metric", {}),
        )
        if isinstance(item, dict)
    ]
    contract["required_columns"] = result_columns
    contract["result_columns"] = result_columns
    contract["strict_result_columns"] = True
    labels = contract.get("column_labels") if isinstance(contract.get("column_labels"), dict) else {}
    allowed = {_normalized_column_key(value) for value in result_columns}
    if labels:
        contract["column_labels"] = {
            key: value
            for key, value in labels.items()
            if _normalized_column_key(key) in allowed
        }
    return contract


# 함수 설명: `_resolve_metric_merge_plan()`는 여러 metric·merge·PLAN 후보와 우선순위를 검토해 실제 사용할 값을 확정합니다.
def _resolve_metric_merge_plan(
    plan: dict[str, Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    pandas_plan: list[Any],
    resolved_grain_plan: dict[str, Any],
    business_time_guard: dict[str, Any],
    function_cases: list[dict[str, Any]],
) -> dict[str, Any]:
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
    if len(metrics) < 2 or len(metric_aliases) < 2:
        return {}

    temporal_merge = bool(
        business_time_guard.get("status") == "applied"
        and metric_aliases.intersection(temporal_aliases)
    )
    typed_join_aliases: set[str] = set()
    typed_join_nodes: list[str] = []
    typed_population_policies: list[str] = []
    for index, step in enumerate(pandas_plan):
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation not in {"join", "merge", "outer_join", "left_join"}:
            continue
        inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        side_aliases: list[str] = []
        for item in inputs[:2]:
            if not isinstance(item, dict):
                continue
            aliases = _step_external_source_aliases(
                {"inputs": [item]},
                pandas_plan,
                set(jobs_by_alias),
            )
            if len(aliases) == 1:
                side_aliases.append(aliases[0])
        if len(side_aliases) != 2 or side_aliases[0] == side_aliases[1]:
            continue
        if not set(side_aliases).issubset(metric_aliases):
            continue
        typed_join_aliases.update(side_aliases)
        typed_join_nodes.append(
            str(step.get("node_id") or f"__step_{index + 1}").strip()
        )
        population_policy = str(step.get("population_policy") or "").strip().lower()
        if population_policy:
            typed_population_policies.append(population_policy)
    if not temporal_merge and len(typed_join_aliases) < 2:
        return {}
    if typed_join_aliases:
        metrics = [
            item
            for item in metrics
            if str(item.get("source_alias") or "").strip() in typed_join_aliases
        ]
        metric_aliases = {
            str(item.get("source_alias") or "").strip()
            for item in metrics
            if str(item.get("source_alias") or "").strip()
        }
        if len(metrics) < 2 or len(metric_aliases) < 2:
            return {}

    canonical_grain = _string_list(
        resolved_grain_plan.get("canonical_columns")
        or resolved_grain_plan.get("grain_columns")
    )
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
    use_left_population = bool(
        typed_population_policies
        and set(typed_population_policies) == {"left_source_only"}
    )
    source_transforms = _metric_merge_source_transforms(
        pandas_plan,
        metric_aliases,
        typed_join_nodes,
        function_cases,
    )
    return {
        "operation": "merge_metric_sources",
        "join_type": "left" if use_left_population else "outer",
        "population_policy": (
            "left_source_only"
            if use_left_population
            else "preserve_all_metric_source_keys"
        ),
        "selection_source": (
            "temporal_metric_contract"
            if temporal_merge
            else "typed_metric_join_lineage"
        ),
        "join_node_ids": typed_join_nodes,
        "source_transforms": source_transforms,
        "grain_mappings": grain_mappings,
        "metrics": metrics,
        "fill_zero_on_success": True,
        "strict": True,
    }


# 함수 설명: typed join의 실제 조상 node에 포함된 source별 Function Case만 결정론적 병합 전처리 계약으로 보존합니다.
def _metric_merge_source_transforms(
    pandas_plan: list[Any],
    metric_aliases: set[str],
    join_node_ids: list[str],
    function_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    nodes_by_id, output_aliases = _pandas_plan_lineage(pandas_plan)
    ancestor_ids: set[str] = set()

    # 함수 설명: node_output 참조를 역추적해 최종 join에 실제로 연결된 모든 조상 node ID를 수집합니다.
    def visit(node_id: str, visited: set[str]) -> None:
        if not node_id or node_id in visited:
            return
        node = nodes_by_id.get(node_id)
        if not isinstance(node, dict):
            return
        ancestor_ids.add(node_id)
        for item in node.get("inputs", []) if isinstance(node.get("inputs"), list) else []:
            if not isinstance(item, dict) or str(item.get("kind") or "") != "node_output":
                continue
            ref = str(item.get("ref") or "").strip()
            parent_id = ref if ref in nodes_by_id else output_aliases.get(ref, "")
            visit(parent_id, {*visited, node_id})

    for node_id in join_node_ids:
        visit(node_id, set())

    result: list[dict[str, Any]] = []
    for index, raw_step in enumerate(pandas_plan):
        if not isinstance(raw_step, dict):
            continue
        if str(raw_step.get("operation") or "").strip() != "apply_pandas_function_case":
            continue
        node_id = str(raw_step.get("node_id") or f"__step_{index + 1}").strip()
        if join_node_ids and node_id not in ancestor_ids:
            continue
        aliases = _step_external_source_aliases(
            raw_step,
            pandas_plan,
            metric_aliases,
        )
        if len(aliases) != 1 or aliases[0] not in metric_aliases:
            continue
        transform = _function_case_source_transform(
            raw_step,
            aliases[0],
            node_id,
        )
        if transform:
            result.append(transform)

    # pandas_function_cases is the normalized semantic selection contract. A
    # model may emit a source-local Function Case separately from the typed DAG
    # or leave the typed node as a structural stub. Deterministic execution must
    # still preserve that selected transform instead of silently aggregating the
    # unfiltered metric source.
    represented = {_function_case_transform_marker(item) for item in result}
    for index, case in enumerate(function_cases, start=1):
        if not isinstance(case, dict):
            continue
        source_alias = str(case.get("source_alias") or "").strip()
        if source_alias not in metric_aliases:
            continue
        transform = _function_case_source_transform(
            case,
            source_alias,
            f"__selected_function_case_{index}",
        )
        if not transform:
            continue
        marker = _function_case_transform_marker(transform)
        if marker in represented:
            continue
        represented.add(marker)
        result.append(transform)
    return result


# 함수 설명: Function Case step 또는 선택 계약을 결정론적 source transform 공통 구조로 변환합니다.
def _function_case_source_transform(
    value: dict[str, Any],
    source_alias: str,
    node_id: str,
) -> dict[str, Any]:
    function_name = str(value.get("function_name") or "").strip()
    if not source_alias or not function_name.isidentifier():
        return {}
    control_keys = {
        "node_id",
        "operation",
        "step",
        "inputs",
        "output_alias",
        "result_alias",
        "source_alias",
        "function_case_key",
        "key",
        "function_name",
        "input_text",
        "execution_contract",
    }
    arguments = (
        deepcopy(value.get("arguments"))
        if isinstance(value.get("arguments"), dict)
        else {}
    )
    if isinstance(value.get("kwargs"), dict):
        arguments.update(deepcopy(value["kwargs"]))
    for key in PANDAS_COLUMN_SCALAR_KEYS | PANDAS_COLUMN_LIST_KEYS:
        if key in control_keys or key not in value or key in arguments:
            continue
        arguments[key] = deepcopy(value[key])
    return {
        "node_id": node_id,
        "source_alias": source_alias,
        "function_case_key": str(
            value.get("function_case_key") or value.get("key") or ""
        ).strip(),
        "function_name": function_name,
        "input_text": str(value.get("input_text") or ""),
        "arguments": arguments,
    }


# 함수 설명: 동일한 source Function Case 계약을 typed DAG와 선택 목록 사이에서 중복 판정할 안정 키로 만듭니다.
def _function_case_transform_marker(value: dict[str, Any]) -> tuple[str, str, str, str]:
    arguments = value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
    return (
        str(value.get("source_alias") or "").strip(),
        str(value.get("function_name") or "").strip(),
        str(value.get("input_text") or ""),
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
    )


# 함수 설명: 결정론적 metric merge에서 선택된 Function Case가 source transform으로 보존되지 않으면 조회 전에 차단합니다.
def _function_case_metric_lineage_validation_errors(
    function_cases: list[dict[str, Any]],
    metric_merge_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    if not function_cases or not metric_merge_plan:
        return []
    metric_aliases = {
        str(item.get("source_alias") or "").strip()
        for item in metric_merge_plan.get("metrics", [])
        if isinstance(item, dict) and str(item.get("source_alias") or "").strip()
    }
    represented = {
        _function_case_transform_marker(item)
        for item in metric_merge_plan.get("source_transforms", [])
        if isinstance(item, dict)
    }
    issues: list[dict[str, Any]] = []
    for case in function_cases:
        if not isinstance(case, dict):
            continue
        source_alias = str(case.get("source_alias") or "").strip()
        if source_alias not in metric_aliases:
            continue
        transform = _function_case_source_transform(case, source_alias, "")
        if transform and _function_case_transform_marker(transform) in represented:
            continue
        issues.append(
            {
                "source_alias": source_alias,
                "function_case_key": str(case.get("key") or "").strip(),
                "function_name": str(case.get("function_name") or "").strip(),
                "issue": "selected_function_case_not_bound_to_metric_source",
            }
        )
    if not issues:
        return []
    return [
        {
            "type": "function_case_metric_lineage_unresolved",
            "message": "선택된 Function Case를 결정론적 metric source 실행 계약에 연결할 수 없습니다.",
            "issues": issues,
        }
    ]


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
    seen_sources: set[tuple[str, str, str, str]] = set()
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
            aggregation = str(contract.get("aggregation") or "sum").strip().lower()
            output_column = _metric_output_column(
                source_column,
                raw_metrics,
                explicit_output=str(contract.get("output_column") or "").strip(),
                aliases=_string_list(contract.get("metric_aliases")),
                used_outputs=used_outputs,
            )
            if not output_column:
                continue
            marker = (
                alias.casefold(),
                _normalized_column_key(source_column),
                aggregation,
                _normalized_column_key(output_column),
            )
            if marker in seen_sources:
                continue
            seen_sources.add(marker)
            used_outputs.add(_normalized_column_key(output_column))
            result.append(
                {
                    "source_alias": alias,
                    "dataset_key": dataset_key,
                    "source_column": source_column,
                    "aggregation": aggregation,
                    "output_column": output_column,
                    "fill_value": "" if aggregation == "collect_unique" else 0,
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
    *,
    candidates: dict[str, Any] | None = None,
    dataset_key: str = "",
) -> list[dict[str, Any]]:
    """Extract aggregate outputs whose input lineage includes one source.

    ``source_alias`` normally appears directly on the aggregate step.  A
    follow-up enrichment can instead filter and join that source with
    ``previous_result`` first, so the aggregate's alias is the joined frame.
    In that case the optional catalog guard keeps only metrics actually owned
    by the requested right-hand dataset.  This is a general lineage rule, not
    a rule for a particular product, process, or HOLD analysis.
    """
    result: list[dict[str, Any]] = []
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        direct_alias = str(step.get("source_alias") or "").strip()
        if direct_alias != source_alias:
            lineage_aliases = _step_external_source_aliases(
                step,
                pandas_plan,
                {source_alias},
            )
            if source_alias not in lineage_aliases:
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
                if candidates is not None and dataset_key and not _catalog_supports_domain_column(
                    candidates,
                    dataset_key,
                    source_column,
                ):
                    # A joined frame can contain measures from either side.
                    # For the deterministic right-side enrichment contract,
                    # accept only a column declared by that right catalog.
                    continue
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


# 함수 설명: LLM이 작성한 출력 계약도 pandas 계획과 같은 Table Catalog 실행 컬럼 계약으로 정규화합니다.
def _normalize_raw_output_contract_columns(
    raw_contract: Any,
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    resolved_grain_plan: dict[str, Any] | None = None,
    resolved_join_plan: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw_contract, dict):
        return {}, {"status": "not_needed", "change_count": 0, "changes": []}
    alias_maps = _pandas_column_alias_maps(
        candidates,
        retrieval_jobs,
        resolved_grain_plan or {},
        resolved_join_plan or [],
    )
    lineage_aliases = [
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
    ]
    if len(lineage_aliases) == 1:
        mapping = alias_maps.get(lineage_aliases[0], {})
        source_alias = lineage_aliases[0]
    else:
        mapping = _consensus_lineage_column_alias_map(alias_maps, lineage_aliases)
        source_alias = "__output_contract__"
    changes: list[dict[str, Any]] = []
    normalized = _normalize_pandas_plan_value(
        raw_contract,
        {source_alias: mapping},
        {"source_alias": source_alias},
        "output_contract",
        changes,
    )
    labels = normalized.get("column_labels")
    if isinstance(labels, dict):
        normalized_labels: dict[str, Any] = {}
        for raw_column, label in labels.items():
            column = _normalize_column_field_value(
                str(raw_column),
                mapping,
                source_alias,
                f"output_contract.column_labels.{raw_column}",
                changes,
            )
            normalized_labels.setdefault(str(column), label)
        normalized["column_labels"] = normalized_labels
    ordering = normalized.get("ordering")
    if isinstance(ordering, dict):
        for key in ("sort_by", "rank_by", "rank_column"):
            if key in ordering:
                ordering[key] = _normalize_column_field_value(
                    ordering[key],
                    mapping,
                    source_alias,
                    f"output_contract.ordering.{key}",
                    changes,
                )
    return normalized, {
        "status": "applied" if changes else "not_needed",
        "change_count": len(changes),
        "changes": changes,
    }


# 함수 설명: pandas 실행 계획의 컬럼 참조를 source별 metadata 계약에 등록된 실제 물리 컬럼명으로 정규화합니다.
# 함수 설명: `_apply_selected_domain_execution_contracts()`는 모델이 만든 미등록 metric과 grain을 선택된 Domain과 Table Catalog 계약으로 교정합니다.
def _apply_selected_domain_execution_contracts(
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    resolved_grain_plan: dict[str, Any],
    locked_metadata_refs: list[dict[str, str]],
) -> tuple[list[Any], dict[str, Any]]:
    """Repair unsupported model columns only from selected Domain/Catalog contracts."""
    contracts_by_dataset: dict[str, list[dict[str, Any]]] = {}
    for reference in locked_metadata_refs:
        item = _find_metadata_item(candidates, reference)
        payload = _metadata_payload(item)
        metrics = _string_list(payload.get("metric_columns"))
        if not metrics:
            metrics = _string_list(payload.get("column"))
        datasets = _merge_strings(
            _string_list(payload.get("data_source")),
            _string_list(payload.get("dataset_key")),
        )
        if not metrics or not datasets:
            continue
        for dataset_key in datasets:
            contracts_by_dataset.setdefault(dataset_key, []).append(
                {
                    "metadata_ref": deepcopy(reference),
                    "metrics": metrics,
                    "aggregation_method": str(
                        payload.get("aggregation_method") or ""
                    ).strip(),
                }
            )

    normalized = deepcopy(pandas_plan)
    changes: list[dict[str, Any]] = []
    known_aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict)
    }
    entity_columns = _string_list(resolved_grain_plan.get("grain_columns"))
    for index, step in enumerate(normalized):
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation not in {
            "groupby_and_aggregate",
            "group_by_and_aggregate",
            "aggregate",
        }:
            continue
        if not _step_uses_raw_catalog_columns(step, normalized, known_aliases):
            continue
        lineage = _step_external_source_aliases(step, normalized, known_aliases)
        if len(lineage) != 1:
            continue
        source_alias = lineage[0]
        dataset_key = _dataset_key_for_alias(source_alias, retrieval_jobs)
        if not dataset_key:
            continue

        group_key = next(
            (
                key
                for key in ("group_by", "group_by_columns", "group_columns", "group_cols")
                if key in step
            ),
            "",
        )
        current_group = _string_list(step.get(group_key)) if group_key else []
        unsupported_group = [
            column
            for column in current_group
            if not _catalog_supports_domain_column(candidates, dataset_key, column)
        ]
        if (
            group_key
            and len(current_group) >= 2
            and unsupported_group
            and entity_columns
            and all(
                _catalog_supports_domain_column(candidates, dataset_key, column)
                for column in entity_columns
            )
        ):
            step[group_key] = deepcopy(entity_columns)
            changes.append(
                {
                    "step_index": index,
                    "source_alias": source_alias,
                    "kind": "grain",
                    "removed_columns": unsupported_group,
                    "replacement_columns": deepcopy(entity_columns),
                }
            )

        aggregations = step.get("aggregations")
        if not isinstance(aggregations, list):
            continue
        unsupported = [
            aggregation
            for aggregation in aggregations
            if isinstance(aggregation, dict)
            and str(aggregation.get("column") or aggregation.get("agg_column") or "").strip()
            and not _catalog_supports_domain_column(
                candidates,
                dataset_key,
                str(aggregation.get("column") or aggregation.get("agg_column") or "").strip(),
            )
        ]
        contracts = contracts_by_dataset.get(dataset_key, [])
        if not unsupported or not contracts:
            continue
        registered_metrics = _merge_strings(
            *[contract.get("metrics", []) for contract in contracts]
        )
        if not registered_metrics or not all(
            _catalog_supports_domain_column(candidates, dataset_key, metric)
            for metric in registered_metrics
        ):
            continue
        retained = [aggregation for aggregation in aggregations if aggregation not in unsupported]
        existing_outputs = {
            _normalized_column_key(
                str(item.get("output_column") or item.get("result_column") or "")
            )
            for item in retained
            if isinstance(item, dict)
        }
        catalog_payload = _metadata_payload(_table_catalog_item(candidates, dataset_key))
        semantics = (
            catalog_payload.get("metric_semantics")
            if isinstance(catalog_payload.get("metric_semantics"), dict)
            else {}
        )
        fallback_method = next(
            (
                str(contract.get("aggregation_method") or "").strip()
                for contract in contracts
                if str(contract.get("aggregation_method") or "").strip()
            ),
            "sum",
        )
        replacements: list[dict[str, Any]] = []
        for metric in registered_metrics:
            if _normalized_column_key(metric) in existing_outputs:
                continue
            semantic = semantics.get(metric) if isinstance(semantics.get(metric), dict) else {}
            method = str(semantic.get("default_rollup") or fallback_method or "sum").strip()
            replacements.append(
                {
                    "column": metric,
                    "method": method,
                    "output_column": metric,
                }
            )
        step["aggregations"] = [*retained, *replacements]
        changes.append(
            {
                "step_index": index,
                "source_alias": source_alias,
                "kind": "metrics",
                "removed_columns": [
                    str(item.get("column") or item.get("agg_column") or "")
                    for item in unsupported
                ],
                "replacement_columns": registered_metrics,
                "metadata_refs": [contract.get("metadata_ref") for contract in contracts],
            }
        )
    return normalized, {
        "status": "applied" if changes else "not_needed",
        "changes": changes,
    }


# 함수 설명: metadata가 확정한 entity grain과 실제 원본 source 집계의 group_by를 실행 전에 일치시킵니다.
def _align_aggregate_steps_with_resolved_grain(
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    resolved_grain_plan: dict[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Complete partially emitted entity grains only from explicit Catalog contracts."""
    normalized = deepcopy(pandas_plan)
    entity_columns = _string_list(resolved_grain_plan.get("grain_columns"))
    if not entity_columns:
        return normalized, {"status": "not_needed", "changes": [], "unresolved": []}

    known_aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict)
    }
    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for index, step in enumerate(normalized):
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation not in {
            "groupby_and_aggregate",
            "group_by_and_aggregate",
            "aggregate",
        }:
            continue
        group_key = next(
            (
                key
                for key in ("group_by", "group_by_columns", "group_columns", "group_cols")
                if key in step
            ),
            "",
        )
        current_group = _string_list(step.get(group_key)) if group_key else []
        if not group_key or not current_group:
            continue
        if not _step_uses_raw_catalog_columns(step, normalized, known_aliases):
            continue
        lineage = _step_external_source_aliases(step, normalized, known_aliases)
        if len(lineage) != 1:
            continue
        source_alias = lineage[0]
        dataset_key = _dataset_key_for_alias(source_alias, retrieval_jobs)
        if not dataset_key:
            continue

        entity_identities = {
            _grain_column_identity(
                column,
                [source_alias],
                candidates,
                retrieval_jobs,
            )
            for column in entity_columns
        }
        current_identities = {
            _grain_column_identity(
                column,
                [source_alias],
                candidates,
                retrieval_jobs,
            )
            for column in current_group
        }
        # A selected entity contract is authoritative only when this aggregate
        # already groups by at least one of its keys. This avoids expanding an
        # unrelated process-only or scalar aggregation.
        if not entity_identities.intersection(current_identities):
            continue
        missing_columns = [
            column
            for column in entity_columns
            if _grain_column_identity(
                column,
                [source_alias],
                candidates,
                retrieval_jobs,
            )
            not in current_identities
        ]
        if not missing_columns:
            continue
        unsupported = [
            column
            for column in missing_columns
            if not _explicit_catalog_column_contract(candidates, dataset_key, column)
        ]
        if unsupported:
            unresolved.append(
                {
                    "step_index": index,
                    "node_id": str(step.get("node_id") or "").strip(),
                    "source_alias": source_alias,
                    "dataset_key": dataset_key,
                    "missing_columns": missing_columns,
                    "unsupported_columns": unsupported,
                }
            )
            continue

        breakdown_columns = [
            column
            for column in current_group
            if _grain_column_identity(
                column,
                [source_alias],
                candidates,
                retrieval_jobs,
            )
            not in entity_identities
        ]
        step[group_key] = _merge_strings(breakdown_columns, entity_columns)
        changes.append(
            {
                "step_index": index,
                "node_id": str(step.get("node_id") or "").strip(),
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "added_columns": missing_columns,
                "group_by": deepcopy(step[group_key]),
                "selection_source": "resolved_grain_plan+table_catalog",
            }
        )

    status = "applied" if changes else "unresolved" if unresolved else "not_needed"
    return normalized, {
        "status": status,
        "changes": changes,
        "unresolved": unresolved,
    }


# 함수 설명: `_pandas_catalog_column_validation_errors()`는 집계 계획에 남은 미등록 source 컬럼을 실행 전에 검증 오류로 변환합니다.
def _pandas_catalog_column_validation_errors(
    pandas_plan: list[Any],
    candidates: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reject remaining aggregate inputs that no selected Catalog can provide."""
    known_aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in retrieval_jobs
        if isinstance(job, dict)
    }
    issues: list[dict[str, Any]] = []
    for index, step in enumerate(pandas_plan):
        if not isinstance(step, dict):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation not in {
            "groupby_and_aggregate",
            "group_by_and_aggregate",
            "aggregate",
        }:
            continue
        if not _step_uses_raw_catalog_columns(step, pandas_plan, known_aliases):
            continue
        lineage = _step_external_source_aliases(step, pandas_plan, known_aliases)
        if len(lineage) != 1:
            continue
        source_alias = lineage[0]
        dataset_key = _dataset_key_for_alias(source_alias, retrieval_jobs)
        if not dataset_key:
            continue
        columns = _merge_strings(
            _string_list(
                step.get("group_by")
                or step.get("group_by_columns")
                or step.get("group_columns")
                or step.get("group_cols")
            ),
            [
                str(item.get("column") or item.get("agg_column") or "").strip()
                for item in step.get("aggregations", [])
                if isinstance(item, dict)
            ],
        )
        unsupported = [
            column
            for column in columns
            if column and not _catalog_supports_domain_column(candidates, dataset_key, column)
        ]
        if unsupported:
            issues.append(
                {
                    "step_index": index,
                    "source_alias": source_alias,
                    "dataset_key": dataset_key,
                    "columns": unsupported,
                }
            )
    if not issues:
        return []
    return [
        {
            "type": "pandas_plan_column_not_in_catalog",
            "message": "pandas execution plan references columns absent from the selected Table Catalog.",
            "issues": issues,
        }
    ]


# 함수 설명: `_step_uses_raw_catalog_columns()`는 집계 입력이 filter만 거친 원본 source인지 파생 결과인지 구분합니다.
def _step_uses_raw_catalog_columns(
    step: dict[str, Any],
    pandas_plan: list[Any],
    known_aliases: set[str],
    visited: set[str] | None = None,
) -> bool:
    """Allow Catalog checks through filters, but not through derived table nodes."""
    inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
    if not inputs:
        return str(step.get("source_alias") or "").strip() in known_aliases
    allowed_passthrough = {
        "apply_filters",
        "filter",
        "filter_rows",
        "apply_pandas_function_case",
        "apply_function_case",
    }
    seen = set(visited or set())
    by_reference: dict[str, dict[str, Any]] = {}
    for candidate in pandas_plan:
        if not isinstance(candidate, dict):
            continue
        for raw_reference in (
            candidate.get("node_id"),
            candidate.get("output_alias"),
        ):
            reference = str(raw_reference or "").strip()
            if reference:
                by_reference[reference] = candidate
    for item in inputs:
        if not isinstance(item, dict):
            return False
        kind = str(item.get("kind") or "").strip().lower()
        reference = str(item.get("ref") or "").strip()
        if kind == "external_source":
            if reference not in known_aliases:
                return False
            continue
        if kind != "node_output" or not reference or reference in seen:
            return False
        parent = by_reference.get(reference)
        if not isinstance(parent, dict):
            return False
        parent_operation = str(
            parent.get("operation") or parent.get("step") or ""
        ).strip().lower()
        if parent_operation not in allowed_passthrough:
            return False
        if not _step_uses_raw_catalog_columns(
            parent,
            pandas_plan,
            known_aliases,
            {*seen, reference},
        ):
            return False
    return True


# 함수 설명: `_reconcile_aggregate_output_contract()`는 교정된 grain과 metric에 맞춰 strict 결과 컬럼 계약을 다시 구성합니다.
def _reconcile_aggregate_output_contract(
    raw_contract: Any,
    pandas_plan: list[Any],
    resolved_grain_plan: dict[str, Any],
) -> dict[str, Any]:
    """Keep strict aggregate output columns aligned with the repaired plan."""
    contract = deepcopy(raw_contract) if isinstance(raw_contract, dict) else {}
    metrics = _aggregation_output_contract(
        {"pandas_execution_plan": pandas_plan}
    ).get("all", [])
    grains = _string_list(resolved_grain_plan.get("grain_columns"))
    if not metrics:
        return contract
    result_columns = _merge_strings(grains, metrics)
    contract["result_mode"] = "aggregate"
    contract["grain_columns"] = grains
    contract["metric_columns"] = metrics
    contract["required_columns"] = result_columns
    contract["result_columns"] = result_columns
    contract["primary_metric"] = metrics[0]
    contract["strict_result_columns"] = True
    labels = contract.get("column_labels")
    if isinstance(labels, dict):
        contract["column_labels"] = {
            key: value for key, value in labels.items() if key in result_columns
        }
    return contract


# 함수 설명: `_normalize_pandas_plan_columns()`는 pandas 실행 계획의 컬럼을 source별 canonical 컬럼으로 정규화합니다.
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
    known_source_aliases = set(alias_maps)
    for index, raw_step in enumerate(pandas_plan):
        if not isinstance(raw_step, dict):
            normalized.append(deepcopy(raw_step))
            continue
        lineage_aliases = _step_external_source_aliases(
            raw_step,
            pandas_plan,
            known_source_aliases,
        )
        step_alias_maps = alias_maps
        inherited_context: dict[str, str] = {}
        if len(lineage_aliases) == 1:
            inherited_context = {"source_alias": lineage_aliases[0]}
        elif len(lineage_aliases) > 1:
            consensus_mapping = _consensus_lineage_column_alias_map(
                alias_maps,
                lineage_aliases,
            )
            if consensus_mapping:
                lineage_context_alias = "__lineage__:" + "|".join(lineage_aliases)
                step_alias_maps = dict(alias_maps)
                step_alias_maps[lineage_context_alias] = consensus_mapping
                inherited_context = {"source_alias": lineage_context_alias}
        normalized.append(
            _normalize_pandas_plan_value(
                raw_step,
                step_alias_maps,
                inherited_context,
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
def _consensus_lineage_column_alias_map(
    alias_maps: dict[str, dict[str, str]],
    lineage_aliases: list[str],
) -> dict[str, str]:
    """Keep only physical-to-standard mappings that do not conflict across lineage."""
    targets_by_column: dict[str, set[str]] = {}
    display_targets: dict[str, str] = {}
    for alias in lineage_aliases:
        mapping = alias_maps.get(alias, {})
        for normalized_column, target in mapping.items():
            target_text = str(target or "").strip()
            if not normalized_column or not target_text:
                continue
            target_key = _normalized_column_key(target_text)
            if not target_key:
                continue
            targets_by_column.setdefault(normalized_column, set()).add(target_key)
            display_targets.setdefault(target_key, target_text)
    return {
        normalized_column: display_targets[next(iter(targets))]
        for normalized_column, targets in targets_by_column.items()
        if len(targets) == 1
    }


# 함수 설명: pandas 실행 계획의 중첩 값에서 alias source 문맥에 맞는 표준 컬럼명만 재귀적으로 확정합니다.
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
            if (
                context_key == "source_alias"
                and text not in alias_maps
                and context.get("source_alias") in alias_maps
            ):
                continue
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
                groups_by_alias.setdefault(grain_alias, []).append((canonical, [canonical, *physical]))

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
                    (canonical, [canonical, *left_candidates])
                )
            if right_alias and canonical and right_candidates:
                groups_by_alias.setdefault(right_alias, []).append(
                    (canonical, [canonical, *right_candidates])
                )

    return {
        alias: _unambiguous_column_alias_map(groups)
        for alias, groups in groups_by_alias.items()
        if groups
    }


# 함수 설명: Table Catalog의 filter_mappings만 실행용 canonical→source 컬럼 계약으로 사용합니다.
def _table_column_alias_groups(item: dict[str, Any]) -> list[tuple[str, list[str]]]:
    payload = _metadata_payload(item)
    declared_columns = _catalog_declared_columns(payload)
    declared_index = {
        _normalized_column_key(column): column
        for column in declared_columns
    }
    result: list[tuple[str, list[str]]] = []
    filter_mapping = payload.get("filter_mappings")
    if not isinstance(filter_mapping, dict):
        return result
    for standard, raw_aliases in filter_mapping.items():
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
        result.append((standard_name, [standard_name, physical, *aliases]))
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
            item = (
                item.get("column_name")
                or item.get("name")
                or item.get("column")
                or item.get("key")
            )
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
            raw_values = value if isinstance(value, (list, tuple, set)) else [value]
            mapped_values: list[str] = []
            for raw_value in raw_values:
                if isinstance(raw_value, dict):
                    raw_value = (
                        raw_value.get("column_name")
                        or raw_value.get("name")
                        or raw_value.get("column")
                        or raw_value.get("key")
                    )
                mapped_values = _merge_strings(mapped_values, _string_list(raw_value))
            result = _merge_strings(result, mapped_values)
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
def _auto_select_metadata_function_case(
    plan: dict[str, Any],
    retrieval_jobs: list[dict[str, Any]],
    metadata_candidates: dict[str, Any],
    question: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover an omitted helper selection from an explicit metadata contract.

    This generic fallback activates only when a registered Function Case
    declares token-oriented execution metadata and the question contains
    multiple structured tokens. Ordinary column filters remain untouched.
    """

    existing = plan.get("pandas_function_cases") or plan.get("pandas_function_case")
    if existing:
        return plan, {"status": "not_needed", "reason": "model_selected_case"}
    domain_items = metadata_candidates.get("domain_items") if isinstance(metadata_candidates, dict) else []
    if not isinstance(domain_items, list):
        domain_items = []
    # 01D always carries a bounded runtime helper registry even when the
    # matching Domain item was trimmed.  It is a trusted metadata contract,
    # not a question-specific fallback, so it remains eligible for recovery.
    runtime_helpers = (
        metadata_candidates.get("runtime_function_helpers")
        if isinstance(metadata_candidates, dict)
        else []
    )
    if not isinstance(runtime_helpers, list):
        runtime_helpers = []
    helper_items = [
        {
            "section": "pandas_function_cases",
            "key": helper.get("function_name"),
            "function_name": helper.get("function_name"),
            "payload": helper,
        }
        for helper in runtime_helpers
        if isinstance(helper, dict) and helper.get("selectable_for_intent") is True
    ]
    skipped_by_typed_filters: list[dict[str, Any]] = []
    for item in [*domain_items, *helper_items]:
        if not isinstance(item, dict) or str(item.get("section") or "").strip() != "pandas_function_cases":
            continue
        payload = _metadata_payload(item)
        helper_name = str(item.get("function_name") or payload.get("function_name") or "").strip()
        if not helper_name:
            continue
        searchable = json.dumps(
            {
                "description": payload.get("description"),
                "input_contract": payload.get("input_contract"),
                "output_contract": payload.get("output_contract"),
                "execution_context": payload.get("execution_context"),
                "selection_criteria": payload.get("selection_criteria"),
                "pseudocode_or_logic": payload.get("pseudocode_or_logic"),
            },
            ensure_ascii=False,
            default=str,
        ).casefold()
        if "token" not in searchable:
            continue
        source_alias = str(
            payload.get("source_alias")
            or (retrieval_jobs[0].get("source_alias") if retrieval_jobs else "")
            or (retrieval_jobs[0].get("dataset_key") if retrieval_jobs else "")
        ).strip()
        policy = payload.get("token_policy") if isinstance(payload.get("token_policy"), dict) else {}
        token_values = _extract_function_case_tokens(question, policy)
        value_tokens = _function_case_value_tokens(token_values, metadata_candidates)
        if helper_name == "match_product_tokens":
            source_job = next(
                (
                    job
                    for job in retrieval_jobs
                    if isinstance(job, dict)
                    and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
                    == source_alias
                ),
                None,
            )
            evidence = _product_token_filter_evidence(
                {
                    "function_name": helper_name,
                    "input_text": question,
                    "source_alias": source_alias,
                },
                source_job,
                metadata_candidates,
            )
            value_tokens = _string_list(evidence.get("structured_tokens"))
            uncovered_tokens = _string_list(evidence.get("uncovered_tokens"))
            if len(uncovered_tokens) < 2:
                skipped_by_typed_filters.append(
                    {
                        "function_name": helper_name,
                        "source_alias": source_alias,
                        "reason": str(evidence.get("reason") or "no_uncovered_structured_product_tokens"),
                        "structured_tokens": value_tokens,
                        "covered_tokens": _string_list(evidence.get("covered_tokens")),
                    }
                )
                continue
        elif len(value_tokens) < 2:
            continue
        # Preserve the user's product wording, including unpatterned values
        # such as package codes.  Query-control terms are supplied separately
        # from the typed output contract, so a metric word such as UPH cannot
        # accidentally become a product token inside the trusted helper.
        helper_arguments: dict[str, Any] = {}
        excluded_tokens = _function_case_control_tokens(plan)
        if excluded_tokens:
            helper_arguments["excluded_tokens"] = excluded_tokens
        case = {
            "section": "pandas_function_cases",
            "key": str(item.get("key") or payload.get("key") or "").strip(),
            "function_name": helper_name,
            "input_text": question,
            "source_alias": source_alias,
            "selection_source": "metadata_token_contract_fallback",
        }
        if helper_arguments:
            case["arguments"] = helper_arguments
        next_plan = deepcopy(plan)
        next_plan["pandas_function_cases"] = [case]
        return next_plan, {
            "status": "applied",
            "function_case_key": case["key"],
            "function_name": helper_name,
            "token_values": value_tokens,
            "source_alias": source_alias,
        }
    if skipped_by_typed_filters:
        return plan, {
            "status": "not_needed",
            "reason": "no_uncovered_structured_product_tokens",
            "skipped": skipped_by_typed_filters,
        }
    return plan, {"status": "not_needed", "reason": "no_matching_token_contract"}


# 함수 설명: Catalog 컬럼명과 겹치는 토큰을 제거해 실제 제품 값만 Function Case 후보로 남깁니다.
def _function_case_value_tokens(
    tokens: list[str],
    metadata_candidates: dict[str, Any],
) -> list[str]:
    """Remove catalog column identifiers from candidate value tokens.

    Generic token patterns also match names such as ``PKG_TYPE1``.  Those are
    comparison columns, not product values, and must not activate a
    product-token helper for an ordinary attribute-comparison question.  The
    exclusion set is derived from the bounded Table Catalog schema; when a
    schema is unavailable we keep the historical token fallback behavior.
    """

    catalog_columns: set[str] = set()
    items = (
        metadata_candidates.get("table_catalog_items")
        if isinstance(metadata_candidates, dict)
        else []
    )
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            payload = _metadata_payload(item)
            raw_columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []
            catalog_columns.update(
                _normalized_column_key(column)
                for column in raw_columns
                if str(column or "").strip()
            )
            canonical = payload.get("canonical_columns")
            if isinstance(canonical, dict):
                catalog_columns.update(
                    _normalized_column_key(column)
                    for column in canonical
                    if str(column or "").strip()
                )
    if not catalog_columns:
        return list(tokens)
    return [
        token
        for token in tokens
        if _normalized_column_key(token) not in catalog_columns
    ]


# Function description: derive non-product control words solely from the
# typed analysis plan.  This keeps natural product wording intact while the
# helper ignores output metrics such as UPH, QTY, or a derived total label.
def _function_case_control_tokens(plan: dict[str, Any]) -> list[str]:
    output = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    candidates: list[Any] = [
        *_string_list(output.get("metric_columns")),
        output.get("primary_metric"),
    ]
    for binding in (
        output.get("metric_bindings", [])
        if isinstance(output.get("metric_bindings"), list)
        else []
    ):
        if isinstance(binding, dict):
            candidates.extend(
                [binding.get("source_column"), binding.get("output_column")]
            )
    for step in (
        plan.get("pandas_execution_plan", [])
        if isinstance(plan.get("pandas_execution_plan"), list)
        else []
    ):
        if not isinstance(step, dict):
            continue
        candidates.extend([step.get("agg_column"), step.get("metric_column")])
        for aggregation in (
            step.get("aggregations", [])
            if isinstance(step.get("aggregations"), list)
            else []
        ):
            if isinstance(aggregation, dict):
                candidates.extend(
                    [aggregation.get("column"), aggregation.get("output_column")]
                )
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        key = _function_token(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


# 함수 설명: `_function_case_items()`는 질문 계획의 helper 선택을 정규화하고 Catalog 실행 계약을 붙입니다.
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
# Function description: remove a selected helper when one typed source filter
# already expresses its entire input literally.  This is a source-sufficiency
# rule, not a function or column-specific exception: helpers remain required
# whenever their input contains additional unconstrained tokens.
def _remove_source_filter_sufficient_function_cases(
    function_cases: list[dict[str, Any]],
    pandas_plan: list[Any],
    retrieval_jobs: list[dict[str, Any]],
    metadata_candidates: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]]:
    jobs_by_alias = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip(): job
        for job in retrieval_jobs
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
    }
    retained: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    removed_markers: set[tuple[str, str, str]] = set()
    aliases_with_single_removed_case: set[str] = set()
    case_counts: dict[str, int] = {}
    for case in function_cases:
        if not isinstance(case, dict):
            continue
        alias = str(case.get("source_alias") or "").strip()
        if alias:
            case_counts[alias] = case_counts.get(alias, 0) + 1
    for case in function_cases:
        if not isinstance(case, dict):
            continue
        alias = str(case.get("source_alias") or "").strip()
        input_text = str(case.get("input_text") or "").strip()
        job = jobs_by_alias.get(alias)
        matched_filter = _direct_filter_covering_function_input(
            job.get("filters") if isinstance(job, dict) else None,
            input_text,
        )
        product_evidence = _product_token_filter_evidence(
            case,
            job,
            metadata_candidates or {},
        )
        if not matched_filter and not product_evidence.get("removable"):
            retained.append(deepcopy(case))
            continue
        marker = (
            str(case.get("key") or "").strip(),
            str(case.get("function_name") or "").strip(),
            alias,
        )
        removed_markers.add(marker)
        if alias and case_counts.get(alias) == 1:
            aliases_with_single_removed_case.add(alias)
        removed.append(
            {
                "source_alias": alias,
                "function_case_key": marker[0],
                "function_name": marker[1],
                "filter_field": matched_filter
                or ", ".join(_string_list(product_evidence.get("filter_fields"))),
                "reason": (
                    "typed_source_filter_covers_entire_function_input"
                    if matched_filter
                    else str(product_evidence.get("reason") or "")
                ),
                "structured_tokens": _string_list(product_evidence.get("structured_tokens")),
                "covered_tokens": _string_list(product_evidence.get("covered_tokens")),
            }
        )

    if not removed:
        return function_cases, pandas_plan, {"status": "not_needed", "removed": []}

    normalized_plan: list[Any] = []
    for step in pandas_plan:
        if not isinstance(step, dict) or str(step.get("operation") or "").strip() != "apply_pandas_function_case":
            normalized_plan.append(deepcopy(step))
            continue
        step_key = str(step.get("function_case_key") or step.get("key") or "").strip()
        step_function = str(step.get("function_name") or "").strip()
        step_alias = str(step.get("source_alias") or "").strip()
        marker_match = bool(step_key or step_function) and any(
            (not step_key or step_key == marker_key)
            and (not step_function or step_function == marker_function)
            and (not step_alias or step_alias == marker_alias)
            for marker_key, marker_function, marker_alias in removed_markers
        )
        unambiguous_alias_match = (
            not step_key
            and not step_function
            and step_alias in aliases_with_single_removed_case
        )
        if marker_match or unambiguous_alias_match:
            continue
        normalized_plan.append(deepcopy(step))
    return retained, normalized_plan, {"status": "applied", "removed": removed}


# 함수 설명: 제품 token helper는 typed 조회 filter로 이미 표현되지 않은 구조화 제품 token이 남아 있을 때만 유지합니다.
# Function description: a product-token helper is useful only when its input
# contains a structured product token not already expressed by a typed source
# filter.  This is deliberately metadata/IR driven: it has no process,
# dataset, or product-code-specific branch.
def _product_token_filter_evidence(
    case: dict[str, Any],
    job: dict[str, Any] | None,
    metadata_candidates: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("function_name") or "").strip() != "match_product_tokens":
        return {"removable": False}
    policy = _function_case_token_policy(case, metadata_candidates)
    raw_tokens = _extract_function_case_tokens(case.get("input_text"), policy)
    structured_tokens = _function_case_value_tokens(raw_tokens, metadata_candidates)
    if not structured_tokens:
        return {
            "removable": True,
            "reason": "no_structured_product_token_evidence",
            "structured_tokens": [],
            "covered_tokens": [],
            "uncovered_tokens": [],
            "filter_fields": [],
        }

    filter_values = _typed_filter_value_keys(
        job.get("filters") if isinstance(job, dict) else None
    )
    covered_tokens: list[str] = []
    uncovered_tokens: list[str] = []
    filter_fields: list[str] = []
    for token in structured_tokens:
        key = _function_token(token)
        matching_fields = filter_values.get(key, [])
        if matching_fields:
            covered_tokens.append(token)
            filter_fields.extend(matching_fields)
        else:
            uncovered_tokens.append(token)
    return {
        "removable": not uncovered_tokens,
        "reason": (
            "typed_source_filters_cover_all_structured_product_tokens"
            if not uncovered_tokens
            else "uncovered_structured_product_tokens_remain"
        ),
        "structured_tokens": structured_tokens,
        "covered_tokens": covered_tokens,
        "uncovered_tokens": uncovered_tokens,
        "filter_fields": _merge_strings(filter_fields),
    }


# 함수 설명: typed 동등·prefix filter의 리터럴 값을 수집해 Helper token의 source filter 충족 여부를 판단합니다.
# Function description: collect literal values from typed equality/prefix
# filters.  Only source-side restrictive operators may cover a helper token.
def _typed_filter_value_keys(filters: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    # 함수 설명: 논리 filter 묶음을 재귀 순회하며 비교 가능한 리터럴 값만 수집합니다.
    def visit(mapping: Any) -> None:
        if not isinstance(mapping, dict):
            return
        for raw_field, condition in mapping.items():
            field = str(raw_field or "").strip()
            if field.casefold() in FILTER_LOGICAL_KEYS:
                nested_items = condition if isinstance(condition, list) else [condition]
                for item in nested_items:
                    visit(item)
                continue
            if not isinstance(condition, dict):
                continue
            operator = str(condition.get("operator") or "eq").strip().casefold()
            if operator not in {"eq", "in", "starts_with", "startswith"}:
                continue
            raw_values = condition.get("values")
            if raw_values is None:
                raw_values = condition.get("value")
            values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
            for value in values:
                key = _function_token(value)
                if not key:
                    continue
                fields = result.setdefault(key, [])
                if field and field not in fields:
                    fields.append(field)

    visit(filters)
    return result


# Function description: locate a typed equality/prefix condition that covers a
# complete helper input rather than merely one token within a larger input.
def _direct_filter_covering_function_input(filters: Any, input_text: str) -> str:
    target = _function_token(input_text)
    if not target or not isinstance(filters, dict):
        return ""
    for field, condition in filters.items():
        if str(field or "").strip().casefold() in FILTER_LOGICAL_KEYS:
            continue
        if not isinstance(condition, dict):
            continue
        operator = str(condition.get("operator") or "eq").strip().casefold()
        raw_values = condition.get("value")
        if raw_values is None:
            raw_values = condition.get("values")
        values = raw_values if isinstance(raw_values, (list, tuple, set)) else [raw_values]
        normalized_values = [_function_token(value) for value in values]
        if operator in {"eq", "starts_with", "startswith"} and len(normalized_values) == 1 and normalized_values[0] == target:
            return str(field)
        if operator == "in" and len(normalized_values) == 1 and normalized_values[0] == target:
            return str(field)
    return ""


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
    normalized_plan = deepcopy(pandas_plan)
    existing_steps = [step for step in normalized_plan if isinstance(step, dict) and str(step.get("operation") or "") == "apply_pandas_function_case"]
    used_case_indexes: set[int] = set()
    case_aliases = {
        str(case.get("source_alias") or "").strip()
        for case in function_cases
        if isinstance(case, dict) and str(case.get("source_alias") or "").strip()
    }
    for step in existing_steps:
        step_function = str(step.get("function_name") or "").strip()
        step_key = str(step.get("function_case_key") or step.get("key") or "").strip()
        step_input = str(step.get("input_text") or "")
        step_alias = str(step.get("source_alias") or "").strip()
        if step_alias not in case_aliases:
            external_aliases = _string_list(
                [
                    item.get("ref")
                    for item in step.get("inputs", [])
                    if isinstance(item, dict)
                    and str(item.get("kind") or "").strip() == "external_source"
                    and str(item.get("ref") or "").strip() in case_aliases
                ]
            )
            if len(external_aliases) == 1:
                step_alias = external_aliases[0]
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index, case in enumerate(function_cases):
            if index in used_case_indexes or not isinstance(case, dict):
                continue
            case_function = str(case.get("function_name") or "").strip()
            case_key = str(case.get("key") or "").strip()
            case_input = str(case.get("input_text") or "")
            case_alias = str(case.get("source_alias") or "").strip()
            if step_function and case_function != step_function:
                continue
            if step_key and case_key != step_key:
                continue
            if step_input and case_input != step_input:
                continue
            if step_alias and case_alias != step_alias:
                continue
            candidates.append((index, case))
        if len(candidates) != 1:
            continue
        case_index, case = candidates[0]
        used_case_indexes.add(case_index)
        for key, value in case.items():
            target_key = "function_case_key" if key == "key" else key
            if step.get(target_key) not in (None, "", [], {}):
                continue
            step[target_key] = deepcopy(value)
        if step_alias and not str(step.get("source_alias") or "").strip():
            step["source_alias"] = step_alias

    steps_to_add = []
    for index, case in enumerate(function_cases):
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
    return [*steps_to_add, *normalized_plan]


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
            repaired = _repair_common_json_syntax(text)
            if repaired != text:
                try:
                    parsed = json.loads(repaired)
                except Exception:
                    parsed = _partial_intent_plan(repaired)
            else:
                parsed = _partial_intent_plan(text)
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: `_repair_common_json_syntax()`는 모델 JSON에서 구조적으로
# 명백한 키 오타와 trailing comma만 보정합니다. 실행 코드나 임의 Python을
# 평가하지 않아 출력 토큰을 늘리는 재호출 없이 계약 검증으로 진행합니다.
def _repair_common_json_syntax(value: str) -> str:
    text = str(value or "")
    repaired = re.sub(
        r"([,{]\s*)-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:",
        r'\1"\2":',
        text,
    )
    repaired = re.sub(
        r"([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:",
        r'\1"\2":',
        repaired,
    )
    return re.sub(r",\s*([}\]])", r"\1", repaired)


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
