# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 Report 후속 API 종료 어댑터
# 역할: Session State Writer를 통과한 응답을 화면 Message와 compact terminal Data로 반환합니다.
# 주요 입력: 세션 저장 완료 report.followup.response.v1 payload
# 주요 출력: Chat Output용 Message, Run Flow Tool용 API Data
# 처리 흐름: 응답 계약 검증 -> 세션 저장 경고 반영 -> 내부 state/source 제거 -> 두 terminal 출력 생성
# 유지보수 포인트: raw Report rows, result-store state, query source 전체를 Tool 응답으로 노출하지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


CONTRACT_VERSION = "report.followup.response.v1"
SESSION_WARNING_TYPE = "report_followup_session_state_unavailable"
SESSION_WARNING_TEXT = "이번 후속 분석 조건을 세션에 저장하지 못했습니다. 다음 '그중' 질문은 방금 결과를 이어가지 못할 수 있습니다."


# 함수 설명: 입력 값을 공백이 제거된 문자열로 정규화합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: Langflow Data 또는 dict에서 안전한 payload 사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return deepcopy(raw) if isinstance(raw, dict) else {}


# 함수 설명: 잘못된 입력 계약에 대한 표준 오류 응답을 생성합니다.
def _invalid_contract() -> dict[str, Any]:
    message = "### Report 후속 분석 오류\nReport 후속 응답 계약을 확인할 수 없습니다."
    issue = {"type": "invalid_report_followup_response_contract", "message": "응답 생성기는 report.followup.response.v1 계약을 반환해야 합니다."}
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": "report_followup",
        "status": "error",
        "success": False,
        "summary": issue["message"],
        "message": message,
        "answer_message": message,
        "data": {"row_count": 0, "matched_row_count": 0, "columns": [], "rows": [], "preview_only": False},
        "analysis": {"status": "error", "row_count": 0, "columns": []},
        "warnings": [],
        "errors": [issue],
    }


# 주요 함수: 세션 저장 결과를 반영하고 필요하면 사용자 경고를 추가합니다.
def finalize_report_followup_response(value: Any) -> dict[str, Any]:
    payload = _payload(value)
    if payload.get("contract_version") != CONTRACT_VERSION:
        return _invalid_contract()
    result = deepcopy(payload)
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    session_write = result.get("session_state_write") if isinstance(result.get("session_state_write"), dict) else {}
    if result.get("status") == "ok" and state and session_write.get("saved") is not True:
        warning = {
            "type": SESSION_WARNING_TYPE,
            "message": SESSION_WARNING_TEXT,
            "reason": _text(session_write.get("reason") or "session_state_write_failed"),
            "errors": [_text(item) for item in session_write.get("errors", []) if _text(item)][:5],
        }
        warnings = [deepcopy(item) for item in result.get("warnings", []) if isinstance(item, dict)]
        if not any(item.get("type") == SESSION_WARNING_TYPE for item in warnings):
            warnings.append(warning)
        result["warnings"] = warnings
        notice = f"> ⚠️ {SESSION_WARNING_TEXT}"
        message = _text(result.get("message") or result.get("answer_message"))
        if notice not in message:
            message = f"{message}\n\n{notice}".strip()
        result["message"] = message
        result["answer_message"] = message
    return result


# 주요 함수: 내부 state와 원본 source를 제거한 공개 API 응답을 만듭니다.
def public_report_followup_response(value: Any) -> dict[str, Any]:
    payload = finalize_report_followup_response(value)
    public = {
        key: deepcopy(payload.get(key))
        for key in (
            "contract_version",
            "response_type",
            "status",
            "success",
            "summary",
            "message",
            "answer_message",
            "data",
            "analysis",
            "warnings",
            "errors",
            "session_state_write",
        )
        if key in payload
    }
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    public["inspection"] = {
        key: deepcopy(value)
        for key, value in inspection.items()
        if key in {"report_followup_prompt", "report_followup_plan", "result_loader", "report_snapshot_execution"}
        and isinstance(value, dict)
    }
    return public


# 주요 함수: 최종 Report 후속 응답을 Chat Output용 Message로 변환합니다.
def report_followup_message(value: Any) -> Message:
    payload = finalize_report_followup_response(value)
    message = Message(text=_text(payload.get("message") or payload.get("answer_message")))
    message.files = []
    message.error = payload.get("status") == "error"
    message.category = "error" if message.error else "message"
    return message


# Langflow 컴포넌트 클래스: Report 후속 답변의 Message와 API terminal 출력을 제공합니다.
class ReportFollowupApiTerminal(Component):
    display_name = "04 Report 후속 API 종료 어댑터"
    description = "세션 저장 후 Report 후속 답변을 Chat Message와 compact Run Flow API Data로 반환합니다."
    name = "ReportFollowupApiTerminal"
    icon = "FileJson"

    # 주요 메서드: 컴포넌트를 Flow terminal node로 초기화합니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [DataInput(name="response_payload", display_name="세션 저장 완료 Report 후속 응답", required=True)]
    outputs = [
        Output(name="message", display_name="Report 후속 메시지", method="build_message", types=["Message"], group_outputs=True),
        Output(name="api_response", display_name="Report 후속 API 응답", method="build_api_response", types=["Data"], group_outputs=True),
    ]

    # Langflow 출력 함수: Chat Output에 연결할 최종 Message를 반환합니다.
    def build_message(self) -> Message:
        message = report_followup_message(getattr(self, "response_payload", None))
        self.status = message
        return message

    # Langflow 출력 함수: Run Flow Tool이 소비할 공개 API Data를 반환합니다.
    def build_api_response(self) -> Data:
        payload = public_report_followup_response(getattr(self, "response_payload", None))
        self.status = _text(payload.get("message"))
        return Data(data=payload)
