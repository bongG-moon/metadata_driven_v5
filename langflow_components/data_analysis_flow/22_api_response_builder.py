from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

def build_api_response(payload_value: Any, display_message_value: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    answer_message = str(payload.get("answer_message") or "")
    display_message = _text(display_message_value) or answer_message
    return {
        "response_type": "data_analysis",
        "status": "ok" if payload.get("analysis", {}).get("status") == "ok" else "error",
        "message": display_message,
        "data_mode": _data_mode(payload),
        "answer_sections": payload.get("answer_sections", {}),
        "request": payload.get("request", {}),
        "intent_plan": payload.get("intent_plan", {}),
        "analysis": payload.get("analysis", {}),
        "data": payload.get("data", {}),
        "data_refs": payload.get("data_refs", []),
        "state": payload.get("state", {}),
        "trace": payload.get("trace", {}),
    }


def _data_mode(payload: dict[str, Any]) -> str:
    source_results = payload.get("source_results") if isinstance(payload.get("source_results"), list) else []
    for source in source_results:
        if not isinstance(source, dict):
            continue
        execution = source.get("source_execution") if isinstance(source.get("source_execution"), dict) else {}
        if (
            execution.get("used_dummy_data") is True
            or source.get("dummy") is True
            or str(source.get("source_type") or "").strip().lower() == "dummy"
        ):
            return "dummy"
    return "live"


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    payload = deepcopy(data) if isinstance(data, dict) else {}
    payload.pop("runtime_sources", None)
    payload.pop("_runtime_rows_by_alias", None)
    payload.pop("_full_result_rows", None)
    payload.pop("_runtime_result_rows", None)
    return payload


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", value)
    if text is None:
        return ""
    return str(text).strip()


class ApiResponseBuilder(Component):
    display_name = "22 API 응답 생성기"
    description = "최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [Output(name="api_response", display_name="API 응답", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=build_api_response(getattr(self, "payload", None), getattr(self, "display_message", "")))
