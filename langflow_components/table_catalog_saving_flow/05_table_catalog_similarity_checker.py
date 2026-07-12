# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 테이블 카탈로그 동일 Key 조회기
# 역할: 생성 후보가 정해진 뒤 해당 dataset_key만 MongoDB에서 조회하여 중복 payload를 최소화합니다.
# 주요 입력: 페이로드 (payload) · 필수, 기존 항목(호환용) (existing_items), MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스
#        (mongo_database), 컬렉션 이름 (collection_name)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 테이블 카탈로그 후보와 기존 문서의 canonical key 또는 허용된 identity 충돌을 찾아 저장 정책 결정에 필요한 match 정보를 만듭니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

import os
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_table_catalog_items"
COLLECTION_ENV = "MONGODB_TABLE_CATALOG_COLLECTION"


# 주요 함수: 신규 후보와 기존 문서의 정확 key 또는 identity 충돌을 판정합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def check_similarity(payload_value: Any, existing_items_value: Any = None, mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    items = [item for item in payload.get("items", []) if isinstance(item, dict)]
    existing = _items(existing_items_value) or _items(payload.get("existing_items"))
    existing_by_id = {_doc_id(item): item for item in existing if _doc_id(item)}
    missing_items = _missing_candidates(items, existing_by_id)
    load = {
        "status": "provided",
        "count": len(existing_by_id),
        "provided_count": len(existing_by_id),
        "queried_candidate_count": 0,
        "loaded_count": 0,
        "errors": [],
    }
    if missing_items:
        loaded, load = _load_candidates(missing_items, mongo_uri, mongo_database, collection_name)
        loaded_count = 0
        for item in loaded:
            doc_id = _doc_id(item)
            if doc_id:
                existing_by_id[doc_id] = item
                loaded_count += 1
        load["provided_count"] = len(existing)
        load["queried_candidate_count"] = len(missing_items)
        load["loaded_count"] = loaded_count
        load["count"] = len(existing_by_id)
    matches = []
    for item in items:
        doc_id = _doc_id(item)
        if doc_id and doc_id in existing_by_id:
            key = str(item.get("dataset_key") or "")
            matches.append({"new_key": key, "existing_key": key, "match_type": "same_key", "recommended_action": "merge", "reason": "같은 dataset_key가 이미 존재합니다.", "existing_item": deepcopy(existing_by_id[doc_id])})
    next_payload = payload
    next_payload.pop("existing_items", None)
    next_payload["existing_matches"] = matches
    next_payload["conflict_warnings"] = [{"severity": "blocker", "message": "같은 dataset_key가 있어 처리 방식 선택이 필요합니다.", "new_item_key": item["new_key"]} for item in matches]
    next_payload.setdefault("trace", {})["duplicate_lookup"] = load
    return next_payload


# 함수 설명: `_missing_candidates()`는 현재 전달된 기존 문서만으로 비교할 수 없어 추가 조회가 필요한 후보를 찾습니다.
def _missing_candidates(items: list[dict[str, Any]], existing_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    missing = []
    seen = set()
    for item in items:
        doc_id = _doc_id(item)
        if doc_id and doc_id not in existing_by_id and doc_id not in seen:
            missing.append(item)
            seen.add(doc_id)
    return missing


# 함수 설명: `_load_candidates()`는 후보 key에 해당하는 기존 MongoDB 문서만 추가 조회해 비교 범위를 최소화합니다.
def _load_candidates(items: list[dict[str, Any]], mongo_uri: str, mongo_database: str, collection_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    uri, database, collection = _resolve_mongo_config(mongo_uri, mongo_database, collection_name)
    if not uri:
        return [], {"status": "skipped", "database": database, "collection_name": collection, "count": 0, "errors": [{"type": "missing_mongo_uri", "message": "MONGODB_URI가 없어 동일 key 조회를 건너뛰었습니다."}]}
    client = None
    try:
        client = getattr(import_module("pymongo"), "MongoClient")(uri, serverSelectionTimeoutMS=5000)
        target = client[database][collection]
        docs = []
        for doc_id in dict.fromkeys(_doc_id(item) for item in items if _doc_id(item)):
            doc = target.find_one({"_id": doc_id})
            if isinstance(doc, dict):
                docs.append(deepcopy(doc))
        return docs, {"status": "ok", "database": database, "collection_name": collection, "count": len(docs), "errors": []}
    except Exception as exc:
        return [], {"status": "error", "database": database, "collection_name": collection, "count": 0, "errors": [{"type": "mongo_duplicate_lookup_error", "message": str(exc)}]}
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_resolve_mongo_config()`는 컴포넌트 입력→환경변수→기본값 순서로 MongoDB database와 collection 설정을 확정합니다.
def _resolve_mongo_config(mongo_uri: str, mongo_database: str, collection_name: str) -> tuple[str, str, str]:
    return (mongo_uri or os.getenv("MONGODB_URI", ""), mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE), collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION))


# 함수 설명: `_doc_id()`는 메타데이터 항목의 section/key 계약으로 canonical MongoDB 문서 ID를 계산합니다.
def _doc_id(item: dict[str, Any]) -> str:
    key = str(item.get("dataset_key") or item.get("key") or "").strip()
    return str(item.get("_id") or (f"table_catalog:{key}" if key else ""))


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_items()`는 Langflow 값이나 payload에서 저장·비교 대상 items 목록만 안전하게 꺼냅니다.
def _items(value: Any) -> list[dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("existing_items") or data.get("items") or []
    else:
        raw = []
    return [deepcopy(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TableCatalogSimilarityChecker(Component):
    display_name = "05 테이블 카탈로그 동일 Key 조회기"
    description = "생성 후보가 정해진 뒤 해당 dataset_key만 MongoDB에서 조회하여 중복 payload를 최소화합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), DataInput(name="existing_items", display_name="기존 항목(호환용)", required=False), MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True), MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True), MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=check_similarity(getattr(self, "payload", None), getattr(self, "existing_items", None), getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", "")))
