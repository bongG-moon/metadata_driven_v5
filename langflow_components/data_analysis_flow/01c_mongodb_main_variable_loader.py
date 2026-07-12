# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01C MongoDB 메인 변수 로더
# 역할: MongoDB에서 메인 변수/필터 메타데이터만 불러와 의도 분석 후보로 전달합니다.
# 주요 입력: MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 메인 변수 컬렉션 (collection_name), 조회 제한 (limit), 상태
#        필터 (status_filter)
# 주요 출력: 메인 변수 메타데이터 (main_flow_filters)
# 처리 흐름: 메인 필터 메타데이터를 읽어 질문의 공정·제품·기간 조건을 표준 필터로 해석할 수 있게 합니다.
# 유지보수 포인트: standalone Flow의 노드 입력으로 연결 설정을 받고, 오류는 숨기지 않고 trace/status에 남기며 연결은 반드시 닫습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_main_flow_filters"


# 주요 함수: 외부 저장소의 필요한 항목을 읽어 현재 페이로드에 안전하게 합칩니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def load_main_variable_metadata(
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
    limit: str = "1000",
    status_filter: str = "active",
) -> dict[str, Any]:
    config = _resolve_config(mongo_uri, mongo_database, collection_name)
    load_limit = _int(limit, 1000)
    if not config["mongo_uri"]:
        return _result("skipped", [], config, status_filter, [{"type": "missing_mongo_uri", "message": "MongoDB 연결 URI 노드 입력이 비어 있어 메인 변수 메타데이터를 불러오지 않았습니다."}])

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(config["mongo_uri"], serverSelectionTimeoutMS=5000)
        docs = list(client[config["mongo_database"]][config["collection_name"]].find(_status_query(status_filter), {"_id": 0}).limit(load_limit))
        return _result("ok", [deepcopy(doc) for doc in docs if isinstance(doc, dict)], config, status_filter, [])
    except Exception as exc:
        return _result("error", [], config, status_filter, [{"type": "mongo_load_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_resolve_config()`는 standalone 노드 입력과 코드 기본값만으로 실제 실행 설정을 확정합니다.
def _resolve_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, str]:
    return {
        "mongo_uri": str(mongo_uri or "").strip(),
        "mongo_database": str(mongo_database or DEFAULT_DATABASE).strip(),
        "collection_name": str(collection_name or DEFAULT_COLLECTION).strip(),
    }


# 함수 설명: `_result()`는 현재 처리 상태·행·오류를 공통 source result 계약으로 묶습니다.
def _result(status: str, items: list[dict[str, Any]], config: dict[str, str], status_filter: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "main_flow_filters": deepcopy(items),
        "metadata_load": {
            "status": status,
            "metadata_kind": "main_flow_filters",
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
class MongoDBMainVariableLoader(Component):
    display_name = "01C MongoDB 메인 변수 로더"
    description = "MongoDB에서 메인 변수/필터 메타데이터만 불러와 의도 분석 후보로 전달합니다."
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=False),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=False),
        MessageTextInput(name="collection_name", display_name="메인 변수 컬렉션", required=False, value=DEFAULT_COLLECTION, advanced=False),
        MessageTextInput(name="limit", display_name="조회 제한", required=False, value="1000", advanced=True),
        MessageTextInput(name="status_filter", display_name="상태 필터", required=False, value="active", advanced=True),
    ]
    outputs = [Output(name="main_flow_filters", display_name="메인 변수 메타데이터", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '메인 변수 메타데이터 (main_flow_filters)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=load_main_variable_metadata(
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", ""),
                getattr(self, "collection_name", ""),
                getattr(self, "limit", "1000"),
                getattr(self, "status_filter", "active"),
            )
        )
