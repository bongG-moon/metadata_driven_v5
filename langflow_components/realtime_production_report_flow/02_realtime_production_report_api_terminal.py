# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 실시간 생산 분석 Report API 종료 어댑터
# 역할: Report 생성기의 compact 구조화 결과를 별도 terminal Data 출력으로 전달합니다.
# 주요 입력: realtime.production.report.v1 Data
# 주요 출력: API 응답
# 처리 흐름: 계약 검증 -> 정상 결과 전달 또는 결정론적 오류 계약 반환
# 유지보수 포인트: 화면 Message와 API terminal을 분리하고 원본 rows/HTML이 terminal payload에 들어가지 않게 유지합니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = "realtime.production.report.v1"


# 함수 설명: `normalize_realtime_production_report_result()`는 realtime·production·report·결과의 표기·자료형 차이를 비교와 저장에 사용할 표준
#        형태로 정규화합니다.
def normalize_realtime_production_report_result(value: Any) -> dict[str, Any]:
    payload = getattr(value, "data", value)
    if isinstance(payload, dict) and payload.get("contract_version") == CONTRACT_VERSION:
        return payload
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": "realtime_production_report",
        "status": "error",
        "success": False,
        "summary": "",
        "message": "### 실시간 생산 분석 오류\nReport 생성 결과 계약을 확인할 수 없습니다.",
        "report_scope": {},
        "kpis": {},
        "artifacts": [],
        "warnings": [],
        "errors": [
            {
                "type": "invalid_realtime_production_report_contract",
                "message": "Report 생성기는 realtime.production.report.v1 Data 계약을 반환해야 합니다.",
            }
        ],
    }


# Langflow 컴포넌트 클래스: Report 생성 결과를 Run Flow API가 반환할 compact terminal Data로 정규화합니다.
class RealtimeProductionReportApiTerminal(Component):
    display_name = "02 실시간 생산 Report API 종료 어댑터"
    description = "실시간 생산 Report 결과를 검증하고 Run Flow용 terminal API Data로 전달합니다."
    name = "RealtimeProductionReportApiTerminal"
    icon = "FileJson"

    # 함수 설명: `__init__()`는 외부 클라이언트나 실행 설정을 인스턴스에 보관해 뒤의 메서드가 같은 연결 문맥을 사용하게 합니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [
        DataInput(
            name="report_result",
            display_name="Report 결과",
            info="01 실시간 생산 분석 Report 생성기의 realtime.production.report.v1 API 응답입니다.",
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

    # 함수 설명: `build_api_response()`는 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
    def build_api_response(self) -> Data:
        payload = normalize_realtime_production_report_result(getattr(self, "report_result", None))
        self.status = str(payload.get("message") or "")
        return Data(data=payload)
