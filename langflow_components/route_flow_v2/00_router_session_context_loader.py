# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 Router 세션 문맥 로더
# 역할: 외부 실행 환경의 안정 세션 ID·직전 질문/답변·직전 선택 Flow만 MongoDB의
#       Router 전용 상태와 합쳐 Router 입력 Message로 정리합니다.
# 주요 입력: Chat Input Message, 외부 session_id/data/metadata, MongoDB 연결 정보
# 주요 출력: 문맥이 포함된 Message, canonical 세션 ID, 문맥 진단 Data
# 처리 흐름: 외부/기본 Message에서 세션을 확정하고, MongoDB의 직전 Router 상태에서
#       최소 라우팅 문맥만 읽어 Router Agent가 사용할 문맥으로 만듭니다.
# 유지보수 포인트: MongoDB·외부 JSON 오류는 현재 질문 실행을 막지 않습니다. 분석 Flow의
#       agent_v4_session_states와 절대 같은 컬렉션을 사용하지 않습니다.
# =============================================================================
"""Build a compact, portable Router context for external multi-turn runs.

The native Langflow Agent history is keyed only by ``graph.session_id``.  That
is sufficient in one Playground graph, but an external gateway can create a
fresh graph for every request.  This component makes the Router's own
cross-request contract explicit without turning it into a full transcript
store:

* canonical session ID from a configured ingress field, metadata, or Message;
* exactly one previous user/assistant exchange and its selected Flow from
  ``router_session_states``;
* an optional ingress-history fallback that extracts only the last completed
  exchange; and
* a fail-open result when any optional state dependency is unavailable.

The component is intentionally Router-specific.  Data Analysis keeps using
its existing ``agent_v4_session_states`` contract for prior plans and result
references.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, HandleInput, MessageTextInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "router_session_states"
DEFAULT_MESSAGE_CHAR_LIMIT = 1200
MAX_MESSAGE_CHAR_LIMIT = 4000
CONTEXT_KEY = "router_session_context"
CONTEXT_VERSION = "router-session-context-v2"


# 함수 설명: 외부 입력의 공백·None·Message 값을 표시용 문자열로 안전하게 해석합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    candidate = getattr(value, "text", None)
    if candidate is not None and candidate is not value:
        return _text(candidate)
    return str(value).strip()


# 함수 설명: BoolInput/API tweak의 bool 또는 문자열 값을 같은 규칙으로 해석합니다.
def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).casefold() not in {"", "0", "false", "no", "off", "none", "null"}


# 함수 설명: 수치형 제한값을 안전 범위 안의 정수로 정규화합니다.
def _bounded_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


# 함수 설명: 문자열 또는 dict JSON을 object로 읽고 오류는 진단용 문자열로 반환합니다.
def _json_object(value: Any, field_name: str) -> tuple[dict[str, Any], str]:
    if isinstance(value, dict):
        return deepcopy(value), ""
    text = _text(value)
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}, f"{field_name} JSON을 해석하지 못해 무시했습니다."
    if not isinstance(parsed, dict):
        return {}, f"{field_name}은 JSON 객체여야 하므로 무시했습니다."
    # GAIA/A2A 경계가 한 번 더 field 이름으로 감싼 형식도 허용합니다.
    nested = parsed.get(field_name)
    if isinstance(nested, dict) and len(parsed) == 1:
        return deepcopy(nested), ""
    return deepcopy(parsed), ""


# 함수 설명: metadata/data의 공통 세션 키 표현을 우선순위대로 읽습니다.
def _session_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("session_id", "sessionId", "conversation_id", "conversationId", "thread_id", "threadId"):
        candidate = _text(value.get(key))
        if candidate:
            return candidate
    return ""


# 함수 설명: 원본 Message에서 data/metadata 계열 mapping을 복사해 문맥 해석에 사용합니다.
def _message_mappings(message: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    data = getattr(message, "data", {})
    metadata_candidates = (
        getattr(message, "metadata", None),
        getattr(message, "a2a_metadata", None),
        getattr(message, "framework2_metadata", None),
        data.get("metadata") if isinstance(data, dict) else None,
        data.get("a2a_metadata") if isinstance(data, dict) else None,
        data.get("framework2_metadata") if isinstance(data, dict) else None,
    )
    metadata = next((deepcopy(item) for item in metadata_candidates if isinstance(item, dict)), {})
    return deepcopy(data) if isinstance(data, dict) else {}, metadata


# 함수 설명: 저장/프롬프트에 넣을 텍스트를 길이 제한과 공백 정리로 축소합니다.
def _compact_text(value: Any, char_limit: int) -> str:
    text = " ".join(_text(value).split())
    return text[:char_limit]


# 함수 설명: 외부 history 한 행을 Router 문맥용 user/assistant 값으로 정규화합니다.
def _normalize_turn(item: Any, char_limit: int) -> tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    role = _text(item.get("role") or item.get("sender") or item.get("sender_type")).casefold()
    if role in {"human", "user", "client"}:
        role = "user"
    elif role in {"assistant", "ai", "bot", "model"}:
        role = "assistant"
    else:
        return "", ""
    content = item.get("content")
    if content in (None, ""):
        content = item.get("text")
    return role, _compact_text(content, char_limit)


# 함수 설명: 외부/레거시 history에서 현재 질문을 제외한 직전 user/assistant 한 쌍만 선택합니다.
def last_completed_exchange_from_history(
    value: Any,
    *,
    current_question: Any = "",
    char_limit: int = DEFAULT_MESSAGE_CHAR_LIMIT,
) -> tuple[str, str]:
    """Return one prior user question and its final answer, if present."""

    current = _compact_text(current_question, char_limit)
    source = value if isinstance(value, list) else []
    turns = [turn for raw in source if (turn := _normalize_turn(raw, char_limit))[1]]
    # Gateways may append the current user input to their history.  The Agent
    # receives it separately, so strip only this trailing duplicate.
    if turns and turns[-1] == ("user", current):
        turns.pop()
    last_answer = ""
    for role, content in reversed(turns):
        if role == "assistant" and not last_answer:
            last_answer = content
            continue
        if role == "user":
            return content, last_answer
    return "", ""


# 함수 설명: Router 전용 Mongo 문서 ID를 분석 세션 문서와 충돌하지 않도록 생성합니다.
def _document_id(session_id: str) -> str:
    return f"router_session:{session_id}"


# 함수 설명: MongoDB에서 Router 전용 상태를 읽고 연결 오류는 fail-open 진단으로 반환합니다.
def _load_router_document(
    *,
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
    session_id: str,
) -> tuple[dict[str, Any], list[str]]:
    if not mongo_uri or not session_id:
        return {}, []
    client = None
    try:
        pymongo = import_module("pymongo")
        client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        collection = client[mongo_database][collection_name]
        document = collection.find_one({"_id": _document_id(session_id)}) or {}
        if not isinstance(document, dict):
            return {}, ["Router 세션 상태 형식이 올바르지 않아 무시했습니다."]
        expiry = document.get("expires_at")
        if isinstance(expiry, datetime) and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if isinstance(expiry, datetime) and expiry <= datetime.now(timezone.utc):
            return {}, ["만료된 Router 세션 상태를 사용하지 않았습니다."]
        return deepcopy(document), []
    except Exception:
        # URI/DB 상세를 답변과 trace에 노출하지 않습니다. 현재 질문은 계속 실행합니다.
        return {}, ["Router 세션 상태를 읽지 못해 현재 질문만으로 라우팅합니다."]
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


# 함수 설명: 외부 input/message/metadata에서 canonical 세션 ID를 정하고 출처를 진단합니다.
def resolve_session_id(
    *,
    input_message: Any,
    explicit_session_id: Any = "",
    ingress_data: dict[str, Any] | None = None,
    ingress_metadata: dict[str, Any] | None = None,
) -> tuple[str, str, list[str]]:
    """Choose an explicit external session before native Message fallback.

    External gateways frequently give native Chat Input a new graph session on
    every request.  A deliberately mapped ``session_id``/metadata ID is thus
    the canonical Router key, while the Message ID remains the direct
    Playground fallback.  Conflicts are visible in diagnostics rather than
    blocking the current request.
    """

    message_data, message_metadata = _message_mappings(input_message)
    candidates = [
        ("explicit", _text(explicit_session_id)),
        ("metadata", _session_from_mapping(ingress_metadata or {})),
        ("data", _session_from_mapping(ingress_data or {})),
        ("message_metadata", _session_from_mapping(message_metadata)),
        ("message_session", _text(getattr(input_message, "session_id", ""))),
        ("message_data", _session_from_mapping(message_data)),
    ]
    chosen_source, chosen = next(((source, value) for source, value in candidates if value), ("", ""))
    distinct = {value for _, value in candidates if value}
    warnings: list[str] = []
    if len(distinct) > 1:
        warnings.append("서로 다른 세션 ID 후보가 있어 외부 입력 우선순위로 Router 문맥을 선택했습니다.")
    return chosen, chosen_source, warnings


# 함수 설명: 상태와 ingress를 합쳐 Agent/도구가 공유할 Router 문맥 payload를 만듭니다.
def load_router_context(
    input_message: Any,
    *,
    session_id: Any = "",
    data: Any = "",
    metadata: Any = "",
    mongo_uri: Any = "",
    mongo_database: Any = DEFAULT_DATABASE,
    session_collection_name: Any = DEFAULT_COLLECTION,
    enabled: Any = True,
    message_char_limit: Any = DEFAULT_MESSAGE_CHAR_LIMIT,
) -> dict[str, Any]:
    """Return a fail-open Router context and a canonical input Message."""

    source_message = input_message if isinstance(input_message, Message) else Message(text=_text(input_message))
    question = _text(getattr(source_message, "text", ""))
    parsed_data, data_error = _json_object(data, "data")
    parsed_metadata, metadata_error = _json_object(metadata, "metadata")
    # A platform may already attach the same mappings to the incoming Message.
    message_data, message_metadata = _message_mappings(source_message)
    if not parsed_data:
        parsed_data = deepcopy(message_data)
    if not parsed_metadata:
        parsed_metadata = deepcopy(message_metadata)

    max_chars = _bounded_int(message_char_limit, DEFAULT_MESSAGE_CHAR_LIMIT, MAX_MESSAGE_CHAR_LIMIT)
    resolved_session, session_source, warnings = resolve_session_id(
        input_message=source_message,
        explicit_session_id=session_id,
        ingress_data=parsed_data,
        ingress_metadata=parsed_metadata,
    )
    warnings.extend(item for item in (data_error, metadata_error) if item)

    state_document: dict[str, Any] = {}
    state_status = "skipped"
    mongo_enabled = _truthy(enabled)
    uri = _text(mongo_uri)
    database = _text(mongo_database) or DEFAULT_DATABASE
    collection = _text(session_collection_name) or DEFAULT_COLLECTION
    if not mongo_enabled:
        state_status = "disabled"
    elif not resolved_session:
        state_status = "missing_session_id"
        warnings.append("세션 ID가 없어 Router 이전 문맥을 불러오지 않았습니다.")
    elif not uri:
        state_status = "missing_mongo_uri"
        warnings.append("MongoDB 연결 정보가 없어 Router 이전 문맥 없이 실행합니다.")
    else:
        state_document, load_warnings = _load_router_document(
            mongo_uri=uri,
            mongo_database=database,
            collection_name=collection,
            session_id=resolved_session,
        )
        warnings.extend(load_warnings)
        state_status = "loaded" if state_document else "not_found"

    ingress_history = parsed_data.get("conversation_history") if isinstance(parsed_data, dict) else []
    if not isinstance(ingress_history, list):
        ingress_history = parsed_metadata.get("conversation_history") if isinstance(parsed_metadata, dict) else []
    # Router state v2 intentionally carries exactly one completed exchange
    # and one selected Flow.  Keep v1 fields as read-only migration fallbacks
    # so a deployment can upgrade without losing its immediately previous
    # route and answer context.
    last_selected_flow = _text(
        state_document.get("last_selected_flow") or state_document.get("last_successful_route")
    ) if state_document else ""
    last_user_question = _compact_text(
        state_document.get("last_user_question") or state_document.get("last_question"),
        max_chars,
    ) if state_document else ""
    last_assistant_answer = _compact_text(
        state_document.get("last_assistant_answer") or state_document.get("last_answer_summary"),
        max_chars,
    ) if state_document else ""
    if not last_user_question or not last_assistant_answer:
        legacy_question, legacy_answer = last_completed_exchange_from_history(
            state_document.get("recent_turns") if state_document else [],
            current_question=question,
            char_limit=max_chars,
        )
        last_user_question = last_user_question or legacy_question
        last_assistant_answer = last_assistant_answer or legacy_answer
    if not last_user_question or not last_assistant_answer:
        ingress_question, ingress_answer = last_completed_exchange_from_history(
            ingress_history,
            current_question=question,
            char_limit=max_chars,
        )
        last_user_question = last_user_question or ingress_question
        last_assistant_answer = last_assistant_answer or ingress_answer
    base_data = deepcopy(message_data)
    context = {
        "version": CONTEXT_VERSION,
        "session_id": resolved_session,
        "session_source": session_source,
        "last_selected_flow": last_selected_flow,
        "last_user_question": last_user_question,
        "last_assistant_answer": last_assistant_answer,
        "state_status": state_status,
        "database": database,
        "collection_name": collection,
        # Router routing needs only the three compact values above.  The Agent
        # must not merge the full native graph transcript back into this
        # context, even in Playground runs with a matching session ID.
        "history_mode": "minimal_router_state",
        "warnings": warnings,
    }
    # Preserve Langflow 1.11's visible text mapping while adding private,
    # structured context for the specialized Router Agent.
    base_data["text"] = question
    base_data[CONTEXT_KEY] = context
    # Keep the Chat Input Message instance whenever possible.  Langflow 1.11
    # identifies the just-stored input by its dynamic DB ``id`` and excludes
    # it from native history.  Rebuilding a fresh Message here would lose that
    # identity and make a normal Playground question appear twice to the
    # Router Agent.
    try:
        message = source_message
        message.text = question
        message.data = base_data
        if resolved_session:
            message.session_id = resolved_session
    except Exception:
        # A non-native Message-like object is still allowed for standalone
        # callers.  Its only safe fallback is a new portable Message.
        message = Message(
            text=question,
            data=base_data,
            sender=getattr(source_message, "sender", None),
            sender_name=getattr(source_message, "sender_name", None),
            files=list(getattr(source_message, "files", []) or []),
            session_id=resolved_session,
            context_id=_text(getattr(source_message, "context_id", "")),
            flow_id=getattr(source_message, "flow_id", None),
            session_metadata=deepcopy(getattr(source_message, "session_metadata", None)),
        )
    return {"message": message, "session_id": resolved_session, "context": context}


# Langflow 컴포넌트 클래스: Router 실행 앞에서 canonical session과 최소 라우팅 문맥을 준비합니다.
class RouterSessionContextLoader(Component):
    display_name = "00 Router 세션 문맥 로더"
    description = "외부 세션 ID와 MongoDB Router 상태를 합쳐 후속 질문 라우팅 문맥을 준비합니다. 상태 조회 실패는 현재 질문 실행을 차단하지 않습니다."
    icon = "MessagesSquare"
    name = "RouterSessionContextLoader"

    inputs = [
        HandleInput(
            name="input_message",
            display_name="사용자 Message",
            info="Native Chat Input의 Message를 연결합니다.",
            input_types=["Message"],
            required=True,
        ),
        MessageTextInput(
            name="session_id",
            display_name="외부 세션 ID",
            info="GAIA/CUBE 실행에서는 안정 세션 ID를 연결하거나 tweak으로 전달합니다. 비우면 Message/metadata의 세션 ID를 사용합니다.",
            value="",
            advanced=False,
        ),
        MultilineInput(
            name="data",
            display_name="외부 data JSON",
            info='선택값입니다. 예: {"conversation_history":[...]}. 최근 이전 사용자 질문 한 건만 보조 문맥으로 사용합니다.',
            value="{}",
            advanced=True,
        ),
        MultilineInput(
            name="metadata",
            display_name="외부 metadata JSON",
            info='선택값입니다. 예: {"session_id":"..."}. 외부 실행에서 이 입력으로 매핑합니다.',
            value="{}",
            advanced=True,
        ),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", value="", advanced=False),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", value=DEFAULT_DATABASE, advanced=False),
        MessageTextInput(name="session_collection_name", display_name="Router 세션 상태 컬렉션", value=DEFAULT_COLLECTION, advanced=False),
        BoolInput(name="enabled", display_name="MongoDB 문맥 사용", value=True, advanced=True),
        MessageTextInput(name="message_char_limit", display_name="메시지 글자 제한", value=str(DEFAULT_MESSAGE_CHAR_LIMIT), advanced=True),
    ]
    outputs = [
        Output(name="message", display_name="문맥 포함 Message", method="build_message", types=["Message"]),
        Output(name="canonical_session_id", display_name="Canonical 세션 ID", method="build_session_id", types=["Message"]),
        Output(name="context", display_name="문맥 진단", method="build_context", types=["Data"]),
    ]

    def _resolved(self) -> dict[str, Any]:
        return load_router_context(
            getattr(self, "input_message", None),
            session_id=getattr(self, "session_id", ""),
            data=getattr(self, "data", ""),
            metadata=getattr(self, "metadata", ""),
            mongo_uri=getattr(self, "mongo_uri", ""),
            mongo_database=getattr(self, "mongo_database", DEFAULT_DATABASE),
            session_collection_name=getattr(self, "session_collection_name", DEFAULT_COLLECTION),
            enabled=getattr(self, "enabled", True),
            message_char_limit=getattr(self, "message_char_limit", DEFAULT_MESSAGE_CHAR_LIMIT),
        )

    def build_message(self) -> Message:
        result = self._resolved()
        self.status = Data(data=result["context"])
        return result["message"]

    def build_session_id(self) -> Message:
        result = self._resolved()
        return Message(text=result["session_id"])

    def build_context(self) -> Data:
        result = self._resolved()
        return Data(data=result["context"])
