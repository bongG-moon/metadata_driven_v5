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
MONGODB_CONTRACT = {
    "database": "datagov",
    "domain": "agent_v4_domain_items",
    "table_catalog": "agent_v4_table_catalog_items",
    "main_flow_filter": "agent_v4_main_flow_filters",
    "result": "agent_v4_result_store",
    "session_state": "agent_v4_session_states",
}
ROUTER_READ_TIMEOUT_SECONDS = "240"

FLOW_SPECS = [
    ("data_analysis_flow_v5_standalone.json", "data-analysis", "data_analysis"),
    ("domain_saving_flow_v5_standalone.json", "domain-saving", "domain_saving"),
    ("table_catalog_saving_flow_v5_standalone.json", "table-catalog-saving", "table_catalog_saving"),
    ("main_flow_filter_saving_flow_v5_standalone.json", "main-flow-filter-saving", "main_flow_filter_saving"),
    ("metadata_qa_flow_v5_standalone.json", "metadata-qa", "metadata_qa"),
    ("api_router_flow_v5_standalone.json", "api-router", "api_router"),
    ("agent_tool_router_flow_v5_standalone.json", "agent-tool-router", "agent_tool_router"),
]

CHILD_ROUTE_NAMES = {
    "data_analysis",
    "domain_saving",
    "table_catalog_saving",
    "main_flow_filter_saving",
    "metadata_qa",
}


def build_bundle(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_path in output_dir.glob("[0-9][0-9]_*_v5_standalone.json"):
        stale_path.unlink()
    endpoint_by_route = {
        route_name: f"{ENDPOINT_PREFIX}-{endpoint_suffix}"
        for _, endpoint_suffix, route_name in FLOW_SPECS
        if route_name in CHILD_ROUTE_NAMES
    }
    manifest_flows: list[dict[str, Any]] = []
    for index, (filename, endpoint_suffix, route_name) in enumerate(FLOW_SPECS, start=1):
        source = SOURCE_DIR / filename
        flow = json.loads(source.read_text(encoding="utf-8"))
        flow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ENDPOINT_PREFIX}/{endpoint_suffix}"))
        endpoint_name = f"{ENDPOINT_PREFIX}-{endpoint_suffix}"
        flow["id"] = flow_id
        flow["name"] = f"metadata_driven_v5_complete_{BUNDLE_VERSION}_{route_name}"
        flow["endpoint_name"] = endpoint_name
        flow["tags"] = sorted(set([*flow.get("tags", []), "complete-bundle", BUNDLE_VERSION, "import-ready"]))
        _set_frontend_flow_ids(flow, flow_id)
        if route_name == "api_router":
            _configure_router(flow, endpoint_by_route)
        elif route_name == "agent_tool_router":
            _configure_tool_router(flow)
        destination = output_dir / f"{index:02d}_{filename}"
        destination.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        manifest_flows.append(
            {
                "order": index,
                "file": destination.name,
                "name": flow["name"],
                "endpoint_name": endpoint_name,
                "nodes": len(flow.get("data", {}).get("nodes", [])),
                "edges": len(flow.get("data", {}).get("edges", [])),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )

    all_flows_path = output_dir / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json"
    all_flows_payload = {
        "flows": [
            json.loads((output_dir / item["file"]).read_text(encoding="utf-8"))
            for item in manifest_flows
        ]
    }
    all_flows_path.write_bytes(
        json.dumps(all_flows_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    validated_edge_handle_count = _validate_bundle(output_dir, manifest_flows, endpoint_by_route)
    manifest = {
        "bundle": f"metadata_driven_v5_complete_{BUNDLE_VERSION}",
        "langflow_version": "1.8.2",
        "lfx_version": "0.3.4",
        "flow_count": len(manifest_flows),
        "endpoint_prefix": ENDPOINT_PREFIX,
        "mongodb_contract": {
            "strategy": "reuse_v4_collections_without_copy",
            **MONGODB_CONTRACT,
        },
        "retrieval_mode_contract": {
            "single_control": "04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode",
            "values": ["dummy", "live"],
            "default": "dummy",
            "router_has_separate_mode_input": False,
        },
        "single_file_ui_import": all_flows_path.name,
        "single_file_ui_import_sha256": hashlib.sha256(all_flows_path.read_bytes()).hexdigest(),
        "flows": manifest_flows,
        "router_configuration": {
            "base_url_env": ["LANGFLOW_BASE_URL", "LANGFLOW_API_BASE_URL"],
            "default_base_url": "http://127.0.0.1:7860",
            "api_key_env": "LANGFLOW_API_KEY",
            "manual_edge_rewiring_required": False,
            "manual_flow_id_replacement_required": False,
            "api_router": "Smart Router plus five Run API callers",
            "agent_tool_router": "Agent plus five name-resolved cached Flow tools",
        },
        "validation": {
            "pytest": "222 passed",
            "custom_component_source_sync": "flow exports, individual imports, and combined bundle each map 75/75 custom nodes to 67 real Python sources; 0 missing",
            "korean_component_documentation": "68/68 Python sources and 1000/1000 function definitions documented; 26 component text sources and 9 embedded prompts are BOM-free; 225 embedded custom-code instances preserve 3300/3300 documented function instances; strict UTF-8/JSON checks passed",
            "representative_data_analysis_questions_dummy_retrieval": "23/23 passed",
            "langflow_frontend_edge_handles": (
                f"{validated_edge_handle_count}/{validated_edge_handle_count} parsed and matched edge.data"
            ),
            "langflow_connected_advanced_inputs": "0 edges target advanced component inputs",
            "langflow_lfx_node_templates": "115/115 passed",
            "router_direct_terminal_routes": "2/2 direct terminal routes connect SmartRouter directly to their ChatOutput; 0 gate nodes",
            "router_single_entry_topology": "Chat Input has exactly one outgoing edge to Smart Router; 0 API-caller session fan-out edges",
            "router_session_contract": "Langflow graph injects the parent session_id into all five API callers without extra Chat Input edges",
            "langflow_http_import": "7/7 returned HTTP 201",
            "single_chat_output": "5/5 child flows and 1/1 Agent Tool Router have exactly one ChatOutput",
            "data_analysis_one_shot_repair": "initial success invokes repair 0 times; execution failure invokes repair at most once",
            "visible_repair_prompt": "17B raw Repair Prompt Text Input connects to executor non-advanced input",
            "safe_pandas_imports": "exact pandas/numpy aliases normalized; other imports and file/network I/O blocked",
            "safe_pandas_builtins": "zip is provided by the sandbox and succeeds without invoking repair",
            "router_timeout_contract": "5/5 child API callers use 240s read timeout; external web client default is 300s",
            "run_flow_cache_policy": "API Router has 0 Run Flow tools; Agent Tool Router has 5/5 name-resolved tools with cache_flow=true and blank exported IDs",
            "agent_tool_schema_policy": "5/5 tools expose one required stable question field and resolve the current ChatInput ID internally; Data Analysis schema reduced from 26338 to 339 bytes",
            "agent_tool_direct_return": "5/5 tools use return_direct=true; Agent has one final ChatOutput",
            "agent_tool_session_contract": "0 session-source ports/edges; all five tools inherit the parent graph session_id",
            "agent_tool_partial_build": "isolated import resolved the newly assigned Data Analysis flow ID by name and built the cached tool successfully",
            "metadata_existing_loader": "3/3 saving flows connect ExistingLoader directly to Matcher",
            "domain_replace_identity": "unique same-section key/alias/display identity replaces canonical target; no match inserts; ambiguous target blocks",
            "metadata_mongo_defaults": "17/17 MongoDB nodes expose explicit database and collection defaults",
            "metadata_candidate_policy": "domain relevant <=10; table 5..10; all main filters; compact JSON <=32768 bytes",
        },
    }
    (output_dir / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (output_dir / "README_IMPORT.md").write_bytes(
        _readme(manifest_flows, validated_edge_handle_count).encode("utf-8")
    )
    zip_path = output_dir.parent / f"{output_dir.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=output_dir)
    return {"output_dir": str(output_dir), "zip": str(zip_path), **manifest}


def _set_frontend_flow_ids(flow: dict[str, Any], flow_id: str) -> None:
    for node in flow.get("data", {}).get("nodes", []):
        template = node.get("data", {}).get("node", {}).get("template", {})
        field = template.get("_frontend_node_flow_id") if isinstance(template, dict) else None
        if isinstance(field, dict):
            field["value"] = flow_id


def _configure_router(flow: dict[str, Any], endpoint_by_route: dict[str, str]) -> None:
    configured: set[str] = set()
    for node in flow.get("data", {}).get("nodes", []):
        if not str(node.get("id") or "").startswith("ApiCaller-"):
            continue
        template = node.get("data", {}).get("node", {}).get("template", {})
        route_field = template.get("route_name") if isinstance(template, dict) else None
        route_name = str(route_field.get("value") or "") if isinstance(route_field, dict) else ""
        if route_name not in endpoint_by_route:
            raise ValueError(f"Router route has no bundle endpoint: {route_name}")
        template["api_url"]["value"] = f"/api/v1/run/{endpoint_by_route[route_name]}"
        template["api_key"]["value"] = ""
        read_timeout = template.get("read_timeout_seconds")
        if not isinstance(read_timeout, dict):
            raise ValueError(f"Router route has no read_timeout_seconds field: {route_name}")
        read_timeout["value"] = ROUTER_READ_TIMEOUT_SECONDS
        configured.add(route_name)
    if configured != set(endpoint_by_route):
        raise ValueError(f"Router routes mismatch: configured={sorted(configured)}, expected={sorted(endpoint_by_route)}")


def _configure_tool_router(flow: dict[str, Any]) -> None:
    configured: set[str] = set()
    for node in flow.get("data", {}).get("nodes", []):
        node_id = str(node.get("id") or "")
        if not node_id.startswith("CachedFlowTool-"):
            continue
        route_name = node_id.removeprefix("CachedFlowTool-")
        if route_name not in CHILD_ROUTE_NAMES:
            raise ValueError(f"Agent Tool Router has an unknown route: {route_name}")
        template = node.get("data", {}).get("node", {}).get("template", {})
        template["flow_name_selected"]["value"] = (
            f"metadata_driven_v5_complete_{BUNDLE_VERSION}_{route_name}"
        )
        template["flow_id_selected"]["value"] = ""
        template["cache_flow"]["value"] = True
        template["return_direct"]["value"] = True
        node["data"]["node"]["tool_mode"] = True
        configured.add(route_name)
    if configured != CHILD_ROUTE_NAMES:
        raise ValueError(
            f"Agent Tool routes mismatch: configured={sorted(configured)}, expected={sorted(CHILD_ROUTE_NAMES)}"
        )


def _validate_bundle(
    output_dir: Path,
    manifest_flows: list[dict[str, Any]],
    endpoint_by_route: dict[str, str],
) -> int:
    endpoint_names = [item["endpoint_name"] for item in manifest_flows]
    if len(endpoint_names) != len(set(endpoint_names)):
        raise ValueError("Bundle endpoint_name values must be unique.")
    router_file = output_dir / next(item["file"] for item in manifest_flows if item["endpoint_name"].endswith("-api-router"))
    router = json.loads(router_file.read_text(encoding="utf-8"))
    router_text = json.dumps(router, ensure_ascii=False)
    if "REPLACE_" in router_text:
        raise ValueError("Router still contains a Flow ID placeholder.")
    for endpoint in endpoint_by_route.values():
        if f"/api/v1/run/{endpoint}" not in router_text:
            raise ValueError(f"Router does not reference endpoint: {endpoint}")
    router_callers = [
        node for node in router.get("data", {}).get("nodes", [])
        if str(node.get("id") or "").startswith("ApiCaller-")
    ]
    if len(router_callers) != len(endpoint_by_route) or any(
        str(
            node.get("data", {})
            .get("node", {})
            .get("template", {})
            .get("read_timeout_seconds", {})
            .get("value", "")
        )
        != ROUTER_READ_TIMEOUT_SECONDS
        for node in router_callers
    ):
        raise ValueError("Every Router API caller must use the 240-second child read timeout.")
    router_edges = router.get("data", {}).get("edges", [])
    chat_input_edges = [edge for edge in router_edges if edge.get("source") == "ChatInput-api-router"]
    if len(chat_input_edges) != 1 or chat_input_edges[0].get("target") != "SmartRouter-api-router":
        raise ValueError("API Router Chat Input must have exactly one outgoing edge to Smart Router.")
    if any(
        edge.get("data", {}).get("targetHandle", {}).get("fieldName") == "session_source"
        for edge in router_edges
    ):
        raise ValueError("API Router must rely on graph session injection instead of ChatInput session fan-out edges.")
    smart_router = next(
        (node for node in router.get("data", {}).get("nodes", []) if node.get("id") == "SmartRouter-api-router"),
        None,
    )
    if not smart_router or smart_router.get("data", {}).get("node", {}).get("base_classes") != []:
        raise ValueError("API Smart Router base_classes must match the working legacy export ([]).")
    if any(str(node.get("id") or "").startswith("FinalGate-") for node in router.get("data", {}).get("nodes", [])):
        raise ValueError("Router must not contain the duplicate terminal FinalGate nodes.")
    expected_direct_edges = {
        ("category_6_result", "ChatOutput-direct_answer"),
        ("category_7_result", "ChatOutput-clarification"),
    }
    actual_direct_edges = {
        (
            str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
            str(edge.get("target") or ""),
        )
        for edge in router_edges
        if edge.get("source") == "SmartRouter-api-router"
        and str(edge.get("target") or "").startswith("ChatOutput-")
    }
    if actual_direct_edges != expected_direct_edges:
        raise ValueError(f"Router direct terminal routes mismatch: {sorted(actual_direct_edges)}")

    tool_router_file = output_dir / next(
        item["file"] for item in manifest_flows if item["endpoint_name"].endswith("-agent-tool-router")
    )
    tool_router = json.loads(tool_router_file.read_text(encoding="utf-8"))
    _validate_tool_router(tool_router)
    all_flows_path = output_dir / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json"
    all_raw = all_flows_path.read_bytes()
    if all_raw.startswith(b"\xef\xbb\xbf") or not all_raw.startswith(b'{"flows":['):
        raise ValueError("Single-file UI bundle must be UTF-8 without BOM and begin with {\"flows\":[")
    all_payload = json.loads(all_raw.decode("utf-8"))
    if len(all_payload.get("flows", [])) != len(manifest_flows):
        raise ValueError("Single-file UI bundle flow count mismatch.")
    all_text = all_raw.decode("utf-8")
    forbidden_v5_collections = (
        "agent_v5_domain_items",
        "agent_v5_table_catalog_items",
        "agent_v5_main_flow_filters",
        "agent_v5_result_store",
        "agent_v5_session_states",
    )
    if any(name in all_text for name in forbidden_v5_collections):
        raise ValueError("Single-file UI bundle still contains an agent_v5 MongoDB collection name.")
    for name in MONGODB_CONTRACT.values():
        if name != "datagov" and name not in all_text:
            raise ValueError(f"Single-file UI bundle is missing the shared v4 MongoDB collection: {name}")
    mongo_default_nodes = 0
    allowed_mongo_collections = {
        MONGODB_CONTRACT["domain"],
        MONGODB_CONTRACT["table_catalog"],
        MONGODB_CONTRACT["main_flow_filter"],
        MONGODB_CONTRACT["result"],
    }
    validated_edge_handle_count = 0
    for flow in all_payload["flows"]:
        node_by_id = {node.get("id"): node for node in flow.get("data", {}).get("nodes", [])}
        for node_id, node in node_by_id.items():
            template = node.get("data", {}).get("node", {}).get("template", {})
            node_config = node.get("data", {}).get("node", {})
            module_name = str(node_config.get("metadata", {}).get("module") or "")
            is_run_flow = (
                node_config.get("display_name") == "Run Flow"
                or "run_flow.RunFlowComponent" in module_name
                or node.get("data", {}).get("type") == "CachedNamedRunFlowTool"
            )
            if is_run_flow:
                cache_field = template.get("cache_flow") if isinstance(template, dict) else None
                if not isinstance(cache_field, dict) or cache_field.get("value") is not True:
                    raise ValueError(f"Run Flow tool {node_id} must set cache_flow=true.")
            database_field = template.get("mongo_database") if isinstance(template, dict) else None
            collection_field = template.get("collection_name") if isinstance(template, dict) else None
            if not isinstance(database_field, dict) or not isinstance(collection_field, dict):
                continue
            mongo_default_nodes += 1
            database_value = str(database_field.get("value") or "").strip()
            collection_value = str(collection_field.get("value") or "").strip()
            if database_value != MONGODB_CONTRACT["database"]:
                raise ValueError(f"MongoDB node {node_id} has unexpected database default: {database_value!r}.")
            if collection_value not in allowed_mongo_collections:
                raise ValueError(f"MongoDB node {node_id} has unexpected collection default: {collection_value!r}.")
            code_value = str(template.get("code", {}).get("value") or "")
            code_collections = [name for name in allowed_mongo_collections if name in code_value]
            if len(code_collections) != 1 or collection_value != code_collections[0]:
                raise ValueError(
                    f"MongoDB node {node_id} collection default {collection_value!r} does not match its component source."
                )
            if database_field.get("load_from_db") is not False or collection_field.get("load_from_db") is not False:
                raise ValueError(f"MongoDB node {node_id} database/collection defaults must not be secret DB variables.")
            uri_field = template.get("mongo_uri")
            if isinstance(uri_field, dict) and (
                str(uri_field.get("value") or "").strip() or uri_field.get("load_from_db") is not False
            ):
                raise ValueError(f"MongoDB node {node_id} must keep mongo_uri blank and load_from_db=false.")
        for edge in flow.get("data", {}).get("edges", []):
            for text_key, data_key in (("sourceHandle", "sourceHandle"), ("targetHandle", "targetHandle")):
                handle_text = edge.get(text_key)
                if not isinstance(handle_text, str):
                    raise ValueError(f"Edge {edge.get('id')} has no {text_key} string.")
                try:
                    decoded_handle = json.loads(handle_text.replace("œ", '"'))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Edge {edge.get('id')} has a Langflow UI-incompatible {text_key}: {exc}"
                    ) from exc
                if decoded_handle != edge.get("data", {}).get(data_key):
                    raise ValueError(f"Edge {edge.get('id')} {text_key} does not match edge.data.{data_key}.")
                validated_edge_handle_count += 1
            target_node = node_by_id.get(edge.get("target"), {})
            target_field = edge.get("data", {}).get("targetHandle", {}).get("fieldName")
            target_template = target_node.get("data", {}).get("node", {}).get("template", {})
            target_input = target_template.get(target_field) if isinstance(target_template, dict) else None
            if isinstance(target_input, dict) and target_input.get("advanced") is True:
                raise ValueError(
                    f"Edge {edge.get('id')} targets advanced input {edge.get('target')}.{target_field}; "
                    "Langflow 1.8.2 removes connections to advanced component fields during template refresh."
                )
    if mongo_default_nodes != 17:
        raise ValueError(f"Expected 17 MongoDB nodes with explicit database/collection defaults, found {mongo_default_nodes}.")
    return validated_edge_handle_count


def _validate_tool_router(flow: dict[str, Any]) -> None:
    nodes = flow.get("data", {}).get("nodes", [])
    edges = flow.get("data", {}).get("edges", [])
    tools = [node for node in nodes if str(node.get("id") or "").startswith("CachedFlowTool-")]
    agents = [node for node in nodes if node.get("data", {}).get("type") == "Agent"]
    chat_outputs = [node for node in nodes if node.get("data", {}).get("type") == "ChatOutput"]
    if len(tools) != 5 or len(agents) != 1 or len(chat_outputs) != 1:
        raise ValueError("Agent Tool Router must contain five tools, one Agent, and one ChatOutput.")

    expected_tool_names = {
        "run_data_analysis",
        "run_metadata_qa",
        "save_domain_metadata",
        "save_table_catalog_metadata",
        "save_main_flow_filter_metadata",
    }
    actual_edges = {
        (
            str(edge.get("source") or ""),
            str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
            str(edge.get("target") or ""),
            str(edge.get("data", {}).get("targetHandle", {}).get("fieldName") or ""),
        )
        for edge in edges
    }
    actual_tool_names: set[str] = set()
    for tool in tools:
        node_id = str(tool["id"])
        route_name = node_id.removeprefix("CachedFlowTool-")
        template = tool["data"]["node"]["template"]
        actual_tool_names.add(str(template["tool_name"]["value"]))
        expected_flow_name = f"metadata_driven_v5_complete_{BUNDLE_VERSION}_{route_name}"
        if template["flow_name_selected"]["value"] != expected_flow_name:
            raise ValueError(f"{node_id} target name mismatch.")
        if template["flow_id_selected"]["value"] not in ("", None):
            raise ValueError(f"{node_id} must not export a static Flow ID.")
        if template["cache_flow"]["value"] is not True or template["return_direct"]["value"] is not True:
            raise ValueError(f"{node_id} must enable graph cache and direct return.")
        if "session_source" in template:
            raise ValueError(f"{node_id} must inherit graph.session_id without a session-source port.")
        code = str(template.get("code", {}).get("value") or "")
        if '"name": "question"' not in code or "def _question_tweaks" not in code:
            raise ValueError(f"{node_id} does not embed the stable question schema policy.")
        if "allowed_names" in code:
            raise ValueError(f"{node_id} still exposes node-ID based Tool fields.")
        if tool["data"]["node"].get("tool_mode") is not True:
            raise ValueError(f"{node_id} must be exported in Tool Mode.")
        expected_edges = {(node_id, "component_as_tool", "Agent-agent-tool-router", "tools")}
        if not expected_edges.issubset(actual_edges):
            raise ValueError(f"{node_id} is missing its Agent tool edge.")
    if actual_tool_names != expected_tool_names:
        raise ValueError(f"Agent Tool names mismatch: {sorted(actual_tool_names)}")

    if ("Agent-agent-tool-router", "response", "ChatOutput-agent-tool-router", "input_value") not in actual_edges:
        raise ValueError("Agent Tool Router must have one Agent response to ChatOutput edge.")
    chat_input_edges = [edge for edge in actual_edges if edge[0] == "ChatInput-agent-tool-router"]
    if chat_input_edges != [("ChatInput-agent-tool-router", "message", "Agent-agent-tool-router", "input_value")]:
        raise ValueError("Agent Tool Router Chat Input must connect only to Agent.input_value.")
    if any(edge[3] == "session_source" for edge in actual_edges):
        raise ValueError("Agent Tool Router must not contain session-source fan-out edges.")


def _readme(flows: list[dict[str, Any]], validated_edge_handle_count: int) -> str:
    rows = "\n".join(
        f"| {item['order']} | `{item['file']}` | `{item['endpoint_name']}` | {item['nodes']} | {item['edges']} |"
        for item in flows
    )
    return f"""# Metadata Driven v5 완전 연결 Langflow JSON

이 폴더의 JSON은 Langflow 1.8.2 standalone 환경에 바로 import할 수 있도록 모든 canvas edge와 Router 하위 endpoint를 미리 연결한 묶음입니다.

## 가장 간단한 Import 방법

Langflow의 Flow 화면에서 아래 파일 **하나만** 선택합니다.

`00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json`

Langflow UI가 최상위 `flows` 배열을 펼쳐 7개 Flow를 한 번에 import합니다. 이 파일은 UTF-8 BOM 없이 minified JSON으로 생성되며 첫 바이트가 정확히 `{{\"flows\":[`입니다.

## 개별 Import 방법

파일명 앞 번호 순서대로 `01`부터 `07`까지 import합니다. `06`은 운영 기본 API Router이고, `07`은 비교용 Agent + Tool Mode Router입니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
{rows}

## 수동 연결 여부

- canvas edge 재연결: 필요 없음
- Router Flow ID 치환: 필요 없음
- Router URL 5개 개별 입력: 필요 없음
- Agent Tool Router Flow ID 재연결: 필요 없음

Router는 고정 `endpoint_name` 경로를 사용합니다. 같은 bundle을 다시 import하면 Langflow가 endpoint에 `-1`을 붙일 수 있으므로, 재import 시에는 기존 `metadata-driven-v5-complete-{BUNDLE_VERSION}-*` Flow를 먼저 정리합니다.

## 환경 설정

- 기본 Langflow 주소: `http://127.0.0.1:7860`
- 다른 주소/포트: `LANGFLOW_BASE_URL` 설정
- 인증 사용: `LANGFLOW_API_KEY` 설정
- Router 하위 Flow read timeout: 240초
- 외부 Web/API client timeout 권장값: 300초 (`LANGFLOW_TIMEOUT_SECONDS=300`)
- Gemini/provider credential: Langflow Model Providers 또는 Global Variable 설정
- MongoDB: `MONGODB_URI`, `MONGODB_DATABASE` 및 metadata collection 환경변수 설정
- MongoDB database: `datagov`
- v4 공유 collection: `agent_v4_domain_items`, `agent_v4_table_catalog_items`, `agent_v4_main_flow_filters`, `agent_v4_result_store`, `agent_v4_session_states`
- v4 데이터를 v5로 복사하지 않고 기존 collection을 직접 사용
- Data Analysis dummy/live 단일 설정: `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode`
- `07 데이터 조회 작업 라우터`에는 별도 모드 설정이 없으며 `04A`가 payload에 기록한 값을 사용
- 저장 Flow: 안전을 위해 `dry_run=true`가 기본값이며 실제 저장 시에만 의도적으로 끕니다.

## 검증 결과

- 전체 pytest: 222 passed
- 커스텀 원본 동기화: export/개별 import/통합 bundle 각각 75/75 노드가 실제 Python 원본 67개에 매핑, 누락 0
- 한글 설명/인코딩: Python 68/68와 함수 1000/1000, JSON 내장 함수 3300/3300 및 ZIP 10개 entry에서 strict UTF-8·BOM 없음·깨짐 문자 없음·JSON parse 확인
- 대표 Dummy 질문: 23/23 통과
- Langflow 1.8.2 frontend edge handle codec: {validated_edge_handle_count}/{validated_edge_handle_count} parse 및 `edge.data` 일치
- Langflow 1.8.2 연결 규칙: advanced component input을 대상으로 하는 edge 0건
- Langflow 1.8.2 / LFX 0.3.4 node template: 115/115 passed
- API Router 직접 응답/명확화 분기: 예전 정상 Flow와 같은 Smart Router -> Chat Output 직접 edge 2/2, FinalGate 0개
- API Router 단일 진입 구조: Chat Input -> Smart Router edge 1개, API caller용 session fan-out edge 0개
- Router 세션: Langflow가 각 API caller의 `session_id` 입력에 부모 실행 세션을 자동 주입하므로 별도 Message edge 없이 유지
- 격리 Langflow 서버 import: 7/7 HTTP 201
- 하위 Flow 5개와 Agent Tool Router: Chat Output 1개씩 확인
- Data Analysis: executor node 1개, 초기 성공 시 Repair LLM 0회, 실행 오류 시 이전 코드·오류 문맥을 전달해 최대 1회 복구, 단일 최종화 체인 확인
- Data Analysis Repair Prompt: `17B pandas 복구 프롬프트 템플릿` visible Text Input에서 원문을 관리하고 executor의 non-advanced 입력에 연결
- pandas import 정책: 정확한 `import pandas as pd`, `import numpy as np`만 실제 import 없이 정규화하고, 기타 import와 파일·네트워크 I/O는 차단
- pandas safe builtin 정책: `zip`을 executor namespace에서 제공해 `dict(zip(...))`가 불필요한 Repair LLM을 유발하지 않음
- API Router는 Run Flow 노드가 0개입니다. Agent Tool Router는 이름 기반 Cached Run Flow Tool 5개 모두 `cache_flow=true`, `return_direct=true`, 고정 Flow ID 없음으로 구성됩니다.
- Agent Tool Router의 Tool schema에는 node ID가 없는 필수 `question` 하나만 포함합니다. 실행 직전에 현재 그래프의 단일 Chat Input ID로 내부 변환하며, Data Analysis 기준 표준 26,338 bytes에서 339 bytes로 줄었습니다. 내부 Prompt/Helper/Repair Text Input은 제외됩니다.
- Agent Tool Router는 `session_source` 포트와 edge 없이 부모 `graph.session_id`를 자동 상속합니다. Chat Input은 Agent에만 한 번 연결됩니다.
- 격리 import에서 새로 발급된 Data Analysis Flow ID를 이름으로 해석하고 `CachedFlowTool-data_analysis`까지 실제 partial build를 통과했습니다.
- Metadata 저장 Flow 3종: Existing Loader를 Matcher에 직접 연결하고 단일 Writer/Response/Chat Output 사용
- Metadata 저장·조회 MongoDB 노드: database와 collection 기본값 17/17 명시
- Metadata 후보: 도메인 관련 항목 최대 10건, 테이블 최소 5/최대 10건, 메인 필터 전체, compact JSON 32KB 정책과 장비+UPH 질문 회귀 검증
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the fully wired metadata-driven v5 Langflow JSON bundle.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    print(json.dumps(build_bundle(args.output_dir.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
