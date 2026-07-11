from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, DropdownInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_SESSION_COLLECTION = "agent_v4_session_states"
DEFAULT_PREVIEW_ROW_LIMIT = 5
DEFAULT_HISTORY_LIMIT = 10
ENABLED_OPTIONS = ["true", "false"]


def write_session_state(
    response_payload_value: Any,
    mongo_uri: Any = "",
    mongo_database: Any = "",
    session_collection_name: Any = "",
    enabled: Any = "true",
    preview_row_limit: Any = "5",
    history_limit: Any = "10",
) -> dict[str, Any]:
    payload = _payload(response_payload_value)
    response = _response_view(payload)
    state = _state_from_response(payload, response)
    session = _session_id_from_payload(payload) or _session_id_from_payload(response) or _session_id_from_state(state) or "demo-session"
    preview_limit = _positive_int(preview_row_limit, DEFAULT_PREVIEW_ROW_LIMIT)
    max_history = _positive_int(history_limit, DEFAULT_HISTORY_LIMIT)
    collection_name = _collection_name(session_collection_name)
    status: dict[str, Any] = {
        "stage": "01_mongodb_session_state_writer",
        "enabled": _truthy(enabled),
        "saved": False,
        "session_id": session,
        "collection_name": collection_name,
        "errors": [],
    }
    if not payload:
        status["errors"] = ["empty payload"]
        return {"session_state_write": status}
    if not state:
        status["reason"] = "state_not_found"
        return {**payload, "session_state_write": status}
    if not _truthy(enabled):
        status["reason"] = "disabled"
        return {**payload, "session_state_write": status}

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
        return {**payload, "session_state_write": status}

    compact_state = _compact_state(state, preview_limit, max_history)
    compact_state.setdefault("session_id", session)
    client = None
    try:
        client, collection = _connect_collection(uri, database, collection_name)
        previous = collection.find_one({"_id": _document_id(session)}) or {}
        previous_turn_count = _positive_int(previous.get("turn_count") if isinstance(previous, dict) else 0, 0)
        document = {
            "_id": _document_id(session),
            "session_id": session,
            "state_version": "agent-v1",
            "state": compact_state,
            "last_question": _question_from_payload(payload, response),
            "last_response_type": str(response.get("response_type") or payload.get("response_type") or ""),
            "turn_count": previous_turn_count + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        collection.replace_one({"_id": document["_id"]}, document, upsert=True)
        status.update(
            {
                "saved": True,
                "turn_count": document["turn_count"],
                "preview_row_limit": preview_limit,
                "state_keys": sorted(compact_state.keys()),
                "errors": [],
            }
        )
        return {**payload, "session_state_write": status}
    except Exception as exc:
        status["errors"] = [str(exc)]
        return {**payload, "session_state_write": status}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _compact_state(state: Any, preview_limit: int, history_limit: int) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    result = deepcopy(state)
    result.pop("runtime_sources", None)
    result["chat_history"] = deepcopy(result.get("chat_history", [])[-history_limit:]) if isinstance(result.get("chat_history"), list) and history_limit else []
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


def _response_view(payload: dict[str, Any]) -> dict[str, Any]:
    api_response = payload.get("api_response")
    return deepcopy(api_response) if isinstance(api_response, dict) else payload


def _state_from_response(payload: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response.get("state"), dict):
        return deepcopy(response["state"])
    if isinstance(payload.get("state"), dict):
        return deepcopy(payload["state"])
    if isinstance(payload.get("next_state"), dict):
        return deepcopy(payload["next_state"])
    return {}


def _question_from_payload(payload: dict[str, Any], response: dict[str, Any]) -> str:
    for source in (payload.get("request"), response.get("request"), payload, response):
        if isinstance(source, dict) and source.get("question") not in (None, ""):
            return str(source["question"])
    return ""


def _session_id_from_payload(payload: dict[str, Any]) -> str:
    for source in (payload.get("request"), payload):
        if not isinstance(source, dict):
            continue
        for key in ("session_id", "conversation_id", "chat_id", "thread_id"):
            if source.get(key) not in (None, ""):
                return str(source[key]).strip()
    return ""


def _session_id_from_state(state: dict[str, Any]) -> str:
    for key in ("session_id", "conversation_id", "chat_id", "thread_id"):
        if state.get(key) not in (None, ""):
            return str(state[key]).strip()
    return ""


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


class MongoDBSessionStateWriter(Component):
    display_name = "01 MongoDB 세션 상태 저장기"
    description = "flow 실행 후 다음 질문에서 사용할 compact state를 MongoDB에 저장합니다."
    icon = "DatabaseZap"
    name = "MongoDBSessionStateWriter"
    inputs = [
        DataInput(name="response_payload", display_name="응답 페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="Mongo URI 선택값", value="", advanced=True),
        MessageTextInput(name="mongo_database", display_name="Mongo Database 선택값", value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="session_collection_name", display_name="세션 상태 컬렉션", value=DEFAULT_SESSION_COLLECTION, advanced=True),
        DropdownInput(name="enabled", display_name="사용 여부", options=ENABLED_OPTIONS, value="true", advanced=True),
        MessageTextInput(name="preview_row_limit", display_name="Preview 행 제한", value=str(DEFAULT_PREVIEW_ROW_LIMIT), advanced=True),
        MessageTextInput(name="history_limit", display_name="History 제한", value=str(DEFAULT_HISTORY_LIMIT), advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        payload = write_session_state(
            getattr(self, "response_payload", None),
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", ""),
            getattr(self, "session_collection_name", ""),
            getattr(self, "enabled", "true"),
            getattr(self, "preview_row_limit", str(DEFAULT_PREVIEW_ROW_LIMIT)),
            getattr(self, "history_limit", str(DEFAULT_HISTORY_LIMIT)),
        )
        self.status = payload.get("session_state_write", {})
        return Data(data=payload)
