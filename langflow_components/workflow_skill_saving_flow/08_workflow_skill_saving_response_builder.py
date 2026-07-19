# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 08 Workflow Skill 등록 응답 정규화기
# 역할: 검수·저장 결과를 채팅과 API가 함께 사용하는 간결한 구조화 응답으로 정리합니다.
# 주요 입력: 페이로드(payload)
# 주요 출력: 페이로드 출력(payload_out)
# 처리 흐름: 저장 상태 판정 -> Workflow·단계 요약 -> 안내·다음 단계 -> compact write result 구성
# 유지보수 포인트: 전체 MongoDB 문서나 내부 중복 조회 payload를 응답에 복제하지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


# 주요 함수: 내부 Workflow Skill 저장 페이로드를 사용자 응답용 compact 계약으로 변환합니다.
def build_response(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    write_result = _dict(payload.get("write_result"))
    review = _dict(payload.get("review"))
    items = [_dict(item) for item in _list(payload.get("items")) if isinstance(item, dict)]
    status = _status(write_result, review)
    rows = [_row(item, write_result) for item in items]
    message = _headline(status, write_result)
    return {
        "metadata_type": "workflow_skill",
        "metadata_label": "Workflow Skill",
        "status": status,
        "success": status in {"dry_run", "saved", "skipped"},
        "message": message,
        "answer_message": message,
        "answer_sections": {
            "summary": {"headline": message},
            "key_points": _key_points(items, write_result),
            "target_table": {
                "title": "등록 대상 Workflow Skill",
                "columns": ["Workflow Key", "표시 이름", "단계 수", "실행 Tool", "처리"],
                "rows": rows,
                "display_limit": 5,
            },
            "workflow_steps": _step_sections(items),
            "notices": _notices(review, write_result, payload),
            "next_steps": _next_steps(status),
        },
        "data": {"columns": ["Workflow Key", "표시 이름", "단계 수", "실행 Tool", "처리"], "rows": rows, "row_count": len(rows)},
        "metadata_authoring": {
            "requested_action": str(_dict(payload.get("request")).get("duplicate_action") or "skip"),
            "dry_run": bool(write_result.get("dry_run", _dict(payload.get("request")).get("dry_run", True))),
            "keys": [str(item.get("key") or "") for item in items],
            "workflow_steps": _step_sections(items),
        },
        "write_result": _compact_write_result(write_result),
        "trace": {"workflow_contract": deepcopy(_dict(_dict(payload.get("trace")).get("workflow_contract")))},
    }


# 함수 설명: `_status()`는 Writer 결과와 review 상태를 외부 응답의 고정 상태로 변환합니다.
def _status(write_result: dict[str, Any], review: dict[str, Any]) -> str:
    if write_result.get("dry_run") and write_result.get("success"):
        return "dry_run"
    status = str(write_result.get("status") or "")
    if status in {"saved", "skipped", "needs_input", "error", "partial_success"}:
        return status
    if _list(review.get("supplement_requests")):
        return "needs_input"
    return "error" if write_result.get("success") is False else "unknown"


# 함수 설명: `_headline()`은 상태별 등록 결과를 한 문장으로 요약합니다.
def _headline(status: str, write_result: dict[str, Any]) -> str:
    if status == "dry_run":
        return f"Workflow Skill {int(write_result.get('would_save_count') or 0)}건의 저장 계획을 확인했습니다."
    if status == "saved":
        return f"Workflow Skill {int(write_result.get('saved_count') or 0)}건을 저장했습니다."
    if status == "skipped":
        return "유사한 기존 Workflow Skill을 유지하고 저장을 건너뛰었습니다."
    if status == "needs_input":
        return "Workflow Skill을 확정하려면 입력 정보를 보완해야 합니다."
    if status == "partial_success":
        return "Workflow Skill 저장이 일부만 완료되었습니다."
    return str(write_result.get("message") or "Workflow Skill 저장을 완료하지 못했습니다.")


# 함수 설명: `_row()`는 Workflow 후보와 operation을 사용자 표의 한 행으로 변환합니다.
def _row(item: dict[str, Any], write_result: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    steps = [_dict(step) for step in _list(payload.get("steps"))]
    operation = _operation_for_key(str(item.get("key") or ""), write_result)
    return {
        "Workflow Key": str(item.get("key") or ""),
        "표시 이름": str(payload.get("display_name") or ""),
        "단계 수": len(steps),
        "실행 Tool": " → ".join(str(step.get("tool_name") or "") for step in steps),
        "처리": _operation_label(operation, bool(write_result.get("dry_run"))),
    }


# 함수 설명: `_step_sections()`는 각 단계의 질문·의존성·handoff를 JSON 대신 읽기 쉬운 요약 목록으로 만듭니다.
def _step_sections(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        payload = _dict(item.get("payload"))
        for index, step in enumerate(_list(payload.get("steps")), start=1):
            step = _dict(step)
            dependencies = ", ".join(str(value) for value in _list(step.get("depends_on"))) or "없음"
            result.append(
                {
                    "순서": index,
                    "단계": str(step.get("step_id") or ""),
                    "Tool": str(step.get("tool_name") or ""),
                    "질문": str(step.get("question") or ""),
                    "선행 단계": dependencies,
                    "결과 전달": "앞 단계 실제 결과 사용" if step.get("handoff") == "result_ref" else "순서만 보장",
                    "오류 정책": "중단" if step.get("on_error") == "stop" else "다음 단계 계속",
                }
            )
    return result


# 함수 설명: `_key_points()`는 생성 후보와 저장 결과에서 한눈에 볼 핵심 건수를 계산합니다.
def _key_points(items: list[dict[str, Any]], write_result: dict[str, Any]) -> list[str]:
    step_count = sum(len(_list(_dict(item.get("payload")).get("steps"))) for item in items)
    return [
        f"생성된 Workflow Skill 후보는 {len(items)}건입니다.",
        f"순차 실행 단계는 총 {step_count}개입니다.",
        f"저장 처리 결과: {_operation_summary(write_result) or str(write_result.get('message') or '처리 결과 없음')}",
    ]


# 함수 설명: `_notices()`는 오류·보완 요청·가정을 중복 없는 사용자 안내 목록으로 정리합니다.
def _notices(review: dict[str, Any], write_result: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, str]]:
    notices = []
    for error in _list(write_result.get("errors")) + _list(review.get("errors")):
        message = str(_dict(error).get("message") or error).strip()
        if message and message not in {item["message"] for item in notices}:
            notices.append({"type": "error", "title": "오류", "message": message})
    for message in _list(review.get("supplement_requests")):
        text = str(message).strip()
        if text:
            notices.append({"type": "supplement", "title": "추가 확인 필요", "message": text})
    for message in _list(review.get("assumptions")):
        text = str(message).strip()
        if text:
            notices.append({"type": "info", "title": "적용 가정", "message": text})
    if payload.get("conflict_warnings"):
        notices.append({"type": "warning", "title": "유사 항목", "message": f"기존 Workflow Skill과 겹치는 후보 {len(_list(payload.get('conflict_warnings')))}건을 확인했습니다."})
    return notices[:12]


# 함수 설명: `_next_steps()`는 dry-run·저장·오류 상태에 맞는 다음 작업을 안내합니다.
def _next_steps(status: str) -> list[str]:
    if status == "dry_run":
        return ["저장 계획과 단계 순서가 맞으면 Dry Run을 false로 바꿔 다시 실행하세요.", "저장 후 08 Workflow Orchestrator에서 intent example 또는 workflow key로 호출해 보세요."]
    if status == "saved":
        return ["08 Workflow Orchestrator의 다음 실행에서 등록한 Workflow Skill을 호출해 보세요.", "잘못 선택되면 keywords와 excluded_keywords를 조정하세요."]
    if status == "needs_input":
        return ["확인할 점의 누락 정보를 원문에 추가해 다시 실행하세요."]
    return ["오류 메시지와 MongoDB standalone 입력값을 확인한 뒤 다시 실행하세요."]


# 함수 설명: `_operation_for_key()`는 현재 Workflow 후보에 대응하는 Writer operation을 찾습니다.
def _operation_for_key(key: str, write_result: dict[str, Any]) -> str:
    operations = [_dict(item) for item in _list(write_result.get("operation_by_key"))]
    for operation in operations:
        if key in {str(operation.get("key") or ""), str(operation.get("requested_key") or "")}:
            return str(operation.get("operation") or "")
    return str(operations[0].get("operation") or "") if len(operations) == 1 else ""


# 함수 설명: `_operation_label()`은 내부 operation 이름을 자연어 처리 상태로 변환합니다.
def _operation_label(operation: str, dry_run: bool) -> str:
    labels = {"inserted": "신규 저장", "replaced": "기존 Skill 교체", "merged": "기존 Skill 병합", "skipped": "기존 Skill 유지", "created_new": "새 Key로 저장"}
    label = labels.get(operation, "처리 안 함" if not operation else operation)
    return f"{label} 예정" if dry_run and operation != "skipped" else label


# 함수 설명: `_operation_summary()`는 Writer operation별 건수를 한 문장으로 요약합니다.
def _operation_summary(write_result: dict[str, Any]) -> str:
    counts = {}
    for item in _list(write_result.get("operation_by_key")):
        operation = str(_dict(item).get("operation") or "")
        if operation:
            label = _operation_label(operation, bool(write_result.get("dry_run")))
            counts[label] = counts.get(label, 0) + 1
    return ", ".join(f"{label} {count}건" for label, count in counts.items())


# 함수 설명: `_compact_write_result()`는 외부 응답에 필요한 저장 상태·operation·오류만 남깁니다.
def _compact_write_result(write_result: dict[str, Any]) -> dict[str, Any]:
    allowed = {"success", "ready_to_save", "status", "dry_run", "saved_count", "would_save_count", "skipped_count", "operation_by_key", "database", "collection_name", "message", "errors", "keys"}
    return {key: deepcopy(value) for key, value in write_result.items() if key in allowed}


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 변경에 안전한 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 값이 dict일 때만 반환하고 아니면 빈 dict를 사용합니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 값이 list일 때만 반환하고 아니면 빈 목록을 사용합니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# Langflow 컴포넌트 클래스: Writer 결과를 단일 구조화 응답 포트로 제공합니다.
class WorkflowSkillSavingResponseBuilder(Component):
    display_name = "08 Workflow Skill 등록 응답 정규화기"
    description = "Workflow Skill 검수·저장 결과를 채팅과 API가 공유할 compact 응답으로 변환합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 현재 저장 결과를 사용자 응답용 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(data=build_response(getattr(self, "payload", None)))
