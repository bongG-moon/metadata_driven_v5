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
            reports.append(
                {
                    "source_alias": str(alias),
                    "dataset_key": str(job.get("dataset_key") or ""),
                    "status": "not_needed",
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
                source_result.get("columns"),
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


# 함수 설명: filter_mappings와 안전한 standard_column_aliases를 실제 alias -> 표준 key 계약으로 역전합니다.
def _canonical_alias_contract(job: dict[str, Any]) -> dict[str, str]:
    metric_source_keys = {
        _column_key(column)
        for column in (
            job.get("metric_semantics", {})
            if isinstance(job.get("metric_semantics"), dict)
            else {}
        )
        if str(column or "").strip()
    }
    candidates: dict[str, list[tuple[int, str]]] = {}
    for priority, mapping_name in enumerate(("filter_mappings", "standard_column_aliases")):
        mapping = job.get(mapping_name)
        if not isinstance(mapping, dict):
            continue
        for raw_canonical, raw_aliases in mapping.items():
            canonical = str(raw_canonical or "").strip()
            aliases = _string_list(raw_aliases)
            if not canonical or not aliases:
                continue
            if mapping_name == "standard_column_aliases" and any(
                _column_key(alias) in metric_source_keys
                and _column_key(alias) != _column_key(canonical)
                for alias in aliases
            ):
                continue
            for alias in [canonical, *aliases]:
                alias_key = _column_key(alias)
                if not alias_key:
                    continue
                item = (priority, canonical)
                if item not in candidates.setdefault(alias_key, []):
                    candidates[alias_key].append(item)

    contract: dict[str, str] = {}
    for alias_key, choices in candidates.items():
        best_priority = min(priority for priority, _ in choices)
        best = [canonical for priority, canonical in choices if priority == best_priority]
        exact = [
            canonical
            for canonical in best
            if _column_key(canonical) == alias_key
        ]
        if len({_column_key(item) for item in exact}) == 1:
            contract[alias_key] = exact[0]
            continue
        if len({_column_key(item) for item in best}) == 1:
            contract[alias_key] = best[0]
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
