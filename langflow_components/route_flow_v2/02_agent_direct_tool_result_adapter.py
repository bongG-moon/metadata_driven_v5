# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 Agent 직접 반환 결과 어댑터
# 역할: Langflow 1.11 Agent가 하위 Flow 내부 LLM 이벤트를 최종 메시지에 섞는 현상을 차단합니다.
# 주요 입력: Agent 응답(agent_message), 도구 최종 답변 우선(prefer_tool_result)
# 주요 출력: 정리된 메시지(message)
# 처리 흐름: 성공한 ToolContent.output의 content만 골라 새 Message로 만들고, 원본 Agent content_blocks는 전달하지 않습니다.
# 유지보수 포인트: Tool 응답의 content/message/output/data 중 표시 가능한 텍스트만 허용하며 임의 JSON 문자열화는 하지 않습니다.
# =============================================================================
"""Normalize a direct-return Agent result before it reaches Chat Output.

Langflow 1.11 Agent graphs forward nested child-flow model events through the
parent Agent event stream.  When a Run Flow tool uses ``return_direct=True``,
the Agent's own ``text`` can therefore contain an internal child LLM payload
instead of the tool's final answer.  The successful tool result is still
available in ``Message.content_blocks`` as ``ToolContent.output``.

This standalone component deliberately creates a new Message containing only
that final tool text.  It is placed between Agent and Chat Output so the
intermediate Agent message is not persisted or rendered by the Playground.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, HandleInput, Output
from lfx.schema.message import Message


_MISSING = object()
_FAILED_TOOL_STATUSES = {"error", "failed", "failure", "cancelled", "canceled"}


# 주요 함수: Langflow/Pydantic 객체와 JSON dict에서 같은 방식으로 필드 값을 읽습니다.
def _value(item: Any, key: str, default: Any = None) -> Any:
    """Read a field from Langflow/Pydantic objects and JSON-compatible dicts."""

    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


# 주요 함수: Pydantic content 객체를 안전하게 mapping 형태로 정규화합니다.
def _as_mapping(item: Any) -> Mapping[str, Any] | None:
    if isinstance(item, Mapping):
        return item
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
        except Exception:  # pragma: no cover - defensive against third-party content objects
            return None
        return dumped if isinstance(dumped, Mapping) else None
    return None


# 주요 함수: 중첩된 rich content를 표시 순서대로 순회합니다.
def _walk_content(items: Any):
    """Yield content blocks in display order, including nested rich content."""

    if isinstance(items, (list, tuple)):
        for item in items:
            yield from _walk_content(item)
        return
    if items is None:
        return
    yield items
    children = _value(items, "contents", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from _walk_content(child)


# 주요 함수: ToolContent 또는 호환 가능한 직렬화 Tool 블록인지 판별합니다.
def _is_tool_content(item: Any) -> bool:
    item_type = str(_value(item, "type", "") or "").casefold()
    if item_type in {"tool", "tool_use", "tool_call"}:
        return True
    output = _value(item, "output", _MISSING)
    return output is not _MISSING and bool(_value(item, "name", "") or _value(item, "tool_call_id", ""))


# 주요 함수: 임의 JSON을 노출하지 않고 사용자에게 표시할 수 있는 텍스트만 추출합니다.
def _text_from_value(value: Any, *, depth: int = 0) -> str:
    """Extract a human-visible answer without stringifying arbitrary JSON."""

    if value is None or depth > 6:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return ""

    # A child Run Flow may return a Message directly instead of a tool dict.
    text = _value(value, "text", _MISSING)
    if text is not _MISSING and text is not value:
        extracted = _text_from_value(text, depth=depth + 1)
        if extracted:
            return extracted

    mapping = _as_mapping(value)
    if mapping is not None:
        for key in ("content", "message", "output", "data"):
            candidate = mapping.get(key, _MISSING)
            if candidate is _MISSING or candidate is value:
                continue
            extracted = _text_from_value(candidate, depth=depth + 1)
            if extracted:
                return extracted
        return ""

    if isinstance(value, (list, tuple)):
        parts = [_text_from_value(item, depth=depth + 1) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


# 주요 함수: 마지막 성공 Tool 결과와 오류 상태를 찾아 직접 반환 후보를 만듭니다.
def _last_tool_result(content_blocks: Any) -> tuple[str, bool, str]:
    """Return (successful_text, saw_tool, last_clean_error_text)."""

    successful_text = ""
    saw_tool = False
    error_text = ""
    for item in _walk_content(content_blocks):
        if not _is_tool_content(item):
            continue
        saw_tool = True
        error = _value(item, "error", None)
        output = _value(item, "output", None)
        status = str(_value(output, "status", "") or "").casefold()
        if error or status in _FAILED_TOOL_STATUSES:
            error_text = _text_from_value(error) or _text_from_value(output)
            continue
        extracted = _text_from_value(output)
        if extracted:
            successful_text = extracted
    return successful_text, saw_tool, error_text


# 주요 함수: 세션 문맥만 보존한 새 Message를 만들어 원본 Agent 이벤트를 끊습니다.
def _message_with_context(source: Any, text: str) -> Message:
    """Make a clean Message while retaining the session used by Chat Output."""

    kwargs: dict[str, Any] = {"text": text}
    for field_name in ("session_id", "context_id"):
        value = _value(source, field_name, None)
        if value not in (None, ""):
            kwargs[field_name] = value
    return Message(**kwargs)


# 주요 함수: return_direct Tool의 최종 답변을 선택하고 하위 LLM JSON 누출을 방지합니다.
def build_direct_tool_result(agent_message: Any, *, prefer_tool_result: bool = True) -> Message:
    """Select the final tool answer and prevent nested LLM JSON from leaking."""

    tool_text, saw_tool, tool_error = _last_tool_result(_value(agent_message, "content_blocks", None))
    agent_text = _text_from_value(_value(agent_message, "text", ""))

    if prefer_tool_result and tool_text:
        return _message_with_context(agent_message, tool_text)
    if saw_tool and not tool_text:
        fallback = tool_error or "도구 실행은 완료되었지만 표시할 최종 답변을 받지 못했습니다."
        return _message_with_context(agent_message, fallback)
    return _message_with_context(agent_message, agent_text)


# Langflow 컴포넌트 클래스: 입력 Agent Message에서 성공 Tool 답변만 선택해 Chat Output으로 보냅니다.
class AgentDirectToolResultAdapter(Component):
    display_name = "02 Agent 직접 반환 결과 어댑터"
    description = "return_direct 도구의 최종 답변만 선택하여 하위 Flow 내부 이벤트가 Playground 답변에 섞이지 않게 합니다."
    icon = "Route"
    name = "AgentDirectToolResultAdapter"

    inputs = [
        HandleInput(
            name="agent_message",
            display_name="Agent 응답",
            info="Agent의 response Message를 연결합니다.",
            input_types=["Message"],
            required=True,
        ),
        BoolInput(
            name="prefer_tool_result",
            display_name="도구 최종 답변 우선",
            info="도구가 성공한 경우 Agent 내부 텍스트 대신 ToolContent.output의 최종 답변을 반환합니다.",
            value=True,
            advanced=True,
        ),
    ]
    outputs = [
        Output(
            name="message",
            display_name="정리된 메시지",
            method="build_message",
            types=["Message"],
        )
    ]

    # Langflow 출력 함수: 캔버스 message 포트가 요청될 때 정리된 최종 Message를 반환합니다.
    def build_message(self) -> Message:
        return build_direct_tool_result(
            getattr(self, "agent_message", None),
            prefer_tool_result=bool(getattr(self, "prefer_tool_result", True)),
        )
