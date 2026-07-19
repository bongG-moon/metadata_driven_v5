# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 10 테이블 카탈로그 등록 API 응답 생성기
# 역할: 테이블 카탈로그 등록 결과와 채팅 표시 메시지를 Web/Run API용 구조화 응답으로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수, 채팅 표시 메시지 (display_message)
# 주요 출력: API 응답 (api_response), API 메시지 (api_message)
# 처리 흐름: 테이블 카탈로그 저장 결과를 웹/API용 dict와 JSON Message 두 출력 계약으로 변환합니다.
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


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_text()`는 Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    text = getattr(value, "text", value)
    return "" if text is None else str(text).strip()


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TableCatalogSavingApiResponseBuilder(Component):
    display_name = "10 테이블 카탈로그 등록 API 응답 생성기"
    description = "테이블 카탈로그 등록 결과와 채팅 표시 메시지를 Web/Run API용 구조화 응답으로 변환합니다."

    # 함수 설명: Python 코드에서 구조화 최종 출력을 선언해 수동 Flow JSON 편집을 없앱니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [
        Output(name="api_response", display_name="API 응답", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="api_message", display_name="API 메시지", method="build_message", types=["Message"], group_outputs=True),
    ]

    # 함수 설명: `_response_once()`는 Data와 Message terminal이 같은 API 응답을 두 번 deepcopy·직렬화하지 않도록 결과를 재사용합니다.
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
