from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_import_ready_bundle import TARGET_LANGFLOW_VERSION  # noqa: E402

COMPONENT_ROOT = ROOT / "langflow_components"
FLOW_EXPORT_ROOT = ROOT / "flow_exports"
IMPORT_READY_ROOT = ROOT / "import_ready_flows"
COMBINED_IMPORT = IMPORT_READY_ROOT / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json"
CUSTOM_MODULE_PREFIXES = ("custom_components.", "v5_auxiliary.")
EXPECTED_FLOW_COUNT = 9
SUPPORT_SOURCE_FILES = {
    "langflow_components/data_analysis_flow/function_case_helper_code_input_example.py",
    # V2 Node 20 now owns lazy AnswerEvidence rendering so this generated
    # compatibility component remains importable but is intentionally not on
    # the active graph.
    "langflow_components/data_analysis_flow_v2/18_route_aware_answer_prompt_builder.py",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_index() -> tuple[dict[str, list[Path]], list[Path]]:
    by_code: dict[str, list[Path]] = defaultdict(list)
    paths = sorted(path for path in COMPONENT_ROOT.rglob("*.py") if "__pycache__" not in path.parts)
    for path in paths:
        by_code[path.read_text(encoding="utf-8")].append(path)
    return dict(by_code), paths


def _is_local_custom_node(node: dict[str, Any]) -> bool:
    module = node.get("data", {}).get("node", {}).get("metadata", {}).get("module", "")
    return isinstance(module, str) and module.startswith(CUSTOM_MODULE_PREFIXES)


def _custom_nodes(flow: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for node in flow.get("data", {}).get("nodes", []) if _is_local_custom_node(node)]


def _flow_key(flow: dict[str, Any]) -> str:
    return str(flow.get("endpoint_name") or flow.get("name") or flow.get("id") or "unknown")


def _version_contract_errors(label: str, flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verify the 1.11 version stamp on every exported/importable component."""

    errors: list[dict[str, Any]] = []
    for flow in flows:
        flow_key = _flow_key(flow)
        if str(flow.get("last_tested_version") or "") != TARGET_LANGFLOW_VERSION:
            errors.append(
                {
                    "flow": flow_key,
                    "artifact": label,
                    "type": "flow_version_mismatch",
                    "expected": TARGET_LANGFLOW_VERSION,
                    "actual": flow.get("last_tested_version"),
                }
            )
        for node in flow.get("data", {}).get("nodes", []):
            component = node.get("data", {}).get("node")
            if not isinstance(component, dict):
                continue
            if str(component.get("lf_version") or "") != TARGET_LANGFLOW_VERSION:
                errors.append(
                    {
                        "flow": flow_key,
                        "artifact": label,
                        "node": str(node.get("id") or ""),
                        "type": "node_version_mismatch",
                        "expected": TARGET_LANGFLOW_VERSION,
                        "actual": component.get("lf_version"),
                    }
                )
    return errors


def _audit_flows(label: str, flows: list[dict[str, Any]], source_by_code: dict[str, list[Path]]) -> dict[str, Any]:
    errors = _version_contract_errors(label, flows)
    source_paths: set[str] = set()
    node_count = 0
    # Keep one mapping entry per artifact.  Flow exports may intentionally
    # share the same public endpoint/name (for example a canonical flow and
    # its immutable donor), so a dict keyed by that public identity would
    # silently discard one of them before parity comparison.
    mapping: list[dict[str, str]] = []

    for flow in flows:
        flow_mapping: dict[str, str] = {}
        for node in _custom_nodes(flow):
            node_count += 1
            node_id = str(node.get("id", ""))
            component = node["data"]["node"]
            code = component.get("template", {}).get("code", {}).get("value")
            if not isinstance(code, str) or not code.strip():
                errors.append({"flow": _flow_key(flow), "node": node_id, "type": "missing_embedded_code"})
                continue
            candidates = source_by_code.get(code, [])
            if len(candidates) != 1:
                errors.append(
                    {
                        "flow": _flow_key(flow),
                        "node": node_id,
                        "type": "missing_source" if not candidates else "ambiguous_source",
                        "candidates": [path.relative_to(ROOT).as_posix() for path in candidates],
                    }
                )
                continue
            relative = candidates[0].relative_to(ROOT).as_posix()
            source_paths.add(relative)
            flow_mapping[node_id] = relative
            expected_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
            actual_hash = component.get("metadata", {}).get("code_hash")
            if actual_hash != expected_hash:
                errors.append(
                    {
                        "flow": _flow_key(flow),
                        "node": node_id,
                        "type": "code_hash_mismatch",
                        "expected": expected_hash,
                        "actual": actual_hash,
                    }
                )
        mapping.append(flow_mapping)

    if len(flows) != EXPECTED_FLOW_COUNT:
        errors.append({"type": "flow_count_mismatch", "expected": EXPECTED_FLOW_COUNT, "actual": len(flows)})
    return {
        "label": label,
        "flow_count": len(flows),
        "custom_node_instances": node_count,
        "unique_source_files": len(source_paths),
        "source_paths": sorted(source_paths),
        "mapping": mapping,
        "errors": errors,
    }


def _individual_import_flows() -> list[dict[str, Any]]:
    paths = sorted(
        {
            *IMPORT_READY_ROOT.glob("[0-9][0-9]_*_v5_standalone.json"),
            *IMPORT_READY_ROOT.glob("[0-9][0-9]_*_v2_standalone.json"),
        }
    )
    return [_load_json(path) for path in paths]


def _combined_import_flows() -> list[dict[str, Any]]:
    payload = _load_json(COMBINED_IMPORT)
    flows = payload.get("flows") if isinstance(payload, dict) else None
    if not isinstance(flows, list):
        raise ValueError(f"combined import does not contain a flows list: {COMBINED_IMPORT}")
    return flows


def _flow_exports() -> list[dict[str, Any]]:
    paths = sorted(
        {
            *FLOW_EXPORT_ROOT.glob("*_v5_standalone.json"),
            *FLOW_EXPORT_ROOT.glob("*_v2_standalone.json"),
        }
    )
    return [_load_json(path) for path in paths]


def audit_repository() -> dict[str, Any]:
    source_by_code, all_source_paths = _source_index()
    reports = [
        _audit_flows("flow_exports", _flow_exports(), source_by_code),
        _audit_flows("import_ready_individual", _individual_import_flows(), source_by_code),
        _audit_flows("import_ready_bundle", _combined_import_flows(), source_by_code),
    ]
    baseline = reports[0]
    parity_errors: list[dict[str, Any]] = []
    baseline_signatures = sorted(json.dumps(item, sort_keys=True) for item in baseline["mapping"])
    for report in reports[1:]:
        report_signatures = sorted(json.dumps(item, sort_keys=True) for item in report["mapping"])
        if report_signatures != baseline_signatures:
            parity_errors.append({"type": "artifact_mapping_mismatch", "left": baseline["label"], "right": report["label"]})

    used = set(baseline["source_paths"])
    all_relative = {path.relative_to(ROOT).as_posix() for path in all_source_paths}
    support_sources = sorted(all_relative.intersection(SUPPORT_SOURCE_FILES))
    route_errors: list[dict[str, Any]] = []
    expected_route_sources = {
        "langflow_components/route_flow_v2/01_cached_named_run_flow_tool.py",
        "langflow_components/route_flow_v2/02_agent_direct_tool_result_adapter.py",
        "langflow_components/route_flow_v2/03_silent_direct_return_router_agent.py",
    }
    for path in sorted(expected_route_sources):
        if path not in used:
            route_errors.append({"type": "route_source_not_used", "path": path})
    for folder in ("router_flow", "router_flow_v2", "router_flow_v3", "router_tool_flow", "route_flow_v3"):
        if (COMPONENT_ROOT / folder).exists():
            route_errors.append({"type": "obsolete_route_folder_present", "path": f"langflow_components/{folder}"})

    inactive_sources = sorted(all_relative - used - set(support_sources))
    inactive_errors = (
        [{"type": "inactive_component_sources", "paths": inactive_sources}] if inactive_sources else []
    )
    errors = [error for report in reports for error in report["errors"]] + parity_errors + route_errors + inactive_errors
    return {
        "status": "ok" if not errors else "error",
        "reports": [
            {key: value for key, value in report.items() if key not in {"mapping", "source_paths"}}
            for report in reports
        ],
        "active_unique_source_files": len(used),
        "all_component_python_files": len(all_relative),
        "support_source_files": support_sources,
        "inactive_source_files": inactive_sources,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate that every exported local custom node has one real .py source.")
    parser.parse_args()
    result = audit_repository()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
