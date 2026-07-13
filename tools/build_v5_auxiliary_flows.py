from __future__ import annotations

import argparse
import json
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lfx.custom.utils import create_component_template


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components"
EXPORT_ROOT = ROOT / "flow_exports"
DONOR_PATH = EXPORT_ROOT / "data_analysis_flow_v5_standalone.json"
COMPONENT_INDEX = Path.home() / "AppData" / "Local" / "com.LangflowDesktop" / ".langflow-venv" / "Lib" / "site-packages" / "lfx" / "_assets" / "component_index.json"
ROUTER_READ_TIMEOUT_SECONDS = "240"
MONGO_GLOBAL_VARIABLE = "MONGO_URL"


@dataclass(frozen=True)
class SavingSpec:
    slug: str
    label: str
    folder: str
    existing_loader: str
    request: str
    variables: str
    prompt: str
    normalizer: str
    matcher: str
    writer: str
    response: str
    message: str
    api: str


@dataclass(frozen=True)
class ToolRouteSpec:
    route_name: str
    flow_name: str
    tool_name: str
    tool_description: str


SAVING_SPECS = [
    SavingSpec("domain", "도메인", "domain_saving_flow", "00_domain_existing_items_loader.py", "00_domain_saving_request_loader.py", "03_domain_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_domain_saving_result_normalizer.py", "05_domain_similarity_checker.py", "07_domain_review_writer.py", "08_domain_saving_response_builder.py", "09_domain_saving_message_adapter.py", "10_domain_saving_api_response_builder.py"),
    SavingSpec("table_catalog", "테이블 카탈로그", "table_catalog_saving_flow", "00_table_catalog_existing_items_loader.py", "00_table_catalog_saving_request_loader.py", "03_table_catalog_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_table_catalog_saving_result_normalizer.py", "05_table_catalog_similarity_checker.py", "07_table_catalog_review_writer.py", "08_table_catalog_saving_response_builder.py", "09_table_catalog_saving_message_adapter.py", "10_table_catalog_saving_api_response_builder.py"),
    SavingSpec("main_flow_filter", "메인 플로우 필터", "main_flow_filters_saving_flow", "00_main_flow_filter_existing_items_loader.py", "00_main_flow_filter_saving_request_loader.py", "03_main_flow_filter_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_main_flow_filter_saving_result_normalizer.py", "05_main_flow_filter_similarity_checker.py", "07_main_flow_filter_review_writer.py", "08_main_flow_filter_saving_response_builder.py", "09_main_flow_filter_saving_message_adapter.py", "10_main_flow_filter_saving_api_response_builder.py"),
]


def load_donor() -> dict[str, Any]:
    return json.loads(DONOR_PATH.read_text(encoding="utf-8"))


def prototypes(donor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_id = {node["id"]: node for node in donor["data"]["nodes"]}
    provider_source = by_id.get("LanguageModel-intent") or by_id.get("Agent-mevnw")
    if provider_source is None:
        raise RuntimeError("Data Analysis donor does not contain a model provider source")
    component_index = json.loads(COMPONENT_INDEX.read_text(encoding="utf-8"))
    return {
        "custom": by_id["CustomComponent-5o0CN"],
        "prompt": by_id["Prompt Template-AUpQz"],
        "agent": _native_component_prototype(
            by_id["CustomComponent-5o0CN"],
            provider_source,
            _find_component(component_index, "Agent"),
            "Agent",
        ),
        "language_model": _native_component_prototype(
            by_id["CustomComponent-5o0CN"],
            provider_source,
            _find_component(component_index, "Language Model"),
            "LanguageModelComponent",
        ),
        "chat_input": by_id["ChatInput-Xs7uo"],
        "chat_output": by_id["ChatOutput-rwbTs"],
    }


def _native_component_prototype(
    shell: dict[str, Any],
    provider_source: dict[str, Any],
    component_config: dict[str, Any],
    node_type: str,
) -> dict[str, Any]:
    """기본 LFX 컴포넌트와 기존 standalone provider 선택값을 결합합니다."""

    if not component_config:
        raise RuntimeError(f"Native component template not found: {node_type}")
    node = deepcopy(shell)
    config = deepcopy(component_config)
    config["lf_version"] = "1.8.2"
    source_template = provider_source["data"]["node"]["template"]
    for field_name in ("model", "api_key"):
        source_field = source_template.get(field_name)
        target_field = config.get("template", {}).get(field_name)
        if not isinstance(source_field, dict) or not isinstance(target_field, dict):
            continue
        for attribute in ("value", "load_from_db", "advanced", "show"):
            if attribute in source_field:
                target_field[attribute] = deepcopy(source_field[attribute])
    node["data"]["type"] = node_type
    node["data"]["node"] = config
    return node


def empty_flow(donor: dict[str, Any], name: str, description: str, endpoint: str, tags: list[str]) -> dict[str, Any]:
    flow = deepcopy(donor)
    flow["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"metadata-driven-v5/{name}"))
    flow["name"] = name
    flow["description"] = description
    flow["endpoint_name"] = endpoint
    flow["tags"] = tags
    flow["last_tested_version"] = "1.8.2"
    flow["data"] = {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 0.55}}
    return flow


def custom_node(proto: dict[str, Any], node_id: str, path: Path, x: float, y: float) -> dict[str, Any]:
    code = path.read_text(encoding="utf-8")
    config, instance = create_component_template({"code": code, "output_types": []}, module_name=f"v5_auxiliary.{path.stem}")
    config["lf_version"] = "1.8.2"
    node = _clone_node(proto, node_id, x, y)
    node["data"]["type"] = instance.__class__.__name__
    node["data"]["node"] = config
    _apply_standalone_mongo_inputs(node)
    return node


def _apply_standalone_mongo_inputs(node: dict[str, Any]) -> None:
    """MongoDB 연결값을 OS 환경변수 대신 Langflow 노드 입력으로 직렬화합니다."""

    template = node.get("data", {}).get("node", {}).get("template", {})
    mongo_uri = template.get("mongo_uri") if isinstance(template, dict) else None
    if not isinstance(mongo_uri, dict):
        return
    mongo_uri["value"] = MONGO_GLOBAL_VARIABLE
    mongo_uri["load_from_db"] = True
    mongo_uri["advanced"] = False
    mongo_uri["show"] = True
    for field_name in (
        "mongo_database",
        "collection_name",
        "session_collection_name",
        "domain_collection_name",
        "table_collection_name",
        "filter_collection_name",
    ):
        field = template.get(field_name)
        if isinstance(field, dict):
            field["load_from_db"] = False
            field["advanced"] = False
            field["show"] = True


def prompt_node(proto: dict[str, Any], node_id: str, prompt_text: str, x: float, y: float) -> dict[str, Any]:
    node = _clone_node(proto, node_id, x, y)
    config = node["data"]["node"]
    config["template"]["template"]["value"] = prompt_text
    dynamic_template = deepcopy(config["template"].get("question"))
    keep = {"_type", "code", "template", "use_double_brackets", "tool_placeholder"}
    for key in list(config["template"]):
        if key not in keep:
            config["template"].pop(key, None)
    for variable in _prompt_variables(prompt_text):
        field = deepcopy(dynamic_template)
        field.update({"name": variable, "display_name": variable, "value": "", "required": True})
        config["template"][variable] = field
    return node


def agent_node(proto: dict[str, Any], node_id: str, x: float, y: float, system_prompt: str) -> dict[str, Any]:
    node = _clone_node(proto, node_id, x, y)
    template = node["data"]["node"]["template"]
    _set_value(template, "api_key", "")
    _set_value(template, "system_prompt", system_prompt)
    # 실제 Tool이 연결되는 Router Agent만 이 factory를 사용합니다.
    _set_value(template, "n_messages", 0)
    _set_value(template, "max_iterations", 1)
    _set_value(template, "add_current_date_tool", False)
    _set_value(template, "max_tokens", 8192)
    _set_value(template, "verbose", False)
    _set_value(template, "tools", "")
    return node


def language_model_node(
    proto: dict[str, Any],
    node_id: str,
    x: float,
    y: float,
    system_message: str,
) -> dict[str, Any]:
    """Tool schema를 전송하지 않는 Langflow 기본 Language Model 노드를 만듭니다."""

    node = _clone_node(proto, node_id, x, y)
    template = node["data"]["node"]["template"]
    _set_value(template, "api_key", "")
    _set_value(template, "system_message", system_message)
    _set_value(template, "stream", False)
    _set_value(template, "temperature", 0.1)
    _set_value(template, "max_tokens", 8192)
    return node


def native_node(proto: dict[str, Any], node_id: str, x: float, y: float) -> dict[str, Any]:
    return _clone_node(proto, node_id, x, y)


def _set_message_storage(node: dict[str, Any], enabled: bool) -> None:
    """ChatInput/ChatOutput의 Langflow message DB 저장 여부를 명시적으로 설정합니다."""
    template = node.get("data", {}).get("node", {}).get("template", {})
    _set_value(template, "should_store_message", enabled)


def _clone_node(proto: dict[str, Any], node_id: str, x: float, y: float) -> dict[str, Any]:
    node = deepcopy(proto)
    node["id"] = node_id
    node["data"]["id"] = node_id
    node["position"] = {"x": x, "y": y}
    node["selected"] = False
    node["dragging"] = False
    return node


def _set_value(template: dict[str, Any], field_name: str, value: Any) -> None:
    if isinstance(template.get(field_name), dict):
        template[field_name]["value"] = value


def _prompt_variables(text: str) -> list[str]:
    result = []
    for match in re.finditer(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})", text):
        if match.group(1) not in result:
            result.append(match.group(1))
    return result


def add_edge(flow: dict[str, Any], source: dict[str, Any], source_name: str, target: dict[str, Any], target_name: str) -> None:
    source_output = next(item for item in source["data"]["node"]["outputs"] if item["name"] == source_name)
    target_input = target["data"]["node"]["template"][target_name]
    output_types = source_output.get("types") or [source_output.get("selected") or "Data"]
    input_types = target_input.get("input_types") or (["Message"] if target_input.get("type") == "str" else ["Data"])
    source_handle = {"dataType": source["data"]["type"], "id": source["id"], "name": source_name, "output_types": output_types}
    target_handle = {"fieldName": target_name, "id": target["id"], "inputTypes": input_types, "type": target_input.get("type") or "other"}
    source_text = _source_handle_text(source_handle)
    target_text = _target_handle_text(target_handle)
    flow["data"]["edges"].append(
        {
            "animated": False,
            "className": "",
            "data": {"sourceHandle": source_handle, "targetHandle": target_handle},
            "id": f"xy-edge__{source['id']}{source_text}-{target['id']}{target_text}",
            "selected": False,
            "source": source["id"],
            "sourceHandle": source_text,
            "target": target["id"],
            "targetHandle": target_text,
        }
    )


def _source_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _target_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _handle_text(value: dict[str, Any]) -> str:
    """Mirror Langflow frontend's stable stringify + quote substitution."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace('"', "œ")


def build_saving_flow(donor: dict[str, Any], spec: SavingSpec) -> dict[str, Any]:
    proto = prototypes(donor)
    flow = empty_flow(donor, f"metadata_driven_v5_{spec.slug}_saving_standalone", f"Optimized {spec.label} metadata saving flow: one extraction LLM, existing-item MongoDB loading plus candidate matching, one deterministic writer for dry-run/live execution, and one compact response terminal.", f"metadata-driven-v5-{spec.slug.replace('_', '-')}-saving", ["v5", "standalone", "metadata-authoring", "optimized"])
    folder = COMPONENT_ROOT / spec.folder
    nodes: dict[str, dict[str, Any]] = {}

    def add(name: str, node: dict[str, Any]) -> dict[str, Any]:
        nodes[name] = node
        flow["data"]["nodes"].append(node)
        return node

    chat = add("chat", native_node(proto["chat_input"], f"ChatInput-{spec.slug}", 0, 0))
    _set_message_storage(chat, True)
    request = add("request", custom_node(proto["custom"], f"Request-{spec.slug}", folder / spec.request, 320, 0))
    _set_value(request["data"]["node"]["template"], "dry_run", True)
    duplicate_action = request["data"]["node"]["template"].get("duplicate_action")
    if isinstance(duplicate_action, dict):
        duplicate_action["options"] = ["skip", "merge", "replace", "create_new"]
        duplicate_action["value"] = "skip"
    variables = add("variables", custom_node(proto["custom"], f"Variables-{spec.slug}", folder / spec.variables, 650, 0))
    extraction_prompt_text = (folder / spec.prompt).read_text(encoding="utf-8")
    extraction_prompt = add("extract_prompt", prompt_node(proto["prompt"], f"PromptExtract-{spec.slug}", extraction_prompt_text, 950, 0))
    extraction_model = add(
        "extract_model",
        language_model_node(
            proto["language_model"],
            f"LanguageModelExtract-{spec.slug}",
            1250,
            0,
            "Return only the JSON object requested by the prompt. Do not add markdown or prose.",
        ),
    )
    normalizer = add("normalizer", custom_node(proto["custom"], f"Normalizer-{spec.slug}", folder / spec.normalizer, 1550, 0))
    existing_loader = add("existing_loader", custom_node(proto["custom"], f"ExistingLoader-{spec.slug}", folder / spec.existing_loader, 1550, 340))
    # 세 matcher 모두 생성 후보가 정해진 뒤 exact key 또는 section/key/alias 후보만 조회하므로 선행 전체 scan을 생략합니다.
    _set_value(existing_loader["data"]["node"]["template"], "limit", "0")
    matcher = add("matcher", custom_node(proto["custom"], f"Matcher-{spec.slug}", folder / spec.matcher, 1850, 0))
    writer = add("writer", custom_node(proto["custom"], f"Writer-{spec.slug}", folder / spec.writer, 2150, 0))
    response = add("response", custom_node(proto["custom"], f"Response-{spec.slug}", folder / spec.response, 2450, 0))
    message = add("message", custom_node(proto["custom"], f"Message-{spec.slug}", folder / spec.message, 2750, -100))
    api = add("api", custom_node(proto["custom"], f"Api-{spec.slug}", folder / spec.api, 3050, 100))
    output = add("chat_output", native_node(proto["chat_output"], f"ChatOutput-{spec.slug}", 3050, -180))
    _set_message_storage(output, True)

    add_edge(flow, chat, "message", request, "raw_text")
    add_edge(flow, request, "payload_out", variables, "payload")
    add_edge(flow, variables, "source_text", extraction_prompt, "source_text")
    add_edge(flow, extraction_prompt, "prompt", extraction_model, "input_value")
    add_edge(flow, request, "payload_out", normalizer, "payload")
    add_edge(flow, extraction_model, "text_output", normalizer, "llm_response")
    add_edge(flow, normalizer, "payload_out", matcher, "payload")
    add_edge(flow, existing_loader, "existing_items", matcher, "existing_items")
    add_edge(flow, matcher, "payload_out", writer, "payload")
    add_edge(flow, writer, "payload_out", response, "payload")
    add_edge(flow, response, "payload_out", message, "payload")
    add_edge(flow, response, "payload_out", api, "payload")
    add_edge(flow, message, "message", api, "display_message")
    add_edge(flow, message, "message", output, "input_value")
    return flow


def build_metadata_qa_flow(donor: dict[str, Any]) -> dict[str, Any]:
    proto = prototypes(donor)
    flow = empty_flow(donor, "metadata_driven_v5_metadata_qa_standalone", "Metadata QA flow with MongoDB projection, mode-specific compact LLM context, SQL-on-demand, byte limit, deterministic fallback, and canonical API response.", "metadata-driven-v5-metadata-qa", ["v5", "standalone", "metadata-qa", "optimized"])
    folder = COMPONENT_ROOT / "metadata_qa_flow"
    nodes: dict[str, dict[str, Any]] = {}

    def add(name: str, node: dict[str, Any]) -> dict[str, Any]:
        nodes[name] = node
        flow["data"]["nodes"].append(node)
        return node

    chat = add("chat", native_node(proto["chat_input"], "ChatInput-metadata-qa", 0, 0))
    _set_message_storage(chat, True)
    request = add("request", custom_node(proto["custom"], "Request-metadata-qa", folder / "00_metadata_qa_request_loader.py", 320, 0))
    snapshot = add("snapshot", custom_node(proto["custom"], "SnapshotLoader-metadata-qa", folder / "01_mongodb_metadata_snapshot_loader.py", 650, 320))
    context = add("context", custom_node(proto["custom"], "Context-metadata-qa", folder / "02_metadata_qa_context_builder.py", 980, 0))
    _set_value(context["data"]["node"]["template"], "max_items", "50")
    _set_value(context["data"]["node"]["template"], "max_bytes", "65536")
    variables = add("variables", custom_node(proto["custom"], "Variables-metadata-qa", folder / "03_metadata_qa_variables_builder.py", 1280, 0))
    prompt_text = (folder / "03_metadata_qa_prompt_template_ko.md").read_text(encoding="utf-8")
    prompt = add("prompt", prompt_node(proto["prompt"], "Prompt-metadata-qa", prompt_text, 1580, 0))
    model = add(
        "model",
        language_model_node(
            proto["language_model"],
            "LanguageModel-metadata-qa",
            1880,
            0,
            "Answer only from the supplied metadata context and return the requested JSON object.",
        ),
    )
    normalizer = add("normalizer", custom_node(proto["custom"], "Normalizer-metadata-qa", folder / "04_metadata_qa_response_normalizer.py", 2180, 0))
    message = add("message", custom_node(proto["custom"], "Message-metadata-qa", folder / "05_metadata_qa_message_adapter.py", 2480, -100))
    api = add("api", custom_node(proto["custom"], "Api-metadata-qa", folder / "06_metadata_qa_api_response_builder.py", 2780, 100))
    output = add("output", native_node(proto["chat_output"], "ChatOutput-metadata-qa", 2780, -160))
    _set_message_storage(output, True)

    add_edge(flow, chat, "message", request, "question")
    # 통합 snapshot loader는 빈 질문을 MongoDB 연결 전에 차단하고 cache miss에도 MongoClient를 한 번만 생성합니다.
    add_edge(flow, request, "payload_out", snapshot, "request_payload")
    add_edge(flow, request, "payload_out", context, "payload")
    add_edge(flow, snapshot, "domain_items", context, "domain_items")
    add_edge(flow, snapshot, "table_catalog_items", context, "table_catalog_items")
    add_edge(flow, snapshot, "main_flow_filters", context, "main_flow_filters")
    add_edge(flow, context, "payload_out", variables, "payload")
    for output_name in ("question", "metadata_context_json", "output_schema_json"):
        add_edge(flow, variables, output_name, prompt, output_name)
    add_edge(flow, prompt, "prompt", model, "input_value")
    add_edge(flow, context, "payload_out", normalizer, "payload")
    add_edge(flow, model, "text_output", normalizer, "llm_response")
    add_edge(flow, normalizer, "payload_out", message, "payload")
    add_edge(flow, normalizer, "payload_out", api, "payload")
    add_edge(flow, message, "message", api, "display_message")
    add_edge(flow, message, "message", output, "input_value")
    return flow


ROUTES = [
    ("data_analysis", "생산량, 재공, 투입, 장비 ASSIGN 등 실제 제조 데이터 조회 또는 분석 질문"),
    ("metadata_qa", "등록된 데이터셋, 필수 파라미터, SQL, 도메인 용어, 계산 로직 확인 질문"),
    ("domain_saving", "도메인 용어, 공정 그룹, 제품 그룹, 분석 규칙 저장 요청"),
    ("table_catalog_saving", "데이터셋, source type, query template, 필수 파라미터, 컬럼 저장 요청"),
    ("main_flow_filter_saving", "DATE, OPER_NAME, ORG 같은 공통 필터 정의 저장 요청"),
]


ROUTE_ENDPOINTS = {
    "data_analysis": "metadata-driven-v5-data-analysis",
    "metadata_qa": "metadata-driven-v5-metadata-qa",
    "domain_saving": "metadata-driven-v5-domain-saving",
    "table_catalog_saving": "metadata-driven-v5-table-catalog-saving",
    "main_flow_filter_saving": "metadata-driven-v5-main-flow-filter-saving",
}


TOOL_ROUTE_SPECS = [
    ToolRouteSpec(
        "data_analysis",
        "metadata_driven_v5_data_analysis_standalone",
        "run_data_analysis",
        "실제 제조 데이터 값의 조회와 계산에 사용합니다. 생산량, 재공, 투입/산출, HOLD, 장비 배정, UPH, 제품별 집계와 비교 질문이 대상입니다. 메타데이터 정의 설명이나 등록 요청에는 사용하지 않습니다.",
    ),
    ToolRouteSpec(
        "metadata_qa",
        "metadata_driven_v5_metadata_qa_standalone",
        "run_metadata_qa",
        "등록된 도메인, 테이블 카탈로그, 필수 파라미터, SQL 템플릿, 컬럼과 계산 규칙을 설명하거나 확인할 때 사용합니다. 실제 생산 수치 조회나 메타데이터 저장에는 사용하지 않습니다.",
    ),
    ToolRouteSpec(
        "domain_saving",
        "metadata_driven_v5_domain_saving_standalone",
        "save_domain_metadata",
        "도메인 용어, 별칭, 공정 그룹, 제품 그룹, 분석 규칙을 신규 저장하거나 유사 기존 항목에 merge/replace하라는 명시적 등록 요청에 사용합니다.",
    ),
    ToolRouteSpec(
        "table_catalog_saving",
        "metadata_driven_v5_table_catalog_saving_standalone",
        "save_table_catalog_metadata",
        "데이터셋 또는 테이블의 source type, query template, 필수 파라미터, 컬럼 스키마를 등록하거나 변경하라는 명시적 요청에 사용합니다.",
    ),
    ToolRouteSpec(
        "main_flow_filter_saving",
        "metadata_driven_v5_main_flow_filter_saving_standalone",
        "save_main_flow_filter_metadata",
        "DATE, OPER_NAME, ORG 등 분석 전반에 공통으로 적용할 메인 필터 정의를 등록하거나 변경하라는 명시적 요청에 사용합니다.",
    ),
]


def _find_component(config: Any, display_name: str) -> dict[str, Any]:
    if isinstance(config, dict):
        if config.get("display_name") == display_name and isinstance(config.get("template"), dict):
            return deepcopy(config)
        for value in config.values():
            result = _find_component(value, display_name)
            if result:
                return result
    elif isinstance(config, list):
        for value in config:
            result = _find_component(value, display_name)
            if result:
                return result
    return {}


def smart_router_node(proto: dict[str, Any], agent_proto: dict[str, Any], node_id: str, routes: list[dict[str, Any]], x: float, y: float) -> dict[str, Any]:
    index = json.loads(COMPONENT_INDEX.read_text(encoding="utf-8"))
    config = _find_component(index, "Smart Router")
    if not config:
        raise RuntimeError("Smart Router component template not found")
    config["template"]["model"]["value"] = deepcopy(agent_proto["data"]["node"]["template"]["model"]["value"])
    config["template"]["api_key"]["value"] = ""
    config["template"]["routes"]["value"] = routes
    config["template"]["enable_else_output"]["value"] = False
    config["template"]["custom_prompt"]["value"] = "저장 요청과 조회 요청을 엄격히 구분한다. 실제 데이터 값 질문은 data_analysis, metadata 정의와 SQL 설명 질문은 metadata_qa로 분류한다."
    output_proto = deepcopy(agent_proto["data"]["node"]["outputs"][0])
    config["outputs"] = []
    for index_value, route in enumerate(routes, start=1):
        output = deepcopy(output_proto)
        output.update({"name": f"category_{index_value}_result", "display_name": route["route_category"], "method": "process_case", "group_outputs": True, "types": ["Message"], "selected": "Message"})
        config["outputs"].append(output)
    # Match the working legacy Smart Router export. Declaring Message here makes
    # the router behave like a message terminal during repeated graph builds.
    config["base_classes"] = []
    node = _clone_node(proto, node_id, x, y)
    node["data"]["type"] = "SmartRouter"
    node["data"]["node"] = config
    return node


def build_router_flow(donor: dict[str, Any]) -> dict[str, Any]:
    proto = prototypes(donor)
    flow = empty_flow(donor, "metadata_driven_v5_api_router_standalone", "Smart Router classification with per-branch Langflow Run API calls, shared session propagation, pooled HTTP connections, secret API keys, and structured status outputs.", "metadata-driven-v5-api-router", ["v5", "standalone", "api-router", "optimized"])
    routes = [{"route_category": name, "route_description": description, "output_value": ""} for name, description in ROUTES]
    routes.extend(
        [
            {"route_category": "direct_answer", "route_description": "인사 또는 기능 안내처럼 하위 flow가 필요 없는 요청", "output_value": "안녕하세요. 제조 데이터 분석, 메타데이터 조회와 등록을 도와드릴 수 있습니다."},
            {"route_category": "clarification", "route_description": "요청 목적이나 대상이 모호해 추가 설명이 필요한 경우", "output_value": "요청할 데이터, 메타데이터 종류 또는 저장하려는 내용을 조금 더 구체적으로 알려주세요."},
        ]
    )
    chat = native_node(proto["chat_input"], "ChatInput-api-router", 0, 0)
    _set_message_storage(chat, True)
    router = smart_router_node(proto["custom"], proto["agent"], "SmartRouter-api-router", routes, 350, 0)
    flow["data"]["nodes"].extend([chat, router])
    add_edge(flow, chat, "message", router, "input_text")

    caller_path = COMPONENT_ROOT / "route_flow" / "01_flow_api_message_caller.py"
    for index_value, (route_name, _) in enumerate(ROUTES, start=1):
        y = (index_value - 1) * 260 - 1100
        caller = custom_node(proto["custom"], f"ApiCaller-{route_name}", caller_path, 800, y)
        template = caller["data"]["node"]["template"]
        _set_value(template, "route_name", route_name)
        _set_value(template, "api_url", f"/api/v1/run/{ROUTE_ENDPOINTS[route_name]}")
        _set_value(template, "read_timeout_seconds", ROUTER_READ_TIMEOUT_SECONDS)
        output = native_node(proto["chat_output"], f"ChatOutput-{route_name}", 1180, y)
        _set_message_storage(output, True)
        flow["data"]["nodes"].extend([caller, output])
        add_edge(flow, router, f"category_{index_value}_result", caller, "flow_input")
        add_edge(flow, caller, "message", output, "input_value")

    for offset, route_index in enumerate((len(ROUTES) + 1, len(ROUTES) + 2)):
        route_name = routes[route_index - 1]["route_category"]
        y = 1600 + offset * 260
        output = native_node(proto["chat_output"], f"ChatOutput-{route_name}", 1180, y)
        _set_message_storage(output, True)
        flow["data"]["nodes"].append(output)
        add_edge(flow, router, f"category_{route_index}_result", output, "input_value")
    return flow


def build_agent_tool_router_flow(donor: dict[str, Any]) -> dict[str, Any]:
    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        "metadata_driven_v5_agent_tool_router_standalone",
        "LLM Agent router with five compact name-resolved cached Flow tools, shared session propagation, direct child responses, and one final Chat Output.",
        "metadata-driven-v5-agent-tool-router",
        ["v5", "standalone", "agent-router", "tool-mode", "cached-flow", "optimized"],
    )
    system_prompt = (COMPONENT_ROOT / "route_flow_v2" / "SYSTEM_PROMPT_KO.md").read_text(encoding="utf-8")
    tool_path = COMPONENT_ROOT / "route_flow_v2" / "01_cached_named_run_flow_tool.py"

    chat = native_node(proto["chat_input"], "ChatInput-agent-tool-router", 0, 0)
    _set_message_storage(chat, True)
    agent = agent_node(proto["agent"], "Agent-agent-tool-router", 850, 0, system_prompt)
    agent_template = agent["data"]["node"]["template"]
    _set_value(agent_template, "max_iterations", 3)
    _set_value(agent_template, "n_messages", 6)
    _set_value(agent_template, "add_current_date_tool", False)
    _set_value(agent_template, "handle_parsing_errors", True)
    _set_value(agent_template, "verbose", False)
    output = native_node(proto["chat_output"], "ChatOutput-agent-tool-router", 1250, 0)
    _set_message_storage(output, True)
    flow["data"]["nodes"].extend([chat, agent, output])
    add_edge(flow, chat, "message", agent, "input_value")

    y_positions = (-520, -260, 0, 260, 520)
    for spec, y in zip(TOOL_ROUTE_SPECS, y_positions, strict=True):
        tool = custom_node(proto["custom"], f"CachedFlowTool-{spec.route_name}", tool_path, 350, y)
        tool_config = tool["data"]["node"]
        tool_config["tool_mode"] = True
        template = tool_config["template"]
        _set_value(template, "flow_name_selected", spec.flow_name)
        _set_value(template, "flow_id_selected", "")
        _set_value(template, "cache_flow", True)
        _set_value(template, "tool_name", spec.tool_name)
        _set_value(template, "tool_description", spec.tool_description)
        _set_value(template, "return_direct", True)
        flow["data"]["nodes"].append(tool)
        add_edge(flow, tool, "component_as_tool", agent, "tools")

    add_edge(flow, agent, "response", output, "input_value")
    return flow


def write_flows() -> list[dict[str, Any]]:
    donor = load_donor()
    outputs = []
    for spec in SAVING_SPECS:
        flow = build_saving_flow(donor, spec)
        path = EXPORT_ROOT / f"{spec.slug}_saving_flow_v5_standalone.json"
        path.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        outputs.append({"path": str(path), "nodes": len(flow["data"]["nodes"]), "edges": len(flow["data"]["edges"])})
    qa = build_metadata_qa_flow(donor)
    qa_path = EXPORT_ROOT / "metadata_qa_flow_v5_standalone.json"
    qa_path.write_bytes((json.dumps(qa, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    outputs.append({"path": str(qa_path), "nodes": len(qa["data"]["nodes"]), "edges": len(qa["data"]["edges"])})
    router = build_router_flow(donor)
    router_path = EXPORT_ROOT / "api_router_flow_v5_standalone.json"
    router_path.write_bytes((json.dumps(router, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    outputs.append({"path": str(router_path), "nodes": len(router["data"]["nodes"]), "edges": len(router["data"]["edges"])})
    tool_router = build_agent_tool_router_flow(donor)
    tool_router_path = EXPORT_ROOT / "agent_tool_router_flow_v5_standalone.json"
    tool_router_path.write_bytes((json.dumps(tool_router, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    outputs.append(
        {
            "path": str(tool_router_path),
            "nodes": len(tool_router["data"]["nodes"]),
            "edges": len(tool_router["data"]["edges"]),
        }
    )
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Build optimized v5 standalone metadata, API router, and Agent Tool router flows.")
    parser.parse_args()
    print(json.dumps(write_flows(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
