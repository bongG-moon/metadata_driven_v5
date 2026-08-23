# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 14B V2 단순 분석 계약 결정기
# 역할: 정규화된 intent와 실제 단일 source schema를 검증해 Fast/Complex/Blocked 경로를 결정합니다.
# 주요 입력: 조회·표준화가 완료된 페이로드
# 주요 출력: simple_analysis_contract가 추가된 페이로드
# 유지보수 포인트: 질문 원문, 공정명, 제품명, 특정 테이블 물리 컬럼을 분기 조건으로 사용하지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
import math
from time import perf_counter
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, IntInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = 1
BASIC_RECIPES = {
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
}
ADVANCED_RECIPES = {
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
SUPPORTED_RECIPES = BASIC_RECIPES | ADVANCED_RECIPES
AGGREGATE_RESULT_RECIPES = {
    "scalar_summary",
    "group_summary",
    "ranked_summary",
    "list_summary",
    "percent_of_total",
    "rank_within_group",
    "threshold_after_aggregate",
    "time_bucket_summary",
    "period_change",
    "running_total",
    "moving_aggregate",
    "percentile_summary",
}
SUPPORTED_AGGREGATIONS = {
    "sum",
    "mean",
    "median",
    "min",
    "max",
    "count",
    "nunique",
    "first",
    "last",
    "collect_unique",
}
SUPPORTED_FILTER_OPERATORS = {
    "eq",
    "in",
    "ne",
    "not_in",
    "gt",
    "ge",
    "lt",
    "le",
    "contains",
    "like",
    "starts_with",
    "ends_with",
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
    "or",
    "any",
}
FILTER_ONLY_OPERATIONS = {"apply_filters", "filter", "filter_rows"}
AGGREGATE_OPERATIONS = {"groupby_and_aggregate", "group_by_and_aggregate", "aggregate"}
SORT_OPERATIONS = {"sort", "sort_and_top_n", "top_n", "bottom_n"}
SELECT_OPERATIONS = {"select_columns", "project_columns", "projection"}
RECIPE_OPERATIONS = {
    "count_rows": "scalar_summary",
    "scalar_summary": "scalar_summary",
    "frequency_summary": "frequency_summary",
    "value_counts": "frequency_summary",
    "distinct_summary": "distinct_summary",
    "distinct_values": "distinct_summary",
    "list_summary": "list_summary",
    "existence_summary": "existence_summary",
    "quality_summary": "quality_summary",
    "latest_earliest": "latest_earliest",
    "percent_of_total": "percent_of_total",
    "rank_within_group": "rank_within_group",
    "threshold_after_aggregate": "threshold_after_aggregate",
    "filter_result": "threshold_after_aggregate",
    "time_bucket_summary": "time_bucket_summary",
    "period_change": "period_change",
    "running_total": "running_total",
    "moving_aggregate": "moving_aggregate",
    "percentile_summary": "percentile_summary",
    "pivot_summary": "pivot_summary",
    "pivot_table": "pivot_summary",
    "crosstab": "pivot_summary",
}
COMPLEX_OPERATIONS = {
    "join",
    "merge",
    "compare_presence",
    "compare_metrics",
    "compare_group_attributes",
    "find_duplicate_groups",
    "apply_row_match_groups",
    "apply_pandas_function_case",
    "derive_formula",
}
VALUELESS_FILTER_OPERATORS = {
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
}
DETERMINISTIC_PLAN_KEYS = (
    "resolved_empty_result_plan",
    "resolved_presence_comparison_plan",
    "resolved_metric_comparison_plan",
    "resolved_metric_merge_plan",
    "resolved_reference_join_plan",
)
DETERMINISTIC_OPERATIONS = {
    "empty_result",
    "compare_presence",
    "compare_metrics",
    "merge_metric_sources",
    "enrich_previous_result",
}
TYPED_DETERMINISTIC_OPERATIONS = {
    "apply_pandas_function_case",
    "apply_filters",
    "apply_row_match_groups",
    "select_columns",
    "groupby_and_aggregate",
    "sort_and_top_n",
    "derive_formula",
    "join",
}
TYPED_DETERMINISTIC_AGGREGATIONS = {
    "sum",
    "mean",
    "min",
    "max",
    "count",
    "nunique",
    "median",
    "first",
    "last",
    "collect_unique",
}
TYPED_DETERMINISTIC_JOIN_TYPES = {"inner", "left", "right", "outer"}
TYPED_FORMULA_OPERATORS = {"add", "subtract", "multiply", "divide"}
TYPED_FORMULA_NULL_POLICIES = {"zero", "propagate"}
TYPED_FORMULA_ZERO_DIVISION_POLICIES = {"zero", "null"}


# 함수 설명: Typed formula 컬럼명을 공백과 대소문자 차이 없이 비교할 표준 키로 변환합니다.
def _typed_formula_column_key(value: Any) -> str:
    return str(value or "").strip().casefold().replace(" ", "_")


# 함수 설명: `resolve_simple_analysis_contract()`는 여러 simple·분석·contract 후보와 우선순위를 검토해 실제 사용할 값을 확정합니다.
def resolve_simple_analysis_contract(
    payload_value: Any,
    fast_path_enabled: Any = True,
    detail_row_limit: Any = 5000,
    max_pivot_columns: Any = 50,
) -> dict[str, Any]:
    """Resolve a generic executable contract without using question keywords."""

    started = perf_counter()
    payload = _payload(payload_value)
    next_payload = payload
    plan = _dict(next_payload.get("intent_plan"))
    trace = next_payload.setdefault("trace", {}).setdefault("inspection", {})

    if _execution_blocked(next_payload):
        contract = _route_contract("blocked", "execution_gate_blocked")
        return _attach_contract(next_payload, contract, trace, started)

    # Clarification and trace-only explanation are terminal response modes,
    # not data-analysis recipes.  Respect only the normalized request-scope
    # contract here; do not infer this route from question keywords.  This
    # keeps every ordinary Fast/Typed/Complex data path byte-for-byte on its
    # existing branch while preventing a pandas model from manufacturing a
    # one-row "answer" DataFrame for a request that needs no calculation.
    terminal_contract = _terminal_response_contract(plan)
    if terminal_contract:
        return _attach_contract(next_payload, terminal_contract, trace, started)

    if not _bool(fast_path_enabled, True):
        contract = _route_contract("complex", "fast_path_disabled")
        return _attach_contract(next_payload, contract, trace, started)

    # The normalizer compiles a compact frame contract from Catalog metadata.
    # Re-check the same Typed DAG against the columns that actually reached
    # this node.  This is deliberately separate from the Fast path: it gates
    # only deterministic Typed execution, so an unsupported Complex plan
    # keeps its established LLM/Pandas fallback instead of becoming a broad
    # new block.
    typed_frame_runtime_trace = _runtime_typed_frame_contract(plan, next_payload)
    trace["typed_frame_runtime_contract"] = deepcopy(typed_frame_runtime_trace)

    # A weak join-only plan can still carry a complete independent-metric
    # contract: two retrieval sources, one source-owned metric per side, one
    # common aggregate grain, and an outer population policy.  Promote only
    # that proven shape to the existing aggregate-before-merge executor.  The
    # helper is deliberately runtime/schema based and leaves every
    # row-enrichment or ambiguous join on its established Typed/Complex path.
    plan, join_only_metric_trace = _rescue_join_only_metric_merge(plan, next_payload)
    if join_only_metric_trace.get("status") == "applied":
        trace["join_only_metric_merge_rescue"] = deepcopy(join_only_metric_trace)

    # The normalizer grounds aliases before retrieval, but a strict metric
    # merge contract can be derived or retained after that point.  Re-check it
    # against the source aliases that actually reached the executor.  This
    # prevents an LLM-only label from turning a valid Typed DAG into a runtime
    # "metric source not found" error.
    plan, metric_merge_runtime_trace = _reconcile_metric_merge_runtime_aliases(
        plan,
        next_payload,
    )
    trace["metric_merge_runtime_alias_reconciliation"] = deepcopy(
        metric_merge_runtime_trace
    )
    if metric_merge_runtime_trace.get("status") == "invalid":
        runtime_aliases = _runtime_retrieval_source_aliases(next_payload, plan)
        typed_contract = _typed_pandas_plan_execution_contract(
            next_payload,
            plan,
            runtime_aliases,
        )
        if typed_contract:
            eligibility = _dict(typed_contract.get("eligibility"))
            eligibility["reason_codes"] = _dedupe(
                _string_list(eligibility.get("reason_codes"))
                + ["invalid_metric_merge_contract_fallback"]
            )
            typed_contract["eligibility"] = eligibility
            typed_contract["fallback_from"] = "resolved_metric_merge_plan"
            typed_contract["fallback_reason"] = "runtime_metric_alias_contract_invalid"
            trace["metric_merge_runtime_alias_reconciliation"]["fallback"] = (
                "execute_typed_pandas_plan"
            )
            next_payload["intent_plan"] = plan
            return _attach_contract(next_payload, typed_contract, trace, started)

        # An unresolved strict merge contract must never win deterministic
        # execution selection.  Preserve the diagnostic trace, then let the
        # ordinary Complex path decide whether a separately valid LLM Pandas
        # plan exists rather than running a contract with an invented source.
        plan.pop("resolved_metric_merge_plan", None)
        plan.pop("resolved_metric_comparison_plan", None)
        trace["metric_merge_runtime_alias_reconciliation"]["fallback"] = (
            "ordinary_complex_path"
        )

    # A complete strict metric-merge contract is more specific than the raw
    # Typed DAG it was derived from.  Select it before generic multi-source
    # Typed execution so a model's accidental raw join cannot reintroduce
    # join-before-aggregate semantics.  Invalid contracts above still retain
    # the established Typed-DAG fallback.
    confirmed_metric_merge = _dict(plan.get("resolved_metric_merge_plan"))
    if (
        confirmed_metric_merge.get("strict") is True
        and str(confirmed_metric_merge.get("operation") or "").strip()
        == "merge_metric_sources"
    ):
        next_payload["intent_plan"] = plan
        contract = _route_contract("complex", "strict_metric_merge_contract")
        contract["deterministic_contract_key"] = "resolved_metric_merge_plan"
        contract["deterministic_operation"] = "merge_metric_sources"
        return _attach_contract(next_payload, contract, trace, started)

    plan["route_resolution"] = _intent_route_candidate(plan)
    next_payload["intent_plan"] = plan

    intent_ir = _dict(plan.get("intent_ir"))
    source_aliases = _string_list(intent_ir.get("route_source_aliases"))
    if not source_aliases:
        source_aliases = _external_source_aliases(next_payload)
    if len(source_aliases) != 1:
        if not source_aliases and _new_data_request_without_runtime_source(
            next_payload,
            plan,
        ):
            failure = {
                "type": "analysis_source_unresolved",
                "message": "신규 데이터 분석 요청에 사용할 실행 source를 확정하지 못했습니다.",
            }
            contract = _route_contract("blocked", "analysis_source_unresolved")
            contract["external_source_aliases"] = []
            contract["validation_errors"] = [deepcopy(failure)]
            _block_analysis_source_unresolved(next_payload, failure)
            return _attach_contract(next_payload, contract, trace, started)
        typed_contract = _typed_pandas_plan_execution_contract(
            next_payload,
            plan,
            source_aliases,
        )
        if typed_contract:
            return _attach_contract(next_payload, typed_contract, trace, started)
        reason = "no_external_source" if not source_aliases else "multiple_external_sources"
        contract = _route_contract("complex", reason)
        contract["external_source_aliases"] = source_aliases
        return _attach_contract(next_payload, contract, trace, started)

    source_alias = source_aliases[0]
    job = _job_for_alias(plan, source_alias)
    dataset_key = str(job.get("dataset_key") or "").strip()
    columns = _source_columns(next_payload, source_alias)
    if not columns:
        contract = _route_contract("blocked", "source_schema_missing")
        contract.update({"source_alias": source_alias, "dataset_key": dataset_key})
        _block_contract(next_payload, "Fast Path source schema를 확인할 수 없습니다.", source_alias)
        return _attach_contract(next_payload, contract, trace, started)

    steps = [deepcopy(item) for item in _list(plan.get("pandas_execution_plan")) if isinstance(item, dict)]
    operations = [_operation(item) for item in steps]
    if any(operation in COMPLEX_OPERATIONS for operation in operations):
        typed_contract = _typed_pandas_plan_execution_contract(
            next_payload,
            plan,
            source_aliases,
        )
        if typed_contract:
            return _attach_contract(next_payload, typed_contract, trace, started)
        contract = _route_contract("complex", "complex_operation_required")
        contract.update({"source_alias": source_alias, "dataset_key": dataset_key, "operations": operations})
        return _attach_contract(next_payload, contract, trace, started)
    unknown_operations = [
        operation
        for operation in operations
        if operation
        and operation not in FILTER_ONLY_OPERATIONS
        and operation not in AGGREGATE_OPERATIONS
        and operation not in SORT_OPERATIONS
        and operation not in SELECT_OPERATIONS
        and operation not in RECIPE_OPERATIONS
    ]
    if unknown_operations:
        contract = _route_contract("complex", "unsupported_operation")
        contract.update(
            {
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "operations": operations,
                "unsupported_operations": _dedupe(unknown_operations),
            }
        )
        return _attach_contract(next_payload, contract, trace, started)

    filters, filter_errors = _resolved_filters(next_payload, plan, job, source_alias, columns)
    if filter_errors:
        contract = _route_contract("blocked", "filter_contract_invalid")
        contract.update(
            {
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "operations": operations,
                "validation_errors": filter_errors,
            }
        )
        _block_contract(next_payload, "메인 필터와 source schema의 매핑을 확정할 수 없습니다.", source_alias, filter_errors)
        return _attach_contract(next_payload, contract, trace, started)

    output_contract = _dict(plan.get("output_contract"))
    recipe = _recipe(steps, output_contract)
    if not recipe or recipe not in SUPPORTED_RECIPES:
        contract = _route_contract("complex", "recipe_unresolved")
        contract.update({"source_alias": source_alias, "dataset_key": dataset_key, "operations": operations})
        return _attach_contract(next_payload, contract, trace, started)

    aggregation_step = _last_step(steps, AGGREGATE_OPERATIONS)
    recipe_step = _last_recipe_step(steps, recipe)
    group_by = _group_by(aggregation_step, output_contract)
    metrics = _metrics(aggregation_step, output_contract, source_alias)
    ordering = _ordering(steps, output_contract)
    projection = _projection(steps, output_contract)
    calculation = _calculation(recipe_step, aggregation_step, output_contract, max_pivot_columns)
    if not ordering:
        ordering = _calculation_ordering(calculation)
    result_columns = _result_columns(recipe, output_contract, projection, group_by, metrics, calculation)
    validation_errors = _validate_contract_parts(
        recipe,
        columns,
        group_by,
        metrics,
        ordering,
        projection,
        result_columns,
        calculation,
        detail_row_limit,
    )
    schema_error_types = {
        "missing_source_column",
        "missing_metric_source_column",
        "unresolved_result_column",
        "unresolved_ordering_column",
    }
    schema_errors = [
        item
        for item in validation_errors
        if str(item.get("type") or "") in schema_error_types
    ]
    if schema_errors:
        contract = _route_contract("blocked", "source_schema_contract_invalid")
        contract.update(
            {
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "recipe": recipe,
                "operations": operations,
                "validation_errors": validation_errors,
            }
        )
        _block_contract(
            next_payload,
            "필수 표준 컬럼이 실제 source schema에 없어 분석을 실행할 수 없습니다.",
            source_alias,
            schema_errors,
        )
        return _attach_contract(next_payload, contract, trace, started)
    if validation_errors:
        contract = _route_contract("complex", "fast_contract_incomplete")
        contract.update(
            {
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "recipe": recipe,
                "operations": operations,
                "validation_errors": validation_errors,
            }
        )
        return _attach_contract(next_payload, contract, trace, started)

    contract = {
        "version": CONTRACT_VERSION,
        "strict": True,
        "route": "fast",
        "operation": "execute_fast_path_recipe",
        "recipe": recipe,
        "source_alias": source_alias,
        "dataset_key": dataset_key,
        "filters": filters,
        "projection": projection,
        "group_by": group_by,
        "metrics": metrics,
        "post_filters": _post_filters(steps, recipe, columns, metrics),
        "ordering": ordering,
        "limit": _limit(steps, output_contract, detail_row_limit if recipe == "detail_query" else 0),
        "tie_policy": str(calculation.get("tie_policy") or "first_n"),
        "null_policy": {
            "dimensions": str(output_contract.get("null_group_policy") or "preserve_as_blank"),
            "metrics": str(output_contract.get("metric_null_policy") or "display_zero"),
        },
        "calculation": calculation,
        "result_columns": result_columns,
        "result_schema_mode": "derived_bounded" if recipe == "pivot_summary" else "fixed",
        "eligibility": {
            "eligible": True,
            "reason_codes": ["single_source", "supported_recipe", "schema_resolved", "filters_resolved"],
        },
    }
    return _attach_contract(next_payload, contract, trace, started)


# 함수 설명: `_recipe()`는 14B V2 단순 분석 계약 결정기 처리 중 recipe 관련 값을 계산·변환하는 내부 helper입니다.
def _recipe(steps: list[dict[str, Any]], output_contract: dict[str, Any]) -> str:
    explicit = str(output_contract.get("fast_path_recipe") or "").strip().lower()
    if explicit in SUPPORTED_RECIPES:
        return explicit
    operations = [_operation(step) for step in steps]
    for operation in reversed(operations):
        mapped = RECIPE_OPERATIONS.get(operation)
        if mapped:
            return mapped
    ordering = _ordering(steps, output_contract)
    if any(operation in AGGREGATE_OPERATIONS for operation in operations):
        aggregations = _list(_last_step(steps, AGGREGATE_OPERATIONS).get("aggregations"))
        methods = {str(_dict(item).get("method") or "").strip().lower() for item in aggregations}
        if "collect_unique" in methods:
            return "list_summary"
        if ordering and _limit(steps, output_contract, 0) > 0:
            return "ranked_summary"
        group_by = _group_by(_last_step(steps, AGGREGATE_OPERATIONS), output_contract)
        return "group_summary" if group_by else "scalar_summary"
    mode = str(output_contract.get("result_mode") or "").strip().lower()
    if mode == "scalar":
        return "scalar_summary"
    if mode in {"detail", "entity_list"}:
        if ordering and _limit(steps, output_contract, 0) > 0:
            return "ranked_summary"
        return "detail_query"
    return "detail_query" if not steps or all(op in FILTER_ONLY_OPERATIONS | SORT_OPERATIONS | SELECT_OPERATIONS for op in operations) else ""


# 함수 설명: `_validate_contract_parts()`는 contract·parts이 실행·저장 계약을 만족하는지 검사하고 위반 내용을 명시적으로 반환합니다.
def _validate_contract_parts(
    recipe: str,
    columns: list[str],
    group_by: list[str],
    metrics: list[dict[str, Any]],
    ordering: list[dict[str, Any]],
    projection: list[str],
    result_columns: list[str],
    calculation: dict[str, Any],
    detail_row_limit: Any,
) -> list[dict[str, Any]]:
    available = {column.casefold() for column in columns}
    errors: list[dict[str, Any]] = []
    for column in [*group_by, *projection]:
        if column.casefold() not in available:
            errors.append({"type": "missing_source_column", "column": column})
    for metric in metrics:
        source_column = str(metric.get("source_column") or "").strip()
        method = str(metric.get("aggregation") or "").strip().lower()
        if not source_column or source_column.casefold() not in available:
            errors.append({"type": "missing_metric_source_column", "column": source_column})
        if method not in SUPPORTED_AGGREGATIONS:
            errors.append({"type": "unsupported_aggregation", "aggregation": method})
    metric_outputs = {str(item.get("output_column") or "").casefold() for item in metrics}
    calculation_outputs = {
        value.casefold()
        for value in _dedupe(
            [
                *_string_list(calculation.get("output_column")),
                *_string_list(calculation.get("output_columns")),
                *_string_list(calculation.get("time_bucket_column")),
            ]
        )
    }
    produced_group_columns = {column.casefold() for column in group_by}
    if recipe == "time_bucket_summary":
        time_column = str(calculation.get("time_column") or "").strip().casefold()
        bucket_column = str(calculation.get("time_bucket_column") or "").strip().casefold()
        if time_column and bucket_column and time_column != bucket_column:
            produced_group_columns.discard(time_column)
            produced_group_columns.add(bucket_column)
    produced_result_columns = (
        produced_group_columns | metric_outputs | calculation_outputs
    )
    allowed_result_columns = (
        produced_result_columns
        if recipe in AGGREGATE_RESULT_RECIPES
        else available | metric_outputs | calculation_outputs
    )
    if recipe != "pivot_summary":
        for column in result_columns:
            if column.casefold() not in allowed_result_columns:
                error_type = (
                    "unproduced_result_column"
                    if recipe in AGGREGATE_RESULT_RECIPES
                    else "unresolved_result_column"
                )
                errors.append({"type": error_type, "column": column})
    for item in ordering:
        column = str(item.get("column") or "").strip()
        if column and column.casefold() not in allowed_result_columns:
            errors.append({"type": "unresolved_ordering_column", "column": column})
    scalar_row_count = recipe == "scalar_summary" and calculation.get("scalar_operation") == "count_rows"
    frequency_count = recipe == "frequency_summary" and bool(calculation.get("output_column"))
    if recipe not in {"detail_query", "distinct_summary", "quality_summary", "latest_earliest", "existence_summary"} and not metrics and not scalar_row_count and not frequency_count:
        errors.append({"type": "missing_metric_contract"})
    if not result_columns and recipe != "pivot_summary":
        errors.append({"type": "missing_result_columns"})
    if recipe in {"ranked_summary", "latest_earliest", "rank_within_group"} and not ordering:
        errors.append({"type": "missing_ordering"})
    errors.extend(_advanced_contract_errors(recipe, calculation, columns, metrics))
    if recipe == "detail_query" and _positive_int(detail_row_limit, 0) <= 0:
        errors.append({"type": "invalid_detail_row_limit"})
    return _dedupe_dicts(errors)


# 함수 설명: `_advanced_contract_errors()`는 14B V2 단순 분석 계약 결정기 처리 중 contract·오류 관련 값을 계산·변환하는 내부 helper입니다.
def _advanced_contract_errors(
    recipe: str,
    calculation: dict[str, Any],
    columns: list[str],
    metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if recipe not in ADVANCED_RECIPES and recipe != "quality_summary":
        return []
    available = {column.casefold() for column in columns}
    errors: list[dict[str, Any]] = []
    required: dict[str, list[str]] = {
        "percent_of_total": ["denominator_scope", "zero_division_policy", "output_column"],
        "rank_within_group": ["rank_method", "tie_policy", "output_column"],
        "threshold_after_aggregate": ["threshold_column", "threshold_operator", "threshold_value"],
        "time_bucket_summary": ["time_column", "time_bucket_column", "frequency", "closed", "label"],
        "period_change": ["time_column", "periods", "change_method", "zero_division_policy", "output_column"],
        "running_total": ["time_column", "output_column"],
        "moving_aggregate": ["time_column", "window", "min_periods", "output_column"],
        "percentile_summary": ["percentile", "percentile_method"],
        "pivot_summary": ["pivot_index", "pivot_columns", "pivot_values", "pivot_aggregation", "max_pivot_columns"],
        "quality_summary": ["quality_check", "quality_columns", "output_column"],
    }
    for field in required.get(recipe, []):
        value = calculation.get(field)
        if value in (None, "", []):
            errors.append({"type": "missing_calculation_contract", "field": field})
    for field in ("time_column",):
        value = str(calculation.get(field) or "").strip()
        if value and value.casefold() not in available:
            errors.append({"type": "missing_calculation_source_column", "field": field, "column": value})
    for field in ("partition_by", "pivot_index", "pivot_columns", "pivot_values"):
        for column in _string_list(calculation.get(field)):
            if column.casefold() not in available and column not in {str(item.get("output_column") or "") for item in metrics}:
                errors.append({"type": "missing_calculation_source_column", "field": field, "column": column})
    percentile = calculation.get("percentile")
    if percentile not in (None, ""):
        try:
            if not 0 <= float(percentile) <= 1:
                raise ValueError
        except Exception:
            errors.append({"type": "invalid_percentile", "value": percentile})
    if recipe == "quality_summary" and str(calculation.get("quality_check") or "").startswith("duplicate"):
        if str(calculation.get("duplicate_policy") or "") not in {"all_rows", "excess_rows"}:
            errors.append({"type": "missing_calculation_contract", "field": "duplicate_policy"})
    return errors


# 함수 설명: `_calculation()`는 14B V2 단순 분석 계약 결정기 처리 중 calculation 관련 값을 계산·변환하는 내부 helper입니다.
def _calculation(
    recipe_step: dict[str, Any],
    aggregation_step: dict[str, Any],
    output_contract: dict[str, Any],
    max_pivot_columns: Any,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for value in (
        output_contract.get("calculation"),
        aggregation_step.get("calculation"),
        recipe_step.get("calculation"),
    ):
        if isinstance(value, dict):
            merged.update(deepcopy(value))
    for key in (
        "partition_by",
        "order_by",
        "denominator_scope",
        "zero_division_policy",
        "rank_method",
        "tie_policy",
        "time_column",
        "time_bucket_column",
        "frequency",
        "timezone",
        "closed",
        "label",
        "periods",
        "change_method",
        "window",
        "min_periods",
        "percentile",
        "percentile_method",
        "output_column",
        "quality_check",
        "quality_columns",
        "duplicate_policy",
        "threshold_column",
        "threshold_operator",
        "threshold_value",
        "pivot_index",
        "pivot_columns",
        "pivot_values",
        "pivot_aggregation",
        "pivot_fill_value",
        "max_pivot_columns",
    ):
        if key in recipe_step:
            merged[key] = deepcopy(recipe_step[key])
    if "max_pivot_columns" not in merged:
        merged["max_pivot_columns"] = _positive_int(max_pivot_columns, 50)
    if _operation(recipe_step) == "count_rows":
        merged["scalar_operation"] = "count_rows"
    return merged


# 함수 설명: `_resolved_filters()`는 14B V2 단순 분석 계약 결정기 처리 중 필터 관련 값을 계산·변환하는 내부 helper입니다.
def _resolved_filters(
    payload: dict[str, Any],
    plan: dict[str, Any],
    job: dict[str, Any],
    source_alias: str,
    columns: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_conditions: list[tuple[str, Any]] = []
    for field, spec in _dict(job.get("filters")).items():
        raw_conditions.append((str(field), spec))
    effective = _dict(_dict(plan.get("condition_resolution")).get("effective_filters"))
    source_effective = _dict(effective.get(source_alias))
    if not source_effective:
        dataset_key = str(job.get("dataset_key") or "").strip()
        source_effective = _dict(effective.get(dataset_key))
    for field, spec in _dict(source_effective.get("filters")).items():
        raw_conditions.append((str(field), spec))

    retriever_applied_all, retriever_applied_fields = _retriever_filter_state(payload, source_alias)
    resolved: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for field, spec_value in raw_conditions:
        spec = _dict(spec_value) if isinstance(spec_value, dict) else {"operator": "eq", "value": spec_value}
        operator = _normalize_operator(spec.get("operator") or spec.get("op") or "eq")
        values = spec.get("values") if isinstance(spec.get("values"), list) else _as_list(spec.get("value"))
        canonical, issue = _canonical_field(field, job, columns)
        if issue:
            errors.append(issue)
            continue
        if operator not in SUPPORTED_FILTER_OPERATORS:
            errors.append({"type": "unsupported_filter_operator", "field": field, "operator": operator})
            continue
        if not values and operator not in VALUELESS_FILTER_OPERATORS:
            errors.append({"type": "missing_filter_value", "field": canonical, "operator": operator})
            continue
        resolved.append(
            {
                "canonical_field": canonical,
                "operator": operator,
                "typed_values": deepcopy(values),
                "value_type": _value_type(values),
                "execution_stage": (
                    "retrieval_pushdown"
                    if retriever_applied_all or canonical.casefold() in retriever_applied_fields
                    else "post_retrieval"
                ),
            }
        )
    return _dedupe_dicts(resolved), _dedupe_dicts(errors)


# 함수 설명: `_retriever_filter_state()`는 source result가 명시적으로 증명한 필터 pushdown만 재실행 대상에서 제외합니다.
def _retriever_filter_state(payload: dict[str, Any], source_alias: str) -> tuple[bool, set[str]]:
    source_result = next(
        (
            item
            for item in _list(payload.get("source_results"))
            if isinstance(item, dict)
            and str(item.get("source_alias") or item.get("dataset_key") or "").strip() == source_alias
        ),
        {},
    )
    execution = _dict(source_result.get("source_execution"))
    state = execution.get("filters_applied_in_retriever")
    if state is True:
        return True, set()
    fields: list[str] = []
    if isinstance(state, dict):
        fields.extend(str(key).strip() for key in state if str(key).strip())
    elif isinstance(state, list):
        for item in state:
            if isinstance(item, dict):
                field = str(item.get("canonical_field") or item.get("field") or item.get("column") or "").strip()
                if field:
                    fields.append(field)
            elif str(item or "").strip():
                fields.append(str(item).strip())
    fields.extend(_string_list(execution.get("applied_filter_fields")))
    return False, {field.casefold() for field in fields}


# 함수 설명: `_canonical_field()`는 14B V2 단순 분석 계약 결정기 처리 중 field 관련 값을 계산·변환하는 내부 helper입니다.
def _canonical_field(field: str, job: dict[str, Any], columns: list[str]) -> tuple[str, dict[str, Any] | None]:
    target = str(field or "").strip()
    available = {column.casefold(): column for column in columns}
    if target.casefold() in available:
        return available[target.casefold()], None
    matches: list[str] = []
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        for canonical, aliases in _dict(job.get(mapping_name)).items():
            group = [str(canonical), *_string_list(aliases), *([str(aliases)] if not isinstance(aliases, list) else [])]
            if target.casefold() in {item.casefold() for item in group if item.strip()} and str(canonical).casefold() in available:
                matches.append(available[str(canonical).casefold()])
    matches = _dedupe(matches)
    if len(matches) == 1:
        return matches[0], None
    return "", {
        "type": "ambiguous_filter_mapping" if matches else "missing_filter_mapping",
        "field": target,
        "candidates": matches,
    }


# 함수 설명: `_metrics()`는 14B V2 단순 분석 계약 결정기 처리 중 metrics 관련 값을 계산·변환하는 내부 helper입니다.
def _metrics(step: dict[str, Any], output_contract: dict[str, Any], source_alias: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(step.get("aggregations")):
        if not isinstance(item, dict):
            continue
        source_column = str(item.get("column") or item.get("agg_column") or "").strip()
        output_column = str(item.get("output_column") or source_column).strip()
        method = str(item.get("method") or item.get("aggregation") or "").strip().lower()
        if method in {"avg", "average"}:
            method = "mean"
        if source_column and output_column and method:
            result.append({"source_column": source_column, "output_column": output_column, "aggregation": method})
    if result:
        return _dedupe_dicts(result)
    for item in _list(output_contract.get("metric_bindings")):
        if not isinstance(item, dict):
            continue
        binding_alias = str(item.get("source_alias") or "").strip()
        if binding_alias and binding_alias != source_alias:
            continue
        source_column = str(item.get("source_column") or "").strip()
        output_column = str(item.get("output_column") or source_column).strip()
        method = str(item.get("aggregation") or "").strip().lower()
        if method in {"avg", "average"}:
            method = "mean"
        if source_column and output_column and method:
            result.append({"source_column": source_column, "output_column": output_column, "aggregation": method})
    return _dedupe_dicts(result)


# 함수 설명: `_group_by()`는 14B V2 단순 분석 계약 결정기 처리 중 BY 관련 값을 계산·변환하는 내부 helper입니다.
def _group_by(step: dict[str, Any], output_contract: dict[str, Any]) -> list[str]:
    return _string_list(
        step.get("group_by")
        or step.get("group_by_columns")
        or step.get("group_columns")
        or output_contract.get("grain_columns")
    )


# 함수 설명: `_ordering()`는 14B V2 단순 분석 계약 결정기 처리 중 ordering 관련 값을 계산·변환하는 내부 helper입니다.
def _ordering(steps: list[dict[str, Any]], output_contract: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    raw_contract = output_contract.get("ordering")
    values = raw_contract if isinstance(raw_contract, list) else [raw_contract] if isinstance(raw_contract, dict) else []
    for item in values:
        column = str(_dict(item).get("column") or _dict(item).get("sort_by") or "").strip()
        direction = str(_dict(item).get("direction") or _dict(item).get("order") or "asc").strip().lower()
        if column:
            result.append({"column": column, "direction": "desc" if direction == "desc" else "asc"})
    for step in steps:
        if _operation(step) not in SORT_OPERATIONS:
            continue
        column = str(step.get("sort_by") or step.get("column") or "").strip()
        default_order = "desc" if _operation(step) in {"top_n", "sort_and_top_n"} else "asc"
        order = str(step.get("order") or default_order).strip().lower()
        if column:
            result.append({"column": column, "direction": "desc" if order == "desc" else "asc"})
    return _dedupe_dicts(result)


# 함수 설명: 고급 레시피의 calculation.order_by를 표준 정렬 컬럼과 방향 계약으로 변환합니다.
def _calculation_ordering(calculation: dict[str, Any]) -> list[dict[str, str]]:
    raw = calculation.get("order_by")
    values = raw if isinstance(raw, list) else [raw] if raw not in (None, "", {}) else []
    result: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, dict):
            column = str(item.get("column") or item.get("sort_by") or "").strip()
            direction = str(item.get("direction") or item.get("order") or "asc").strip().lower()
        else:
            column = str(item or "").strip()
            direction = "asc"
        if column:
            result.append({"column": column, "direction": "desc" if direction == "desc" else "asc"})
    return _dedupe_dicts(result)


# 함수 설명: `_projection()`는 14B V2 단순 분석 계약 결정기 처리 중 projection 관련 값을 계산·변환하는 내부 helper입니다.
def _projection(steps: list[dict[str, Any]], output_contract: dict[str, Any]) -> list[str]:
    for step in reversed(steps):
        if _operation(step) in SELECT_OPERATIONS:
            columns = _string_list(step.get("columns") or step.get("result_columns"))
            if columns:
                return columns
    return []


# 함수 설명: `_result_columns()`는 컬럼에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _result_columns(
    recipe: str,
    output_contract: dict[str, Any],
    projection: list[str],
    group_by: list[str],
    metrics: list[dict[str, Any]],
    calculation: dict[str, Any],
) -> list[str]:
    # A concrete select/projection step is the executable output shape for a
    # detail query.  Model-produced ``output_contract.result_columns`` can
    # contain catalog default columns that were never selected (for example a
    # HOLD detail request that only asks for LOT_ID and quantities).  Letting
    # those extras override the select step makes the Fast executor reject an
    # otherwise valid filtered frame at the final contract check.  Aggregate
    # recipes still keep the declared contract because their grain/metrics are
    # produced by the aggregation itself.
    recipe_hint = str(recipe or "").strip().lower()
    configured = _string_list(output_contract.get("result_columns"))
    if projection and recipe_hint in {"detail_query", "latest_earliest", "distinct_summary"}:
        metric_columns = [str(item.get("output_column") or "").strip() for item in metrics]
        derived = _string_list(calculation.get("output_columns"))
        return _dedupe([*projection, *metric_columns, *derived])
    if configured:
        return configured
    metric_columns = [str(item.get("output_column") or "").strip() for item in metrics]
    derived = _string_list(calculation.get("output_columns"))
    return _dedupe([*projection, *group_by, *metric_columns, *derived])


# 함수 설명: `_post_filters()`는 14B V2 단순 분석 계약 결정기 처리 중 필터 관련 값을 계산·변환하는 내부 helper입니다.
def _post_filters(
    steps: list[dict[str, Any]],
    recipe: str,
    columns: list[str],
    metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if recipe != "threshold_after_aggregate":
        return []
    step = _last_recipe_step(steps, recipe)
    calculation = _dict(step.get("calculation"))
    result = {
        "column": str(step.get("threshold_column") or calculation.get("threshold_column") or "").strip(),
        "operator": _normalize_operator(step.get("threshold_operator") or calculation.get("threshold_operator") or ""),
        "value": step.get("threshold_value", calculation.get("threshold_value")),
    }
    return [result] if result["column"] and result["operator"] else []


# 함수 설명: `_limit()`는 제한값이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _limit(steps: list[dict[str, Any]], output_contract: dict[str, Any], default: Any) -> int:
    candidates: list[Any] = []
    for step in steps:
        if _operation(step) in SORT_OPERATIONS:
            candidates.extend([step.get("limit"), step.get("n")])
    ordering = output_contract.get("ordering")
    if isinstance(ordering, dict):
        candidates.append(ordering.get("limit"))
    candidates.extend([output_contract.get("limit"), default])
    for value in candidates:
        number = _positive_int(value, 0)
        if number > 0:
            return number
    return 0


# 함수 설명: `_external_source_aliases()`는 14B V2 단순 분석 계약 결정기 처리 중 데이터 소스·aliases 관련 값을 계산·변환하는 내부 helper입니다.
def _external_source_aliases(payload: dict[str, Any]) -> list[str]:
    plan = _dict(payload.get("intent_plan"))
    graph = _dict(plan.get("resolved_execution_graph"))
    aliases = [
        str(item.get("source_alias") or "").strip()
        for item in _list(graph.get("external_source_requirements"))
        if isinstance(item, dict)
        and str(item.get("provider") or "").strip() in {"retrieval_job", "previous_source"}
        and str(item.get("source_alias") or "").strip()
    ]
    if not aliases:
        runtime_sources = _dict(payload.get("runtime_sources"))
        job_aliases = [
            str(item.get("source_alias") or item.get("dataset_key") or "").strip()
            for item in _list(plan.get("retrieval_jobs"))
            if isinstance(item, dict)
        ]
        aliases = [alias for alias in job_aliases if alias in runtime_sources]
    # A retrieval-free follow-up transform (for example, sort/top-N over the
    # immediately preceding result) has no retrieval job by design.  Its
    # restored ``previous_result`` frame is nevertheless a single, already
    # validated runtime source and can use the same deterministic Fast recipe.
    # Require all three facts below so this never turns an arbitrary state
    # value into an executable source: canonical reference mode, no new jobs,
    # and an execution-graph requirement backed by a restored runtime frame.
    if (
        not aliases
        and str(plan.get("reference_mode") or "").strip()
        == "previous_result_transform"
        and not _list(plan.get("retrieval_jobs"))
    ):
        runtime_sources = _dict(payload.get("runtime_sources"))
        previous_aliases = [
            str(item.get("source_alias") or "").strip()
            for item in _list(graph.get("external_source_requirements"))
            if isinstance(item, dict)
            and str(item.get("provider") or "").strip() == "previous_result"
            and str(item.get("source_alias") or "").strip() == "previous_result"
        ]
        aliases = [
            alias
            for alias in _dedupe(previous_aliases)
            if alias in runtime_sources and isinstance(runtime_sources.get(alias), list)
        ]
    return _dedupe(aliases)


# 함수 설명: `_source_columns()`는 컬럼 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_columns(payload: dict[str, Any], source_alias: str) -> list[str]:
    for item in _list(payload.get("source_results")):
        if not isinstance(item, dict):
            continue
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias == source_alias:
            columns = _string_list(item.get("columns"))
            if columns:
                return columns
    rows = _dict(payload.get("runtime_sources")).get(source_alias)
    result: list[str] = []
    for row in rows[:20] if isinstance(rows, list) else []:
        if isinstance(row, dict):
            result.extend(str(key) for key in row)
    return _dedupe(result)


# 함수 설명: `_job_for_alias()`는 14B V2 단순 분석 계약 결정기 처리 중 대상·alias 관련 값을 계산·변환하는 내부 helper입니다.
def _job_for_alias(plan: dict[str, Any], alias: str) -> dict[str, Any]:
    for item in _list(plan.get("retrieval_jobs")):
        if not isinstance(item, dict):
            continue
        current = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if current == alias:
            return item
    return {}


# 함수 설명: `_block_contract()`는 14B V2 단순 분석 계약 결정기 처리 중 contract 관련 값을 계산·변환하는 내부 helper입니다.
def _block_contract(
    payload: dict[str, Any],
    message: str,
    source_alias: str,
    issues: list[dict[str, Any]] | None = None,
) -> None:
    failure = {
        "type": "fast_path_contract_invalid",
        "message": message,
        "source_alias": source_alias,
        "issues": deepcopy(issues or []),
    }
    payload["execution_gate"] = {"status": "blocked", "reason": "fast_path_contract_invalid", "failures": [failure]}
    payload.setdefault("trace", {}).setdefault("errors", []).append(failure)


# 함수 설명: `_attach_contract()`는 14B V2 단순 분석 계약 결정기 처리 중 contract 관련 값을 계산·변환하는 내부 helper입니다.
def _attach_contract(
    payload: dict[str, Any],
    contract: dict[str, Any],
    inspection: dict[str, Any],
    started: float,
) -> dict[str, Any]:
    plan = _dict(payload.get("intent_plan"))
    execution_profile = _analysis_execution_profile(plan, contract)
    contract.update(execution_profile)
    payload["simple_analysis_contract"] = deepcopy(contract)
    route_resolution = _dict(plan.get("route_resolution"))
    route_resolution.update(
        {
            "final_route": str(contract.get("route") or "complex"),
            "final_recipe": str(contract.get("recipe") or ""),
            "final_reason_codes": deepcopy(
                _dict(contract.get("eligibility")).get("reason_codes") or []
            ),
            "analysis_execution_mode": str(contract.get("analysis_execution_mode") or ""),
            "requires_pandas_llm": bool(contract.get("requires_pandas_llm")),
            "resolved_after_retrieval": True,
        }
    )
    plan["route_resolution"] = route_resolution
    payload["intent_plan"] = plan
    inspection["fast_path"] = {
        "stage": "14b_simple_analysis_contract_resolver",
        "eligible": bool(_dict(contract.get("eligibility")).get("eligible")),
        "selected_route": str(contract.get("route") or "complex"),
        "recipe": str(contract.get("recipe") or ""),
        "reason_codes": deepcopy(_dict(contract.get("eligibility")).get("reason_codes") or []),
        "validation_errors": deepcopy(contract.get("validation_errors") or []),
        "analysis_execution_mode": str(contract.get("analysis_execution_mode") or ""),
        "requires_pandas_llm": bool(contract.get("requires_pandas_llm")),
        "deterministic_contract_key": str(contract.get("deterministic_contract_key") or ""),
        "deterministic_operation": str(contract.get("deterministic_operation") or ""),
        "llm_calls": {"intent": 1, "pandas_generation": 0, "repair": 0, "answer": 0},
        "timing_ms": {"route_resolution": round((perf_counter() - started) * 1000, 3)},
    }
    return payload


# 함수 설명: `_analysis_execution_profile()`는 execution·profile에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _analysis_execution_profile(
    plan: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the internal execution submode without changing Fast/Complex routing."""

    route = str(contract.get("route") or "complex").strip().lower()
    if route == "blocked":
        return {
            "analysis_execution_mode": "blocked",
            "requires_pandas_llm": False,
        }
    if str(contract.get("operation") or "").strip() == "complete_without_pandas":
        return {
            "analysis_execution_mode": "terminal_response",
            "requires_pandas_llm": False,
        }
    if route == "fast":
        return {
            "analysis_execution_mode": "deterministic_fast",
            "requires_pandas_llm": False,
        }
    if (
        contract.get("strict") is True
        and str(contract.get("operation") or "").strip()
        == "execute_typed_pandas_plan"
    ):
        return {
            "analysis_execution_mode": "deterministic_typed_plan",
            "requires_pandas_llm": False,
            "deterministic_operation": "execute_typed_pandas_plan",
        }
    for key in DETERMINISTIC_PLAN_KEYS:
        candidate = _dict(plan.get(key))
        operation = str(candidate.get("operation") or "").strip()
        if candidate.get("strict") is True and operation in DETERMINISTIC_OPERATIONS:
            return {
                "analysis_execution_mode": "deterministic_contract",
                "requires_pandas_llm": False,
                "deterministic_contract_key": key,
                "deterministic_operation": operation,
            }
    return {
        "analysis_execution_mode": "llm_pandas",
        "requires_pandas_llm": True,
    }


# 함수 설명: 정규화기가 명시적으로 확정한 비분석 응답만 pandas 없는 terminal 계약으로 변환합니다.
def _terminal_response_contract(plan: dict[str, Any]) -> dict[str, Any]:
    request_scope = str(plan.get("request_scope") or "").strip()
    reference_mode = str(plan.get("reference_mode") or "").strip()
    reuse_strategy = str(plan.get("reuse_strategy") or "").strip()
    # Mixed plans are not complete terminal contracts.  If a weak model emits
    # both clarification/explanation and executable jobs or steps, preserve
    # the established execution validation path instead of bypassing it.
    if _list(plan.get("retrieval_jobs")) or _list(
        plan.get("pandas_execution_plan")
    ):
        return {}
    terminal_kind = ""
    reason = ""
    if request_scope == "clarification":
        terminal_kind = "clarification"
        reason = "clarification_response"
    elif request_scope == "followup_explain" and (
        reference_mode == "previous_trace" or reuse_strategy == "trace_only"
    ):
        terminal_kind = "direct_answer"
        reason = "followup_explain_response"
    if not terminal_kind:
        return {}
    # Keep the public Fast/Complex/Blocked route enum unchanged.  Terminal is
    # an internal Complex submode so existing API builders and route labels do
    # not need a new externally visible value.
    contract = _route_contract("complex", reason)
    contract.update(
        {
            "operation": "complete_without_pandas",
            "terminal_kind": terminal_kind,
        }
    )
    return contract


# 함수 설명: 신규 데이터 요청이 실제 runtime source 없이 pandas 생성 경로로 흘러가는 경우만 식별합니다.
def _new_data_request_without_runtime_source(
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> bool:
    if str(plan.get("request_scope") or "new_analysis").strip() != "new_analysis":
        return False
    runtime_sources = _dict(payload.get("runtime_sources"))
    if runtime_sources:
        return False
    # Flow 01's normalized ``new_analysis`` scope is a data-analysis request.
    # Once explicit clarification and explanation modes have been separated,
    # an entirely empty plan is not a valid direct-answer contract; allowing
    # it through would recreate the constant-DataFrame success path.
    return True


# 함수 설명: source가 없는 신규 데이터 분석을 실행 오류로 위장하지 않고 조회 전 차단 계약으로 기록합니다.
def _block_analysis_source_unresolved(
    payload: dict[str, Any],
    failure: dict[str, Any],
) -> None:
    payload["execution_gate"] = {
        "status": "blocked",
        "reason": "analysis_source_unresolved",
        "failures": [deepcopy(failure)],
    }
    payload["answer_message"] = str(failure.get("message") or "")
    payload.setdefault("trace", {}).setdefault("errors", []).append(
        deepcopy(failure)
    )


# 함수 설명: `_intent_route_candidate()`는 조회 전 실행 계약만으로 Fast 후보 또는 Complex 필요 여부를 결정하며 최종 판정을 대신하지 않습니다.
def _runtime_retrieval_source_aliases(
    payload: dict[str, Any],
    plan: dict[str, Any],
) -> list[str]:
    """Return retrieval-job aliases that are present in the executor input."""

    runtime_sources = _dict(payload.get("runtime_sources"))
    aliases: list[str] = []
    for job in _list(plan.get("retrieval_jobs")):
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias and alias in runtime_sources:
            aliases.append(alias)
    return _dedupe(aliases)


# 함수 설명: 완전한 join-only 독립 지표 계약만 기존 source별 집계 후 병합 계약으로 승격합니다.
def _rescue_join_only_metric_merge(
    plan_value: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover a strict aggregate-before-outer-merge contract at runtime.

    This is intentionally narrower than general Typed joins.  It accepts one
    join node whose two inputs are the two retrieved sources, whose metrics are
    uniquely owned by opposite sides, and whose declared result contains only
    the shared grain plus those metrics.  Any enrichment signal, ambiguous
    column ownership, or incomplete aggregate contract returns the original
    plan unchanged so the existing executor behavior remains authoritative.
    """

    plan = deepcopy(plan_value)
    existing = _dict(plan.get("resolved_metric_merge_plan"))
    if (
        existing.get("strict") is True
        and str(existing.get("operation") or "").strip()
        == "merge_metric_sources"
    ):
        return plan, {"status": "not_applicable"}
    if _dict(plan.get("resolved_reference_join_plan")) or _list(
        plan.get("pandas_function_cases")
    ):
        return plan, {"status": "not_applicable"}

    steps = [
        item
        for item in _list(plan.get("pandas_execution_plan"))
        if isinstance(item, dict)
    ]
    if len(steps) != 1:
        return plan, {"status": "not_applicable"}
    step = steps[0]
    operation = _operation(step)
    if operation not in {"join", "merge", "outer_join"}:
        return plan, {"status": "not_applicable"}
    join_type = str(step.get("join_type") or step.get("how") or "").strip().lower()
    if operation == "outer_join" and not join_type:
        join_type = "outer"
    population_policy = str(step.get("population_policy") or "").strip().lower()
    if (
        join_type != "outer"
        or population_policy != "preserve_all_metric_source_keys"
    ):
        return plan, {"status": "not_applicable"}
    blank_policy = str(
        step.get("blank_policy") or step.get("null_key_policy") or ""
    ).strip()
    if blank_policy and blank_policy != "normalize_blank":
        return plan, {"status": "not_applicable"}

    inputs = [item for item in _list(step.get("inputs")) if isinstance(item, dict)]
    if (
        len(inputs) != 2
        or any(str(item.get("kind") or "").strip() != "external_source" for item in inputs)
    ):
        return plan, {"status": "not_applicable"}
    source_aliases = [str(item.get("ref") or "").strip() for item in inputs]
    if not all(source_aliases) or len(set(source_aliases)) != 2:
        return plan, {"status": "not_applicable"}
    declared_left = str(step.get("left_source_alias") or "").strip()
    declared_right = str(step.get("right_source_alias") or "").strip()
    if (declared_left and declared_left != source_aliases[0]) or (
        declared_right and declared_right != source_aliases[1]
    ):
        return plan, {"status": "not_applicable"}
    if set(_runtime_retrieval_source_aliases(payload, plan)) != set(source_aliases):
        return plan, {"status": "not_applicable"}

    # These fields express row enrichment or another semantic operation.  Do
    # not erase them while rewriting the topology of the plan.
    if any(
        _string_list(step.get(key))
        for key in (
            "left_value_columns",
            "right_value_columns",
            "comparison_columns",
            "projection",
            "columns",
        )
    ) or _list(step.get("aggregations")) or _dict(step.get("calculation")):
        return plan, {"status": "not_applicable"}
    if str(step.get("multi_match_policy") or "").strip():
        return plan, {"status": "not_applicable"}
    for join_contract in _list(plan.get("join_plan")):
        if not isinstance(join_contract, dict):
            continue
        if _string_list(join_contract.get("left_value_columns")) or _string_list(
            join_contract.get("right_value_columns")
        ) or str(join_contract.get("multi_match_policy") or "").strip():
            return plan, {"status": "not_applicable"}

    group_by = _string_list(step.get("group_by"))
    group_keys = [_metric_merge_column_key(value) for value in group_by]
    if not group_by or any(not value for value in group_keys) or len(set(group_keys)) != len(group_keys):
        return plan, {"status": "not_applicable"}
    # If the model redundantly supplied ordinary join keys, they must describe
    # exactly the same grain.  A disagreement is not repaired here.
    common_keys = _string_list(step.get("on") or step.get("join_keys"))
    left_keys = _string_list(step.get("left_on"))
    right_keys = _string_list(step.get("right_on"))
    for declared_keys in (common_keys, left_keys, right_keys):
        if declared_keys and [
            _metric_merge_column_key(value) for value in declared_keys
        ] != group_keys:
            return plan, {"status": "not_applicable"}

    output_contract = _dict(plan.get("output_contract"))
    if (
        str(output_contract.get("result_mode") or "").strip().lower() != "aggregate"
        or output_contract.get("strict_result_columns") is not True
    ):
        return plan, {"status": "not_applicable"}
    declared_grain = _string_list(output_contract.get("grain_columns"))
    if declared_grain and [
        _metric_merge_column_key(value) for value in declared_grain
    ] != group_keys:
        return plan, {"status": "not_applicable"}

    jobs_by_alias: dict[str, dict[str, Any]] = {}
    for raw_job in _list(plan.get("retrieval_jobs")):
        if not isinstance(raw_job, dict):
            continue
        alias = str(raw_job.get("source_alias") or raw_job.get("dataset_key") or "").strip()
        if not alias:
            continue
        if alias in jobs_by_alias:
            return plan, {"status": "not_applicable"}
        jobs_by_alias[alias] = raw_job
    if set(jobs_by_alias) != set(source_aliases):
        return plan, {"status": "not_applicable"}

    columns_by_alias = {
        alias: _metric_merge_runtime_columns(payload, alias) for alias in source_aliases
    }
    if any(not columns_by_alias[alias] for alias in source_aliases):
        return plan, {"status": "not_applicable"}

    grain_mappings: list[dict[str, Any]] = []
    for output_column in group_by:
        source_candidates: dict[str, list[str]] = {}
        for alias in source_aliases:
            actual = _unique_metric_merge_runtime_column(
                output_column,
                jobs_by_alias[alias],
                columns_by_alias[alias],
            )
            if not actual:
                return plan, {"status": "not_applicable"}
            source_candidates[alias] = [actual]
        grain_mappings.append(
            {
                "canonical_column": output_column,
                "output_column": output_column,
                "source_candidates": source_candidates,
            }
        )

    raw_metrics = [
        str(step.get("left_metric_column") or "").strip(),
        str(step.get("right_metric_column") or "").strip(),
    ]
    if not all(raw_metrics) or len({_metric_merge_column_key(value) for value in raw_metrics}) != 2:
        return plan, {"status": "not_applicable"}
    if any(_metric_merge_column_key(value) in set(group_keys) for value in raw_metrics):
        return plan, {"status": "not_applicable"}

    raw_bindings = [
        item
        for item in _list(output_contract.get("metric_bindings"))
        if isinstance(item, dict)
    ]
    if raw_bindings and len(raw_bindings) != 2:
        return plan, {"status": "not_applicable"}
    metrics: list[dict[str, Any]] = []
    aggregation_sources: list[str] = []
    for index, (alias, requested_metric) in enumerate(
        zip(source_aliases, raw_metrics),
        start=1,
    ):
        opposite_alias = source_aliases[1] if index == 1 else source_aliases[0]
        binding_matches = [
            item
            for item in raw_bindings
            if str(item.get("source_alias") or "").strip() == alias
            and _metric_merge_column_key(requested_metric)
            in {
                _metric_merge_column_key(item.get("source_column")),
                _metric_merge_column_key(item.get("output_column")),
            }
        ]
        if raw_bindings and len(binding_matches) != 1:
            return plan, {"status": "not_applicable"}
        binding = binding_matches[0] if binding_matches else {}
        declared_dataset = str(binding.get("dataset_key") or "").strip()
        dataset_key = str(jobs_by_alias[alias].get("dataset_key") or "").strip()
        if declared_dataset and declared_dataset.casefold() != dataset_key.casefold():
            return plan, {"status": "not_applicable"}
        source_column_request = str(
            binding.get("source_column") or requested_metric
        ).strip()
        source_column = _unique_metric_merge_runtime_column(
            source_column_request,
            jobs_by_alias[alias],
            columns_by_alias[alias],
        )
        # The metric must be owned by exactly this source.  A column available
        # on both sides is not sufficient evidence for independent measures.
        opposite_column = _unique_metric_merge_runtime_column(
            source_column_request,
            jobs_by_alias[opposite_alias],
            columns_by_alias[opposite_alias],
        )
        if not source_column or opposite_column:
            return plan, {"status": "not_applicable"}

        output_column = str(binding.get("output_column") or requested_metric).strip()
        if not output_column:
            return plan, {"status": "not_applicable"}
        side = "left" if index == 1 else "right"
        binding_aggregation = _normalized_metric_merge_aggregation(
            binding.get("aggregation") or binding.get("aggregation_method")
        )
        step_aggregation = _normalized_metric_merge_aggregation(
            _join_only_metric_aggregation(step, side)
        )
        semantic_aggregation, semantic_status = (
            _join_only_metric_semantic_aggregation(
                jobs_by_alias[alias],
                source_column,
            )
        )
        if semantic_status == "invalid" or any(
            value and value not in SUPPORTED_AGGREGATIONS
            for value in (binding_aggregation, step_aggregation)
        ):
            return plan, {"status": "not_applicable"}
        aggregation_evidence = [
            ("output_contract_metric_binding", binding_aggregation),
            ("typed_join_explicit_aggregation", step_aggregation),
            ("trusted_retrieval_metric_semantics", semantic_aggregation),
        ]
        proven_aggregations = {
            value for _, value in aggregation_evidence if value
        }
        if len(proven_aggregations) != 1:
            return plan, {"status": "not_applicable"}
        aggregation = next(iter(proven_aggregations))
        aggregation_sources.append(
            "+".join(source for source, value in aggregation_evidence if value)
        )
        metrics.append(
            {
                "source_alias": alias,
                "dataset_key": dataset_key,
                "source_column": source_column,
                "source_candidates": [source_column],
                "aggregation": aggregation,
                "output_column": output_column,
                "fill_value": "" if aggregation == "collect_unique" else 0,
                "fill_on_absence": aggregation
                in {"sum", "count", "nunique", "collect_unique"},
            }
        )

    metric_outputs = [str(item.get("output_column") or "").strip() for item in metrics]
    metric_output_keys = [_metric_merge_column_key(value) for value in metric_outputs]
    if len(set(metric_output_keys)) != 2:
        return plan, {"status": "not_applicable"}
    declared_metrics = _string_list(output_contract.get("metric_columns"))
    if (
        len(declared_metrics) != 2
        or {_metric_merge_column_key(value) for value in declared_metrics}
        != set(metric_output_keys)
    ):
        return plan, {"status": "not_applicable"}
    allowed_output_keys = set(group_keys) | set(metric_output_keys)
    for field_name in ("required_columns", "result_columns"):
        declared_columns = _string_list(output_contract.get(field_name))
        declared_keys = {_metric_merge_column_key(value) for value in declared_columns}
        if declared_columns and (
            not (set(group_keys) | set(metric_output_keys)).issubset(declared_keys)
            or not declared_keys.issubset(allowed_output_keys)
        ):
            return plan, {"status": "not_applicable"}

    node_id = str(step.get("node_id") or "__step_1").strip()
    merge_plan = {
        "operation": "merge_metric_sources",
        "join_type": "outer",
        "population_policy": "preserve_all_metric_source_keys",
        "selection_source": "runtime_join_only_metric_rescue",
        "execution_shape": "output_contract_independent_metric_shape",
        "join_node_ids": [node_id],
        "source_transforms": [],
        "grain_mappings": grain_mappings,
        "metrics": metrics,
        "fill_zero_on_success": True,
        "strict": True,
    }
    plan["resolved_metric_merge_plan"] = merge_plan
    return plan, {
        "stage": "14b_simple_analysis_contract_resolver",
        "status": "applied",
        "policy": "source_local_aggregate_before_outer_metric_merge",
        "join_node_id": node_id,
        "source_aliases": source_aliases,
        "grain_columns": group_by,
        "metric_columns": metric_outputs,
        "aggregation_sources": aggregation_sources,
    }


# 함수 설명: join-only metric rescue가 사용할 실제 runtime 컬럼 목록을 source별로 가져옵니다.
def _metric_merge_runtime_columns(
    payload: dict[str, Any],
    source_alias: str,
) -> list[str]:
    rows = _dict(payload.get("runtime_sources")).get(source_alias)
    runtime_columns: list[str] = []
    for row in rows[:20] if isinstance(rows, list) else []:
        if isinstance(row, dict):
            runtime_columns.extend(str(key).strip() for key in row if str(key).strip())
    return _dedupe(runtime_columns) or _source_columns(payload, source_alias)


# 함수 설명: 요청 컬럼을 source runtime schema와 hydrated alias mapping에서 하나로만 확정합니다.
def _unique_metric_merge_runtime_column(
    requested: Any,
    job: dict[str, Any],
    columns: list[str],
) -> str:
    requested_key = _metric_merge_column_key(requested)
    if not requested_key:
        return ""
    candidate_keys = {requested_key}
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        for canonical, raw_aliases in _dict(job.get(mapping_name)).items():
            group = [str(canonical), *_string_list(raw_aliases)]
            group_keys = {
                _metric_merge_column_key(value) for value in group if str(value).strip()
            }
            if requested_key in group_keys:
                candidate_keys.update(group_keys)
    matches = _dedupe(
        [
            str(column).strip()
            for column in columns
            if _metric_merge_column_key(column) in candidate_keys
        ]
    )
    return matches[0] if len(matches) == 1 else ""


# 함수 설명: join-only plan에 명시된 side별 또는 공통 집계 방식을 읽습니다.
def _join_only_metric_aggregation(step: dict[str, Any], side: str) -> str:
    for key in (
        f"{side}_aggregation",
        f"{side}_aggregation_method",
        f"{side}_agg_method",
        "aggregation",
        "aggregation_method",
        "agg_method",
    ):
        value = str(step.get(key) or "").strip().lower()
        if value:
            return "mean" if value in {"avg", "average"} else value
    return ""


# 함수 설명: trusted retrieval job의 metric semantics에서 허용 집계와 일치하는 기본 집계만 확정합니다.
def _join_only_metric_semantic_aggregation(
    job: dict[str, Any],
    source_column: str,
) -> tuple[str, str]:
    semantics = _dict(job.get("metric_semantics"))
    if not semantics:
        return "", "missing"
    source_key = _metric_merge_column_key(source_column)
    candidate_keys = {source_key}
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        for canonical, raw_aliases in _dict(job.get(mapping_name)).items():
            group_keys = {
                _metric_merge_column_key(value)
                for value in [str(canonical), *_string_list(raw_aliases)]
                if str(value).strip()
            }
            if source_key in group_keys:
                candidate_keys.update(group_keys)
    matches = [
        value
        for key, value in semantics.items()
        if _metric_merge_column_key(key) in candidate_keys
        and isinstance(value, dict)
    ]
    if not matches:
        return "", "missing"
    if len(matches) != 1:
        return "", "invalid"
    contract = matches[0]
    default_rollup = _normalized_metric_merge_aggregation(
        contract.get("default_rollup")
    )
    allowed_rollups = {
        _normalized_metric_merge_aggregation(value)
        for value in _string_list(contract.get("allowed_rollups"))
        if _normalized_metric_merge_aggregation(value)
    }
    if (
        not default_rollup
        or default_rollup not in SUPPORTED_AGGREGATIONS
        or not allowed_rollups
        or default_rollup not in allowed_rollups
        or not allowed_rollups.issubset(SUPPORTED_AGGREGATIONS)
    ):
        return "", "invalid"
    return default_rollup, "trusted"


# 함수 설명: metric rollup alias를 deterministic executor의 표준 집계명으로 정규화합니다.
def _normalized_metric_merge_aggregation(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "mean" if normalized in {"avg", "average"} else normalized


# 함수 설명: 컬럼 소유권 및 output 계약 비교용 대소문자 무시 key를 생성합니다.
def _metric_merge_column_key(value: Any) -> str:
    return str(value or "").strip().casefold()


# 함수 설명: `_reconcile_metric_merge_runtime_aliases()`는 독립 지표 병합 계약의 소스 별칭과 실행 시점 DataFrame을 안전하게 맞춥니다.
def _reconcile_metric_merge_runtime_aliases(
    plan_value: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ground a strict metric merge in actual runtime retrieval aliases.

    A metric merge is deterministic only when every declared metric source is
    also an actual retrieval job and runtime DataFrame. The model may use a
    presentation alias even though its ``dataset_key`` identifies one unique
    job. Such a unique dataset-key witness can safely repair the alias. We do
    not guess when a dataset has multiple jobs or when the reference has no
    trusted witness.
    """

    plan = deepcopy(plan_value)
    merge_plan = _dict(plan.get("resolved_metric_merge_plan"))
    if (
        merge_plan.get("strict") is not True
        or str(merge_plan.get("operation") or "").strip()
        != "merge_metric_sources"
    ):
        return plan, {"status": "not_applicable"}

    # New contracts record the topology that justified replacing a Typed raw
    # join.  Validate that compact provenance here as a second boundary: an
    # imported or stale contract must not silently claim independent metrics
    # while carrying row-enrichment semantics.
    execution_shape = str(merge_plan.get("execution_shape") or "").strip()
    if execution_shape:
        allowed_shapes = {
            "aggregate_outputs_then_join",
            "raw_join_rewritten_as_metric_merge",
            "output_contract_independent_metric_shape",
            "temporal_metric_merge",
        }
        join_type = str(merge_plan.get("join_type") or "").strip().lower()
        population_policy = str(
            merge_plan.get("population_policy") or ""
        ).strip().lower()
        join_node_ids = _string_list(merge_plan.get("join_node_ids"))
        shape_issues: list[dict[str, Any]] = []
        if execution_shape not in allowed_shapes:
            shape_issues.append(
                {"type": "unsupported_metric_merge_execution_shape", "value": execution_shape}
            )
        if join_type not in {"left", "outer"}:
            shape_issues.append(
                {"type": "metric_merge_join_type_invalid", "value": join_type}
            )
        expected_population = (
            "left_source_only"
            if join_type == "left"
            else "preserve_all_metric_source_keys"
        )
        if population_policy and population_policy != expected_population:
            shape_issues.append(
                {
                    "type": "metric_merge_population_policy_mismatch",
                    "join_type": join_type,
                    "population_policy": population_policy,
                }
            )
        if execution_shape != "temporal_metric_merge" and len(join_node_ids) != 1:
            shape_issues.append(
                {"type": "metric_merge_join_provenance_missing", "join_node_ids": join_node_ids}
            )
        if shape_issues:
            return plan, {"status": "invalid", "issues": shape_issues, "rewrites": []}

    runtime_sources = _dict(payload.get("runtime_sources"))
    jobs_by_alias: dict[str, dict[str, Any]] = {}
    jobs_by_dataset: dict[str, list[str]] = {}
    for job in _list(plan.get("retrieval_jobs")):
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        if not alias or not dataset_key or alias not in runtime_sources:
            continue
        jobs_by_alias[alias] = job
        jobs_by_dataset.setdefault(dataset_key.casefold(), []).append(alias)

    # 함수 설명: `alias_for()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
    def alias_for(reference: Any, dataset_key_hint: Any = "") -> str:
        raw = str(reference or "").strip()
        if raw in jobs_by_alias:
            return raw
        exact = [
            alias
            for alias in jobs_by_alias
            if raw and raw.casefold() == alias.casefold()
        ]
        if len(exact) == 1:
            return exact[0]
        dataset_key = str(dataset_key_hint or "").strip().casefold()
        dataset_matches = jobs_by_dataset.get(dataset_key, []) if dataset_key else []
        return dataset_matches[0] if len(dataset_matches) == 1 else ""

    issues: list[dict[str, Any]] = []
    rewrites: list[dict[str, str]] = []
    alias_map: dict[str, str] = {}
    metrics: list[dict[str, Any]] = []
    for index, raw_metric in enumerate(_list(merge_plan.get("metrics")), start=1):
        if not isinstance(raw_metric, dict):
            issues.append({"type": "invalid_metric_spec", "index": index})
            continue
        metric = deepcopy(raw_metric)
        raw_alias = str(metric.get("source_alias") or "").strip()
        alias = alias_for(raw_alias, metric.get("dataset_key"))
        if not alias:
            issues.append(
                {
                    "type": "unresolved_metric_source_alias",
                    "index": index,
                    "source_alias": raw_alias,
                    "dataset_key": str(metric.get("dataset_key") or ""),
                }
            )
            continue
        job = jobs_by_alias[alias]
        actual_dataset_key = str(job.get("dataset_key") or "").strip()
        declared_dataset_key = str(metric.get("dataset_key") or "").strip()
        if declared_dataset_key and declared_dataset_key.casefold() != actual_dataset_key.casefold():
            issues.append(
                {
                    "type": "metric_source_dataset_mismatch",
                    "index": index,
                    "source_alias": raw_alias,
                    "declared_dataset_key": declared_dataset_key,
                    "actual_dataset_key": actual_dataset_key,
                }
            )
            continue
        if raw_alias and raw_alias in alias_map and alias_map[raw_alias] != alias:
            issues.append(
                {
                    "type": "ambiguous_metric_source_alias",
                    "source_alias": raw_alias,
                }
            )
            continue
        if raw_alias and raw_alias != alias:
            rewrites.append(
                {
                    "field": f"resolved_metric_merge_plan.metrics[{index - 1}].source_alias",
                    "from": raw_alias,
                    "to": alias,
                }
            )
        if raw_alias:
            alias_map[raw_alias] = alias
        metric["source_alias"] = alias
        metric["dataset_key"] = actual_dataset_key
        metrics.append(metric)

    if len(metrics) < 2 or len({str(item.get("source_alias") or "") for item in metrics}) < 2:
        issues.append(
            {
                "type": "metric_source_population_incomplete",
                "metric_source_aliases": [
                    str(item.get("source_alias") or "") for item in metrics
                ],
            }
        )

    rewritten_grain_mappings: list[dict[str, Any]] = []
    expected_aliases = {str(item.get("source_alias") or "") for item in metrics}
    for index, raw_mapping in enumerate(_list(merge_plan.get("grain_mappings")), start=1):
        if not isinstance(raw_mapping, dict):
            issues.append({"type": "invalid_grain_mapping", "index": index})
            continue
        mapping = deepcopy(raw_mapping)
        raw_candidates = mapping.get("source_candidates")
        if not isinstance(raw_candidates, dict):
            issues.append(
                {
                    "type": "missing_grain_source_candidates",
                    "index": index,
                }
            )
            continue
        candidates: dict[str, Any] = {}
        for raw_alias, columns in raw_candidates.items():
            raw_alias_text = str(raw_alias or "").strip()
            alias = alias_map.get(raw_alias_text) or alias_for(raw_alias_text)
            if not alias:
                issues.append(
                    {
                        "type": "unresolved_grain_source_alias",
                        "index": index,
                        "source_alias": raw_alias_text,
                    }
                )
                continue
            if raw_alias_text != alias:
                rewrites.append(
                    {
                        "field": f"resolved_metric_merge_plan.grain_mappings[{index - 1}].source_candidates",
                        "from": raw_alias_text,
                        "to": alias,
                    }
                )
            if alias in candidates:
                issues.append(
                    {
                        "type": "duplicate_grain_source_alias",
                        "index": index,
                        "source_alias": alias,
                    }
                )
                continue
            candidates[alias] = deepcopy(columns)
        missing_aliases = sorted(alias for alias in expected_aliases if alias and alias not in candidates)
        if missing_aliases:
            issues.append(
                {
                    "type": "metric_grain_source_missing",
                    "index": index,
                    "source_aliases": missing_aliases,
                }
            )
        mapping["source_candidates"] = candidates
        rewritten_grain_mappings.append(mapping)

    if not rewritten_grain_mappings:
        issues.append({"type": "metric_grain_mappings_missing"})

    rewritten_transforms: list[dict[str, Any]] = []
    for index, raw_transform in enumerate(_list(merge_plan.get("source_transforms")), start=1):
        if not isinstance(raw_transform, dict):
            issues.append({"type": "invalid_metric_source_transform", "index": index})
            continue
        transform = deepcopy(raw_transform)
        raw_alias = str(transform.get("source_alias") or "").strip()
        alias = alias_map.get(raw_alias) or alias_for(raw_alias, transform.get("dataset_key"))
        if not alias:
            issues.append(
                {
                    "type": "unresolved_metric_transform_alias",
                    "index": index,
                    "source_alias": raw_alias,
                }
            )
            continue
        if raw_alias != alias:
            rewrites.append(
                {
                    "field": f"resolved_metric_merge_plan.source_transforms[{index - 1}].source_alias",
                    "from": raw_alias,
                    "to": alias,
                }
            )
        transform["source_alias"] = alias
        rewritten_transforms.append(transform)

    if issues:
        return plan, {
            "status": "invalid",
            "available_source_aliases": sorted(jobs_by_alias),
            "issues": issues,
            "rewrites": rewrites,
        }

    merge_plan["metrics"] = metrics
    merge_plan["grain_mappings"] = rewritten_grain_mappings
    if "source_transforms" in merge_plan:
        merge_plan["source_transforms"] = rewritten_transforms
    plan["resolved_metric_merge_plan"] = merge_plan

    # A comparison contract is derived from the same merge sources. Apply the
    # confirmed rewrites consistently so it cannot revive a stale alias later
    # in deterministic execution selection.
    comparison_plan = _dict(plan.get("resolved_metric_comparison_plan"))
    if comparison_plan and alias_map:
        plan["resolved_metric_comparison_plan"] = _rewrite_nested_metric_aliases(
            comparison_plan,
            alias_map,
        )
    output_contract = _dict(plan.get("output_contract"))
    bindings = _list(output_contract.get("metric_bindings"))
    if bindings and alias_map:
        output_contract["metric_bindings"] = [
            _rewrite_nested_metric_aliases(binding, alias_map)
            if isinstance(binding, dict)
            else deepcopy(binding)
            for binding in bindings
        ]
        plan["output_contract"] = output_contract
    return plan, {
        "status": "reconciled" if rewrites else "valid",
        "available_source_aliases": sorted(jobs_by_alias),
        "rewrites": rewrites,
    }


# 함수 설명: `_rewrite_nested_metric_aliases()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _rewrite_nested_metric_aliases(value: Any, aliases: dict[str, str]) -> Any:
    """Rewrite only already-confirmed alias values in a nested contract."""

    if isinstance(value, list):
        return [_rewrite_nested_metric_aliases(item, aliases) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {
            "source_alias",
            "left_source_alias",
            "right_source_alias",
            "reference_source_alias",
            "target_source_alias",
        }:
            raw = str(item or "").strip()
            result[key] = aliases.get(raw, item)
        elif key == "source_candidates" and isinstance(item, dict):
            result[key] = {
                aliases.get(str(alias or "").strip(), str(alias or "").strip()): _rewrite_nested_metric_aliases(columns, aliases)
                for alias, columns in item.items()
            }
        else:
            result[key] = _rewrite_nested_metric_aliases(item, aliases)
    return result


# 함수 설명: Typed formula 단계가 임의 식 실행 없이 선언형 산술 계약만 사용하는지 확인합니다.
def _typed_formula_contract(step: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return one normalized arithmetic contract or a compact validation reason.

    The deterministic executor never evaluates expression strings.  A formula
    consists only of existing column references and finite numeric constants,
    which makes the same Typed-IR primitive usable for any registered derived
    metric without admitting arbitrary Python.
    """

    formula = _dict(step.get("formula"))
    output_column = str(formula.get("output_column") or "").strip()
    operator = str(formula.get("operator") or "").strip().lower()
    operands = formula.get("operands")
    if not output_column or operator not in TYPED_FORMULA_OPERATORS:
        return {}, "formula_output_or_operator_invalid"
    if not isinstance(operands, list) or not 2 <= len(operands) <= 8:
        return {}, "formula_operands_invalid"
    if operator in {"subtract", "divide"} and len(operands) != 2:
        return {}, "formula_operand_count_invalid"

    normalized_operands: list[dict[str, Any]] = []
    column_keys: set[str] = set()
    for operand in operands:
        if not isinstance(operand, dict) or len(operand) != 1:
            return {}, "formula_operand_invalid"
        if "column" in operand:
            column = str(operand.get("column") or "").strip()
            if not column:
                return {}, "formula_operand_column_invalid"
            normalized_operands.append({"column": column})
            column_keys.add(_typed_formula_column_key(column))
            continue
        if "constant" not in operand or isinstance(operand.get("constant"), bool):
            return {}, "formula_operand_invalid"
        try:
            constant = float(operand.get("constant"))
        except (TypeError, ValueError):
            return {}, "formula_operand_constant_invalid"
        if not math.isfinite(constant):
            return {}, "formula_operand_constant_invalid"
        normalized_operands.append({"constant": operand.get("constant")})
    if _typed_formula_column_key(output_column) in column_keys:
        return {}, "formula_output_self_reference"

    null_policy = str(formula.get("null_policy") or "propagate").strip().lower()
    if null_policy not in TYPED_FORMULA_NULL_POLICIES:
        return {}, "formula_null_policy_invalid"
    normalized: dict[str, Any] = {
        "output_column": output_column,
        "operator": operator,
        "operands": normalized_operands,
        "null_policy": null_policy,
    }
    if operator == "divide":
        zero_division_policy = str(
            formula.get("zero_division_policy") or "null"
        ).strip().lower()
        if zero_division_policy not in TYPED_FORMULA_ZERO_DIVISION_POLICIES:
            return {}, "formula_zero_division_policy_invalid"
        normalized["zero_division_policy"] = zero_division_policy
    if "round_digits" in formula:
        raw_round_digits = formula.get("round_digits")
        if isinstance(raw_round_digits, bool):
            return {}, "formula_round_digits_invalid"
        try:
            round_digits = int(raw_round_digits)
        except (TypeError, ValueError):
            return {}, "formula_round_digits_invalid"
        try:
            is_integral = float(round_digits) == float(raw_round_digits)
        except (TypeError, ValueError):
            is_integral = False
        if not is_integral or not 0 <= round_digits <= 12:
            return {}, "formula_round_digits_invalid"
        normalized["round_digits"] = round_digits
    return normalized, ""


# 함수 설명: 실제 조회 결과의 컬럼으로 Typed 단계별 frame 계약을 다시 확인합니다.
def _runtime_typed_frame_contract(
    plan: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Verify a supported Typed DAG against the frames that were actually retrieved.

    This is a narrow execution-boundary check, not a second planner.  It proves
    only column flow that follows directly from a Typed operation: filters and
    ordering preserve a frame, projections narrow it, aggregates emit their
    declared grain and output columns, and joins consume the immediate left and
    right schemas.  If a schema cannot be observed or an operation is outside
    this small set, return ``not_applicable`` so the established Complex path
    remains available.  A known missing column is the only ``invalid`` result.
    """

    steps = [item for item in _list(plan.get("pandas_execution_plan")) if isinstance(item, dict)]
    if not steps or len(steps) > 32:
        return {"status": "not_applicable", "compiled_node_count": 0, "issues": []}

    runtime_sources = _dict(payload.get("runtime_sources"))
    external_aliases = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        for item in _list(plan.get("retrieval_jobs"))
        if isinstance(item, dict)
    }
    external_aliases.update(
        alias
        for alias in runtime_sources
        if str(alias or "").strip() == "previous_result"
    )
    external_aliases = {alias for alias in external_aliases if alias and alias in runtime_sources}
    if not external_aliases:
        return {"status": "not_applicable", "compiled_node_count": 0, "issues": []}

    # 함수 설명: 대소문자와 공백 차이를 무시하는 컬럼 비교용 키를 만듭니다.
    def identity(value: Any) -> str:
        return str(value or "").strip().casefold().replace(" ", "_")

    # 함수 설명: 실행 frame의 실제 컬럼 이름을 요청한 표준 컬럼으로 찾습니다.
    def actual_column(columns: list[str], requested: Any) -> str:
        requested_id = identity(requested)
        return next(
            (column for column in columns if requested_id and identity(column) == requested_id),
            "",
        )

    # 함수 설명: source 또는 앞 단계 별칭에 연결된 실행 frame 컬럼을 반환합니다.
    def frame_columns(alias: str) -> list[str]:
        columns = _source_columns(payload, alias)
        if columns:
            return columns
        frame = runtime_sources.get(alias)
        raw_columns = getattr(frame, "columns", None)
        if raw_columns is not None:
            return _dedupe([str(column).strip() for column in raw_columns if str(column).strip()])
        return []

    states: dict[str, dict[str, Any]] = {}
    for alias in external_aliases:
        columns = frame_columns(alias)
        # A connector can validly return no rows.  The schema itself is still
        # expected in source_results; without it we cannot prove Typed flow and
        # must not claim that an empty runtime frame proves a missing column.
        if not columns:
            return {"status": "not_applicable", "compiled_node_count": 0, "issues": []}
        states[alias] = {"columns": list(columns), "known": True}

    issues: list[dict[str, Any]] = []
    compiled = 0

    # 함수 설명: Typed 단계의 입력 별칭을 하나의 목록으로 정규화합니다.
    def inputs_for(step: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in _list(step.get("inputs")) if isinstance(item, dict)]

    # 함수 설명: 입력 별칭의 현재 실행 스키마를 조회합니다.
    def state_for_input(item: dict[str, Any]) -> dict[str, Any] | None:
        reference = str(item.get("ref") or "").strip()
        return states.get(reference)

    for step in steps:
        operation = _operation(step)
        node_id = str(step.get("node_id") or "").strip()
        output_alias = str(step.get("output_alias") or node_id).strip()
        inputs = inputs_for(step)
        if not node_id or not output_alias:
            return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
        if operation not in TYPED_DETERMINISTIC_OPERATIONS:
            return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
        input_states = [state_for_input(item) for item in inputs]
        if not inputs or any(state is None for state in input_states):
            return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
        states_list = [state for state in input_states if isinstance(state, dict)]

        if operation in {
            "apply_filters",
            "apply_pandas_function_case",
            "apply_row_match_groups",
            "sort_and_top_n",
        }:
            if len(states_list) != 1:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            result_columns = list(states_list[0]["columns"])
            if operation == "sort_and_top_n":
                sort_by = str(step.get("sort_by") or "").strip()
                if sort_by and not actual_column(result_columns, sort_by):
                    issues.append(
                        {"type": "typed_frame_runtime_sort_column_missing", "node_id": node_id, "column": sort_by}
                    )
        elif operation == "select_columns":
            if len(states_list) != 1:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            source_columns = list(states_list[0]["columns"])
            requested = _string_list(step.get("projection") or step.get("columns"))
            if not requested:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            missing = [column for column in requested if not actual_column(source_columns, column)]
            if missing:
                issues.append(
                    {"type": "typed_frame_runtime_projection_column_missing", "node_id": node_id, "columns": missing}
                )
            result_columns = [actual_column(source_columns, column) or column for column in requested]
        elif operation == "groupby_and_aggregate":
            if len(states_list) != 1:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            source_columns = list(states_list[0]["columns"])
            group_by = _string_list(
                step.get("group_by") or step.get("group_by_columns") or step.get("group_columns")
            )
            aggregations = [item for item in _list(step.get("aggregations")) if isinstance(item, dict)]
            if not aggregations:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            missing = [column for column in group_by if not actual_column(source_columns, column)]
            output_columns: list[str] = []
            for aggregation in aggregations:
                source_column = str(
                    aggregation.get("column") or aggregation.get("source_column") or aggregation.get("agg_column") or ""
                ).strip()
                output_column = str(
                    aggregation.get("output_column") or aggregation.get("result_column") or ""
                ).strip()
                if not source_column or not output_column or not actual_column(source_columns, source_column):
                    missing.append(source_column or output_column)
                    continue
                output_columns.append(output_column)
            if missing:
                issues.append(
                    {"type": "typed_frame_runtime_aggregate_column_missing", "node_id": node_id, "columns": _dedupe(missing)}
                )
            result_columns = _dedupe([*group_by, *output_columns])
        elif operation == "derive_formula":
            if len(states_list) != 1:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            formula, formula_error = _typed_formula_contract(step)
            if formula_error:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            source_columns = list(states_list[0]["columns"])
            operand_columns = [
                str(operand.get("column") or "").strip()
                for operand in formula.get("operands", [])
                if isinstance(operand, dict) and "column" in operand
            ]
            missing = [
                column
                for column in operand_columns
                if not actual_column(source_columns, column)
            ]
            output_column = str(formula.get("output_column") or "").strip()
            if actual_column(source_columns, output_column):
                issues.append(
                    {
                        "type": "typed_frame_runtime_formula_output_conflict",
                        "node_id": node_id,
                        "column": output_column,
                    }
                )
            if missing:
                issues.append(
                    {
                        "type": "typed_frame_runtime_formula_operand_missing",
                        "node_id": node_id,
                        "columns": _dedupe(missing),
                    }
                )
            result_columns = _dedupe([*source_columns, output_column])
        elif operation == "join":
            if len(states_list) != 2:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            left_columns = list(states_list[0]["columns"])
            right_columns = list(states_list[1]["columns"])
            left_keys = _string_list(step.get("left_on"))
            right_keys = _string_list(step.get("right_on"))
            if not left_keys:
                left_keys = _string_list(step.get("on") or step.get("group_by"))
                right_keys = list(left_keys)
            if not left_keys or len(left_keys) != len(right_keys):
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            missing_keys = [
                f"left.{column}" for column in left_keys if not actual_column(left_columns, column)
            ] + [
                f"right.{column}" for column in right_keys if not actual_column(right_columns, column)
            ]
            if missing_keys:
                issues.append(
                    {"type": "typed_frame_runtime_join_key_missing", "node_id": node_id, "columns": missing_keys}
                )
            right_values = _string_list(step.get("right_value_columns"))
            missing_values = [
                column for column in right_values if not actual_column(right_columns, column)
            ]
            if missing_values:
                issues.append(
                    {"type": "typed_frame_runtime_join_value_missing", "node_id": node_id, "columns": missing_values}
                )
            join_type = str(step.get("join_type") or "inner").strip().lower()
            if join_type not in TYPED_DETERMINISTIC_JOIN_TYPES:
                return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}
            right_key_ids = {identity(column) for column in right_keys}
            selected_right = right_values or right_columns
            if join_type == "left" and right_values:
                selected_right = [
                    column for column in right_values if identity(column) not in right_key_ids
                ]
            result_columns = _dedupe([*left_columns, *selected_right])
        else:
            return {"status": "not_applicable", "compiled_node_count": compiled, "issues": []}

        states[node_id] = {"columns": result_columns, "known": True}
        states[output_alias] = states[node_id]
        compiled += 1

    return {
        "status": "invalid" if issues else "verified",
        "compiled_node_count": compiled,
        "issues": issues[:8],
    }


# 함수 설명: `_typed_pandas_plan_execution_contract()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _typed_pandas_plan_execution_contract(
    payload: dict[str, Any],
    plan: dict[str, Any],
    source_aliases: list[str],
) -> dict[str, Any]:
    """Return a deterministic Typed-IR contract only for an unambiguous DataFrame DAG.

    This is intentionally narrower than the general Complex path. It accepts
    only a declarative DataFrame DAG whose every input is an earlier output,
    an actually retrieved source, or the trusted prior-result frame restored
    for a follow-up. Unsupported calculations stay on the LLM Pandas path
    rather than being guessed here.
    """

    if not source_aliases or len(source_aliases) > 8:
        return {}
    steps = [
        deepcopy(item)
        for item in _list(plan.get("pandas_execution_plan"))
        if isinstance(item, dict)
    ]
    if not steps or len(steps) > 32:
        return {}
    formula_indexes = [
        index
        for index, step in enumerate(steps)
        if _operation(step) == "derive_formula"
    ]
    # Formula values are a terminal presentation calculation in the initial
    # Typed contract.  Allow a final stable ordering only; a later projection,
    # re-aggregation, or join needs a separately reviewed compositional rule.
    for formula_index in formula_indexes:
        suffix_operations = [
            _operation(step)
            for step in steps[formula_index + 1 :]
        ]
        if any(operation != "sort_and_top_n" for operation in suffix_operations):
            return {}
    output_contract = _dict(plan.get("output_contract"))
    result_columns = _string_list(output_contract.get("result_columns"))
    if not result_columns:
        result_columns = _string_list(output_contract.get("required_columns"))
    if output_contract.get("strict_result_columns") is not True or not result_columns:
        return {}
    if _list(plan.get("validation_errors")):
        return {}
    graph = _dict(plan.get("resolved_execution_graph"))
    if _list(graph.get("validation_errors")):
        return {}

    static_frame_contract = _dict(plan.get("typed_frame_contract"))
    if static_frame_contract.get("status") == "invalid":
        return {}
    runtime_frame_contract = _runtime_typed_frame_contract(plan, payload)
    if runtime_frame_contract.get("status") != "verified":
        return {}

    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    jobs_by_alias: dict[str, dict[str, Any]] = {}
    for job in _list(plan.get("retrieval_jobs")):
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias:
            if alias in jobs_by_alias:
                return {}
            jobs_by_alias[alias] = job
    if set(source_aliases) != set(jobs_by_alias).intersection(source_aliases):
        return {}
    if any(alias not in runtime_sources for alias in source_aliases):
        return {}

    graph = _dict(plan.get("resolved_execution_graph"))
    trusted_previous_aliases = {
        str(item.get("source_alias") or "").strip()
        for item in _list(graph.get("external_source_requirements"))
        if isinstance(item, dict)
        and str(item.get("provider") or "").strip()
        in {"previous_result", "previous_source"}
        and str(item.get("source_alias") or "").strip() in runtime_sources
    }
    reference_mode = str(plan.get("reference_mode") or "").strip()
    if (
        reference_mode in {"previous_result_rows", "previous_result_transform"}
        and "previous_result" in runtime_sources
    ):
        trusted_previous_aliases.add("previous_result")
    allowed_external_aliases = set(jobs_by_alias) | trusted_previous_aliases
    if not allowed_external_aliases:
        return {}

    selected_function_cases = [
        item
        for item in _list(plan.get("pandas_function_cases"))
        if isinstance(item, dict)
    ]
    source_transforms: list[dict[str, Any]] = []
    transform_markers: set[tuple[str, str, str, str]] = set()
    produced: set[str] = set()
    produced_operations: dict[str, str] = {}
    used_external_aliases: list[str] = []
    for step in steps:
        operation = _operation(step)
        if operation not in TYPED_DETERMINISTIC_OPERATIONS:
            return {}
        node_id = str(step.get("node_id") or "").strip()
        output_alias = str(step.get("output_alias") or node_id).strip()
        if (
            not node_id
            or not output_alias
            or node_id in produced
            or output_alias in produced
            or node_id in allowed_external_aliases
            or output_alias in allowed_external_aliases
        ):
            return {}
        inputs = [item for item in _list(step.get("inputs")) if isinstance(item, dict)]
        expected_input_count = 2 if operation == "join" else 1
        if len(inputs) != expected_input_count:
            return {}
        for item in inputs:
            kind = str(item.get("kind") or "").strip()
            reference = str(item.get("ref") or "").strip()
            if kind == "external_source":
                if reference not in allowed_external_aliases:
                    return {}
                if reference not in used_external_aliases:
                    used_external_aliases.append(reference)
            elif kind == "node_output":
                if reference not in produced:
                    return {}
            else:
                return {}

        if operation == "apply_pandas_function_case":
            if len(inputs) != 1 or str(inputs[0].get("kind") or "") != "external_source":
                return {}
            source_alias = str(inputs[0].get("ref") or "").strip()
            function_name = str(step.get("function_name") or "").strip()
            function_case_key = str(
                step.get("function_case_key") or step.get("key") or ""
            ).strip()
            input_text = str(step.get("input_text") or "")
            matching_cases = [
                item
                for item in selected_function_cases
                if str(item.get("source_alias") or "").strip() == source_alias
                and (
                    not function_name
                    or str(item.get("function_name") or "").strip() == function_name
                )
                and (
                    not function_case_key
                    or str(item.get("key") or "").strip() == function_case_key
                )
                and (
                    not input_text
                    or str(item.get("input_text") or "") == input_text
                )
            ]
            if len(matching_cases) != 1:
                return {}
            selected_case = matching_cases[0]
            transform_name = str(selected_case.get("function_name") or "").strip()
            transform_key = str(selected_case.get("key") or "").strip()
            transform_input = str(selected_case.get("input_text") or "")
            if not transform_name:
                return {}
            marker = (source_alias, transform_name, transform_key, transform_input)
            if marker in transform_markers:
                return {}
            transform_markers.add(marker)
            source_transforms.append(
                {
                    "node_id": node_id,
                    "source_alias": source_alias,
                    "function_name": transform_name,
                    "function_case_key": transform_key,
                    "input_text": transform_input,
                    "arguments": deepcopy(selected_case.get("arguments"))
                    if isinstance(selected_case.get("arguments"), dict)
                    else {},
                }
            )
        elif operation == "apply_row_match_groups":
            reference_alias = str(step.get("reference_source_alias") or "").strip()
            match_columns = _string_list(step.get("match_columns"))
            blank_policy = str(step.get("blank_policy") or "normalize_blank").strip()
            if (
                not reference_alias
                or reference_alias not in trusted_previous_aliases
                or not match_columns
                or blank_policy != "normalize_blank"
            ):
                return {}
            if reference_alias not in used_external_aliases:
                used_external_aliases.append(reference_alias)
        elif operation == "select_columns":
            if not _string_list(step.get("projection") or step.get("columns")):
                return {}
        elif operation == "groupby_and_aggregate":
            aggregations = [item for item in _list(step.get("aggregations")) if isinstance(item, dict)]
            if not aggregations:
                return {}
            for aggregation in aggregations:
                source_column = str(aggregation.get("column") or aggregation.get("source_column") or "").strip()
                output_column = str(aggregation.get("output_column") or "").strip()
                method = str(aggregation.get("method") or aggregation.get("aggregation") or "").strip().lower()
                if not source_column or not output_column or method not in TYPED_DETERMINISTIC_AGGREGATIONS:
                    return {}
        elif operation == "sort_and_top_n":
            if not str(step.get("sort_by") or "").strip():
                return {}
            try:
                if int(step.get("limit") or 0) < 0:
                    return {}
            except (TypeError, ValueError):
                return {}
        elif operation == "derive_formula":
            if (
                len(inputs) != 1
                or str(inputs[0].get("kind") or "").strip() != "node_output"
            ):
                return {}
            upstream_operation = produced_operations.get(
                str(inputs[0].get("ref") or "").strip(),
                "",
            )
            if upstream_operation not in AGGREGATE_OPERATIONS:
                return {}
            formula, formula_error = _typed_formula_contract(step)
            if formula_error:
                return {}
            output_column = str(formula.get("output_column") or "").strip()
            if not output_column or not any(
                _typed_formula_column_key(column)
                == _typed_formula_column_key(output_column)
                for column in result_columns
            ):
                return {}
        elif operation == "join":
            join_type = str(step.get("join_type") or "inner").strip().lower()
            if join_type not in TYPED_DETERMINISTIC_JOIN_TYPES:
                return {}
            left_keys = _string_list(step.get("left_on"))
            right_keys = _string_list(step.get("right_on"))
            shared_keys = _string_list(step.get("on")) or _string_list(step.get("group_by"))
            if (left_keys or right_keys) and (not left_keys or len(left_keys) != len(right_keys)):
                return {}
            if not left_keys and not shared_keys:
                return {}
        produced.update({node_id, output_alias})
        produced_operations[node_id] = operation
        produced_operations[output_alias] = operation

    return {
        "version": CONTRACT_VERSION,
        "strict": True,
        "route": "complex",
        "operation": "execute_typed_pandas_plan",
        "steps": steps,
        "result_columns": result_columns,
        "external_source_aliases": used_external_aliases,
        "source_transforms": source_transforms,
        "eligibility": {
            "eligible": False,
            "reason_codes": ["typed_plan_contract_resolved", "model_code_not_required"],
        },
    }


# 함수 설명: `_intent_route_candidate()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _intent_route_candidate(plan: dict[str, Any]) -> dict[str, Any]:
    intent_ir = _dict(plan.get("intent_ir"))
    steps = [deepcopy(item) for item in _list(plan.get("pandas_execution_plan")) if isinstance(item, dict)]
    ir_operations = [
        str(item).strip().lower()
        for item in _string_list(intent_ir.get("operations"))
        if str(item).strip()
    ]
    operations = ir_operations or [_operation(item) for item in steps]
    output_contract = _dict(plan.get("output_contract"))
    recipe = _recipe(steps, output_contract)
    aliases = _string_list(intent_ir.get("route_source_aliases"))
    if not aliases:
        aliases = _dedupe(
            [
                str(item.get("source_alias") or item.get("dataset_key") or "").strip()
                for item in _list(plan.get("retrieval_jobs"))
                if isinstance(item, dict)
                and str(item.get("source_alias") or item.get("dataset_key") or "").strip()
            ]
        )
    graph = _dict(plan.get("resolved_execution_graph"))
    if not aliases:
        aliases = _dedupe(
            [
                str(item.get("source_alias") or "").strip()
                for item in _list(graph.get("external_source_requirements"))
                if isinstance(item, dict)
                and str(item.get("provider") or "").strip() in {"retrieval_job", "previous_source"}
                and str(item.get("source_alias") or "").strip()
            ]
        )
    if len(aliases) > 1:
        return {
            "intent_candidate": "complex_required",
            "candidate_recipe": recipe,
            "candidate_reason_codes": ["multiple_external_sources"],
            "candidate_source_aliases": aliases,
        }
    if any(operation in COMPLEX_OPERATIONS for operation in operations):
        return {
            "intent_candidate": "complex_required",
            "candidate_recipe": recipe,
            "candidate_reason_codes": ["complex_operation_required"],
            "candidate_operations": operations,
            "candidate_source_aliases": aliases,
        }
    unknown_operations = [
        operation
        for operation in operations
        if operation
        and operation not in FILTER_ONLY_OPERATIONS
        and operation not in AGGREGATE_OPERATIONS
        and operation not in SORT_OPERATIONS
        and operation not in SELECT_OPERATIONS
        and operation not in RECIPE_OPERATIONS
    ]
    if len(aliases) == 1 and recipe in SUPPORTED_RECIPES and not unknown_operations:
        return {
            "intent_candidate": "fast_candidate",
            "candidate_recipe": recipe,
            "candidate_reason_codes": ["single_source", "supported_recipe"],
            "candidate_source_aliases": aliases,
        }
    return {
        "intent_candidate": "complex_candidate",
        "candidate_recipe": recipe,
        "candidate_reason_codes": ["route_contract_incomplete"],
        "candidate_operations": operations,
        "candidate_source_aliases": aliases,
    }


# 함수 설명: `_route_contract()`는 14B V2 단순 분석 계약 결정기 처리 중 contract 관련 값을 계산·변환하는 내부 helper입니다.
def _route_contract(route: str, reason: str) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "strict": True,
        "route": route,
        "eligibility": {"eligible": route == "fast", "reason_codes": [reason]},
    }


# 함수 설명: `_execution_blocked()`는 14B V2 단순 분석 계약 결정기 처리 중 blocked 관련 값을 계산·변환하는 내부 helper입니다.
def _execution_blocked(payload: dict[str, Any]) -> bool:
    return str(_dict(payload.get("execution_gate")).get("status") or "").strip().lower() == "blocked"


# 함수 설명: `_last_step()`는 14B V2 단순 분석 계약 결정기 처리 중 STEP 관련 값을 계산·변환하는 내부 helper입니다.
def _last_step(steps: list[dict[str, Any]], operations: set[str]) -> dict[str, Any]:
    return next((step for step in reversed(steps) if _operation(step) in operations), {})


# 함수 설명: `_last_recipe_step()`는 14B V2 단순 분석 계약 결정기 처리 중 recipe·STEP 관련 값을 계산·변환하는 내부 helper입니다.
def _last_recipe_step(steps: list[dict[str, Any]], recipe: str) -> dict[str, Any]:
    return next((step for step in reversed(steps) if RECIPE_OPERATIONS.get(_operation(step)) == recipe), {})


# 함수 설명: `_operation()`는 저장 작업을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _operation(step: dict[str, Any]) -> str:
    return str(step.get("operation") or step.get("step") or "").strip().lower()


# 함수 설명: `_normalize_operator()`는 연산자의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다.
def _normalize_operator(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "=": "eq", "==": "eq", "!=": "ne", ">": "gt", ">=": "ge", "gte": "ge",
        "<": "lt", "<=": "le", "lte": "le", "startswith": "starts_with", "prefix": "starts_with",
        "endswith": "ends_with", "suffix": "ends_with", "notin": "not_in", "isnull": "is_null",
        "notnull": "not_null", "notblank": "not_blank",
    }
    return aliases.get(text, text)


# 함수 설명: `_value_type()`는 14B V2 단순 분석 계약 결정기 처리 중 유형 관련 값을 계산·변환하는 내부 helper입니다.
def _value_type(values: list[Any]) -> str:
    non_null = [value for value in values if value is not None]
    if non_null and all(isinstance(value, bool) for value in non_null):
        return "boolean"
    if non_null and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_null):
        return "number"
    return "string"


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Data):
        value = value.data
    if hasattr(value, "data") and isinstance(value.data, dict):
        value = value.data
    return deepcopy(value) if isinstance(value, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_as_list()`는 단일 값과 여러 값 입력을 모두 같은 list 형태로 맞춰 반복 처리를 단순화합니다.
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return _dedupe([str(item).strip() for item in value if str(item or "").strip()])


# 함수 설명: `_dedupe()`는 dedupe의 중복을 제거하고 최초 등장 순서를 유지합니다.
def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


# 함수 설명: `_dedupe_dicts()`는 dicts의 중복을 제거하고 최초 등장 순서를 유지합니다.
def _dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


# 함수 설명: `_positive_int()`는 입력 숫자를 1 이상의 정수로 제한해 preview·history 한도에 사용합니다.
def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        return default
    return number if number > 0 else default


# 함수 설명: `_bool()`는 문자열·숫자·불리언 표기를 일관된 bool 값으로 해석합니다.
def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


# Langflow 컴포넌트 클래스: 조회 완료 payload를 범용 Fast/Complex/Blocked 실행 계약으로 변환합니다.
class SimpleAnalysisContractResolver(Component):
    display_name = "14B V2 단순 분석 계약 결정기"
    description = "실제 source schema와 정규화 계약으로 Fast/Complex/Blocked 경로를 결정합니다."
    inputs = [
        DataInput(name="payload", display_name="조회 페이로드", required=True),
        BoolInput(name="fast_path_enabled", display_name="Fast Path 사용", value=True, advanced=False),
        IntInput(name="detail_row_limit", display_name="상세 조회 최대 행", value=5000, advanced=True),
        IntInput(name="max_pivot_columns", display_name="Pivot 최대 컬럼", value=50, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="경로 결정 페이로드", method="build_payload")]

    # 함수 설명: `build_payload()`는 페이로드 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
    def build_payload(self) -> Data:
        return Data(
            data=resolve_simple_analysis_contract(
                getattr(self, "payload", None),
                getattr(self, "fast_path_enabled", True),
                getattr(self, "detail_row_limit", 5000),
                getattr(self, "max_pivot_columns", 50),
            )
        )
