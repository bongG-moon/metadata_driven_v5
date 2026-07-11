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


def load_main_filter_metadata(
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
    limit: str = "1000",
    status_filter: str = "active",
) -> dict[str, Any]:
    config = _resolve_config(mongo_uri, mongo_database, collection_name)
    load_limit = _int(limit, 1000)
    if not config["mongo_uri"]:
        return _result("skipped", [], config, status_filter, [{"type": "missing_mongo_uri", "message": "MONGODB_URI가 없어 메인 필터를 불러오지 않았습니다."}])

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(config["mongo_uri"], serverSelectionTimeoutMS=5000)
        projection = {"_id": 0, "filter_key": 1, "key": 1, "status": 1, "payload": 1}
        docs = list(client[config["mongo_database"]][config["collection_name"]].find(_status_query(status_filter), projection).limit(load_limit))
        return _result("ok", [deepcopy(doc) for doc in docs if isinstance(doc, dict)], config, status_filter, [])
    except Exception as exc:
        return _result("error", [], config, status_filter, [{"type": "mongo_load_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


def _resolve_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, str]:
    return {
        "mongo_uri": mongo_uri or os.getenv("MONGODB_URI", ""),
        "mongo_database": mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        "collection_name": collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION),
    }


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


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


def _status_query(status_filter: str) -> dict[str, Any]:
    value = str(status_filter or "active").strip()
    if not value or value.lower() == "all":
        return {}
    return {"status": value}


class MetadataQaMainFilterLoader(Component):
    display_name = "01C 메타데이터 QA 메인 필터 로더"
    description = "MongoDB에서 메인 플로우 필터 메타데이터를 읽기 전용으로 불러옵니다."
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="collection_name", display_name="메인 필터 컬렉션", required=False, value=DEFAULT_COLLECTION, advanced=True),
        MessageTextInput(name="limit", display_name="조회 제한", required=False, value="1000", advanced=True),
        MessageTextInput(name="status_filter", display_name="상태 필터", required=False, value="active", advanced=True),
    ]
    outputs = [Output(name="main_flow_filters", display_name="메인 필터", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        result = load_main_filter_metadata(
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", ""),
            getattr(self, "collection_name", ""),
            getattr(self, "limit", "1000"),
            getattr(self, "status_filter", "active"),
        )
        load = result.get("metadata_load", {}) if isinstance(result, dict) else {}
        self.status = f"{load.get('status', 'unknown')} / {load.get('collection_name', DEFAULT_COLLECTION)} / {load.get('count', 0)}건"
        return Data(data=result)
