from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


def build_api_response(payload_value: Any, display_message_value: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    message = _text(display_message_value) or str(payload.get("answer_message") or payload.get("message") or "").strip()
    status = str(payload.get("status") or "ok")
    response = {
        "response_type": "metadata_qa",
        "status": status,
        "direct_response_ready": True,
        "message": message,
        "answer_type": payload.get("answer_type") or payload.get("metadata_qa", {}).get("answer_type", ""),
        "answer_sections": payload.get("answer_sections", {}),
        "request": payload.get("request", {}),
        "metadata_route": payload.get("metadata_route", {}),
        "metadata_qa": payload.get("metadata_qa", {}),
        "data": payload.get("data", {"columns": [], "rows": [], "row_count": 0}),
        "state": payload.get("state", {}),
        "trace": payload.get("trace", {}),
    }
    return response


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        payload = deepcopy(data)
    else:
        payload = {}
    payload.pop("metadata_qa_context", None)
    return payload


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", value)
    if text is None:
        return ""
    return str(text).strip()


class MetadataQaApiResponseBuilder(Component):
    display_name = "06 메타데이터 QA API 응답 생성기"
    description = "메타데이터 QA 결과를 Web/Run API에서 읽을 수 있는 구조화 응답으로 변환합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [
        Output(name="api_response", display_name="API 응답", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="api_message", display_name="API 메시지", method="build_message", types=["Message"], group_outputs=True),
    ]

    def build_payload(self) -> Data:
        return Data(data=build_api_response(getattr(self, "payload", None), getattr(self, "display_message", "")))

    def build_message(self) -> Message:
        response = build_api_response(getattr(self, "payload", None), getattr(self, "display_message", ""))
        return Message(text=json.dumps({"api_response": response}, ensure_ascii=False, default=str))
