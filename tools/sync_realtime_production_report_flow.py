"""Synchronize only Flow 07 after changing its standalone component source.

The general import-bundle builder rewrites every base Flow and deletes/rebuilds
the individual imports.  This targeted tool intentionally updates only Flow 07
and its entry in the combined import file, manifest, and ZIP.  It preserves
unrelated in-progress changes in the other Flow artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

import build_import_ready_bundle as bundle
import build_v5_auxiliary_flows as auxiliary


ROOT = Path(__file__).resolve().parents[1]
EXPORT_PATH = ROOT / "flow_exports" / "07_realtime_production_report_flow_v5_standalone.json"
IMPORT_DIR = ROOT / "import_ready_flows"
IMPORT_PATH = IMPORT_DIR / "07_realtime_production_report_flow_v5_standalone.json"
COMBINED_PATH = IMPORT_DIR / f"00_metadata_driven_v5_complete_{bundle.BUNDLE_VERSION}_ALL_FLOWS.json"
MANIFEST_PATH = IMPORT_DIR / "manifest.json"
ROUTE_NAME = "realtime_production_report"
ENDPOINT_SUFFIX = "realtime-production-report"
ENDPOINT_NAME = f"{bundle.ENDPOINT_PREFIX}-{ENDPOINT_SUFFIX}"
FLOW_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{bundle.ENDPOINT_PREFIX}/{ENDPOINT_SUFFIX}"))


def _write_pretty_json(path: Path, value: Any) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _write_compact_json(path: Path, value: Any) -> None:
    path.write_bytes(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _node_count(flow: dict[str, Any]) -> int:
    return len(flow.get("data", {}).get("nodes", []))


def _edge_count(flow: dict[str, Any]) -> int:
    return len(flow.get("data", {}).get("edges", []))


def _assert_target_version(flow: dict[str, Any]) -> None:
    if flow.get("last_tested_version") != bundle.TARGET_LANGFLOW_VERSION:
        raise ValueError("Flow 07 last_tested_version is not Langflow 1.9.2.")
    for node in flow.get("data", {}).get("nodes", []):
        component = node.get("data", {}).get("node")
        if isinstance(component, dict) and component.get("lf_version") != bundle.TARGET_LANGFLOW_VERSION:
            raise ValueError(f"Flow 07 node version mismatch: {node.get('id')}")


def _replace_combined_flow(import_flow: dict[str, Any]) -> int:
    combined = json.loads(COMBINED_PATH.read_text(encoding="utf-8"))
    flows = combined.get("flows")
    if not isinstance(flows, list):
        raise ValueError(f"Combined import file has no flows array: {COMBINED_PATH}")
    target_indexes = [
        index
        for index, candidate in enumerate(flows)
        if isinstance(candidate, dict)
        and (
            candidate.get("endpoint_name") == ENDPOINT_NAME
            or candidate.get("name") == bundle.FLOW_DISPLAY_NAMES[ROUTE_NAME]
        )
    ]
    if len(target_indexes) != 1:
        raise ValueError(f"Expected one Flow 07 in combined import file, found {len(target_indexes)}.")
    flows[target_indexes[0]] = deepcopy(import_flow)
    _write_compact_json(COMBINED_PATH, combined)
    return len(flows)


def _update_manifest(import_flow: dict[str, Any], combined_flow_count: int) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    flow_items = manifest.get("flows")
    if not isinstance(flow_items, list):
        raise ValueError(f"Manifest has no flows array: {MANIFEST_PATH}")
    target_items = [
        item
        for item in flow_items
        if isinstance(item, dict)
        and (
            item.get("endpoint_name") == ENDPOINT_NAME
            or item.get("file") == IMPORT_PATH.name
        )
    ]
    if len(target_items) != 1:
        raise ValueError(f"Expected one Flow 07 manifest item, found {len(target_items)}.")
    target = target_items[0]
    target.update(
        {
            "file": IMPORT_PATH.name,
            "name": bundle.FLOW_DISPLAY_NAMES[ROUTE_NAME],
            "endpoint_name": ENDPOINT_NAME,
            "nodes": _node_count(import_flow),
            "edges": _edge_count(import_flow),
            "sha256": hashlib.sha256(IMPORT_PATH.read_bytes()).hexdigest(),
        }
    )
    manifest["flow_count"] = len(flow_items)
    if manifest["flow_count"] != combined_flow_count:
        raise ValueError(
            "Manifest and combined import Flow counts differ; refusing to rewrite the manifest."
        )
    manifest["single_file_ui_import"] = COMBINED_PATH.name
    manifest["single_file_ui_import_sha256"] = hashlib.sha256(COMBINED_PATH.read_bytes()).hexdigest()
    _write_pretty_json(MANIFEST_PATH, manifest)


def _refresh_zip() -> Path:
    zip_path = IMPORT_DIR.parent / f"{IMPORT_DIR.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(
        str(zip_path.with_suffix("")),
        "zip",
        root_dir=IMPORT_DIR.parent,
        base_dir=IMPORT_DIR.name,
    )
    return zip_path


def sync(*, refresh_zip: bool = True) -> dict[str, Any]:
    """Regenerate source/export/import representations for the Flow 07 only."""
    donor = auxiliary.load_donor()
    export_flow = auxiliary._stamp_flow_version(
        auxiliary.build_realtime_production_report_flow(donor)
    )
    _assert_target_version(export_flow)
    _write_pretty_json(EXPORT_PATH, export_flow)

    import_flow = bundle._stamp_flow(
        deepcopy(export_flow),
        flow_id=FLOW_ID,
        route_name=ROUTE_NAME,
        endpoint_name=ENDPOINT_NAME,
    )
    _assert_target_version(import_flow)
    _write_pretty_json(IMPORT_PATH, import_flow)
    combined_flow_count = _replace_combined_flow(import_flow)
    _update_manifest(import_flow, combined_flow_count)

    zip_path = _refresh_zip() if refresh_zip else None
    return {
        "export": str(EXPORT_PATH),
        "import": str(IMPORT_PATH),
        "combined": str(COMBINED_PATH),
        "manifest": str(MANIFEST_PATH),
        "zip": str(zip_path) if zip_path else "",
        "nodes": _node_count(import_flow),
        "edges": _edge_count(import_flow),
        "endpoint_name": ENDPOINT_NAME,
        "report_api_default": "http://127.0.0.1:5000",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize the single MongoDB collection Report API update into Flow 07 only."
    )
    parser.add_argument("--no-zip", action="store_true", help="Do not refresh import_ready_flows.zip.")
    args = parser.parse_args()
    print(json.dumps(sync(refresh_zip=not args.no_zip), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
