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
    message = _text(display_message_value) or str(payload.get("message") or "").strip()
    return {
        "response_type": "metadata_authoring",
        "metadata_type": payload.get("metadata_type", "table_catalog"),
        "metadata_label": payload.get("metadata_label", "테이블 카탈로그"),
        "status": payload.get("status", "ok"),
        "success": bool(payload.get("success")),
        "direct_response_ready": True,
        "message": message,
        "answer_sections": payload.get("answer_sections", {}),
        "data": payload.get("data", {"columns": [], "rows": [], "row_count": 0}),
        "metadata_authoring": payload.get("metadata_authoring", {}),
        "write_result": payload.get("write_result", {}),
        "trace": payload.get("trace", {}),
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _text(value: Any) -> str:
    text = getattr(value, "text", value)
    return "" if text is None else str(text).strip()


class TableCatalogSavingApiResponseBuilder(Component):
    display_name = "10 테이블 카탈로그 등록 API 응답 생성기"
    description = "테이블 카탈로그 등록 결과와 채팅 표시 메시지를 Web/Run API용 구조화 응답으로 변환합니다."
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
