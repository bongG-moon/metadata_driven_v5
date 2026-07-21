# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 이름 기반 Cached Run Flow 도구
# 역할: 같은 프로젝트의 Flow를 이름으로 찾아 고정 question 입력과 실제 ID 기반 그래프 캐시를 사용하는 Agent 도구로 제공합니다.
# 주요 입력: 런타임 사용자 질문 (question) · 필수, 대상 Flow 이름 (flow_name_selected) · 필수, 해석된 Flow ID (flow_id_selected),
#        세션 ID (session_id), Flow 그래프 캐시 (cache_flow), 도구 이름 (tool_name) · 필수, 도구 설명 (tool_description) · 필수,
#        결과 직접 반환 (return_direct)
# 주요 출력: Flow 도구 (component_as_tool)
# 처리 흐름: Langflow 실행 사용자로 이름을 실제 ID에 해석한 뒤, 선택된 Tool만 runtime Chat I/O ID를 찾아 실행합니다.
# 유지보수 포인트: 실제 ID는 캐시에 재사용하되 export에는 고정하지 않으며, 부모 Router만 질문·답변 메시지를 저장합니다.
# =============================================================================

from __future__ import annotations

import re
from typing import Any

from lfx.base.tools.run_flow import RunFlowBaseComponent
from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, MessageTextInput, MultilineInput, Output, StrInput


# 함수 설명: `_as_iso_text()`는 datetime 등 시간 값을 캐시 갱신 비교에 사용할 ISO 문자열로 변환합니다.
def _as_iso_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# 함수 설명: 현재 하위 Flow에서 사용자 입력을 받을 Chat Input ID를 정확히 하나만 확정합니다.
# Flow import로 node ID가 바뀌어도 실행 시점 그래프를 기준으로 다시 찾습니다.
def _single_chat_input_id(vertices: Any) -> str:
    chat_input_ids = [
        str(vertex.id)
        for vertex in list(vertices or [])
        if (getattr(vertex, "data", {}) or {}).get("type") == "ChatInput"
        or getattr(vertex, "display_name", "") == "Chat Input"
    ]
    if len(chat_input_ids) != 1:
        raise ValueError("대상 Flow에는 사용자 입력용 Chat Input이 정확히 하나 있어야 합니다.")
    return chat_input_ids[0]


# 함수 설명: 현재 하위 Flow에서 답변을 저장하는 Chat Output ID를 정확히 하나만 확정합니다.
# import 과정에서 node ID가 바뀌므로 고정 문자열 대신 선택된 runtime graph에서 찾습니다.
def _single_chat_output_id(vertices: Any) -> str:
    chat_output_ids = [
        str(vertex.id)
        for vertex in list(vertices or [])
        if (getattr(vertex, "data", {}) or {}).get("type") == "ChatOutput"
        or getattr(vertex, "display_name", "") == "Chat Output"
    ]
    if len(chat_output_ids) != 1:
        raise ValueError("대상 Flow에는 답변용 Chat Output이 정확히 하나 있어야 합니다.")
    return chat_output_ids[0]


# 함수 설명: Agent가 고정 question 필드로 전달한 값을 현재 Chat Input용 Run Flow tweak로 변환합니다.
# `-`와 `~`가 포함된 내부 node key는 LLM Tool schema 밖에서만 생성해 provider의 필드명 정규화를 피합니다.
def _question_tweaks(
    chat_input_id: Any,
    flow_tweak_data: Any,
    chat_output_id: Any = "",
) -> dict[str, dict[str, Any]]:
    node_id = str(chat_input_id or "").strip()
    if not node_id:
        raise ValueError("현재 하위 Flow의 Chat Input ID를 확인할 수 없습니다.")

    tool_values = flow_tweak_data.model_dump() if hasattr(flow_tweak_data, "model_dump") else flow_tweak_data
    if not isinstance(tool_values, dict):
        tool_values = {}
    question = str(tool_values.get("question") or "").strip()
    if not question:
        raise ValueError("하위 Flow에 전달할 사용자 질문이 비어 있습니다.")
    tweaks: dict[str, dict[str, Any]] = {
        node_id: {
            "input_value": question,
            "should_store_message": False,
        }
    }
    output_id = str(chat_output_id or "").strip()
    if output_id:
        tweaks[output_id] = {"should_store_message": False}
    return tweaks


# 함수 설명: `_question_tool_field()`는 graph를 열지 않고 Router Agent에 노출할 고정 question schema를 반환합니다.
def _question_tool_field() -> dict[str, Any]:
    """Return the fixed public schema exposed to the routing agent."""
    return {
        "name": "question",
        "display_name": "사용자 질문",
        "info": "현재 사용자 질문 원문입니다.",
        "required": True,
        "value": "",
        "tool_mode": True,
        "type": str,
        "input_types": [],
        "is_list": False,
    }


# 함수 설명: 현재 하위 Flow의 단일 Chat Output.message를 Agent가 반환할 실행 대상으로 확정합니다.
# API용 구조화 terminal이 함께 있어도 화면 답변 계약은 Chat Output 하나만 사용합니다.
def _chat_output_target(graph: Any, chat_output_id: Any) -> tuple[str, str]:
    """Return the terminal message output of the resolved Chat Output vertex."""
    target_id = str(chat_output_id or "").strip()
    if not target_id:
        raise ValueError("현재 하위 Flow의 Chat Output ID를 확인할 수 없습니다.")

    successor_map = getattr(graph, "successor_map", {}) or {}
    vertices = [
        vertex
        for vertex in list(getattr(graph, "vertices", []) or [])
        if str(getattr(vertex, "id", "") or "") == target_id
    ]
    if len(vertices) != 1:
        raise ValueError("현재 하위 Flow에서 단일 Chat Output vertex를 확인할 수 없습니다.")

    vertex = vertices[0]
    successors = successor_map.get(getattr(vertex, "id", None), successor_map.get(target_id, []))
    if successors:
        raise ValueError("하위 Flow의 Chat Output은 후속 연결이 없는 최종 노드여야 합니다.")

    message_outputs: list[str] = []
    for output in list(getattr(vertex, "outputs", []) or []):
        output_name = output.get("name") if isinstance(output, dict) else getattr(output, "name", None)
        if str(output_name or "").strip() == "message":
            message_outputs.append("message")
    if len(message_outputs) != 1:
        raise ValueError("하위 Flow의 Chat Output에는 message 출력이 정확히 하나 있어야 합니다.")
    return target_id, "message"


# 함수 설명: 선택한 Chat Output만 현재 child 실행의 공식 출력으로 활성화합니다.
# 별도의 API terminal이 있는 Flow도 Route V2에서는 화면 Message 하나만 실행·반환합니다.
def _promote_graph_output(graph: Any, target: tuple[str, str]) -> None:
    vertex_id, output_name = target
    if output_name != "message":
        raise ValueError("Route V2의 최종 출력은 Chat Output.message여야 합니다.")

    vertices = list(getattr(graph, "vertices", []) or [])
    selected = [vertex for vertex in vertices if str(getattr(vertex, "id", "") or "") == str(vertex_id)]
    if len(selected) != 1:
        raise ValueError("선택한 Chat Output이 현재 child graph와 일치하지 않습니다.")

    try:
        for vertex in vertices:
            vertex.is_output = vertex is selected[0]
    except Exception as exc:  # noqa: BLE001
        raise ValueError("선택한 Chat Output을 Langflow 단일 실행 출력으로 등록하지 못했습니다.") from exc

    active_outputs = [vertex for vertex in vertices if bool(getattr(vertex, "is_output", False))]
    if active_outputs != selected:
        raise ValueError("선택하지 않은 child Flow 출력의 비활성화가 반영되지 않았습니다.")


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class CachedNamedRunFlowTool(RunFlowBaseComponent):
    display_name = "01 이름 기반 Cached Run Flow 도구"
    description = "고정 question 도구를 먼저 제공하고 선택된 Flow만 이름으로 찾아 캐시된 그래프로 실행합니다."
    name = "CachedNamedRunFlowTool"
    icon = "Workflow"

    inputs = [
        StrInput(
            name="flow_name_selected",
            display_name="대상 Flow 이름",
            info="Import된 하위 Flow의 정확한 이름입니다. 실행 시 실제 DB ID를 다시 조회합니다.",
            required=True,
        ),
        StrInput(
            name="flow_id_selected",
            display_name="해석된 Flow ID",
            info="실행 시 이름으로 해석되며 export에 고정하지 않습니다.",
            value="",
            show=False,
            override_skip=True,
        ),
        MessageTextInput(
            name="session_id",
            display_name="세션 ID",
            info="직접 지정할 때만 사용합니다. 비우면 Langflow 부모 Flow 실행 세션을 자동 상속합니다.",
            value="",
            advanced=True,
        ),
        BoolInput(
            name="cache_flow",
            display_name="Flow 그래프 캐시",
            info="하위 Flow 그래프 구성을 실제 Flow ID 기준으로 캐시합니다. 데이터와 답변은 캐시하지 않습니다.",
            value=True,
            advanced=True,
        ),
        StrInput(
            name="tool_name",
            display_name="도구 이름",
            info="Agent가 호출할 영문 도구 이름입니다.",
            required=True,
        ),
        MultilineInput(
            name="tool_description",
            display_name="도구 설명",
            info="Agent가 정확히 하나의 하위 Flow를 선택할 수 있도록 사용 범위를 설명합니다.",
            required=True,
        ),
        BoolInput(
            name="return_direct",
            display_name="결과 직접 반환",
            info="하위 Flow의 최종 답변을 추가 LLM 재작성 없이 그대로 반환합니다.",
            value=True,
            advanced=True,
        ),
    ]

    outputs = [
        Output(
            name="component_as_tool",
            display_name="Flow 도구",
            method="to_toolkit",
            types=["Tool"],
            tool_mode=True,
        )
    ]

    # 주요 메서드: Langflow 실행 사용자로 대상 Flow 이름을 현재 ID에 다시 해석해 재사용 가능한 그래프를 가져옵니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
    async def get_graph(
        self,
        flow_name_selected: str | None = None,
        flow_id_selected: str | None = None,
        updated_at: str | None = None,
    ):
        del flow_id_selected
        flow_name = str(flow_name_selected or getattr(self, "flow_name_selected", "") or "").strip()
        if not flow_name:
            raise ValueError("대상 Flow 이름이 필요합니다.")

        # Component.user_id는 Langflow가 주입한 _user_id를 우선 사용하고, 없으면 부모 graph.user_id를 반환합니다.
        # 읽기 전용 속성이므로 직접 변경하지 않고 이름/ID 조회와 캐시에서 같은 런타임 값을 사용합니다.
        runtime_user_id = str(getattr(self, "user_id", "") or "").strip()
        if not runtime_user_id:
            raise ValueError(
                "Router 실행 사용자 ID가 없어 하위 Flow를 조회할 수 없습니다. "
                "Router와 하위 Flow를 같은 사용자로 import하고 같은 사용자/API key로 실행하세요."
            )
        # Import·복제·재배포 뒤 hidden ID가 이전 Flow를 가리킬 수 있으므로 매 실행마다 정확한 이름을 현재 ID로 해석합니다.
        # 해석된 실제 ID는 아래 graph cache key로만 사용하며 export에는 고정하지 않습니다.
        flow = await super().get_flow(flow_name_selected=flow_name, flow_id_selected=None)
        flow_data = getattr(flow, "data", None) or {}
        actual_id = str(flow_data.get("id") or "").strip()
        actual_updated_at = _as_iso_text(flow_data.get("updated_at")) or _as_iso_text(updated_at)
        if not actual_id:
            raise ValueError(
                "현재 Router 실행 사용자에게서 대상 Flow를 찾지 못했거나 ID가 없습니다. "
                f"flow_name={flow_name!r}, user_id={runtime_user_id!r}. "
                "실제 Flow 이름에 '(1)' 등이 붙지 않았는지와 하위 Flow 소유자가 같은지 확인하세요."
            )

        self.flow_name_selected = flow_name
        self.flow_id_selected = actual_id
        self._attributes["flow_name_selected"] = flow_name
        self._attributes["flow_id_selected"] = actual_id
        self._attributes["flow_name_selected_updated_at"] = actual_updated_at
        self._cached_flow_updated_at = actual_updated_at
        graph = await super().get_graph(
            flow_name_selected=flow_name,
            flow_id_selected=actual_id,
            updated_at=actual_updated_at,
        )
        self._resolved_chat_input_id = _single_chat_input_id(getattr(graph, "vertices", []))
        self._resolved_chat_output_id = _single_chat_output_id(getattr(graph, "vertices", []))
        target = _chat_output_target(graph, self._resolved_chat_output_id)
        _promote_graph_output(graph, target)
        self._resolved_flow_output_target = target
        return graph

    # 주요 메서드: `get_required_data()`는 graph 조회 없이 고정 question schema와 lazy output만 구성합니다.
    # Tool 목록을 빌드할 때는 하위 Flow를 조회하지 않고 고정 question schema만 노출합니다.
    # 실제 하위 Flow 해석과 graph build는 선택된 Tool의 `_run_selected_flow` 호출 시점으로 미룹니다.
    async def get_required_data(self):
        self._sync_flow_outputs(
            [
                Output(
                    name="lazy_flow_result",
                    display_name="하위 Flow 결과",
                    method="_run_selected_flow",
                    types=["Message", "Data", "Text"],
                    tool_mode=True,
                )
            ]
        )
        return str(getattr(self, "tool_description", "") or self.description), [_question_tool_field()]

    # 함수 설명: `_run_selected_flow()`는 Agent가 실제 선택한 Tool에 대해서만 하위 Flow를 해석·빌드·실행합니다.
    async def _run_selected_flow(self):
        """Resolve, validate, build, and run the selected child flow lazily."""
        self._last_run_outputs = None
        await self._get_cached_run_outputs(user_id=self.user_id, output_type="any")
        target = getattr(self, "_resolved_flow_output_target", None)
        if not target:
            raise ValueError("대상 Flow의 최종 출력을 확인할 수 없습니다.")
        vertex_id, output_name = target
        return await self._resolve_flow_output(vertex_id=vertex_id, output_name=output_name)

    # 주요 메서드: 고정 question Tool 인자를 현재 그래프의 Chat Input node tweak로 변환합니다.
    # 기본 Run Flow의 node-ID 기반 외부 인자명을 사용하지 않아 모델/provider별 특수문자 변형을 차단합니다.
    def _build_flow_tweak_data(self) -> dict[str, dict[str, str]]:
        return _question_tweaks(
            getattr(self, "_resolved_chat_input_id", ""),
            self._attributes.get("flow_tweak_data"),
            getattr(self, "_resolved_chat_output_id", ""),
        )

    # 함수 설명: `_get_tools()`는 입력 또는 외부 저장소에서 tools을 읽고 호출자가 사용할 형태로 반환합니다.
    async def _get_tools(self):
        tools = await super()._get_tools()
        if len(tools) != 1:
            raise ValueError("대상 Flow에는 Agent 도구로 사용할 최종 출력이 정확히 하나 있어야 합니다.")

        tool = tools[0]
        tool_name = re.sub(r"[^a-zA-Z0-9_-]", "-", str(self.tool_name or "")).strip("-")
        if not tool_name:
            raise ValueError("도구 이름은 영문, 숫자, 밑줄 또는 하이픈을 포함해야 합니다.")
        tool.name = tool_name
        tool.description = str(self.tool_description or "").strip()
        tool.tags = [tool_name]
        tool.return_direct = bool(self.return_direct)
        self.status = f"{tool.name}: {tool.description}"
        return [tool]

    # 함수 설명: `_pre_run_setup()`는 명시 session_id가 없으면 부모 graph 세션을 상속하고 Flow tool 실행 전 상태를 준비합니다.
    def _pre_run_setup(self) -> None:
        super()._pre_run_setup()
        explicit = str(getattr(self, "session_id", "") or "").strip()
        parent_session = str(getattr(getattr(self, "graph", None), "session_id", "") or "").strip()
        inherited = explicit or parent_session
        if inherited:
            self.session_id = inherited
            self._attributes["session_id"] = inherited
