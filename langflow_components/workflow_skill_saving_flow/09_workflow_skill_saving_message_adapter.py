# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 09 Workflow Skill 등록 메시지 어댑터
# 역할: 구조화된 등록 결과를 JSON 노출 없이 읽기 쉬운 Markdown 메시지 하나로 렌더링합니다.
# 주요 입력: 페이로드(payload)
# 주요 출력: 메시지(message)
# 처리 흐름: 요약 -> 등록 대상 표 -> 순차 단계 -> 확인할 점 -> 다음 단계 순으로 표시합니다.
# 유지보수 포인트: 이 출력만 Chat Output에 연결해 중간 LLM JSON과 질문이 채팅에 반복 저장되지 않게 합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

CELL_LIMIT = 180


# 주요 함수: Workflow Skill 등록 응답을 사용자가 읽을 수 있는 단일 Markdown 문자열로 변환합니다.
def build_message(payload_value: Any) -> str:
    payload = _payload(payload_value)
    sections = _dict(payload.get("answer_sections"))
    result = []
    headline = str(_dict(sections.get("summary")).get("headline") or payload.get("message") or "").strip()
    if headline:
        result.append("### 등록 결과\n" + headline)
    points = [str(item).strip() for item in _list(sections.get("key_points")) if str(item or "").strip()]
    if points:
        result.append("### 한눈에 보기\n" + "\n".join(f"- {point}" for point in points))
    table = _dict(sections.get("target_table"))
    rows = [_dict(row) for row in _list(table.get("rows"))]
    if rows:
        columns = [str(value) for value in _list(table.get("columns"))]
        result.append(f"### {str(table.get('title') or '등록 대상')}\n" + _markdown_table(rows, columns) + f"\n\n총 {len(rows)}건입니다.")
    steps = [_dict(step) for step in _list(sections.get("workflow_steps"))]
    if steps:
        lines = ["### 실행 순서"]
        for step in steps:
            lines.append(f"{step.get('순서')}. `{step.get('단계')}` · `{step.get('Tool')}`")
            lines.append(f"   - 질문: {step.get('질문')}")
            lines.append(f"   - 선행 단계: {step.get('선행 단계')} / 결과 전달: {step.get('결과 전달')} / 오류 정책: {step.get('오류 정책')}")
        result.append("\n".join(lines))
    notices = [_dict(item) for item in _list(sections.get("notices"))]
    if notices:
        lines = ["### 확인할 점"]
        lines.extend(f"- {item.get('title') or item.get('type')}: {item.get('message')}" for item in notices if str(item.get("message") or "").strip())
        result.append("\n".join(lines))
    next_steps = [str(item).strip() for item in _list(sections.get("next_steps")) if str(item or "").strip()]
    if next_steps:
        result.append("### 다음 단계\n" + "\n".join(f"- {step}" for step in next_steps))
    return "\n\n".join(result) if result else json.dumps(payload, ensure_ascii=False, default=str)


# 함수 설명: `_markdown_table()`은 지정된 컬럼 순서로 escape가 적용된 Markdown 표를 만듭니다.
def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    header = "| " + " | ".join(_escape(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_escape(row.get(column, "")) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider, *body])


# 함수 설명: `_escape()`는 표 셀을 깨뜨리는 줄바꿈과 구분자를 치환하고 과도한 길이를 제한합니다.
def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", "<br>").replace("|", "\\|")
    return text[: CELL_LIMIT - 3] + "..." if len(text) > CELL_LIMIT else text


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


# Langflow 컴포넌트 클래스: 구조화 등록 결과를 Chat Output용 단일 Message로 제공합니다.
class WorkflowSkillSavingMessageAdapter(Component):
    display_name = "09 Workflow Skill 등록 메시지 어댑터"
    description = "Workflow Skill 등록 결과와 실행 단계를 JSON 없이 읽기 쉬운 Markdown으로 표시합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="message", display_name="메시지", method="build_output_message", types=["Message"])]

    # Langflow 출력 함수: 최종 Chat Output에 연결할 등록 결과 Message를 반환합니다.
    def build_output_message(self) -> Message:
        return Message(text=build_message(getattr(self, "payload", None)))
