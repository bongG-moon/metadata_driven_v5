from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any

import requests
from lfx.custom.eval import eval_custom_component_code
from lfx.custom.utils import create_component_template


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLOW = ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json"
DEFAULT_STOP_COMPONENT = "CustomComponent-DXrpf"


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
        auth_response = session.get(base + "/auto_login", timeout=30)
        auth_response.raise_for_status()
        token = str(auth_response.json().get("access_token") or "")
        if not token:
            raise RuntimeError("LANGFLOW_API_KEY 또는 auto_login access token이 필요합니다.")
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
    parser.add_argument("--server-url", default="", help="Optional running Langflow URL, for example http://127.0.0.1:7867")
    parser.add_argument("--partial-build", action="store_true", help="After import, run through the metadata candidate node.")
    parser.add_argument("--stop-component-id", default=DEFAULT_STOP_COMPONENT)
    args = parser.parse_args()

    flow = json.loads(args.flow.read_text(encoding="utf-8"))
    result: dict[str, Any] = {
        "langflow_version": importlib.metadata.version("langflow"),
        "lfx_version": importlib.metadata.version("lfx"),
        "flow": str(args.flow),
        "node_templates": validate_node_templates(flow),
    }
    if args.server_url:
        result["server"] = import_and_partial_build(
            args.flow,
            args.server_url,
            args.partial_build,
            args.stop_component_id,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    failed = result["node_templates"]["failed"] > 0
    partial = result.get("server", {}).get("partial_build")
    if isinstance(partial, dict) and not partial.get("passed"):
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
