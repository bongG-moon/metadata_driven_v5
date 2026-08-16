# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 실시간 생산 분석 Report API 종료 어댑터
# 역할: 세션 저장이 끝난 Report 결과를 검증하고 화면 Message와 terminal Data를 함께 전달합니다.
# 주요 입력: 세션 저장기를 통과한 realtime.production.report.v1 Data, Report Message
# 주요 출력: 화면 Message, API 응답
# 처리 흐름: 세션 저장 결과 계약 검증 -> 화면 Message passthrough + compact API 결과 전달
# 유지보수 포인트: 화면 Message도 반드시 이 노드를 통과하게 해 세션 저장 순서와 실패 상태가 사용자 응답에 반영되도록 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


CONTRACT_VERSION = "realtime.production.report.v1"
SESSION_WARNING_TYPE = "report_session_state_unavailable"
SESSION_WARNING_TEXT = "후속 질문용 상태를 저장하지 못했습니다. Report는 확인할 수 있지만 이 결과를 이어서 질문하려면 Report를 다시 생성해 주세요."
INVALIDATION_WARNING_TYPE = "report_context_invalidation_failed"
INVALIDATION_WARNING_TEXT = "이전 Report 문맥을 무효화하지 못했습니다. 같은 세션의 후속 질문이 이전 Report를 참조할 수 있으니 새 세션에서 Report를 다시 생성해 주세요."


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


# 함수 설명: `finalize_report_after_session_write()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def finalize_report_after_session_write(report_result_value: Any) -> dict[str, Any]:
    """Disable follow-up claims when the session writer did not persist state."""

    payload = deepcopy(normalize_realtime_production_report_result(report_result_value))
    followup = payload.get("followup") if isinstance(payload.get("followup"), dict) else {}
    session_write = (
        payload.get("session_state_write")
        if isinstance(payload.get("session_state_write"), dict)
        else {}
    )
    state_write_intended = isinstance(payload.get("state"), dict) and bool(payload.get("state"))
    if session_write.get("saved") is True or not state_write_intended:
        return payload

    reason = str(session_write.get("reason") or "session_state_write_failed").strip()
    errors = [str(item) for item in session_write.get("errors", []) if str(item).strip()]
    usable_followup_failed = followup.get("available") is True
    warning_type = SESSION_WARNING_TYPE if usable_followup_failed else INVALIDATION_WARNING_TYPE
    warning_text = SESSION_WARNING_TEXT if usable_followup_failed else INVALIDATION_WARNING_TEXT
    warning = {
        "type": warning_type,
        "message": warning_text,
        "reason": reason,
        "errors": errors[:5],
    }
    warnings = [
        deepcopy(item)
        for item in payload.get("warnings", [])
        if isinstance(item, dict)
    ]
    if not any(item.get("type") == warning_type for item in warnings):
        warnings.append(warning)
    payload["warnings"] = warnings
    payload["followup"] = {
        **deepcopy(followup),
        "available": False,
        "reason": warning_type,
    }
    payload["state"] = {}
    message = str(payload.get("message") or "").rstrip()
    notice = f"> ⚠️ {warning_text}"
    if notice not in message:
        payload["message"] = f"{message}\n\n{notice}".strip()
    return payload


# 함수 설명: `report_message_after_session_write()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def report_message_after_session_write(report_result_value: Any, message_value: Any) -> Message:
    """Return the Report message only after the stored result contract is available.

    ``report_result_value`` is intentionally evaluated even when the caller only
    needs the Chat Output. This makes the visible response explicitly depend on
    session persistence and surfaces a failed write to the user.
    """

    payload = finalize_report_after_session_write(report_result_value)
    original_text = str(getattr(message_value, "text", "") or "")
    has_session_warning = any(
        isinstance(item, dict)
        and item.get("type") in {SESSION_WARNING_TYPE, INVALIDATION_WARNING_TYPE}
        for item in payload.get("warnings", [])
    )
    if isinstance(message_value, Message) and original_text and not has_session_warning:
        return message_value
    text = str(payload.get("message") or original_text)
    message = Message(text=text)
    message.files = []
    message.error = str(payload.get("status") or "") == "error"
    message.category = "error" if message.error else "message"
    return message


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
            display_name="세션 저장 완료 Report 결과",
            info="세션 상태 저장기를 통과한 realtime.production.report.v1 API 응답입니다.",
            required=True,
        ),
        HandleInput(
            name="report_message",
            display_name="Report 메시지",
            info="01 실시간 생산 분석 Report 생성기의 화면용 Markdown Message입니다.",
            input_types=["Message"],
            required=True,
        ),
    ]
    outputs = [
        Output(
            name="message",
            display_name="세션 저장 후 Report 메시지",
            method="build_message",
            types=["Message"],
            group_outputs=True,
        ),
        Output(
            name="api_response",
            display_name="API 응답",
            method="build_api_response",
            types=["Data"],
            group_outputs=True,
        ),
    ]

    # 함수 설명: Chat Output 경로가 세션 저장기 결과에 의존하도록 Report Message를 이 노드에서 전달합니다.
    def build_message(self) -> Message:
        message = report_message_after_session_write(
            getattr(self, "report_result", None),
            getattr(self, "report_message", None),
        )
        self.status = message
        return message

    # 함수 설명: `build_api_response()`는 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
    def build_api_response(self) -> Data:
        payload = finalize_report_after_session_write(getattr(self, "report_result", None))
        self.status = str(payload.get("message") or "")
        return Data(data=payload)
