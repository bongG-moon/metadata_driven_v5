# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: CUBE 스케줄 등록 응답 생성기
# 역할: 저장 결과를 채팅 Message와 Run API용 구조화 terminal로 제공합니다.
# 주요 입력: MongoDB Writer가 반환한 저장 결과 Data입니다.
# 주요 출력: 외부 API가 직접 사용할 수 있는 최소 구조의 api_response입니다.
# 유지보수 포인트: MongoDB URI와 BOT token 같은 비밀값은 응답에 포함하지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


# 함수 설명: Writer 결과에서 공개 가능한 스케줄 필드와 오류·경고만 골라 API 응답 계약으로 만듭니다.
def build_response(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    payload = deepcopy(data) if isinstance(data, dict) else {}
    document = payload.get("schedule_document") if isinstance(payload.get("schedule_document"), dict) else {}
    return {
        "response_type": "cube_schedule_authoring",
        "status": payload.get("status", "error"),
        "success": bool(payload.get("success")),
        "direct_response_ready": True,
        "message": str(payload.get("message") or "CUBE 스케줄 처리 결과가 없습니다."),
        "data": {
            "schema_version": document.get("schema_version"),
            "schedule_id": document.get("schedule_id"),
            "version": document.get("version"),
            "employee_id": document.get("employee_id"),
            "channel_id": document.get("channel_id"),
            "question": document.get("question"),
            "schedule": deepcopy(document.get("schedule")) if isinstance(document.get("schedule"), dict) else {},
            "enabled": document.get("enabled"),
            "database": payload.get("database"),
            "collection_name": payload.get("collection_name"),
        },
        "errors": deepcopy(payload.get("errors")) if isinstance(payload.get("errors"), list) else [],
        "warnings": deepcopy(payload.get("warnings")) if isinstance(payload.get("warnings"), list) else [],
    }


# Langflow 컴포넌트 클래스: 스케줄 authoring 결과를 Run API의 구조화 terminal로 노출하는 standalone 노드입니다.
class CubeScheduleResponseBuilder(Component):
    display_name = "03 CUBE 스케줄 등록 응답 생성기"
    description = "CUBE 스케줄 등록 결과를 채팅과 Run API 응답으로 제공합니다."

    # 주요 메서드: Langflow가 이 노드를 Flow의 명시적 output terminal로 인식하도록 초기화합니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [DataInput(name="write_result", display_name="저장 결과", required=True)]
    outputs = [Output(name="api_response", display_name="API 응답", method="build_payload", types=["Data"])]

    # 주요 메서드: 동일 입력에 대한 응답을 노드 실행 중 한 번만 계산하고 캐시해 중복 처리를 막습니다.
    def _response(self) -> dict[str, Any]:
        value = getattr(self, "write_result", None)
        if getattr(self, "_cache_input", None) is not value:
            self._cache_input = value
            self._cache_response = build_response(value)
        return self._cache_response

    # Langflow 출력 함수: 캐시된 공개 응답을 Langflow Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(data=self._response())
