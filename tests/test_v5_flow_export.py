from __future__ import annotations

import json
from pathlib import Path

from tools.build_import_ready_bundle import FLOW_SPECS, build_bundle
from tools.build_v5_auxiliary_flows import (
    build_agent_tool_router_flow,
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
    "07. v5_realtime_production_report",
    "10. v5_report_followup",
    "11. v5_realtime_production_report_legacy",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _all_active_flows() -> list[dict]:
    manifest = _load(IMPORT_ROOT / "manifest.json")
    return [_load(IMPORT_ROOT / item["file"]) for item in manifest["flows"]]


def test_selected_flow_manifest_contains_only_supported_flows() -> None:
    manifest = _load(IMPORT_ROOT / "manifest.json")
    assert manifest["flow_count"] == 9
    assert [item["name"] for item in manifest["flows"]] == EXPECTED_NAMES
    assert [item["order"] for item in manifest["flows"]] == [1, 2, 3, 4, 5, 6, 7, 10, 11]


def test_active_exports_and_imports_have_no_gaia_boundary_nodes() -> None:
    for flow in _all_active_flows():
        nodes = flow["data"]["nodes"]
        assert not [node for node in nodes if "GaiA" in str(node.get("data", {}).get("type", ""))]
        assert sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) == 1
        assert sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes) == 1

        for edge in flow["data"]["edges"]:
            assert "GaiA" not in str(edge.get("source", ""))
            assert "GaiA" not in str(edge.get("target", ""))


def test_native_boundaries_keep_analysis_direct_and_clean_agent_tool_results() -> None:
    analysis = _load(EXPORT_ROOT / "data_analysis_flow_v2_standalone.json")
    agent = _load(EXPORT_ROOT / "06_agent_tool_router_flow_v5_standalone.json")

    analysis_edges = {(edge["source"], edge["target"]) for edge in analysis["data"]["edges"]}
    assert ("ChatInput-Xs7uo", "CustomComponent-xpbhS") in analysis_edges
    assert ("CustomComponent-A5y0b", "ChatOutput-rwbTs") in analysis_edges

    agent_edges = {(edge["source"], edge["target"]) for edge in agent["data"]["edges"]}
    assert ("ChatInput-agent-tool-router", "Agent-agent-tool-router") in agent_edges
    assert ("Agent-agent-tool-router", "DirectToolResultAdapter-agent-tool-router") in agent_edges
    assert ("DirectToolResultAdapter-agent-tool-router", "ChatOutput-agent-tool-router") in agent_edges
    router_agent = next(node for node in agent["data"]["nodes"] if node["id"] == "Agent-agent-tool-router")
    assert router_agent["data"]["type"] == "SilentDirectReturnRouterAgent"
    router_template = router_agent["data"]["node"]["template"]
    assert router_template["add_calculator_tool"]["value"] is False
    assert "add_calculator_tool" in router_agent["data"]["node"]["field_order"]


def test_default_router_exposes_one_dedicated_report_followup_tool() -> None:
    flow = build_agent_tool_router_flow(load_donor())
    nodes = flow["data"]["nodes"]
    tool_nodes = [
        node
        for node in nodes
        if str(node.get("id") or "").startswith("CachedFlowTool-")
    ]
    assert len(tool_nodes) == 7

    report_tool = next(
        node for node in tool_nodes if node["id"] == "CachedFlowTool-report_followup"
    )
    template = report_tool["data"]["node"]["template"]
    assert template["flow_name_selected"]["value"] == "10. v5_report_followup"
    assert template["flow_id_selected"]["value"] == ""
    assert template["tool_name"]["value"] == "run_report_followup"
    assert template["return_direct"]["value"] is True
    assert template["required_all_keywords"]["value"] == ""
    assert template["required_any_phrases"]["value"] == ""
    assert "새 groupby 집계" in template["tool_description"]["value"]

    data_template = next(
        node for node in tool_nodes if node["id"] == "CachedFlowTool-data_analysis"
    )["data"]["node"]["template"]
    realtime_template = next(
        node for node in tool_nodes if node["id"] == "CachedFlowTool-realtime_production_report"
    )["data"]["node"]["template"]
    assert data_template["flow_name_selected"]["value"] == "01. v5_data_analysis"
    assert data_template["tool_name"]["value"] == "run_data_analysis"
    assert realtime_template["flow_name_selected"]["value"] == "07. v5_realtime_production_report"
    assert all(
        node["data"]["node"]["template"]["flow_name_selected"]["value"]
        != "11. v5_realtime_production_report_legacy"
        for node in tool_nodes
    )


def test_router_freshness_phrases_do_not_treat_report_column_names_as_refresh() -> None:
    system_prompt = (
        ROOT / "langflow_components" / "route_flow_v2" / "SYSTEM_PROMPT_KO.md"
    ).read_text(encoding="utf-8")

    for phrase in ("현재 기준", "현재 데이터", "지금 시점", "최신 데이터", "다시 조회", "새로 조회"):
        assert phrase in system_prompt
    assert "`현재작업재공`, `현재고`, `현재수량`" in system_prompt
    assert "새 groupby 집계" in system_prompt


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

    context_id = "ReportContextPayload-realtime-production-report"
    result_store_id = "ReportContextResultStore-realtime-production-report"
    report_id = "RealtimeProductionReportBuilder-realtime-production-report"
    session_writer_id = "ReportSessionStateWriter-realtime-production-report"
    terminal_id = "RealtimeProductionReportApiTerminal-realtime-production-report"

    assert {
        context_id,
        result_store_id,
        report_id,
        session_writer_id,
        terminal_id,
    }.issubset(nodes)
    assert ("ChatInput-realtime-production-report", context_id) in edges
    assert ("ProcessGroupSelectionGate-realtime-production-report", context_id) in edges
    assert (context_id, result_store_id) in edges
    assert (result_store_id, report_id) in edges
    assert (report_id, session_writer_id) in edges
    assert (session_writer_id, terminal_id) in edges
    assert (report_id, terminal_id) in edges
    assert (terminal_id, "ChatOutput-realtime-production-report") in edges
    assert (report_id, "ChatOutput-realtime-production-report") not in edges
    assert (
        "ChatInput-realtime-production-report",
        "message",
        context_id,
        "question",
    ) in edge_ports
    assert (
        "ProcessGroupSelectionGate-realtime-production-report",
        "selected_dataset",
        context_id,
        "dataset",
    ) in edge_ports
    assert (context_id, "context_payload", result_store_id, "payload") in edge_ports
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
    assert {context_id, result_store_id, report_id, session_writer_id, terminal_id}.issubset(ancestors)

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

    assert flow["name"] == "11. v5_realtime_production_report_legacy"
    assert flow["endpoint_name"] == "metadata-driven-v5-realtime-production-report-legacy"
    assert len(nodes) == 9
    assert len(flow["data"]["edges"]) == 11
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

    assert flow["name"] == "10. v5_report_followup"
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
    assert [item["order"] for item in result["flows"]] == [1, 2, 3, 4, 5, 6, 7, 10, 11]
    combined = _load(tmp_path / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json")
    assert len(combined["flows"]) == 9
