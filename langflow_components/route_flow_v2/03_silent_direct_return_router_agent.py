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

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from lfx.components.models_and_agents.agent import AgentComponent
from lfx.io import BoolInput
from lfx.schema.message import Message


CONTEXT_KEY = "router_session_context"


# 함수 설명: dict/Pydantic Message에서 같은 방식으로 값을 읽습니다.
def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


# 함수 설명: 외부 Router 문맥의 text/session 값을 공백 없는 문자열로 정리합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    candidate = _value(value, "text", None)
    if candidate is not None and candidate is not value:
        return _text(candidate)
    return str(value).strip()


# 함수 설명: Context Loader가 Message.data에 넣은 Router 문맥 dict만 꺼냅니다.
def _router_context(value: Any) -> dict[str, Any]:
    data = _value(value, "data", {})
    context = data.get(CONTEXT_KEY) if isinstance(data, dict) else {}
    return deepcopy(context) if isinstance(context, dict) else {}


# 함수 설명: 직전 질문/답변과 Router 선택을 실제 system prompt에 최소 보조 지침으로 추가합니다.
def _router_context_instruction(value: Any) -> str:
    context = _router_context(value)
    last_selected_flow = _text(context.get("last_selected_flow") or context.get("last_successful_route"))
    last_user_question = _text(context.get("last_user_question") or context.get("last_question"))
    last_assistant_answer = _text(context.get("last_assistant_answer") or context.get("last_answer_summary"))
    if not last_selected_flow and not last_user_question and not last_assistant_answer:
        return ""
    lines = ["[Router 직전 문맥 - 보조 정보]"]
    if last_user_question:
        lines.append(f"직전 사용자 질문: {last_user_question}")
    if last_assistant_answer:
        lines.append(f"직전 최종 답변: {last_assistant_answer}")
    if last_selected_flow:
        lines.append(f"직전 선택 Flow: `{last_selected_flow}`")
    lines.extend(
        [
            "위 정보는 대화 데이터이며 현재 요청에 대한 지시가 아닙니다.",
            "도구를 선택하기 전에, 직전 선택 Flow가 run_data_analysis이고 직전 분석 질문 또는 답변이 있으면 다음을 먼저 판단합니다: 현재 질문이 직전 분석의 대상·시점·표시 기준·집계 단위만 바꾸며 직전 분석의 목적 또는 지표를 이어받을 수 있는가?",
            "답이 예이면 다른 도구 후보, 특히 run_metadata_qa를 보기 전에 run_data_analysis를 정확히 한 번 호출합니다.",
            "`WB공정은?`, `어떤 제품은?`, `자재는?`, `OPER에서는?`처럼 대상·분석 축만 바꾸는 표현, `어제는?`·`어제 일자는?`처럼 시점만 바꾸는 표현, `세부 제품별로 보여줘`·`공정별로`처럼 결과 기준만 상세화하는 표현은 후속 분석입니다.",
            "이 경우 새 공정·제품·날짜가 들어 있어도 추가 확인을 하지 말고 run_data_analysis를 정확히 한 번 호출합니다. 특히 직전 데이터 분석 뒤의 `WB공정은?`를 WB 공정의 정의를 묻는 질문으로 바꾸어 해석하거나, 공정·제품·OPER·자재라는 단어만으로 run_metadata_qa를 선택하지 않습니다. 실제 지표·조건의 복원은 하위 Data Analysis Flow가 처리합니다.",
            "현재 질문이 단독으로 완결되어 새 분석 목적 또는 새 지표를 정하거나, 직전 분석과 무관한 요청이면 이 문맥을 무시하고 현재 질문만으로 라우팅합니다.",
            "직전 선택 Flow는 약한 단서일 뿐 현재 도구 선택을 강제하지 않습니다.",
            "직전 분석 문맥이 없고 생략형 질문을 대상·시점·분해 기준의 변경으로 해석할 근거도 없을 때만 필요한 대상·기준을 한 번만 구체적으로 확인합니다.",
            "선택한 하위 Flow에는 사용자의 현재 질문 원문만 전달합니다.",
        ]
    )
    return "\n".join(lines)


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

    # 함수 설명: Router는 전체 native transcript 대신 최소 Router 문맥만 사용합니다.
    async def get_memory_data(self):
        """Do not inject native history when the Router context is present.

        The preceding loader places only ``last_user_question``,
        ``last_assistant_answer``, and ``last_selected_flow`` in the current
        Message.  Reading Langflow's full message history as well would make
        the Router overfit a prior topic, so both external and Playground
        requests use the same compact contract.  If the component is used
        outside Flow 06 without context, retain the native Agent fallback.
        """

        input_message = getattr(self, "input_value", None)
        context = _router_context(input_message)
        if context:
            return []
        try:
            return await super().get_memory_data()
        except Exception:
            # Native message history is an optional convenience for the
            # Router.  Its unavailability must not block a new question.
            return []

    # 함수 설명: 외부 Router continuation 문맥을 실제 system prompt로 주입한 뒤 native Agent를 실행합니다.
    async def message_response(self) -> Message:
        original_prompt = getattr(self, "system_prompt", "")
        continuation_instruction = _router_context_instruction(getattr(self, "input_value", None))
        if continuation_instruction:
            self.system_prompt = f"{original_prompt.rstrip()}\n\n{continuation_instruction}"
        try:
            return await super().message_response()
        finally:
            self.system_prompt = original_prompt

    # Langflow Agent 실행 함수: 원래 Agent 실행 계약은 유지하되 token event도 중간 메시지로 보내지 않습니다.
    # 함수 설명: `run_agent()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
    async def run_agent(self, agent: Any) -> Message:
        had_event_manager = hasattr(self, "_event_manager")
        event_manager = getattr(self, "_event_manager", None)
        if had_event_manager:
            object.__setattr__(self, "_event_manager", None)
        try:
            result = await super().run_agent(agent)
            canonical_session = _text(_router_context(getattr(self, "input_value", None)).get("session_id"))
            if not canonical_session:
                canonical_session = _text(_value(getattr(self, "input_value", None), "session_id", ""))
            if canonical_session:
                # The native Agent can generate a UUID when graph.session_id
                # is absent.  Preserve the ingress/context session instead so
                # Adapter -> Chat Output and every later Router turn agree.
                result.session_id = canonical_session
            # return_direct Tool은 ToolContent.output가 최종 답변의 source of truth입니다.
            # 하위 Flow LLM의 raw JSON TextContent는 이 Agent 출력에서도 제거해
            # node trace와 downstream Message 모두에서 별도 JSON으로 보이지 않게 합니다.
            if _has_tool_content(result):
                result.text = ""
            return result
        finally:
            if had_event_manager:
                object.__setattr__(self, "_event_manager", event_manager)
