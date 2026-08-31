from __future__ import annotations

import json
from pathlib import Path

from tools.build_import_ready_bundle import FLOW_SPECS, build_bundle
from tools.build_v5_auxiliary_flows import (
    build_agent_tool_router_flow,
    build_metadata_qa_flow,
    build_realtime_production_report_legacy_flow,
    build_realtime_production_report_flow,
    build_report_followup_flow,
    load_donor,
)


ROOT = Path(__file__).resolve().parents[1]
EXPORT_ROOT = ROOT / "flow_exports"
IMPORT_ROOT = ROOT / "import_ready_flows"
EXPECTED_NAMES = [
    "01. v5_data_analysis",
    "02. v5_domain_saving",
    "03. v5_table_catalog_saving",
    "04. v5_main_flow_filter_saving",
    "05. v5_metadata_qa",
    "06. v5_agent_tool_router",
    "07. v5_realtime_production_report_legacy",
    "07-1. v5_realtime_production_report",
    "07-2. v5_report_followup",
]
EXPECTED_FILES = [
    "01_data_analysis_flow_v2_standalone.json",
    "02_domain_saving_flow_v5_standalone.json",
    "03_table_catalog_saving_flow_v5_standalone.json",
    "04_main_flow_filter_saving_flow_v5_standalone.json",
    "05_metadata_qa_flow_v5_standalone.json",
    "06_agent_tool_router_flow_v5_standalone.json",
    "07_realtime_production_report_legacy_flow_v5_standalone.json",
    "07_1_realtime_production_report_flow_v5_standalone.json",
    "07_2_report_followup_flow_v5_standalone.json",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _all_active_flows() -> list[dict]:
    manifest = _load(IMPORT_ROOT / "manifest.json")
    return [_load(IMPORT_ROOT / item["file"]) for item in manifest["flows"]]


def _informational_notes(flow: dict) -> list[dict]:
    return [node for node in flow["data"]["nodes"] if node.get("type") == "noteNode"]


def test_auxiliary_flows_document_stages_with_non_executing_sticky_notes() -> None:
    expected_prefix = "note-auxiliary-"
    for flow in _all_active_flows():
        if flow["name"] == "01. v5_data_analysis":
            # Flow 01 owns its more detailed Fast/Complex note set and has a
            # dedicated canvas regression test in test_data_analysis_flow_v2.
            continue
        notes = _informational_notes(flow)
        note_ids = {str(node.get("id") or "") for node in notes}
        execution_nodes = [node for node in flow["data"]["nodes"] if node.get("type") != "noteNode"]
        min_execution_y = min(float(node["position"]["y"]) for node in execution_nodes)

        assert len(notes) == 4
        assert all(note_id.startswith(expected_prefix) for note_id in note_ids)
        assert all(node["data"]["type"] == "note" for node in notes)
        assert all(node["data"]["node"]["description"].startswith("## ") for node in notes)
        assert all(
            float(node["position"]["y"]) + float(node["height"]) <= min_execution_y
            for node in notes
        )
        assert not any(
            edge["source"] in note_ids or edge["target"] in note_ids
            for edge in flow["data"]["edges"]
        )


def test_selected_flow_manifest_contains_only_supported_flows() -> None:
    manifest = _load(IMPORT_ROOT / "manifest.json")
    assert manifest["flow_count"] == 9
    assert [item["name"] for item in manifest["flows"]] == EXPECTED_NAMES
    assert [item["order"] for item in manifest["flows"]] == list(range(1, 10))
    assert [item["display_order"] for item in manifest["flows"]] == [
        "01", "02", "03", "04", "05", "06", "07", "07-1", "07-2"
    ]
    assert [item["file"] for item in manifest["flows"]] == EXPECTED_FILES
    endpoint_by_name = {item["name"]: item["endpoint_name"] for item in manifest["flows"]}
    assert endpoint_by_name["07. v5_realtime_production_report_legacy"].endswith(
        "-realtime-production-report-legacy"
    )
    assert endpoint_by_name["07-1. v5_realtime_production_report"].endswith(
        "-realtime-production-report"
    )
    assert endpoint_by_name["07-2. v5_report_followup"].endswith("-report-followup")


def test_active_exports_keep_native_boundaries_and_router_has_gaia_ingress_and_session_extractor() -> None:
    for flow in _all_active_flows():
        nodes = flow["data"]["nodes"]
        if flow["name"] == "06. v5_agent_tool_router":
            gaia_input = next(node for node in nodes if node["id"] == "GaiAInput-agent-tool-router")
            gaia_session_extractor = next(
                node for node in nodes if node["id"] == "GaiAExternalSessionIdExtractor-agent-tool-router"
            )
            # Langflow only routes a top-level /run input_value to the
            # ChatInput runtime type. The custom node's display/source prove
            # that this is the operating GaiA Input, not the native node.
            assert gaia_input["data"]["type"] == "ChatInput"
            assert gaia_input["data"]["node"]["display_name"] == "GaiA Input"
            assert "class GaiAInput" in gaia_input["data"]["node"]["template"]["code"]["value"]
            assert gaia_session_extractor["data"]["type"] == "GaiAExternalSessionIdExtractor"
            assert sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) == 1
            assert not any(node["id"] == "ChatInput-agent-tool-router" for node in nodes)
        else:
            assert sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) == 1
        assert sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes) == 1


def test_native_boundaries_keep_analysis_direct_and_clean_agent_tool_results() -> None:
    analysis = _load(EXPORT_ROOT / "data_analysis_flow_v2_standalone.json")
    agent = _load(EXPORT_ROOT / "06_agent_tool_router_flow_v5_standalone.json")

    analysis_edges = {(edge["source"], edge["target"]) for edge in analysis["data"]["edges"]}
    assert ("ChatInput-Xs7uo", "CustomComponent-xpbhS") in analysis_edges
    assert ("CustomComponent-A5y0b", "ChatOutput-rwbTs") in analysis_edges

    agent_edges = {(edge["source"], edge["target"]) for edge in agent["data"]["edges"]}
    assert ("GaiAInput-agent-tool-router", "GaiAExternalSessionIdExtractor-agent-tool-router") in agent_edges
    assert ("GaiAInput-agent-tool-router", "RouterSessionContext-agent-tool-router") in agent_edges
    assert ("RouterSessionContext-agent-tool-router", "Agent-agent-tool-router") in agent_edges
    assert ("Agent-agent-tool-router", "DirectToolResultAdapter-agent-tool-router") in agent_edges
    assert ("DirectToolResultAdapter-agent-tool-router", "RouterSessionStateWriter-agent-tool-router") in agent_edges
    assert ("RouterSessionStateWriter-agent-tool-router", "ChatOutput-agent-tool-router") in agent_edges
    router_agent = next(node for node in agent["data"]["nodes"] if node["id"] == "Agent-agent-tool-router")
    assert router_agent["data"]["type"] == "SilentDirectReturnRouterAgent"
    router_template = router_agent["data"]["node"]["template"]
    assert router_template["add_calculator_tool"]["value"] is False
    assert router_template["n_messages"]["value"] == 1
    assert "add_calculator_tool" in router_agent["data"]["node"]["field_order"]
    context_loader = next(node for node in agent["data"]["nodes"] if node["id"] == "RouterSessionContext-agent-tool-router")
    gaia_input = next(node for node in agent["data"]["nodes"] if node["id"] == "GaiAInput-agent-tool-router")
    gaia_session_extractor = next(
        node for node in agent["data"]["nodes"] if node["id"] == "GaiAExternalSessionIdExtractor-agent-tool-router"
    )
    writer = next(node for node in agent["data"]["nodes"] if node["id"] == "RouterSessionStateWriter-agent-tool-router")
    assert context_loader["data"]["type"] == "RouterSessionContextLoader"
    assert gaia_input["data"]["type"] == "ChatInput"
    assert gaia_input["data"]["node"]["display_name"] == "GaiA Input"
    assert gaia_session_extractor["data"]["type"] == "GaiAExternalSessionIdExtractor"
    assert writer["data"]["type"] == "RouterSessionStateWriter"
    for node in (context_loader, writer):
        template = node["data"]["node"]["template"]
        assert template["mongo_database"]["value"] == "datagov"
        assert template["session_collection_name"]["value"] == "router_session_states"
        assert template["mongo_uri"]["value"] == "MONGO_URL"
        assert template["mongo_uri"]["load_from_db"] is True
    context_template = context_loader["data"]["node"]["template"]
    assert context_template["enabled"]["value"] is True
    assert "history_limit" not in context_template
    assert "history_limit" not in writer["data"]["node"]["template"]
    for input_name in ("input_message", "session_id", "data", "metadata", "mongo_uri"):
        assert input_name in context_template
    edge_ports = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in agent["data"]["edges"]
    }
    assert (
        "GaiAInput-agent-tool-router",
        "message",
        "GaiAExternalSessionIdExtractor-agent-tool-router",
        "input_message",
    ) in edge_ports
    assert (
        "GaiAInput-agent-tool-router",
        "message",
        "RouterSessionContext-agent-tool-router",
        "input_message",
    ) in edge_ports
    assert (
        "GaiAExternalSessionIdExtractor-agent-tool-router",
        "external_session_id",
        "RouterSessionContext-agent-tool-router",
        "session_id",
    ) in edge_ports
    assert (
        "GaiAInput-agent-tool-router",
        "message",
        "RouterSessionContext-agent-tool-router",
        "session_id",
    ) not in edge_ports
    assert (
        "RouterSessionContext-agent-tool-router",
        "message",
        "Agent-agent-tool-router",
        "input_value",
    ) in edge_ports


def test_router_gaia_ingress_receives_external_input_and_session_edge_uses_message_type() -> None:
    """Protect the actual GAIA ingress/type contract, not only JSON wiring."""

    from lfx.graph.graph.base import Graph

    flow = _load(EXPORT_ROOT / "06_agent_tool_router_flow_v5_standalone.json")
    graph = Graph.from_payload(flow["data"], flow_id="router-gaia-input-test", user_id=None, flow_name=flow["name"])
    ingress = graph.get_vertex("GaiAInput-agent-tool-router")
    assert ingress.vertex_type == "ChatInput"
    assert ingress.is_input is True
    assert ingress.display_name == "GaiA Input"

    # This is the same top-level input_value path GAIA sends to the Flow.
    question = "WB공정은?"
    graph._set_inputs([], {"input_value": question}, "chat")
    assert ingress.params["input_value"] == question

    edges = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
            tuple(edge["data"]["sourceHandle"]["output_types"]),
            tuple(edge["data"]["targetHandle"]["inputTypes"]),
        )
        for edge in flow["data"]["edges"]
    }
    assert (
        "GaiAExternalSessionIdExtractor-agent-tool-router",
        "external_session_id",
        "RouterSessionContext-agent-tool-router",
        "session_id",
        ("Message",),
        ("Message",),
    ) in edges


def test_default_router_exposes_six_supported_tools_without_report_followup() -> None:
    flow = build_agent_tool_router_flow(load_donor())
    nodes = flow["data"]["nodes"]
    router_agent = next(node for node in nodes if node["id"] == "Agent-agent-tool-router")
    router_template = router_agent["data"]["node"]["template"]
    assert router_template["handle_parsing_errors"]["value"] is False
    assert router_template["max_iterations"]["value"] == 1

    tool_nodes = [
        node
        for node in nodes
        if str(node.get("id") or "").startswith("CachedFlowTool-")
    ]
    assert len(tool_nodes) == 6
    assert "CachedFlowTool-report_followup" not in {node["id"] for node in tool_nodes}
    tool_names = {
        node["data"]["node"]["template"]["tool_name"]["value"]
        for node in tool_nodes
    }
    assert tool_names == {
        "run_data_analysis",
        "run_metadata_qa",
        "save_domain_metadata",
        "save_table_catalog_metadata",
        "save_main_flow_filter_metadata",
        "run_realtime_production_report",
    }

    data_template = next(
        node for node in tool_nodes if node["id"] == "CachedFlowTool-data_analysis"
    )["data"]["node"]["template"]
    realtime_template = next(
        node for node in tool_nodes if node["id"] == "CachedFlowTool-realtime_production_report"
    )["data"]["node"]["template"]
    assert data_template["flow_name_selected"]["value"] == "01. v5_data_analysis"
    assert data_template["tool_name"]["value"] == "run_data_analysis"
    assert realtime_template["flow_name_selected"]["value"] == "07-1. v5_realtime_production_report"
    assert all(
        node["data"]["node"]["template"]["flow_name_selected"]["value"]
        not in {
            "07. v5_realtime_production_report_legacy",
            "07-2. v5_report_followup",
        }
        for node in tool_nodes
    )
    edge_ports = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }
    for tool in tool_nodes:
        assert (
            "RouterSessionContext-agent-tool-router",
            "canonical_session_id",
            tool["id"],
            "session_id",
        ) in edge_ports


def test_router_prompt_supports_general_followups_without_report_followup_tool() -> None:
    system_prompt = (
        ROOT / "langflow_components" / "route_flow_v2" / "SYSTEM_PROMPT_KO.md"
    ).read_text(encoding="utf-8")

    assert "직전 질문·답변·선택 Flow" in system_prompt
    assert "후속 질문" in system_prompt
    assert "run_data_analysis" in system_prompt
    assert "WB공정은?" in system_prompt
    assert "어떤 제품은?" in system_prompt
    assert "자재는?" in system_prompt
    assert "OPER에서는?" in system_prompt
    assert "어제 일자는?" in system_prompt
    assert "세부 제품별로 보여줘" in system_prompt
    assert "후속 분석 우선 판별" in system_prompt
    assert "run_metadata_qa` 선택보다 먼저 수행" in system_prompt
    assert "WB 공정의 정의를 묻는 질문으로 바꾸어 해석하지 않습니다" in system_prompt
    assert "공정·제품·OPER·자재라는 단어만으로 `run_metadata_qa`를 선택하지 않습니다" in system_prompt
    assert "짧은 변경형 질문에 대상 또는 시점이 새로 들어갔다는 사실만으로는 독립 신규 요청으로 보지 않습니다" in system_prompt
    assert "현재 질문 원문만 전달" in system_prompt
    assert "run_report_followup" not in system_prompt
    assert "Report Snapshot" not in system_prompt
    assert "Flow 07-2" not in system_prompt


def test_base_data_analysis_hides_explicit_upstream_result_reference() -> None:
    analysis = _load(EXPORT_ROOT / "data_analysis_flow_v2_standalone.json")
    request_loader = next(
        node
        for node in analysis["data"]["nodes"]
        if node.get("id") == "CustomComponent-xpbhS"
    )
    component = request_loader["data"]["node"]
    template = component["template"]

    assert component["field_order"] == ["question", "previous_state"]
    assert "upstream_result_ref" not in template


def test_metadata_qa_flow_restores_and_persists_compact_catalog_inventory_state() -> None:
    flow = build_metadata_qa_flow(load_donor())
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edge_ports = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }
    loader_id = "SessionStateLoader-metadata-qa"
    writer_id = "SessionStateWriter-metadata-qa"

    assert {loader_id, writer_id}.issubset(nodes)
    assert (
        "ChatInput-metadata-qa",
        "message",
        loader_id,
        "question",
    ) in edge_ports
    assert (
        loader_id,
        "loaded_state",
        "Request-metadata-qa",
        "previous_state",
    ) in edge_ports
    assert (
        "Normalizer-metadata-qa",
        "payload_out",
        writer_id,
        "response_payload",
    ) in edge_ports
    assert (
        writer_id,
        "payload_out",
        "Message-metadata-qa",
        "payload",
    ) in edge_ports
    assert (
        writer_id,
        "payload_out",
        "Api-metadata-qa",
        "payload",
    ) in edge_ports

    for node_id in (loader_id, writer_id):
        template = nodes[node_id]["data"]["node"]["template"]
        assert template["mongo_uri"]["value"] == "MONGO_URL"
        assert template["mongo_uri"]["load_from_db"] is True
        assert template["mongo_database"]["value"] == "datagov"
        assert template["session_collection_name"]["value"] == "agent_v4_session_states"


def test_realtime_report_flow_publishes_context_and_session_state() -> None:
    flow = build_realtime_production_report_flow(load_donor())
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edges = {(edge["source"], edge["target"]) for edge in flow["data"]["edges"]}
    edge_ports = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }

    bundle_id = "RealtimeReportViewBundle-realtime-production-report"
    publisher_id = "ReportContextPublisher-realtime-production-report"
    result_store_id = "ReportContextResultStore-realtime-production-report"
    report_id = "RealtimeProductionReportBuilder-realtime-production-report"
    session_writer_id = "ReportSessionStateWriter-realtime-production-report"
    terminal_id = "RealtimeProductionReportApiTerminal-realtime-production-report"
    gate_id = "ProcessGroupSelectionGate-realtime-production-report"

    assert len([node for node in nodes.values() if node["type"] != "noteNode"]) == 11
    assert len(flow["data"]["edges"]) == 15
    assert "ProcessGroupPrompt-realtime-production-report" not in nodes
    assert "LanguageModelProcessGroup-realtime-production-report" not in nodes
    assert {
        bundle_id,
        publisher_id,
        result_store_id,
        report_id,
        session_writer_id,
        terminal_id,
        gate_id,
    }.issubset(nodes)
    assert (
        "RealtimeProductionDeterministicProcessGroupSelectionGate"
        in nodes[gate_id]["data"]["node"]["template"]["code"]["value"]
    )
    assert ("ChatInput-realtime-production-report", bundle_id) in edges
    assert ("ProcessGroupSelectionGate-realtime-production-report", bundle_id) in edges
    assert ("ChatInput-realtime-production-report", publisher_id) in edges
    assert (bundle_id, publisher_id) in edges
    assert (publisher_id, result_store_id) in edges
    assert (result_store_id, report_id) in edges
    assert (report_id, session_writer_id) in edges
    assert (session_writer_id, terminal_id) in edges
    assert (report_id, terminal_id) in edges
    assert (terminal_id, "ChatOutput-realtime-production-report") in edges
    assert (report_id, "ChatOutput-realtime-production-report") not in edges
    assert (
        "ChatInput-realtime-production-report",
        "message",
        bundle_id,
        "question",
    ) in edge_ports
    assert (
        "ProcessGroupSelectionGate-realtime-production-report",
        "selected_dataset",
        bundle_id,
        "dataset",
    ) in edge_ports
    assert (
        "ChatInput-realtime-production-report",
        "message",
        publisher_id,
        "question",
    ) in edge_ports
    assert (bundle_id, "report_bundle", publisher_id, "report_bundle") in edge_ports
    assert (publisher_id, "context_payload", result_store_id, "payload") in edge_ports
    assert (result_store_id, "payload_out", report_id, "context_payload") in edge_ports
    assert (report_id, "api_response", session_writer_id, "response_payload") in edge_ports
    assert (session_writer_id, "payload_out", terminal_id, "report_result") in edge_ports
    assert (report_id, "message", terminal_id, "report_message") in edge_ports
    assert (terminal_id, "message", "ChatOutput-realtime-production-report", "input_value") in edge_ports

    reverse_edges: dict[str, set[str]] = {}
    for source, target in edges:
        reverse_edges.setdefault(target, set()).add(source)
    ancestors: set[str] = set()
    pending = ["ChatOutput-realtime-production-report"]
    while pending:
        current = pending.pop()
        for parent in reverse_edges.get(current, set()):
            if parent not in ancestors:
                ancestors.add(parent)
                pending.append(parent)
    assert {bundle_id, publisher_id, result_store_id, report_id, session_writer_id, terminal_id}.issubset(ancestors)

    result_store_template = nodes[result_store_id]["data"]["node"]["template"]
    assert result_store_template["mongo_uri"]["value"] == "MONGO_URL"
    assert result_store_template["mongo_uri"]["load_from_db"] is True
    assert result_store_template["mongo_database"]["value"] == "datagov"
    assert result_store_template["collection_name"]["value"] == "agent_v4_result_store"
    assert result_store_template["ttl_hours"]["value"] == "4"

    session_template = nodes[session_writer_id]["data"]["node"]["template"]
    assert session_template["mongo_uri"]["value"] == "MONGO_URL"
    assert session_template["mongo_uri"]["load_from_db"] is True
    assert session_template["mongo_database"]["value"] == "datagov"
    assert session_template["session_collection_name"]["value"] == "agent_v4_session_states"


def test_legacy_realtime_report_preserves_original_direct_graph() -> None:
    flow = build_realtime_production_report_legacy_flow(load_donor())
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edge_ports = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }
    suffix = "realtime-production-report-legacy"
    report_id = f"RealtimeProductionReportBuilder-{suffix}"

    assert flow["name"] == "07. v5_realtime_production_report_legacy"
    assert flow["endpoint_name"] == "metadata-driven-v5-realtime-production-report-legacy"
    assert len([node for node in nodes.values() if node["type"] != "noteNode"]) == 9
    assert len(flow["data"]["edges"]) == 11
    assert f"ProcessGroupPrompt-{suffix}" in nodes
    assert f"LanguageModelProcessGroup-{suffix}" in nodes
    assert (
        "RealtimeProductionProcessGroupSelectionGate"
        in nodes[f"ProcessGroupSelectionGate-{suffix}"]["data"]["node"]["template"]["code"]["value"]
    )
    assert not [node_id for node_id in nodes if "ReportContext" in node_id or "SessionStateWriter" in node_id]
    assert (report_id, "message", f"ChatOutput-{suffix}", "input_value") in edge_ports
    assert (
        report_id,
        "api_response",
        f"RealtimeProductionReportApiTerminal-{suffix}",
        "report_result",
    ) in edge_ports


def test_report_followup_flow_restores_snapshot_without_source_retrievers() -> None:
    flow = build_report_followup_flow(load_donor())
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edge_ports = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }

    assert flow["name"] == "07-2. v5_report_followup"
    assert flow["last_tested_version"] == "1.11.0"
    assert sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes.values()) == 1
    assert sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes.values()) == 1
    assert {
        "SessionStateLoader-report-followup",
        "PromptBuilder-report-followup",
        "GuardedPlanRouter-report-followup",
        "PlanNormalizer-report-followup",
        "ResultLoader-report-followup",
        "SnapshotExecutor-report-followup",
        "ResponseBuilder-report-followup",
        "SessionStateWriter-report-followup",
        "ApiTerminal-report-followup",
    }.issubset(nodes)
    assert "LanguageModel-report-followup" not in nodes
    assert not [
        node_id
        for node_id in nodes
        if any(token in node_id for token in ("Oracle", "Goodocs", "Datalake", "RetrievalJob"))
    ]
    assert (
        "ChatInput-report-followup",
        "message",
        "SessionStateLoader-report-followup",
        "question",
    ) in edge_ports
    assert (
        "SessionStateLoader-report-followup",
        "loaded_state",
        "PromptBuilder-report-followup",
        "loaded_state",
    ) in edge_ports
    assert (
        "PromptBuilder-report-followup",
        "payload_out",
        "GuardedPlanRouter-report-followup",
        "payload",
    ) in edge_ports
    assert (
        "PromptBuilder-report-followup",
        "prompt",
        "GuardedPlanRouter-report-followup",
        "prompt",
    ) in edge_ports
    assert (
        "GuardedPlanRouter-report-followup",
        "text_output",
        "PlanNormalizer-report-followup",
        "llm_response",
    ) in edge_ports
    assert (
        "PlanNormalizer-report-followup",
        "payload_out",
        "ResultLoader-report-followup",
        "payload",
    ) in edge_ports
    assert (
        "ResultLoader-report-followup",
        "payload_out",
        "SnapshotExecutor-report-followup",
        "payload",
    ) in edge_ports
    assert (
        "SessionStateWriter-report-followup",
        "payload_out",
        "ApiTerminal-report-followup",
        "response_payload",
    ) in edge_ports
    assert (
        "ApiTerminal-report-followup",
        "message",
        "ChatOutput-report-followup",
        "input_value",
    ) in edge_ports
    assert not [
        edge
        for edge in flow["data"]["edges"]
        if edge["source"] == "ApiTerminal-report-followup"
        and edge["data"]["sourceHandle"]["name"] == "api_response"
    ]

    loader_template = nodes["SessionStateLoader-report-followup"]["data"]["node"]["template"]
    guarded_template = nodes["GuardedPlanRouter-report-followup"]["data"]["node"]["template"]
    result_template = nodes["ResultLoader-report-followup"]["data"]["node"]["template"]
    writer_template = nodes["SessionStateWriter-report-followup"]["data"]["node"]["template"]
    for template in (loader_template, result_template, writer_template):
        assert template["mongo_uri"]["value"] == "MONGO_URL"
        assert template["mongo_uri"]["load_from_db"] is True
        assert template["mongo_database"]["value"] == "datagov"
    assert result_template["collection_name"]["value"] == "agent_v4_result_store"
    assert loader_template["session_collection_name"]["value"] == "agent_v4_session_states"
    assert writer_template["session_collection_name"]["value"] == "agent_v4_session_states"
    assert nodes["GuardedPlanRouter-report-followup"]["data"]["type"] == "ReportFollowupGuardedPlanRouter"
    assert {"model", "model_name", "provider", "api_key"}.issubset(guarded_template)
    assert guarded_template["model"]["value"][0]["name"] == "gemini-3.5-flash-lite"
    assert guarded_template["model_name"]["name"] == "model_name"
    assert guarded_template["provider"]["name"] == "provider"
    assert guarded_template["api_key"]["name"] == "api_key"
    assert guarded_template["max_tokens"]["value"] == 1800
    assert "status != \"ready\"" in guarded_template["code"]["value"]


def test_bundle_builder_is_reproducible_for_selected_base_flows(tmp_path: Path) -> None:
    result = build_bundle(tmp_path)
    assert result["flow_count"] == 9
    assert [item["name"] for item in result["flows"]] == EXPECTED_NAMES
    assert [item["order"] for item in result["flows"]] == list(range(1, 10))
    assert [item["display_order"] for item in result["flows"]] == [
        "01", "02", "03", "04", "05", "06", "07", "07-1", "07-2"
    ]
    assert [item["file"] for item in result["flows"]] == EXPECTED_FILES
    combined = _load(tmp_path / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json")
    assert len(combined["flows"]) == 9
