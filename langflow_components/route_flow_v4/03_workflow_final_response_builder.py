# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 Workflow 최종 응답 생성기
# 역할: 마지막 native 모델 답변과 결정론적 Workflow 실행 상태를 단일 Chat Message/API 응답으로 결합합니다.
# 주요 입력: 02 최종 Context Data, 최종 모델 응답 Message/Text
# 주요 출력: 단일 답변 Message, terminal API 응답 Data
# 처리 흐름: final context 검증 -> 모델 응답 확인 -> partial/error 상태 보강 -> compact workflow API envelope 생성
# 유지보수 포인트: 모델 응답이 비거나 오류여도 실행 단계 실패를 숨기지 않으며 prompt_variables와 result_ref는 API에서 제외합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

FINAL_CONTEXT_CONTRACT_VERSION = "workflow.final_context.v1"
RESPONSE_TYPE = "workflow_orchestration"
MAX_MESSAGE_CHARS = 16_000
MAX_STEP_SUMMARY_CHARS = 800


# 주요 함수: 모델 답변과 실행 결과를 한 개의 사용자 Message 및 API payload 계약으로 구성합니다.
def build_workflow_final_response(final_context_value: Any, final_model_response_value: Any) -> dict[str, Any]:
    context_input = _payload(final_context_value)
    workflow, context_errors = _workflow_context(context_input)
    model_text, model_errors = _model_response(final_model_response_value)
    execution_status = str(workflow.get("execution_status") or context_input.get("status") or "error").strip().lower()
    steps = [_compact_api_step(item) for item in _list(workflow.get("steps")) if isinstance(item, dict)]
    errors = _merge_issues(
        context_errors,
        context_input.get("errors"),
        workflow.get("errors"),
        model_errors,
        *[step.get("errors") for step in steps],
    )

    if model_text and not model_errors:
        message = _clip(model_text, MAX_MESSAGE_CHARS)
        if execution_status in {"partial", "error", "stopped"}:
            message = _append_execution_notice(message, execution_status, steps)
    else:
        message = _deterministic_failure_message(execution_status, steps, errors)

    api_status = _api_status(execution_status, bool(model_text and not model_errors), steps, errors)
    workflow_summary = {
        "contract_version": FINAL_CONTEXT_CONTRACT_VERSION,
        "workflow_run_id": _clip(workflow.get("workflow_run_id"), 160),
        "workflow_key": _clip(workflow.get("workflow_key"), 128),
        "execution_status": execution_status,
        "step_count": len(steps),
        "steps": steps,
    }
    api_response = {
        "response_type": RESPONSE_TYPE,
        "status": api_status,
        "message": message,
        "workflow": workflow_summary,
        "errors": errors,
    }
    return {"message": message, "api_response": api_response}


# 함수 설명: `_workflow_context()`는 02 Data 안의 JSON 문자열을 workflow.final_context.v1 dict로 검증합니다.
def _workflow_context(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = value.get("workflow_context")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {}, [_issue("final_context_parse_error", f"최종 Workflow Context JSON을 해석하지 못했습니다: {exc}")]
    if not isinstance(raw, dict):
        return {}, [_issue("final_context_missing", "최종 Workflow Context가 비어 있습니다.")]
    if str(raw.get("contract_version") or "") != FINAL_CONTEXT_CONTRACT_VERSION:
        return {}, [_issue("invalid_final_context_contract", "최종 Context 계약이 workflow.final_context.v1이 아닙니다.")]
    return deepcopy(raw), []


# 함수 설명: `_model_response()`는 Message/Text를 읽고 명시적 status/error payload를 최종 모델 오류로 분리합니다.
def _model_response(value: Any) -> tuple[str, list[dict[str, Any]]]:
    data = getattr(value, "data", None)
    errors: list[dict[str, Any]] = []
    if isinstance(data, dict):
        errors.extend(_issues(data.get("errors")))
        if str(data.get("status") or "").strip().lower() in {"error", "failed", "failure"}:
            errors.append(_issue("final_model_error", str(data.get("message") or "최종 모델 실행이 실패했습니다.")))
        text = data.get("message") or data.get("text") or getattr(value, "text", "") or ""
    else:
        text = getattr(value, "text", value)
    text = str(text or "").strip()
    if not text:
        errors.append(_issue("final_model_response_empty", "최종 모델 응답이 비어 있습니다."))
    return text, _merge_issues(errors)


# 함수 설명: `_compact_api_step()`은 API에 단계 상태·요약·오류만 남기고 내부 참조와 식별자 목록을 제거합니다.
def _compact_api_step(value: dict[str, Any]) -> dict[str, Any]:
    meta = value.get("result_ref_meta") if isinstance(value.get("result_ref_meta"), dict) else {}
    return {
        "step_index": _safe_int(value.get("step_index")),
        "step_id": _clip(value.get("step_id"), 64),
        "tool_name": _clip(value.get("tool_name"), 100),
        "status": _clip(value.get("status"), 20),
        "summary": _clip(value.get("summary"), MAX_STEP_SUMMARY_CHARS),
        "row_count": meta.get("row_count"),
        "warnings": _issues(value.get("warnings"), count=3, text_limit=240),
        "errors": _issues(value.get("errors"), count=3, text_limit=240),
    }


# 함수 설명: `_append_execution_notice()`는 모델이 생성한 본문 뒤에 실패/누락 단계를 결정론적으로 한 번 표시합니다.
def _append_execution_notice(message: str, execution_status: str, steps: list[dict[str, Any]]) -> str:
    failed = [step for step in steps if str(step.get("status")) not in {"ok", "partial"}]
    lines = [message.rstrip(), "", "### Workflow 실행 상태", f"- 전체 상태: {execution_status}"]
    if failed:
        for step in failed:
            reason = _first_issue_message(step.get("errors")) or str(step.get("summary") or "실행 실패")
            lines.append(f"- {step.get('step_id') or step.get('tool_name')}: {_clip(reason, 240)}")
    else:
        lines.append("- 일부 단계 결과가 완전하지 않아 제공 가능한 범위만 종합했습니다.")
    return _clip("\n".join(lines), MAX_MESSAGE_CHARS)


# 함수 설명: `_deterministic_failure_message()`는 모델을 사용할 수 없을 때 성공/실패 단계 요약만으로 사용자에게 상태를 설명합니다.
def _deterministic_failure_message(
    execution_status: str,
    steps: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> str:
    lines = ["### 답변", "최종 답변 생성 모델의 응답을 사용할 수 없어 Workflow 실행 상태를 안내드립니다.", "", "### Workflow 실행 결과"]
    lines.append(f"- 전체 상태: {execution_status}")
    for step in steps:
        label = step.get("step_id") or step.get("tool_name") or f"step-{step.get('step_index')}"
        summary = str(step.get("summary") or _first_issue_message(step.get("errors")) or "결과 요약 없음")
        lines.append(f"- {label} ({step.get('status') or 'unknown'}): {_clip(summary, 400)}")
    if errors:
        lines.extend(("", "### 경고/오류"))
        for error in errors[:5]:
            lines.append(f"- {_clip(error.get('message'), 300)}")
    return _clip("\n".join(lines), MAX_MESSAGE_CHARS)


# 함수 설명: `_api_status()`는 모델 응답과 단계 성공 범위를 API의 ok/partial/error 상태로 정규화합니다.
def _api_status(
    execution_status: str,
    model_ok: bool,
    steps: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> str:
    if not model_ok:
        return "error"
    if execution_status == "complete" and not errors:
        return "ok"
    if any(str(step.get("status")) in {"ok", "partial"} for step in steps):
        return "partial"
    return "error"


# 함수 설명: `_merge_issues()`는 여러 오류·경고 입력을 type/message 기준으로 중복 없이 최대 12건 합칩니다.
def _merge_issues(*values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        for item in _issues(value, count=12, text_limit=320):
            marker = (str(item.get("type") or ""), str(item.get("message") or ""))
            if marker in seen:
                continue
            seen.add(marker)
            result.append(item)
            if len(result) >= 12:
                return result
    return result


# 함수 설명: `_issues()`는 dict/string 오류를 짧은 표준 type/message 목록으로 바꿉니다.
def _issues(value: Any, count: int = 8, text_limit: int = 320) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else ([value] if value not in (None, "", {}, []) else [])
    result: list[dict[str, Any]] = []
    for item in values[:count]:
        if isinstance(item, dict):
            result.append({"type": _clip(item.get("type") or "error", 100), "message": _clip(item.get("message"), text_limit)})
        else:
            result.append({"type": "error", "message": _clip(item, text_limit)})
    return result


# 함수 설명: `_first_issue_message()`는 단계 오류 목록에서 첫 사람이 읽을 수 있는 message를 반환합니다.
def _first_issue_message(value: Any) -> str:
    issues = _issues(value, count=1)
    return str(issues[0].get("message") or "") if issues else ""


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 최종 Context payload 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_list()`는 list가 아닌 값을 빈 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_safe_int()`는 잘못된 외부 step index도 최종 상태 메시지로 처리할 수 있도록 0으로 변환합니다.
def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


# 함수 설명: `_clip()`은 긴 문자열을 허용 길이 안에서 말줄임 처리합니다.
def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: `_issue()`는 최종 응답 오류를 표준 type/message dict로 만듭니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": message, **extra}


# Langflow 컴포넌트 클래스: native 모델 뒤에서 단일 ChatOutput과 terminal api_response를 동시에 제공합니다.
class WorkflowFinalResponseBuilder(Component):
    display_name = "03 Workflow 최종 응답 생성기"
    description = "마지막 모델 응답과 Workflow 실행 상태를 단일 사용자 Message/API 응답으로 결합합니다."
    name = "WorkflowFinalResponseBuilder"
    icon = "MessageSquareText"

    inputs = [
        HandleInput(
            name="final_context",
            display_name="최종 Context Data",
            info="02 최종 합성 Context 생성기의 final_context 출력입니다.",
            input_types=["Data"],
            required=True,
        ),
        HandleInput(
            name="final_model_response",
            display_name="최종 모델 응답",
            info="마지막 native Language Model 또는 Agent의 Message/Text 결과입니다.",
            input_types=["Message", "Data"],
            required=False,
        ),
    ]
    outputs = [
        Output(name="message", display_name="최종 답변 Message", method="build_message", types=["Message"], group_outputs=True),
        Output(name="api_response", display_name="Terminal API 응답", method="build_api_response", types=["Data"], group_outputs=True),
    ]

    # 함수 설명: `_result_once()`는 Message/API 출력이 같은 모델 응답과 Workflow 상태를 사용하도록 결과를 캐시합니다.
    def _result_once(self) -> dict[str, Any]:
        context = getattr(self, "final_context", None)
        response = getattr(self, "final_model_response", None)
        cache_key = (id(context), id(response))
        if getattr(self, "_workflow_response_cache_key", None) != cache_key:
            self._workflow_response_cache_key = cache_key
            self._workflow_response_result = build_workflow_final_response(context, response)
        return self._workflow_response_result

    # Langflow 출력 함수: Playground의 유일한 Chat Output에 연결할 최종 Message를 반환합니다.
    def build_message(self) -> Message:
        result = self._result_once()
        self.status = {"status": result["api_response"].get("status"), "response_type": RESPONSE_TYPE}
        return Message(text=result["message"])

    # Langflow 출력 함수: Run/API 호출자가 단계 상태를 확인할 terminal Data를 반환합니다.
    def build_api_response(self) -> Data:
        return Data(data=deepcopy(self._result_once()["api_response"]))
