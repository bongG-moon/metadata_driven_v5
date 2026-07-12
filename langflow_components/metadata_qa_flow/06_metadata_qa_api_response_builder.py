# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 06 메타데이터 QA API 응답 생성기
# 역할: 메타데이터 QA 결과를 Web/Run API에서 읽을 수 있는 구조화 응답으로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수, 채팅 표시 메시지 (display_message)
# 주요 출력: API 응답 (api_response), API 메시지 (api_message)
# 처리 흐름: 최종 QA API 응답에서 큰 내부 context를 제거하고 구조화 data와 Message envelope을 제공합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
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


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        payload = deepcopy(data)
    else:
        payload = {}
    payload.pop("metadata_qa_context", None)
    return payload


# 함수 설명: `_text()`는 Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", value)
    if text is None:
        return ""
    return str(text).strip()


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
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

    # 함수 설명: `_response_once()`는 Data와 Message terminal이 같은 QA 응답을 두 번 deepcopy·직렬화하지 않도록 결과를 재사용합니다.
    def _response_once(self) -> dict[str, Any]:
        payload = getattr(self, "payload", None)
        message = getattr(self, "display_message", "")
        cache_key = (id(payload), id(message))
        if getattr(self, "_response_cache_key", None) != cache_key:
            self._response_cache_key = cache_key
            self._response_cache = build_api_response(payload, message)
        return self._response_cache

    # Langflow 출력 함수: 'API 응답 (api_response)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=self._response_once())

    # Langflow 출력 함수: 'API 메시지 (api_message)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_message(self) -> Message:
        return Message(text=json.dumps({"api_response": self._response_once()}, ensure_ascii=False, default=str))
