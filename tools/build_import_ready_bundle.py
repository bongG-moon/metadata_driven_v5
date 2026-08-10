"""Build the import-ready bundle for the currently supported Langflow flows.

The repository intentionally keeps this builder small: it only knows about the
seven base flows that are present in the import-ready bundle.  The isolated
two-stage continuation flows are appended by
``build_continuation_import_ready_bundle.py``.
"""

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
TARGET_LANGFLOW_VERSION = "1.9.2"
TARGET_LANGFLOW_BASE_VERSION = "0.9.2"
TARGET_LFX_VERSION = "0.4.2"
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
    ("07_realtime_production_report_flow_v5_standalone.json", "realtime-production-report", "realtime_production_report"),
]
IMPORT_ORDER = {route_name: index for index, (_, _, route_name) in enumerate(FLOW_SPECS, start=1)}
FLOW_DISPLAY_NAMES = {
    "data_analysis": "01. v5_data_analysis",
    "domain_saving": "02. v5_domain_saving",
    "table_catalog_saving": "03. v5_table_catalog_saving",
    "main_flow_filter_saving": "04. v5_main_flow_filter_saving",
    "metadata_qa": "05. v5_metadata_qa",
    "agent_tool_router": "06. v5_agent_tool_router",
    "realtime_production_report": "07. v5_realtime_production_report",
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


def _destination_name(source_name: str, order: int) -> str:
    return source_name if source_name.startswith(f"{order:02d}_") else f"{order:02d}_{source_name}"


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
            ("Agent-agent-tool-router", "ChatOutput-agent-tool-router"),
        }
        if not required.issubset(edge_pairs):
            raise ValueError("Agent Tool Router must keep direct native Chat Input -> Agent -> Chat Output edges.")
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
        f"| {item['order']} | `{item['file']}` | `{item['endpoint_name']}` | {item['nodes']} | {item['edges']} |"
        for item in manifest_flows
    )
    return f"""# metadata_driven_v5 import-ready bundle

이 bundle은 현재 지원하는 **7개 기본 Flow**만 포함합니다. 모두 Langflow {TARGET_LANGFLOW_VERSION} / langflow-base {TARGET_LANGFLOW_BASE_VERSION} / LFX {TARGET_LFX_VERSION} 기준으로 생성되었습니다.

## Import

Langflow Desktop에서 `00_metadata_driven_v5_complete_{BUNDLE_VERSION}_ALL_FLOWS.json` 하나를 import하거나, 아래 순서대로 개별 파일을 import합니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
{rows}

`08`과 `09`의 종속 조회 continuation Flow는 `python tools\\build_continuation_import_ready_bundle.py`로 추가 생성됩니다.

## 운영 설정

- Langflow 모델 Provider와 `MONGO_URL` Credential Global Variable을 import 후 설정합니다.
- 기본 Data Analysis는 `01. v5_data_analysis`입니다. 명시적인 상위 결과 참조가 필요한 2단계 분석만 `08. v5_data_analysis_continuation`을 사용합니다.
- 결과 CSV/JSON 다운로드와 실시간 Report HTML 발행은 API_SERVER(`python API_SERVER\\app.py`, bind `0.0.0.0:5000`)가 담당합니다. Flow 07 Report HTML과 메타데이터는 API_SERVER의 단일 MongoDB 컬렉션에 저장되므로 Flow의 Report API 주소를 접근 가능한 API URL로 설정합니다.
- 기존 Router Tool에 저장된 `flow_id_selected`가 있으면, import 뒤 대상 Flow를 한 번 다시 선택해 현재 Flow ID로 갱신합니다.

## 생성 시 구조 검증

- GaiA Input/Output boundary node 없음
- 각 Flow의 native Chat Input/Chat Output 각각 1개
- 모든 node `lf_version={TARGET_LANGFLOW_VERSION}`
- edge handle {edge_handle_count}/{edge_handle_count}, custom component template {component_count}/{component_count}
"""


def build_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    """Generate the seven current base import artifacts and their manifest."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in output_dir.glob("[0-9][0-9]_*_standalone.json"):
        stale_path.unlink()

    manifest_flows: list[dict[str, Any]] = []
    for source_name, endpoint_suffix, route_name in FLOW_SPECS:
        order = IMPORT_ORDER[route_name]
        source = SOURCE_DIR / source_name
        if not source.exists():
            raise FileNotFoundError(f"Active flow export is missing: {source}")
        flow = json.loads(source.read_text(encoding="utf-8"))
        endpoint_name = f"{ENDPOINT_PREFIX}-{endpoint_suffix}"
        flow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ENDPOINT_PREFIX}/{endpoint_suffix}"))
        _stamp_flow(flow, flow_id=flow_id, route_name=route_name, endpoint_name=endpoint_name)
        destination = output_dir / _destination_name(source_name, order)
        destination.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        manifest_flows.append(
            {
                "order": order,
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
    parser = argparse.ArgumentParser(description="Build the current seven-flow metadata-driven Langflow bundle.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
