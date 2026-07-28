# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00A 실시간 생산 공정그룹 카탈로그 로더
# 역할: Domain Metadata의 process_groups 항목만 읽어 LLM과 검증 Gate가 공유할 허용목록을 만듭니다.
# 주요 입력: MongoDB 연결 설정, 상태 필터, 조회 제한
# 주요 출력: domain.process_group.catalog.v1 Data
# 처리 흐름: MongoDB 문서 조회 -> process_groups만 선택 -> key/alias/process 표준화 -> 카탈로그 반환
# 유지보수 포인트: 공정그룹 원본은 datagov.agent_v4_domain_items의 active process_groups 항목으로 고정합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = "domain.process_group.catalog.v1"
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_domain_items"
DEFAULT_LIMIT = 200


# 함수 설명: `_text()`는 입력값을 공정그룹 메타데이터 비교에 사용할 앞뒤 공백 없는 문자열로 변환합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: `_bounded_int()`는 조회 제한값을 정수로 변환하고 허용 범위 안으로 제한합니다.
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(float(_text(value))) if _text(value) else default
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(lower, min(parsed, upper))


# 함수 설명: `_string_list()`는 문자열 또는 배열 입력을 순서가 유지되는 중복 없는 문자열 목록으로 정규화합니다.
def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.replace(";", ",").replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `normalize_process_groups()`는 원본 Domain 문서에서 활성 process_groups만 선택해 공통 카탈로그 형태로 바꿉니다.
def normalize_process_groups(items: list[dict[str, Any]], status_filter: str = "active") -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    groups: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    seen: set[str] = set()
    expected_status = _text(status_filter).lower()
    for item in items:
        section = _text(item.get("section"))
        if section and section != "process_groups":
            continue
        item_status = _text(item.get("status")).lower()
        if expected_status and expected_status != "all" and item_status and item_status != expected_status:
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        key = _text(item.get("key") or payload.get("key")).upper()
        display_name = _text(payload.get("display_name") or item.get("display_name") or key)
        field = _text(payload.get("field") or item.get("field") or "OPER_NAME")
        aliases = _string_list(payload.get("aliases") or item.get("aliases"))
        processes = _string_list(payload.get("processes") or item.get("processes"))
        if not key or not display_name or not processes:
            warnings.append(
                {
                    "type": "invalid_process_group_item",
                    "message": f"key/display_name/processes가 불완전한 항목을 제외했습니다: {_text(item.get('_id') or key or '-')}",
                }
            )
            continue
        if key in seen:
            warnings.append({"type": "duplicate_process_group_key", "message": f"중복 공정그룹 key를 제외했습니다: {key}"})
            continue
        seen.add(key)
        for value in (key, display_name):
            if value and value not in aliases:
                aliases.append(value)
        groups.append(
            {
                "key": key,
                "display_name": display_name,
                "aliases": aliases,
                "field": field,
                "processes": processes,
                "source_id": _text(item.get("_id")),
            }
        )
    groups.sort(key=lambda group: group["key"])
    return groups, warnings


# 함수 설명: `load_process_group_catalog()`는 MongoDB에서 공정그룹 문서를 읽어 표준 카탈로그 계약을 만듭니다.
def load_process_group_catalog(
    *,
    mongo_uri: Any = "",
    mongo_database: Any = DEFAULT_DATABASE,
    collection_name: Any = DEFAULT_COLLECTION,
    status_filter: Any = "active",
    limit: Any = DEFAULT_LIMIT,
) -> dict[str, Any]:
    database = _text(mongo_database) or DEFAULT_DATABASE
    collection = _text(collection_name) or DEFAULT_COLLECTION
    status = _text(status_filter) or "active"
    errors: list[dict[str, str]] = []
    raw_items: list[dict[str, Any]] = []

    uri = _text(mongo_uri)
    if not uri:
        errors.append({"type": "missing_mongo_uri", "message": "MongoDB 연결 URI가 비어 있어 공정그룹 카탈로그를 조회할 수 없습니다."})
    else:
        client = None
        try:
            mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
            client = mongo_client_cls(uri, serverSelectionTimeoutMS=5000)
            query: dict[str, Any] = {"section": "process_groups"}
            if status.lower() != "all":
                query["status"] = status
            cursor = client[database][collection].find(query).limit(
                _bounded_int(limit, DEFAULT_LIMIT, 1, 2_000)
            )
            raw_items = [deepcopy(item) for item in cursor if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001
            errors.append({"type": "process_group_catalog_load_error", "message": str(exc)})
        finally:
            if client is not None:
                client.close()

    groups, warnings = normalize_process_groups(raw_items, status)
    if not errors and not groups:
        errors.append({"type": "empty_process_group_catalog", "message": "활성 공정그룹 메타데이터가 없습니다."})
    return {
        "contract_version": CONTRACT_VERSION,
        "status": "ok" if not errors else "error",
        "source_type": "mongodb",
        "process_groups": groups,
        "candidate_count": len(groups),
        "source": {
            "database": database,
            "collection_name": collection,
            "status_filter": status,
        },
        "warnings": warnings,
        "errors": errors,
    }


# Langflow 컴포넌트 클래스: Domain Metadata의 공정그룹 항목을 LLM과 Gate가 공유하는 허용목록으로 제공합니다.
class RealtimeProductionProcessGroupCatalogLoader(Component):
    display_name = "00A 실시간 생산 공정그룹 카탈로그"
    description = "도메인 process_groups 메타데이터를 LLM 선택과 규칙 검증에 사용할 허용목록으로 만듭니다."
    name = "RealtimeProductionProcessGroupCatalogLoader"
    icon = "ListTree"
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=False),
        MessageTextInput(
            name="mongo_database",
            display_name="MongoDB 데이터베이스",
            value=DEFAULT_DATABASE,
            required=False,
            advanced=False,
        ),
        MessageTextInput(
            name="collection_name",
            display_name="도메인 컬렉션",
            value=DEFAULT_COLLECTION,
            required=False,
            advanced=False,
        ),
        MessageTextInput(
            name="status_filter",
            display_name="상태 필터",
            value="active",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="limit",
            display_name="공정그룹 조회 제한",
            value=str(DEFAULT_LIMIT),
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(
            name="process_group_catalog",
            display_name="공정그룹 카탈로그",
            method="build_catalog",
            types=["Data"],
        )
    ]

    # 함수 설명: `build_catalog()`는 현재 노드 설정으로 공정그룹 카탈로그를 조회하고 Langflow Data로 반환합니다.
    def build_catalog(self) -> Data:
        payload = load_process_group_catalog(
            mongo_uri=getattr(self, "mongo_uri", ""),
            mongo_database=getattr(self, "mongo_database", DEFAULT_DATABASE),
            collection_name=getattr(self, "collection_name", DEFAULT_COLLECTION),
            status_filter=getattr(self, "status_filter", "active"),
            limit=getattr(self, "limit", str(DEFAULT_LIMIT)),
        )
        self.status = f"공정그룹 {payload['candidate_count']:,}개 / {payload['status']}"
        return Data(data=payload)
