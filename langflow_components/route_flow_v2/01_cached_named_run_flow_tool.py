from __future__ import annotations

import re
from typing import Any

from lfx.base.tools.run_flow import RunFlowBaseComponent
from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, MessageTextInput, MultilineInput, Output, StrInput


def _as_iso_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class CachedNamedRunFlowTool(RunFlowBaseComponent):
    display_name = "01 이름 기반 Cached Run Flow 도구"
    description = "같은 프로젝트의 Flow를 이름으로 찾아 실제 ID 기반 그래프 캐시와 함께 Agent 도구로 제공합니다."
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

    async def get_graph(
        self,
        flow_name_selected: str | None = None,
        flow_id_selected: str | None = None,
        updated_at: str | None = None,
    ):
        del flow_id_selected, updated_at
        flow_name = str(flow_name_selected or getattr(self, "flow_name_selected", "") or "").strip()
        if not flow_name:
            raise ValueError("대상 Flow 이름이 필요합니다.")

        flow = await super().get_flow(flow_name_selected=flow_name, flow_id_selected=None)
        flow_data = getattr(flow, "data", None) or {}
        actual_id = str(flow_data.get("id") or "").strip()
        actual_updated_at = _as_iso_text(flow_data.get("updated_at"))
        if not actual_id:
            raise ValueError(f"대상 Flow를 찾지 못했거나 ID가 없습니다: {flow_name}")

        self.flow_name_selected = flow_name
        self.flow_id_selected = actual_id
        self._attributes["flow_name_selected"] = flow_name
        self._attributes["flow_id_selected"] = actual_id
        self._attributes["flow_name_selected_updated_at"] = actual_updated_at
        self._cached_flow_updated_at = actual_updated_at
        return await super().get_graph(
            flow_name_selected=flow_name,
            flow_id_selected=actual_id,
            updated_at=actual_updated_at,
        )

    def get_new_fields(self, inputs_vertex):
        chat_input_ids = {
            vertex.id
            for vertex in inputs_vertex
            if vertex.data.get("type") == "ChatInput" or vertex.display_name == "Chat Input"
        }
        fields = super().get_new_fields(inputs_vertex)
        allowed_names = {f"{node_id}{self.IOPUT_SEP}input_value" for node_id in chat_input_ids}
        compact_fields = [field for field in fields if field.get("name") in allowed_names]
        if len(compact_fields) != 1:
            raise ValueError("대상 Flow에는 사용자 입력용 Chat Input이 정확히 하나 있어야 합니다.")
        return compact_fields

    async def get_required_data(self):
        graph = await self.get_graph(self.flow_name_selected, None, None)
        self._sync_flow_outputs(self._format_flow_outputs(graph))
        fields = self.update_input_types(self.get_new_fields_from_graph(graph))
        description = graph.description or str(getattr(self, "tool_description", "") or "")
        return description, [field for field in fields if field.get("tool_mode", False)]

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

    def _pre_run_setup(self) -> None:
        super()._pre_run_setup()
        explicit = str(getattr(self, "session_id", "") or "").strip()
        parent_session = str(getattr(getattr(self, "graph", None), "session_id", "") or "").strip()
        inherited = explicit or parent_session
        if inherited:
            self.session_id = inherited
            self._attributes["session_id"] = inherited
