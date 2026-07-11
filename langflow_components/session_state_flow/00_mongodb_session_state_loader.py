from __future__ import annotations

import json
import os
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, DropdownInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_SESSION_COLLECTION = "agent_v4_session_states"
DEFAULT_PREVIEW_ROW_LIMIT = 5
ENABLED_OPTIONS = ["true", "false"]


def load_session_state(
    question: Any = "",
    fallback_state_value: Any = None,
    mongo_uri: Any = "",
    mongo_database: Any = "",
    session_collection_name: Any = "",
    enabled: Any = "true",
    preview_row_limit: Any = "5",
) -> dict[str, Any]:
    fallback_state = _state_from_value(fallback_state_value)
    session = _session_id_from_value(question) or _session_id_from_state(fallback_state) or "demo-session"
    preview_limit = _positive_int(preview_row_limit, DEFAULT_PREVIEW_ROW_LIMIT)
    collection_name = _collection_name(session_collection_name)
    status: dict[str, Any] = {
        "stage": "00_mongodb_session_state_loader",
        "enabled": _truthy(enabled),
        "loaded": False,
        "source": "empty",
        "session_id": session,
        "collection_name": collection_name,
        "errors": [],
    }

    if fallback_state:
        state = _compact_state(fallback_state, preview_limit)
        state.setdefault("session_id", session)
        status.update({"loaded": True, "source": "input_state", "preview_row_limit": preview_limit})
        return {"state": state, "session_state_load": status}

    if not _truthy(enabled):
        status["source"] = "disabled"
        return {"state": {"session_id": session}, "session_state_load": status}

    uri = _clean(mongo_uri) or os.getenv("MONGODB_URI", "") or os.getenv("MONGO_URI", "")
    database = _clean(mongo_database) or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE)
    missing = []
    if not uri:
        missing.append("Mongo URI is empty.")
    if not database:
        missing.append("Mongo database is empty.")
    if not collection_name:
        missing.append("Mongo session state collection name is empty.")
    if missing:
        status["errors"] = missing
        return {"state": {"session_id": session}, "session_state_load": status}

    client = None
    try:
        client, collection = _connect_collection(uri, database, collection_name)
        document = collection.find_one({"_id": _document_id(session)}) or collection.find_one({"session_id": session})
        if not isinstance(document, dict):
            status["source"] = "mongodb_not_found"
            return {"state": {"session_id": session}, "session_state_load": status}
        state = _compact_state(document.get("state") if isinstance(document.get("state"), dict) else {}, preview_limit)
        state.setdefault("session_id", session)
        status.update(
            {
                "loaded": bool(state),
                "source": "mongodb",
                "updated_at": document.get("updated_at", ""),
                "turn_count": document.get("turn_count", 0),
                "preview_row_limit": preview_limit,
            }
        )
        return {"state": state, "session_state_load": status}
    except Exception as exc:
        status["errors"] = [str(exc)]
        return {"state": {"session_id": session}, "session_state_load": status}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _compact_state(state: Any, preview_limit: int) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    result = deepcopy(state)
    result.pop("runtime_sources", None)
    result["chat_history"] = deepcopy(result.get("chat_history", [])[-10:]) if isinstance(result.get("chat_history"), list) else []
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


def _state_from_value(value: Any) -> dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    if isinstance(payload.get("state"), dict):
        return deepcopy(payload["state"])
    if any(key in payload for key in ("session_id", "chat_history", "context", "current_data", "followup_source_results")):
        return deepcopy(payload)
    return {}


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return deepcopy(data)
    text = getattr(value, "text", None) or getattr(value, "content", None)
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return deepcopy(parsed) if isinstance(parsed, dict) else {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return deepcopy(parsed) if isinstance(parsed, dict) else {}
    return {}


def _connect_collection(uri: str, database: str, collection_name: str) -> tuple[Any, Any]:
    pymongo = import_module("pymongo")
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    return client, client[database][collection_name]


def _collection_name(value: Any) -> str:
    return _clean(value) or os.getenv("MONGODB_SESSION_STATE_COLLECTION", DEFAULT_SESSION_COLLECTION)


def _document_id(session_id: str) -> str:
    return f"session_state:{session_id}"


def _session_id_from_state(state: dict[str, Any]) -> str:
    for key in ("session_id", "conversation_id", "chat_id", "thread_id"):
        if state.get(key) not in (None, ""):
            return str(state[key]).strip()
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    if request.get("session_id") not in (None, ""):
        return str(request["session_id"]).strip()
    return ""


def _session_id_from_value(value: Any) -> str:
    for attr in ("session_id", "conversation_id", "chat_id", "thread_id"):
        text = str(getattr(value, attr, "") or "").strip()
        if text:
            return text
    return _session_id_from_state(_payload(value))


def _rows_from(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("rows")
    if not isinstance(rows, list):
        rows = value.get("preview_rows")
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


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "n", "off", "disabled"}


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _json_ready(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return value


class MongoDBSessionStateLoader(Component):
    display_name = "00 MongoDB 세션 상태 로더"
    description = "session_id 기준으로 MongoDB에서 이전 compact state를 불러와 다음 요청의 이전 상태로 전달합니다."
    icon = "Database"
    name = "MongoDBSessionStateLoader"
    inputs = [
        MessageTextInput(name="question", display_name="사용자 질문", required=True, tool_mode=True),
        DataInput(name="fallback_state", display_name="직접 전달 상태", required=False, advanced=True),
        MessageTextInput(name="mongo_uri", display_name="Mongo URI 선택값", value="", advanced=True),
        MessageTextInput(name="mongo_database", display_name="Mongo Database 선택값", value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="session_collection_name", display_name="세션 상태 컬렉션", value=DEFAULT_SESSION_COLLECTION, advanced=True),
        DropdownInput(name="enabled", display_name="사용 여부", options=ENABLED_OPTIONS, value="true", advanced=True),
        MessageTextInput(name="preview_row_limit", display_name="Preview 행 제한", value=str(DEFAULT_PREVIEW_ROW_LIMIT), advanced=True),
    ]
    outputs = [Output(name="loaded_state", display_name="불러온 이전 상태", method="build_state", types=["Data"])]

    def _result(self) -> dict[str, Any]:
        cached = getattr(self, "_cached_session_state_payload", None)
        if isinstance(cached, dict):
            return cached
        result = load_session_state(
            getattr(self, "question", ""),
            getattr(self, "fallback_state", None),
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", ""),
            getattr(self, "session_collection_name", ""),
            getattr(self, "enabled", "true"),
            getattr(self, "preview_row_limit", str(DEFAULT_PREVIEW_ROW_LIMIT)),
        )
        self._cached_session_state_payload = result
        self.status = result.get("session_state_load", {})
        return result

    def build_state(self) -> Data:
        return Data(data=deepcopy(self._result().get("state", {})))
