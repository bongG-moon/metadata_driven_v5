# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 14 조회 페이로드 어댑터
# 역할: 소스 조회 결과 행을 pandas용 런타임 소스로 옮기고 요약 조회 결과를 유지합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 전체 행은 pandas 실행용 runtime_sources에 두고 LLM에는 schema와 작은 preview만 전달해 토큰 사용량을 줄입니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

RUNTIME_BUFFER_KEYS = {
    "runtime_sources",
    "_runtime_rows_by_alias",
    "_full_result_rows",
    "_runtime_result_rows",
}
SOURCE_SCHEMA_CONTRACT_VERSION = 1

# 주요 함수: 조회 행과 LLM용 요약을 분리하는 pandas 실행 직전 페이로드를 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_retrieval_payload(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    next_payload = payload
    if "_runtime_rows_by_alias" in next_payload:
        existing_sources = (
            next_payload.get("runtime_sources")
            if isinstance(next_payload.get("runtime_sources"), dict)
            else {}
        )
        retrieved_sources = next_payload.pop("_runtime_rows_by_alias", {})
        next_payload["runtime_sources"] = _merge_sources_by_alias(existing_sources, retrieved_sources)
    _standardize_runtime_source_columns(next_payload)
    _apply_metric_value_transforms(next_payload)
    _validate_runtime_source_schema(next_payload)
    return next_payload


# 함수 설명: `_merge_sources_by_alias()`는 MongoDB에서 복원한 upstream_result를 보존하고 같은 alias의 실제 신규 조회만 교체합니다.
def _merge_sources_by_alias(existing: dict[str, Any], additions: Any) -> dict[str, Any]:
    result = {str(alias): rows for alias, rows in existing.items() if str(alias or "").strip()}
    if not isinstance(additions, dict):
        return result
    for alias, rows in additions.items():
        text = str(alias or "").strip()
        if text:
            result[text] = rows
    return result


# 함수 설명: 조회된 실제 컬럼을 hydrated Table Catalog의 표준 key로 한 번만 바꾸고 이후 pandas 경로에는 표준 컬럼만 전달합니다.
def _standardize_runtime_source_columns(payload: dict[str, Any]) -> None:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    jobs_by_alias = {
        _source_alias(job): job
        for job in jobs
        if isinstance(job, dict) and _source_alias(job)
    }
    runtime_sources = (
        payload.get("runtime_sources")
        if isinstance(payload.get("runtime_sources"), dict)
        else {}
    )
    source_results = (
        payload.get("source_results")
        if isinstance(payload.get("source_results"), list)
        else []
    )
    result_by_alias = {
        _source_alias(item): item
        for item in source_results
        if isinstance(item, dict) and _source_alias(item)
    }

    reports: list[dict[str, Any]] = []
    total_conflicts: list[dict[str, Any]] = []
    for alias, rows in list(runtime_sources.items()):
        job = jobs_by_alias.get(str(alias))
        if not isinstance(job, dict) or not isinstance(rows, list):
            continue
        alias_contract = _canonical_alias_contract(job)
        if not alias_contract:
            observed_columns = _source_columns(rows, result_by_alias.get(str(alias)))
            reports.append(
                {
                    "source_alias": str(alias),
                    "dataset_key": str(job.get("dataset_key") or ""),
                    "status": "not_needed",
                    "observed_columns": observed_columns,
                    "runtime_columns": observed_columns,
                    "rename_map": {},
                    "conflict_count": 0,
                }
            )
            continue

        # `source_results.columns` is a compact schema projection and may
        # already contain canonical names even when the full runtime rows
        # still use physical names (for example EQUIP_MODEL/OPER_NM).  The
        # rows are the execution truth, so always inspect both sources and
        # decide whether standardization is needed from runtime columns.
        metadata_columns = _string_list(
            (result_by_alias.get(str(alias)) or {}).get("columns")
            if isinstance(result_by_alias.get(str(alias)), dict)
            else []
        )
        runtime_columns = _runtime_columns(rows)
        observed_columns = _merge_column_names(metadata_columns, runtime_columns)
        needs_standardization = any(
            alias_contract.get(_column_key(column), str(column)) != str(column)
            for column in runtime_columns
        )
        if not needs_standardization:
            source_result = result_by_alias.get(str(alias))
            if isinstance(source_result, dict):
                source_result["columns"] = _standardize_columns(
                    _merge_column_names(metadata_columns, runtime_columns),
                    alias_contract,
                )
            reports.append(
                {
                    "source_alias": str(alias),
                    "dataset_key": str(job.get("dataset_key") or ""),
                    "status": "not_needed",
                    "observed_columns": observed_columns,
                    "runtime_columns": runtime_columns or _standardize_columns(
                        metadata_columns,
                        alias_contract,
                    ),
                    "rename_map": {},
                    "conflict_count": 0,
                }
            )
            continue

        standardized_rows: list[Any] = []
        conflicts: list[dict[str, Any]] = []
        applied_map: dict[str, str] = {}
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                standardized_rows.append(row)
                continue
            standardized, row_map, row_conflicts = _standardize_row(
                row,
                alias_contract,
                row_index,
            )
            standardized_rows.append(standardized)
            applied_map.update(row_map)
            conflicts.extend(row_conflicts)
        runtime_sources[str(alias)] = standardized_rows

        source_result = result_by_alias.get(str(alias))
        if isinstance(source_result, dict):
            source_result["columns"] = _standardize_columns(
                _merge_column_names(metadata_columns, runtime_columns),
                alias_contract,
            )
            if isinstance(source_result.get("preview_rows"), list):
                preview_rows: list[Any] = []
                for row_index, row in enumerate(source_result["preview_rows"]):
                    if not isinstance(row, dict):
                        preview_rows.append(row)
                        continue
                    standardized, preview_map, preview_conflicts = _standardize_row(
                        row,
                        alias_contract,
                        row_index,
                    )
                    preview_rows.append(standardized)
                    applied_map.update(preview_map)
                    conflicts.extend(preview_conflicts)
                source_result["preview_rows"] = preview_rows

        conflicts = _deduplicate_conflicts(conflicts)
        report = {
            "source_alias": str(alias),
            "dataset_key": str(job.get("dataset_key") or ""),
            "status": "error" if conflicts else ("applied" if applied_map else "not_needed"),
            "observed_columns": observed_columns,
            "runtime_columns": _runtime_columns(runtime_sources.get(str(alias)))
            or _standardize_columns(metadata_columns, alias_contract),
            "rename_map": applied_map,
            "conflict_count": len(conflicts),
        }
        if conflicts:
            report["conflicts"] = conflicts[:20]
            total_conflicts.extend(
                {
                    "source_alias": str(alias),
                    "dataset_key": str(job.get("dataset_key") or ""),
                    **item,
                }
                for item in conflicts
            )
            if isinstance(source_result, dict):
                source_result["status"] = "error"
                source_result.setdefault("errors", []).append(
                    {
                        "type": "source_column_standardization_conflict",
                        "message": "표준 컬럼으로 연결된 실제 컬럼들의 값이 서로 충돌합니다.",
                        "conflicts": conflicts[:20],
                    }
                )
        reports.append(report)

    trace = payload.setdefault("trace", {})
    inspection = trace.setdefault("inspection", {})
    inspection["source_column_standardization"] = {
        "stage": "14_retrieval_payload_adapter",
        "status": "error" if total_conflicts else (
            "applied" if any(item.get("status") == "applied" for item in reports) else "not_needed"
        ),
        "policy": "table_catalog_canonical_columns_only",
        "sources": reports,
        "conflict_count": len(total_conflicts),
    }
    if total_conflicts:
        trace.setdefault("errors", []).append(
            {
                "type": "source_column_standardization_conflict",
                "message": "Pandas 실행 전에 표준 컬럼 단일화에 실패했습니다.",
                "conflicts": total_conflicts[:20],
            }
        )


# 함수 설명: 실제 runtime source에 필수 표준 컬럼이 모두 만들어졌는지 source별로 검증합니다.
# 매핑 선언의 존재가 아니라 변환 후 rows/columns를 기준으로 complete 여부를 결정합니다.
def _validate_runtime_source_schema(payload: dict[str, Any]) -> None:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)] if isinstance(plan.get("retrieval_jobs"), list) else []
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    source_results = payload.get("source_results") if isinstance(payload.get("source_results"), list) else []
    result_by_alias = {
        _source_alias(item): item
        for item in source_results
        if isinstance(item, dict) and _source_alias(item)
    }
    standardization = (
        payload.get("trace", {}).get("inspection", {}).get("source_column_standardization", {})
        if isinstance(payload.get("trace"), dict)
        and isinstance(payload.get("trace", {}).get("inspection"), dict)
        else {}
    )
    standardization_by_alias = {
        str(item.get("source_alias") or "").strip(): item
        for item in standardization.get("sources", [])
        if isinstance(item, dict) and str(item.get("source_alias") or "").strip()
    }
    required_by_alias = _required_runtime_columns_by_alias(plan, jobs)
    reports: list[dict[str, Any]] = []
    unresolved_sources: list[dict[str, Any]] = []

    for job in jobs:
        alias = _source_alias(job)
        if not alias:
            continue
        source_result = result_by_alias.get(alias)
        rows = runtime_sources.get(alias)
        if rows is None and not isinstance(source_result, dict):
            continue
        standardization_report = standardization_by_alias.get(alias, {})
        observed_columns = _string_list(standardization_report.get("observed_columns"))
        if not observed_columns:
            observed_columns = _source_columns(rows, source_result)
        # A non-empty runtime row set is authoritative for execution.  Do
        # not let a canonical `source_results.columns` projection hide a
        # physical runtime key; that would make the schema gate report
        # "complete" while the pandas DataFrame is still missing the key.
        runtime_columns = _runtime_columns(rows)
        if not runtime_columns:
            runtime_columns = _string_list(source_result.get("columns")) if isinstance(source_result, dict) else []
        required_columns = _string_list(required_by_alias.get(alias, []))
        runtime_keys = {_column_key(column) for column in runtime_columns}
        unresolved = [
            column
            for column in required_columns
            if _column_key(column) not in runtime_keys
        ]
        resolved_count = len(required_columns) - len(unresolved)
        if unresolved:
            status = "partial" if resolved_count else "unresolved"
        else:
            status = "complete"
        if str(standardization_report.get("status") or "").strip().lower() == "error":
            status = "conflict"

        report = {
            "source_alias": alias,
            "dataset_key": str(job.get("dataset_key") or ""),
            "status": status,
            "observed_columns": observed_columns,
            "source_bindings": _source_bindings(job, required_columns, observed_columns),
            "rename_map": deepcopy(standardization_report.get("rename_map") or {}),
            "runtime_columns": runtime_columns,
            "required_runtime_columns": required_columns,
            "unresolved_required_columns": unresolved,
        }
        reports.append(report)
        if isinstance(source_result, dict):
            source_result["columns_standardized"] = status == "complete"
            source_result["source_schema_contract"] = deepcopy(report)
        if status in {"partial", "unresolved"}:
            unresolved_sources.append(report)

    trace = payload.setdefault("trace", {})
    inspection = trace.setdefault("inspection", {})
    inspection["source_schema_resolution"] = {
        "stage": "14_retrieval_payload_adapter",
        "version": SOURCE_SCHEMA_CONTRACT_VERSION,
        "status": "error" if unresolved_sources else (
            "complete" if reports else "not_needed"
        ),
        "policy": "validate_required_canonical_columns_after_standardization",
        "sources": reports,
        "unresolved_source_count": len(unresolved_sources),
    }
    if unresolved_sources:
        trace.setdefault("errors", []).append(
            {
                "type": "source_schema_contract_unresolved",
                "message": "Pandas 실행 전에 필수 표준 컬럼 계약을 완성하지 못했습니다.",
                "sources": [
                    {
                        "source_alias": item["source_alias"],
                        "dataset_key": item["dataset_key"],
                        "unresolved_required_columns": item["unresolved_required_columns"],
                        "observed_columns": item["observed_columns"],
                        "source_bindings": item["source_bindings"],
                    }
                    for item in unresolved_sources
                ],
            }
        )


# 함수 설명: intent plan에서 source별로 pandas 실행 전에 존재해야 하는 직접 입력 컬럼을 수집합니다.
# 질문·dataset 이름을 하드코딩하지 않고 filter, step, metric binding, detail output 계약만 사용합니다.
def _required_runtime_columns_by_alias(
    plan: dict[str, Any],
    jobs: list[dict[str, Any]],
) -> dict[str, list[str]]:
    aliases = [_source_alias(job) for job in jobs if _source_alias(job)]
    required: dict[str, list[str]] = {alias: [] for alias in aliases}
    lineage: dict[str, set[str]] = {alias: {alias} for alias in aliases}

    for job in jobs:
        alias = _source_alias(job)
        for field in _filter_fields(job.get("filters")):
            _append_unique(required.setdefault(alias, []), field)

    steps = [item for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)] if isinstance(plan.get("pandas_execution_plan"), list) else []
    direct_operations = {
        "apply_filters", "filter", "filter_rows", "select_columns",
        "project_columns", "projection", "sort", "sort_and_top_n",
        "top_n", "bottom_n",
    }
    for step in steps:
        sources = _step_source_lineage(step, lineage)
        if len(sources) == 1:
            source_alias = next(iter(sources))
            for column in _step_direct_source_columns(step):
                _append_unique(required.setdefault(source_alias, []), column)
        for ref in (step.get("node_id"), step.get("output_alias")):
            text = str(ref or "").strip()
            if text and sources:
                lineage[text] = set(sources)

    output_contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    for binding in output_contract.get("metric_bindings", []) if isinstance(output_contract.get("metric_bindings"), list) else []:
        if not isinstance(binding, dict):
            continue
        alias = str(binding.get("source_alias") or "").strip()
        column = str(binding.get("source_column") or "").strip()
        if alias in required and column:
            _append_unique(required[alias], column)

    operations = {str(item.get("operation") or "").strip().lower() for item in steps}
    is_direct_single_source = len(aliases) == 1 and (
        not operations or operations.issubset(direct_operations)
    )
    result_mode = str(output_contract.get("result_mode") or "").strip().lower()
    if is_direct_single_source and result_mode in {"detail", "entity_list", ""}:
        alias = aliases[0]
        direct_output_columns = _string_list(output_contract.get("required_columns"))
        if not direct_output_columns and output_contract.get("strict_result_columns") is True:
            direct_output_columns = _string_list(output_contract.get("result_columns"))
        for column in direct_output_columns:
            _append_unique(required[alias], column)
        for column in _string_list(output_contract.get("metric_columns")):
            _append_unique(required[alias], column)
    return required


# 함수 설명: 한 pandas step이 참조하는 외부 source 계보를 node/output alias까지 따라가 계산합니다.
def _step_source_lineage(step: dict[str, Any], lineage: dict[str, set[str]]) -> set[str]:
    refs: list[str] = []
    for key in ("source_alias", "left_source_alias", "right_source_alias"):
        text = str(step.get(key) or "").strip()
        if text:
            refs.append(text)
    for item in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("ref") or item.get("source_alias") or "").strip()
        if text:
            refs.append(text)
    sources: set[str] = set()
    for ref in refs:
        sources.update(lineage.get(ref, {ref} if ref in lineage else set()))
    return sources


# 함수 설명: source DataFrame에서 직접 읽어야 하는 group, metric, projection 컬럼을 step에서 추출합니다.
def _step_direct_source_columns(step: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for column in _string_list(step.get("group_by")):
        _append_unique(result, column)
    for column in _string_list(step.get("columns") or step.get("result_columns")):
        _append_unique(result, column)
    for aggregation in step.get("aggregations", []) if isinstance(step.get("aggregations"), list) else []:
        if isinstance(aggregation, dict):
            _append_unique(result, str(aggregation.get("column") or "").strip())
    for key in ("agg_column", "source_column", "column"):
        _append_unique(result, str(step.get(key) or "").strip())
    return result


# 함수 설명: dict/list 형태의 filter 계약에서 canonical field 이름만 중복 없이 추출합니다.
def _filter_fields(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        items = value.items()
        for field, condition in items:
            if str(field or "").strip().lower() in {"and", "or", "any"} and isinstance(condition, list):
                for nested in condition:
                    for nested_field in _filter_fields(nested):
                        _append_unique(result, nested_field)
            else:
                _append_unique(result, str(field or "").strip())
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or item.get("column") or "").strip()
            if field:
                _append_unique(result, field)
            for nested_field in _filter_fields(item.get("values")):
                _append_unique(result, nested_field)
    return result


# 함수 설명: canonical 필수 컬럼이 실제 조회 schema의 어느 물리 컬럼과 연결됐는지 표시용 계약을 만듭니다.
def _source_bindings(
    job: dict[str, Any],
    required_columns: list[str],
    observed_columns: list[str],
) -> dict[str, str]:
    mapping = job.get("filter_mappings") if isinstance(job.get("filter_mappings"), dict) else {}
    observed_index = {_column_key(column): column for column in observed_columns}
    result: dict[str, str] = {}
    for canonical in required_columns:
        candidates = [canonical, *_string_list(mapping.get(canonical))]
        matched = next(
            (
                observed_index[_column_key(candidate)]
                for candidate in candidates
                if _column_key(candidate) in observed_index
            ),
            "",
        )
        if matched:
            result[canonical] = matched
    return result


# 함수 설명: runtime rows와 source result schema를 합쳐 순서를 보존한 실제 컬럼 목록을 만듭니다.
def _source_columns(rows: Any, source_result: Any) -> list[str]:
    columns = _string_list(source_result.get("columns")) if isinstance(source_result, dict) else []
    for column in _runtime_columns(rows):
        _append_unique(columns, column)
    return columns


# 함수 설명: 실제 runtime rows에서만 실행 컬럼을 추출해 model-facing schema와 구분합니다.
def _runtime_columns(rows: Any) -> list[str]:
    """Return columns observed in runtime rows, preserving first-seen order.

    `source_results.columns` is intentionally a compact, model-facing schema
    and is not guaranteed to describe the keys used by the full row buffer.
    Keeping this helper separate lets schema validation and standardization
    distinguish the two contracts without guessing a physical alias.
    """
    columns: list[str] = []
    if not isinstance(rows, list):
        return columns
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        for column in row:
            _append_unique(columns, str(column))
    return columns


# 함수 설명: metadata schema와 runtime 컬럼을 순서 보존·중복 제거 방식으로 합칩니다.
def _merge_column_names(*groups: Any) -> list[str]:
    """Merge schema and runtime column lists without duplicate names."""
    result: list[str] = []
    for group in groups:
        for column in _string_list(group):
            _append_unique(result, column)
    return result


# 함수 설명: 비어 있지 않은 문자열을 기존 순서를 보존하며 목록에 한 번만 추가합니다.
def _append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


# 함수 설명: Table Catalog의 metric_semantics.value_transform 계약을 pandas 실행 전에 한 번만 적용합니다.
# 변환은 dataset/컬럼 이름을 하드코딩하지 않고 hydrated retrieval job에 선언된 계약만 사용합니다.
def _apply_metric_value_transforms(payload: dict[str, Any]) -> None:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    jobs_by_alias = {
        _source_alias(job): job
        for job in jobs
        if isinstance(job, dict) and _source_alias(job)
    }
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    source_results = payload.get("source_results") if isinstance(payload.get("source_results"), list) else []
    result_by_alias = {
        _source_alias(item): item
        for item in source_results
        if isinstance(item, dict) and _source_alias(item)
    }

    reports: list[dict[str, Any]] = []
    total_errors: list[dict[str, Any]] = []
    for alias, rows in list(runtime_sources.items()):
        alias_text = str(alias)
        job = jobs_by_alias.get(alias_text)
        if not isinstance(job, dict) or not isinstance(rows, list):
            continue
        source_result = result_by_alias.get(alias_text)
        contracts, contract_errors = _metric_transform_contracts(job, rows, source_result)
        report: dict[str, Any] = {
            "source_alias": alias_text,
            "dataset_key": str(job.get("dataset_key") or ""),
            "status": "not_needed",
            "transforms": [],
            "error_count": len(contract_errors),
        }
        if contract_errors:
            report["status"] = "error"
            report["errors"] = contract_errors[:20]
            total_errors.extend(
                {"source_alias": alias_text, "dataset_key": str(job.get("dataset_key") or ""), **item}
                for item in contract_errors
            )
            _mark_metric_transform_error(source_result, contract_errors)
            reports.append(report)
            continue
        if not contracts:
            reports.append(report)
            continue

        applied_state = _applied_metric_transform_state(source_result)
        pending: list[dict[str, Any]] = []
        state_errors: list[dict[str, Any]] = []
        for contract in contracts:
            column_key = _column_key(contract["column"])
            previous = applied_state.get(column_key)
            if previous is None:
                pending.append(contract)
                report["transforms"].append({**contract, "status": "pending"})
                continue
            if _metric_transform_signature(previous) == _metric_transform_signature(contract):
                report["transforms"].append({**contract, "status": "already_applied"})
                continue
            state_errors.append(
                {
                    "type": "metric_value_transform_contract_changed",
                    "column": contract["column"],
                    "message": "Stored source rows were transformed with a different metric value contract.",
                }
            )

        if state_errors:
            report["status"] = "error"
            report["error_count"] = len(state_errors)
            report["errors"] = state_errors
            total_errors.extend(
                {"source_alias": alias_text, "dataset_key": str(job.get("dataset_key") or ""), **item}
                for item in state_errors
            )
            _mark_metric_transform_error(source_result, state_errors)
            reports.append(report)
            continue

        if not pending:
            report["status"] = "already_applied"
            reports.append(report)
            continue

        transformed_rows, row_stats, row_errors = _transform_metric_rows(rows, pending)
        preview_rows = source_result.get("preview_rows") if isinstance(source_result, dict) else None
        transformed_preview = preview_rows
        preview_errors: list[dict[str, Any]] = []
        if isinstance(preview_rows, list):
            transformed_preview, _, preview_errors = _transform_metric_rows(preview_rows, pending)
        combined_errors = _deduplicate_transform_errors([*row_errors, *preview_errors])
        if combined_errors:
            report["status"] = "error"
            report["error_count"] = len(combined_errors)
            report["errors"] = combined_errors[:20]
            total_errors.extend(
                {"source_alias": alias_text, "dataset_key": str(job.get("dataset_key") or ""), **item}
                for item in combined_errors
            )
            _mark_metric_transform_error(source_result, combined_errors)
            reports.append(report)
            continue

        runtime_sources[alias_text] = transformed_rows
        if isinstance(source_result, dict):
            if isinstance(transformed_preview, list):
                source_result["preview_rows"] = transformed_preview
            merged_state = {
                **applied_state,
                **{
                    _column_key(contract["column"]): {
                        "column": contract["column"],
                        "coerce_numeric": contract["coerce_numeric"],
                        "multiplier": contract["multiplier"],
                    }
                    for contract in pending
                },
            }
            source_result["metric_value_transforms_applied"] = list(merged_state.values())
        report["status"] = "applied"
        report["transforms"] = [
            {
                **item,
                "status": "applied" if _column_key(item["column"]) in {
                    _column_key(contract["column"]) for contract in pending
                } else item.get("status", "already_applied"),
                "converted_value_count": row_stats.get(_column_key(item["column"]), 0),
            }
            for item in contracts
        ]
        reports.append(report)

    trace = payload.setdefault("trace", {})
    inspection = trace.setdefault("inspection", {})
    inspection["metric_value_transformation"] = {
        "stage": "14_retrieval_payload_adapter",
        "status": "error" if total_errors else (
            "applied" if any(item.get("status") == "applied" for item in reports) else (
                "already_applied" if any(item.get("status") == "already_applied" for item in reports) else "not_needed"
            )
        ),
        "policy": "table_catalog_metric_value_transform_once_before_pandas",
        "sources": reports,
        "error_count": len(total_errors),
    }
    if total_errors:
        trace.setdefault("errors", []).append(
            {
                "type": "metric_value_transform_failed",
                "message": "Table Catalog metric value transformation failed before pandas execution.",
                "errors": total_errors[:20],
            }
        )


# 함수 설명: retrieval job의 metric_semantics에서 검증된 source 값 변환 계약과 대상 컬럼을 추출합니다.
def _metric_transform_contracts(
    job: dict[str, Any],
    rows: list[Any],
    source_result: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    semantics = job.get("metric_semantics") if isinstance(job.get("metric_semantics"), dict) else {}
    alias_contract = _canonical_alias_contract(job)
    available_columns = _available_source_columns(rows, source_result)
    contracts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for raw_metric, raw_semantics in semantics.items():
        if not isinstance(raw_semantics, dict) or not isinstance(raw_semantics.get("value_transform"), dict):
            continue
        transform = raw_semantics["value_transform"]
        metric = str(raw_metric or "").strip()
        canonical = alias_contract.get(_column_key(metric), metric)
        column = next(
            (item for item in available_columns if _column_key(item) == _column_key(canonical)),
            canonical,
        )
        multiplier = transform.get("multiplier", 1)
        coerce_numeric = transform.get("coerce_numeric", False)
        if isinstance(multiplier, bool):
            valid_multiplier = False
        else:
            try:
                multiplier = float(multiplier)
                valid_multiplier = math.isfinite(multiplier)
            except (TypeError, ValueError, OverflowError):
                valid_multiplier = False
        if not valid_multiplier or not isinstance(coerce_numeric, bool):
            errors.append(
                {
                    "type": "invalid_metric_value_transform_contract",
                    "column": metric,
                    "message": "value_transform requires a finite numeric multiplier and boolean coerce_numeric.",
                }
            )
            continue
        if rows and not any(_column_key(item) == _column_key(column) for item in available_columns):
            errors.append(
                {
                    "type": "metric_value_transform_column_missing",
                    "column": metric,
                    "message": "The metric column declared by value_transform is missing from the retrieved source.",
                }
            )
            continue
        contracts.append(
            {
                "column": column,
                "coerce_numeric": coerce_numeric,
                "multiplier": _compact_number(multiplier),
            }
        )
    return contracts, errors


# 함수 설명: 한 source의 행 복사본에 숫자 변환과 배수를 트랜잭션처럼 적용하고 오류·변환 건수를 반환합니다.
def _transform_metric_rows(
    rows: list[Any],
    contracts: list[dict[str, Any]],
) -> tuple[list[Any], dict[str, int], list[dict[str, Any]]]:
    transformed = deepcopy(rows)
    stats = {_column_key(item["column"]): 0 for item in contracts}
    errors: list[dict[str, Any]] = []
    for row_index, row in enumerate(transformed):
        if not isinstance(row, dict):
            continue
        for contract in contracts:
            column = contract["column"]
            actual_column = next(
                (item for item in row if _column_key(item) == _column_key(column)),
                None,
            )
            if actual_column is None or _conflict_value(row.get(actual_column)) is None:
                continue
            numeric = _numeric_metric_value(row.get(actual_column), contract["coerce_numeric"])
            if numeric is None:
                errors.append(
                    {
                        "type": "metric_value_transform_numeric_coercion_failed",
                        "column": column,
                        "row_index": row_index,
                        "message": "A nonblank metric value could not be converted to a number.",
                    }
                )
                continue
            row[actual_column] = _compact_number(numeric * float(contract["multiplier"]))
            stats[_column_key(column)] += 1
    return transformed, stats, errors


# 함수 설명: bool과 비유한값을 제외하고 쉼표가 포함된 문자열까지 계약에 따라 안전한 유한 숫자로 변환합니다.
def _numeric_metric_value(value: Any, coerce_numeric: bool) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if not coerce_numeric:
        return None
    text = str(value or "").strip().replace(",", "")
    try:
        numeric = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


# 함수 설명: 소수부가 없는 변환 결과는 int로 줄여 JSON과 pandas에서 불필요한 .0 표시를 방지합니다.
def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else float(value)


# 함수 설명: source 결과 schema와 실제 앞부분 행을 합쳐 변환 대상 컬럼 후보를 순서 보존 목록으로 만듭니다.
def _available_source_columns(rows: list[Any], source_result: dict[str, Any] | None) -> list[str]:
    columns = _string_list(source_result.get("columns")) if isinstance(source_result, dict) else []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        for column in row:
            text = str(column)
            if text not in columns:
                columns.append(text)
    return columns


# 함수 설명: 저장·복원되는 source result에서 이미 적용한 metric 변환 표식을 컬럼별로 복원합니다.
def _applied_metric_transform_state(source_result: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(source_result, dict):
        return {}
    items = source_result.get("metric_value_transforms_applied")
    if not isinstance(items, list):
        return {}
    return {
        _column_key(item.get("column")): item
        for item in items
        if isinstance(item, dict) and _column_key(item.get("column"))
    }


# 함수 설명: 후속 source 재사용 시 같은 변환인지 비교할 수 있도록 숫자 변환 여부와 배수를 signature로 만듭니다.
def _metric_transform_signature(value: dict[str, Any]) -> tuple[bool, float | None]:
    try:
        multiplier = float(value.get("multiplier"))
    except (TypeError, ValueError, OverflowError):
        multiplier = None
    return value.get("coerce_numeric") is True, multiplier


# 함수 설명: 부분 변환 데이터가 pandas로 진행되지 않도록 source result를 명시적인 조회 오류 상태로 전환합니다.
def _mark_metric_transform_error(source_result: dict[str, Any] | None, errors: list[dict[str, Any]]) -> None:
    if not isinstance(source_result, dict):
        return
    source_result["status"] = "error"
    source_result.setdefault("errors", []).append(
        {
            "type": "metric_value_transform_failed",
            "message": "Metric source values could not be normalized to the Table Catalog unit contract.",
            "errors": deepcopy(errors[:20]),
        }
    )


# 함수 설명: 전체 행과 preview에서 함께 발견된 동일 변환 오류를 유형·컬럼·행 기준으로 한 번만 남깁니다.
def _deduplicate_transform_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in errors:
        marker = (item.get("type"), item.get("column"), item.get("row_index"))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


# 함수 설명: filter_mappings만 실제 source alias -> 표준 실행 key 계약으로 역전합니다.
def _canonical_alias_contract(job: dict[str, Any]) -> dict[str, str]:
    mapping = job.get("filter_mappings")
    if not isinstance(mapping, dict):
        return {}
    candidates: dict[str, list[str]] = {}
    for raw_canonical, raw_aliases in mapping.items():
        canonical = str(raw_canonical or "").strip()
        aliases = _string_list(raw_aliases)
        if not canonical or not aliases:
            continue
        for alias in [canonical, *aliases]:
            alias_key = _column_key(alias)
            if alias_key and canonical not in candidates.setdefault(alias_key, []):
                candidates[alias_key].append(canonical)

    contract: dict[str, str] = {}
    for alias_key, choices in candidates.items():
        exact = [
            canonical
            for canonical in choices
            if _column_key(canonical) == alias_key
        ]
        if len({_column_key(item) for item in exact}) == 1:
            contract[alias_key] = exact[0]
            continue
        if len({_column_key(item) for item in choices}) == 1:
            contract[alias_key] = choices[0]
    return contract


# 함수 설명: 한 행의 같은 의미 컬럼을 표준 key 하나로 coalesce하고 실제 값 충돌은 숨기지 않습니다.
def _standardize_row(
    row: dict[str, Any],
    alias_contract: dict[str, str],
    row_index: int,
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    groups: dict[str, list[tuple[str, Any]]] = {}
    output_order: list[str] = []
    canonical_names: dict[str, str] = {}
    applied_map: dict[str, str] = {}
    for raw_column, value in row.items():
        column = str(raw_column)
        canonical = alias_contract.get(_column_key(column), column)
        target_key = _column_key(canonical)
        canonical_names.setdefault(target_key, canonical)
        groups.setdefault(target_key, []).append((column, value))
        if target_key not in output_order:
            output_order.append(target_key)
        if column != canonical:
            applied_map[column] = canonical

    result: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for target_key in output_order:
        canonical = canonical_names[target_key]
        values = groups[target_key]
        normalized_values = {
            _conflict_value(value)
            for _, value in values
            if _conflict_value(value) is not None
        }
        if len(normalized_values) > 1:
            conflicts.append(
                {
                    "row_index": row_index,
                    "canonical_column": canonical,
                    "source_columns": [column for column, _ in values],
                }
            )
        exact_nonblank = next(
            (
                value
                for column, value in values
                if _column_key(column) == target_key and _conflict_value(value) is not None
            ),
            None,
        )
        selected = exact_nonblank
        if selected is None:
            selected = next(
                (value for _, value in values if _conflict_value(value) is not None),
                values[0][1],
            )
        result[canonical] = selected
    return result, applied_map, conflicts


# 함수 설명: 빈 source에서도 schema가 표준 컬럼만 노출되도록 columns 목록을 같은 계약으로 변환합니다.
def _standardize_columns(value: Any, alias_contract: dict[str, str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for column in _string_list(value):
        canonical = alias_contract.get(_column_key(column), column)
        marker = _column_key(canonical)
        if marker and marker not in seen:
            seen.add(marker)
            result.append(canonical)
    return result


# 함수 설명: 같은 row/표준 컬럼 충돌을 preview와 전체 row 중복 없이 trace에 기록합니다.
def _deduplicate_conflicts(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in conflicts:
        marker = (
            item.get("row_index"),
            item.get("canonical_column"),
            tuple(item.get("source_columns", [])),
        )
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


# 함수 설명: null/blank는 보완 가능한 결측으로, 실제 값은 타입을 보존한 비교값으로 정규화합니다.
def _conflict_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    if text.casefold() in {"", "<na>", "empty", "nan", "nat", "none", "null"}:
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    return ("text", text)


# 함수 설명: source 결과와 조회 작업에서 동일한 alias 식별자를 추출합니다.
def _source_alias(value: dict[str, Any]) -> str:
    return str(value.get("source_alias") or value.get("dataset_key") or "").strip()


# 함수 설명: 대소문자와 구분문자 차이를 제거해 컬럼 비교용 키를 만듭니다.
def _column_key(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


# 함수 설명: 컬럼 선언값을 비어 있지 않은 중복 없는 문자열 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("column_name") or item.get("name") or item.get("column") or item.get("key")
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return {}
    payload = {
        key: deepcopy(item)
        for key, item in data.items()
        if key not in RUNTIME_BUFFER_KEYS
    }
    for key in RUNTIME_BUFFER_KEYS:
        if key in data:
            payload[key] = data[key]
    return payload


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class RetrievalPayloadAdapter(Component):
    display_name = "14 조회 페이로드 어댑터"
    description = "소스 조회 결과 행을 pandas용 런타임 소스로 옮기고 요약 조회 결과를 유지합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_retrieval_payload(getattr(self, "payload", None)))
