from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any


FIXTURE_EXACT = "fixture_exact"
SEMANTIC_LIVE = "semantic_live"
VALIDATION_PROFILES = {FIXTURE_EXACT, SEMANTIC_LIVE}
ADVISORY_ISSUE_TYPES = {
    "duplicate_result_grain",
    "result_limit_exceeded",
    "missing_ordering_column",
    "result_order_violation",
    "function_case_not_executed",
    "function_case_helper_missing",
    "metric_aggregation_not_allowed",
}


def resolve_validation_profile(value: str = "auto", *, use_llm: bool = False) -> str:
    profile = str(value or "auto").strip().lower()
    if profile == "auto":
        return SEMANTIC_LIVE if use_llm else FIXTURE_EXACT
    if profile not in VALIDATION_PROFILES:
        raise ValueError(f"unsupported validation profile: {value!r}")
    return profile


def validate_semantic_payload(
    payload: Any,
    *,
    question: str = "",
    pandas_variables: Any = None,
) -> dict[str, Any]:
    """Validate the hydrated execution/result contract without relying on labels.

    The validator only evaluates contracts already declared by the plan or trusted
    catalog. It does not contain metric-name-specific fallbacks.
    """

    data = payload if isinstance(payload, dict) else {}
    plan = data.get("intent_plan") if isinstance(data.get("intent_plan"), dict) else {}
    analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
    rows = _dict_rows(data.get("data", {}).get("rows", []))
    columns = _unique_text(analysis.get("columns") or _row_columns(rows))
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    issues: list[dict[str, Any]] = []
    issues.extend(_execution_graph_errors(plan))
    issues.extend(_metric_binding_errors(plan, columns))
    issues.extend(_result_shape_errors(plan, rows, columns))
    issues.extend(_ordering_errors(plan, rows, columns))
    issues.extend(_temporal_contract_errors(plan))
    issues.extend(_function_case_errors(plan, pandas_variables))
    issues.extend(_certificate_errors(analysis, rows))
    for issue in issues:
        if str(issue.get("type") or "") in ADVISORY_ISSUE_TYPES:
            warnings.append(issue)
        else:
            errors.append(issue)

    return {
        "status": "ok" if not errors else "error",
        "errors": _unique_dicts(errors),
        "warnings": _unique_dicts(warnings),
        "question": str(question or ""),
    }


def validate_case_expectation(case: Any, payload: Any) -> list[dict[str, Any]]:
    """Validate stable case semantics while ignoring aliases, fixture values and order."""

    expected = case if isinstance(case, dict) else {}
    data = payload if isinstance(payload, dict) else {}
    plan = data.get("intent_plan") if isinstance(data.get("intent_plan"), dict) else {}
    actual_jobs = _dict_items(plan.get("retrieval_jobs"))
    expected_plan = (
        expected.get("intent_response", {}).get("intent_plan", {})
        if isinstance(expected.get("intent_response"), dict)
        else {}
    )
    expected_jobs = _dict_items(expected_plan.get("retrieval_jobs"))
    errors: list[dict[str, Any]] = []

    for expected_job in expected_jobs:
        dataset_key = str(expected_job.get("dataset_key") or "").strip()
        candidates = [
            job
            for job in actual_jobs
            if str(job.get("dataset_key") or "").strip() == dataset_key
        ]
        if not candidates:
            errors.append(
                {
                    "type": "missing_expected_dataset",
                    "dataset_key": dataset_key,
                    "message": f"required dataset is missing: {dataset_key}",
                }
            )
            continue
        expected_filters = _dict(expected_job.get("filters"))
        if not expected_filters:
            continue
        if not any(_filters_cover(_dict(job.get("filters")), expected_filters) for job in candidates):
            errors.append(
                {
                    "type": "missing_expected_filter_contract",
                    "dataset_key": dataset_key,
                    "expected_filters": deepcopy(expected_filters),
                    "actual_filters": [deepcopy(_dict(job.get("filters"))) for job in candidates],
                    "message": f"dataset filter contract is not satisfied: {dataset_key}",
                }
            )

    return _unique_dicts(errors)


def fixture_differences(case: Any, payload: Any) -> list[str]:
    """Return exact fixture differences for diagnostics in semantic-live mode."""

    expected = case if isinstance(case, dict) else {}
    data = payload if isinstance(payload, dict) else {}
    rows = _dict_rows(data.get("data", {}).get("rows", []))
    columns = _unique_text(data.get("analysis", {}).get("columns", []))
    differences: list[str] = []

    expected_count = expected.get("expected_row_count")
    if expected_count is not None and len(rows) != int(expected_count):
        differences.append(f"row_count != {expected_count}")
    expected_first = expected.get("expected_first_row")
    if isinstance(expected_first, dict):
        if not rows:
            differences.append("missing expected first row")
        else:
            for key, value in expected_first.items():
                if rows[0].get(key) != value:
                    differences.append(f"first row {key} != {value!r}")
    expected_rows = expected.get("expected_rows")
    if isinstance(expected_rows, list):
        for index, expected_row in enumerate(expected_rows, start=1):
            if isinstance(expected_row, dict) and not any(
                all(row.get(key) == value for key, value in expected_row.items())
                for row in rows
            ):
                differences.append(f"missing expected row #{index}: {expected_row!r}")
    for column in expected.get("required_columns", []):
        if column not in columns:
            differences.append(f"missing expected fixture column: {column}")
    actual_kind = str(data.get("intent_plan", {}).get("analysis_kind") or "")
    expected_kind = str(
        expected.get("intent_response", {}).get("intent_plan", {}).get("analysis_kind") or ""
    )
    if actual_kind != expected_kind:
        differences.append(f"analysis_kind != {expected_kind!r}")
    return differences


def _execution_graph_errors(plan: dict[str, Any]) -> list[dict[str, Any]]:
    graph = _dict(plan.get("resolved_execution_graph"))
    errors = [deepcopy(item) for item in _dict_items(graph.get("validation_errors"))]
    jobs = _dict_items(plan.get("retrieval_jobs"))
    for requirement in _dict_items(graph.get("external_source_requirements")):
        if not bool(requirement.get("required", True)):
            continue
        source_alias = str(requirement.get("source_alias") or "").strip()
        dataset_key = str(requirement.get("dataset_key") or "").strip()
        matched = any(
            (not source_alias or str(job.get("source_alias") or "").strip() == source_alias)
            and (not dataset_key or str(job.get("dataset_key") or "").strip() == dataset_key)
            for job in jobs
        )
        if not matched:
            errors.append(
                {
                    "type": "unresolved_external_source_requirement",
                    "source_alias": source_alias,
                    "dataset_key": dataset_key,
                    "message": "execution graph requirement has no retrieval job",
                }
            )
    return errors


def _metric_binding_errors(plan: dict[str, Any], columns: list[str]) -> list[dict[str, Any]]:
    contract = _dict(plan.get("output_contract"))
    jobs = _dict_items(plan.get("retrieval_jobs"))
    errors: list[dict[str, Any]] = []
    for binding in _dict_items(contract.get("metric_bindings")):
        source_alias = str(binding.get("source_alias") or "").strip()
        dataset_key = str(binding.get("dataset_key") or "").strip()
        source_column = str(binding.get("source_column") or "").strip()
        output_column = str(binding.get("output_column") or "").strip()
        aggregation = str(binding.get("aggregation") or "").strip().lower()
        job = _resolve_binding_job(plan, jobs, source_alias, dataset_key)
        if job is None:
            errors.append(
                {
                    "type": "metric_binding_source_unresolved",
                    "source_alias": source_alias,
                    "dataset_key": dataset_key,
                    "output_column": output_column,
                    "message": "metric binding does not resolve to a retrieval job",
                }
            )
            continue
        if not source_column or not output_column:
            errors.append(
                {
                    "type": "invalid_metric_binding",
                    "source_alias": source_alias,
                    "source_column": source_column,
                    "output_column": output_column,
                    "message": "metric binding requires source_column and output_column",
                }
            )
            continue
        if output_column not in columns:
            errors.append(
                {
                    "type": "missing_bound_metric_result_column",
                    "output_column": output_column,
                    "message": "bound metric is missing from the result columns",
                }
            )
        semantics = _metric_semantics(job, source_column)
        if semantics:
            allowed = {
                str(value).strip().lower()
                for value in semantics.get("allowed_rollups", [])
                if str(value or "").strip()
            }
            if semantics.get("additive") is False and aggregation == "sum":
                errors.append(
                    {
                        "type": "non_additive_metric_sum",
                        "dataset_key": str(job.get("dataset_key") or ""),
                        "source_column": source_column,
                        "aggregation": aggregation,
                        "message": "non-additive metric cannot be summed",
                    }
                )
            elif allowed and aggregation and aggregation not in allowed:
                errors.append(
                    {
                        "type": "metric_aggregation_not_allowed",
                        "dataset_key": str(job.get("dataset_key") or ""),
                        "source_column": source_column,
                        "aggregation": aggregation,
                        "allowed_aggregations": sorted(allowed),
                        "message": "metric aggregation is not allowed by trusted metadata",
                    }
                )
    return errors


def _resolve_binding_job(
    plan: dict[str, Any],
    jobs: list[dict[str, Any]],
    source_alias: str,
    dataset_key: str,
) -> dict[str, Any] | None:
    direct = next(
        (
            item
            for item in jobs
            if (not source_alias or str(item.get("source_alias") or "").strip() == source_alias)
            and (not dataset_key or str(item.get("dataset_key") or "").strip() == dataset_key)
        ),
        None,
    )
    if direct is not None:
        return direct
    lineage_sources = _external_lineage_sources(plan, source_alias)
    candidates = [
        item
        for item in jobs
        if str(item.get("source_alias") or "").strip() in lineage_sources
        and (not dataset_key or str(item.get("dataset_key") or "").strip() == dataset_key)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _external_lineage_sources(plan: dict[str, Any], target_alias: str) -> set[str]:
    jobs = {
        str(item.get("source_alias") or "").strip()
        for item in _dict_items(plan.get("retrieval_jobs"))
        if str(item.get("source_alias") or "").strip()
    }
    if target_alias in jobs:
        return {target_alias}
    references: dict[str, set[str]] = {}
    for step in _dict_items(plan.get("pandas_execution_plan")):
        names = _unique_text([step.get("node_id"), step.get("output_alias")])
        refs = {
            str(item.get("ref") or "").strip()
            for item in _dict_items(step.get("inputs"))
            if str(item.get("ref") or "").strip()
        }
        refs.update(
            str(step.get(key) or "").strip()
            for key in ("source_alias", "left_source_alias", "right_source_alias")
            if str(step.get(key) or "").strip()
        )
        for name in names:
            references[name] = refs

    visited: set[str] = set()

    def resolve(alias: str) -> set[str]:
        if alias in jobs:
            return {alias}
        if not alias or alias in visited:
            return set()
        visited.add(alias)
        result: set[str] = set()
        for ref in references.get(alias, set()):
            result.update(resolve(ref))
        return result

    return resolve(target_alias)


def _result_shape_errors(
    plan: dict[str, Any], rows: list[dict[str, Any]], columns: list[str]
) -> list[dict[str, Any]]:
    contract = _dict(plan.get("output_contract"))
    if not contract:
        return []
    errors: list[dict[str, Any]] = []
    required = _unique_text(contract.get("required_columns"))
    grain = _unique_text(
        _dict(plan.get("resolved_output_grain_plan")).get("grain_columns")
        or contract.get("grain_columns")
    )
    if bool(contract.get("strict_result_columns")):
        missing = [column for column in required if column not in columns]
        if missing:
            errors.append(
                {
                    "type": "missing_required_result_columns",
                    "columns": missing,
                    "message": "strict output contract columns are missing",
                }
            )
    missing_grain = [column for column in grain if column not in columns]
    if missing_grain:
        errors.append(
            {
                "type": "missing_result_grain_columns",
                "columns": missing_grain,
                "message": "declared result grain columns are missing",
            }
        )
    if rows and grain and not missing_grain:
        keys = [tuple(_hashable(row.get(column)) for column in grain) for row in rows]
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        if duplicates:
            errors.append(
                {
                    "type": "duplicate_result_grain",
                    "grain_columns": grain,
                    "duplicate_key_count": len(duplicates),
                    "message": "result contains duplicate rows at the declared grain",
                }
            )
    return errors


def _ordering_errors(
    plan: dict[str, Any], rows: list[dict[str, Any]], columns: list[str]
) -> list[dict[str, Any]]:
    ordering = _dict(_dict(plan.get("output_contract")).get("ordering"))
    if not ordering:
        return []
    sort_by = str(ordering.get("sort_by") or "").strip()
    direction = str(ordering.get("order") or "asc").strip().lower()
    limit = _positive_int(ordering.get("limit"))
    errors: list[dict[str, Any]] = []
    if limit and len(rows) > limit:
        errors.append(
            {
                "type": "result_limit_exceeded",
                "limit": limit,
                "row_count": len(rows),
                "message": "result exceeds the declared top-N limit",
            }
        )
    if not sort_by:
        return errors
    if sort_by not in columns:
        errors.append(
            {
                "type": "missing_ordering_column",
                "sort_by": sort_by,
                "message": "declared ordering column is missing",
            }
        )
        return errors
    values = [row.get(sort_by) for row in rows]
    if not _is_monotonic(values, descending=direction == "desc"):
        errors.append(
            {
                "type": "result_order_violation",
                "sort_by": sort_by,
                "order": direction,
                "message": "result rows do not follow the declared ordering contract",
            }
        )
    return errors


def _temporal_contract_errors(plan: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = _dict_items(plan.get("retrieval_jobs"))
    errors: list[dict[str, Any]] = []
    for temporal in _dict_items(plan.get("temporal_semantics")):
        query_date = str(temporal.get("query_date") or "").strip()
        date_param = str(temporal.get("date_param") or "DATE").strip()
        if not query_date:
            continue
        source_alias = str(temporal.get("source_alias") or "").strip()
        dataset_key = str(temporal.get("dataset_key") or "").strip()
        job = next(
            (
                item
                for item in jobs
                if (not source_alias or str(item.get("source_alias") or "").strip() == source_alias)
                and (not dataset_key or str(item.get("dataset_key") or "").strip() == dataset_key)
            ),
            None,
        )
        if job is None:
            errors.append(
                {
                    "type": "temporal_source_unresolved",
                    "source_alias": source_alias,
                    "dataset_key": dataset_key,
                    "message": "temporal contract does not resolve to a retrieval job",
                }
            )
            continue
        actual = _temporal_job_value(job, date_param)
        if str(actual or "").strip() != query_date:
            errors.append(
                {
                    "type": "temporal_query_date_mismatch",
                    "source_alias": str(job.get("source_alias") or ""),
                    "date_param": date_param,
                    "expected_query_date": query_date,
                    "actual_query_date": actual,
                    "message": "retrieval date does not match temporal semantics",
                }
            )
    return errors


def _function_case_errors(plan: dict[str, Any], pandas_variables: Any) -> list[dict[str, Any]]:
    cases = _dict_items(plan.get("pandas_function_cases"))
    if not cases:
        return []
    steps = _dict_items(plan.get("pandas_execution_plan"))
    errors: list[dict[str, Any]] = []
    context = pandas_variables if isinstance(pandas_variables, dict) else {}
    helper_text = "\n".join(
        str(context.get(key) or "")
        for key in ("function_case_selection_json", "function_case_helper_code")
    )
    for case in cases:
        key = str(case.get("function_case_key") or case.get("key") or "").strip()
        name = str(case.get("function_name") or "").strip()
        source_alias = str(case.get("source_alias") or "").strip()
        matched = any(
            str(step.get("operation") or "").strip() == "apply_pandas_function_case"
            and (not key or str(step.get("function_case_key") or step.get("key") or "").strip() == key)
            and (not name or str(step.get("function_name") or "").strip() == name)
            and (not source_alias or str(step.get("source_alias") or "").strip() == source_alias)
            for step in steps
        )
        if not matched:
            errors.append(
                {
                    "type": "function_case_not_executed",
                    "function_case_key": key,
                    "function_name": name,
                    "source_alias": source_alias,
                    "message": "selected function case is absent from the execution plan",
                }
            )
        if context and name and name not in helper_text:
            errors.append(
                {
                    "type": "function_case_helper_missing",
                    "function_case_key": key,
                    "function_name": name,
                    "message": "selected function helper is absent from pandas context",
                }
            )
    return errors


def _certificate_errors(analysis: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    certificate = _dict(analysis.get("semantic_execution_certificate"))
    if not certificate:
        return []
    errors: list[dict[str, Any]] = []
    validation = str(certificate.get("postcondition_validation") or "").strip().lower()
    if validation and validation != "passed":
        errors.append(
            {
                "type": "semantic_postcondition_failed",
                "operation": certificate.get("operation"),
                "postcondition_validation": validation,
                "message": "semantic execution postcondition did not pass",
            }
        )
    result_count = certificate.get("result_row_count")
    if result_count is not None and int(result_count) != len(rows):
        errors.append(
            {
                "type": "semantic_certificate_row_count_mismatch",
                "certificate_row_count": int(result_count),
                "actual_row_count": len(rows),
                "message": "semantic certificate row count differs from the result",
            }
        )
    return errors


def _filters_cover(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for field, expected_condition in expected.items():
        if _normalized_name(field) in {"DATE", "WORKDATE"}:
            continue
        actual_key = next(
            (key for key in actual if _normalized_name(key) == _normalized_name(field)),
            None,
        )
        if actual_key is None or not _condition_equivalent(actual.get(actual_key), expected_condition):
            return False
    return True


def _condition_equivalent(actual: Any, expected: Any) -> bool:
    actual_dict = actual if isinstance(actual, dict) else {"operator": "eq", "value": actual}
    expected_dict = expected if isinstance(expected, dict) else {"operator": "eq", "value": expected}
    actual_operator = _normalized_operator(actual_dict.get("operator"))
    expected_operator = _normalized_operator(expected_dict.get("operator"))
    equivalent_single_value_operators = {actual_operator, expected_operator} == {"eq", "in"}
    if actual_operator != expected_operator and not equivalent_single_value_operators:
        return False
    actual_values = _condition_value_set(actual_dict.get("value"))
    expected_values = _condition_value_set(expected_dict.get("value"))
    if equivalent_single_value_operators and len(actual_values) != 1:
        return False
    return expected_values.issubset(actual_values)


def _condition_value_set(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {_normalized_value(item) for item in values if str(item or "").strip()}


def _metric_semantics(job: dict[str, Any], source_column: str) -> dict[str, Any]:
    semantics = _dict(job.get("metric_semantics"))
    normalized = _normalized_name(source_column)
    for key, value in semantics.items():
        if _normalized_name(key) == normalized and isinstance(value, dict):
            return value
    return {}


def _temporal_job_value(job: dict[str, Any], date_param: str) -> Any:
    normalized = _normalized_name(date_param)
    for container_key in ("required_params", "filters"):
        container = _dict(job.get(container_key))
        for key, value in container.items():
            if _normalized_name(key) != normalized:
                continue
            return value.get("value") if isinstance(value, dict) else value
    return ""


def _is_monotonic(values: list[Any], *, descending: bool) -> bool:
    comparable = [_sort_value(value) for value in values]
    pairs = zip(comparable, comparable[1:])
    return all(left >= right if descending else left <= right for left, right in pairs)


def _sort_value(value: Any) -> tuple[int, Any]:
    if value is None or str(value).strip() == "":
        return (0, "")
    try:
        return (2, float(value))
    except (TypeError, ValueError):
        return (1, str(value).casefold())


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _row_columns(rows: list[dict[str, Any]]) -> list[str]:
    return _unique_text(key for row in rows for key in row)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    return _dict_items(value)


def _unique_text(values: Any) -> list[str]:
    iterable = values if isinstance(values, (list, tuple, set)) else list(values) if values else []
    result: list[str] = []
    for value in iterable:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalized_name(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _normalized_value(value: Any) -> str:
    return str(value or "").strip().casefold()


def _normalized_operator(value: Any) -> str:
    text = str(value or "eq").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "==": "eq",
        "equals": "eq",
        "ge": "gte",
        ">=": "gte",
        "le": "lte",
        "<=": "lte",
        ">": "gt",
        "<": "lt",
        "startswith": "starts_with",
        "prefix": "starts_with",
        "notblank": "not_blank",
        "is_not_blank": "not_blank",
    }
    return aliases.get(text, text)


def _hashable(value: Any) -> Any:
    if isinstance(value, (dict, list, set, tuple)):
        return repr(value)
    return value


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        marker = repr(sorted(value.items(), key=lambda item: item[0]))
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
