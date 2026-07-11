# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 09 메인 플로우 필터 등록 메시지 어댑터
# 역할: 메인 플로우 필터 등록 결과 페이로드를 Playground 채팅용 서비스형 Markdown 메시지로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 메시지 (message)
# 처리 흐름: 구조화된 메인 플로우 필터 저장 결과를 요약·대상 표·다음 단계가 포함된 Markdown Message 하나로 렌더링합니다.
# 유지보수 포인트: 이 노드만 최종 Chat Output에 연결해 중간 질문이나 JSON이 대화 기록에 중복 출력되지 않게 합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

TABLE_LIMIT = 12
CELL_LIMIT = 140


# 주요 함수: 구조화 결과를 사용자가 읽을 수 있는 단일 Markdown Message로 변환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_message(payload_value: Any) -> str:
    payload = _payload(payload_value)
    sections = []
    answer_sections = _dict(payload.get("answer_sections"))
    summary = _dict(answer_sections.get("summary"))
    headline = str(summary.get("headline") or payload.get("answer_message") or payload.get("message") or "").strip()
    if headline:
        sections.append("### 등록 결과\n" + headline)
    key_points = _list(answer_sections.get("key_points"))
    if key_points:
        sections.append("### 한눈에 보기\n" + "\n".join(f"- {str(point).strip()}" for point in key_points if str(point).strip()))
    table_section = _table_section(_dict(answer_sections.get("target_table")), _dict(payload.get("data")))
    if table_section:
        sections.append(table_section)
    notices = _notices_section(answer_sections.get("notices"))
    if notices:
        sections.append(notices)
    next_steps = _next_steps_section(answer_sections.get("next_steps"))
    if next_steps:
        sections.append(next_steps)
    return "\n\n".join(sections) if sections else json.dumps(payload, ensure_ascii=False, default=str)


def _table_section(table: dict[str, Any], data: dict[str, Any]) -> str:
    rows = _row_list(table.get("rows")) or (_row_list(data.get("rows")) if table.get("row_source") == "data.rows" else [])
    if not rows:
        return ""
    columns = _string_list(table.get("columns")) or _columns_from_rows(rows)
    preview_rows = rows[: int(table.get("display_limit") or TABLE_LIMIT)]
    title = str(table.get("title") or "등록 대상").strip()
    note = f"\n\n총 {len(rows)}건 중 {len(preview_rows)}건을 표시했습니다." if len(rows) > len(preview_rows) else f"\n\n총 {len(rows)}건입니다."
    return f"### {title}\n" + _markdown_table(preview_rows, columns) + note


def _notices_section(value: Any) -> str:
    notices = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if not notices:
        return ""
    lines = ["### 확인할 점"]
    for item in notices[:8]:
        title = str(item.get("title") or item.get("type") or "안내").strip()
        message = str(item.get("message") or "").strip()
        if message:
            lines.append(f"- {title}: {message}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _next_steps_section(value: Any) -> str:
    steps = [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []
    if not steps:
        return ""
    return "### 다음 단계\n" + "\n".join(f"- {step}" for step in steps[:5])


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    header = "| " + " | ".join(_escape(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_escape(row.get(column, "")) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider] + body)


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _row_list(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    return columns


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", "<br>").replace("|", "\\|")
    return text[: CELL_LIMIT - 3] + "..." if len(text) > CELL_LIMIT else text


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MainFlowFilterSavingMessageAdapter(Component):
    display_name = "09 메인 플로우 필터 등록 메시지 어댑터"
    description = "메인 플로우 필터 등록 결과 페이로드를 Playground 채팅용 서비스형 Markdown 메시지로 변환합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="message", display_name="메시지", method="build_output_message", types=["Message"])]

    # Langflow 출력 함수: '메시지 (message)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_message(self) -> Message:
        return Message(text=build_message(getattr(self, "payload", None)))
