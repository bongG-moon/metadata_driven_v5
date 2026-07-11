"""Data retriever runtime with a dummy-first adapter path."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import ALLOWED_SOURCE_TYPES, ensure_dict, ensure_list, make_error
from .dummy_data import rows_for_dataset


PREVIEW_LIMIT = 5


def run_data_retriever(payload: dict[str, Any], table_catalog: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate, route, retrieve, merge, and adapt retrieval jobs."""

    working = deepcopy(payload)
    validation = validate_retrieval_jobs(working, table_catalog)
    working.setdefault("trace", {}).setdefault("warnings", []).extend(validation["warnings"])
    working.setdefault("trace", {}).setdefault("errors", []).extend(validation["errors"])
    jobs = validation["jobs"]
    routed = route_jobs_by_source(jobs)
    adapter_payloads = [dummy_retrieve(source_type, routed.get(source_type, []), table_catalog) for source_type in sorted(ALLOWED_SOURCE_TYPES)]
    merged = merge_source_results(working, adapter_payloads)
    return build_retrieval_runtime_sources(merged)


def validate_retrieval_jobs(
    payload: dict[str, Any],
    table_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    plan = ensure_dict(payload.get("intent_plan"))
    raw_jobs = ensure_list(plan.get("retrieval_jobs"))
    jobs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, raw_job in enumerate(raw_jobs):
        job = deepcopy(raw_job) if isinstance(raw_job, dict) else {}
        job_errors = []
        for field in ("dataset_key", "source_alias", "source_type"):
            if not job.get(field):
                job_errors.append(make_error("missing_retrieval_job_field", f"{field} is required", field=field, index=index))
        source_type = job.get("source_type")
        if source_type and source_type not in ALLOWED_SOURCE_TYPES:
            job_errors.append(make_error("unsupported_source_type", f"unsupported source_type: {source_type}", index=index))

        catalog_item = table_catalog.get(str(job.get("dataset_key", "")))
        if not catalog_item:
            job_errors.append(
                make_error(
                    "missing_table_catalog",
                    f"table catalog not found for dataset_key: {job.get('dataset_key')}",
                    dataset_key=job.get("dataset_key"),
                    index=index,
                )
            )
        else:
            payload_section = ensure_dict(catalog_item.get("payload", catalog_item))
            catalog_source_type = payload_section.get("source_type") or ensure_dict(payload_section.get("source_config")).get("source_type")
            if catalog_source_type and source_type and catalog_source_type != source_type:
                job_errors.append(
                    make_error(
                        "source_type_mismatch",
                        f"job source_type {source_type} does not match catalog source_type {catalog_source_type}",
                        dataset_key=job.get("dataset_key"),
                        index=index,
                    )
                )
            required_params = ensure_list(payload_section.get("required_params"))
            provided_params = ensure_dict(job.get("required_params"))
            for param in required_params:
                if param not in provided_params:
                    job_errors.append(
                        make_error(
                            "missing_required_param",
                            f"{param} is required for {job.get('dataset_key')}",
                            dataset_key=job.get("dataset_key"),
                            field=f"required_params.{param}",
                            index=index,
                        )
                    )

        if job_errors:
            errors.extend(job_errors)
            continue
        job.setdefault("job_id", f"job_{index + 1}")
        jobs.append(job)

    return {"jobs": jobs, "errors": errors, "warnings": warnings}


def route_jobs_by_source(jobs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    routed = {source_type: [] for source_type in ALLOWED_SOURCE_TYPES}
    for job in jobs:
        routed.setdefault(job["source_type"], []).append(job)
    return routed


def dummy_retrieve(
    source_type: str,
    jobs: list[dict[str, Any]],
    table_catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not jobs:
        return {
            "source_type": source_type,
            "status": "skipped",
            "skipped": True,
            "skip_reason": f"no {source_type} retrieval jobs",
            "executed_jobs": [],
            "source_results": [],
            "warnings": [],
            "errors": [],
        }

    source_results = []
    errors = []
    for job in jobs:
        catalog_item = table_catalog[job["dataset_key"]]
        catalog_payload = ensure_dict(catalog_item.get("payload", catalog_item))
        raw_rows = rows_for_dataset(job["dataset_key"])
        materialized = materialize_rows(raw_rows, job, catalog_payload)
        source_results.append(
            {
                "source_alias": job["source_alias"],
                "dataset_key": job["dataset_key"],
                "source_type": job["source_type"],
                "status": "ok",
                "row_count": len(materialized["rows"]),
                "columns": materialized["columns"],
                "preview_rows": materialized["rows"][:PREVIEW_LIMIT],
                "rows": materialized["rows"],
                "applied_params": job.get("required_params", {}),
                "applied_filters": materialized["applied_filters"],
                "data_ref": "",
                "source_execution": {
                    "used_dummy_data": True,
                    "elapsed_ms": 0,
                    "adapter": "dummy",
                    "declared_source_type": source_type,
                },
                "warnings": materialized["warnings"],
                "errors": [],
            }
        )
    return {
        "source_type": source_type,
        "status": "error" if errors else "ok",
        "skipped": False,
        "executed_jobs": [job["job_id"] for job in jobs],
        "source_results": source_results,
        "warnings": [],
        "errors": errors,
    }


def materialize_rows(
    rows: list[dict[str, Any]],
    job: dict[str, Any],
    catalog_payload: dict[str, Any],
) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    filtered = list(rows)
    filter_mappings = normalize_mapping(catalog_payload.get("filter_mappings", {}))
    applied_standard: dict[str, Any] = {}
    applied_physical: dict[str, Any] = {}

    for standard_key, condition in ensure_dict(job.get("filters")).items():
        values, operator = condition_values(condition)
        physical_columns = filter_mappings.get(standard_key, [standard_key])
        applied_standard[standard_key] = values if len(values) != 1 else values[0]
        for column in physical_columns:
            filtered = apply_filter(filtered, column, values, operator)
            applied_physical[column] = values if len(values) != 1 else values[0]

    param_mappings = normalize_mapping(catalog_payload.get("required_param_mappings", {}))
    for param, value in ensure_dict(job.get("required_params")).items():
        columns = param_mappings.get(param, [])
        # Required params are trace-only by default. Some live adapters use them
        # for query placeholders before rows are returned; dummy rows can also be
        # filtered when the mapped column exists.
        for column in columns:
            if filtered and column in filtered[0]:
                filtered = apply_filter(filtered, column, [value], "eq")
                applied_physical[column] = value

    aliased_rows, alias_map = apply_standard_aliases(filtered, catalog_payload)
    columns = sorted({column for row in aliased_rows for column in row})
    return {
        "rows": aliased_rows,
        "columns": columns,
        "applied_filters": {
            "standard": applied_standard,
            "physical": applied_physical,
            "column_aliases_applied": alias_map,
        },
        "warnings": warnings,
    }


def normalize_mapping(value: Any) -> dict[str, list[str]]:
    mapping = {}
    for key, mapped in ensure_dict(value).items():
        mapping[str(key)] = [str(item) for item in ensure_list(mapped)]
    return mapping


def condition_values(condition: Any) -> tuple[list[Any], str]:
    if isinstance(condition, dict):
        operator = condition.get("operator") or condition.get("op") or "eq"
        if "values" in condition:
            return ensure_list(condition.get("values")), operator
        if "value" in condition:
            return ensure_list(condition.get("value")), operator
        return [], operator
    return ensure_list(condition), "eq"


def apply_filter(rows: list[dict[str, Any]], column: str, values: list[Any], operator: str) -> list[dict[str, Any]]:
    if not values:
        return rows
    normalized_values = {str(value) for value in values}
    if operator in {"in", "eq"}:
        return [row for row in rows if str(row.get(column)) in normalized_values]
    if operator == "not_in":
        return [row for row in rows if str(row.get(column)) not in normalized_values]
    if operator == "contains":
        return [row for row in rows if any(value in str(row.get(column, "")) for value in normalized_values)]
    return rows


def apply_standard_aliases(
    rows: list[dict[str, Any]],
    catalog_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    alias_candidates = normalize_mapping(catalog_payload.get("standard_column_aliases", {}))
    # filter mappings are also useful aliases when the standard key differs from
    # the physical source column.
    for standard, physicals in normalize_mapping(catalog_payload.get("filter_mappings", {})).items():
        alias_candidates.setdefault(standard, physicals)

    applied: dict[str, str] = {}
    aliased_rows = []
    for row in rows:
        next_row = dict(row)
        for standard, physicals in alias_candidates.items():
            if standard in next_row:
                continue
            for physical in physicals:
                if physical in next_row:
                    next_row[standard] = next_row[physical]
                    applied[standard] = physical
                    break
        aliased_rows.append(next_row)
    return aliased_rows, applied


def merge_source_results(payload: dict[str, Any], adapter_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    merged = deepcopy(payload)
    source_results = []
    skipped_sources = []
    errors = []
    warnings = []
    for adapter_payload in adapter_payloads:
        if adapter_payload.get("skipped"):
            skipped_sources.append(
                {
                    "source_type": adapter_payload.get("source_type"),
                    "skip_reason": adapter_payload.get("skip_reason"),
                }
            )
            continue
        source_results.extend(adapter_payload.get("source_results", []))
        errors.extend(adapter_payload.get("errors", []))
        warnings.extend(adapter_payload.get("warnings", []))

    compact_results = []
    for result in source_results:
        compact = {key: value for key, value in result.items() if key != "rows"}
        compact_results.append(compact)

    merged["source_results"] = compact_results
    trace = merged.setdefault("trace", {})
    trace.setdefault("warnings", []).extend(warnings)
    trace.setdefault("errors", []).extend(errors)
    trace.setdefault("inspection", {})["data_retrieval"] = {
        "stage": "source_retrieval_merger",
        "status": "error" if errors else "ok",
        "executed_job_count": sum(len(item.get("executed_jobs", [])) for item in adapter_payloads),
        "sources": compact_results,
        "skipped_sources": skipped_sources,
    }
    merged["_source_rows_by_alias"] = {result["source_alias"]: result.get("rows", []) for result in source_results}
    return merged


def build_retrieval_runtime_sources(payload: dict[str, Any]) -> dict[str, Any]:
    next_payload = deepcopy(payload)
    next_payload["runtime_sources"] = next_payload.pop("_source_rows_by_alias", {})
    return next_payload


def strip_runtime_sources_for_final(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove full rows before final API/session state payload."""

    final_payload = deepcopy(payload)
    final_payload.pop("runtime_sources", None)
    final_payload.pop("_source_rows_by_alias", None)
    return final_payload
