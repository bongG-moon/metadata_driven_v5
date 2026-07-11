# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 10 메인 플로우 필터 등록 API 응답 생성기
# 역할: 메인 플로우 필터 등록 결과와 채팅 표시 메시지를 Web/Run API용 구조화 응답으로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수, 채팅 표시 메시지 (display_message)
# 주요 출력: API 응답 (api_response), API 메시지 (api_message)
# 처리 흐름: 메인 플로우 필터 저장 결과를 웹/API용 dict와 JSON Message 두 출력 계약으로 변환합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


# 주요 함수: 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_api_response(payload_value: Any, display_message_value: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    message = _text(display_message_value) or str(payload.get("message") or "").strip()
    return {
        "response_type": "metadata_authoring",
        "metadata_type": payload.get("metadata_type", "main_flow_filter"),
        "metadata_label": payload.get("metadata_label", "메인 플로우 필터"),
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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MainFlowFilterSavingApiResponseBuilder(Component):
    display_name = "10 메인 플로우 필터 등록 API 응답 생성기"
    description = "메인 플로우 필터 등록 결과와 채팅 표시 메시지를 Web/Run API용 구조화 응답으로 변환합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [
        Output(name="api_response", display_name="API 응답", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="api_message", display_name="API 메시지", method="build_message", types=["Message"], group_outputs=True),
    ]

    # Langflow 출력 함수: 'API 응답 (api_response)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_api_response(getattr(self, "payload", None), getattr(self, "display_message", "")))

    # Langflow 출력 함수: 'API 메시지 (api_message)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_message(self) -> Message:
        response = build_api_response(getattr(self, "payload", None), getattr(self, "display_message", ""))
        return Message(text=json.dumps({"api_response": response}, ensure_ascii=False, default=str))
