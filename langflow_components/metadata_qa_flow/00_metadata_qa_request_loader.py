# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 메타데이터 QA 요청 로더
# 역할: 메타데이터 조회/설명 질문을 읽기 전용 QA 페이로드로 변환합니다.
# 주요 입력: 사용자 질문 (question) · 필수, 이전 상태 (previous_state)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 메타데이터 질문과 이전 상태를 읽기 전용 QA 페이로드로 초기화하고 trace 영역을 준비합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


# 주요 함수: 사용자 입력과 이전 상태를 후속 노드가 공유할 표준 요청 dict로 변환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_request(question: Any, previous_state_value: Any = None) -> dict[str, Any]:
    text = str(question or "").strip()
    errors = [] if text else [{"type": "empty_question", "message": "메타데이터 QA 질문이 비어 있습니다."}]
    return {
        "request": {"question": text},
        "state": _payload(previous_state_value),
        "metadata_route": {"route": "metadata_qa", "status": "ready" if text else "warning"},
        "trace": {
            "warnings": [],
            "errors": errors,
            "inspection": {
                "metadata_qa_request": {
                    "stage": "00_metadata_qa_request_loader",
                    "status": "ok" if text else "warning",
                }
            },
        },
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataQaRequestLoader(Component):
    display_name = "00 메타데이터 QA 요청 로더"
    description = "메타데이터 조회/설명 질문을 읽기 전용 QA 페이로드로 변환합니다."
    inputs = [
        MessageTextInput(name="question", display_name="사용자 질문", required=True, tool_mode=True),
        DataInput(name="previous_state", display_name="이전 상태", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_request(getattr(self, "question", ""), getattr(self, "previous_state", None)))
