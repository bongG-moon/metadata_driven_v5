"""Build the complete import-ready bundle for the supported Langflow flows."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "flow_exports"
DEFAULT_OUTPUT_DIR = ROOT / "import_ready_flows"
BUNDLE_VERSION = "20260710"
ENDPOINT_PREFIX = f"metadata-driven-v5-complete-{BUNDLE_VERSION}"
TARGET_LANGFLOW_VERSION = "1.11.0"
TARGET_LANGFLOW_BASE_VERSION = "0.11.0"
TARGET_LFX_VERSION = "1.11.0"
MONGO_GLOBAL_VARIABLE = "MONGO_URL"

MONGODB_CONTRACT = {
    "database": "datagov",
    "domain": "agent_v4_domain_items",
    "table_catalog": "agent_v4_table_catalog_items",
    "main_flow_filter": "agent_v4_main_flow_filters",
    "result": "agent_v4_result_store",
    "session_state": "agent_v4_session_states",
}

# The public route and tool name stay stable while the graph behind Flow 01 is
# the V2 implementation.
CANONICAL_DATA_ANALYSIS_SOURCE = "data_analysis_flow_v2_standalone.json"
FLOW_SPECS = [
    (CANONICAL_DATA_ANALYSIS_SOURCE, "data-analysis", "data_analysis"),
    ("domain_saving_flow_v5_standalone.json", "domain-saving", "domain_saving"),
    ("table_catalog_saving_flow_v5_standalone.json", "table-catalog-saving", "table_catalog_saving"),
    ("main_flow_filter_saving_flow_v5_standalone.json", "main-flow-filter-saving", "main_flow_filter_saving"),
    ("metadata_qa_flow_v5_standalone.json", "metadata-qa", "metadata_qa"),
    ("06_agent_tool_router_flow_v5_standalone.json", "agent-tool-router", "agent_tool_router"),
    ("07_realtime_production_report_legacy_flow_v5_standalone.json", "realtime-production-report-legacy", "realtime_production_report_legacy"),
    ("07_1_realtime_production_report_flow_v5_standalone.json", "realtime-production-report", "realtime_production_report"),
    ("07_2_report_followup_flow_v5_standalone.json", "report-followup", "report_followup"),
]
IMPORT_ORDER = {
    "data_analysis": 1,
    "domain_saving": 2,
    "table_catalog_saving": 3,
    "main_flow_filter_saving": 4,
    "metadata_qa": 5,
    "agent_tool_router": 6,
    "realtime_production_report_legacy": 7,
    "realtime_production_report": 8,
    "report_followup": 9,
}
FLOW_NUMBER_LABELS = {
    "data_analysis": "01",
    "domain_saving": "02",
    "table_catalog_saving": "03",
    "main_flow_filter_saving": "04",
    "metadata_qa": "05",
    "agent_tool_router": "06",
    "realtime_production_report_legacy": "07",
    "realtime_production_report": "07-1",
    "report_followup": "07-2",
}
FLOW_DISPLAY_NAMES = {
    "data_analysis": "01. v5_data_analysis",
    "domain_saving": "02. v5_domain_saving",
    "table_catalog_saving": "03. v5_table_catalog_saving",
    "main_flow_filter_saving": "04. v5_main_flow_filter_saving",
    "metadata_qa": "05. v5_metadata_qa",
    "agent_tool_router": "06. v5_agent_tool_router",
    "realtime_production_report_legacy": "07. v5_realtime_production_report_legacy",
    "realtime_production_report": "07-1. v5_realtime_production_report",
    "report_followup": "07-2. v5_report_followup",
}


def _set_frontend_flow_ids(flow: dict[str, Any], flow_id: str) -> None:
    """Write the deterministic Flow id into any frontend-only template field."""
    for node in flow.get("data", {}).get("nodes", []):
        template = node.get("data", {}).get("node", {}).get("template", {})
        field = template.get("_frontend_node_flow_id") if isinstance(template, dict) else None
        if isinstance(field, dict):
            field["value"] = flow_id


def _component_nodes(flow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        node
        for node in flow.get("data", {}).get("nodes", [])
        if isinstance(node.get("data", {}).get("node"), dict)
    ]


def _custom_component_count(flows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for flow in flows
        for node in _component_nodes(flow)
        if node["data"]["node"].get("template", {}).get("_type") == "Component"
        and isinstance(node["data"]["node"].get("template", {}).get("code"), dict)
    )


def _stamp_flow(flow: dict[str, Any], *, flow_id: str, route_name: str, endpoint_name: str) -> dict[str, Any]:
    flow["id"] = flow_id
    flow["name"] = FLOW_DISPLAY_NAMES[route_name]
    flow["endpoint_name"] = endpoint_name
    flow["last_tested_version"] = TARGET_LANGFLOW_VERSION
    flow["tags"] = sorted(
        set([*flow.get("tags", []), "complete-bundle", BUNDLE_VERSION, "import-ready"])
    )
    _set_frontend_flow_ids(flow, flow_id)
    return flow


def _destination_name(source_name: str, number_label: str) -> str:
    file_prefix = number_label.replace("-", "_")
    return source_name if source_name.startswith(f"{file_prefix}_") else f"{file_prefix}_{source_name}"


def _validate_base_flow(flow: dict[str, Any], item: dict[str, Any]) -> int:
    """Validate the shared native Langflow shape without legacy Flow contracts."""
    nodes = flow.get("data", {}).get("nodes", [])
    edges = flow.get("data", {}).get("edges", [])
    file_name = str(item["file"])

    if any("GaiA" in str(node.get("data", {}).get("type") or "") for node in nodes):
        raise ValueError(f"Active flow still contains a removed GaiA boundary node: {file_name}")
    if sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) != 1:
        raise ValueError(f"Active flow must contain exactly one native Chat Input: {file_name}")
    if sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes) != 1:
        raise ValueError(f"Active flow must contain exactly one native Chat Output: {file_name}")
    for node in _component_nodes(flow):
        if node["data"]["node"].get("lf_version") != TARGET_LANGFLOW_VERSION:
            raise ValueError(f"Flow node version mismatch: {file_name}:{node.get('id')}")

    if item["name"] == FLOW_DISPLAY_NAMES["data_analysis"]:
        adapter = next(
            (node for node in nodes if node.get("id") == "CustomComponent-A5y0b"),
            None,
        )
        if adapter is None:
            raise ValueError("Data Analysis V2 answer adapter is missing.")
        adapter_node = adapter.get("data", {}).get("node", {})
        field_order = adapter_node.get("field_order", [])
        template = adapter_node.get("template", {})
        if {"show_analysis_evidence", "show_next_questions"} & set(field_order):
            raise ValueError("Removed answer-body display options remain in Flow 01 field_order.")
        if {"show_analysis_evidence", "show_next_questions"} & set(template):
            raise ValueError("Removed answer-body display options remain in Flow 01 template.")
        if "show_intermediate_results" not in field_order:
            raise ValueError("Flow 01 must retain the curated intermediate-results display option.")

    if item["name"] == FLOW_DISPLAY_NAMES["agent_tool_router"]:
        edge_pairs = {(str(edge.get("source") or ""), str(edge.get("target") or "")) for edge in edges}
        required = {
            ("ChatInput-agent-tool-router", "Agent-agent-tool-router"),
            ("Agent-agent-tool-router", "DirectToolResultAdapter-agent-tool-router"),
            ("DirectToolResultAdapter-agent-tool-router", "ChatOutput-agent-tool-router"),
        }
        if not required.issubset(edge_pairs):
            raise ValueError("Agent Tool Router must route the direct Tool result through its result adapter before Chat Output.")
        agent = next((node for node in nodes if node.get("id") == "Agent-agent-tool-router"), None)
        if agent is None or agent.get("data", {}).get("type") != "SilentDirectReturnRouterAgent":
            raise ValueError("Agent Tool Router must use the silent direct-return Agent to suppress child Flow event leakage.")
        report_tool = next(
            (node for node in nodes if node.get("id") == "CachedFlowTool-report_followup"),
            None,
        )
        if report_tool is None:
            raise ValueError("Agent Tool Router must expose the dedicated Report follow-up Tool.")
        report_template = report_tool.get("data", {}).get("node", {}).get("template", {})
        if report_template.get("flow_name_selected", {}).get("value") != FLOW_DISPLAY_NAMES["report_followup"]:
            raise ValueError("Report follow-up Tool must target Flow 07-2.")
        if report_template.get("tool_name", {}).get("value") != "run_report_followup":
            raise ValueError("Report follow-up Tool public name is inconsistent.")
        if report_template.get("return_direct", {}).get("value") is not True:
            raise ValueError("Report follow-up Tool must return the child Flow answer directly.")
        data_tool = next(
            (node for node in nodes if node.get("id") == "CachedFlowTool-data_analysis"),
            None,
        )
        if data_tool is None:
            raise ValueError("Agent Tool Router must expose Flow 01 as its sole general Data Analysis Tool.")
        data_template = data_tool.get("data", {}).get("node", {}).get("template", {})
        if data_template.get("flow_name_selected", {}).get("value") != FLOW_DISPLAY_NAMES["data_analysis"]:
            raise ValueError("General Data Analysis Tool must target Flow 01.")
        realtime_tool = next(
            (node for node in nodes if node.get("id") == "CachedFlowTool-realtime_production_report"),
            None,
        )
        realtime_template = (realtime_tool or {}).get("data", {}).get("node", {}).get("template", {})
        if realtime_template.get("flow_name_selected", {}).get("value") != FLOW_DISPLAY_NAMES["realtime_production_report"]:
            raise ValueError("Router realtime Report Tool must target the follow-up-enabled Flow 07-1.")

    if item["name"] == FLOW_DISPLAY_NAMES["report_followup"]:
        edge_ports = {
            (
                str(edge.get("source") or ""),
                str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
                str(edge.get("target") or ""),
                str(edge.get("data", {}).get("targetHandle", {}).get("fieldName") or ""),
            )
            for edge in edges
        }
        required = {
            (
                "SessionStateLoader-report-followup",
                "loaded_state",
                "PromptBuilder-report-followup",
                "loaded_state",
            ),
            (
                "PlanNormalizer-report-followup",
                "payload_out",
                "ResultLoader-report-followup",
                "payload",
            ),
            (
                "ResultLoader-report-followup",
                "payload_out",
                "SnapshotExecutor-report-followup",
                "payload",
            ),
            (
                "ApiTerminal-report-followup",
                "message",
                "ChatOutput-report-followup",
                "input_value",
            ),
        }
        if not required.issubset(edge_ports):
            raise ValueError("Flow 07-2 must restore and execute the Report Snapshot before its single Chat Output.")
        if any(
            any(token in str(node.get("id") or "") for token in ("Oracle", "Goodocs", "Datalake", "RetrievalJob"))
            for node in nodes
        ):
            raise ValueError("Flow 07-2 must not contain a live source retriever.")

    if item["name"] == FLOW_DISPLAY_NAMES["realtime_production_report"]:
        node_ids = {str(node.get("id") or "") for node in nodes}
        required_ids = {
            "RealtimeReportViewBundle-realtime-production-report",
            "ReportContextPublisher-realtime-production-report",
            "ReportContextResultStore-realtime-production-report",
            "ReportSessionStateWriter-realtime-production-report",
        }
        if not required_ids.issubset(node_ids):
            raise ValueError("Flow 07-1 must publish the same-session Report follow-up Context.")

    if item["name"] == FLOW_DISPLAY_NAMES["realtime_production_report_legacy"]:
        if len(nodes) != 9 or len(edges) != 11:
            raise ValueError("Legacy Report Flow 07 must preserve the original 9-node/11-edge graph.")
        node_ids = {str(node.get("id") or "") for node in nodes}
        if any(token in node_id for node_id in node_ids for token in ("ReportContext", "SessionStateWriter")):
            raise ValueError("Legacy Report Flow 07 must not publish follow-up Context or session state.")
        edge_pairs = {(str(edge.get("source") or ""), str(edge.get("target") or "")) for edge in edges}
        suffix = "realtime-production-report-legacy"
        required_pairs = {
            (f"RealtimeProductionReportBuilder-{suffix}", f"ChatOutput-{suffix}"),
            (f"RealtimeProductionReportBuilder-{suffix}", f"RealtimeProductionReportApiTerminal-{suffix}"),
        }
        if not required_pairs.issubset(edge_pairs):
            raise ValueError("Legacy Report Flow 07 must retain direct Chat/API terminal outputs.")
    return 2 * len(edges)


def _validate_base_bundle(output_dir: Path, manifest_flows: list[dict[str, Any]]) -> int:
    expected_names = [FLOW_DISPLAY_NAMES[route_name] for _, _, route_name in FLOW_SPECS]
    actual_names = [str(item.get("name") or "") for item in manifest_flows]
    if actual_names != expected_names:
        raise ValueError(f"Bundle Flow display names mismatch: actual={actual_names}, expected={expected_names}")
    endpoints = [str(item.get("endpoint_name") or "") for item in manifest_flows]
    if len(endpoints) != len(set(endpoints)):
        raise ValueError("Bundle endpoint_name values must be unique.")
    return sum(
        _validate_base_flow(
            json.loads((output_dir / item["file"]).read_text(encoding="utf-8")),
            item,
        )
        for item in manifest_flows
    )


def _readme(
    manifest_flows: list[dict[str, Any]],
    edge_handle_count: int,
    component_count: int,
) -> str:
    rows = "\n".join(
        f"| {item['display_order']} | `{item['file']}` | `{item['endpoint_name']}` | {item['nodes']} | {item['edges']} |"
        for item in manifest_flows
    )
    return f"""# metadata_driven_v5 import-ready bundle

이 bundle은 현재 지원하는 **9개 Flow(01~06, 07, 07-1, 07-2)**를 포함합니다. 모두 Langflow {TARGET_LANGFLOW_VERSION} / langflow-base {TARGET_LANGFLOW_BASE_VERSION} / LFX {TARGET_LFX_VERSION} 기준으로 생성되었습니다.

## Import

Langflow Desktop에서 `00_metadata_driven_v5_complete_{BUNDLE_VERSION}_ALL_FLOWS.json` 하나를 import하거나, 아래 순서대로 개별 파일을 import합니다.

| 번호 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
{rows}

## 운영 설정

- Langflow 모델 Provider와 `MONGO_URL` Credential Global Variable을 import 후 설정합니다.
- 모든 일반 Data Analysis와 일반 분석 후속 질문은 `01. v5_data_analysis`가 담당합니다.
- 같은 세션의 Report Snapshot 또는 Report가 미리 만든 집계 View에 대한 컬럼 선택·필터·정렬·순위는 `07-2. v5_report_followup`이 담당합니다. 새 groupby 계산, 최신 데이터나 다른 데이터셋이 필요한 질문은 `01. v5_data_analysis`로 보냅니다.
- `07. v5_realtime_production_report_legacy`는 변경 전 직접 응답 구조를 보존한 호환 Flow이며 Router가 자동 선택하지 않습니다. `07-1. v5_realtime_production_report`가 후속분석 Context를 저장하는 현재 Router 대상 Report입니다.
- 결과 CSV/JSON 다운로드와 실시간 Report HTML 발행은 API_SERVER(`python API_SERVER\\app.py`, bind `0.0.0.0:5000`)가 담당합니다. Report HTML과 메타데이터는 API_SERVER의 단일 MongoDB 컬렉션에 저장되므로 Flow의 Report API 주소를 접근 가능한 API URL로 설정합니다.
- 기존 Router Tool에 저장된 `flow_id_selected`가 있으면, import 뒤 대상 Flow를 한 번 다시 선택해 현재 Flow ID로 갱신합니다.

## 생성 시 구조 검증

- GaiA Input/Output boundary node 없음
- 각 Flow의 native Chat Input/Chat Output 각각 1개
- 모든 node `lf_version={TARGET_LANGFLOW_VERSION}`
- edge handle {edge_handle_count}/{edge_handle_count}, custom component template {component_count}/{component_count}
"""


def build_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    """Generate all supported import artifacts and their manifest."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in output_dir.glob("[0-9][0-9]_*_standalone.json"):
        stale_path.unlink()

    manifest_flows: list[dict[str, Any]] = []
    for source_name, endpoint_suffix, route_name in FLOW_SPECS:
        order = IMPORT_ORDER[route_name]
        display_order = FLOW_NUMBER_LABELS[route_name]
        source = SOURCE_DIR / source_name
        if not source.exists():
            raise FileNotFoundError(f"Active flow export is missing: {source}")
        flow = json.loads(source.read_text(encoding="utf-8"))
        endpoint_name = f"{ENDPOINT_PREFIX}-{endpoint_suffix}"
        flow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ENDPOINT_PREFIX}/{endpoint_suffix}"))
        _stamp_flow(flow, flow_id=flow_id, route_name=route_name, endpoint_name=endpoint_name)
        destination = output_dir / _destination_name(source_name, display_order)
        destination.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        manifest_flows.append(
            {
                "order": order,
                "display_order": display_order,
                "file": destination.name,
                "name": flow["name"],
                "endpoint_name": endpoint_name,
                "nodes": len(flow.get("data", {}).get("nodes", [])),
                "edges": len(flow.get("data", {}).get("edges", [])),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )

    flows = [json.loads((output_dir / item["file"]).read_text(encoding="utf-8")) for item in manifest_flows]
    combined_path = output_dir / f"00_metadata_driven_v5_complete_{BUNDLE_VERSION}_ALL_FLOWS.json"
    combined_path.write_bytes(json.dumps({"flows": flows}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    edge_handle_count = _validate_base_bundle(output_dir, manifest_flows)
    component_count = _custom_component_count(flows)

    manifest = {
        "bundle": f"metadata_driven_v5_complete_{BUNDLE_VERSION}",
        "langflow_version": TARGET_LANGFLOW_VERSION,
        "langflow_base_version": TARGET_LANGFLOW_BASE_VERSION,
        "lfx_version": TARGET_LFX_VERSION,
        "flow_count": len(manifest_flows),
        "endpoint_prefix": ENDPOINT_PREFIX,
        "single_file_ui_import": combined_path.name,
        "single_file_ui_import_sha256": hashlib.sha256(combined_path.read_bytes()).hexdigest(),
        "mongodb_contract": {
            "configuration_source": "Langflow node input / MONGO_URL Credential Global Variable",
            **MONGODB_CONTRACT,
        },
        "data_analysis_routing_contract": {
            "canonical_route": "data_analysis",
            "canonical_source": CANONICAL_DATA_ANALYSIS_SOURCE,
            "canonical_display_name": FLOW_DISPLAY_NAMES["data_analysis"],
            "canonical_endpoint_name": f"{ENDPOINT_PREFIX}-data-analysis",
            "external_tool_name": "run_data_analysis",
        },
        "report_followup_routing_contract": {
            "strategy": "isolated_report_snapshot_followup",
            "report_source_flow": FLOW_DISPLAY_NAMES["realtime_production_report"],
            "report_followup_flow": FLOW_DISPLAY_NAMES["report_followup"],
            "router_flow": FLOW_DISPLAY_NAMES["agent_tool_router"],
            "external_tool_name": "run_report_followup",
            "fresh_or_cross_source_route": FLOW_DISPLAY_NAMES["data_analysis"],
            "legacy_report_flow": FLOW_DISPLAY_NAMES["realtime_production_report_legacy"],
            "legacy_report_router_exposed": False,
        },
        "validation": {
            "bundle_structure": "native Chat boundaries, version stamps, Flow names, endpoints and active adapter inputs are verified while building",
            "langflow_frontend_edge_handles": f"{edge_handle_count}/{edge_handle_count} structural handles",
            "langflow_lfx_node_templates": f"{component_count}/{component_count} custom component templates across {len(flows)} base flows",
            "runtime_parse": "Run tools/validate_langflow_runtime.py after changing custom component source.",
        },
        "flows": manifest_flows,
    }
    (output_dir / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (output_dir / "README_IMPORT.md").write_bytes(_readme(manifest_flows, edge_handle_count, component_count).encode("utf-8"))

    zip_path = output_dir.parent / f"{output_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=output_dir.parent, base_dir=output_dir.name)
    return {"output_dir": str(output_dir), "zip": str(zip_path), **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the current nine-flow metadata-driven Langflow bundle.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
