# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 09 테이블 카탈로그 등록 메시지 어댑터
# 역할: 테이블 카탈로그 등록 결과 페이로드를 Playground 채팅용 서비스형 Markdown 메시지로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 메시지 (message)
# 처리 흐름: 구조화된 테이블 카탈로그 저장 결과를 요약·대상 표·다음 단계가 포함된 Markdown Message 하나로 렌더링합니다.
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


# 함수 설명: `_table_section()`는 구조화 rows/columns를 최종 답변의 표 section 계약으로 변환합니다.
def _table_section(table: dict[str, Any], data: dict[str, Any]) -> str:
    rows = _row_list(table.get("rows")) or (_row_list(data.get("rows")) if table.get("row_source") == "data.rows" else [])
    if not rows:
        return ""
    columns = _string_list(table.get("columns")) or _columns_from_rows(rows)
    preview_rows = rows[: int(table.get("display_limit") or TABLE_LIMIT)]
    title = str(table.get("title") or "등록 대상").strip()
    note = f"\n\n총 {len(rows)}건 중 {len(preview_rows)}건을 표시했습니다." if len(rows) > len(preview_rows) else f"\n\n총 {len(rows)}건입니다."
    return f"### {title}\n" + _markdown_table(preview_rows, columns) + note


# 함수 설명: `_notices_section()`는 응답 section을 최종 Message에 넣을 독립 Markdown section으로 렌더링합니다.
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


# 함수 설명: `_next_steps_section()`는 steps·응답 section을 최종 Message에 넣을 독립 Markdown section으로 렌더링합니다.
def _next_steps_section(value: Any) -> str:
    steps = [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []
    if not steps:
        return ""
    return "### 다음 단계\n" + "\n".join(f"- {step}" for step in steps[:5])


# 함수 설명: `_markdown_table()`는 컬럼과 행을 길이 제한·escape 규칙이 적용된 Markdown 표로 렌더링합니다.
def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    header = "| " + " | ".join(_escape(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_escape(row.get(column, "")) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider] + body)


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_row_list()`는 여러 입력 형태에서 dict인 행만 골라 표준 행 목록으로 반환합니다.
def _row_list(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_columns_from_rows()`는 행 목록의 key 등장 순서를 유지하면서 결과 테이블의 컬럼 목록을 계산합니다.
def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    return columns


# 함수 설명: `_escape()`는 Markdown 표 셀을 깨뜨리는 구분자와 줄바꿈 문자를 안전하게 escape합니다.
def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", "<br>").replace("|", "\\|")
    return text[: CELL_LIMIT - 3] + "..." if len(text) > CELL_LIMIT else text


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TableCatalogSavingMessageAdapter(Component):
    display_name = "09 테이블 카탈로그 등록 메시지 어댑터"
    description = "테이블 카탈로그 등록 결과 페이로드를 Playground 채팅용 서비스형 Markdown 메시지로 변환합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="message", display_name="메시지", method="build_output_message", types=["Message"])]

    # Langflow 출력 함수: '메시지 (message)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_message(self) -> Message:
        return Message(text=build_message(getattr(self, "payload", None)))
