# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 22 API 응답 생성기
# 역할: 최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다.
# 주요 입력: 페이로드 (payload) · 필수, 채팅 표시 메시지 (display_message)
# 주요 출력: API 응답 (api_response)
# 처리 흐름: 웹/API 소비자가 필요한 결과만 남기고 runtime source와 대용량 내부 필드를 제거한 응답 envelope을 만듭니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

# 주요 함수: 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
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


# 함수 설명: `_data_mode()`는 payload의 retrieval_mode와 source 결과를 확인해 dummy/live 응답 표시 모드를 결정합니다.
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


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    payload = deepcopy(data) if isinstance(data, dict) else {}
    payload.pop("runtime_sources", None)
    payload.pop("_runtime_rows_by_alias", None)
    payload.pop("_full_result_rows", None)
    payload.pop("_runtime_result_rows", None)
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
class ApiResponseBuilder(Component):
    display_name = "22 API 응답 생성기"
    description = "최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [Output(name="api_response", display_name="API 응답", method="build_payload")]

    # Langflow 출력 함수: 'API 응답 (api_response)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_api_response(getattr(self, "payload", None), getattr(self, "display_message", "")))
