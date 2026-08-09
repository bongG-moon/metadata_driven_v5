from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from tools import build_v5_auxiliary_flows as base
except ModuleNotFoundError:  # 직접 `python tools/...py` 실행 시 script directory fallback
    import build_v5_auxiliary_flows as base


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components"
EXPORT_PATH = ROOT / "flow_exports" / "09_agent_tool_router_continuation_flow_v5_standalone.json"
FLOW_NAME = "09. v5_agent_tool_router_continuation"
DATA_ANALYSIS_FLOW_NAME = "08. v5_data_analysis_continuation"
ENDPOINT_NAME = "metadata-driven-v5-agent-tool-router-continuation"


def _tool_specs() -> list[base.ToolRouteSpec]:
    specs: list[base.ToolRouteSpec] = []
    for spec in base.TOOL_ROUTE_SPECS:
        if spec.route_name == "data_analysis":
            specs.append(
                base.ToolRouteSpec(
                    route_name=spec.route_name,
                    flow_name=DATA_ANALYSIS_FLOW_NAME,
                    tool_name=spec.tool_name,
                    tool_description=(
                        spec.tool_description
                        + " 단일 조회는 한 번 실행하고, 구조화된 종속 조회 계약이 있는 복합 질문은 "
                        "동일 세션에서 최대 한 번 자동 연계하여 최종 답변만 반환합니다."
                    ),
                    required_all_keywords=spec.required_all_keywords,
                    required_any_phrases=spec.required_any_phrases,
                    keyword_gate_message=spec.keyword_gate_message,
                )
            )
        else:
            specs.append(spec)
    return specs


def build_flow(donor: dict[str, Any]) -> dict[str, Any]:
    proto = base.prototypes(donor)
    flow = base.empty_flow(
        donor,
        FLOW_NAME,
        (
            "Additive Agent Tool Router that leaves Flow 07 unchanged, keeps one Agent Tool call with "
            "max_iterations=1 and return_direct, and lets only the Data Analysis Tool perform a bounded "
            "structured continuation call without parsing answer text or exposing raw rows/trace/code."
        ),
        ENDPOINT_NAME,
        [
            "v5",
            "standalone",
            "agent-router",
            "structured-continuation",
            "max-two-stages",
            "selected-flow-id",
            "optimized",
        ],
    )
    system_prompt = (
        COMPONENT_ROOT / "route_flow_v2_continuation" / "SYSTEM_PROMPT_KO.md"
    ).read_text(encoding="utf-8")
    continuation_tool_path = (
        COMPONENT_ROOT
        / "route_flow_v2_continuation"
        / "01_cached_continuation_run_flow_tool.py"
    )
    regular_tool_path = COMPONENT_ROOT / "route_flow_v2" / "01_cached_named_run_flow_tool.py"

    chat = base.native_node(proto["chat_input"], "ChatInput-agent-tool-router-continuation", 0, 0)
    base._set_message_storage(chat, True)
    agent = base.agent_node(
        proto["agent"],
        "Agent-agent-tool-router-continuation",
        890,
        0,
        system_prompt,
    )
    agent_template = agent["data"]["node"]["template"]
    base._set_value(agent_template, "max_iterations", 1)
    base._set_value(agent_template, "n_messages", 5)
    base._set_value(agent_template, "add_current_date_tool", False)
    base._set_value(agent_template, "handle_parsing_errors", True)
    base._set_value(agent_template, "verbose", False)
    output = base.native_node(
        proto["chat_output"],
        "ChatOutput-agent-tool-router-continuation",
        1310,
        0,
    )
    base._set_message_storage(output, True)
    flow["data"]["nodes"].extend([chat, agent, output])
    base.add_edge(flow, chat, "message", agent, "input_value")

    y_positions = (-650, -390, -130, 130, 390, 650)
    for spec, y in zip(_tool_specs(), y_positions, strict=True):
        is_data_analysis = spec.route_name == "data_analysis"
        tool_path = continuation_tool_path if is_data_analysis else regular_tool_path
        node_id = (
            "ContinuationFlowTool-data_analysis"
            if is_data_analysis
            else f"CachedFlowTool-{spec.route_name}"
        )
        tool = base.custom_node(proto["custom"], node_id, tool_path, 350, y)
        config = tool["data"]["node"]
        config["tool_mode"] = True
        template = config["template"]
        base._set_value(template, "flow_name_selected", spec.flow_name)
        base._set_value(template, "flow_id_selected", "")
        base._set_value(template, "flow_resolution_mode", base.FLOW_ID_PREFERRED if hasattr(base, "FLOW_ID_PREFERRED") else "Flow ID 우선")
        base._set_value(template, "cache_flow", True)
        base._set_value(template, "tool_name", spec.tool_name)
        base._set_value(template, "tool_description", spec.tool_description)
        base._set_value(template, "required_all_keywords", spec.required_all_keywords)
        base._set_value(template, "required_any_phrases", spec.required_any_phrases)
        base._set_value(template, "keyword_gate_message", spec.keyword_gate_message)
        base._set_value(template, "return_direct", True)
        if is_data_analysis:
            base._set_value(template, "preferred_output_names", "api_response")
            base._set_value(template, "enable_auto_continuation", True)
            base._set_value(template, "max_continuation_stages", 2)
            base._set_value(template, "continuation_timeout_seconds", 240)
        flow["data"]["nodes"].append(tool)
        base.add_edge(flow, tool, "component_as_tool", agent, "tools")

    base.add_edge(flow, agent, "response", output, "input_value")
    # Active flows use native Chat Input/Chat Output only.
    return flow


def write_flow() -> dict[str, Any]:
    flow = base._stamp_flow_version(build_flow(base.load_donor()))
    EXPORT_PATH.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {
        "path": str(EXPORT_PATH),
        "name": flow.get("name"),
        "endpoint_name": flow.get("endpoint_name"),
        "nodes": len(flow.get("data", {}).get("nodes", [])),
        "edges": len(flow.get("data", {}).get("edges", [])),
    }


def main() -> None:
    print(json.dumps(write_flow(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
