from __future__ import annotations

import os
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_result_store"
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 24 * 7
TTL_INDEX_NAME = "agent_v4_result_store_expires_at_ttl"


def store_result(
    payload_value: Any,
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
    ttl_hours: Any = "",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    mongo_uri, mongo_database, collection_name = _resolve_config(mongo_uri, mongo_database, collection_name)
    next_payload = deepcopy(payload)
    if not mongo_uri:
        return _mark_skipped(next_payload, mongo_database, collection_name, "MONGODB_URI가 없어 분석 결과를 result store에 저장하지 않았습니다.")

    client = None
    data_ref = _build_data_ref(next_payload)
    ttl_hours_value = _effective_ttl_hours(ttl_hours)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours_value)
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(mongo_uri, serverSelectionTimeoutMS=5000)
        collection = client[mongo_database][collection_name]
        ttl_index_error = _ensure_ttl_index(collection)
        doc = {
            "_id": data_ref,
            "data_ref": data_ref,
            "session_id": str(next_payload.get("request", {}).get("session_id") or ""),
            "question": str(next_payload.get("request", {}).get("question") or ""),
            "created_at": _to_iso(now),
            "expires_at": expires_at,
            "expires_at_iso": _to_iso(expires_at),
            "ttl_hours": ttl_hours_value,
            "payload": {
                "request": _json_ready(next_payload.get("request", {})),
                "metadata_refs": _json_ready(next_payload.get("metadata_refs", [])),
                "intent_plan": _json_ready(next_payload.get("intent_plan", {})),
                "source_results": _json_ready(next_payload.get("source_results", [])),
                "runtime_sources": _json_ready(next_payload.get("runtime_sources", {})),
                "result_rows": _json_ready(_result_rows_for_store(next_payload)),
                "analysis": _json_ready(_compact_analysis_for_store(next_payload.get("analysis", {}))),
                "data": _json_ready(_compact_data_for_store(next_payload.get("data", {}))),
            },
        }
        collection.replace_one({"_id": data_ref}, doc, upsert=True)
        data_refs = _build_data_refs(next_payload, data_ref, mongo_database, collection_name)
        result_ref = data_refs[0] if data_refs else _data_ref_object(data_ref, mongo_database, collection_name, "payload.result_rows", "analysis_result", "분석 결과")
        next_payload.setdefault("data", {})["data_ref"] = result_ref
        next_payload["data_refs"] = data_refs
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
            "stage": "23_mongodb_result_store",
            "status": "ok",
            "database": mongo_database,
            "collection_name": collection_name,
            "data_ref": data_ref,
            "data_refs": data_refs,
            "ttl_hours": ttl_hours_value,
            "expires_at": _to_iso(expires_at),
            "errors": [],
        }
        if ttl_index_error:
            next_payload["trace"]["inspection"]["result_store"]["ttl_index_warning"] = ttl_index_error
        return next_payload
    except Exception as exc:
        return _mark_error(next_payload, mongo_database, collection_name, data_ref, [{"type": "mongo_write_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


def _resolve_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (
        mongo_uri or os.getenv("MONGODB_URI", ""),
        mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name or os.getenv("MONGODB_RESULT_COLLECTION", DEFAULT_COLLECTION),
    )


def _effective_ttl_hours(value: Any) -> int:
    text = str(value or "").strip()
    try:
        parsed = int(float(text)) if text else DEFAULT_TTL_HOURS
    except Exception:
        parsed = DEFAULT_TTL_HOURS
    return max(1, min(parsed, MAX_TTL_HOURS))


def _ensure_ttl_index(collection: Any) -> str:
    create_index = getattr(collection, "create_index", None)
    if not callable(create_index):
        return ""
    try:
        create_index([("expires_at", 1)], expireAfterSeconds=0, name=TTL_INDEX_NAME)
    except Exception as exc:
        return str(exc)
    return ""


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json_ready(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return None if value != value or value in (float("inf"), -float("inf")) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_ready(item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_ready(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item_value) for item_value in value]
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


def _result_rows_for_store(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("_full_result_rows")
    if rows is None:
        rows = payload.get("_runtime_result_rows")
    if rows is None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("rows", [])
    return rows if isinstance(rows, list) else []


def _compact_analysis_for_store(value: Any) -> dict[str, Any]:
    analysis = value if isinstance(value, dict) else {}
    keep_keys = (
        "status",
        "row_count",
        "columns",
        "analysis_code",
        "llm_generated_code",
        "pandas_filter_preamble",
        "used_helpers",
        "step_outputs",
        "function_case_results",
        "error",
    )
    return {key: deepcopy(analysis[key]) for key in keep_keys if key in analysis and key != "rows"}


def _compact_data_for_store(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    keep_keys = ("columns", "row_count", "data_ref")
    return {key: deepcopy(data[key]) for key in keep_keys if key in data and key != "rows"}


def _build_data_ref(payload: dict[str, Any]) -> str:
    existing = payload.get("data", {}).get("data_ref") if isinstance(payload.get("data"), dict) else ""
    if isinstance(existing, dict):
        existing = existing.get("ref_id") or existing.get("data_ref") or existing.get("_id") or ""
    if existing:
        return str(existing)
    session_id = str(payload.get("request", {}).get("session_id") or "session")
    return f"result:{session_id}:{uuid.uuid4().hex}"


def _build_data_refs(payload: dict[str, Any], ref_id: str, database: str, collection_name: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result_ref = _data_ref_object(
        ref_id,
        database,
        collection_name,
        "payload.result_rows",
        "analysis_result",
        "분석 결과 데이터",
        row_count=data.get("row_count"),
        columns=data.get("columns"),
    )
    refs = [result_ref]

    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    source_result_by_alias = _source_result_by_alias(payload.get("source_results"))
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


def _data_ref_object(
    ref_id: str,
    database: str,
    collection_name: str,
    path: str,
    role: str,
    label: str,
    **extra: Any,
) -> dict[str, Any]:
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
        if _has_value(value):
            result[key] = _json_ready(value)
    return result


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


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


def _mark_skipped(payload: dict[str, Any], database: str, collection_name: str, message: str) -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("warnings", []).append({"type": "missing_mongo_uri", "message": message})
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
        "stage": "23_mongodb_result_store",
        "status": "skipped",
        "database": database,
        "collection_name": collection_name,
        "data_ref": "",
        "errors": [{"type": "missing_mongo_uri", "message": message}],
    }
    return payload


def _mark_error(payload: dict[str, Any], database: str, collection_name: str, data_ref: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("errors", []).extend(errors)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
        "stage": "23_mongodb_result_store",
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


class MongoDBResultStore(Component):
    display_name = "23 MongoDB 결과 저장소"
    description = "pandas 분석 결과와 런타임 조회 결과를 MongoDB result store에 저장하고 data_ref를 페이로드에 남깁니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, advanced=True),
        MessageTextInput(name="collection_name", display_name="결과 컬렉션", required=False, advanced=True),
        MessageTextInput(name="ttl_hours", display_name="데이터 보관 시간(시간)", value=str(DEFAULT_TTL_HOURS), required=False, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(
            data=store_result(
                getattr(self, "payload", None),
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", ""),
                getattr(self, "collection_name", ""),
                getattr(self, "ttl_hours", ""),
            )
        )
