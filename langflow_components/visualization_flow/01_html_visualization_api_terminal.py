# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 HTML 시각화 API 종료 어댑터
# 역할: 시각화 생성기의 구조화 결과를 별도 terminal Data 출력으로 전달합니다.
# 주요 입력: 시각화 결과 (visualization_result) · 필수
# 주요 출력: API 응답 (api_response)
# 처리 흐름: visualization.result.v1 계약 검증 -> 정상 결과 그대로 전달 또는 결정론적 오류 계약 반환
# 유지보수 포인트: 화면용 Message 경로와 API terminal 경로를 분리해 Run Flow가 구조화 결과를 안정적으로 수집하게 합니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = "visualization.result.v1"


# 함수 설명: Langflow Data 또는 일반 dict에서 시각화 계약을 꺼내며 자연어 문자열 추측은 허용하지 않습니다.
def normalize_visualization_api_result(value: Any) -> dict[str, Any]:
    payload = getattr(value, "data", value)
    if isinstance(payload, dict) and payload.get("contract_version") == CONTRACT_VERSION:
        return payload
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": "html_visualization",
        "status": "error",
        "success": False,
        "message": "### 시각화 오류\n시각화 생성 결과 계약을 확인할 수 없습니다.",
        "artifacts": [],
        "warnings": [],
        "errors": [
            {
                "type": "invalid_visualization_result_contract",
                "message": "시각화 생성기는 visualization.result.v1 Data 계약을 반환해야 합니다.",
            }
        ],
    }


# Langflow 컴포넌트 클래스: 화면 출력과 분리된 단일 terminal API 포트를 캔버스에 제공합니다.
# 별도 종료 노드이므로 Run Flow가 비종료 생성기 포트를 임의 승격하지 않고 구조화 결과를 수집할 수 있습니다.
class HTMLVisualizationApiTerminal(Component):
    display_name = "01 HTML 시각화 API 종료 어댑터"
    description = "시각화 결과를 검증하고 Run Flow용 terminal API Data로 전달합니다."
    name = "HTMLVisualizationApiTerminal"
    icon = "FileJson"

    # 함수 설명: Python 코드에서 구조화 최종 출력을 선언해 수동 Flow JSON 편집을 없앱니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [
        DataInput(
            name="visualization_result",
            display_name="시각화 결과",
            info="00 HTML 시각화 생성기의 visualization.result.v1 API 응답입니다.",
            required=True,
        )
    ]
    outputs = [
        Output(
            name="api_response",
            display_name="API 응답",
            method="build_api_response",
            types=["Data"],
        )
    ]

    # Langflow 출력 함수: 검증된 시각화 결과를 별도 terminal Data로 반환합니다.
    def build_api_response(self) -> Data:
        payload = normalize_visualization_api_result(getattr(self, "visualization_result", None))
        self.status = str(payload.get("message") or "")
        return Data(data=payload)
