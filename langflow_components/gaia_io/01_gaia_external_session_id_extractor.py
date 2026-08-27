# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: GaiA 외부 세션 ID 추출
# 역할: 운영 GaiA Input Message의 metadata.session_id를 읽어 Router 00의
#       MessageTextInput에 연결 가능한 세션 전용 Message로 변환합니다.
# 주요 입력: GaiA Input의 Message
# 주요 출력: 외부 세션 ID(Message), 세션 진단(Data)
# 유지보수 포인트: 사용자 질문 Message를 외부 세션 ID 입력에 직접 연결하지 않습니다.
# =============================================================================
"""Extract a session-only Message from the GaiA ingress Message.

``00 Router 세션 문맥 로더.session_id`` is a ``MessageTextInput``. Langflow
therefore accepts a Message edge, but reads its ``text`` value. Passing the
GaiA user Message directly would make the question itself the session key.
This component creates a separate Message whose text is only the stable
external session ID, preserving the intended type contract without changing
Router 00's generic fallback behavior.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


SESSION_KEYS = (
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
    "thread_id",
    "threadId",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        try:
            parsed = json.loads(text, strict=False)
        except (TypeError, ValueError):
            return {}
    return deepcopy(parsed) if isinstance(parsed, dict) else {}


def _session_from_mapping(value: Any) -> str:
    mapping = _as_mapping(value)
    for key in SESSION_KEYS:
        candidate = str(mapping.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


def extract_external_session_id(message: Any) -> tuple[str, str]:
    """Return ``(session_id, source)`` without treating user text as an ID."""

    metadata = _as_mapping(getattr(message, "metadata", None))
    data = _as_mapping(getattr(message, "data", None))
    # GaiA Input places session_id in metadata. The remaining fallbacks make
    # the extractor tolerant of compatible A2A ingress wrappers only.
    candidates = (
        ("metadata", _session_from_mapping(metadata)),
        ("message_session", str(getattr(message, "session_id", "") or "").strip()),
        ("data_metadata", _session_from_mapping(data.get("metadata"))),
        ("data", _session_from_mapping(data)),
    )
    return next(((value, source) for source, value in candidates if value), ("", ""))


class GaiAExternalSessionIdExtractor(Component):
    display_name = "GaiA 외부 세션 ID 추출"
    description = "GaiA Input metadata의 안정 세션 ID를 Router 00 입력용 Message로 추출합니다."
    icon = "Fingerprint"
    name = "GaiAExternalSessionIdExtractor"

    inputs = [
        HandleInput(
            name="input_message",
            display_name="GaiA Input Message",
            info="운영 GaiA Input의 message 출력을 연결합니다.",
            input_types=["Message"],
            required=True,
        )
    ]
    outputs = [
        Output(
            name="external_session_id",
            display_name="외부 세션 ID",
            method="build_external_session_id",
            types=["Message"],
        ),
        Output(
            name="diagnostics",
            display_name="세션 진단",
            method="build_diagnostics",
            types=["Data"],
        ),
    ]

    def _resolved(self) -> tuple[str, str]:
        return extract_external_session_id(getattr(self, "input_message", None))

    def build_external_session_id(self) -> Message:
        session_id, _source = self._resolved()
        # Router 00's session_id is MessageTextInput(input_types=["Message"]),
        # so this output deliberately carries only the canonical ID in text.
        return Message(text=session_id, data={"text": session_id}, session_id=session_id or None)

    def build_diagnostics(self) -> Data:
        session_id, source = self._resolved()
        return Data(
            data={
                "component": "GaiA 외부 세션 ID 추출",
                "session_id_present": bool(session_id),
                "session_source": source or "missing",
            }
        )
