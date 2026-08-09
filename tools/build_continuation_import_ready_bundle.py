"""Append the optional two-stage continuation flows to the base bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_import_ready_bundle as base  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / "import_ready_flows"
CONTINUATION_FLOW_SPECS = [
    (
        "08_data_analysis_flow_v2_continuation_standalone.json",
        "08_data_analysis_flow_v2_continuation_standalone.json",
        "08. v5_data_analysis_continuation",
        "data-analysis-continuation",
    ),
    (
        "09_agent_tool_router_continuation_flow_v5_standalone.json",
        "09_agent_tool_router_continuation_flow_v5_standalone.json",
        "09. v5_agent_tool_router_continuation",
        "agent-tool-router-continuation",
    ),
]


def _custom_component_count(flows: list[dict[str, Any]]) -> int:
    return sum(
        1
        for flow in flows
        for node in flow.get("data", {}).get("nodes", [])
        if node.get("data", {}).get("node", {}).get("template", {}).get("_type") == "Component"
        and isinstance(node.get("data", {}).get("node", {}).get("template", {}).get("code"), dict)
    )


def _validate_additive_flows(flows: list[dict[str, Any]]) -> None:
    if len(flows) != 2:
        raise ValueError("Exactly two continuation flows are required.")
    data_flow, router_flow = flows
    data_text = json.dumps(data_flow, ensure_ascii=False)
    router_text = json.dumps(router_flow, ensure_ascii=False)
    for token in ("dependent_retrieval_plan", "continuation_contract", "continuation_ref", "intent_llm_skipped"):
        if token not in data_text:
            raise ValueError(f"Continuation Data Analysis flow is missing contract token: {token}")
    for token in ("08. v5_data_analysis_continuation", "continuation_execution", "stages_executed", "auto_continued"):
        if token not in router_text:
            raise ValueError(f"Continuation Agent Tool Router is missing contract token: {token}")
    for flow in flows:
        nodes = flow.get("data", {}).get("nodes", [])
        if str(flow.get("last_tested_version") or "") != base.TARGET_LANGFLOW_VERSION:
            raise ValueError("Continuation Flow version must be Langflow 1.9.2.")
        if any("GaiA" in str(node.get("data", {}).get("type") or "") for node in nodes):
            raise ValueError("Continuation Flow must not contain a GaiA boundary node.")
        if sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) != 1:
            raise ValueError("Continuation Flow must contain exactly one native Chat Input.")
        if sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes) != 1:
            raise ValueError("Continuation Flow must contain exactly one native Chat Output.")
        for node in nodes:
            component = node.get("data", {}).get("node", {})
            if isinstance(component, dict) and component.get("lf_version") != base.TARGET_LANGFLOW_VERSION:
                raise ValueError(f"Continuation node version mismatch: {node.get('id')}")


def _render_readme(
    output_dir: Path,
    manifest_flows: list[dict[str, Any]],
    edge_handle_count: int,
    component_count: int,
) -> str:
    rows = "\n".join(
        f"| {item['order']} | `{item['file']}` | `{item['endpoint_name']}` | {item['nodes']} | {item['edges']} |"
        for item in manifest_flows
    )
    return f"""# metadata_driven_v5 import-ready bundle

이 bundle에는 현재 지원하는 **{len(manifest_flows)}개 Flow**가 모두 포함되어 있습니다. 모든 Flow는 Langflow {base.TARGET_LANGFLOW_VERSION} / langflow-base {base.TARGET_LANGFLOW_BASE_VERSION} / LFX {base.TARGET_LFX_VERSION} 기준으로 생성되었습니다.

## Import

Langflow Desktop에서 `00_metadata_driven_v5_complete_{base.BUNDLE_VERSION}_ALL_FLOWS.json` 하나를 import하거나, 아래 순서대로 개별 파일을 import합니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
{rows}

## 실행 범위

- 기본 분석은 `01. v5_data_analysis`입니다.
- 현재 분석 결과를 다음 조회 조건으로 넘기는 경우에만 `08. v5_data_analysis_continuation`과 `09. v5_agent_tool_router_continuation`을 사용합니다.
- `06. v5_agent_tool_router`와 `09. v5_agent_tool_router_continuation`에 이전 import의 `flow_id_selected`가 남아 있다면 현재 Flow를 다시 선택합니다.
- CSV/JSON 다운로드와 Flow 07 HTML Report 발행은 Artifact Server(`python -m artifact_server`, 기본 `127.0.0.1:8765`)가 담당합니다.

## 생성 및 구조 검증

- GaiA Input/Output boundary node 없음; 각 Flow는 native Chat Input/Chat Output을 각각 하나씩 사용
- 모든 node `lf_version={base.TARGET_LANGFLOW_VERSION}`
- edge handle {edge_handle_count}/{edge_handle_count}
- custom component template {component_count}/{component_count}
"""


def build_continuation_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    """Regenerate the seven base flows, then add current continuation exports."""
    output_dir = output_dir.resolve()
    base.build_bundle(output_dir)
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_names = [base.FLOW_DISPLAY_NAMES[route] for _, _, route in base.FLOW_SPECS]
    if [item.get("name") for item in manifest.get("flows", [])] != expected_names:
        raise ValueError("Base bundle flow order does not match the supported Flow set.")

    protected_paths = (
        output_dir / "01_data_analysis_flow_v2_standalone.json",
        output_dir / "06_agent_tool_router_flow_v5_standalone.json",
    )
    protected_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected_paths}

    appended_flows: list[dict[str, Any]] = []
    appended_items: list[dict[str, Any]] = []
    for order, (source_name, destination_name, display_name, endpoint_suffix) in enumerate(CONTINUATION_FLOW_SPECS, start=8):
        source = base.SOURCE_DIR / source_name
        if not source.exists():
            raise FileNotFoundError(f"Continuation flow export is missing: {source}")
        flow = json.loads(source.read_text(encoding="utf-8"))
        flow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{base.ENDPOINT_PREFIX}/{endpoint_suffix}"))
        endpoint_name = f"{base.ENDPOINT_PREFIX}-{endpoint_suffix}"
        flow["id"] = flow_id
        flow["name"] = display_name
        flow["endpoint_name"] = endpoint_name
        flow["last_tested_version"] = base.TARGET_LANGFLOW_VERSION
        flow["tags"] = sorted(
            set([*flow.get("tags", []), "complete-bundle", base.BUNDLE_VERSION, "import-ready", "continuation"])
        )
        base._set_frontend_flow_ids(flow, flow_id)
        destination = output_dir / destination_name
        destination.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        appended_flows.append(flow)
        appended_items.append(
            {
                "order": order,
                "file": destination.name,
                "name": display_name,
                "endpoint_name": endpoint_name,
                "nodes": len(flow.get("data", {}).get("nodes", [])),
                "edges": len(flow.get("data", {}).get("edges", [])),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )

    _validate_additive_flows(appended_flows)
    for name, expected in protected_hashes.items():
        if hashlib.sha256((output_dir / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Continuation append modified base artifact: {name}")

    manifest_flows = [*manifest["flows"], *appended_items]
    all_flows = [
        json.loads((output_dir / item["file"]).read_text(encoding="utf-8"))
        for item in manifest_flows
    ]
    combined_path = output_dir / f"00_metadata_driven_v5_complete_{base.BUNDLE_VERSION}_ALL_FLOWS.json"
    combined_path.write_bytes(json.dumps({"flows": all_flows}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    edge_handle_count = 2 * sum(len(flow.get("data", {}).get("edges", [])) for flow in all_flows)
    component_count = _custom_component_count(all_flows)

    manifest["flows"] = manifest_flows
    manifest["flow_count"] = len(manifest_flows)
    manifest["single_file_ui_import_sha256"] = hashlib.sha256(combined_path.read_bytes()).hexdigest()
    manifest["continuation_routing_contract"] = {
        "strategy": "isolated_optional_two_stage",
        "base_flows": ["01. v5_data_analysis", "06. v5_agent_tool_router"],
        "base_flows_unchanged": ["01. v5_data_analysis", "06. v5_agent_tool_router"],
        "data_analysis_flow": "08. v5_data_analysis_continuation",
        "agent_tool_router_flow": "09. v5_agent_tool_router_continuation",
        "max_child_runs": 2,
        "intermediate_answer_model_calls": 0,
        "resume_intent_model_calls": 0,
        "agent_observation": "compact continuation status only; no raw rows, trace, or generated code",
    }
    validation = manifest.setdefault("validation", {})
    validation["langflow_frontend_edge_handles"] = f"{edge_handle_count}/{edge_handle_count} structural handles"
    validation["langflow_lfx_node_templates"] = (
        f"{component_count}/{component_count} custom component templates across {len(all_flows)} flows"
    )
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (output_dir / "README_IMPORT.md").write_bytes(
        _render_readme(output_dir, manifest_flows, edge_handle_count, component_count).encode("utf-8")
    )

    zip_path = output_dir.parent / f"{output_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=output_dir.parent, base_dir=output_dir.name)
    return {
        "status": "ok",
        "flow_count": len(manifest_flows),
        "files": [item["file"] for item in appended_items],
        "combined": combined_path.name,
        "zip": zip_path.name,
        "protected_hashes": protected_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the current nine-flow bundle with optional continuation support.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_continuation_bundle(args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
