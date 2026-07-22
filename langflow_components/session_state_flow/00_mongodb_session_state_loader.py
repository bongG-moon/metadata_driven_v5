# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 MongoDB 세션 상태 로더
# 역할: session_id 기준으로 MongoDB에서 이전 compact state를 불러와 다음 요청의 이전 상태로 전달합니다.
# 주요 입력: 사용자 질문 (question) · 필수, 직접 전달 상태 (fallback_state), Mongo URI 선택값 (mongo_uri), Mongo Database 선택값
#        (mongo_database), 세션 상태 컬렉션 (session_collection_name), 사용 여부 (enabled), Preview 행 제한 (preview_row_limit)
# 주요 출력: 불러온 이전 상태 (loaded_state)
# 처리 흐름: 직접 전달된 상태를 우선 사용하고, 없으면 session ID로 MongoDB 상태를 읽어 runtime 데이터를 제거한 작은 이전 상태를 만듭니다.
# 유지보수 포인트: 세션 상태에는 runtime_sources와 대용량 rows를 그대로 넣지 말고 후속 질문에 필요한 요약과 data_ref만 남깁니다.
# =============================================================================

from __future__ import annotations

import json
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


# 주요 함수: 직접 상태 또는 MongoDB 세션 상태를 후속 질문용 크기로 정규화합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def load_session_state(
    question: Any = "",
    fallback_state_value: Any = None,
    mongo_uri: Any = "",
    mongo_database: Any = "",
    session_collection_name: Any = "",
    enabled: Any = "true",
    preview_row_limit: Any = "5",
    runtime_session_id: Any = "",
) -> dict[str, Any]:
    fallback_state = _state_from_value(fallback_state_value)
    session = (
        _clean(runtime_session_id)
        or _session_id_from_value(question)
        or _session_id_from_state(fallback_state)
    )
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
        return {"state": {"session_id": session} if session else {}, "session_state_load": status}

    # 실제 session_id가 없을 때 공용 demo-session을 조회하면 서로 다른 사용자와 초기화된 대화가 섞입니다.
    # standalone graph가 제공하는 runtime session까지 확인한 뒤에도 비어 있으면 MongoDB 조회를 생략합니다.
    if not session:
        status["source"] = "missing_session_id"
        status["errors"] = ["현재 실행의 session_id를 확인할 수 없어 이전 세션 상태를 불러오지 않았습니다."]
        return {"state": {}, "session_state_load": status}

    uri = _clean(mongo_uri)
    database = _clean(mongo_database) or DEFAULT_DATABASE
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


# 함수 설명: `_compact_state()`는 세션 상태에서 runtime source와 큰 rows를 제거하고 후속 질문에 필요한 요약만 남깁니다.
def _compact_state(state: Any, preview_limit: int) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    result: dict[str, Any] = {}
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    session_id = _session_id_from_state(state)
    if session_id:
        result["session_id"] = session_id
    last_question = state.get("last_question") or request.get("question")
    if last_question not in (None, ""):
        result["last_question"] = str(last_question)
    if state.get("last_answer_message") not in (None, ""):
        result["last_answer_message"] = str(state.get("last_answer_message"))
    result["current_data"] = _compact_current_data(state.get("current_data"), preview_limit)
    result["followup_source_results"] = [
        _compact_source_result(item, preview_limit)
        for item in state.get("followup_source_results", [])
        if isinstance(item, dict)
    ]
    for key in ("last_intent_plan", "last_applied_criteria"):
        value = state.get(key)
        if isinstance(value, dict) and value:
            result[key] = deepcopy(value)
    runtime_source_refs = _compact_runtime_source_refs(
        state.get("runtime_source_refs"),
        result.get("current_data"),
        result.get("followup_source_results"),
    )
    if runtime_source_refs:
        result["runtime_source_refs"] = runtime_source_refs
    return _json_ready(result)


# 함수 설명: 현재 turn의 source alias에 해당하는 MongoDB 참조만 남겨 과거 source 참조가 다음 질문에 누적되지 않도록 합니다.
def _compact_runtime_source_refs(
    value: Any,
    current_data: Any,
    followup_sources: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed_aliases = {
        str(alias).strip()
        for alias in (current_data.get("source_aliases", []) if isinstance(current_data, dict) else [])
        if str(alias or "").strip()
    }
    for source in followup_sources if isinstance(followup_sources, list) else []:
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or "").strip()
        if alias:
            allowed_aliases.add(alias)
    return {
        str(alias): deepcopy(ref)
        for alias, ref in value.items()
        if str(alias) in allowed_aliases and isinstance(ref, dict)
    }


# 함수 설명: `_compact_current_data()`는 현재·데이터에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_compact_source_result()`는 데이터 소스·결과에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_state_from_value()`는 원본·값에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _state_from_value(value: Any) -> dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    if isinstance(payload.get("state"), dict):
        # `_payload()`가 입력 경계에서 이미 전체 값을 한 번 복사했으므로
        # 여기서는 그 복사본의 하위 state를 그대로 반환해 대용량 세션을 재복사하지 않습니다.
        return payload["state"]
    if any(key in payload for key in ("session_id", "chat_history", "context", "current_data", "followup_source_results")):
        return payload
    return {}


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
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


# 함수 설명: `_connect_collection()`는 짧은 server selection timeout으로 MongoDB client와 대상 collection을 생성합니다.
def _connect_collection(uri: str, database: str, collection_name: str) -> tuple[Any, Any]:
    pymongo = import_module("pymongo")
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    return client, client[database][collection_name]


# 함수 설명: `_collection_name()`는 standalone 노드 입력과 코드 기본값으로 실제 세션 상태 collection 이름을 결정합니다.
def _collection_name(value: Any) -> str:
    return _clean(value) or DEFAULT_SESSION_COLLECTION


# 함수 설명: `_document_id()`는 session_id로 `session_state:{id}` 형식의 canonical 문서 ID를 만듭니다.
def _document_id(session_id: str) -> str:
    return f"session_state:{session_id}"


# 함수 설명: `_session_id_from_state()`는 ID·원본·상태을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _session_id_from_state(state: dict[str, Any]) -> str:
    for key in ("session_id", "conversation_id", "chat_id", "thread_id"):
        if state.get(key) not in (None, ""):
            return str(state[key]).strip()
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    if request.get("session_id") not in (None, ""):
        return str(request["session_id"]).strip()
    return ""


# 함수 설명: `_session_id_from_value()`는 ID·원본·값을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _session_id_from_value(value: Any) -> str:
    for attr in ("session_id", "conversation_id", "chat_id", "thread_id"):
        text = str(getattr(value, attr, "") or "").strip()
        if text:
            return text
    return _session_id_from_state(_payload(value))


# 함수 설명: Langflow 직접 실행과 Run Flow 실행에서 현재 graph가 가진 실제 session_id를 읽습니다.
def _runtime_session_id(component: Any) -> str:
    graph = getattr(component, "graph", None)
    graph_session = str(getattr(graph, "session_id", "") or "").strip()
    if graph_session:
        return graph_session
    return str(getattr(component, "_session_id", "") or "").strip()


# 함수 설명: `_rows_from()`는 복합 데이터에서 저장/표시에 사용할 dict 행 목록을 추출합니다.
def _rows_from(value: dict[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("rows")
    if not isinstance(rows, list):
        rows = value.get("preview_rows")
    if not isinstance(rows, list):
        rows = value.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


# 함수 설명: `_rows_columns()`는 행 목록과 명시 컬럼을 함께 정규화해 표준 rows/columns 쌍을 만듭니다.
def _rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


# 함수 설명: `_truthy()`는 입력값이 활성/참 의미로 해석되는지 공통 규칙으로 판정합니다.
def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "n", "off", "disabled"}


# 함수 설명: `_positive_int()`는 입력 숫자를 1 이상의 정수로 제한해 preview·history 한도에 사용합니다.
def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


# 함수 설명: `_clean()`는 선택 입력을 안전한 문자열로 바꾸고 불필요한 앞뒤 공백을 제거합니다.
def _clean(value: Any) -> str:
    return str(value or "").strip()


# 함수 설명: `_json_ready()`는 datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.
def _json_ready(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return value


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MongoDBSessionStateLoader(Component):
    display_name = "00 MongoDB 세션 상태 로더"
    description = "session_id 기준으로 MongoDB에서 이전 compact state를 불러와 다음 요청의 이전 상태로 전달합니다."
    icon = "Database"
    name = "MongoDBSessionStateLoader"
    inputs = [
        MessageTextInput(name="question", display_name="사용자 질문", required=True, tool_mode=True),
        DataInput(name="fallback_state", display_name="직접 전달 상태", required=False, advanced=True),
        MessageTextInput(name="mongo_uri", display_name="Mongo URI 선택값", value="", advanced=False),
        MessageTextInput(name="mongo_database", display_name="Mongo Database 선택값", value=DEFAULT_DATABASE, advanced=False),
        MessageTextInput(name="session_collection_name", display_name="세션 상태 컬렉션", value=DEFAULT_SESSION_COLLECTION, advanced=False),
        DropdownInput(name="enabled", display_name="사용 여부", options=ENABLED_OPTIONS, value="true", advanced=True),
        MessageTextInput(name="preview_row_limit", display_name="Preview 행 제한", value=str(DEFAULT_PREVIEW_ROW_LIMIT), advanced=True),
    ]
    outputs = [Output(name="loaded_state", display_name="불러온 이전 상태", method="build_state", types=["Data"])]

    # 함수 설명: `_result()`는 현재 처리 상태·행·오류를 공통 source result 계약으로 묶습니다.
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
            runtime_session_id=_runtime_session_id(self),
        )
        self._cached_session_state_payload = result
        self.status = result.get("session_state_load", {})
        return result

    # Langflow 출력 함수: '불러온 이전 상태 (loaded_state)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_state(self) -> Data:
        return Data(data=deepcopy(self._result().get("state", {})))
