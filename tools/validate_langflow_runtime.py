from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from typing import Any

import requests
from lfx.custom.eval import eval_custom_component_code
from lfx.custom.utils import create_component_template


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_import_ready_bundle import (  # noqa: E402
    TARGET_LANGFLOW_BASE_VERSION,
    TARGET_LANGFLOW_VERSION,
    TARGET_LFX_VERSION,
)

DEFAULT_FLOW = ROOT / "flow_exports" / "data_analysis_flow_v2_standalone.json"
DEFAULT_STOP_COMPONENT = "CustomComponent-DXrpf"
FLOW_EXPORT_ROOT = ROOT / "flow_exports"
UPGRADE_STATUS_PATTERN = re.compile(r"^\s*\[(?P<status>[A-Z_]+)\].*?\s+-\s+id:\s+(?P<id>.+?)\s*$", re.MULTILINE)


def _flow_export_paths() -> list[Path]:
    """Return the nine supported standalone exports in deterministic order."""

    return sorted(FLOW_EXPORT_ROOT.glob("*_standalone.json"))


def _is_local_custom_node(node: dict[str, Any]) -> bool:
    metadata = node.get("data", {}).get("node", {}).get("metadata", {})
    module = metadata.get("module") if isinstance(metadata, dict) else ""
    return isinstance(module, str) and module.startswith(("custom_components.", "v5_auxiliary."))


def validate_runtime_versions() -> dict[str, Any]:
    """Keep the validation result tied to the declared 1.11 package contract."""

    packages = {
        "langflow": TARGET_LANGFLOW_VERSION,
        "langflow-base": TARGET_LANGFLOW_BASE_VERSION,
        "lfx": TARGET_LFX_VERSION,
    }
    actual: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    for package, expected in packages.items():
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed = ""
        actual[package] = installed
        if installed != expected:
            errors.append({"package": package, "expected": expected, "actual": installed or "not installed"})
    return {
        "expected": packages,
        "actual": actual,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "default_python_minor": "3.13",
        "uses_default_python_minor": (sys.version_info.major, sys.version_info.minor) == (3, 13),
        "errors": errors,
    }


def validate_flow_version_contract(flow: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify flow/node stamps before checking executable component templates."""

    errors: list[dict[str, Any]] = []
    if str(flow.get("last_tested_version") or "") != TARGET_LANGFLOW_VERSION:
        errors.append(
            {
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
                    "type": "node_version_mismatch",
                    "node": str(node.get("id") or ""),
                    "expected": TARGET_LANGFLOW_VERSION,
                    "actual": component.get("lf_version"),
                }
            )
    return errors


def validate_node_templates(flow: dict[str, Any]) -> dict[str, Any]:
    passed: list[str] = []
    failures: list[dict[str, str]] = []
    for node in flow.get("data", {}).get("nodes", []):
        config = node.get("data", {}).get("node", {})
        template = config.get("template", {})
        code_field = template.get("code")
        if template.get("_type") != "Component" or not isinstance(code_field, dict):
            continue
        try:
            code = str(code_field.get("value") or "")
            component_class = eval_custom_component_code(code)
            create_component_template({"code": code, "output_types": []}, module_name="v5_runtime_validation")
            expected_inputs = [item.name for item in getattr(component_class, "inputs", [])]
            expected_outputs = [item.name for item in getattr(component_class, "outputs", [])]
            if not expected_outputs and str(node.get("data", {}).get("type") or "") == "SmartRouter":
                routes = template.get("routes", {}).get("value", [])
                expected_outputs = [f"category_{index}_result" for index, _ in enumerate(routes, start=1)]
                if bool(template.get("enable_else_output", {}).get("value")):
                    expected_outputs.append("default_result")
            serialized_inputs = list(config.get("field_order", []))
            serialized_outputs = [item.get("name") for item in config.get("outputs", [])]
            if expected_inputs != serialized_inputs:
                raise ValueError(f"input mismatch: {expected_inputs} != {serialized_inputs}")
            if expected_outputs != serialized_outputs:
                raise ValueError(f"output mismatch: {expected_outputs} != {serialized_outputs}")
            passed.append(str(node.get("id") or ""))
        except Exception as exc:
            failures.append({"id": str(node.get("id") or ""), "error": f"{type(exc).__name__}: {exc}"})
    return {"checked": len(passed) + len(failures), "passed": len(passed), "failed": len(failures), "failures": failures}


def validate_lfx_upgrade(flow_path: Path, flow: dict[str, Any]) -> dict[str, Any]:
    """Audit native upgrade status while keeping standalone custom code on its own parser path.

    LFX cannot infer a migration path for an embedded standalone component, so it
    reports those nodes as ``BLOCKED`` even after their source parses correctly.
    Treating that expected state as a global failure would make the 1.11 upgrade
    check unusable. Native ``SAFE``/``BLOCKED`` states remain failures because
    they mean a built-in component still needs migration work.
    """

    custom_node_ids = {
        str(node.get("id") or "")
        for node in flow.get("data", {}).get("nodes", [])
        if _is_local_custom_node(node)
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "lfx", "upgrade", str(flow_path), "--strict"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except Exception as exc:
        return {"checked": False, "errors": [{"type": "lfx_upgrade_command_failed", "message": str(exc)}]}

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    statuses = [match.groupdict() for match in UPGRADE_STATUS_PATTERN.finditer(output)]
    errors: list[dict[str, Any]] = []
    ignored_custom_blocks: list[str] = []
    for item in statuses:
        status = item["status"]
        node_id = item["id"]
        if status == "BLOCKED" and node_id in custom_node_ids:
            ignored_custom_blocks.append(node_id)
        elif status in {"SAFE", "BLOCKED"}:
            errors.append({"type": "native_component_upgrade_required", "status": status, "node": node_id})
        elif status != "OK":
            errors.append({"type": "unknown_lfx_upgrade_status", "status": status, "node": node_id})
    if not statuses:
        errors.append(
            {
                "type": "lfx_upgrade_no_status",
                "returncode": completed.returncode,
                "output": output[-2000:],
            }
        )
    elif completed.returncode not in {0, 1}:
        errors.append(
            {
                "type": "lfx_upgrade_unexpected_exit",
                "returncode": completed.returncode,
                "output": output[-2000:],
            }
        )
    return {
        "checked": True,
        "returncode": completed.returncode,
        "status_count": len(statuses),
        "ignored_custom_blocked": sorted(ignored_custom_blocks),
        "errors": errors,
    }


def import_and_partial_build(
    flow_path: Path,
    server_url: str,
    partial_build: bool,
    stop_component_id: str,
) -> dict[str, Any]:
    base = server_url.rstrip("/") + "/api/v1"
    session = requests.Session()
    headers: dict[str, str] = {}
    api_key = os.getenv("LANGFLOW_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    else:
        try:
            auth_response = session.get(base + "/auto_login", timeout=30)
            auth_response.raise_for_status()
            token = str(auth_response.json().get("access_token") or "")
        except requests.RequestException as exc:
            raise RuntimeError(
                "Langflow 1.11에서는 auto-login이 기본적으로 보장되지 않습니다. "
                "LANGFLOW_API_KEY를 설정하거나 명시적으로 auto-login을 허용한 로컬 서버를 사용하세요."
            ) from exc
        if not token:
            raise RuntimeError(
                "LANGFLOW_API_KEY가 필요합니다. auto-login 토큰이 없으면 1.11 서버의 인증 설정을 확인하세요."
            )
        headers["Authorization"] = f"Bearer {token}"

    with flow_path.open("rb") as flow_file:
        response = session.post(
            base + "/flows/upload/",
            headers=headers,
            files={"file": (flow_path.name, flow_file, "application/json")},
            timeout=240,
        )
    response.raise_for_status()
    imported_value = response.json()
    imported = imported_value[-1] if isinstance(imported_value, list) else imported_value
    result: dict[str, Any] = {
        "upload_status": response.status_code,
        "flow_id": imported.get("id"),
        "flow_name": imported.get("name"),
        "nodes": len(imported.get("data", {}).get("nodes", [])),
        "edges": len(imported.get("data", {}).get("edges", [])),
    }
    if not partial_build:
        return result

    flow_id = str(imported.get("id") or "")
    build_response = session.post(
        f"{base}/build/{flow_id}/flow",
        headers={**headers, "Content-Type": "application/json"},
        params={"stop_component_id": stop_component_id, "event_delivery": "direct", "log_builds": "true"},
        json={
            "inputs": {
                "input_value": "오늘 DA공정 WIP 알려줘",
                "session": "metadata-driven-v5-runtime-validation",
                "type": "chat",
            }
        },
        timeout=300,
    )
    build_response.raise_for_status()
    vertices: list[dict[str, Any]] = []
    for line in build_response.text.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("event") != "end_vertex":
            continue
        build_data = event.get("data", {}).get("build_data", {})
        vertices.append(
            {
                "id": build_data.get("id"),
                "valid": build_data.get("valid"),
                "duration": build_data.get("data", {}).get("duration"),
                "error": None if build_data.get("valid") else build_data.get("params"),
            }
        )
    result["partial_build"] = {
        "stop_component_id": stop_component_id,
        "vertices": vertices,
        "passed": bool(vertices)
        and all(vertex.get("valid") is True for vertex in vertices)
        and any(vertex.get("id") == stop_component_id for vertex in vertices),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the v5 export with the actual Langflow/LFX runtime.")
    parser.add_argument("--flow", type=Path, default=DEFAULT_FLOW)
    parser.add_argument("--all-flows", action="store_true", help="Validate all supported standalone exports instead of one flow.")
    parser.add_argument("--skip-lfx-upgrade", action="store_true", help="Skip the native LFX upgrade compatibility audit.")
    parser.add_argument("--server-url", default="", help="Optional running Langflow URL, for example http://127.0.0.1:7867")
    parser.add_argument("--partial-build", action="store_true", help="After import, run through the metadata candidate node.")
    parser.add_argument("--stop-component-id", default=DEFAULT_STOP_COMPONENT)
    args = parser.parse_args()

    if args.server_url and args.all_flows:
        parser.error("--server-url은 단일 --flow 검증에서만 사용할 수 있습니다.")
    flow_paths = _flow_export_paths() if args.all_flows else [args.flow]
    if not flow_paths:
        parser.error("검증할 standalone Flow export를 찾을 수 없습니다.")

    flow_results: list[dict[str, Any]] = []
    for flow_path in flow_paths:
        flow = json.loads(flow_path.read_text(encoding="utf-8"))
        flow_result: dict[str, Any] = {
            "flow": str(flow_path),
            "version_contract_errors": validate_flow_version_contract(flow),
            "node_templates": validate_node_templates(flow),
        }
        if not args.skip_lfx_upgrade:
            flow_result["lfx_upgrade"] = validate_lfx_upgrade(flow_path, flow)
        flow_results.append(flow_result)

    result: dict[str, Any] = {
        "runtime": validate_runtime_versions(),
        "flows": flow_results,
    }
    if args.server_url:
        result["server"] = import_and_partial_build(
            args.flow,
            args.server_url,
            args.partial_build,
            args.stop_component_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    failed = bool(result["runtime"]["errors"])
    for flow_result in flow_results:
        failed = failed or bool(flow_result["version_contract_errors"])
        failed = failed or flow_result["node_templates"]["failed"] > 0
        upgrade = flow_result.get("lfx_upgrade")
        if isinstance(upgrade, dict) and upgrade.get("errors"):
            failed = True
    partial = result.get("server", {}).get("partial_build")
    if isinstance(partial, dict) and not partial.get("passed"):
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
