from __future__ import annotations

import ast
import json
from collections import defaultdict, deque
from pathlib import Path

from tools.build_import_ready_bundle import FLOW_DISPLAY_NAMES, FLOW_SPECS
from tools.build_v5_data_analysis_flow import (
    COMPONENT_FILES,
    DEFAULT_SOURCE,
    LANGUAGE_MODEL_NODE_IDS,
    NEW_COMPONENTS,
    PROMPT_FILES,
    REPAIR_PROMPT_NODE_ID,
    REPAIR_PROMPT_SOURCE,
    REMOVED_REPAIR_NODES,
    ROOT,
    build_flow,
)


EXPORT_PATH = ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json"
UI_BUNDLE_PATH = ROOT / "import_ready_flows" / "00_metadata_driven_v5_complete_20260710_ALL_FLOWS.json"
SHARED_V4_COLLECTIONS = {
    "domain": "agent_v4_domain_items",
    "table_catalog": "agent_v4_table_catalog_items",
    "main_flow_filter": "agent_v4_main_flow_filters",
    "result": "agent_v4_result_store",
    "session_state": "agent_v4_session_states",
}
WORKFLOW_SKILL_COLLECTION = "agent_v4_workflow_skills"
EXPECTED_FLOW_DISPLAY_NAMES = [
    "01. v5_data_analysis",
    "02. v5_domain_saving",
    "03. v5_table_catalog_saving",
    "04. v5_main_flow_filter_saving",
    "05. v5_metadata_qa",
    "06. v5_api_router",
    "07. v5_agent_tool_router",
    "08. v5_workflow_orchestrator",
    "09. v5_workflow_skill_saving",
    "10. v5_html_visualization",
]


def _edge_keys(flow: dict) -> set[tuple[str, str, str, str]]:
    return {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"].get("fieldName")
            or edge["data"]["targetHandle"].get("name", ""),
        )
        for edge in flow["data"]["edges"]
    }


def test_v5_auxiliary_builder_uses_numbered_display_names_and_child_targets():
    source = (ROOT / "tools" / "build_v5_auxiliary_flows.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    display_names = ast.literal_eval(assignments["FLOW_DISPLAY_NAMES"])
    assert display_names == {
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
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert 'FLOW_DISPLAY_NAMES[f"{spec.slug}_saving"]' in functions["build_saving_flow"]
    assert 'FLOW_DISPLAY_NAMES["metadata_qa"]' in functions["build_metadata_qa_flow"]
    assert 'FLOW_DISPLAY_NAMES["api_router"]' in functions["build_router_flow"]
    assert 'FLOW_DISPLAY_NAMES["agent_tool_router"]' in functions["build_agent_tool_router_flow"]
    assert 'FLOW_DISPLAY_NAMES["workflow_orchestrator"]' in functions["build_workflow_orchestrator_flow"]
    assert 'FLOW_DISPLAY_NAMES["html_visualization"]' in functions["build_html_visualization_flow"]
    assert "metadata-driven-v5-{spec.slug.replace('_', '-')}-saving" in functions["build_saving_flow"]
    for function_name, endpoint_name in {
        "build_metadata_qa_flow": "metadata-driven-v5-metadata-qa",
        "build_router_flow": "metadata-driven-v5-api-router",
        "build_agent_tool_router_flow": "metadata-driven-v5-agent-tool-router",
        "build_workflow_orchestrator_flow": "metadata-driven-v5-workflow-orchestrator",
        "build_html_visualization_flow": "metadata-driven-v5-html-visualization",
    }.items():
        assert endpoint_name in functions[function_name]

    expected_children = {
        "data_analysis": "01. v5_data_analysis",
        "metadata_qa": "05. v5_metadata_qa",
        "domain_saving": "02. v5_domain_saving",
        "table_catalog_saving": "03. v5_table_catalog_saving",
        "main_flow_filter_saving": "04. v5_main_flow_filter_saving",
    }

    def child_targets(assignment_name: str) -> dict[str, str]:
        targets = {}
        for call in assignments[assignment_name].elts:
            route_name = ast.literal_eval(call.args[0])
            flow_name = call.args[1]
            assert isinstance(flow_name, ast.Subscript)
            assert isinstance(flow_name.value, ast.Name) and flow_name.value.id == "FLOW_DISPLAY_NAMES"
            display_key = ast.literal_eval(flow_name.slice)
            targets[route_name] = display_names[display_key]
        return targets

    assert child_targets("TOOL_ROUTE_SPECS") == expected_children
    assert child_targets("WORKFLOW_TOOL_ROUTE_SPECS") == {
        **expected_children,
        "html_visualization": "10. v5_html_visualization",
    }
    assert ast.literal_eval(assignments["ROUTE_ENDPOINTS"]) == {
        "data_analysis": "metadata-driven-v5-data-analysis",
        "metadata_qa": "metadata-driven-v5-metadata-qa",
        "domain_saving": "metadata-driven-v5-domain-saving",
        "table_catalog_saving": "metadata-driven-v5-table-catalog-saving",
        "main_flow_filter_saving": "metadata-driven-v5-main-flow-filter-saving",
    }


def test_v5_flow_export_is_reproducible_and_acyclic():
    built = build_flow(DEFAULT_SOURCE)
    checked_in = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))

    assert built == checked_in
    assert len(built["data"]["nodes"]) == 43
    assert len(built["data"]["edges"]) == 67
    assert _is_acyclic(built)


def test_custom_structured_terminals_are_explicit_graph_outputs_for_standard_run_flow():
    expected_terminals = {
        "data_analysis_flow_v5_standalone.json": "CustomComponent-3eVde",
        "domain_saving_flow_v5_standalone.json": "Api-domain",
        "table_catalog_saving_flow_v5_standalone.json": "Api-table_catalog",
        "main_flow_filter_saving_flow_v5_standalone.json": "Api-main_flow_filter",
        "metadata_qa_flow_v5_standalone.json": "Api-metadata-qa",
        "workflow_skill_saving_flow_v5_standalone.json": "Api-workflow_skill",
        "html_visualization_flow_v5_standalone.json": "HtmlVisualizationApiTerminal-html-visualization",
    }
    for filename, node_id in expected_terminals.items():
        flow = json.loads((ROOT / "flow_exports" / filename).read_text(encoding="utf-8"))
        nodes = {node["id"]: node for node in flow["data"]["nodes"]}
        terminal = nodes[node_id]["data"]["node"]
        assert terminal["is_output"] is True, filename
        assert "self.is_output = True" in terminal["template"]["code"]["value"], filename
        assert not any(edge["source"] == node_id for edge in flow["data"]["edges"]), filename


def test_structured_graph_output_ownership_stays_in_component_python():
    data_builder = (ROOT / "tools" / "build_v5_data_analysis_flow.py").read_text(encoding="utf-8")
    auxiliary_builder = (ROOT / "tools" / "build_v5_auxiliary_flows.py").read_text(encoding="utf-8")

    assert 'node_index["CustomComponent-3eVde"]["data"]["node"]["is_output"] = True' not in data_builder
    assert "def mark_graph_output(" not in auxiliary_builder
    assert "_declared_component_bool(code, \"is_output\")" in data_builder


def test_v5_bundle_flow_display_names_follow_import_order_without_changing_slugs():
    assert [FLOW_DISPLAY_NAMES[route_name] for _, _, route_name in FLOW_SPECS] == EXPECTED_FLOW_DISPLAY_NAMES
    assert [route_name for _, _, route_name in FLOW_SPECS] == [
        "data_analysis",
        "domain_saving",
        "table_catalog_saving",
        "main_flow_filter_saving",
        "metadata_qa",
        "api_router",
        "agent_tool_router",
        "workflow_orchestrator",
        "workflow_skill_saving",
        "html_visualization",
    ]


def test_v5_flow_export_embeds_current_component_and_prompt_sources():
    flow = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}

    for node_id, relative_path in COMPONENT_FILES.items():
        embedded = nodes[node_id]["data"]["node"]["template"]["code"]["value"]
        source = (ROOT / "langflow_components" / relative_path).read_text(encoding="utf-8")
        assert embedded == source, node_id
    for node_id, spec in NEW_COMPONENTS.items():
        embedded = nodes[node_id]["data"]["node"]["template"]["code"]["value"]
        source = (ROOT / "langflow_components" / spec["file"]).read_text(encoding="utf-8")
        assert embedded == source, node_id
    for node_id in LANGUAGE_MODEL_NODE_IDS.values():
        node = nodes[node_id]
        assert node["data"]["type"] == "LanguageModelComponent"
        assert node["data"]["node"]["metadata"]["module"] == (
            "lfx.components.models_and_agents.language_model.LanguageModelComponent"
        )
    for node_id, relative_path in PROMPT_FILES.items():
        embedded = nodes[node_id]["data"]["node"]["template"]["template"]["value"]
        source = (ROOT / "langflow_components" / "data_analysis_flow" / relative_path).read_text(encoding="utf-8")
        assert embedded == source, node_id
    assert nodes[REPAIR_PROMPT_NODE_ID]["data"]["node"]["template"]["input_value"]["value"] == REPAIR_PROMPT_SOURCE.read_text(encoding="utf-8")
    assert nodes["CustomComponent-s3mf1"]["data"]["node"]["template"]["repair_prompt_template"]["value"] == ""
    assert nodes["CustomComponent-3eVde"]["data"]["node"]["is_output"] is True


def test_v5_flow_export_has_one_pandas_execution_and_one_finalization_chain():
    flow = build_flow(DEFAULT_SOURCE)
    edges = _edge_keys(flow)
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}

    assert len(nodes) == 43
    assert len(flow["data"]["edges"]) == 67
    assert _is_acyclic(flow)
    assert ("CustomComponent-s3mf1", "payload_out", "CustomComponent-AUrFb", "payload") in edges
    assert ("CustomComponent-bhiAG", "payload_out", "CustomComponent-v5ExecutionGate", "payload") in edges
    assert ("CustomComponent-v5ExecutionGate", "payload_out", "CustomComponent-fc0Vb", "payload") in edges
    assert ("CustomComponent-v5ExecutionGate", "payload_out", "CustomComponent-s3mf1", "payload") in edges
    assert ("Prompt Template-AUpQz", "prompt", "LanguageModel-intent", "input_value") in edges
    assert ("LanguageModel-intent", "text_output", "CustomComponent-5o0CN", "llm_response") in edges
    assert ("Prompt Template-xtzD5", "prompt", "LanguageModel-pandas", "input_value") in edges
    assert ("LanguageModel-pandas", "text_output", "CustomComponent-s3mf1", "llm_response") in edges
    assert ("Prompt Template-ELVKc", "prompt", "LanguageModel-answer", "input_value") in edges
    assert ("LanguageModel-answer", "text_output", "CustomComponent-BVItv", "answer_text") in edges
    assert ("CustomComponent-bhiAG", "payload_out", "CustomComponent-fc0Vb", "payload") not in edges
    assert ("CustomComponent-bhiAG", "payload_out", "CustomComponent-s3mf1", "payload") not in edges
    assert ("CustomComponent-v5Helper", "selected_helper_code", "CustomComponent-s3mf1", "function_case_helper_code") in edges
    assert (REPAIR_PROMPT_NODE_ID, "text", "CustomComponent-s3mf1", "repair_prompt_template") in edges
    assert not REMOVED_REPAIR_NODES.intersection(nodes)
    assert "CustomComponent-v5RepairGate" not in nodes
    assert not any(node_id.startswith("CustomComponent-v5Pass") for node_id in nodes)
    assert not any(node_id.startswith("Agent-v5Pass") for node_id in nodes)
    assert not any(node_id.startswith("Prompt Template-v5Pass") for node_id in nodes)
    assert not any(node_id.startswith("ChatOutput-v5Pass") for node_id in nodes)
    assert [node_id for node_id, node in nodes.items() if node["data"].get("type") == "PandasCodeExecutor"] == [
        "CustomComponent-s3mf1"
    ]
    executor_template = nodes["CustomComponent-s3mf1"]["data"]["node"]["template"]
    assert executor_template["function_case_helper_code"]["advanced"] is False
    assert executor_template["repair_prompt_template"]["advanced"] is False
    assert executor_template["repair_prompt_template"]["value"] == ""
    assert executor_template["max_repair_attempts"]["value"] == "1"
    assert executor_template["max_repair_attempts"]["options"] == ["0", "1"]
    assert executor_template["model"]["value"]
    repair_edge = next(
        edge
        for edge in flow["data"]["edges"]
        if edge["source"] == REPAIR_PROMPT_NODE_ID
        and edge["target"] == "CustomComponent-s3mf1"
        and edge["data"]["targetHandle"]["fieldName"] == "repair_prompt_template"
    )
    assert repair_edge["data"]["sourceHandle"]["output_types"] == ["Message"]
    assert repair_edge["data"]["targetHandle"]["inputTypes"] == ["Message"]
    assert [node_id for node_id, node in nodes.items() if node["data"].get("type") == "ChatOutput"] == [
        "ChatOutput-rwbTs"
    ]
    assert not any(
        node["data"].get("type") in {"LoopComponent", "ParserComponent", "ConditionalPromptRequestBuilder"}
        for node in nodes.values()
    )
    direct_chat_nodes = [
        node for node in nodes.values() if node["data"].get("type") in {"ChatInput", "ChatOutput"}
    ]
    assert len(direct_chat_nodes) == 2
    assert all(
        node["data"]["node"]["template"]["should_store_message"]["value"] is True
        for node in direct_chat_nodes
    )
    language_models = [node for node in nodes.values() if node["data"].get("type") == "LanguageModelComponent"]
    assert len(language_models) == 3
    for model in language_models:
        template = model["data"]["node"]["template"]
        assert template["max_tokens"]["value"] == 8192
        assert template["stream"]["value"] is False
        assert template["temperature"]["value"] == 0.1
        assert "tools" not in template
        assert "add_current_date_tool" not in template
        assert "control_payload" not in template
    assert ("CustomComponent-A5y0b", "message", "ChatOutput-rwbTs", "input_value") in edges
    assert ("CustomComponent-fXdS4", "payload_out", "CustomComponent-A5y0b", "payload") in edges
    assert ("CustomComponent-fXdS4", "payload_out", "CustomComponent-3eVde", "payload") in edges
    assert ("CustomComponent-BVItv", "payload_out", "CustomComponent-A5y0b", "payload") not in edges
    assert ("CustomComponent-BVItv", "payload_out", "CustomComponent-3eVde", "payload") not in edges
    answer_adapter_template = nodes["CustomComponent-A5y0b"]["data"]["node"]["template"]
    assert answer_adapter_template["show_pandas_code"]["value"] is True


def test_v5_flow_export_routes_catalog_and_helpers_through_compaction_nodes():
    flow = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    edges = _edge_keys(flow)
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}

    assert ("CustomComponent-HFsYn", "payload_out", "CustomComponent-DXrpf", "payload") in edges
    assert ("CustomComponent-5o0CN", "payload_out", "CustomComponent-v5Hydrate", "payload") in edges
    assert ("MongoDBDomainMetadataLoader-OM3Hg", "table_catalog_items", "CustomComponent-v5Hydrate", "table_catalog_items") in edges
    assert ("CustomComponent-v5Hydrate", "payload_out", "CustomComponent-O8vfz", "payload") in edges
    assert ("CustomComponent-O8vfz", "payload_out", "CustomComponent-v5UpstreamBinder", "payload") in edges
    assert ("CustomComponent-v5UpstreamBinder", "payload_out", "CustomComponent-vVkhs", "payload") in edges
    assert ("CustomComponent-O8vfz", "payload_out", "CustomComponent-vVkhs", "payload") not in edges
    assert ("CustomComponent-v5Helper", "selected_helper_code", "Prompt Template-xtzD5", "function_case_helper_code") in edges
    assert ("TextInput-AXG9a", "text", "Prompt Template-xtzD5", "function_case_helper_code") not in edges
    hydrate_template = nodes["CustomComponent-v5Hydrate"]["data"]["node"]["template"]
    request_loader_template = nodes["CustomComponent-xpbhS"]["data"]["node"]["template"]
    upstream_binder = nodes["CustomComponent-v5UpstreamBinder"]["data"]["node"]
    router_template = nodes["CustomComponent-x6NXu"]["data"]["node"]["template"]
    candidate_node = nodes["CustomComponent-DXrpf"]["data"]["node"]
    candidate_template = candidate_node["template"]
    assert hydrate_template["retrieval_mode"]["value"] == "dummy"
    assert hydrate_template["retrieval_mode"]["options"] == ["dummy", "live"]
    assert "execution_mode" not in hydrate_template
    assert "retrieval_mode" not in router_template
    assert nodes["CustomComponent-v5Hydrate"]["data"]["node"]["field_order"] == [
        "payload",
        "table_catalog_items",
        "retrieval_mode",
    ]
    assert nodes["CustomComponent-x6NXu"]["data"]["node"]["field_order"] == ["payload"]
    assert request_loader_template["upstream_result_ref"]["value"] == ""
    assert request_loader_template["upstream_result_ref"]["advanced"] is False
    assert upstream_binder["field_order"] == ["payload"]
    assert candidate_node["field_order"] == [
        "payload",
        "domain_items",
        "table_catalog_items",
        "main_flow_filters",
        "max_domain_items",
        "min_table_items",
        "max_table_items",
        "max_bytes",
    ]
    assert candidate_template["max_domain_items"]["value"] == "10"
    assert candidate_template["min_table_items"]["value"] == "5"
    assert candidate_template["max_table_items"]["value"] == "10"
    assert candidate_template["max_bytes"]["value"] == "32768"
    assert all(
        candidate_template[name]["advanced"] is True
        for name in ("max_domain_items", "min_table_items", "max_table_items", "max_bytes")
    )
    assert "max_items" not in candidate_template


def test_v5_flow_export_binds_standalone_mongo_inputs_and_shared_v4_collections():
    flow = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    mongo_fields = []
    collection_values = []
    for node in flow["data"]["nodes"]:
        template = node.get("data", {}).get("node", {}).get("template", {})
        for field_name, field in template.items():
            if not isinstance(field, dict):
                continue
            if field_name == "mongo_uri":
                mongo_fields.append(field)
            if "collection" in field_name and isinstance(field.get("value"), str):
                collection_values.append(field["value"])

    assert mongo_fields
    assert all(
        field.get("value") == "MONGO_URL"
        and field.get("load_from_db") is True
        and field.get("advanced") is False
        for field in mongo_fields
    )
    assert collection_values
    assert set(collection_values) == set(SHARED_V4_COLLECTIONS.values())
    assert "agent_v5_" not in json.dumps(flow, ensure_ascii=False)

    standalone_sources = (
        ROOT / "langflow_components" / "data_analysis_flow" / "01a_mongodb_domain_metadata_loader.py",
        ROOT / "langflow_components" / "data_analysis_flow" / "01b_mongodb_table_catalog_loader.py",
        ROOT / "langflow_components" / "data_analysis_flow" / "01c_mongodb_main_variable_loader.py",
        ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py",
        ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py",
        ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py",
        ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py",
    )
    assert all('os.getenv("MONGODB' not in path.read_text(encoding="utf-8") for path in standalone_sources)


def test_v5_single_file_ui_bundle_is_bomless_json_with_all_flows():
    raw = UI_BUNDLE_PATH.read_bytes()
    assert raw.startswith(b'{"flows":[')
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    payload = json.loads(raw.decode("utf-8"))
    assert len(payload["flows"]) == 10
    assert all(isinstance(flow.get("data"), dict) and flow.get("name") for flow in payload["flows"])
    assert [flow["name"] for flow in payload["flows"]] == EXPECTED_FLOW_DISPLAY_NAMES
    assert len({flow["endpoint_name"] for flow in payload["flows"]}) == 10
    assert all("-dummy-" not in flow["endpoint_name"] for flow in payload["flows"])
    assert not list(UI_BUNDLE_PATH.parent.glob("*_dummy_*_flow_v5_standalone.json"))
    router = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-api-router"))
    callers = [node for node in router["data"]["nodes"] if str(node.get("id") or "").startswith("ApiCaller-")]
    assert len(callers) == 5
    assert all(node["data"]["node"]["template"]["read_timeout_seconds"]["value"] == "240" for node in callers)
    smart_router = next(node for node in router["data"]["nodes"] if node["id"] == "SmartRouter-api-router")
    assert smart_router["data"]["node"]["base_classes"] == []
    chat_input_edges = [edge for edge in router["data"]["edges"] if edge["source"] == "ChatInput-api-router"]
    assert len(chat_input_edges) == 1
    assert chat_input_edges[0]["target"] == "SmartRouter-api-router"
    assert not any(
        edge["data"]["targetHandle"]["fieldName"] == "session_source"
        for edge in router["data"]["edges"]
    )
    assert not any(node["data"]["node"].get("display_name") == "Run Flow" for node in router["data"]["nodes"])
    assert not any(str(node.get("id") or "").startswith("FinalGate-") for node in router["data"]["nodes"])
    direct_edges = {
        (edge["data"]["sourceHandle"]["name"], edge["target"])
        for edge in router["data"]["edges"]
        if edge["source"] == "SmartRouter-api-router" and edge["target"].startswith("ChatOutput-")
    }
    assert direct_edges == {
        ("category_6_result", "ChatOutput-direct_answer"),
        ("category_7_result", "ChatOutput-clarification"),
    }

    tool_router = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-agent-tool-router"))
    tools = [node for node in tool_router["data"]["nodes"] if str(node.get("id") or "").startswith("CachedFlowTool-")]
    assert len(tools) == 5
    assert len([node for node in tool_router["data"]["nodes"] if node["data"].get("type") == "ChatOutput"]) == 1
    assert all(node["data"]["node"]["template"]["cache_flow"]["value"] is True for node in tools)
    assert all(node["data"]["node"]["template"]["return_direct"]["value"] is True for node in tools)
    assert all(node["data"]["node"]["template"]["flow_id_selected"]["value"] == "" for node in tools)
    assert all("session_source" not in node["data"]["node"]["template"] for node in tools)
    assert all(
        'runtime_user_id = str(getattr(self, "user_id"' in node["data"]["node"]["template"]["code"]["value"]
        and "self.user_id =" not in node["data"]["node"]["template"]["code"]["value"]
        and "UUID(requested_flow_id)" not in node["data"]["node"]["template"]["code"]["value"]
        and "def _chat_output_target" in node["data"]["node"]["template"]["code"]["value"]
        and "def _promote_graph_output" in node["data"]["node"]["template"]["code"]["value"]
        for node in tools
    )
    tool_chat_edges = [
        edge for edge in tool_router["data"]["edges"] if edge["source"] == "ChatInput-agent-tool-router"
    ]
    assert len(tool_chat_edges) == 1
    assert tool_chat_edges[0]["target"] == "Agent-agent-tool-router"
    assert not any(
        edge["data"]["targetHandle"]["fieldName"] == "session_source"
        for edge in tool_router["data"]["edges"]
    )
    assert all(
        node["data"]["node"]["template"]["flow_name_selected"]["value"]
        == FLOW_DISPLAY_NAMES[node["id"].removeprefix("CachedFlowTool-")]
        for node in tools
    )

    individual_flows = sorted(UI_BUNDLE_PATH.parent.glob("[0-9][0-9]_*_v5_standalone.json"))
    assert [path.name[:2] for path in individual_flows] == [f"{index:02d}" for index in range(1, 11)]
    assert individual_flows[-1].name == "10_html_visualization_flow_v5_standalone.json"
    manifest = json.loads((UI_BUNDLE_PATH.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["flow_count"] == 10
    assert [item["order"] for item in manifest["flows"]] == list(range(1, 11))
    assert [item["name"] for item in manifest["flows"]] == EXPECTED_FLOW_DISPLAY_NAMES
    assert manifest["flows"][-1]["file"] == "10_html_visualization_flow_v5_standalone.json"


def test_v5_bundle_route_v4_uses_native_loop_exact_tools_and_one_terminal_answer():
    payload = json.loads(UI_BUNDLE_PATH.read_text(encoding="utf-8"))
    flow = next(
        item for item in payload["flows"] if item["endpoint_name"].endswith("-workflow-orchestrator")
    )
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edges = _edge_keys(flow)

    assert flow["name"] == "08. v5_workflow_orchestrator"
    assert flow["endpoint_name"] == "metadata-driven-v5-complete-20260710-workflow-orchestrator"
    assert len(nodes) == 18
    assert len(flow["data"]["edges"]) == 26
    assert not any(node["data"].get("type") == "Agent" for node in nodes.values())
    assert len([node for node in nodes.values() if node["data"].get("type") == "LoopComponent"]) == 1
    assert len([node for node in nodes.values() if node["data"].get("type") == "LanguageModelComponent"]) == 2
    assert len([node for node in nodes.values() if node["data"].get("type") == "ChatOutput"]) == 1

    component_sources = {
        "WorkflowRegistryLoader-workflow-orchestrator": "route_flow_v4/00a_mongodb_workflow_registry_loader.py",
        "WorkflowPlanParser-workflow-orchestrator": "route_flow_v4/00_workflow_plan_parser.py",
        "SequentialStepExecutor-workflow-orchestrator": "route_flow_v4/01_sequential_step_executor.py",
        "FinalContext-workflow-orchestrator": "route_flow_v4/02_final_context_builder.py",
        "FinalResponse-workflow-orchestrator": "route_flow_v4/03_workflow_final_response_builder.py",
    }
    for node_id, relative_path in component_sources.items():
        expected = (ROOT / "langflow_components" / relative_path).read_text(encoding="utf-8")
        assert nodes[node_id]["data"]["node"]["template"]["code"]["value"] == expected

    tool_source = (
        ROOT / "langflow_components" / "route_flow_v4" / "04_workflow_named_run_flow_tool.py"
    ).read_text(encoding="utf-8")
    tools = [node for node_id, node in nodes.items() if node_id.startswith("WorkflowFlowTool-")]
    assert len(tools) == 6
    assert all(node["data"]["node"]["template"]["code"]["value"] == tool_source for node in tools)
    assert all(node["data"]["node"]["template"]["return_direct"]["value"] is False for node in tools)
    assert all(node["data"]["node"]["template"]["cache_flow"]["value"] is True for node in tools)
    assert all(node["data"]["node"]["template"]["flow_id_selected"]["value"] == "" for node in tools)
    assert all(
        node["data"]["node"]["template"]["preferred_output_names"]["value"] == "api_response"
        for node in tools
    )
    assert all(
        node["data"]["node"]["template"]["flow_name_selected"]["value"]
        == FLOW_DISPLAY_NAMES[node["id"].removeprefix("WorkflowFlowTool-")]
        for node in tools
    )
    assert all(
        (node["id"], "component_as_tool", "SequentialStepExecutor-workflow-orchestrator", "tools")
        in edges
        for node in tools
    )
    visualization_tool = nodes["WorkflowFlowTool-html_visualization"]
    visualization_template = visualization_tool["data"]["node"]["template"]
    assert visualization_template["tool_name"]["value"] == "run_visualization"
    assert visualization_template["accepts_upstream_result_ref"]["value"] is True
    assert visualization_template["can_produce_result_ref"]["value"] is False

    registry_template = nodes["WorkflowRegistryLoader-workflow-orchestrator"]["data"]["node"]["template"]
    assert registry_template["registry_source"]["value"] == "mongodb"
    assert registry_template["mongo_uri"]["value"] == "MONGO_URL"
    assert registry_template["mongo_database"]["value"] == "datagov"
    assert registry_template["collection_name"]["value"] == WORKFLOW_SKILL_COLLECTION
    assert registry_template["candidate_limit"]["value"] == "8"
    assert registry_template["max_registry_bytes"]["value"] == "65536"
    planner_template = nodes["PromptPlanner-workflow-orchestrator"]["data"]["node"]["template"]
    assert planner_template["workflow_registry_json"]["value"] == "{}"
    tool_catalog = json.loads(planner_template["allowed_tool_catalog"]["value"])
    catalog_by_name = {item["tool_name"]: item for item in tool_catalog}
    assert set(catalog_by_name) == {
        "run_data_analysis",
        "run_metadata_qa",
        "save_domain_metadata",
        "save_table_catalog_metadata",
        "save_main_flow_filter_metadata",
        "run_visualization",
    }
    assert catalog_by_name["run_data_analysis"]["can_produce_result_ref"] is True
    assert catalog_by_name["run_data_analysis"]["requires_upstream_result_ref"] is False
    assert catalog_by_name["run_visualization"]["accepts_upstream_result_ref"] is True
    assert catalog_by_name["run_visualization"]["can_produce_result_ref"] is False
    assert catalog_by_name["run_visualization"]["requires_upstream_result_ref"] is True
    assert "{allowed_tool_catalog}" in planner_template["template"]["value"]
    parser_template = nodes["WorkflowPlanParser-workflow-orchestrator"]["data"]["node"]["template"]
    assert parser_template["workflow_registry_json"]["value"] == "{}"
    assert json.loads(parser_template["tool_capabilities_json"]["value"]) == tool_catalog
    assert "handoff는 현재 단계가 앞 단계의 결과를 입력으로 받는지" in planner_template["template"]["value"]
    assert (
        "ChatInput-workflow-orchestrator",
        "message",
        "WorkflowRegistryLoader-workflow-orchestrator",
        "user_question",
    ) in edges
    assert (
        "WorkflowRegistryLoader-workflow-orchestrator",
        "workflow_registry_json",
        "PromptPlanner-workflow-orchestrator",
        "workflow_registry_json",
    ) in edges
    assert (
        "WorkflowRegistryLoader-workflow-orchestrator",
        "workflow_registry_json",
        "WorkflowPlanParser-workflow-orchestrator",
        "workflow_registry_json",
    ) in edges

    assert (
        "WorkflowPlanParser-workflow-orchestrator",
        "loop_dataframe",
        "Loop-workflow-orchestrator",
        "data",
    ) in edges
    assert (
        "Loop-workflow-orchestrator",
        "item",
        "SequentialStepExecutor-workflow-orchestrator",
        "loop_item",
    ) in edges
    assert (
        "SequentialStepExecutor-workflow-orchestrator",
        "step_result",
        "Loop-workflow-orchestrator",
        "item",
    ) in edges
    assert (
        "WorkflowPlanParser-workflow-orchestrator",
        "workflow_plan",
        "FinalContext-workflow-orchestrator",
        "execution_context",
    ) in edges
    assert (
        "Loop-workflow-orchestrator",
        "done",
        "FinalContext-workflow-orchestrator",
        "loop_results",
    ) in edges
    assert (
        "FinalResponse-workflow-orchestrator",
        "message",
        "ChatOutput-workflow-orchestrator",
        "input_value",
    ) in edges
    assert not any(
        source == "FinalResponse-workflow-orchestrator" and output_name == "api_response"
        for source, output_name, _target, _field in edges
    )

    final_response_outputs = {
        output["name"] for output in nodes["FinalResponse-workflow-orchestrator"]["data"]["node"]["outputs"]
    }
    assert final_response_outputs == {"message", "api_response"}
    assert nodes["FinalContext-workflow-orchestrator"]["data"]["node"]["template"]["execution_context"]["advanced"] is False
    assert nodes["LanguageModelPlanner-workflow-orchestrator"]["data"]["node"]["template"]["max_tokens"]["value"] == 8192
    assert nodes["LanguageModelFinal-workflow-orchestrator"]["data"]["node"]["template"]["max_tokens"]["value"] == 4096
    assert all(
        "tools" not in node["data"]["node"]["template"]
        for node in nodes.values()
        if node["data"].get("type") == "LanguageModelComponent"
    )


def test_v5_bundle_html_visualization_has_result_ref_input_and_terminal_api_response():
    payload = json.loads(UI_BUNDLE_PATH.read_text(encoding="utf-8"))
    flow = next(item for item in payload["flows"] if item["endpoint_name"].endswith("-html-visualization"))
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    edges = _edge_keys(flow)

    assert flow["name"] == "10. v5_html_visualization"
    assert flow["endpoint_name"] == "metadata-driven-v5-complete-20260710-html-visualization"
    assert set(nodes) == {
        "ChatInput-html-visualization",
        "HtmlVisualizationBuilder-html-visualization",
        "ChatOutput-html-visualization",
        "HtmlVisualizationApiTerminal-html-visualization",
    }
    assert edges == {
        (
            "ChatInput-html-visualization",
            "message",
            "HtmlVisualizationBuilder-html-visualization",
            "question",
        ),
        (
            "HtmlVisualizationBuilder-html-visualization",
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
    builder = nodes["HtmlVisualizationBuilder-html-visualization"]
    template = builder["data"]["node"]["template"]
    assert template["code"]["value"] == (
        ROOT / "langflow_components" / "visualization_flow" / "00_html_visualization_builder.py"
    ).read_text(encoding="utf-8")
    assert template["upstream_result_ref"]["advanced"] is False
    assert template["mongo_uri"]["value"] == "MONGO_URL"
    assert template["mongo_uri"]["load_from_db"] is True
    assert template["mongo_database"]["value"] == "datagov"
    assert template["collection_name"]["value"] == "agent_v4_result_store"
    assert template["report_api_url"]["value"] == "http://127.0.0.1:8010"
    assert template["report_api_url"]["advanced"] is False
    assert template["report_ttl_hours"]["value"] == "24"
    assert template["report_ttl_hours"]["advanced"] is False
    assert {output["name"] for output in builder["data"]["node"]["outputs"]} == {"message", "api_response"}
    terminal = nodes["HtmlVisualizationApiTerminal-html-visualization"]
    terminal_template = terminal["data"]["node"]["template"]
    assert terminal_template["code"]["value"] == (
        ROOT / "langflow_components" / "visualization_flow" / "01_html_visualization_api_terminal.py"
    ).read_text(encoding="utf-8")
    assert {output["name"] for output in terminal["data"]["node"]["outputs"]} == {"api_response"}
    assert not any(source == terminal["id"] for source, _handle, _target, _field in edges)
    assert nodes["ChatInput-html-visualization"]["data"]["node"]["template"]["should_store_message"]["value"] is True
    assert nodes["ChatOutput-html-visualization"]["data"]["node"]["template"]["should_store_message"]["value"] is True


def test_v5_single_file_ui_bundle_uses_exact_shared_v4_collection_mappings():
    raw = UI_BUNDLE_PATH.read_text(encoding="utf-8")
    payload = json.loads(raw)
    collection_values = {
        field["value"]
        for flow in payload["flows"]
        for node in flow["data"]["nodes"]
        for field_name, field in node.get("data", {}).get("node", {}).get("template", {}).items()
        if "collection" in field_name
        and isinstance(field, dict)
        and isinstance(field.get("value"), str)
        and field["value"]
    }

    all_collections = {*SHARED_V4_COLLECTIONS.values(), WORKFLOW_SKILL_COLLECTION}
    assert collection_values == all_collections
    for collection_name in all_collections:
        assert collection_name in raw
        assert collection_name.replace("agent_v4_", "agent_v5_") not in raw


def test_v5_child_flows_support_direct_playground_and_native_language_models():
    payload = json.loads(UI_BUNDLE_PATH.read_text(encoding="utf-8"))
    child_flows = [
        flow
        for flow in payload["flows"]
        if not flow["endpoint_name"].endswith(
            ("-api-router", "-agent-tool-router", "-workflow-orchestrator")
        )
    ]
    assert len(child_flows) == 7

    child_models = []
    for flow in child_flows:
        chat_nodes = [
            node
            for node in flow["data"]["nodes"]
            if node["data"].get("type") in {"ChatInput", "ChatOutput"}
        ]
        assert chat_nodes
        # Child 기본값은 direct Playground 질문/답변 표시를 위해 저장을 켭니다.
        # Router nested 호출은 caller/tool tweak에서만 child 저장을 끕니다.
        assert all(node["data"]["node"]["template"]["should_store_message"]["value"] is True for node in chat_nodes)
        child_models.extend(
            node
            for node in flow["data"]["nodes"]
            if node["data"].get("type") == "LanguageModelComponent"
        )

    assert len(child_models) == 8
    for node in child_models:
        template = node["data"]["node"]["template"]
        assert template["max_tokens"]["value"] == 8192
        assert template["stream"]["value"] is False
        assert template["temperature"]["value"] == 0.1
        assert "tools" not in template
        assert "add_current_date_tool" not in template
        assert "n_messages" not in template
        assert "max_iterations" not in template

    router_flows = [
        flow
        for flow in payload["flows"]
        if flow["endpoint_name"].endswith(
            ("-api-router", "-agent-tool-router", "-workflow-orchestrator")
        )
    ]
    assert len(router_flows) == 3
    for flow in router_flows:
        router_chat_nodes = [
            node
            for node in flow["data"]["nodes"]
            if node["data"].get("type") in {"ChatInput", "ChatOutput"}
        ]
        assert router_chat_nodes
        assert all(
            node["data"]["node"]["template"]["should_store_message"]["value"] is True
            for node in router_chat_nodes
        )


def test_v5_result_store_limits_qa_request_guards_and_targeted_existing_loaders_are_wired():
    payload = json.loads(UI_BUNDLE_PATH.read_text(encoding="utf-8"))
    data_analysis = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-data-analysis"))
    result_store = next(node for node in data_analysis["data"]["nodes"] if node["id"] == "CustomComponent-AUrFb")
    result_template = result_store["data"]["node"]["template"]
    assert result_template["max_result_rows"]["value"] == "20000"
    assert result_template["max_source_rows_per_alias"]["value"] == "10000"
    assert result_template["max_document_bytes"]["value"] == "8388608"
    assert result_template["mongo_uri"]["value"] == "MONGO_URL"
    assert result_template["mongo_uri"]["load_from_db"] is True
    assert result_template["mongo_uri"]["advanced"] is False
    assert result_template["mongo_database"]["value"] == "datagov"
    assert result_template["collection_name"]["value"] == "agent_v4_result_store"
    assert all(
        result_template[name]["advanced"] is True
        for name in ("max_result_rows", "max_source_rows_per_alias", "max_document_bytes")
    )

    metadata_qa = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-metadata-qa"))
    qa_edges = _edge_keys(metadata_qa)
    qa_nodes = {node["id"]: node for node in metadata_qa["data"]["nodes"]}
    assert ("Request-metadata-qa", "payload_out", "SnapshotLoader-metadata-qa", "request_payload") in qa_edges
    assert ("Prompt-metadata-qa", "prompt", "LanguageModel-metadata-qa", "input_value") in qa_edges
    assert ("LanguageModel-metadata-qa", "text_output", "Normalizer-metadata-qa", "llm_response") in qa_edges
    assert qa_nodes["SnapshotLoader-metadata-qa"]["data"]["node"]["template"]["cache_ttl_seconds"]["value"] == "15"
    assert qa_nodes["LanguageModel-metadata-qa"]["data"]["type"] == "LanguageModelComponent"
    assert len([node for node in qa_nodes.values() if node["data"].get("type") == "ChatOutput"]) == 1

    domain = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-domain-saving"))
    table = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-table-catalog-saving"))
    main_filter = next(flow for flow in payload["flows"] if flow["endpoint_name"].endswith("-main-flow-filter-saving"))
    for flow, loader_id, matcher_id in (
        (domain, "ExistingLoader-domain", "Matcher-domain"),
        (table, "ExistingLoader-table_catalog", "Matcher-table_catalog"),
        (main_filter, "ExistingLoader-main_flow_filter", "Matcher-main_flow_filter"),
    ):
        assert loader_id not in {node["id"] for node in flow["data"]["nodes"]}
        assert "existing_items" not in flow["data"]["nodes"][[node["id"] for node in flow["data"]["nodes"]].index(matcher_id)]["data"]["node"]["template"]
        assert all(edge["target"] != matcher_id or edge["data"]["targetHandle"].get("fieldName") != "existing_items" for edge in flow["data"]["edges"])


def test_v5_single_file_ui_bundle_handles_parse_with_langflow_frontend_codec():
    payload = json.loads(UI_BUNDLE_PATH.read_text(encoding="utf-8"))
    validated_handle_count = 0

    for flow in payload["flows"]:
        for edge in flow["data"]["edges"]:
            for handle_name in ("sourceHandle", "targetHandle"):
                handle_text = edge[handle_name]
                assert "┇" not in handle_text
                assert json.loads(handle_text.replace("œ", '"')) == edge["data"][handle_name]
                validated_handle_count += 1

    assert validated_handle_count > 0
    manifest = json.loads((UI_BUNDLE_PATH.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["validation"]["langflow_frontend_edge_handles"] == (
        f"{validated_handle_count}/{validated_handle_count} parsed and matched edge.data"
    )


def _is_acyclic(flow: dict) -> bool:
    node_ids = {node["id"] for node in flow["data"]["nodes"]}
    graph: dict[str, set[str]] = defaultdict(set)
    indegree = {node_id: 0 for node_id in node_ids}
    for edge in flow["data"]["edges"]:
        source, target = edge["source"], edge["target"]
        if target in graph[source]:
            continue
        graph[source].add(target)
        indegree[target] += 1
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in graph[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return visited == len(node_ids)
