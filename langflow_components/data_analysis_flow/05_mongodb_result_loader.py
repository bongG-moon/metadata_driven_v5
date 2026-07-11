from __future__ import annotations

import os
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_result_store"
RESULT_PREVIEW_LIMIT = 50


def load_previous_result(payload_value: Any, mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    ref = _find_data_ref(payload)
    mongo_uri, mongo_database, collection_name = _resolve_config(mongo_uri, mongo_database, collection_name)
    next_payload = deepcopy(payload)
    if not ref:
        return _mark_skipped(next_payload, mongo_database, collection_name, "missing_data_ref", "data_ref가 없어 이전 결과를 불러오지 않았습니다.", add_warning=False)
    if not mongo_uri:
        return _mark_skipped(next_payload, mongo_database, collection_name, "missing_mongo_uri", "MONGODB_URI가 없어 이전 결과를 불러오지 않았습니다.", ref)

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(mongo_uri, serverSelectionTimeoutMS=5000)
        doc = client[mongo_database][collection_name].find_one({"_id": ref}, {"_id": 0}) or {}
        if not doc:
            return _mark_skipped(next_payload, mongo_database, collection_name, "result_not_found", "data_ref에 해당하는 이전 결과가 없습니다.", ref)
        stored_payload = doc.get("payload", {}) if isinstance(doc.get("payload"), dict) else {}
        for key in ("source_results", "runtime_sources", "analysis"):
            if key in stored_payload:
                next_payload[key] = deepcopy(stored_payload[key])
        next_payload["data"] = _restore_data_from_stored_payload(stored_payload)
        data_refs = _build_data_refs(stored_payload, ref, mongo_database, collection_name)
        next_payload.setdefault("data", {})["data_ref"] = data_refs[0] if data_refs else _data_ref_object(ref, mongo_database, collection_name, "payload.result_rows", "analysis_result", "분석 결과 데이터")
        next_payload["data_refs"] = data_refs
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
            "stage": "05_mongodb_result_loader",
            "status": "ok",
            "database": mongo_database,
            "collection_name": collection_name,
            "data_ref": ref,
            "data_refs": data_refs,
            "errors": [],
        }
        return next_payload
    except Exception as exc:
        return _mark_error(next_payload, mongo_database, collection_name, ref, [{"type": "mongo_load_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


def _resolve_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (
        mongo_uri or os.getenv("MONGODB_URI", ""),
        mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name or os.getenv("MONGODB_RESULT_COLLECTION", DEFAULT_COLLECTION),
    )


def _find_data_ref(payload: dict[str, Any]) -> str:
    data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
    state = payload.get("state", {}) if isinstance(payload.get("state"), dict) else {}
    current_data = state.get("current_data", {}) if isinstance(state.get("current_data"), dict) else {}
    for candidate in (data.get("data_ref"), current_data.get("data_ref"), state.get("data_ref")):
        ref = _ref_id(candidate)
        if ref:
            return ref
    return ""


def _ref_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("ref_id") or value.get("data_ref") or value.get("_id") or "").strip()
    return str(value or "").strip()


def _build_data_refs(stored_payload: dict[str, Any], ref_id: str, database: str, collection_name: str) -> list[dict[str, Any]]:
    data = _restore_data_from_stored_payload(stored_payload)
    refs = [
        _data_ref_object(
            ref_id,
            database,
            collection_name,
            "payload.result_rows",
            "analysis_result",
            "분석 결과 데이터",
            row_count=data.get("row_count"),
            columns=data.get("columns"),
        )
    ]
    runtime_sources = stored_payload.get("runtime_sources") if isinstance(stored_payload.get("runtime_sources"), dict) else {}
    source_result_by_alias = _source_result_by_alias(stored_payload.get("source_results"))
    for alias, rows in runtime_sources.items():
        if not isinstance(rows, list):
            continue
        source_result = source_result_by_alias.get(str(alias), {})
        refs.append(
            _data_ref_object(
                ref_id,
                database,
                collection_name,
                f"payload.runtime_sources.{alias}",
                "source_rows",
                f"사용 원본 데이터: {alias}",
                row_count=source_result.get("row_count") or len(rows),
                columns=_columns_from_rows(rows),
                source_alias=str(alias),
                dataset_key=source_result.get("dataset_key"),
                source_type=source_result.get("source_type"),
            )
        )
    return refs


def _data_ref_object(ref_id: str, database: str, collection_name: str, path: str, role: str, label: str, **extra: Any) -> dict[str, Any]:
    result = {
        "store": "mongodb",
        "ref_id": ref_id,
        "database": database,
        "collection_name": collection_name,
        "path": path,
        "role": role,
        "label": label,
    }
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            result[key] = deepcopy(value)
    return result


def _source_result_by_alias(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias:
            result[alias] = item
    return result


def _columns_from_rows(rows: list[Any]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def _restore_data_from_stored_payload(stored_payload: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(stored_payload.get("data")) if isinstance(stored_payload.get("data"), dict) else {}
    result_rows = stored_payload.get("result_rows") if isinstance(stored_payload.get("result_rows"), list) else []
    if result_rows and "rows" not in data:
        data["rows"] = deepcopy(result_rows[:RESULT_PREVIEW_LIMIT])
    if result_rows and "row_count" not in data:
        data["row_count"] = len(result_rows)
    if result_rows and not data.get("columns"):
        data["columns"] = _columns_from_rows(result_rows)
    return data


def _mark_skipped(
    payload: dict[str, Any],
    database: str,
    collection_name: str,
    error_type: str,
    message: str,
    data_ref: str = "",
    add_warning: bool = True,
) -> dict[str, Any]:
    if add_warning:
        payload.setdefault("trace", {}).setdefault("warnings", []).append({"type": error_type, "message": message})
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "skipped",
        "database": database,
        "collection_name": collection_name,
        "data_ref": data_ref,
        "errors": [{"type": error_type, "message": message}],
    }
    return payload


def _mark_error(payload: dict[str, Any], database: str, collection_name: str, data_ref: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("errors", []).extend(errors)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "error",
        "database": database,
        "collection_name": collection_name,
        "data_ref": data_ref,
        "errors": errors,
    }
    return payload


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


class MongoDBResultLoader(Component):
    display_name = "05 MongoDB 이전 결과 로더"
    description = "payload/state 안의 data_ref를 자동으로 찾아 MongoDB result store의 이전 분석 결과를 복원합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, advanced=True),
        MessageTextInput(name="collection_name", display_name="결과 컬렉션", required=False, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(
            data=load_previous_result(
                getattr(self, "payload", None),
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", ""),
                getattr(self, "collection_name", ""),
            )
        )
