# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 09 메타데이터 등록 메시지 어댑터 rev_2
# 역할: 기존 등록 결과와 함께 원문·정제안·참조 변환·재입력 예시를 Playground Markdown으로 표시합니다.
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


def build_message(payload_value: Any) -> str:
    payload = _payload(payload_value)
    sections = []
    answer_sections = _dict(payload.get("answer_sections"))
    summary = _dict(answer_sections.get("summary"))
    headline = str(summary.get("headline") or payload.get("message") or "").strip()
    if headline:
        sections.append("### 등록 결과\n" + headline)

    authoring = _dict(payload.get("metadata_authoring"))
    original = str(authoring.get("original_text") or "").strip()
    refined = str(authoring.get("refined_text") or "").strip()
    if original:
        sections.append("### 사용자 원문\n```text\n" + original + "\n```")
    if refined:
        sections.append("### Flow 정제안\n```text\n" + refined + "\n```")
    resolved = [item for item in _list(authoring.get("resolved_references")) if isinstance(item, dict)]
    if resolved:
        lines = ["### 확정된 계약 변환"]
        for item in resolved[:20]:
            lines.append(f"- `{item.get('input', '')}` → `{item.get('target', '')}` ({item.get('kind', '')})")
        sections.append("\n".join(lines))

    key_points = _string_list(answer_sections.get("key_points"))
    if key_points:
        sections.append("### 한눈에 보기\n" + "\n".join(f"- {item}" for item in key_points))
    table = _dict(answer_sections.get("target_table"))
    table_section = _table_section(table, _dict(payload.get("data")))
    if table_section:
        sections.append(table_section)
    notices = [item for item in _list(answer_sections.get("notices")) if isinstance(item, dict)]
    if notices:
        lines = ["### 확인할 점"]
        for item in notices[:12]:
            message = str(item.get("message") or "").strip()
            if message:
                lines.append(f"- {item.get('title') or item.get('type') or '안내'}: {message}")
        if len(lines) > 1:
            sections.append("\n".join(lines))
    retry_example = str(authoring.get("retry_example") or "").strip()
    retry_examples = _string_list(authoring.get("retry_examples"))
    if retry_example and not retry_examples:
        retry_examples = [retry_example]
    if len(retry_examples) == 1:
        sections.append("### 이렇게 다시 입력해 보세요\n```text\n" + retry_examples[0] + "\n```")
    elif retry_examples:
        lines = ["### 이렇게 다시 입력해 보세요", "실제 계약과 맞는 예시 하나를 통째로 복사해 다시 실행하세요."]
        for index, example in enumerate(retry_examples, start=1):
            lines.extend([f"#### 선택안 {index}", "```text", example, "```"])
        sections.append("\n\n".join(lines))
    next_steps = _string_list(answer_sections.get("next_steps"))
    if next_steps:
        sections.append("### 다음 단계\n" + "\n".join(f"- {item}" for item in next_steps[:6]))
    return "\n\n".join(sections) if sections else json.dumps(payload, ensure_ascii=False, default=str)


def _table_section(table: dict[str, Any], data: dict[str, Any]) -> str:
    rows = _row_list(table.get("rows")) or (_row_list(data.get("rows")) if table.get("row_source") == "data.rows" else [])
    if not rows:
        return ""
    columns = _string_list(table.get("columns")) or _columns_from_rows(rows)
    preview = rows[: int(table.get("display_limit") or TABLE_LIMIT)]
    title = str(table.get("title") or "등록 대상").strip()
    note = f"\n\n총 {len(rows)}건 중 {len(preview)}건을 표시했습니다." if len(rows) > len(preview) else f"\n\n총 {len(rows)}건입니다."
    return f"### {title}\n" + _markdown_table(preview, columns) + note


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    header = "| " + " | ".join(_escape(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_escape(row.get(column, "")) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", "<br>").replace("|", "\\|")
    return text[: CELL_LIMIT - 3] + "..." if len(text) > CELL_LIMIT else text


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    result = []
    for row in rows:
        for key in row:
            if key not in result:
                result.append(str(key))
    return result


def _row_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class MetadataAuthoringMessageAdapterRev2(Component):
    display_name = "09 메타데이터 등록 메시지 어댑터 rev_2"
    description = "원문과 Flow 정제안, 확정 변환, 재입력 예시를 포함한 최종 Markdown Message를 만듭니다."
    inputs = [DataInput(name="payload", display_name="rev_2 응답", required=True)]
    outputs = [Output(name="message", display_name="메시지", method="build_output_message", types=["Message"])]

    def build_output_message(self) -> Message:
        return Message(text=build_message(getattr(self, "payload", None)))
