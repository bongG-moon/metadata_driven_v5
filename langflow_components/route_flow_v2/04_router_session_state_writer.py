# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 Router 세션 상태 저장기
# 역할: Router가 성공적으로 선택한 하위 Flow와 짧은 대화 문맥만 Router 전용 MongoDB
#       컬렉션에 저장하고, 최종 Message는 변경 없이 Chat Output으로 전달합니다.
# 주요 입력: Router 문맥 Message, Agent 원본 응답, 정리된 최종 Message, MongoDB 설정
# 주요 출력: 최종 Message, 저장 상태 Data
# 처리 흐름: ToolContent에서 선택된 도구와 명시 오류를 판별한 뒤 정상 완료인 경우만
#       직전 사용자 질문·최종 답변·선택 Flow만 upsert합니다.
# 유지보수 포인트: 저장 실패·상태 부재·도구 오류는 기존 정상 상태를 덮어쓰지 않으며,
#       어떤 경우에도 현재 답변 전송을 차단하지 않습니다.
# =============================================================================
"""Persist a minimal Router continuation state after a completed direct tool run.

This is deliberately independent from the Data Analysis session writer.  The
Router needs only enough context to decide where a terse follow-up belongs;
the child Data Analysis Flow keeps its own rich criteria/result state in
``agent_v4_session_states``.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, HandleInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "router_session_states"
DEFAULT_MESSAGE_CHAR_LIMIT = 1200
DEFAULT_TTL_HOURS = 24
MAX_MESSAGE_CHAR_LIMIT = 4000
MAX_TTL_HOURS = 24 * 30
CONTEXT_KEY = "router_session_context"
STATE_VERSION = "router-session-state-v2"
FAILED_STATUSES = {"error", "failed", "failure", "cancelled", "canceled"}
KST = timezone(timedelta(hours=9), "KST")


# 함수 설명: dict/Pydantic 객체에서 같은 방식으로 속성을 읽습니다.
def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


# 함수 설명: Message 또는 일반 값을 공백 제거한 문자열로 바꿉니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    candidate = _value(value, "text", None)
    if candidate is not None and candidate is not value:
        return _text(candidate)
    return str(value).strip()


# 함수 설명: BoolInput/API tweak의 bool 또는 문자열 값을 같은 규칙으로 해석합니다.
def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _text(value).casefold() not in {"", "0", "false", "no", "off", "none", "null"}


# 함수 설명: 상태 저장의 history/TTL 제한값을 안전한 범위로 고정합니다.
def _bounded_int(value: Any, default: int, maximum: int) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


# 함수 설명: 원본 긴 답변·줄바꿈을 Router 문맥용 짧은 문자열로 정리합니다.
def _compact_text(value: Any, char_limit: int) -> str:
    return " ".join(_text(value).split())[:char_limit]


# 함수 설명: TTL·정렬용 UTC BSON Date와 별도로 MongoDB에서 바로 확인할 KST ISO 시각을 만듭니다.
def _kst_iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST).isoformat()


# 함수 설명: 현재 Router 문맥 Message의 structured data에서 canonical 세션과 최소 라우팅 문맥을 꺼냅니다.
def _router_context(message: Any) -> dict[str, Any]:
    data = _value(message, "data", {})
    context = data.get(CONTEXT_KEY) if isinstance(data, dict) else {}
    return deepcopy(context) if isinstance(context, dict) else {}


# 함수 설명: Router collection document ID를 다른 Flow의 세션 문서와 충돌하지 않게 만듭니다.
def _document_id(session_id: str) -> str:
    return f"router_session:{session_id}"


# 함수 설명: 중첩 ToolContent를 순회해 Router가 실제로 선택한 마지막 Tool 결과를 찾습니다.
def _walk_content(value: Any):
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_content(item)
        return
    if value is None:
        return
    yield value
    children = _value(value, "contents", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from _walk_content(child)


# 함수 설명: ToolContent 또는 직렬화 호환 Tool block인지 판별합니다.
def _is_tool_content(value: Any) -> bool:
    item_type = _text(_value(value, "type", "")).casefold()
    if item_type in {"tool", "tool_use", "tool_call"}:
        return True
    return _value(value, "output", object()) is not None and bool(
        _text(_value(value, "name", "")) or _text(_value(value, "tool_call_id", ""))
    )


# 함수 설명: Tool wrapper 또는 최종 Message가 명시한 실행 실패만 좁게 감지합니다.
def _has_explicit_tool_failure(value: Any) -> bool:
    if bool(_value(value, "error", False)):
        return True
    if _text(_value(value, "category", "")).casefold() == "error":
        return True
    direct_status = _text(_value(value, "status", "")).casefold()
    if direct_status in FAILED_STATUSES:
        return True
    data = _value(value, "data", {})
    if not isinstance(data, Mapping):
        return False
    data_status = _text(data.get("status")).casefold()
    if data_status in FAILED_STATUSES:
        return True
    route_gate = data.get("route_gate")
    if isinstance(route_gate, Mapping) and _text(route_gate.get("status")).casefold() in {
        "blocked",
        *FAILED_STATUSES,
    }:
        return True
    return False


# 함수 설명: Tool output의 명시 오류 상태만 찾아 이전 정상 Router 상태 덮어쓰기를 막습니다.
def _tool_result(agent_message: Any) -> tuple[str, bool]:
    """Return (last_completed_tool_name, explicit_failure_seen).

    A child Flow can deliberately return a normal clarification or a blocked
    business response.  That is still a completed *Router selection* and is
    valuable context for its next user turn.  Only Tool/runtime errors that
    are explicitly represented in the contract stop state replacement.
    """

    selected = ""
    failure = False
    for item in _walk_content(_value(agent_message, "content_blocks", None)):
        if not _is_tool_content(item):
            continue
        output = _value(item, "output", None)
        status = _text(_value(output, "status", "")).casefold()
        error = _value(item, "error", None)
        if error or status in FAILED_STATUSES or _has_explicit_tool_failure(output):
            failure = True
            continue
        name = _text(_value(item, "name", ""))
        if name:
            selected = name
    return selected, failure


# 함수 설명: 최종 Message의 세션을 canonical Router 세션으로 보정하되 text/data는 그대로 유지합니다.
def _preserve_message(answer_message: Any, session_id: str) -> Message:
    if isinstance(answer_message, Message):
        message = answer_message
    else:
        message = Message(text=_text(answer_message))
    if session_id:
        message.session_id = session_id
    if not isinstance(getattr(message, "data", None), dict):
        message.data = {"text": _text(getattr(message, "text", ""))}
    else:
        message.data.setdefault("text", _text(getattr(message, "text", "")))
    return message


# 함수 설명: MongoDB에서 현재 document를 읽고 TTL index를 보장합니다.
def _connect_collection(mongo_uri: str, mongo_database: str, collection_name: str) -> tuple[Any, Any]:
    pymongo = import_module("pymongo")
    client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    collection = client[mongo_database][collection_name]
    # Existing deployments can grant read/write but not createIndex.  TTL is
    # an operational cleanup enhancement, never a prerequisite for Router
    # continuity, so an index permission issue must not make every write fail.
    try:
        collection.create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass
    return client, collection


# 함수 설명: 상태 문서를 낙관적 revision으로 갱신해 늦은 요청이 최신 turn을 덮지 않게 합니다.
def _write_document(collection: Any, document: dict[str, Any], previous: dict[str, Any]) -> bool:
    if previous:
        expected_revision = int(previous.get("revision") or 0)
        result = collection.replace_one(
            {"_id": document["_id"], "revision": expected_revision},
            document,
            upsert=False,
        )
        return int(getattr(result, "matched_count", 0) or 0) == 1
    collection.replace_one({"_id": document["_id"]}, document, upsert=True)
    return True


# 함수 설명: 성공한 Router Tool 결과만 상태로 저장하고 모든 실패는 final Message를 그대로 통과시킵니다.
def write_router_session_state(
    context_message: Any,
    agent_message: Any,
    answer_message: Any,
    *,
    mongo_uri: Any = "",
    mongo_database: Any = DEFAULT_DATABASE,
    session_collection_name: Any = DEFAULT_COLLECTION,
    enabled: Any = True,
    message_char_limit: Any = DEFAULT_MESSAGE_CHAR_LIMIT,
    ttl_hours: Any = DEFAULT_TTL_HOURS,
) -> dict[str, Any]:
    """Persist compact Router state without ever failing the current response."""

    context = _router_context(context_message)
    session_id = _text(context.get("session_id")) or _text(_value(context_message, "session_id", ""))
    message = _preserve_message(answer_message, session_id)
    status: dict[str, Any] = {
        "stage": "04_router_session_state_writer",
        "saved": False,
        "session_id": session_id,
        "collection_name": _text(session_collection_name) or DEFAULT_COLLECTION,
        "reason": "",
        "errors": [],
    }
    if not _truthy(enabled):
        status["reason"] = "disabled"
        return {"message": message, "status": status}
    if not session_id:
        status["reason"] = "missing_session_id"
        return {"message": message, "status": status}

    selected_tool, explicit_failure = _tool_result(agent_message)
    if explicit_failure:
        status["reason"] = "tool_error"
        return {"message": message, "status": status}
    if not selected_tool:
        status["reason"] = "no_completed_tool"
        return {"message": message, "status": status}

    question = _text(_value(context_message, "text", ""))
    if not question:
        status["reason"] = "missing_question"
        return {"message": message, "status": status}

    uri = _text(mongo_uri)
    if not uri:
        status["reason"] = "missing_mongo_uri"
        return {"message": message, "status": status}

    database = _text(mongo_database) or DEFAULT_DATABASE
    collection_name = _text(session_collection_name) or DEFAULT_COLLECTION
    status["collection_name"] = collection_name
    max_chars = _bounded_int(message_char_limit, DEFAULT_MESSAGE_CHAR_LIMIT, MAX_MESSAGE_CHAR_LIMIT)
    max_ttl = _bounded_int(ttl_hours, DEFAULT_TTL_HOURS, MAX_TTL_HOURS)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=max_ttl)
    client = None
    try:
        client, collection = _connect_collection(uri, database, collection_name)
        previous = collection.find_one({"_id": _document_id(session_id)}) or {}
        if not isinstance(previous, dict):
            previous = {}
        previous_revision = int(previous.get("revision") or 0)
        document = {
            "_id": _document_id(session_id),
            "session_id": session_id,
            "state_version": STATE_VERSION,
            "revision": previous_revision + 1,
            "turn_count": int(previous.get("turn_count") or 0) + 1,
            # Router does not own child Flow's analysis memory.  It only
            # persists the smallest completed exchange needed to route an
            # omitted follow-up without allowing a long transcript to
            # dominate a new ask.
            "last_selected_flow": selected_tool,
            "last_user_question": _compact_text(question, max_chars),
            "last_assistant_answer": _compact_text(_value(message, "text", ""), max_chars),
            "created_at": previous.get("created_at") or now,
            "created_at_kst": str(previous.get("created_at_kst") or _kst_iso(previous.get("created_at")) or _kst_iso(now)),
            "updated_at": now,
            "updated_at_kst": _kst_iso(now),
            "expires_at": expires_at,
            "expires_at_kst": _kst_iso(expires_at),
        }
        if not _write_document(collection, document, previous):
            status["reason"] = "stale_router_state"
            return {"message": message, "status": status}
        status.update(
            {
                "saved": True,
                "reason": "",
                "revision": document["revision"],
                "turn_count": document["turn_count"],
                "last_selected_flow": selected_tool,
            }
        )
        return {"message": message, "status": status}
    except Exception:
        status["reason"] = "mongo_write_failed"
        status["errors"] = ["Router 세션 상태를 저장하지 못했지만 현재 답변은 정상 반환합니다."]
        return {"message": message, "status": status}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


# Langflow 컴포넌트 클래스: Router 결과 경로의 마지막에서 compact state를 best-effort로 기록합니다.
class RouterSessionStateWriter(Component):
    display_name = "04 Router 세션 상태 저장기"
    description = "성공한 Router 도구 선택과 직전 질문·답변만 MongoDB에 저장합니다. 저장 실패는 답변 전송을 막지 않습니다."
    icon = "DatabaseZap"
    name = "RouterSessionStateWriter"

    inputs = [
        HandleInput(name="context_message", display_name="Router 문맥 Message", input_types=["Message"], required=True),
        HandleInput(name="agent_message", display_name="Agent 원본 응답", input_types=["Message"], required=True),
        HandleInput(name="answer_message", display_name="정리된 최종 Message", input_types=["Message"], required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", value="", advanced=False),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", value=DEFAULT_DATABASE, advanced=False),
        MessageTextInput(name="session_collection_name", display_name="Router 세션 상태 컬렉션", value=DEFAULT_COLLECTION, advanced=False),
        BoolInput(name="enabled", display_name="세션 상태 저장", value=True, advanced=True),
        MessageTextInput(name="message_char_limit", display_name="메시지 글자 제한", value=str(DEFAULT_MESSAGE_CHAR_LIMIT), advanced=True),
        MessageTextInput(name="ttl_hours", display_name="세션 상태 유효시간(시간)", value=str(DEFAULT_TTL_HOURS), advanced=True),
    ]
    outputs = [
        Output(name="message", display_name="최종 Message", method="build_message", types=["Message"]),
        Output(name="write_status", display_name="저장 상태", method="build_write_status", types=["Data"]),
    ]

    def _write(self) -> dict[str, Any]:
        return write_router_session_state(
            getattr(self, "context_message", None),
            getattr(self, "agent_message", None),
            getattr(self, "answer_message", None),
            mongo_uri=getattr(self, "mongo_uri", ""),
            mongo_database=getattr(self, "mongo_database", DEFAULT_DATABASE),
            session_collection_name=getattr(self, "session_collection_name", DEFAULT_COLLECTION),
            enabled=getattr(self, "enabled", True),
            message_char_limit=getattr(self, "message_char_limit", DEFAULT_MESSAGE_CHAR_LIMIT),
            ttl_hours=getattr(self, "ttl_hours", DEFAULT_TTL_HOURS),
        )

    def build_message(self) -> Message:
        result = self._write()
        self.status = Data(data=result["status"])
        return result["message"]

    def build_write_status(self) -> Data:
        return Data(data=self._write()["status"])
