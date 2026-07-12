# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01A 메타데이터 QA 도메인 로더
# 역할: MongoDB에서 도메인 메타데이터를 읽기 전용으로 불러옵니다.
# 주요 입력: MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 도메인 컬렉션 (collection_name), 조회 제한 (limit), 상태
#        필터 (status_filter)
# 주요 출력: 도메인 메타데이터 (domain_items)
# 처리 흐름: 도메인 용어·별칭·공정 그룹처럼 질문 해석에 필요한 활성 도메인 문서를 읽습니다.
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
DEFAULT_COLLECTION = "agent_v4_domain_items"
COLLECTION_ENV = "MONGODB_DOMAIN_COLLECTION"


# 주요 함수: 외부 저장소의 필요한 항목을 읽어 현재 페이로드에 안전하게 합칩니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def load_domain_metadata(
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
    limit: str = "1000",
    status_filter: str = "active",
) -> dict[str, Any]:
    config = _resolve_config(mongo_uri, mongo_database, collection_name)
    load_limit = _int(limit, 1000)
    if not config["mongo_uri"]:
        return _result("skipped", [], config, status_filter, [{"type": "missing_mongo_uri", "message": "MONGODB_URI가 없어 도메인 메타데이터를 불러오지 않았습니다."}])

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(config["mongo_uri"], serverSelectionTimeoutMS=5000)
        projection = {"_id": 0, "section": 1, "key": 1, "status": 1, "payload": 1}
        docs = list(client[config["mongo_database"]][config["collection_name"]].find(_status_query(status_filter), projection).limit(load_limit))
        return _result("ok", [deepcopy(doc) for doc in docs if isinstance(doc, dict)], config, status_filter, [])
    except Exception as exc:
        return _result("error", [], config, status_filter, [{"type": "mongo_load_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_resolve_config()`는 노드 입력·환경변수·카탈로그 기본값의 우선순위로 실제 실행 설정을 확정합니다.
def _resolve_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, str]:
    return {
        "mongo_uri": mongo_uri or os.getenv("MONGODB_URI", ""),
        "mongo_database": mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        "collection_name": collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION),
    }


# 함수 설명: `_result()`는 현재 처리 상태·행·오류를 공통 source result 계약으로 묶습니다.
def _result(status: str, items: list[dict[str, Any]], config: dict[str, str], status_filter: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "domain_items": deepcopy(items),
        "metadata_load": {
            "status": status,
            "metadata_kind": "domain_items",
            "database": config["mongo_database"],
            "collection_name": config["collection_name"],
            "count": len(items),
            "status_filter": status_filter or "active",
            "errors": errors,
        },
    }


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


# 함수 설명: `_status_query()`는 active/all 선택에 맞는 MongoDB status 조회 조건을 만듭니다.
def _status_query(status_filter: str) -> dict[str, Any]:
    value = str(status_filter or "active").strip()
    if not value or value.lower() == "all":
        return {}
    return {"status": value}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataQaDomainMetadataLoader(Component):
    display_name = "01A 메타데이터 QA 도메인 로더"
    description = "MongoDB에서 도메인 메타데이터를 읽기 전용으로 불러옵니다."
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="collection_name", display_name="도메인 컬렉션", required=False, value=DEFAULT_COLLECTION, advanced=True),
        MessageTextInput(name="limit", display_name="조회 제한", required=False, value="1000", advanced=True),
        MessageTextInput(name="status_filter", display_name="상태 필터", required=False, value="active", advanced=True),
    ]
    outputs = [Output(name="domain_items", display_name="도메인 메타데이터", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '도메인 메타데이터 (domain_items)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        result = load_domain_metadata(
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", ""),
            getattr(self, "collection_name", ""),
            getattr(self, "limit", "1000"),
            getattr(self, "status_filter", "active"),
        )
        load = result.get("metadata_load", {}) if isinstance(result, dict) else {}
        self.status = f"{load.get('status', 'unknown')} / {load.get('collection_name', DEFAULT_COLLECTION)} / {load.get('count', 0)}건"
        return Data(data=result)
