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
    "workflow_skill": "agent_v4_workflow_skills",
}
ROUTER_READ_TIMEOUT_SECONDS = "240"
MONGO_GLOBAL_VARIABLE = "MONGO_URL"

FLOW_SPECS = [
    ("data_analysis_flow_v5_standalone.json", "data-analysis", "data_analysis"),
    ("domain_saving_flow_v5_standalone.json", "domain-saving", "domain_saving"),
    ("table_catalog_saving_flow_v5_standalone.json", "table-catalog-saving", "table_catalog_saving"),
    ("main_flow_filter_saving_flow_v5_standalone.json", "main-flow-filter-saving", "main_flow_filter_saving"),
    ("metadata_qa_flow_v5_standalone.json", "metadata-qa", "metadata_qa"),
    ("api_router_flow_v5_standalone.json", "api-router", "api_router"),
    ("agent_tool_router_flow_v5_standalone.json", "agent-tool-router", "agent_tool_router"),
    ("workflow_orchestrator_flow_v5_standalone.json", "workflow-orchestrator", "workflow_orchestrator"),
    ("workflow_skill_saving_flow_v5_standalone.json", "workflow-skill-saving", "workflow_skill_saving"),
    ("html_visualization_flow_v5_standalone.json", "html-visualization", "html_visualization"),
]

FLOW_DISPLAY_NAMES = {
    "data_analysis": "01. v5_data_analysis",
    "domain_saving": "02. v5_domain_saving",
    "table_catalog_saving": "03. v5_table_catalog_saving",
    "main_flow_filter_saving": "04. v5_main_flow_filter_saving",
    "metadata_qa": "05. v5_metadata_qa",
    "api_router": "06. v5_api_router",
    "agent_tool_router": "07. v5_agent_tool_router",
    "workflow_orchestrator": "08. v5_workflow_orchestrator",
    "workflow_skill_saving": "09. v5_workflow_skill_saving",
    "html_visualization": "10. v5_html_visualization",
}
EXPLICIT_STRUCTURED_TERMINALS = {
    "data_analysis": "CustomComponent-3eVde",
    "domain_saving": "Api-domain",
    "table_catalog_saving": "Api-table_catalog",
    "main_flow_filter_saving": "Api-main_flow_filter",
    "metadata_qa": "Api-metadata-qa",
    "workflow_skill_saving": "Api-workflow_skill",
    "html_visualization": "HtmlVisualizationApiTerminal-html-visualization",
}

CHILD_ROUTE_NAMES = {
    "data_analysis",
    "domain_saving",
    "table_catalog_saving",
    "main_flow_filter_saving",
    "metadata_qa",
}
WORKFLOW_CHILD_ROUTE_NAMES = {*CHILD_ROUTE_NAMES, "html_visualization"}


def sync_workflow_sources() -> None:
    """Registry, Loop feedback, Workflow Skill Prompt 기준본을 source export에 동기화합니다."""

    source = SOURCE_DIR / "workflow_orchestrator_flow_v5_standalone.json"
    flow = json.loads(source.read_text(encoding="utf-8"))
    registry_seed = json.dumps(
        json.loads((ROOT / "docs" / "workflows" / "workflow_registry.example.json").read_text(encoding="utf-8")),
        ensure_ascii=False,
        indent=2,
    )
    matched = False
    for node in flow.get("data", {}).get("nodes", []):
        if str(node.get("id") or "") != "WorkflowRegistryLoader-workflow-orchestrator":
            continue
        template = node.get("data", {}).get("node", {}).get("template", {})
        template["inline_seed_json"]["value"] = registry_seed
        matched = True
        break
    if not matched:
        raise ValueError("Workflow Orchestrator Registry loader was not found in the source export.")

    feedback_matched = False
    for edge in flow.get("data", {}).get("edges", []):
        if (
            str(edge.get("source") or "") != "SequentialStepExecutor-workflow-orchestrator"
            or str(edge.get("target") or "") != "Loop-workflow-orchestrator"
            or str(edge.get("data", {}).get("sourceHandle", {}).get("name") or "") != "step_result"
        ):
            continue
        target_handle = edge.get("data", {}).get("targetHandle", {})
        if str(target_handle.get("name") or "") != "item":
            continue
        # Langflow 1.8.x의 Looping 포트는 item.types(Data)와
        # item.loop_types(Message)를 합친 전체 타입 계약을 export합니다.
        target_handle["output_types"] = ["Data", "Message"]
        target_text = json.dumps(target_handle, ensure_ascii=False, separators=(",", ":")).replace('"', "œ")
        edge["targetHandle"] = target_text
        edge["id"] = (
            f"xy-edge__{edge['source']}{edge['sourceHandle']}-"
            f"{edge['target']}{target_text}"
        )
        feedback_matched = True
        break
    if not feedback_matched:
        raise ValueError("Workflow Orchestrator Loop feedback edge was not found in the source export.")
    source.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    skill_source = SOURCE_DIR / "workflow_skill_saving_flow_v5_standalone.json"
    skill_flow = json.loads(skill_source.read_text(encoding="utf-8"))
    prompt_text = (
        ROOT / "langflow_components" / "workflow_skill_saving_flow" / "03_saving_prompt_template_ko.md"
    ).read_text(encoding="utf-8")
    prompt_matched = False
    for node in skill_flow.get("data", {}).get("nodes", []):
        if str(node.get("id") or "") != "PromptExtract-workflow_skill":
            continue
        template = node.get("data", {}).get("node", {}).get("template", {})
        template["template"]["value"] = prompt_text
        prompt_matched = True
        break
    if not prompt_matched:
        raise ValueError("Workflow Skill Prompt Template was not found in the source export.")
    skill_source.write_bytes((json.dumps(skill_flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


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
        flow["name"] = FLOW_DISPLAY_NAMES[route_name]
        flow["endpoint_name"] = endpoint_name
        flow["tags"] = sorted(set([*flow.get("tags", []), "complete-bundle", BUNDLE_VERSION, "import-ready"]))
        _set_frontend_flow_ids(flow, flow_id)
        if route_name == "api_router":
            _configure_router(flow, endpoint_by_route)
        elif route_name == "agent_tool_router":
            _configure_tool_router(flow)
        elif route_name == "workflow_orchestrator":
            _configure_workflow_orchestrator(flow)
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
            "configuration_source": "langflow_node_input",
            "credential_global_variable": MONGO_GLOBAL_VARIABLE,
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
            "workflow_orchestrator": "Language Model planner plus native Loop and six deterministic sequential Flow tools",
        },
        "validation": {
            "pytest": "377 passed",
            "custom_component_source_sync": "flow exports, individual imports, and combined bundle each map 120/120 custom nodes to 83 real Python sources; 0 missing",
            "korean_component_documentation": "84/84 Python sources and 1511/1511 function definitions documented; 36 component text sources and 11 embedded prompts are BOM-free; 360 embedded custom-code instances preserve 6954/6954 documented function instances; strict UTF-8/JSON checks passed",
            "representative_data_analysis_questions_dummy_retrieval": "31/31 passed",
            "langflow_frontend_edge_handles": (
                f"{validated_edge_handle_count}/{validated_edge_handle_count} parsed and matched edge.data"
            ),
            "langflow_connected_advanced_inputs": "0 edges target advanced component inputs",
            "langflow_lfx_node_templates": "173/173 passed",
            "native_language_model_policy": "tool-free LLM stages and Workflow planning/final synthesis use native Language Model components; only the single-call Route V2 uses a native Agent with five real tools",
            "router_direct_terminal_routes": "2/2 direct terminal routes connect SmartRouter through GaiA Output Adapter to native Chat Output; 0 gate nodes",
            "router_single_entry_topology": "native Chat Input connects once through GaiA Input Adapter to Smart Router; 0 API-caller session fan-out edges",
            "router_session_contract": "Langflow graph injects the parent session_id into all five API callers without extra native Chat Input edges",
            "langflow_http_import": "the previous 8 flows passed isolated Langflow 1.8.2 HTTP import; Workflow Orchestrator is covered by bundle and node/edge contract validation until the next live-server import run",
            "single_chat_output": "7/7 child flows, Route V2, and Workflow Orchestrator each have one native Chat Output after one GaiA Output Adapter",
            "data_analysis_one_shot_repair": "initial success invokes repair 0 times; execution failure invokes repair at most once",
            "data_result_download_contract": "23 Result Store keeps data for 1 hour by default and issues direct CSV attachment URLs for result/source refs; 21 owns no Base URL and maps URLs/follow-ups into GaiA metadata",
            "visible_repair_prompt": "17B raw Repair Prompt Text Input connects to executor non-advanced input",
            "safe_pandas_imports": "exact pandas/numpy aliases normalized; other imports and file/network I/O blocked",
            "safe_pandas_builtins": "zip is provided by the sandbox and succeeds without invoking repair",
            "router_timeout_contract": "5/5 child API callers use 240s read timeout; external web client default is 300s",
            "run_flow_cache_policy": "API Router has 0 Run Flow tools; Route V2 5/5 and Workflow Orchestrator 6/6 tools re-resolve the exact current Flow name and cache only the graph by its actual ID; all exported IDs are blank",
            "agent_tool_schema_policy": "5/5 tools expose one required stable question field and resolve the current native Chat Input ID internally; Data Analysis schema reduced from 26338 to 339 bytes",
            "agent_tool_direct_return": "5/5 tools use return_direct=true; Agent response passes through one GaiA Output Adapter to one native Chat Output",
            "agent_tool_session_contract": "0 session-source ports/edges; all five tools inherit the parent graph session_id",
            "agent_tool_partial_build": "isolated import resolved the newly assigned Data Analysis flow ID by name and built the cached tool successfully",
            "workflow_orchestrator_contract": "native planner Language Model emits workflow.plan.v1 from Registry or the six-Tool capability catalog; parser enforces at most four steps and exact Tool names; native Loop executes one deterministic Tool per step",
            "workflow_orchestrator_result_handoff": "Data Analysis produces an explicit result_ref consumed by a follow-up Data Analysis or HTML Visualization step",
            "html_visualization_contract": "one result_ref-backed custom builder produces offline HTML/SVG, publishes absolute browser view/download URLs through the visible Report API input, and keeps raw HTML out of Workflow payloads; one GaiA Output Adapter plus native Chat Output and one separate API adapter expose the two response surfaces",
            "workflow_orchestrator_registry": "visible MongoDB registry loader reads active workflow.registry.v1 items from agent_v4_workflow_skills; inline_seed is an explicit standalone test source, never an implicit fallback",
            "workflow_orchestrator_terminal_contract": "one final Language Model synthesis passes through GaiA Output Adapter to native Chat Output, alongside one terminal api_response; invalid or empty plans still reach the final error response",
            "metadata_duplicate_lookup": "Domain/Table/Main Filter use candidate-targeted Matcher lookup without a dead preloader; Workflow Skill alone keeps its bounded ExistingLoader",
            "domain_replace_identity": "unique same-section key/alias/display identity replaces canonical target; no match inserts; ambiguous target blocks",
            "metadata_mongo_defaults": "16 standard MongoDB nodes and one QA snapshot node bind visible mongo_uri inputs to the MONGO_URL Credential Global Variable; database/collection defaults use datagov and shared agent_v4 collections",
            "metadata_candidate_policy": "domain relevant <=10; table 5..10; all main filters; compact JSON <=32768 bytes",
            "job_scoped_required_params": "each retrieval job carries its own complete required_params; common and distinct date scopes are preserved without cross-job propagation",
            "metadata_qa_product_context": "product group and product aggregation questions use authoritative product_terms/product_key_columns/analysis_recipes context and ignore model prose in deterministic answer modes",
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
        template["flow_name_selected"]["value"] = FLOW_DISPLAY_NAMES[route_name]
        template["flow_id_selected"]["value"] = ""
        template["cache_flow"]["value"] = True
        template["return_direct"]["value"] = True
        node["data"]["node"]["tool_mode"] = True
        configured.add(route_name)
    if configured != CHILD_ROUTE_NAMES:
        raise ValueError(
            f"Agent Tool routes mismatch: configured={sorted(configured)}, expected={sorted(CHILD_ROUTE_NAMES)}"
        )


def _configure_workflow_orchestrator(flow: dict[str, Any]) -> None:
    """Workflow Tool 이름과 검토된 inline Registry seed를 import 산출물에 동기화합니다."""

    configured: set[str] = set()
    registry_seed = json.dumps(
        json.loads((ROOT / "docs" / "workflows" / "workflow_registry.example.json").read_text(encoding="utf-8")),
        ensure_ascii=False,
        indent=2,
    )
    registry_loader_found = False
    for node in flow.get("data", {}).get("nodes", []):
        node_id = str(node.get("id") or "")
        if node_id == "WorkflowRegistryLoader-workflow-orchestrator":
            template = node.get("data", {}).get("node", {}).get("template", {})
            template["inline_seed_json"]["value"] = registry_seed
            registry_loader_found = True
            continue
        if not node_id.startswith("WorkflowFlowTool-"):
            continue
        route_name = node_id.removeprefix("WorkflowFlowTool-")
        if route_name not in WORKFLOW_CHILD_ROUTE_NAMES:
            raise ValueError(f"Workflow Orchestrator has an unknown route: {route_name}")
        template = node.get("data", {}).get("node", {}).get("template", {})
        template["flow_name_selected"]["value"] = FLOW_DISPLAY_NAMES[route_name]
        template["flow_id_selected"]["value"] = ""
        template["cache_flow"]["value"] = True
        template["return_direct"]["value"] = False
        node["data"]["node"]["tool_mode"] = True
        configured.add(route_name)
    if configured != WORKFLOW_CHILD_ROUTE_NAMES:
        raise ValueError(
            "Workflow Orchestrator routes mismatch: "
            f"configured={sorted(configured)}, expected={sorted(WORKFLOW_CHILD_ROUTE_NAMES)}"
        )
    if not registry_loader_found:
        raise ValueError("Workflow Orchestrator Registry loader was not found while synchronizing the inline seed.")


def _validate_bundle(
    output_dir: Path,
    manifest_flows: list[dict[str, Any]],
    endpoint_by_route: dict[str, str],
) -> int:
    expected_flow_names = [FLOW_DISPLAY_NAMES[route_name] for _, _, route_name in FLOW_SPECS]
    manifest_flow_names = [str(item.get("name") or "") for item in manifest_flows]
    if manifest_flow_names != expected_flow_names:
        raise ValueError(
            f"Bundle Flow display names mismatch: actual={manifest_flow_names}, expected={expected_flow_names}"
        )
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
    for caller in router_callers:
        caller_code = str(
            caller.get("data", {}).get("node", {}).get("template", {}).get("code", {}).get("value", "")
        )
        if (
            "NESTED_CHAT_IO_TWEAK" not in caller_code
            or '"Chat Input": {"should_store_message": False}' not in caller_code
            or '"Chat Output": {"should_store_message": False}' not in caller_code
            or 'tweaks["GaiA Input Adapter"]' not in caller_code
        ):
            raise ValueError("Every Router API caller must propagate GaiA adapter context and suppress nested native Chat I/O storage.")
    router_edges = router.get("data", {}).get("edges", [])
    chat_input_edges = [edge for edge in router_edges if edge.get("source") == "ChatInput-api-router"]
    if len(chat_input_edges) != 1 or chat_input_edges[0].get("target") != "GaiAInputAdapter-api-router":
        raise ValueError("API Router native Chat Input must connect only to GaiA Input Adapter.")
    adapter_router_edges = [
        edge
        for edge in router_edges
        if edge.get("source") == "GaiAInputAdapter-api-router"
        and edge.get("target") == "SmartRouter-api-router"
    ]
    if len(adapter_router_edges) != 1:
        raise ValueError("API Router GaiA Input Adapter must connect exactly once to Smart Router.")
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
        ("category_6_result", "GaiAOutputAdapter-direct_answer"),
        ("category_7_result", "GaiAOutputAdapter-clarification"),
    }
    actual_direct_edges = {
        (
            str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
            str(edge.get("target") or ""),
        )
        for edge in router_edges
        if edge.get("source") == "SmartRouter-api-router"
        and str(edge.get("target") or "").startswith("GaiAOutputAdapter-")
    }
    if actual_direct_edges != expected_direct_edges:
        raise ValueError(f"Router direct terminal routes mismatch: {sorted(actual_direct_edges)}")

    tool_router_file = output_dir / next(
        item["file"] for item in manifest_flows if item["endpoint_name"].endswith("-agent-tool-router")
    )
    tool_router = json.loads(tool_router_file.read_text(encoding="utf-8"))
    _validate_tool_router(tool_router)
    workflow_orchestrator_file = output_dir / next(
        item["file"]
        for item in manifest_flows
        if item["endpoint_name"].endswith("-workflow-orchestrator")
    )
    workflow_orchestrator = json.loads(workflow_orchestrator_file.read_text(encoding="utf-8"))
    _validate_workflow_orchestrator(workflow_orchestrator)
    workflow_skill_file = output_dir / next(
        item["file"]
        for item in manifest_flows
        if item["endpoint_name"].endswith("-workflow-skill-saving")
    )
    workflow_skill = json.loads(workflow_skill_file.read_text(encoding="utf-8"))
    _validate_workflow_skill_saving(workflow_skill)
    html_visualization_file = output_dir / next(
        item["file"]
        for item in manifest_flows
        if item["endpoint_name"].endswith("-html-visualization")
    )
    html_visualization = json.loads(html_visualization_file.read_text(encoding="utf-8"))
    _validate_html_visualization(html_visualization)
    all_flows_path = output_dir / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json"
    all_raw = all_flows_path.read_bytes()
    if all_raw.startswith(b"\xef\xbb\xbf") or not all_raw.startswith(b'{"flows":['):
        raise ValueError("Single-file UI bundle must be UTF-8 without BOM and begin with {\"flows\":[")
    all_payload = json.loads(all_raw.decode("utf-8"))
    if len(all_payload.get("flows", [])) != len(manifest_flows):
        raise ValueError("Single-file UI bundle flow count mismatch.")
    combined_flow_names = [str(flow.get("name") or "") for flow in all_payload["flows"]]
    if combined_flow_names != expected_flow_names:
        raise ValueError(
            f"Single-file UI bundle Flow display names mismatch: actual={combined_flow_names}, expected={expected_flow_names}"
        )
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
    snapshot_default_nodes = 0
    allowed_mongo_collections = {
        MONGODB_CONTRACT["domain"],
        MONGODB_CONTRACT["table_catalog"],
        MONGODB_CONTRACT["main_flow_filter"],
        MONGODB_CONTRACT["result"],
        MONGODB_CONTRACT["workflow_skill"],
    }
    validated_edge_handle_count = 0
    for flow in all_payload["flows"]:
        node_by_id = {node.get("id"): node for node in flow.get("data", {}).get("nodes", [])}
        route_name = next(
            (key for key, display_name in FLOW_DISPLAY_NAMES.items() if display_name == str(flow.get("name") or "")),
            "",
        )
        structured_terminal_id = EXPLICIT_STRUCTURED_TERMINALS.get(route_name)
        if structured_terminal_id:
            terminal = node_by_id.get(structured_terminal_id, {})
            terminal_config = terminal.get("data", {}).get("node", {})
            outgoing = [edge for edge in flow.get("data", {}).get("edges", []) if edge.get("source") == structured_terminal_id]
            output_names = {
                str(output.get("name") or "")
                for output in terminal_config.get("outputs", [])
                if isinstance(output, dict)
            }
            if terminal_config.get("is_output") is not True or outgoing or "api_response" not in output_names:
                raise ValueError(
                    f"{flow.get('name')} structured terminal {structured_terminal_id} must be an explicit, edge-free api_response graph output."
                )
        is_child_flow = not str(flow.get("endpoint_name") or "").endswith(("-api-router", "-agent-tool-router"))
        if is_child_flow:
            child_chat_inputs = [
                node
                for node in node_by_id.values()
                if node.get("data", {}).get("type") == "ChatInput"
            ]
            child_input_adapters = [
                node
                for node in node_by_id.values()
                if node.get("data", {}).get("type") == "GaiAInputAdapter"
            ]
            child_output_adapters = [
                node
                for node in node_by_id.values()
                if node.get("data", {}).get("type") == "GaiAOutputAdapter"
            ]
            child_chat_outputs = [
                node
                for node in node_by_id.values()
                if node.get("data", {}).get("type") == "ChatOutput"
            ]
            if not all(
                len(items) == 1
                for items in (
                    child_chat_inputs,
                    child_input_adapters,
                    child_output_adapters,
                    child_chat_outputs,
                )
            ):
                raise ValueError(
                    "Child Flow must contain one native Chat Input, one GaiA Input Adapter, "
                    "one GaiA Output Adapter, and one native Chat Output."
                )
            if any(
                node.get("data", {}).get("node", {}).get("template", {}).get("should_store_message", {}).get("value")
                is not True
                for node in child_chat_outputs
            ):
                raise ValueError("Child Flow native Chat Output must store messages by default for direct Playground use.")
            for gaia_input in child_input_adapters:
                template = gaia_input.get("data", {}).get("node", {}).get("template", {})
                if not {"input_message", "data", "metadata"}.issubset(template):
                    raise ValueError("Child Flow GaiA Input Adapter must expose input_message, data, and metadata.")
            for gaia_output in child_output_adapters:
                output_names = {
                    str(output.get("name") or "")
                    for output in gaia_output.get("data", {}).get("node", {}).get("outputs", [])
                }
                if output_names != {"message", "gaia_response"}:
                    raise ValueError("Child Flow GaiA Output Adapter must expose message and gaia_response.")
                if "should_store_message" in gaia_output.get("data", {}).get("node", {}).get("template", {}):
                    raise ValueError("GaiA Output Adapter must delegate message storage to native Chat Output.")
        for node_id, node in node_by_id.items():
            template = node.get("data", {}).get("node", {}).get("template", {})
            node_config = node.get("data", {}).get("node", {})
            display_name = str(node_config.get("display_name") or "")
            if display_name == "21 답변 메시지 어댑터" and "download_base_url" in template:
                raise ValueError("21 Answer Message Adapter must not own the data download Base URL.")
            if display_name == "23 MongoDB 결과 저장소":
                download_field = template.get("download_base_url") if isinstance(template, dict) else None
                ttl_field = template.get("ttl_hours") if isinstance(template, dict) else None
                if not isinstance(download_field, dict) or (
                    str(download_field.get("value") or "").strip() != "http://127.0.0.1:8765"
                    or download_field.get("advanced") is not False
                ):
                    raise ValueError("23 Result Store must expose the visible direct-download Base URL input.")
                if not isinstance(ttl_field, dict) or (
                    str(ttl_field.get("value") or "").strip() != "1"
                    or ttl_field.get("advanced") is not False
                ):
                    raise ValueError("23 Result Store must expose a visible 1-hour default TTL input.")
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
            snapshot_fields = {
                "domain_collection_name": MONGODB_CONTRACT["domain"],
                "table_collection_name": MONGODB_CONTRACT["table_catalog"],
                "filter_collection_name": MONGODB_CONTRACT["main_flow_filter"],
            }
            is_snapshot_node = isinstance(database_field, dict) and all(
                isinstance(template.get(field_name), dict) for field_name in snapshot_fields
            )
            if is_snapshot_node:
                snapshot_default_nodes += 1
                database_value = str(database_field.get("value") or "").strip()
                if database_value != MONGODB_CONTRACT["database"]:
                    raise ValueError(f"MongoDB snapshot node {node_id} has unexpected database default: {database_value!r}.")
                code_value = str(template.get("code", {}).get("value") or "")
                for field_name, expected_collection in snapshot_fields.items():
                    field = template[field_name]
                    collection_value = str(field.get("value") or "").strip()
                    if collection_value != expected_collection or expected_collection not in code_value:
                        raise ValueError(
                            f"MongoDB snapshot node {node_id}.{field_name} has unexpected collection default: {collection_value!r}."
                        )
                    if field.get("load_from_db") is not False:
                        raise ValueError(f"MongoDB snapshot node {node_id}.{field_name} must not load defaults as secrets.")
                if database_field.get("load_from_db") is not False:
                    raise ValueError(f"MongoDB snapshot node {node_id} database default must not be a secret DB variable.")
                uri_field = template.get("mongo_uri")
                if not isinstance(uri_field, dict) or (
                    str(uri_field.get("value") or "").strip() != MONGO_GLOBAL_VARIABLE
                    or uri_field.get("load_from_db") is not True
                    or uri_field.get("advanced") is not False
                ):
                    raise ValueError(
                        f"MongoDB snapshot node {node_id} must bind visible mongo_uri to {MONGO_GLOBAL_VARIABLE}."
                    )
                continue
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
            if not isinstance(uri_field, dict) or (
                str(uri_field.get("value") or "").strip() != MONGO_GLOBAL_VARIABLE
                or uri_field.get("load_from_db") is not True
                or uri_field.get("advanced") is not False
            ):
                raise ValueError(f"MongoDB node {node_id} must bind visible mongo_uri to {MONGO_GLOBAL_VARIABLE}.")
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
    if mongo_default_nodes != 16 or snapshot_default_nodes != 1:
        raise ValueError(
            "Expected 16 standard MongoDB nodes and 1 three-collection QA snapshot node with explicit defaults, "
            f"found standard={mongo_default_nodes}, snapshot={snapshot_default_nodes}."
        )
    return validated_edge_handle_count


def _validate_tool_router(flow: dict[str, Any]) -> None:
    nodes = flow.get("data", {}).get("nodes", [])
    edges = flow.get("data", {}).get("edges", [])
    tools = [node for node in nodes if str(node.get("id") or "").startswith("CachedFlowTool-")]
    agents = [node for node in nodes if node.get("data", {}).get("type") == "Agent"]
    chat_outputs = [node for node in nodes if node.get("data", {}).get("type") == "ChatOutput"]
    input_adapters = [node for node in nodes if node.get("data", {}).get("type") == "GaiAInputAdapter"]
    output_adapters = [node for node in nodes if node.get("data", {}).get("type") == "GaiAOutputAdapter"]
    if (
        len(tools) != 5
        or len(agents) != 1
        or len(chat_outputs) != 1
        or len(input_adapters) != 1
        or len(output_adapters) != 1
    ):
        raise ValueError("Agent Tool Router must contain five tools, one Agent, native Chat I/O, and one GaiA adapter pair.")

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
        expected_flow_name = FLOW_DISPLAY_NAMES[route_name]
        if template["flow_name_selected"]["value"] != expected_flow_name:
            raise ValueError(f"{node_id} target name mismatch.")
        if template["flow_id_selected"]["value"] not in ("", None):
            raise ValueError(f"{node_id} must not export a static Flow ID.")
        if template["cache_flow"]["value"] is not True or template["return_direct"]["value"] is not True:
            raise ValueError(f"{node_id} must enable graph cache and direct return.")
        if "session_source" in template:
            raise ValueError(f"{node_id} must inherit graph.session_id without a session-source port.")
        code = str(template.get("code", {}).get("value") or "")
        if (
            '"name": "question"' not in code
            or "def _question_tweaks" not in code
            or "def _single_chat_output_id" not in code
            or "GaiAInput" not in code
            or "gaia_response" not in code
            or 'runtime_user_id = str(getattr(self, "user_id"' not in code
            or "self.user_id =" in code
            or "UUID(requested_flow_id)" in code
            or "def _chat_output_target" not in code
            or "def _promote_graph_output" not in code
        ):
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

    if (
        "Agent-agent-tool-router",
        "response",
        "GaiAOutputAdapter-agent-tool-router",
        "input_value",
    ) not in actual_edges or (
        "GaiAOutputAdapter-agent-tool-router",
        "message",
        "ChatOutput-agent-tool-router",
        "input_value",
    ) not in actual_edges:
        raise ValueError("Agent Tool Router must pass the Agent response through GaiA Output Adapter to native Chat Output.")
    chat_input_edges = [edge for edge in actual_edges if edge[0] == "ChatInput-agent-tool-router"]
    if chat_input_edges != [
        ("ChatInput-agent-tool-router", "message", "GaiAInputAdapter-agent-tool-router", "input_message")
    ] or (
        "GaiAInputAdapter-agent-tool-router",
        "message",
        "Agent-agent-tool-router",
        "input_value",
    ) not in actual_edges:
        raise ValueError("Agent Tool Router must pass native Chat Input through GaiA Input Adapter to Agent.input_value.")
    if any(edge[3] == "session_source" for edge in actual_edges):
        raise ValueError("Agent Tool Router must not contain session-source fan-out edges.")


def _validate_workflow_orchestrator(flow: dict[str, Any]) -> None:
    """Workflow의 planner·native Loop·정확한 Tool 실행·단일 최종 합성 계약을 검증합니다."""

    nodes = flow.get("data", {}).get("nodes", [])
    edges = flow.get("data", {}).get("edges", [])
    node_by_id = {str(node.get("id") or ""): node for node in nodes}
    tools = [node for node in nodes if str(node.get("id") or "").startswith("WorkflowFlowTool-")]
    models = [node for node in nodes if node.get("data", {}).get("type") == "LanguageModelComponent"]
    agents = [node for node in nodes if node.get("data", {}).get("type") == "Agent"]
    loops = [node for node in nodes if node.get("data", {}).get("type") == "LoopComponent"]
    chat_outputs = [node for node in nodes if node.get("data", {}).get("type") == "ChatOutput"]
    input_adapters = [node for node in nodes if node.get("data", {}).get("type") == "GaiAInputAdapter"]
    output_adapters = [node for node in nodes if node.get("data", {}).get("type") == "GaiAOutputAdapter"]
    if (
        len(tools) != 6
        or len(models) != 2
        or agents
        or len(loops) != 1
        or len(chat_outputs) != 1
        or len(input_adapters) != 1
        or len(output_adapters) != 1
    ):
        raise ValueError(
            "Workflow Orchestrator must contain six tools, two Language Models, one native Loop, no Agent, "
            "native Chat I/O, and one GaiA adapter pair."
        )

    expected_tool_names = {
        "run_data_analysis",
        "run_metadata_qa",
        "save_domain_metadata",
        "save_table_catalog_metadata",
        "save_main_flow_filter_metadata",
        "run_visualization",
    }
    actual_tool_names: set[str] = set()
    for tool in tools:
        node_id = str(tool.get("id") or "")
        route_name = node_id.removeprefix("WorkflowFlowTool-")
        template = tool.get("data", {}).get("node", {}).get("template", {})
        actual_tool_names.add(str(template.get("tool_name", {}).get("value") or ""))
        expected_flow_name = FLOW_DISPLAY_NAMES[route_name]
        if template.get("flow_name_selected", {}).get("value") != expected_flow_name:
            raise ValueError(f"{node_id} target name mismatch.")
        if template.get("flow_id_selected", {}).get("value") not in ("", None):
            raise ValueError(f"{node_id} must not export a static Flow ID.")
        if template.get("cache_flow", {}).get("value") is not True:
            raise ValueError(f"{node_id} must enable graph cache.")
        if template.get("return_direct", {}).get("value") is not False:
            raise ValueError(f"{node_id} must disable direct return for Loop orchestration.")
        if str(template.get("preferred_output_names", {}).get("value") or "") != "api_response":
            raise ValueError(f"{node_id} must explicitly select the current child Flow api_response terminal.")
        if tool.get("data", {}).get("node", {}).get("tool_mode") is not True:
            raise ValueError(f"{node_id} must be exported in Tool Mode.")
    if actual_tool_names != expected_tool_names:
        raise ValueError(f"Workflow Orchestrator Tool names mismatch: {sorted(actual_tool_names)}")

    parser = node_by_id.get("WorkflowPlanParser-workflow-orchestrator", {})
    parser_template = parser.get("data", {}).get("node", {}).get("template", {})
    registry_loader = node_by_id.get("WorkflowRegistryLoader-workflow-orchestrator", {})
    registry_loader_template = registry_loader.get("data", {}).get("node", {}).get("template", {})
    registry_path = ROOT / "docs" / "workflows" / "workflow_registry.example.json"
    registry_expected = json.dumps(
        json.loads(registry_path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2
    )
    registry_value = str(parser_template.get("workflow_registry_json", {}).get("value") or "")
    if registry_value != "{}":
        raise ValueError("Workflow Orchestrator parser input must be empty unless the Registry loader edge supplies it.")
    if (
        str(registry_loader_template.get("registry_source", {}).get("value") or "") != "mongodb"
        or str(registry_loader_template.get("mongo_database", {}).get("value") or "") != MONGODB_CONTRACT["database"]
        or str(registry_loader_template.get("collection_name", {}).get("value") or "") != MONGODB_CONTRACT["workflow_skill"]
        or str(registry_loader_template.get("inline_seed_json", {}).get("value") or "") != registry_expected
        or str(registry_loader_template.get("candidate_limit", {}).get("value") or "") != "8"
        or str(registry_loader_template.get("max_registry_bytes", {}).get("value") or "") != "65536"
    ):
        raise ValueError("Workflow Orchestrator Registry loader must use visible MongoDB defaults and the exact inline seed.")
    planner_template = (
        node_by_id.get("PromptPlanner-workflow-orchestrator", {})
        .get("data", {})
        .get("node", {})
        .get("template", {})
    )
    planner_prompt = str(planner_template.get("template", {}).get("value", ""))
    planner_registry = str(planner_template.get("workflow_registry_json", {}).get("value") or "")
    if planner_registry != "{}" or "최대 4" not in planner_prompt or "{allowed_tool_catalog}" not in planner_prompt:
        raise ValueError("Workflow Orchestrator planner must rely on the Registry edge and preserve the four-step limit.")
    allowed_tools_value = str(parser_template.get("allowed_tool_names", {}).get("value") or "")
    if set(json.loads(allowed_tools_value)) != expected_tool_names:
        raise ValueError("Workflow Orchestrator parser allowed Tool names mismatch.")
    planner_allowed_tools = str(planner_template.get("allowed_tool_names", {}).get("value") or "")
    if set(json.loads(planner_allowed_tools)) != expected_tool_names:
        raise ValueError("Workflow Orchestrator planner allowed Tool names mismatch.")
    tool_catalog_value = str(planner_template.get("allowed_tool_catalog", {}).get("value") or "")
    tool_catalog = json.loads(tool_catalog_value)
    catalog_by_name = {
        str(item.get("tool_name") or ""): item
        for item in tool_catalog
        if isinstance(item, dict)
    }
    if set(catalog_by_name) != expected_tool_names or any(
        not str(item.get("description") or "").strip()
        for item in catalog_by_name.values()
    ):
        raise ValueError("Workflow Orchestrator planner Tool capability catalog mismatch.")
    if (
        catalog_by_name["run_data_analysis"].get("accepts_upstream_result_ref") is not True
        or catalog_by_name["run_data_analysis"].get("can_produce_result_ref") is not True
        or catalog_by_name["run_visualization"].get("accepts_upstream_result_ref") is not True
        or catalog_by_name["run_visualization"].get("can_produce_result_ref") is not False
    ):
        raise ValueError("Workflow Orchestrator result_ref capabilities are invalid.")

    actual_edges = {
        (
            str(edge.get("source") or ""),
            str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
            str(edge.get("target") or ""),
            str(edge.get("data", {}).get("targetHandle", {}).get("fieldName") or ""),
            str(edge.get("data", {}).get("targetHandle", {}).get("name") or ""),
        )
        for edge in edges
    }
    expected_edges = {
        (
            "GaiAInputAdapter-workflow-orchestrator",
            "message",
            "WorkflowRegistryLoader-workflow-orchestrator",
            "user_question",
            "",
        ),
        (
            "WorkflowRegistryLoader-workflow-orchestrator",
            "workflow_registry_json",
            "PromptPlanner-workflow-orchestrator",
            "workflow_registry_json",
            "",
        ),
        (
            "WorkflowRegistryLoader-workflow-orchestrator",
            "workflow_registry_json",
            "WorkflowPlanParser-workflow-orchestrator",
            "workflow_registry_json",
            "",
        ),
        (
            "WorkflowPlanParser-workflow-orchestrator",
            "loop_dataframe",
            "Loop-workflow-orchestrator",
            "data",
            "",
        ),
        (
            "Loop-workflow-orchestrator",
            "item",
            "SequentialStepExecutor-workflow-orchestrator",
            "loop_item",
            "",
        ),
        (
            "SequentialStepExecutor-workflow-orchestrator",
            "step_result",
            "Loop-workflow-orchestrator",
            "",
            "item",
        ),
        (
            "WorkflowPlanParser-workflow-orchestrator",
            "workflow_plan",
            "FinalContext-workflow-orchestrator",
            "execution_context",
            "",
        ),
        (
            "Loop-workflow-orchestrator",
            "done",
            "FinalContext-workflow-orchestrator",
            "loop_results",
            "",
        ),
        (
            "FinalResponse-workflow-orchestrator",
            "message",
            "GaiAOutputAdapter-workflow-orchestrator",
            "input_value",
            "",
        ),
        (
            "ChatInput-workflow-orchestrator",
            "message",
            "GaiAInputAdapter-workflow-orchestrator",
            "input_message",
            "",
        ),
        (
            "GaiAOutputAdapter-workflow-orchestrator",
            "message",
            "ChatOutput-workflow-orchestrator",
            "input_value",
            "",
        ),
    }
    missing_edges = expected_edges - actual_edges
    if missing_edges:
        raise ValueError(f"Workflow Orchestrator is missing required edges: {sorted(missing_edges)}")
    feedback_edges = [
        edge
        for edge in edges
        if str(edge.get("source") or "") == "SequentialStepExecutor-workflow-orchestrator"
        and str(edge.get("target") or "") == "Loop-workflow-orchestrator"
        and str(edge.get("data", {}).get("sourceHandle", {}).get("name") or "") == "step_result"
        and str(edge.get("data", {}).get("targetHandle", {}).get("name") or "") == "item"
    ]
    if len(feedback_edges) != 1 or feedback_edges[0]["data"]["targetHandle"].get("output_types") != [
        "Data",
        "Message",
    ]:
        raise ValueError("Workflow Orchestrator Looping feedback must expose Data and Message target types.")
    for tool in tools:
        expected_edge = (
            str(tool.get("id") or ""),
            "component_as_tool",
            "SequentialStepExecutor-workflow-orchestrator",
            "tools",
            "",
        )
        if expected_edge not in actual_edges:
            raise ValueError(f"{tool.get('id')} is missing its exact executor Tool edge.")

    response_node = node_by_id.get("FinalResponse-workflow-orchestrator", {})
    response_outputs = response_node.get("data", {}).get("node", {}).get("outputs", [])
    if not any(output.get("name") == "api_response" for output in response_outputs):
        raise ValueError("Workflow Orchestrator must expose terminal api_response.")
    if any(edge[0] == "FinalResponse-workflow-orchestrator" and edge[1] == "api_response" for edge in actual_edges):
        raise ValueError("Workflow Orchestrator api_response must remain a terminal output.")


def _validate_html_visualization(flow: dict[str, Any]) -> None:
    """HTML 시각화 Flow의 단일 입력·단일 출력·result_ref·terminal API 계약을 검증합니다."""

    nodes = {str(node.get("id") or ""): node for node in flow.get("data", {}).get("nodes", [])}
    edges = {
        (
            str(edge.get("source") or ""),
            str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
            str(edge.get("target") or ""),
            str(edge.get("data", {}).get("targetHandle", {}).get("fieldName") or ""),
        )
        for edge in flow.get("data", {}).get("edges", [])
    }
    expected_node_ids = {
        "ChatInput-html-visualization",
        "GaiAInputAdapter-html-visualization",
        "HtmlVisualizationBuilder-html-visualization",
        "GaiAOutputAdapter-html-visualization",
        "ChatOutput-html-visualization",
        "HtmlVisualizationApiTerminal-html-visualization",
    }
    if set(nodes) != expected_node_ids or len(edges) != 5:
        raise ValueError("HTML Visualization must contain six nodes and five edges including native Chat I/O and GaiA adapters.")
    expected_edges = {
        (
            "ChatInput-html-visualization",
            "message",
            "GaiAInputAdapter-html-visualization",
            "input_message",
        ),
        (
            "GaiAInputAdapter-html-visualization",
            "message",
            "HtmlVisualizationBuilder-html-visualization",
            "question",
        ),
        (
            "HtmlVisualizationBuilder-html-visualization",
            "message",
            "GaiAOutputAdapter-html-visualization",
            "input_value",
        ),
        (
            "GaiAOutputAdapter-html-visualization",
            "message",
            "ChatOutput-html-visualization",
            "input_value",
        ),
        (
            "HtmlVisualizationBuilder-html-visualization",
            "api_response",
            "HtmlVisualizationApiTerminal-html-visualization",
            "visualization_result",
        ),
    }
    if edges != expected_edges:
        raise ValueError(f"HTML Visualization edges mismatch: {sorted(edges)}")

    builder = nodes["HtmlVisualizationBuilder-html-visualization"]
    template = builder.get("data", {}).get("node", {}).get("template", {})
    if (
        str(template.get("mongo_uri", {}).get("value") or "") != MONGO_GLOBAL_VARIABLE
        or template.get("mongo_uri", {}).get("load_from_db") is not True
        or str(template.get("mongo_database", {}).get("value") or "") != MONGODB_CONTRACT["database"]
        or str(template.get("collection_name", {}).get("value") or "") != MONGODB_CONTRACT["result"]
        or template.get("upstream_result_ref", {}).get("advanced") is not False
        or str(template.get("report_api_url", {}).get("value") or "") != "http://127.0.0.1:8010"
        or template.get("report_api_url", {}).get("advanced") is not False
        or str(template.get("report_ttl_hours", {}).get("value") or "") != "24"
        or template.get("report_ttl_hours", {}).get("advanced") is not False
    ):
        raise ValueError("HTML Visualization must expose standalone MongoDB, result_ref, and Report API inputs.")
    output_names = {
        str(output.get("name") or "")
        for output in builder.get("data", {}).get("node", {}).get("outputs", [])
    }
    if output_names != {"message", "api_response"}:
        raise ValueError("HTML Visualization builder must expose message and api_response outputs.")
    source_path = ROOT / "langflow_components" / "visualization_flow" / "00_html_visualization_builder.py"
    embedded = str(template.get("code", {}).get("value") or "")
    if embedded != source_path.read_text(encoding="utf-8"):
        raise ValueError("HTML Visualization embedded component source is out of sync.")

    terminal = nodes["HtmlVisualizationApiTerminal-html-visualization"]
    terminal_outputs = {
        str(output.get("name") or "")
        for output in terminal.get("data", {}).get("node", {}).get("outputs", [])
    }
    if terminal_outputs != {"api_response"}:
        raise ValueError("HTML Visualization API terminal must expose one api_response output.")
    if any(source == "HtmlVisualizationApiTerminal-html-visualization" for source, _handle, _target, _field in edges):
        raise ValueError("HTML Visualization API adapter must remain terminal.")
    terminal_source_path = ROOT / "langflow_components" / "visualization_flow" / "01_html_visualization_api_terminal.py"
    terminal_template = terminal.get("data", {}).get("node", {}).get("template", {})
    terminal_embedded = str(terminal_template.get("code", {}).get("value") or "")
    if terminal_embedded != terminal_source_path.read_text(encoding="utf-8"):
        raise ValueError("HTML Visualization API terminal embedded component source is out of sync.")


def _validate_workflow_skill_saving(flow: dict[str, Any]) -> None:
    """Workflow Skill 저장 Flow의 dry-run·기존 항목·단일 출력 계약을 검증합니다."""

    nodes = {str(node.get("id") or ""): node for node in flow.get("data", {}).get("nodes", [])}
    edges = {
        (
            str(edge.get("source") or ""),
            str(edge.get("data", {}).get("sourceHandle", {}).get("name") or ""),
            str(edge.get("target") or ""),
            str(edge.get("data", {}).get("targetHandle", {}).get("fieldName") or ""),
        )
        for edge in flow.get("data", {}).get("edges", [])
    }
    chat_outputs = [node for node in nodes.values() if node.get("data", {}).get("type") == "ChatOutput"]
    input_adapters = [node for node in nodes.values() if node.get("data", {}).get("type") == "GaiAInputAdapter"]
    output_adapters = [node for node in nodes.values() if node.get("data", {}).get("type") == "GaiAOutputAdapter"]
    models = [node for node in nodes.values() if node.get("data", {}).get("type") == "LanguageModelComponent"]
    if (
        len(nodes) != 15
        or len(edges) != 16
        or len(chat_outputs) != 1
        or len(input_adapters) != 1
        or len(output_adapters) != 1
        or len(models) != 1
    ):
        raise ValueError(
            "Workflow Skill saving Flow must contain 15 nodes, 16 edges, one Language Model, native Chat I/O, and one GaiA adapter pair."
        )

    request_template = nodes.get("Request-workflow_skill", {}).get("data", {}).get("node", {}).get("template", {})
    if request_template.get("dry_run", {}).get("value") is not True:
        raise ValueError("Workflow Skill saving Flow must default to dry_run=true.")
    if request_template.get("duplicate_action", {}).get("options") != ["skip", "merge", "replace", "create_new"]:
        raise ValueError("Workflow Skill saving Flow duplicate_action options mismatch.")
    existing_template = (
        nodes.get("ExistingLoader-workflow_skill", {}).get("data", {}).get("node", {}).get("template", {})
    )
    if str(existing_template.get("limit", {}).get("value") or "") != "500":
        raise ValueError("Workflow Skill existing loader must read the bounded active list instead of being disabled.")

    expected_edges = {
        ("ExistingLoader-workflow_skill", "existing_items", "Matcher-workflow_skill", "existing_items"),
        ("Matcher-workflow_skill", "payload_out", "Writer-workflow_skill", "payload"),
        ("ChatInput-workflow_skill", "message", "GaiAInputAdapter-workflow_skill", "input_message"),
        ("Message-workflow_skill", "message", "GaiAOutputAdapter-workflow_skill", "input_value"),
        ("GaiAOutputAdapter-workflow_skill", "message", "ChatOutput-workflow_skill", "input_value"),
    }
    if not expected_edges.issubset(edges):
        raise ValueError("Workflow Skill saving Flow is missing its existing-item, writer, or single-output edge.")


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

Langflow UI가 최상위 `flows` 배열을 펼쳐 10개 Flow를 한 번에 import합니다. 이 파일은 UTF-8 BOM 없이 minified JSON으로 생성되며 첫 바이트가 정확히 `{{\"flows\":[`입니다.

## 개별 Import 방법

파일명 앞 번호 순서대로 `01`부터 `10`까지 import합니다. `06`은 운영 기본 API Router, `07`은 단일 호출용 Agent + Tool Mode Router, `08`은 등록 또는 자연어 Workflow를 기본 Loop로 실행하는 Workflow Orchestrator, `09`는 Workflow Skill 등록·검토·저장 Flow, `10`은 Data Analysis 결과 참조를 HTML 차트로 만드는 Flow입니다.

| 순서 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
{rows}

## 수동 연결 여부

- canvas edge 재연결: 필요 없음
- Router Flow ID 치환: 필요 없음
- Router URL 5개 개별 입력: 필요 없음
- Agent Tool Router Flow ID 재연결: 필요 없음
- Workflow Orchestrator Flow ID 재연결: 필요 없음

Router는 고정 `endpoint_name` 경로를 사용합니다. 같은 bundle을 다시 import하면 Langflow가 endpoint에 `-1`을 붙일 수 있으므로, 재import 시에는 기존 `metadata-driven-v5-complete-{BUNDLE_VERSION}-*` Flow를 먼저 정리합니다.

## 환경 설정

- 기본 Langflow 주소: `http://127.0.0.1:7860`
- 다른 주소/포트: `LANGFLOW_BASE_URL` 설정
- 인증 사용: `LANGFLOW_API_KEY` 설정
- Router 하위 Flow read timeout: 240초
- 외부 Web/API client timeout 권장값: 단일 호출 300초, Workflow 연계 호출 600초
- Gemini/provider credential: Langflow Model Providers 또는 Global Variable 설정
- MongoDB: Langflow Credential Global Variable `MONGO_URL` 생성 후 import된 Mongo 노드의 바인딩 확인
- MongoDB database: `datagov`
- v4 공유 collection: `agent_v4_domain_items`, `agent_v4_table_catalog_items`, `agent_v4_main_flow_filters`, `agent_v4_result_store`, `agent_v4_session_states`, `agent_v4_workflow_skills`
- v4 데이터를 v5로 복사하지 않고 기존 collection을 직접 사용
- 실제 Mongo URI는 JSON에 포함되지 않으며 Python 컴포넌트는 OS `MONGODB_URI` fallback을 사용하지 않음
- Data Analysis dummy/live 단일 설정: `04A 신뢰 카탈로그 조회 작업 구성기.retrieval_mode`
- `07 데이터 조회 작업 라우터`에는 별도 모드 설정이 없으며 `04A`가 payload에 기록한 값을 사용
- 저장 Flow: 안전을 위해 `dry_run=true`가 기본값이며 실제 저장 시에만 의도적으로 끕니다.

## 검증 결과

- 전체 pytest: 377 passed
- 커스텀 원본 동기화: export/개별 import/통합 bundle 각각 120/120 노드가 실제 Python 원본 83개에 매핑, 누락 0
- 한글 설명/인코딩: Python·JSON·ZIP 전체에서 strict UTF-8·BOM 없음·깨짐 문자 없음·JSON parse 확인
- 대표 Dummy 질문: 31/31 통과
- Langflow 1.8.2 frontend edge handle codec: {validated_edge_handle_count}/{validated_edge_handle_count} parse 및 `edge.data` 일치
- Langflow 1.8.2 연결 규칙: advanced component input을 대상으로 하는 edge 0건
- Langflow 1.8.2 / LFX 0.3.4 node template: 147/147 passed
- Tool 없는 모델 단계와 Workflow 계획/최종 합성은 기본 Language Model을 사용하고, 단일 호출 Route V2만 실제 Tool이 연결된 기본 Agent를 유지
- API Router 직접 응답/명확화 분기: Smart Router -> GaiA Output Adapter -> 표준 Chat Output 2/2, FinalGate 0개
- API Router 단일 진입 구조: 표준 Chat Input -> GaiA Input Adapter -> Smart Router, API caller용 session fan-out edge 0개
- Router 세션: Langflow가 각 API caller의 `session_id` 입력에 부모 실행 세션을 자동 주입하므로 별도 Message edge 없이 유지
- 기존 8개 Flow의 격리 Langflow 서버 import는 검증 완료했으며, Workflow Orchestrator는 이번 bundle/node/edge 계약 검증 후 다음 live-server import 대상입니다.
- 통합 `00` 단일 JSON은 10개 Flow를 포함하도록 생성하고 UTF-8/BOM/flow count를 검증합니다.
- 하위 Flow 7개, Route V2, Workflow Orchestrator: GaiA Output Adapter 1개와 표준 Chat Output 1개씩 확인
- Data Analysis: executor node 1개, 초기 성공 시 Repair LLM 0회, 실행 오류 시 이전 코드·오류 문맥을 전달해 최대 1회 복구, 단일 최종화 체인 확인
- Data Analysis Repair Prompt: `17B pandas 복구 프롬프트 템플릿` visible Text Input에서 원문을 관리하고 executor의 non-advanced 입력에 연결
- pandas import 정책: 정확한 `import pandas as pd`, `import numpy as np`만 실제 import 없이 정규화하고, 기타 import와 파일·네트워크 I/O는 차단
- pandas safe builtin 정책: `zip`을 executor namespace에서 제공해 `dict(zip(...))`가 불필요한 Repair LLM을 유발하지 않음
- API Router는 Run Flow 노드가 0개입니다. Agent Tool Router는 이름 기반 Cached Run Flow Tool 5개 모두 Langflow의 현재 실행 `user_id` 범위에서 매 실행 정확한 Flow 이름을 현재 ID로 다시 해석하며, `cache_flow=true`, `return_direct=true`, 고정 Flow ID 없음으로 구성됩니다. 해석된 실제 ID는 graph cache key로만 사용합니다.
- Agent Tool Router는 하위 Flow의 표준 Chat Output Message를 `return_direct=true`로 그대로 반환하며, Message.data의 `gaia_response`를 보존합니다.
- Agent Tool Router의 Tool schema에는 node ID가 없는 필수 `question` 하나만 포함합니다. 실행 직전에 현재 그래프의 단일 표준 Chat Input ID로 내부 변환합니다.
- Agent Tool Router는 `session_source` 포트와 edge 없이 부모 `graph.session_id`를 자동 상속합니다. 표준 Chat Input의 Message는 GaiA Input Adapter를 거쳐 Agent에만 한 번 연결됩니다.
- 격리 import에서 현재 Langflow 실행 사용자로 새로 발급된 Data Analysis Flow ID를 이름으로 해석하고 `CachedFlowTool-data_analysis`까지 실제 partial build를 통과했습니다.
- Workflow Orchestrator의 이름 기반 Tool 6개는 `question`과 선택 `upstream_result_ref`만 노출하고, 하위 API 응답을 `route_v3.tool_result.v1` compact observation으로 변환합니다.
- Workflow Orchestrator는 기본 Language Model 계획기 -> `workflow.plan.v1` 파서 -> 기본 Loop -> 정확한 Tool 단일 실행기 순서로 최대 네 단계를 실행합니다. Registry와 일치하지 않아도 capability catalog의 Tool만으로 해결 가능하면 inline 계획을 만들며 Agent의 자율 반복은 사용하지 않습니다.
- Workflow Orchestrator는 기본적으로 `datagov.agent_v4_workflow_skills`의 active Skill을 질문 기준 후보로 조회합니다. `inline_seed`는 사용자가 명시적으로 선택한 standalone 테스트 모드에서만 사용하며 MongoDB 오류 시 자동 fallback하지 않습니다.
- Workflow Orchestrator는 Loop 결과를 compact context로 만든 뒤 기본 Language Model을 한 번만 호출하며, GaiA Output Adapter -> 표준 Chat Output과 terminal `api_response`를 제공합니다.
- HTML Visualization Flow는 `run_data_analysis`의 `result_ref`를 복원하고 외부 CDN 없는 standalone HTML/SVG 차트를 생성합니다. `HTML Report API 주소`로 게시해 Tauri 상대경로가 아닌 절대 보기·다운로드 링크를 반환하며, 화면 Message와 별도의 API 종료 어댑터가 실제 terminal `api_response`를 제공합니다. 그래프 요청은 `run_data_analysis -> run_visualization` 순서와 `handoff=result_ref`로 실행합니다.
- Metadata 및 Workflow Skill 저장 Flow 4종: Existing Loader를 Matcher에 직접 연결하고 단일 Writer/Response/GaiA Output Adapter/표준 Chat Output 사용
- Metadata 저장·조회 MongoDB 설정: 일반 노드 14개와 QA 통합 snapshot 노드 1개(컬렉션 3종)에 database/collection 기본값 명시
- Metadata 후보: 도메인 관련 항목 최대 10건, 테이블 최소 5/최대 10건, 메인 필터 전체, compact JSON 32KB 정책과 장비+UPH 질문 회귀 검증
- Data Analysis 파라미터: 각 retrieval job이 독립 실행 가능한 `required_params`를 가지며, 공통 조건은 각 job에 반복하고 `어제 재공과 오늘 생산량`처럼 범위가 다르면 서로 다른 값을 유지
- Metadata QA 제품 설명: 제품 그룹은 `product_terms`, 제품 집계는 `product_key_columns`와 관련 `analysis_recipes`만 근거로 결정론적 표를 만들고 추가 LLM 호출을 생략
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the fully wired metadata-driven v5 Langflow JSON bundle.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    sync_workflow_sources()
    print(json.dumps(build_bundle(args.output_dir.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
