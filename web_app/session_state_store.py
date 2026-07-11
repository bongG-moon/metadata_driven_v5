from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from typing import Any


DEFAULT_SESSION_COLLECTION = "agent_v4_session_states"
DEFAULT_PREVIEW_ROW_LIMIT = 5
DEFAULT_HISTORY_LIMIT = 10


@dataclass(frozen=True)
class MongoSessionStateSettings:
    enabled: bool = False
    mongo_uri: str = ""
    mongo_database: str = "datagov"
    collection_name: str = DEFAULT_SESSION_COLLECTION
    preview_row_limit: int = DEFAULT_PREVIEW_ROW_LIMIT
    history_limit: int = DEFAULT_HISTORY_LIMIT


class MongoDBSessionStateStore:
    def __init__(self, settings: MongoSessionStateSettings) -> None:
        self.settings = settings
        self.last_load_status: dict[str, Any] = {}
        self.last_write_status: dict[str, Any] = {}

    def load_state(self, session_id: str) -> dict[str, Any]:
        session = str(session_id or "demo-session")
        status = {
            "enabled": self.settings.enabled,
            "loaded": False,
            "source": "disabled" if not self.settings.enabled else "mongodb",
            "session_id": session,
            "collection_name": self.settings.collection_name,
            "errors": [],
        }
        if not self.settings.enabled:
            self.last_load_status = status
            return {}
        if not self.settings.mongo_uri:
            status["errors"] = ["Mongo URI is empty."]
            self.last_load_status = status
            return {}
        client = None
        try:
            client, collection = self._collection()
            document = collection.find_one({"_id": _document_id(session)}) or collection.find_one({"session_id": session})
            if not isinstance(document, dict):
                status["source"] = "mongodb_not_found"
                self.last_load_status = status
                return {}
            state = document.get("state") if isinstance(document.get("state"), dict) else {}
            compact = _compact_state(state, self.settings.preview_row_limit, self.settings.history_limit)
            status.update(
                {
                    "loaded": bool(compact),
                    "source": "mongodb",
                    "updated_at": document.get("updated_at", ""),
                    "turn_count": document.get("turn_count", 0),
                }
            )
            self.last_load_status = status
            return compact
        except Exception as exc:
            status["errors"] = [str(exc)]
            self.last_load_status = status
            return {}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def save_state(
        self,
        session_id: str,
        state: dict[str, Any] | None,
        question: str = "",
        response_type: str = "",
    ) -> dict[str, Any]:
        session = str(session_id or "demo-session")
        status = {
            "enabled": self.settings.enabled,
            "saved": False,
            "session_id": session,
            "collection_name": self.settings.collection_name,
            "errors": [],
        }
        if not self.settings.enabled:
            status["reason"] = "disabled"
            self.last_write_status = status
            return status
        if not isinstance(state, dict) or not state:
            status["reason"] = "state_not_found"
            self.last_write_status = status
            return status
        if not self.settings.mongo_uri:
            status["errors"] = ["Mongo URI is empty."]
            self.last_write_status = status
            return status
        compact = _compact_state(state, self.settings.preview_row_limit, self.settings.history_limit)
        client = None
        try:
            client, collection = self._collection()
            previous = collection.find_one({"_id": _document_id(session)}) or {}
            turn_count = _positive_int(previous.get("turn_count") if isinstance(previous, dict) else 0, 0) + 1
            document = {
                "_id": _document_id(session),
                "session_id": session,
                "state_version": "agent-v1",
                "state": compact,
                "last_question": str(question or ""),
                "last_response_type": str(response_type or ""),
                "turn_count": turn_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            collection.replace_one({"_id": document["_id"]}, document, upsert=True)
            status.update({"saved": True, "turn_count": turn_count, "state_keys": sorted(compact.keys())})
            self.last_write_status = status
            return status
        except Exception as exc:
            status["errors"] = [str(exc)]
            self.last_write_status = status
            return status
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _collection(self) -> tuple[Any, Any]:
        pymongo = import_module("pymongo")
        client = pymongo.MongoClient(self.settings.mongo_uri, serverSelectionTimeoutMS=5000)
        return client, client[self.settings.mongo_database][self.settings.collection_name]


def _compact_state(state: dict[str, Any], preview_limit: int, history_limit: int) -> dict[str, Any]:
    result = deepcopy(state)
    result.pop("runtime_sources", None)
    if isinstance(result.get("chat_history"), list):
        result["chat_history"] = deepcopy(result["chat_history"][-history_limit:]) if history_limit else []
    else:
        result["chat_history"] = []
    result["context"] = dict(result.get("context", {})) if isinstance(result.get("context"), dict) else {}
    result["current_data"] = _compact_current_data(result.get("current_data"), preview_limit)
    result["followup_source_results"] = [
        _compact_source_result(item, preview_limit)
        for item in result.get("followup_source_results", [])
        if isinstance(item, dict)
    ]
    if not isinstance(result.get("runtime_source_refs"), dict):
        result.pop("runtime_source_refs", None)
    return _json_ready(result)


def _compact_current_data(current_data: Any, preview_limit: int) -> dict[str, Any]:
    if not isinstance(current_data, dict):
        return {}
    result = deepcopy(current_data)
    rows = _rows_from(result)
    row_count = _positive_int(result.get("row_count"), len(rows))
    if rows:
        result["rows"] = deepcopy(rows[:preview_limit])
        result.pop("data", None)
        result["data_is_preview"] = row_count > len(result["rows"])
        if isinstance(result.get("data_ref"), dict):
            result.setdefault("data_ref_loaded", False)
            result.setdefault("data_ref_load_mode", "preview")
    result["row_count"] = row_count
    columns = result.get("columns") if isinstance(result.get("columns"), list) else []
    if not columns:
        columns = _rows_columns(rows)
    result["columns"] = columns
    product_key_columns = [str(item) for item in result.get("product_key_columns", []) if str(item or "").strip()] if isinstance(result.get("product_key_columns"), list) else []
    result["product_key_columns"] = product_key_columns
    product_key_values = result.get("product_key_values") if isinstance(result.get("product_key_values"), list) else []
    if not product_key_values and product_key_columns:
        product_key_values = _product_key_values(_rows_from(result), product_key_columns)
    result["product_key_values"] = deepcopy(product_key_values)
    result["product_key_count"] = _positive_int(result.get("product_key_count"), len(product_key_values))
    if not isinstance(result.get("source_dataset_keys"), list):
        result["source_dataset_keys"] = []
    if not isinstance(result.get("source_aliases"), list):
        result["source_aliases"] = []
    return result


def _compact_source_result(source: dict[str, Any], preview_limit: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "source_alias",
        "dataset_key",
        "source_type",
        "columns",
        "data_ref",
        "row_count",
        "data_is_reference",
        "data_is_preview",
        "applied_params",
        "applied_filters",
    ):
        if source.get(key) not in (None, "", [], {}):
            result[key] = deepcopy(source[key])
    rows = _rows_from(source)
    if rows and not isinstance(result.get("data_ref"), dict):
        result["rows"] = deepcopy(rows[:preview_limit])
        result["row_count"] = _positive_int(result.get("row_count"), len(rows))
        result["data_is_preview"] = len(rows) > preview_limit
    return result


def _rows_from(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("rows")
    if not isinstance(rows, list):
        rows = value.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def _product_key_values(rows: list[dict[str, Any]], product_key_columns: list[str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in rows:
        product = {key: row.get(key) for key in product_key_columns if row.get(key) not in {None, ""}}
        if product and product not in values:
            values.append(product)
    return values


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _json_ready(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return value


def _document_id(session_id: str) -> str:
    return f"session_state:{session_id}"
