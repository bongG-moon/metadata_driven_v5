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
}
VALUELESS_FILTER_OPERATORS = {
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
}


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
    plan["route_resolution"] = _intent_route_candidate(plan)
    next_payload["intent_plan"] = plan
    trace = next_payload.setdefault("trace", {}).setdefault("inspection", {})

    if _execution_blocked(next_payload):
        contract = _route_contract("blocked", "execution_gate_blocked")
        return _attach_contract(next_payload, contract, trace, started)
    if not _bool(fast_path_enabled, True):
        contract = _route_contract("complex", "fast_path_disabled")
        return _attach_contract(next_payload, contract, trace, started)

    source_aliases = _external_source_aliases(next_payload)
    if len(source_aliases) != 1:
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
    result_columns = _result_columns(output_contract, projection, group_by, metrics, calculation)
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
    plan["resolved_fast_path_plan"] = deepcopy(contract)
    next_payload["intent_plan"] = plan
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
    allowed_result_columns = available | metric_outputs | calculation_outputs
    if recipe != "pivot_summary":
        for column in result_columns:
            if column.casefold() not in allowed_result_columns:
                errors.append({"type": "unresolved_result_column", "column": column})
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
    output_contract: dict[str, Any],
    projection: list[str],
    group_by: list[str],
    metrics: list[dict[str, Any]],
    calculation: dict[str, Any],
) -> list[str]:
    configured = _string_list(output_contract.get("result_columns"))
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
    payload["simple_analysis_contract"] = deepcopy(contract)
    plan = _dict(payload.get("intent_plan"))
    route_resolution = _dict(plan.get("route_resolution"))
    route_resolution.update(
        {
            "final_route": str(contract.get("route") or "complex"),
            "final_recipe": str(contract.get("recipe") or ""),
            "final_reason_codes": deepcopy(
                _dict(contract.get("eligibility")).get("reason_codes") or []
            ),
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
        "llm_calls": {"intent": 1, "pandas_generation": 0, "repair": 0, "answer": 0},
        "timing_ms": {"route_resolution": round((perf_counter() - started) * 1000, 3)},
    }
    return payload


# 함수 설명: `_intent_route_candidate()`는 조회 전 실행 계약만으로 Fast 후보 또는 Complex 필요 여부를 결정하며 최종 판정을 대신하지 않습니다.
def _intent_route_candidate(plan: dict[str, Any]) -> dict[str, Any]:
    steps = [deepcopy(item) for item in _list(plan.get("pandas_execution_plan")) if isinstance(item, dict)]
    operations = [_operation(item) for item in steps]
    output_contract = _dict(plan.get("output_contract"))
    recipe = _recipe(steps, output_contract)
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
