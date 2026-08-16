# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 Report 후속 응답 생성기
# 역할: 결정론적 실행 결과를 사용자 답변/API payload로 만들고 원래 Report anchor를 유지한 다음 View 계획만 갱신합니다.
# 주요 입력: 02 실행 결과 payload, 표 미리보기 행 수
# 주요 출력: 공용 Session State Writer에 전달할 response payload
# 처리 흐름: 상태별 고정 메시지 생성 -> bounded Markdown 표 -> compact current_view_plan 저장
# 유지보수 포인트: current_data의 report_context/data_ref/source refs를 결과 표로 덮어쓰지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, IntInput, Output
from lfx.schema.data import Data


RESPONSE_CONTRACT_VERSION = "report.followup.response.v1"
DEFAULT_PREVIEW_LIMIT = 10
MAX_PREVIEW_LIMIT = 20


# 함수 설명: 입력 값을 공백이 제거된 문자열로 정규화합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: Langflow Data 또는 dict에서 안전한 payload 사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    if not isinstance(raw, dict):
        return {}
    result = {key: deepcopy(item) for key, item in raw.items() if key not in {"runtime_sources", "_full_result_rows"}}
    if isinstance(raw.get("_full_result_rows"), list):
        result["_full_result_rows"] = raw["_full_result_rows"]
    return result


# 함수 설명: 표 미리보기 행 수를 허용 범위 안의 정수로 제한합니다.
def _bounded_preview_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = DEFAULT_PREVIEW_LIMIT
    return max(1, min(parsed, MAX_PREVIEW_LIMIT))


# 함수 설명: 결과 값을 사용자 표시에 적합한 짧은 문자열로 변환합니다.
def _display_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (float, Decimal)):
        try:
            number = float(value)
            if number != number:
                return "-"
            return f"{number:,.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError, OverflowError):
            pass
    return _text(value) or "-"


# 함수 설명: Markdown 표 셀을 깨뜨리는 문자를 안전하게 이스케이프합니다.
def _markdown_cell(value: Any) -> str:
    return _display_value(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


# 함수 설명: 선택 컬럼과 결과 행으로 Markdown 표를 생성합니다.
def _markdown_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    if not columns or not rows:
        return ""
    header = "| " + " | ".join(_markdown_cell(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_markdown_cell(row.get(column)) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider, *body])


# 함수 설명: trace에서 사용자에게 설명할 마지막 오류를 찾습니다.
def _first_error(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    for item in reversed(trace.get("errors", []) if isinstance(trace.get("errors"), list) else []):
        if isinstance(item, dict) and _text(item.get("message")):
            return item
    return {}


# 함수 설명: 다음 후속 질문에 필요한 최소 View 계획만 보존합니다.
def _compact_view_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    return {
        "contract_version": _text(plan.get("contract_version")),
        "source_alias": _text(plan.get("source_alias")),
        "dataset_key": _text(plan.get("dataset_key")),
        "source_view_key": _text(plan.get("source_view_key")),
        "grain": deepcopy(plan.get("grain")) if isinstance(plan.get("grain"), dict) else {},
        "operations": [deepcopy(item) for item in plan.get("operations", []) if isinstance(item, dict)][:16],
        "output_columns": [str(item) for item in plan.get("output_columns", []) if _text(item)][:50],
    }


# 함수 설명: Report anchor를 유지하면서 다음 턴용 compact session state를 만듭니다.
def _next_state(payload: dict[str, Any], message: str, plan: dict[str, Any]) -> dict[str, Any]:
    state = deepcopy(payload.get("state")) if isinstance(payload.get("state"), dict) else {}
    if not state:
        return {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    current_data = deepcopy(state.get("current_data")) if isinstance(state.get("current_data"), dict) else {}
    compact_plan = _compact_view_plan(plan)
    current_data["current_view_plan"] = compact_plan
    state.update(
        {
            "session_id": _text(state.get("session_id") or request.get("session_id")),
            "last_question": _text(request.get("question")),
            "last_answer_message": message,
            "current_data": current_data,
            "last_intent_plan": {
                "analysis_kind": "report_followup_snapshot_transform",
                "request_scope": "followup_transform",
                "reference_mode": "previous_source",
                "reuse_strategy": "previous_source",
                "retrieval_jobs": [],
                "report_execution_plan": compact_plan,
            },
            "last_applied_criteria": {
                "source_alias": compact_plan.get("source_alias"),
                "operations": deepcopy(compact_plan.get("operations", [])),
            },
        }
    )
    return state


# 함수 설명: Report 교체 경합을 막을 세션 revision과 context 참조 조건을 만듭니다.
def _session_state_guard(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    context = current_data.get("report_context") if isinstance(current_data.get("report_context"), dict) else {}
    context_ref = _text(context.get("context_ref"))
    try:
        revision = int(state.get("_session_state_revision"))
    except (TypeError, ValueError, OverflowError):
        return {}
    if revision < 0 or not context_ref:
        return {}
    return {
        "expected_turn_count": revision,
        "expected_report_context_ref": context_ref,
    }


# 함수 설명: 실행 상태별 사용자 안내 메시지를 결정론적으로 생성합니다.
def _error_message(gate_status: str, issue: dict[str, Any]) -> str:
    if gate_status == "handoff_required":
        return "### 답변\n이 요청은 저장된 Report 내부 조회가 아니라 현재·최신 원천 데이터 조회가 필요합니다. 일반 데이터 분석 Flow에서 실행해 주세요."
    if gate_status == "clarification_required":
        return f"### 확인 필요\n{_text(issue.get('message')) or '사용할 Report View를 하나로 확정할 수 없습니다.'}"
    if issue.get("type") == "report_context_missing":
        return "### 확인 필요\n같은 세션에서 유효한 Report를 찾을 수 없습니다. 먼저 Report를 생성한 뒤 이어서 질문해 주세요."
    return f"### Report 후속 분석 오류\n{_text(issue.get('message')) or '저장된 Report 데이터를 안전하게 분석하지 못했습니다.'}"


# 주요 함수: 실행 결과를 답변 표와 세션 저장 계약이 포함된 응답으로 변환합니다.
def build_report_followup_response(payload_value: Any, table_preview_limit: Any = DEFAULT_PREVIEW_LIMIT) -> dict[str, Any]:
    payload = _payload(payload_value)
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    gate = payload.get("execution_gate") if isinstance(payload.get("execution_gate"), dict) else {}
    gate_status = _text(gate.get("status")).casefold()
    intent = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    plan = intent.get("report_execution_plan") if isinstance(intent.get("report_execution_plan"), dict) else {}
    warnings = [deepcopy(item) for item in payload.get("trace", {}).get("warnings", []) if isinstance(item, dict)] if isinstance(payload.get("trace"), dict) else []
    errors = [deepcopy(item) for item in payload.get("trace", {}).get("errors", []) if isinstance(item, dict)] if isinstance(payload.get("trace"), dict) else []

    if analysis.get("status") != "ok" or gate_status != "ready":
        issue = _first_error(payload)
        message = _error_message(gate_status, issue)
        status = "clarification_required" if gate_status == "clarification_required" else "handoff_required" if gate_status == "handoff_required" else "error"
        return {
            "contract_version": RESPONSE_CONTRACT_VERSION,
            "response_type": "report_followup",
            "status": status,
            "success": status in {"clarification_required", "handoff_required"},
            "summary": _text(issue.get("message")),
            "message": message,
            "answer_message": message,
            "data": {"row_count": 0, "matched_row_count": 0, "columns": [], "rows": [], "preview_only": False},
            "analysis": deepcopy(analysis),
            "state": {},
            "warnings": warnings,
            "errors": errors,
            "trace": deepcopy(payload.get("trace")) if isinstance(payload.get("trace"), dict) else {},
        }

    columns = [str(item) for item in data.get("columns", []) if _text(item)] if isinstance(data.get("columns"), list) else []
    full_rows = payload.get("_full_result_rows") if isinstance(payload.get("_full_result_rows"), list) else data.get("rows", [])
    rows = [deepcopy(row) for row in full_rows if isinstance(row, dict)]
    limit = _bounded_preview_limit(table_preview_limit)
    preview_rows = rows[:limit]
    matched_count = int(data.get("matched_row_count") or analysis.get("matched_row_count") or len(rows))
    result_count = int(data.get("row_count") or len(rows))
    if result_count == 0:
        message = "### 답변\n방금 Report에서 요청 조건에 맞는 결과를 찾지 못했습니다."
    else:
        if result_count < matched_count:
            summary = f"방금 Report에서 조건에 맞는 결과는 총 {matched_count:,}건이며, 요청한 순서와 개수에 따라 {result_count:,}건을 표시했습니다."
        else:
            summary = f"방금 Report에서 조건에 맞는 결과는 총 {result_count:,}건입니다."
        table = _markdown_table(columns, preview_rows)
        preview_notice = f"\n\n총 {result_count:,}건 중 {len(preview_rows):,}건을 표시했습니다." if len(preview_rows) < result_count else ""
        message = f"### 답변\n{summary}"
        if table:
            message += f"\n\n### 결과 테이블\n{table}{preview_notice}"

    next_state = _next_state(payload, message, plan)
    state_guard = _session_state_guard(payload)
    if not state_guard:
        issue = {
            "type": "report_session_state_guard_missing",
            "message": "Report 후속 분석 결과를 저장할 세션 revision/context 보호 조건이 없습니다.",
        }
        errors.append(issue)
        error_message = _error_message("blocked", issue)
        return {
            "contract_version": RESPONSE_CONTRACT_VERSION,
            "response_type": "report_followup",
            "status": "error",
            "success": False,
            "summary": issue["message"],
            "message": error_message,
            "answer_message": error_message,
            "data": {"row_count": 0, "matched_row_count": 0, "columns": [], "rows": [], "preview_only": False},
            "analysis": {**deepcopy(analysis), "status": "error"},
            "state": {},
            "warnings": warnings,
            "errors": errors,
            "trace": deepcopy(payload.get("trace")) if isinstance(payload.get("trace"), dict) else {},
        }
    public_data = {
        "row_count": result_count,
        "matched_row_count": matched_count,
        "columns": columns,
        "rows": preview_rows,
        "preview_only": len(preview_rows) < result_count,
    }
    return {
        "contract_version": RESPONSE_CONTRACT_VERSION,
        "response_type": "report_followup",
        "status": "ok",
        "success": True,
        "summary": message.split("\n", 2)[1] if "\n" in message else message,
        "message": message,
        "answer_message": message,
        "data": public_data,
        "analysis": deepcopy(analysis),
        "state": next_state,
        "session_state_guard": state_guard,
        "warnings": warnings,
        "errors": [],
        "trace": deepcopy(payload.get("trace")) if isinstance(payload.get("trace"), dict) else {},
    }


# Langflow 컴포넌트 클래스: Report 후속 결과와 guarded session state를 조립합니다.
class ReportFollowupResponseBuilder(Component):
    display_name = "03 Report 후속 응답 생성기"
    description = "결정론적 결과를 표로 표시하고 Report anchor를 유지한 compact state를 만듭니다."
    name = "ReportFollowupResponseBuilder"
    icon = "MessageSquareText"
    inputs = [
        DataInput(name="payload", display_name="Report 후속 실행 결과", required=True),
        IntInput(name="table_preview_limit", display_name="결과 표 미리보기 행 수", value=DEFAULT_PREVIEW_LIMIT, required=False, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="Report 후속 응답 페이로드", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 화면과 세션 Writer가 함께 사용할 응답 payload를 반환합니다.
    def build_payload(self) -> Data:
        result = build_report_followup_response(getattr(self, "payload", None), getattr(self, "table_preview_limit", DEFAULT_PREVIEW_LIMIT))
        self.status = _text(result.get("message"))
        return Data(data=result)
