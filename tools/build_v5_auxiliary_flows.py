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


@dataclass(frozen=True)
class OrchestratorToolRouteSpec:
    route_name: str
    flow_name: str
    tool_name: str
    tool_description: str
    accepts_upstream_result_ref: bool = False
    can_produce_result_ref: bool = False
    entity_id_columns: str = ""


SAVING_SPECS = [
    SavingSpec("domain", "도메인", "domain_saving_flow", "00_domain_existing_items_loader.py", "00_domain_saving_request_loader.py", "03_domain_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_domain_saving_result_normalizer.py", "05_domain_similarity_checker.py", "07_domain_review_writer.py", "08_domain_saving_response_builder.py", "09_domain_saving_message_adapter.py", "10_domain_saving_api_response_builder.py"),
    SavingSpec("table_catalog", "테이블 카탈로그", "table_catalog_saving_flow", "00_table_catalog_existing_items_loader.py", "00_table_catalog_saving_request_loader.py", "03_table_catalog_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_table_catalog_saving_result_normalizer.py", "05_table_catalog_similarity_checker.py", "07_table_catalog_review_writer.py", "08_table_catalog_saving_response_builder.py", "09_table_catalog_saving_message_adapter.py", "10_table_catalog_saving_api_response_builder.py"),
    SavingSpec("main_flow_filter", "메인 플로우 필터", "main_flow_filters_saving_flow", "00_main_flow_filter_existing_items_loader.py", "00_main_flow_filter_saving_request_loader.py", "03_main_flow_filter_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_main_flow_filter_saving_result_normalizer.py", "05_main_flow_filter_similarity_checker.py", "07_main_flow_filter_review_writer.py", "08_main_flow_filter_saving_response_builder.py", "09_main_flow_filter_saving_message_adapter.py", "10_main_flow_filter_saving_api_response_builder.py"),
    SavingSpec("workflow_skill", "Workflow Skill", "workflow_skill_saving_flow", "00_workflow_skill_existing_items_loader.py", "00_workflow_skill_saving_request_loader.py", "03_workflow_skill_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_workflow_skill_saving_result_normalizer.py", "05_workflow_skill_similarity_checker.py", "07_workflow_skill_review_writer.py", "08_workflow_skill_saving_response_builder.py", "09_workflow_skill_saving_message_adapter.py", "10_workflow_skill_saving_api_response_builder.py"),
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
        "loop": _native_component_prototype(
            by_id["CustomComponent-5o0CN"],
            provider_source,
            _find_component(component_index, "Loop"),
            "LoopComponent",
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


def add_loop_feedback_edge(
    flow: dict[str, Any],
    source: dict[str, Any],
    source_name: str,
    loop: dict[str, Any],
    loop_output_name: str = "item",
) -> None:
    """기본 Loop의 allows_loop 출력으로 돌아가는 전용 feedback edge를 추가합니다.

    Loop feedback의 target handle은 일반 template input이 아니라 Loop의 `item`
    output 계약을 사용합니다. 일반 ``add_edge``로 만들면 Langflow import 시 연결이
    제거되므로 frontend와 같은 output-handle 형식으로 직렬화합니다.
    """

    source_output = next(item for item in source["data"]["node"]["outputs"] if item["name"] == source_name)
    loop_output = next(
        item for item in loop["data"]["node"]["outputs"] if item["name"] == loop_output_name
    )
    if loop_output.get("allows_loop") is not True:
        raise ValueError(f"Loop feedback target must allow loops: {loop['id']}.{loop_output_name}")
    source_types = source_output.get("types") or [source_output.get("selected") or "Data"]
    target_types = loop_output.get("types") or [loop_output.get("selected") or "Data"]
    source_handle = {
        "dataType": source["data"]["type"],
        "id": source["id"],
        "name": source_name,
        "output_types": source_types,
    }
    target_handle = {
        "dataType": loop["data"]["type"],
        "id": loop["id"],
        "name": loop_output_name,
        "output_types": target_types,
    }
    source_text = _source_handle_text(source_handle)
    target_text = _target_handle_text(target_handle)
    flow["data"]["edges"].append(
        {
            "animated": False,
            "className": "",
            "data": {"sourceHandle": source_handle, "targetHandle": target_handle},
            "id": f"xy-edge__{source['id']}{source_text}-{loop['id']}{target_text}",
            "selected": False,
            "source": source["id"],
            "sourceHandle": source_text,
            "target": loop["id"],
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
    # 기존 Metadata 3종은 matcher가 후보 확정 뒤 exact identity만 조회하므로 선행 전체 scan을 생략합니다.
    # Workflow Skill은 사용자가 기존 목록을 확인하고 replace/merge할 수 있도록 제한된 active 목록을 실제 연결합니다.
    _set_value(
        existing_loader["data"]["node"]["template"],
        "limit",
        "500" if spec.slug == "workflow_skill" else "0",
    )
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


# Route V3는 V2와 동일한 하위 Flow를 사용하되, Tool 간 결과 참조를 전달할 수 있는
# capability를 명시합니다. 현재는 Data Analysis만 MongoDB result_ref를 생성·소비하며,
# 향후 이상 LOT Flow를 추가할 때 같은 spec에 LOT_ID capability를 선언하면 됩니다.
ORCHESTRATOR_TOOL_ROUTE_SPECS = [
    OrchestratorToolRouteSpec(
        "data_analysis",
        "metadata_driven_v5_data_analysis_standalone",
        "run_data_analysis",
        "실제 제조 데이터 값의 조회와 계산에 사용합니다. 첫 분석으로 실행할 수도 있고, upstream_result_ref가 있으면 직전 분석 결과를 명시적으로 복원해 연계 분석할 수 있습니다.",
        accepts_upstream_result_ref=True,
        can_produce_result_ref=True,
        entity_id_columns="LOT_ID",
    ),
    OrchestratorToolRouteSpec(
        "metadata_qa",
        "metadata_driven_v5_metadata_qa_standalone",
        "run_metadata_qa",
        "등록된 도메인, 테이블 카탈로그, 필수 파라미터, SQL 템플릿, 컬럼과 계산 규칙을 설명하거나 확인할 때 사용합니다. 다른 Tool 결과 참조를 소비하지 않습니다.",
    ),
    OrchestratorToolRouteSpec(
        "domain_saving",
        "metadata_driven_v5_domain_saving_standalone",
        "save_domain_metadata",
        "사용자가 명시적으로 요청한 도메인 용어, 공정 그룹, 제품 그룹 또는 분석 규칙 저장에만 사용합니다. 한 요청에서 저장 Tool은 최대 한 번만 호출합니다.",
    ),
    OrchestratorToolRouteSpec(
        "table_catalog_saving",
        "metadata_driven_v5_table_catalog_saving_standalone",
        "save_table_catalog_metadata",
        "사용자가 명시적으로 요청한 데이터셋 source type, query template, 필수 파라미터 또는 컬럼 스키마 저장에만 사용합니다.",
    ),
    OrchestratorToolRouteSpec(
        "main_flow_filter_saving",
        "metadata_driven_v5_main_flow_filter_saving_standalone",
        "save_main_flow_filter_metadata",
        "사용자가 명시적으로 요청한 DATE, OPER_NAME, ORG 등 공통 필터 정의 저장에만 사용합니다.",
    ),
]


WORKFLOW_ALLOWED_TOOL_NAMES = [spec.tool_name for spec in ORCHESTRATOR_TOOL_ROUTE_SPECS]
WORKFLOW_REGISTRY_PATH = ROOT / "docs" / "workflows" / "workflow_registry.example.json"


def _workflow_registry_json() -> str:
    """build-time 원본을 읽어 standalone JSON에 내장할 Registry 문자열을 반환합니다."""

    registry = json.loads(WORKFLOW_REGISTRY_PATH.read_text(encoding="utf-8"))
    if registry.get("contract_version") != "workflow.registry.v1":
        raise ValueError("Workflow Registry contract_version must be workflow.registry.v1.")
    return json.dumps(registry, ensure_ascii=False, indent=2)


def _workflow_allowed_tools_json() -> str:
    """Parser와 planner에 동일하게 전달할 허용 Tool 이름 JSON을 반환합니다."""

    return json.dumps(WORKFLOW_ALLOWED_TOOL_NAMES, ensure_ascii=False, indent=2)


def _workflow_planner_prompt() -> str:
    """자연어 요청을 검증 가능한 workflow.plan.v1 JSON으로 바꾸는 기본 Prompt 본문을 만듭니다."""

    return """너는 제조 데이터 Workflow 계획기다.
사용자 요청을 아래 Registry와 허용 Tool만 사용해 `workflow.plan.v1` JSON object 하나로 변환한다.
Markdown code fence, 설명 문장, 주석은 출력하지 않는다.

[사용자 요청]
{user_question}

[필수 규칙]
1. 단계는 최소 1개, 최대 4개다.
2. 요청이 Registry workflow와 의미상 일치하면 등록된 workflow_key와 단계 순서·dependency·handoff를 유지한다.
3. 번호가 있는 자연어 절차나 결합 질문은 필요한 단계만 순서대로 만든다.
4. step_id는 영문자로 시작하는 영문·숫자·밑줄·하이픈만 사용한다.
5. tool_name은 아래 허용 이름 중 하나만 사용한다.
6. depends_on은 반드시 앞 단계 step_id만 참조한다.
7. 첫 단계는 depends_on=[] 및 handoff=none이다.
8. 이전 결과의 실제 대상 집합을 다음 조회에 넘겨야 할 때만 handoff=result_ref를 사용하고 depends_on을 정확히 하나 둔다.
9. 단순 선행 순서만 필요하면 depends_on은 지정하되 handoff=none으로 둔다.
10. 각 단계는 on_error=stop 또는 continue를 명시한다. 기본은 stop이다.
11. 저장 Tool은 사용자가 저장·등록·변경을 명시한 경우에만 선택한다.
12. 없는 Tool이나 Registry 항목을 만들지 않는다.

[출력 형태]
{
  "contract_version": "workflow.plan.v1",
  "workflow_key": "등록 key 또는 inline",
  "title": "짧은 제목",
  "description": "짧은 목적",
  "steps": [
    {
      "step_id": "step_name",
      "tool_name": "run_data_analysis",
      "question": "하위 Flow에 전달할 독립적인 한국어 질문",
      "depends_on": [],
      "handoff": "none",
      "on_error": "stop"
    }
  ]
}

[허용 Tool 이름]
{allowed_tool_names}

[Workflow Registry]
{workflow_registry_json}
"""


def _workflow_final_prompt() -> str:
    """Loop 실행 결과만 근거로 마지막 답변을 한 번 생성하는 기본 Prompt 본문을 반환합니다."""

    return (COMPONENT_ROOT / "route_flow_v4" / "SYSTEM_PROMPT_KO.md").read_text(encoding="utf-8")


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


def build_agent_orchestrator_router_flow(donor: dict[str, Any]) -> dict[str, Any]:
    """최대 네 개 하위 Flow를 순차 호출하고 마지막에 한 번만 답변하는 Route V3를 만듭니다."""

    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        "metadata_driven_v5_agent_orchestrator_router_standalone",
        "LLM Agent orchestrator with compact name-resolved cached Flow tools, explicit result_ref handoff, up to four sequential tool calls, and one final Chat Output.",
        "metadata-driven-v5-agent-orchestrator-router",
        ["v5", "standalone", "agent-orchestrator", "tool-mode", "cached-flow", "multi-tool", "optimized"],
    )
    system_prompt = (COMPONENT_ROOT / "route_flow_v3" / "SYSTEM_PROMPT_KO.md").read_text(encoding="utf-8")
    tool_path = COMPONENT_ROOT / "route_flow_v3" / "01_orchestrated_named_run_flow_tool.py"

    chat = native_node(proto["chat_input"], "ChatInput-agent-orchestrator-router", 0, 0)
    _set_message_storage(chat, True)
    agent = agent_node(proto["agent"], "Agent-agent-orchestrator-router", 850, 0, system_prompt)
    agent_template = agent["data"]["node"]["template"]
    # Tool action 네 번과 마지막 최종 응답 생성을 위한 여유를 둡니다. 실제 Tool 호출 수는
    # V3 system prompt에서 최대 네 번으로 제한하고 반복 호출 회귀 테스트로 확인합니다.
    _set_value(agent_template, "max_iterations", 5)
    _set_value(agent_template, "n_messages", 8)
    _set_value(agent_template, "add_current_date_tool", False)
    _set_value(agent_template, "handle_parsing_errors", True)
    _set_value(agent_template, "verbose", False)
    output = native_node(proto["chat_output"], "ChatOutput-agent-orchestrator-router", 1250, 0)
    _set_message_storage(output, True)
    flow["data"]["nodes"].extend([chat, agent, output])
    add_edge(flow, chat, "message", agent, "input_value")

    y_positions = (-520, -260, 0, 260, 520)
    for spec, y in zip(ORCHESTRATOR_TOOL_ROUTE_SPECS, y_positions, strict=True):
        tool = custom_node(proto["custom"], f"OrchestratedFlowTool-{spec.route_name}", tool_path, 350, y)
        tool_config = tool["data"]["node"]
        tool_config["tool_mode"] = True
        template = tool_config["template"]
        _set_value(template, "flow_name_selected", spec.flow_name)
        _set_value(template, "flow_id_selected", "")
        _set_value(template, "cache_flow", True)
        _set_value(template, "tool_name", spec.tool_name)
        _set_value(template, "tool_description", spec.tool_description)
        _set_value(template, "accepts_upstream_result_ref", spec.accepts_upstream_result_ref)
        _set_value(template, "can_produce_result_ref", spec.can_produce_result_ref)
        _set_value(template, "entity_id_columns", spec.entity_id_columns)
        _set_value(template, "return_direct", False)
        flow["data"]["nodes"].append(tool)
        add_edge(flow, tool, "component_as_tool", agent, "tools")

    add_edge(flow, agent, "response", output, "input_value")
    return flow


def build_workflow_orchestrator_flow(donor: dict[str, Any]) -> dict[str, Any]:
    """계획 LLM과 기본 Loop로 최대 네 단계 Workflow를 결정론적으로 실행하는 Route V4를 만듭니다."""

    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        "metadata_driven_v5_workflow_orchestrator_standalone",
        "Workflow-plan orchestrator with a native planning Language Model, visible standalone registry, native Loop, deterministic exact-tool step executor, compact final synthesis, one Chat Output, and terminal api_response.",
        "metadata-driven-v5-workflow-orchestrator",
        [
            "v5",
            "standalone",
            "workflow-orchestrator",
            "native-loop",
            "cached-flow",
            "multi-tool",
            "optimized",
        ],
    )
    folder = COMPONENT_ROOT / "route_flow_v4"
    tool_path = COMPONENT_ROOT / "route_flow_v3" / "01_orchestrated_named_run_flow_tool.py"
    registry_json = _workflow_registry_json()
    allowed_tools_json = _workflow_allowed_tools_json()

    chat = native_node(proto["chat_input"], "ChatInput-workflow-orchestrator", 0, 0)
    _set_message_storage(chat, True)
    registry_loader = custom_node(
        proto["custom"],
        "WorkflowRegistryLoader-workflow-orchestrator",
        folder / "00a_mongodb_workflow_registry_loader.py",
        340,
        -620,
    )
    registry_loader_template = registry_loader["data"]["node"]["template"]
    _set_value(registry_loader_template, "registry_source", "mongodb")
    _set_value(registry_loader_template, "mongo_database", "datagov")
    _set_value(registry_loader_template, "collection_name", "agent_v4_workflow_skills")
    _set_value(registry_loader_template, "inline_seed_json", registry_json)
    _set_value(registry_loader_template, "status_filter", "active")
    _set_value(registry_loader_template, "max_items", "1000")
    _set_value(registry_loader_template, "candidate_limit", "8")
    _set_value(registry_loader_template, "max_registry_bytes", "65536")
    planner_prompt = prompt_node(
        proto["prompt"],
        "PromptPlanner-workflow-orchestrator",
        _workflow_planner_prompt(),
        340,
        -260,
    )
    planner_prompt_template = planner_prompt["data"]["node"]["template"]
    _set_value(planner_prompt_template, "workflow_registry_json", "{}")
    _set_value(planner_prompt_template, "allowed_tool_names", allowed_tools_json)
    planner_model = language_model_node(
        proto["language_model"],
        "LanguageModelPlanner-workflow-orchestrator",
        680,
        -260,
        "Return exactly one workflow.plan.v1 JSON object. Do not emit markdown, code fences, or prose.",
    )
    parser = custom_node(
        proto["custom"],
        "WorkflowPlanParser-workflow-orchestrator",
        folder / "00_workflow_plan_parser.py",
        1020,
        -260,
    )
    parser_template = parser["data"]["node"]["template"]
    _set_value(parser_template, "workflow_key", "")
    _set_value(parser_template, "workflow_registry_json", "{}")
    _set_value(parser_template, "allowed_tool_names", allowed_tools_json)
    loop = native_node(proto["loop"], "Loop-workflow-orchestrator", 1370, -260)
    executor = custom_node(
        proto["custom"],
        "SequentialStepExecutor-workflow-orchestrator",
        folder / "01_sequential_step_executor.py",
        2050,
        -260,
    )
    _set_value(executor["data"]["node"]["template"], "observation_byte_limit", "8192")
    final_context = custom_node(
        proto["custom"],
        "FinalContext-workflow-orchestrator",
        folder / "02_final_context_builder.py",
        2390,
        -260,
    )
    _set_value(final_context["data"]["node"]["template"], "max_context_bytes", "32768")
    final_prompt = prompt_node(
        proto["prompt"],
        "PromptFinal-workflow-orchestrator",
        _workflow_final_prompt(),
        2730,
        -260,
    )
    final_model = language_model_node(
        proto["language_model"],
        "LanguageModelFinal-workflow-orchestrator",
        3070,
        -260,
        "Synthesize one faithful Korean answer from the validated workflow context only.",
    )
    _set_value(final_model["data"]["node"]["template"], "max_tokens", 4096)
    final_response = custom_node(
        proto["custom"],
        "FinalResponse-workflow-orchestrator",
        folder / "03_workflow_final_response_builder.py",
        3410,
        -260,
    )
    output = native_node(proto["chat_output"], "ChatOutput-workflow-orchestrator", 3760, -360)
    _set_message_storage(output, True)
    flow["data"]["nodes"].extend(
        [
            chat,
            registry_loader,
            planner_prompt,
            planner_model,
            parser,
            loop,
            executor,
            final_context,
            final_prompt,
            final_model,
            final_response,
            output,
        ]
    )

    tool_y_positions = (-920, -660, -400, -140, 120)
    for spec, y in zip(ORCHESTRATOR_TOOL_ROUTE_SPECS, tool_y_positions, strict=True):
        tool = custom_node(proto["custom"], f"WorkflowFlowTool-{spec.route_name}", tool_path, 1710, y)
        tool_config = tool["data"]["node"]
        tool_config["tool_mode"] = True
        template = tool_config["template"]
        _set_value(template, "flow_name_selected", spec.flow_name)
        _set_value(template, "flow_id_selected", "")
        _set_value(template, "cache_flow", True)
        _set_value(template, "tool_name", spec.tool_name)
        _set_value(template, "tool_description", spec.tool_description)
        _set_value(template, "accepts_upstream_result_ref", spec.accepts_upstream_result_ref)
        _set_value(template, "can_produce_result_ref", spec.can_produce_result_ref)
        _set_value(template, "entity_id_columns", spec.entity_id_columns)
        _set_value(template, "return_direct", False)
        flow["data"]["nodes"].append(tool)
        add_edge(flow, tool, "component_as_tool", executor, "tools")

    add_edge(flow, chat, "message", planner_prompt, "user_question")
    add_edge(flow, chat, "message", registry_loader, "user_question")
    add_edge(flow, registry_loader, "workflow_registry_json", planner_prompt, "workflow_registry_json")
    add_edge(flow, planner_prompt, "prompt", planner_model, "input_value")
    add_edge(flow, planner_model, "text_output", parser, "workflow_input")
    add_edge(flow, chat, "message", parser, "user_question")
    add_edge(flow, registry_loader, "workflow_registry_json", parser, "workflow_registry_json")
    add_edge(flow, parser, "loop_dataframe", loop, "data")
    add_edge(flow, loop, "item", executor, "loop_item")
    add_loop_feedback_edge(flow, executor, "step_result", loop, "item")
    add_edge(flow, parser, "workflow_plan", final_context, "execution_context")
    add_edge(flow, loop, "done", final_context, "loop_results")
    add_edge(flow, chat, "message", final_context, "user_question")
    add_edge(flow, final_context, "question", final_prompt, "question")
    add_edge(flow, final_context, "workflow_context", final_prompt, "workflow_context")
    add_edge(flow, final_context, "synthesis_instruction", final_prompt, "synthesis_instruction")
    add_edge(flow, final_prompt, "prompt", final_model, "input_value")
    add_edge(flow, final_context, "final_context", final_response, "final_context")
    add_edge(flow, final_model, "text_output", final_response, "final_model_response")
    add_edge(flow, final_response, "message", output, "input_value")
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
    orchestrator_router = build_agent_orchestrator_router_flow(donor)
    orchestrator_router_path = EXPORT_ROOT / "agent_orchestrator_router_flow_v5_standalone.json"
    orchestrator_router_path.write_bytes((json.dumps(orchestrator_router, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    outputs.append(
        {
            "path": str(orchestrator_router_path),
            "nodes": len(orchestrator_router["data"]["nodes"]),
            "edges": len(orchestrator_router["data"]["edges"]),
        }
    )
    workflow_orchestrator = build_workflow_orchestrator_flow(donor)
    workflow_orchestrator_path = EXPORT_ROOT / "workflow_orchestrator_flow_v5_standalone.json"
    workflow_orchestrator_path.write_bytes(
        (json.dumps(workflow_orchestrator, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    outputs.append(
        {
            "path": str(workflow_orchestrator_path),
            "nodes": len(workflow_orchestrator["data"]["nodes"]),
            "edges": len(workflow_orchestrator["data"]["edges"]),
        }
    )
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Build optimized v5 standalone metadata, Workflow Skill authoring, API router, Agent Tool router, Agent Orchestrator router, and Workflow Orchestrator flows.")
    parser.parse_args()
    print(json.dumps(write_flows(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
