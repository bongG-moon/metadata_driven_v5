from __future__ import annotations

import json
from pathlib import Path

from tools.build_import_ready_bundle import FLOW_SPECS, build_bundle


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
    "08. v5_data_analysis_continuation",
    "09. v5_agent_tool_router_continuation",
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
    assert [item["order"] for item in manifest["flows"]] == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_active_exports_and_imports_have_no_gaia_boundary_nodes() -> None:
    for flow in _all_active_flows():
        nodes = flow["data"]["nodes"]
        assert not [node for node in nodes if "GaiA" in str(node.get("data", {}).get("type", ""))]
        assert sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) == 1
        assert sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes) == 1

        for edge in flow["data"]["edges"]:
            assert "GaiA" not in str(edge.get("source", ""))
            assert "GaiA" not in str(edge.get("target", ""))


def test_native_boundaries_are_direct_for_analysis_and_agent_router() -> None:
    analysis = _load(EXPORT_ROOT / "data_analysis_flow_v2_standalone.json")
    agent = _load(EXPORT_ROOT / "06_agent_tool_router_flow_v5_standalone.json")

    analysis_edges = {(edge["source"], edge["target"]) for edge in analysis["data"]["edges"]}
    assert ("ChatInput-Xs7uo", "CustomComponent-xpbhS") in analysis_edges
    assert ("CustomComponent-A5y0b", "ChatOutput-rwbTs") in analysis_edges

    agent_edges = {(edge["source"], edge["target"]) for edge in agent["data"]["edges"]}
    assert ("ChatInput-agent-tool-router", "Agent-agent-tool-router") in agent_edges
    assert ("Agent-agent-tool-router", "ChatOutput-agent-tool-router") in agent_edges


def test_continuation_flows_are_native_and_versioned() -> None:
    for filename in (
        "08_data_analysis_flow_v2_continuation_standalone.json",
        "09_agent_tool_router_continuation_flow_v5_standalone.json",
    ):
        flow = _load(EXPORT_ROOT / filename)
        assert flow["last_tested_version"] == "1.9.2"
        assert all(
            node.get("data", {}).get("node", {}).get("lf_version") == "1.9.2"
            for node in flow["data"]["nodes"]
            if isinstance(node.get("data", {}).get("node"), dict)
        )


def test_bundle_builder_is_reproducible_for_selected_base_flows(tmp_path: Path) -> None:
    result = build_bundle(tmp_path)
    assert result["flow_count"] == 7
    assert [item["name"] for item in result["flows"]] == EXPECTED_NAMES[:7]
    combined = _load(tmp_path / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json")
    assert len(combined["flows"]) == 7
