# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 무중간이벤트 직접 반환 Router Agent
# 역할: return_direct Tool 실행 중 하위 Flow의 내부 LLM 이벤트가 상위 Playground 메시지로 노출되는 것을 차단합니다.
# 주요 입력: Agent 기본 입력(모델, 지시문, 입력 메시지, tools)
# 주요 출력: Response(response), Structured Response(structured_response)
# 처리 흐름: 기본 Agent의 Tool 실행과 최종 Message 생성은 그대로 사용하되, 중간 send_message와 token event만 비활성화합니다.
# 유지보수 포인트: 이 컴포넌트는 Router Flow에서만 사용하고, 최종 응답은 뒤의 직접 반환 결과 어댑터와 Chat Output이 저장합니다.
# =============================================================================
"""A Router-only Agent that keeps direct Tool results out of nested event noise.

Langflow 1.11 forwards child Flow model events into an Agent's event stream.
The native Agent persists those events as intermediate Playground messages,
which can expose a child Flow's raw intent JSON even when the Tool itself
returns the correct final response.  This component preserves native Agent
planning/tool behavior while making its intermediate event callbacks no-ops.
The downstream direct-result adapter and Chat Output remain the sole visible
message path.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.components.models_and_agents.agent import AgentComponent
from lfx.io import BoolInput
from lfx.schema.message import Message


# 주요 함수: Agent가 Tool을 호출한 뒤 남긴 raw LLM TextContent가 있는지 확인합니다.
def _has_tool_content(message: Message) -> bool:
    for content in getattr(message, "content_blocks", []) or []:
        content_type = str(getattr(content, "type", "") or "").casefold()
        if content_type in {"tool", "tool_use", "tool_call"}:
            return True
        if getattr(content, "output", None) is not None and getattr(content, "name", None):
            return True
    return False


# 주요 함수: Langflow 1.11 Agent 입력 계약을 Router 기본값으로 구성합니다.
def _router_agent_inputs() -> list[Any]:
    """Keep the Router template compatible with Agent input additions in Langflow 1.11."""

    inputs = deepcopy(list(getattr(AgentComponent, "inputs", []) or []))
    calculator_input = next(
        (item for item in inputs if str(getattr(item, "name", "") or "") == "add_calculator_tool"),
        None,
    )
    if calculator_input is not None:
        # The built-in 1.11 default is true, but this specialized Router
        # exposes only its connected Flow Tools by default.
        calculator_input.value = False
        return inputs

    # The 1.11 Agent validates this key whenever the model field is updated.
    # Keep the explicit Router-safe default if a customized Agent schema omits it.
    inputs.append(
        BoolInput(
            name="add_calculator_tool",
            display_name="Calculator",
            info="Router Flow에서는 별도 계산기 Tool을 추가하지 않습니다.",
            value=False,
            advanced=True,
        )
    )
    return inputs


# Langflow 컴포넌트 클래스: 기본 Agent의 Tool 호출은 유지하고 Playground 중간 이벤트만 억제합니다.
class SilentDirectReturnRouterAgent(AgentComponent):
    display_name = "03 무중간이벤트 직접 반환 Router Agent"
    description = "하위 Flow 내부 이벤트를 표시하지 않고 return_direct Tool의 최종 결과만 다음 Chat Output으로 전달합니다."
    icon = "Bot"
    name = "SilentDirectReturnRouterAgent"
    inputs = _router_agent_inputs()

    # Langflow 메시지 전송 함수: Agent의 부분 응답과 중간 Tool 이벤트를 DB·Playground에 기록하지 않습니다.
    # 함수 설명: `send_message()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
    async def send_message(
        self,
        message: Message,
        id_: str | None = None,
        *,
        skip_db_update: bool = False,
    ) -> Message:
        del id_, skip_db_update
        return message

    # Langflow Agent 실행 함수: 원래 Agent 실행 계약은 유지하되 token event도 중간 메시지로 보내지 않습니다.
    # 함수 설명: `run_agent()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
    async def run_agent(self, agent: Any) -> Message:
        had_event_manager = hasattr(self, "_event_manager")
        event_manager = getattr(self, "_event_manager", None)
        if had_event_manager:
            object.__setattr__(self, "_event_manager", None)
        try:
            result = await super().run_agent(agent)
            # return_direct Tool은 ToolContent.output가 최종 답변의 source of truth입니다.
            # 하위 Flow LLM의 raw JSON TextContent는 이 Agent 출력에서도 제거해
            # node trace와 downstream Message 모두에서 별도 JSON으로 보이지 않게 합니다.
            if _has_tool_content(result):
                result.text = ""
            return result
        finally:
            if had_event_manager:
                object.__setattr__(self, "_event_manager", event_manager)
