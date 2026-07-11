from __future__ import annotations

import os
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_main_flow_filters"
COLLECTION_ENV = "MONGODB_MAIN_FLOW_FILTER_COLLECTION"


def load_existing_items(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "", limit: str = "500") -> dict[str, Any]:
    mongo_uri, mongo_database, collection_name = _resolve_mongo_config(mongo_uri, mongo_database, collection_name)
    load_limit = _int(limit, 500)
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


def _resolve_mongo_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (
        mongo_uri or os.getenv("MONGODB_URI", ""),
        mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION),
    )


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
            "metadata_type": "main_flow_filter",
            "database": database,
            "collection_name": collection,
            "count": len(safe_items),
            "errors": errors,
        },
    }


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


class MainFlowFilterExistingItemsLoader(Component):
    display_name = "00 메인 플로우 필터 기존 항목 로더"
    description = "MongoDB에서 기존 메인 플로우 필터 메타데이터를 불러와 중복 검사에 사용합니다."
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True),
        MessageTextInput(name="limit", display_name="조회 제한", required=False, value="500", advanced=True),
    ]
    outputs = [Output(name="existing_items", display_name="기존 항목", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=load_existing_items(getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", ""), getattr(self, "limit", "500")))
