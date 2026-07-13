from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

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


def _edge_keys(flow: dict) -> set[tuple[str, str, str, str]]:
    return {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }


def test_v5_flow_export_is_reproducible_and_acyclic():
    built = build_flow(DEFAULT_SOURCE)
    checked_in = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))

    assert built == checked_in
    assert len(built["data"]["nodes"]) == 42
    assert len(built["data"]["edges"]) == 66
    assert _is_acyclic(built)


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


def test_v5_flow_export_has_one_pandas_execution_and_one_finalization_chain():
    flow = build_flow(DEFAULT_SOURCE)
    edges = _edge_keys(flow)
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}

    assert len(nodes) == 42
    assert len(flow["data"]["edges"]) == 66
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


def test_v5_flow_export_routes_catalog_and_helpers_through_compaction_nodes():
    flow = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    edges = _edge_keys(flow)
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}

    assert ("CustomComponent-HFsYn", "payload_out", "CustomComponent-DXrpf", "payload") in edges
    assert ("CustomComponent-5o0CN", "payload_out", "CustomComponent-v5Hydrate", "payload") in edges
    assert ("MongoDBDomainMetadataLoader-OM3Hg", "table_catalog_items", "CustomComponent-v5Hydrate", "table_catalog_items") in edges
    assert ("CustomComponent-v5Helper", "selected_helper_code", "Prompt Template-xtzD5", "function_case_helper_code") in edges
    assert ("TextInput-AXG9a", "text", "Prompt Template-xtzD5", "function_case_helper_code") not in edges
    hydrate_template = nodes["CustomComponent-v5Hydrate"]["data"]["node"]["template"]
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
    assert len(payload["flows"]) == 7
    assert all(isinstance(flow.get("data"), dict) and flow.get("name") for flow in payload["flows"])
    assert len({flow["endpoint_name"] for flow in payload["flows"]}) == 7
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
        node["data"]["node"]["template"]["flow_name_selected"]["value"].startswith(
            "metadata_driven_v5_complete_20260710_"
        )
        for node in tools
    )


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

    assert collection_values == set(SHARED_V4_COLLECTIONS.values())
    for collection_name in SHARED_V4_COLLECTIONS.values():
        assert collection_name in raw
        assert collection_name.replace("agent_v4_", "agent_v5_") not in raw


def test_v5_child_flows_support_direct_playground_and_native_language_models():
    payload = json.loads(UI_BUNDLE_PATH.read_text(encoding="utf-8"))
    child_flows = [
        flow
        for flow in payload["flows"]
        if not flow["endpoint_name"].endswith(("-api-router", "-agent-tool-router"))
    ]
    assert len(child_flows) == 5

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

    assert len(child_models) == 7
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
        if flow["endpoint_name"].endswith(("-api-router", "-agent-tool-router"))
    ]
    assert len(router_flows) == 2
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
    assert next(node for node in domain["data"]["nodes"] if node["id"] == "ExistingLoader-domain")["data"]["node"]["template"]["limit"]["value"] == "0"
    assert next(node for node in table["data"]["nodes"] if node["id"] == "ExistingLoader-table_catalog")["data"]["node"]["template"]["limit"]["value"] == "0"
    assert next(node for node in main_filter["data"]["nodes"] if node["id"] == "ExistingLoader-main_flow_filter")["data"]["node"]["template"]["limit"]["value"] == "0"


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
