from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

KOREA_ZONE_NAME = "Asia/Seoul"


def build_request(question: Any, previous_state_value: Any = None, session_id: str = "") -> dict[str, Any]:
    resolved_session_id = _resolve_session_id(previous_state_value, session_id)
    return {
        "request": {
            "question": str(question or ""),
            "session_id": resolved_session_id,
            "reference_date": _korea_today(),
        },
        "state": _payload(previous_state_value),
        "metadata_refs": [],
        "intent_plan": {},
        "source_results": [],
        "runtime_sources": {},
        "analysis": {},
        "data": {},
        "answer_message": "",
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }


def _resolve_session_id(previous_state_value: Any = None, session_id: Any = "") -> str:
    text = str(session_id or "").strip()
    if text:
        return text
    state = _payload(previous_state_value)
    for key in ("session_id", "conversation_id", "thread_id"):
        value = state.get(key)
        if value:
            return str(value)
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    if request.get("session_id"):
        return str(request["session_id"])
    return "demo-session"


def _korea_today() -> str:
    return datetime.now(_korea_timezone()).strftime("%Y%m%d")


def _korea_timezone():
    try:
        zoneinfo = import_module("zoneinfo")
        return zoneinfo.ZoneInfo(KOREA_ZONE_NAME)
    except Exception:
        return timezone(timedelta(hours=9), "KST")


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


class AnalysisRequestLoader(Component):
    display_name = "00 분석 요청 로더"
    description = "질문과 이전 상태를 표준 데이터 분석 페이로드로 변환합니다."
    inputs = [
        MessageTextInput(name="question", display_name="사용자 질문", required=True, tool_mode=True),
        DataInput(name="previous_state", display_name="이전 상태", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=build_request(getattr(self, "question", ""), getattr(self, "previous_state", None)))
