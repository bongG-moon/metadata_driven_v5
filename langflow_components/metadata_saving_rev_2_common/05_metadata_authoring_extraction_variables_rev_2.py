# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 메타데이터 저장 후보 변수 생성기 rev_2
# 역할: 검증된 정제안을 기존 저장 후보 추출 Prompt에 전달하고 미확정 상태에서는 items 생성을 명시적으로 보류합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


def build_extraction_text(payload_value: Any) -> str:
    payload = _payload(payload_value)
    request = _dict(payload.get("request"))
    refinement = _dict(payload.get("refinement"))
    draft = _dict(payload.get("metadata_authoring_draft"))
    source = str(draft.get("refined_text") or refinement.get("refined_text") or request.get("raw_text") or "").strip()
    missing = _string_list(draft.get("missing_information")) or _string_list(refinement.get("missing_information"))
    unresolved = [item for item in _list(draft.get("unresolved_references")) if isinstance(item, dict)]
    needs_input = bool(draft.get("needs_more_input")) or bool(refinement.get("needs_more_input")) or bool(missing) or bool(unresolved)
    if not needs_input:
        return source
    lines = [
        "[REV_2 저장 보류]",
        "아래 참조가 확정되지 않았으므로 items는 빈 배열로 반환하고 missing_information을 그대로 반환하세요.",
    ]
    lines.extend(f"- {item}" for item in missing)
    for item in unresolved:
        candidates = ", ".join(_string_list(item.get("candidates"))) or "후보 없음"
        lines.append(f"- {item.get('input', '')}: {candidates}")
    if source:
        lines.extend(["", "정제 중인 사용자 설명:", source])
    return "\n".join(lines)


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


class MetadataAuthoringExtractionVariablesRev2(Component):
    display_name = "05 메타데이터 저장 후보 변수 생성기 rev_2"
    description = "검증된 정제안을 기존 저장 JSON 추출 단계에 전달합니다."
    inputs = [DataInput(name="payload", display_name="정제 페이로드", required=True)]
    outputs = [Output(name="source_text", display_name="정제 등록안", method="build_source_text", types=["Message"])]

    def build_source_text(self) -> Message:
        return Message(text=build_extraction_text(getattr(self, "payload", None)))
