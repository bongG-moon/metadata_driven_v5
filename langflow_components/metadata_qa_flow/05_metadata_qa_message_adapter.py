# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 메타데이터 QA 메시지 어댑터
# 역할: 메타데이터 QA 페이로드를 Playground 채팅용 한국어 markdown 메시지로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 메시지 (message)
# 처리 흐름: QA 결과를 답변·표·SQL·관련 메타데이터·경고 순서의 Markdown Message 하나로 렌더링합니다.
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
CELL_LIMIT = 160


# 주요 함수: 구조화 결과를 사용자가 읽을 수 있는 단일 Markdown Message로 변환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_message(payload_value: Any) -> str:
    payload = _payload(payload_value)
    if not payload:
        return ""
    answer_sections = payload.get("answer_sections") if isinstance(payload.get("answer_sections"), dict) else {}
    if answer_sections:
        sections = _message_from_answer_sections(payload, answer_sections)
        if sections:
            return "\n\n".join(sections)

    sections = []
    answer = str(payload.get("answer_message") or payload.get("message") or "").strip()
    if answer:
        sections.append("### 답변\n" + answer)

    sql_section = _sql_section(_dict(payload.get("metadata_qa")).get("sql_blocks"))
    if sql_section:
        sections.append(sql_section)

    table_section = _table_section(_dict(payload.get("data")))
    if table_section:
        sections.append(table_section)

    refs_section = _refs_section(_dict(payload.get("metadata_qa")).get("source_refs"))
    if refs_section:
        sections.append(refs_section)

    warning_section = _warning_section(_dict(payload.get("trace")))
    if warning_section:
        sections.append(warning_section)
    return "\n\n".join(sections) if sections else json.dumps(payload, ensure_ascii=False, default=str)


def _message_from_answer_sections(payload: dict[str, Any], answer_sections: dict[str, Any]) -> list[str]:
    sections: list[str] = []
    summary = _dict(answer_sections.get("summary"))
    answer = str(summary.get("headline") or payload.get("answer_message") or payload.get("message") or "").strip()
    if answer:
        sections.append("### 답변\n" + answer)

    key_points_section = _key_points_section(answer_sections.get("key_points"))
    if key_points_section:
        sections.append(key_points_section)

    detail_section = _detail_table_section(_dict(answer_sections.get("detail_table")), _dict(payload.get("data")))
    if detail_section:
        sections.append(detail_section)

    sql_section = _sql_section(answer_sections.get("sql_blocks"))
    if sql_section:
        sections.append(sql_section)

    examples_section = _usage_examples_section(answer_sections.get("usage_examples"))
    if examples_section:
        sections.append(examples_section)

    route_hint_section = _route_hint_section(_dict(answer_sections.get("route_hint")))
    if route_hint_section:
        sections.append(route_hint_section)

    if bool(answer_sections.get("show_related_items")):
        related_section = _refs_section(answer_sections.get("related_items") or _dict(payload.get("metadata_qa")).get("source_refs"))
        if related_section:
            sections.append(related_section)

    warning_section = _section_warnings(answer_sections.get("warnings")) or _warning_section(_dict(payload.get("trace")))
    if warning_section:
        sections.append(warning_section)
    return sections


def _key_points_section(value: Any) -> str:
    points = [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []
    if not points:
        return ""
    lines = ["### 한눈에 보기"]
    lines.extend(f"- {point}" for point in points[:6])
    return "\n".join(lines)


def _detail_table_section(detail_table: dict[str, Any], data: dict[str, Any]) -> str:
    rows = _row_list(detail_table.get("rows")) or (_row_list(data.get("rows")) if detail_table.get("row_source") == "data.rows" else [])
    columns = _string_list(detail_table.get("columns")) or _columns_from_rows(rows)
    if not rows:
        return ""
    title = str(detail_table.get("title") or "관련 메타데이터").strip()
    display_limit = _int(detail_table.get("display_limit"), TABLE_LIMIT)
    preview_rows = rows[:display_limit]
    row_count = int(detail_table.get("row_count") or len(rows))
    note = f"\n\n총 {row_count}건 중 {len(preview_rows)}건을 표시했습니다." if row_count > len(preview_rows) else f"\n\n총 {row_count}건입니다."
    return f"### {title}\n" + _markdown_table(preview_rows, columns) + note


def _usage_examples_section(value: Any) -> str:
    examples = [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []
    if not examples:
        return ""
    lines = ["### 다음에 물어볼 수 있는 질문"]
    lines.extend(f"- {example}" for example in examples[:5])
    return "\n".join(lines)


def _route_hint_section(route_hint: dict[str, Any]) -> str:
    if not route_hint:
        return ""
    message = str(route_hint.get("message") or "").strip()
    target_route = str(route_hint.get("target_route") or "").strip()
    lines = ["### 권장 실행 경로"]
    if target_route:
        lines.append(f"- 대상 route: `{target_route}`")
    if message:
        lines.append(f"- 안내: {message}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _section_warnings(value: Any) -> str:
    warnings = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    if not warnings:
        return ""
    lines = ["### 참고"]
    for item in warnings[:8]:
        message = str(item.get("message") or item.get("type") or "").strip()
        if message:
            lines.append(f"- {message}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _sql_section(sql_blocks_value: Any) -> str:
    blocks = [block for block in sql_blocks_value if isinstance(block, dict)] if isinstance(sql_blocks_value, list) else []
    if not blocks:
        return ""
    lines = ["### 등록된 Query Template"]
    for block in blocks[:3]:
        label = str(block.get("label") or "query_template").strip()
        sql = str(block.get("sql") or "").strip()
        if not sql:
            continue
        lines.append(f"#### {label}")
        lines.append("```sql\n" + sql + "\n```")
    return "\n".join(lines)


def _table_section(data: dict[str, Any]) -> str:
    rows = _row_list(data.get("rows"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    if not rows:
        return ""
    preview_rows = rows[:TABLE_LIMIT]
    note = f"\n\n총 {len(rows)}건 중 {len(preview_rows)}건을 표시했습니다." if len(rows) > len(preview_rows) else f"\n\n총 {len(rows)}건입니다."
    return "### 관련 메타데이터\n" + _markdown_table(preview_rows, columns) + note


def _refs_section(value: Any) -> str:
    refs = [ref for ref in value if isinstance(ref, dict)] if isinstance(value, list) else []
    if not refs:
        return ""
    labels = []
    for ref in refs[:10]:
        metadata_type = str(ref.get("metadata_type") or "").strip()
        section = str(ref.get("section") or "").strip()
        key = str(ref.get("key") or "").strip()
        labels.append(":".join(part for part in (metadata_type, section, key) if part))
    return "### 사용한 메타데이터\n" + "\n".join(f"- `{label}`" for label in labels if label)


def _warning_section(trace: dict[str, Any]) -> str:
    warnings = _list(trace.get("warnings"))
    errors = _list(trace.get("errors"))
    if not warnings and not errors:
        return ""
    lines = ["### 경고/오류"]
    for item in warnings[:8]:
        lines.append(f"- 경고: `{_display(item)}`")
    for item in errors[:8]:
        lines.append(f"- 오류: `{_display(item)}`")
    return "\n".join(lines)


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    header = "| " + " | ".join(_escape(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(_escape(row.get(column, "")) for column in columns) + " |")
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
    text = _display(value).replace("\n", "<br>").replace("|", "\\|")
    return text[: CELL_LIMIT - 3] + "..." if len(text) > CELL_LIMIT else text


def _display(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "" if value is None else str(value)


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataQaMessageAdapter(Component):
    display_name = "05 메타데이터 QA 메시지 어댑터"
    description = "메타데이터 QA 페이로드를 Playground 채팅용 한국어 markdown 메시지로 변환합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="message", display_name="메시지", method="build_output_message", types=["Message"])]

    # Langflow 출력 함수: '메시지 (message)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_message(self) -> Message:
        return Message(text=build_message(getattr(self, "payload", None)))
