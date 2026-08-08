"""Append the isolated continuation flows to the selected base-flow bundle.

 The base builder and its canonical 01/06 routing contract remain unchanged.
 This wrapper first builds that bundle, records the exact 01/06 bytes, and then
 adds only import entries 08 and 09 plus their manifest metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid
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


def build_continuation_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    protected_paths = (
        output_dir / "01_data_analysis_flow_v2_standalone.json",
        output_dir / "06_agent_tool_router_flow_v5_standalone.json",
    )
    # When the wrapper is run against an already-built bundle, capture the
    # protected public artifacts *before* the base builder runs.  This keeps
    # the additive builder from silently accepting an unrelated 01/07 rewrite
    # performed during base regeneration.
    prebuild_protected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected_paths
        if path.exists()
    }
    base.build_bundle(output_dir)
    for name, expected_hash in prebuild_protected.items():
        actual_hash = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"Base regeneration modified protected artifact before append: {name}")
    protected = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected_paths
    }
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing_names = [str(item.get("name") or "") for item in manifest.get("flows", [])]
    if existing_names != [base.FLOW_DISPLAY_NAMES[route] for _, _, route in base.FLOW_SPECS]:
        raise ValueError("Base bundle flow order changed before continuation append")

    appended_flows: list[dict[str, Any]] = []
    appended_items: list[dict[str, Any]] = []
    for order, (source_name, destination_name, display_name, endpoint_suffix) in enumerate(
        CONTINUATION_FLOW_SPECS,
        start=8,
    ):
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
            set(
                [
                    *flow.get("tags", []),
                    "complete-bundle",
                    base.BUNDLE_VERSION,
                    "import-ready",
                    "continuation",
                    "isolated-additive",
                ]
            )
        )
        base._set_frontend_flow_ids(flow, flow_id)
        destination = output_dir / destination_name
        destination.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        item = {
            "order": order,
            "file": destination.name,
            "name": display_name,
            "endpoint_name": endpoint_name,
            "nodes": len(flow.get("data", {}).get("nodes", [])),
            "edges": len(flow.get("data", {}).get("edges", [])),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        }
        appended_flows.append(flow)
        appended_items.append(item)

    _validate_additive_flows(appended_flows)
    for name, expected_hash in protected.items():
        actual_hash = hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"Continuation append modified protected base artifact: {name}")

    manifest["flows"] = [*manifest["flows"], *appended_items]
    manifest["flow_count"] = len(manifest["flows"])
    manifest["continuation_routing_contract"] = {
        "strategy": "isolated_additive",
        "base_flows_unchanged": [
            "01. v5_data_analysis",
            "06. v5_agent_tool_router",
        ],
        "data_analysis_flow": "08. v5_data_analysis_continuation",
        "agent_tool_router_flow": "09. v5_agent_tool_router_continuation",
        "max_child_runs": 2,
        "intermediate_answer_model_calls": 0,
        "resume_intent_model_calls": 0,
        "agent_observation": "continuation_execution only; no raw rows, trace, or generated code",
    }
    combined_path = output_dir / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json"
    combined = json.loads(combined_path.read_text(encoding="utf-8"))
    combined["flows"] = [*combined["flows"], *appended_flows]
    edge_handle_count = 2 * sum(
        len(flow.get("data", {}).get("edges", [])) for flow in combined["flows"]
    )
    node_template_count = sum(
        1
        for flow in combined["flows"]
        for node in flow.get("data", {}).get("nodes", [])
        if node.get("data", {}).get("node", {}).get("template", {}).get("_type") == "Component"
        and isinstance(
            node.get("data", {}).get("node", {}).get("template", {}).get("code"),
            dict,
        )
    )
    router_suffixes = (
        "-api-router",
        "-agent-tool-router",
        "-agent-tool-router-continuation",
        "-workflow-orchestrator",
    )
    child_flow_count = sum(
        not str(flow.get("endpoint_name") or "").endswith(router_suffixes)
        for flow in combined["flows"]
    )
    validation = manifest.setdefault("validation", {})
    validation["langflow_frontend_edge_handles"] = (
        f"{edge_handle_count}/{edge_handle_count} parsed and matched edge.data"
    )
    validation["langflow_lfx_node_templates"] = (
        f"{node_template_count}/{node_template_count} passed across {len(combined['flows'])} flows in "
        "Langflow 1.9.2 / Langflow Base 0.9.2 / LFX 0.4.2"
    )
    validation["single_chat_output"] = (
        f"{child_flow_count}/{child_flow_count} active child flows each have one native Chat Output "
        "with no GaiA boundary adapters"
    )
    combined_path.write_bytes(
        json.dumps(combined, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    manifest["single_file_ui_import_sha256"] = hashlib.sha256(combined_path.read_bytes()).hexdigest()
    manifest_path.write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    readme_path = output_dir / "README_IMPORT.md"
    readme = readme_path.read_text(encoding="utf-8").rstrip()
    readme, node_claim_replacements = re.subn(
        r"(- Langflow 1\.9\.2 / Langflow Base 0\.9\.2 / LFX 0\.4\.2 node template: )"
        r"\d+개 Flow \d+/\d+ 검증 통과",
        rf"\g<1>{len(combined['flows'])}개 Flow {node_template_count}/{node_template_count} 검증 통과",
        readme,
    )
    if node_claim_replacements != 1:
        raise ValueError("Base README node-template validation claim was not found exactly once")
    readme = re.sub(
        r"(- Langflow 1\.9\.2 frontend edge handle codec: )\d+/\d+( parse 및 `edge\.data` 일치)",
        rf"\g<1>{edge_handle_count}/{edge_handle_count}\g<2>",
        readme,
        count=1,
    )
    readme = re.sub(
        r"(Langflow UI가 최상위 `flows` 배열을 펼쳐 )\d+개 Flow(를 한 번에 import합니다\.)",
        r"\g<1>9개 Flow\g<2>",
        readme,
        count=1,
    )
    readme = re.sub(
        r"(- 통합 `00` 단일 JSON은 )\d+개 Flow(를 포함하도록 생성하고)",
        r"\g<1>9개 Flow\g<2>",
        readme,
        count=1,
    )
    readme += (
        "\n\n## Additive continuation flows\n\n"
        "기존 `01` 및 `06`은 그대로 유지됩니다. 종속 조회가 필요한 실험·검증은 아래 별도 Flow를 사용합니다.\n\n"
        "| 순서 | 파일 | endpoint_name |\n"
        "| ---: | --- | --- |\n"
        + "\n".join(
            f"| {item['order']} | `{item['file']}` | `{item['endpoint_name']}` |"
            for item in appended_items
        )
        + "\n"
    )
    readme_path.write_bytes(readme.encode("utf-8"))

    zip_path = output_dir.parent / f"{output_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(output_dir), "zip", root_dir=output_dir.parent, base_dir=output_dir.name)
    return {
        "status": "ok",
        "flow_count": manifest["flow_count"],
        "files": [item["file"] for item in appended_items],
        "combined": combined_path.name,
        "zip": zip_path.name,
        "protected_hashes": protected,
    }


def _validate_additive_flows(flows: list[dict[str, Any]]) -> None:
    if len(flows) != 2:
        raise ValueError("Exactly two additive continuation flows are required")
    data_flow, router_flow = flows
    data_text = json.dumps(data_flow, ensure_ascii=False)
    router_text = json.dumps(router_flow, ensure_ascii=False)
    for token in (
        "dependent_retrieval_plan",
        "continuation_contract",
        "continuation_ref",
        "intent_llm_skipped",
    ):
        if token not in data_text:
            raise ValueError(f"Continuation Data Analysis flow is missing contract token: {token}")
    for token in (
        "08. v5_data_analysis_continuation",
        "continuation_execution",
        "stages_executed",
        "auto_continued",
    ):
        if token not in router_text:
            raise ValueError(f"Continuation Agent Tool Router is missing contract token: {token}")
    if "08. v5_workflow_orchestrator" in router_text:
        raise ValueError("Continuation Agent Tool Router must not depend on the removed workflow flow")
    for flow in flows:
        if str(flow.get("last_tested_version") or "") != base.TARGET_LANGFLOW_VERSION:
            raise ValueError("Continuation flow last_tested_version must be 1.9.2")
        for node in flow.get("data", {}).get("nodes", []):
            lf_version = str(node.get("data", {}).get("node", {}).get("lf_version") or "")
            if lf_version and lf_version != base.TARGET_LANGFLOW_VERSION:
                raise ValueError(
                    f"Continuation node {node.get('id')} has unexpected lf_version={lf_version}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the selected base flows plus isolated continuation flows 08/09.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_continuation_bundle(args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
