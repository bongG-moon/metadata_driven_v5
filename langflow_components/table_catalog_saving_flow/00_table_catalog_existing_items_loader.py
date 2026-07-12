# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 테이블 카탈로그 기존 항목 로더
# 역할: MongoDB에서 기존 테이블 카탈로그 메타데이터를 불러와 중복 검사에 사용합니다.
# 주요 입력: MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 컬렉션 이름 (collection_name), 조회 제한 (limit)
# 주요 출력: 기존 항목 (existing_items)
# 처리 흐름: 테이블 카탈로그 등록 후보와 비교할 기존 문서를 MongoDB에서 최소 projection으로 읽고 registration trace 같은 불필요 필드를 제거합니다.
# 유지보수 포인트: 연결 설정은 노드 입력→환경변수→기본값 순으로 해석하며, 오류는 숨기지 않고 trace/status에 남기고 연결은 반드시 닫습니다.
# =============================================================================

from __future__ import annotations

import os
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_table_catalog_items"
COLLECTION_ENV = "MONGODB_TABLE_CATALOG_COLLECTION"


# 주요 함수: 등록 후보와 비교할 기존 MongoDB 문서를 최소 필드로 읽습니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def load_existing_items(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "", limit: str = "500") -> dict[str, Any]:
    mongo_uri, mongo_database, collection_name = _resolve_mongo_config(mongo_uri, mongo_database, collection_name)
    load_limit = _int(limit, 500)
    if load_limit == 0:
        return _result("skipped", [], mongo_database, collection_name, [])
    if not mongo_uri:
        return _result("skipped", [], mongo_database, collection_name, [{"type": "missing_mongo_uri", "message": "MONGODB_URI가 없어 기존 항목을 불러오지 않았습니다."}])
    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(mongo_uri, serverSelectionTimeoutMS=5000)
        docs = list(
            client[mongo_database][collection_name]
            .find({}, {"registration_trace": 0})
            .limit(load_limit)
        )
        return _result("ok", docs, mongo_database, collection_name, [])
    except Exception as exc:
        return _result("error", [], mongo_database, collection_name, [{"type": "mongo_load_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_resolve_mongo_config()`는 컴포넌트 입력→환경변수→기본값 순서로 MongoDB database와 collection 설정을 확정합니다.
def _resolve_mongo_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (
        mongo_uri or os.getenv("MONGODB_URI", ""),
        mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION),
    )


# 함수 설명: `_result()`는 현재 처리 상태·행·오류를 공통 source result 계약으로 묶습니다.
def _result(status: str, items: list[dict[str, Any]], database: str, collection: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    safe_items = []
    for item in items:
        safe_item = deepcopy(item)
        safe_item.pop("registration_trace", None)
        safe_items.append(safe_item)
    return {
        "existing_items": safe_items,
        "metadata_load": {
            "status": status,
            "metadata_type": "table_catalog",
            "database": database,
            "collection_name": collection,
            "count": len(safe_items),
            "errors": errors,
        },
    }


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return default


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TableCatalogExistingItemsLoader(Component):
    display_name = "00 테이블 카탈로그 기존 항목 로더"
    description = "MongoDB에서 기존 테이블 카탈로그 메타데이터를 불러와 중복 검사에 사용합니다."
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True),
        MessageTextInput(name="limit", display_name="조회 제한", required=False, value="500", advanced=True),
    ]
    outputs = [Output(name="existing_items", display_name="기존 항목", method="build_payload")]

    # Langflow 출력 함수: '기존 항목 (existing_items)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=load_existing_items(getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", ""), getattr(self, "limit", "500")))
