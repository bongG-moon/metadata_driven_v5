# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 이름 기반 Cached Run Flow 도구
# 역할: 같은 프로젝트의 Flow를 이름으로 찾아 실제 ID 기반 그래프 캐시와 함께 Agent 도구로 제공합니다.
# 주요 입력: 대상 Flow 이름 (flow_name_selected) · 필수, 해석된 Flow ID (flow_id_selected), 세션 ID (session_id), Flow 그래프 캐시
#        (cache_flow), 도구 이름 (tool_name) · 필수, 도구 설명 (tool_description) · 필수, 결과 직접 반환 (return_direct)
# 주요 출력: Flow 도구 (component_as_tool)
# 처리 흐름: Flow 이름을 현재 ID로 해석해 그래프만 캐시하는 Agent 도구이며, 부모 세션과 단일 Chat Input/Output 계약을 유지합니다.
# 유지보수 포인트: cache_flow는 그래프 빌드만 재사용하고 답변은 캐시하지 않습니다. return_direct와 부모 세션 상속 계약을 유지합니다.
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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
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

    # 주요 메서드: 대상 Flow 이름을 ID로 해석하고 재사용 가능한 그래프를 가져옵니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
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

    # 주요 메서드: 대상 Flow의 실제 Chat Input만 Agent tool schema로 노출합니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
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

    # 주요 메서드: Flow tool 실행에 필요한 그래프와 입력 정보를 준비합니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
    async def get_required_data(self):
        graph = await self.get_graph(self.flow_name_selected, None, None)
        self._sync_flow_outputs(self._format_flow_outputs(graph))
        fields = self.update_input_types(self.get_new_fields_from_graph(graph))
        description = graph.description or str(getattr(self, "tool_description", "") or "")
        return description, [field for field in fields if field.get("tool_mode", False)]

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
