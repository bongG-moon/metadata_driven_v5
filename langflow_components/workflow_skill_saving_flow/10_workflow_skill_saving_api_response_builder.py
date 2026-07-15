# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 10 Workflow Skill 등록 API 응답 생성기
# 역할: 구조화 등록 결과와 표시 메시지를 Web·Run API용 안정적인 응답으로 만듭니다.
# 주요 입력: 페이로드(payload), 채팅 표시 메시지(display_message)
# 주요 출력: API 응답(api_response), API 메시지(api_message)
# 처리 흐름: 사용자 공개 필드만 선택 -> Data와 JSON Message 출력에서 같은 응답 재사용
# 유지보수 포인트: 등록 원문·MongoDB 문서 전체·내부 LLM 응답은 API에 포함하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


# 주요 함수: Workflow Skill 등록 결과에서 외부 API가 소비할 공개 필드만 선택합니다.
def build_api_response(payload_value: Any, display_message_value: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    message = _text(display_message_value) or str(payload.get("message") or "").strip()
    return {
        "response_type": "workflow_skill_authoring",
        "metadata_type": "workflow_skill",
        "metadata_label": "Workflow Skill",
        "status": payload.get("status", "unknown"),
        "success": bool(payload.get("success")),
        "direct_response_ready": True,
        "message": message,
        "answer_sections": deepcopy(_dict(payload.get("answer_sections"))),
        "data": deepcopy(_dict(payload.get("data"))),
        "metadata_authoring": deepcopy(_dict(payload.get("metadata_authoring"))),
        "write_result": deepcopy(_dict(payload.get("write_result"))),
        "trace": deepcopy(_dict(payload.get("trace"))),
    }


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 변경에 안전한 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_text()`는 Message 또는 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    text = getattr(value, "text", value)
    return "" if text is None else str(text).strip()


# 함수 설명: `_dict()`는 값이 dict일 때만 반환하고 아니면 빈 dict를 사용합니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# Langflow 컴포넌트 클래스: 동일한 API 응답을 Data와 Message terminal로 제공합니다.
class WorkflowSkillSavingApiResponseBuilder(Component):
    display_name = "10 Workflow Skill 등록 API 응답 생성기"
    description = "Workflow Skill 등록 결과를 Web·Run API용 compact 응답으로 변환합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [
        Output(name="api_response", display_name="API 응답", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="api_message", display_name="API 메시지", method="build_message", types=["Message"], group_outputs=True),
    ]

    # 함수 설명: `_response_once()`는 두 terminal이 같은 응답을 반복 생성하지 않도록 현재 입력 기준으로 캐시합니다.
    def _response_once(self) -> dict[str, Any]:
        payload = getattr(self, "payload", None)
        message = getattr(self, "display_message", "")
        cache_key = (id(payload), id(message))
        if getattr(self, "_response_cache_key", None) != cache_key:
            self._response_cache_key = cache_key
            self._response_cache = build_api_response(payload, message)
        return self._response_cache

    # Langflow 출력 함수: 공개 API 응답을 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(data=self._response_once())

    # Langflow 출력 함수: 공개 API 응답을 JSON Message로 반환합니다.
    def build_message(self) -> Message:
        return Message(text=json.dumps({"api_response": self._response_once()}, ensure_ascii=False, default=str))
