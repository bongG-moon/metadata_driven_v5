# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01E 종속 Table Catalog 후보 폐쇄기
# 역할: 질문 기반 후보가 축소된 뒤에도 선택 Domain이 명시한 dataset과
#       Catalog의 명시적 dataset 연결을 최대 5개 후보 안에서 보존합니다.
# 주요 입력: 01D metadata candidates, 전체 Table Catalog loader 결과
# 주요 출력: 종속 dataset 후보가 우선 보호된 metadata candidates
# 처리 원칙: 질문/공정명을 하드코딩하지 않고 저장된 dataset 참조만 사용합니다.
# 안전성: 연결 근거가 없으면 후보를 추가하지 않으며 최대 후보 수를 넘기지 않습니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_MAX_TABLE_ITEMS = 5
MAX_TABLE_ITEMS = 5

DOMAIN_DATASET_REFERENCE_KEYS = {
    "data_source",
    "dataset_key",
    "dataset_keys",
    "dataset_ref",
    "dataset_refs",
    "source_dataset",
    "source_datasets",
    "target_dataset",
    "target_datasets",
}

CATALOG_DATASET_LINK_KEYS = {
    "dependency_dataset",
    "dependency_dataset_key",
    "dependency_dataset_keys",
    "depends_on_dataset",
    "depends_on_datasets",
    "source_dataset",
    "source_dataset_key",
    "source_dataset_keys",
    "upstream_dataset",
    "upstream_dataset_key",
    "upstream_dataset_keys",
    "target_dataset",
    "target_dataset_key",
    "target_dataset_keys",
}

PRUNED_METADATA_KEYS = {
    "_id",
    "registration_trace",
    "raw_trace",
    "raw_text",
    "raw_text_preview",
    "refined_text",
    "review",
    "write_result",
    "llm_response",
    "existing_matches",
    "duplicate_decision",
    "created_by_prompt",
    "created_at",
    "updated_at",
    "text",
}

UNTRUSTED_TABLE_CONFIG_KEYS = {
    "query_template",
    "sql_template",
    "oracle_sql",
    "sql",
    "query",
    "endpoint",
    "url",
    "api_url",
    "headers",
    "credential",
    "credentials",
    "password",
    "token",
    "api_key",
}


# 주요 함수: 선택 Domain과 Catalog의 명시적 dataset 연결을 따라 후보 폐쇄를 계산합니다.
def close_dependency_catalog_candidates(
    metadata_candidates_value: Any,
    table_catalog_items_value: Any,
    *,
    max_table_items: Any = DEFAULT_MAX_TABLE_ITEMS,
) -> dict[str, Any]:
    """Protect explicit dependency catalogs before filling the remaining quota."""

    root = _payload(metadata_candidates_value)
    if not root:
        return {}
    candidates = (
        root.get("metadata_candidates")
        if isinstance(root.get("metadata_candidates"), dict)
        else root
    )
    if not isinstance(candidates, dict):
        return root

    limit = _bounded_limit(max_table_items)
    selected_tables = _items(candidates.get("table_catalog_items"))
    full_tables = _extract_items(table_catalog_items_value, "table_catalog_items")
    full_index = _catalog_index([*full_tables, *selected_tables])
    selected_index = _catalog_index(selected_tables)

    domain_refs = _ordered_dataset_references(
        _items(candidates.get("domain_items")),
        DOMAIN_DATASET_REFERENCE_KEYS,
    )
    closure_refs = _catalog_dependency_closure(domain_refs, full_index)
    protected_refs = [key for key in closure_refs if key in full_index]
    temporal_companion_refs = _temporal_family_companion_refs(
        selected_tables,
        full_tables,
    )

    next_tables: list[dict[str, Any]] = []
    included: set[str] = set()

    # Domain에 명시된 dataset과 그 Catalog 연결은 랭킹/temporal companion보다 우선합니다.
    for dataset_key in protected_refs:
        if len(next_tables) >= limit:
            break
        item = selected_index.get(dataset_key) or full_index.get(dataset_key)
        if not isinstance(item, dict) or dataset_key in included:
            continue
        next_tables.append(_sanitize_table_item(item))
        included.add(dataset_key)

    # 남은 슬롯에는 기존 relevance 후보와 같은 family의 다른 time scope를 먼저 함께 보존합니다.
    # 이 근거가 있어야 compiler가 현재/이력 Catalog 중 유일한 대체 대상을 안전하게 고를 수 있습니다.
    for dataset_key in temporal_companion_refs:
        if len(next_tables) >= limit:
            break
        item = selected_index.get(dataset_key) or full_index.get(dataset_key)
        if not isinstance(item, dict) or dataset_key in included:
            continue
        next_tables.append(_sanitize_table_item(item))
        included.add(dataset_key)

    # 남은 슬롯만 기존 01D relevance 순서로 채웁니다.
    for item in selected_tables:
        if len(next_tables) >= limit:
            break
        dataset_key = _dataset_key(item)
        if not dataset_key or dataset_key in included:
            continue
        next_tables.append(deepcopy(item))
        included.add(dataset_key)

    candidates["table_catalog_items"] = next_tables
    if root is not candidates:
        root["metadata_candidates"] = candidates

    missing_refs = [key for key in protected_refs if key not in included]
    unknown_refs = [key for key in closure_refs if key not in full_index]
    metadata_load = root.get("metadata_load") if isinstance(root.get("metadata_load"), dict) else {}
    metadata_load = deepcopy(metadata_load)
    selected_counts = (
        deepcopy(metadata_load.get("selected_counts"))
        if isinstance(metadata_load.get("selected_counts"), dict)
        else {}
    )
    selected_counts["table_catalog_items"] = len(next_tables)
    metadata_load["selected_counts"] = selected_counts
    metadata_load["dependency_catalog_closure"] = {
        "status": "complete" if not missing_refs and not unknown_refs else "incomplete",
        "max_table_items": limit,
        "domain_dataset_refs": domain_refs,
        "closure_dataset_refs": closure_refs,
        "included_dataset_refs": [key for key in closure_refs if key in included],
        "temporal_companion_refs": temporal_companion_refs,
        "included_temporal_companion_refs": [
            key for key in temporal_companion_refs if key in included
        ],
        "missing_due_to_limit": missing_refs,
        "unknown_dataset_refs": unknown_refs,
        "table_candidate_bytes": _json_bytes(next_tables),
    }
    root["metadata_load"] = metadata_load
    return root


# 함수 설명: 기존 후보의 구조화된 dataset_family와 time_scope를 따라 다른 시간 범위 Catalog를 우선 후보로 만듭니다.
def _temporal_family_companion_refs(
    selected_tables: list[dict[str, Any]],
    full_tables: list[dict[str, Any]],
) -> list[str]:
    full_index = _catalog_index(full_tables)
    ordered: list[str] = []
    seen: set[str] = set()
    for selected in selected_tables:
        dataset_key = _dataset_key(selected)
        payload = _catalog_payload(full_index.get(dataset_key) or selected)
        family = _catalog_dataset_family(payload)
        scope = _catalog_time_scope(payload)
        if not dataset_key:
            continue
        if dataset_key not in seen:
            ordered.append(dataset_key)
            seen.add(dataset_key)
        if not family or not scope:
            continue
        for candidate in full_tables:
            candidate_key = _dataset_key(candidate)
            candidate_payload = _catalog_payload(candidate)
            candidate_scope = _catalog_time_scope(candidate_payload)
            if (
                not candidate_key
                or candidate_key in seen
                or _catalog_dataset_family(candidate_payload) != family
                or not candidate_scope
                or candidate_scope == scope
            ):
                continue
            ordered.append(candidate_key)
            seen.add(candidate_key)
    return ordered


# 함수 설명: Catalog 본문에서 명시된 dataset family 식별자를 읽습니다.
def _catalog_dataset_family(payload: dict[str, Any]) -> str:
    criteria = payload.get("selection_criteria") if isinstance(payload.get("selection_criteria"), dict) else {}
    return str(payload.get("dataset_family") or criteria.get("dataset_family") or "").strip().casefold()


# 함수 설명: Catalog의 구조화된 시간 범위를 current_day/history 표준값으로 정규화합니다.
def _catalog_time_scope(payload: dict[str, Any]) -> str:
    criteria = payload.get("selection_criteria") if isinstance(payload.get("selection_criteria"), dict) else {}
    raw = str(criteria.get("time_scope") or payload.get("time_scope") or "").strip().casefold()
    if raw in {"current", "current_day", "today", "realtime", "current_time"}:
        return "current_day"
    if raw in {"history", "historical", "past", "as_of_date"}:
        return "history"
    return raw


# 함수 설명: 명시적인 Catalog dataset 링크를 양방향으로 따라 고정점 폐쇄를 만듭니다.
def _catalog_dependency_closure(
    initial_refs: list[str],
    catalog_index: dict[str, dict[str, Any]],
) -> list[str]:
    ordered = list(dict.fromkeys(initial_refs))
    seen = set(ordered)
    changed = True
    while changed:
        changed = False
        for dataset_key, item in catalog_index.items():
            links = _ordered_dataset_references([_catalog_payload(item)], CATALOG_DATASET_LINK_KEYS)
            if dataset_key in seen:
                additions = links
            elif any(link in seen for link in links):
                additions = [dataset_key]
            else:
                continue
            for addition in additions:
                if addition and addition not in seen:
                    ordered.append(addition)
                    seen.add(addition)
                    changed = True
    return ordered


# 함수 설명: 중첩 metadata에서 지정된 키가 소유한 dataset 식별자를 발견 순서대로 수집합니다.
def _ordered_dataset_references(
    items: list[dict[str, Any]],
    reference_keys: set[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    # 함수 설명: 단일 dataset 참조를 중복 없이 발견 순서에 추가합니다.
    def append(value: Any) -> None:
        if isinstance(value, dict):
            value = value.get("dataset_key") or value.get("key")
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)

    # 함수 설명: 중첩 dict/list를 순회하며 허용된 참조 키의 값만 수집합니다.
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key) in reference_keys:
                    for raw in nested if isinstance(nested, list) else [nested]:
                        append(raw)
                elif isinstance(nested, (dict, list)):
                    visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for item in items:
        visit(item.get("payload") if isinstance(item.get("payload"), dict) else item)
    return result


# 함수 설명: Catalog 목록을 dataset_key 기준으로 색인합니다.
def _catalog_index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        dataset_key = _dataset_key(item)
        if dataset_key and dataset_key not in result:
            result[dataset_key] = deepcopy(item)
    return result


# 함수 설명: wrapper와 payload 양쪽 형식에서 canonical dataset_key를 읽습니다.
def _dataset_key(item: dict[str, Any]) -> str:
    payload = _catalog_payload(item)
    return str(
        item.get("dataset_key")
        or item.get("key")
        or payload.get("dataset_key")
        or payload.get("key")
        or ""
    ).strip()


# 함수 설명: Catalog wrapper가 있으면 실제 payload를 반환합니다.
def _catalog_payload(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("payload") if isinstance(item.get("payload"), dict) else item


# 함수 설명: 전체 loader 또는 직접 list 입력에서 Catalog item 목록을 추출합니다.
def _extract_items(value: Any, key: str) -> list[dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, list):
        return [deepcopy(item) for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    raw = data.get(key)
    if not isinstance(raw, list) and isinstance(data.get("metadata_candidates"), dict):
        raw = data["metadata_candidates"].get(key)
    return [deepcopy(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


# 함수 설명: 새로 추가되는 전체 Catalog item에서 비밀/원문/SQL 필드를 제거합니다.
def _sanitize_table_item(item: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_value(item)
    return sanitized if isinstance(sanitized, dict) else {}


# 함수 설명: Catalog 값을 재귀 복사하면서 prompt 비노출 필드를 제거합니다.
def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            text = str(key)
            if text in PRUNED_METADATA_KEYS or text in UNTRUSTED_TABLE_CONFIG_KEYS:
                continue
            result[text] = _sanitize_value(nested)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return deepcopy(value)


# 함수 설명: Data/Message/dict 입력을 안전한 독립 dict로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: list 입력 중 object 항목만 복사합니다.
def _items(value: Any) -> list[dict[str, Any]]:
    return [deepcopy(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# 함수 설명: 사용자 입력값을 1~5 범위의 Table Catalog 후보 수로 제한합니다.
def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_TABLE_ITEMS
    return max(1, min(parsed, MAX_TABLE_ITEMS))


# 함수 설명: 후보 목록의 UTF-8 compact JSON 크기를 계산합니다.
def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


# Langflow 컴포넌트 클래스: 01D 결과와 전체 Catalog를 결합해 의존 후보 폐쇄를 적용합니다.
class DependencyCatalogCandidateClosure(Component):
    display_name = "01E 종속 Table Catalog 후보 폐쇄기"
    description = "Domain/Catalog에 명시된 dataset 의존 후보를 5개 한도 안에서 우선 보존합니다."
    inputs = [
        DataInput(name="metadata_candidates", display_name="메타데이터 후보", required=True),
        DataInput(name="table_catalog_items", display_name="전체 Table Catalog", required=True),
        MessageTextInput(
            name="max_table_items",
            display_name="테이블 최대 후보 수",
            value="5",
            advanced=True,
        ),
    ]
    outputs = [
        Output(
            name="metadata_candidates_out",
            display_name="폐쇄 적용 메타데이터 후보",
            method="build_payload",
        )
    ]

    # Langflow 출력 함수: 후보 폐쇄 결과를 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(
            data=close_dependency_catalog_candidates(
                getattr(self, "metadata_candidates", None),
                getattr(self, "table_catalog_items", None),
                max_table_items=getattr(self, "max_table_items", DEFAULT_MAX_TABLE_ITEMS),
            )
        )
