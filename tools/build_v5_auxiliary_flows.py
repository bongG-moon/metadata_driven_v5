from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from lfx.custom.utils import create_component_template


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components"
EXPORT_ROOT = ROOT / "flow_exports"
# The maintained V2 donor provides the audited Langflow 1.11.0 native node
# templates.  The retired V1 export is deliberately not a build dependency.
DONOR_PATH = ROOT / "tools" / "assets" / "data_analysis_flow_v2_donor.json"
ROUTER_READ_TIMEOUT_SECONDS = "240"
MONGO_GLOBAL_VARIABLE = "MONGO_URL"
TARGET_LANGFLOW_VERSION = "1.11.0"
DEFAULT_LANGUAGE_MODEL = "gemini-3.5-flash-lite"
FLOW_DISPLAY_NAMES = {
    # Stable external target. The import bundle binds this name to the V2
    # hybrid graph; router Tool names and client contracts do not change.
    "data_analysis": "01. v5_data_analysis",
    "domain_saving": "02. v5_domain_saving",
    "table_catalog_saving": "03. v5_table_catalog_saving",
    "main_flow_filter_saving": "04. v5_main_flow_filter_saving",
    "metadata_qa": "05. v5_metadata_qa",
    "agent_tool_router": "06. v5_agent_tool_router",
    "realtime_production_report_legacy": "07. v5_realtime_production_report_legacy",
    "realtime_production_report": "07-1. v5_realtime_production_report",
    "report_followup": "07-2. v5_report_followup",
}


def _resolve_component_index() -> Path:
    """실행 중인 LFX 패키지의 기본 컴포넌트 인덱스를 우선 사용합니다."""

    spec = find_spec("lfx")
    candidates: list[Path] = []
    explicit_index = str(os.getenv("LANGFLOW_COMPONENT_INDEX_PATH") or "").strip()
    if explicit_index:
        candidates.append(Path(explicit_index).expanduser().resolve())
    if spec is not None and spec.origin:
        candidates.append(Path(spec.origin).resolve().parent / "_assets" / "component_index.json")
    candidates.append(
        Path.home()
        / "AppData"
        / "Local"
        / "com.LangflowDesktop"
        / ".langflow-venv"
        / "Lib"
        / "site-packages"
        / "lfx"
        / "_assets"
        / "component_index.json"
    )
    component_index = next((path for path in candidates if path.exists()), None)
    if component_index is None:
        raise RuntimeError("실행 중인 Langflow/LFX의 component_index.json을 찾을 수 없습니다.")
    return component_index


COMPONENT_INDEX = _resolve_component_index()


@dataclass(frozen=True)
class SavingSpec:
    slug: str
    label: str
    folder: str
    existing_loader: str | None
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
    required_all_keywords: str = ""
    required_any_phrases: str = ""
    keyword_gate_message: str = ""



SAVING_SPECS = [
    SavingSpec("domain", "도메인", "domain_saving_flow", None, "00_domain_saving_request_loader.py", "03_domain_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_domain_saving_result_normalizer.py", "05_domain_similarity_checker.py", "07_domain_review_writer.py", "08_domain_saving_response_builder.py", "09_domain_saving_message_adapter.py", "10_domain_saving_api_response_builder.py"),
    SavingSpec("table_catalog", "테이블 카탈로그", "table_catalog_saving_flow", None, "00_table_catalog_saving_request_loader.py", "03_table_catalog_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_table_catalog_saving_result_normalizer.py", "05_table_catalog_similarity_checker.py", "07_table_catalog_review_writer.py", "08_table_catalog_saving_response_builder.py", "09_table_catalog_saving_message_adapter.py", "10_table_catalog_saving_api_response_builder.py"),
    SavingSpec("main_flow_filter", "메인 플로우 필터", "main_flow_filters_saving_flow", None, "00_main_flow_filter_saving_request_loader.py", "03_main_flow_filter_saving_variables_builder.py", "03_saving_prompt_template_ko.md", "04_main_flow_filter_saving_result_normalizer.py", "05_main_flow_filter_similarity_checker.py", "07_main_flow_filter_review_writer.py", "08_main_flow_filter_saving_response_builder.py", "09_main_flow_filter_saving_message_adapter.py", "10_main_flow_filter_saving_api_response_builder.py"),
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
        "prompt": _native_component_prototype(
            by_id["CustomComponent-5o0CN"],
            provider_source,
            _find_component(component_index, "Prompt Template"),
            "Prompt Template",
        ),
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
    config["lf_version"] = TARGET_LANGFLOW_VERSION
    source_template = provider_source["data"]["node"]["template"]
    for field_name in ("model", "api_key"):
        source_field = source_template.get(field_name)
        target_field = config.get("template", {}).get(field_name)
        if not isinstance(source_field, dict) or not isinstance(target_field, dict):
            continue
        for attribute in ("value", "load_from_db", "advanced", "show"):
            if attribute in source_field:
                target_field[attribute] = deepcopy(source_field[attribute])
    _set_default_language_model(config.get("template", {}))
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
    flow["last_tested_version"] = TARGET_LANGFLOW_VERSION
    flow["data"] = {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 0.55}}
    return flow


def custom_node(proto: dict[str, Any], node_id: str, path: Path, x: float, y: float) -> dict[str, Any]:
    code = path.read_text(encoding="utf-8")
    config, instance = create_component_template({"code": code, "output_types": []}, module_name=f"v5_auxiliary.{path.stem}")
    config["lf_version"] = TARGET_LANGFLOW_VERSION
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
    dynamic_template = deepcopy(config["template"].get("tool_placeholder"))
    if not isinstance(dynamic_template, dict):
        raise RuntimeError("Langflow 1.11 Prompt Template dynamic input prototype is missing.")
    keep = {"_type", "code", "template", "use_double_brackets", "tool_placeholder"}
    for key in list(config["template"]):
        if key not in keep:
            config["template"].pop(key, None)
    config["custom_fields"] = {"template": []}
    for variable in _prompt_variables(prompt_text):
        field = deepcopy(dynamic_template)
        field.update(
            {
                "name": variable,
                "display_name": variable,
                "value": "",
                "required": True,
                "tool_mode": False,
                "advanced": False,
                "show": True,
            }
        )
        config["template"][variable] = field
        config["custom_fields"]["template"].append(variable)
    return node


def agent_node(proto: dict[str, Any], node_id: str, x: float, y: float, system_prompt: str) -> dict[str, Any]:
    node = _clone_node(proto, node_id, x, y)
    _configure_agent_template(node["data"]["node"]["template"], system_prompt)
    return node


def silent_router_agent_node(
    proto: dict[str, Any],
    node_id: str,
    path: Path,
    x: float,
    y: float,
    system_prompt: str,
) -> dict[str, Any]:
    """Build the Router-specific Agent that suppresses nested child Flow events."""

    node = custom_node(proto, node_id, path, x, y)
    _configure_agent_template(node["data"]["node"]["template"], system_prompt)
    return node


def _configure_agent_template(template: dict[str, Any], system_prompt: str) -> None:
    """Apply the stable Router Agent defaults to native and custom Agent templates."""

    _set_default_language_model(template)
    _set_value(template, "api_key", "")
    _set_value(template, "system_prompt", system_prompt)
    # 실제 Tool이 연결되는 Router Agent만 이 factory를 사용합니다.
    _set_value(template, "n_messages", 0)
    _set_value(template, "max_iterations", 1)
    _set_value(template, "add_current_date_tool", False)
    _set_value(template, "add_calculator_tool", False)
    _set_value(template, "max_tokens", 8192)
    _set_value(template, "verbose", False)
    _set_value(template, "tools", "")


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
    _set_default_language_model(template)
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


def _set_default_language_model(template: dict[str, Any]) -> None:
    """Set the selected model for native Language Model/Agent templates."""

    model = template.get("model")
    if not isinstance(model, dict) or model.get("type") != "model":
        return
    value = model.get("value")
    selected = deepcopy(value[0]) if isinstance(value, list) and value and isinstance(value[0], dict) else {}
    selected["name"] = DEFAULT_LANGUAGE_MODEL
    selected.setdefault("icon", "GoogleGenerativeAI")
    selected.setdefault("provider", "Google Generative AI")
    selected.setdefault(
        "metadata",
        {
            "api_key_param": "google_api_key",
            "context_length": 128000,
            "max_tokens_field_name": "max_output_tokens",
            "model_class": "ChatGoogleGenerativeAIFixed",
            "model_name_param": "model",
        },
    )
    model["value"] = [selected]
    options = model.get("options")
    if isinstance(options, list) and not any(
        isinstance(option, dict) and option.get("name") == DEFAULT_LANGUAGE_MODEL
        for option in options
    ):
        option = deepcopy(selected)
        option.setdefault("category", option.get("provider", "Google Generative AI"))
        options.insert(0, option)


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


def _edge_port(edge: dict[str, Any], side: str) -> str:
    """Langflow edge의 source/target handle에서 연결된 포트 이름을 읽습니다."""

    handle = edge.get("data", {}).get(f"{side}Handle", {})
    key = "name" if side == "source" else "fieldName"
    value = handle.get(key) if isinstance(handle, dict) else ""
    return str(value or "")


def _source_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _target_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _handle_text(value: dict[str, Any]) -> str:
    """Mirror Langflow frontend's stable stringify + quote substitution."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace('"', "œ")


def build_saving_flow(donor: dict[str, Any], spec: SavingSpec) -> dict[str, Any]:
    proto = prototypes(donor)
    lookup_description = "existing-item MongoDB loading plus candidate matching" if spec.existing_loader else "candidate-targeted MongoDB duplicate lookup"
    flow = empty_flow(donor, FLOW_DISPLAY_NAMES[f"{spec.slug}_saving"], f"Optimized {spec.label} metadata saving flow: one extraction LLM, {lookup_description}, one deterministic writer for dry-run/live execution, and one compact response terminal.", f"metadata-driven-v5-{spec.slug.replace('_', '-')}-saving", ["v5", "standalone", "metadata-authoring", "optimized"])
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
    existing_loader = None
    # Workflow Skill은 등록 목록을 계획에 함께 제공해야 하므로 제한된 active 목록을 실제 연결합니다.
    # Domain/Table/Main Filter는 후보가 확정된 뒤 05가 exact key/identity만 조회하므로 선행 loader를 만들지 않습니다.
    if spec.existing_loader:
        existing_loader = add("existing_loader", custom_node(proto["custom"], f"ExistingLoader-{spec.slug}", folder / spec.existing_loader, 1550, 340))
        _set_value(existing_loader["data"]["node"]["template"], "limit", "500")
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
    if existing_loader is not None:
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
    flow = empty_flow(donor, FLOW_DISPLAY_NAMES["metadata_qa"], "Metadata QA flow with MongoDB projection, deterministic catalog comparison/scope answers, compact same-session inventory reuse, SQL-on-demand, and canonical API response.", "metadata-driven-v5-metadata-qa", ["v5", "standalone", "metadata-qa", "optimized"])
    folder = COMPONENT_ROOT / "metadata_qa_flow"
    nodes: dict[str, dict[str, Any]] = {}

    def add(name: str, node: dict[str, Any]) -> dict[str, Any]:
        nodes[name] = node
        flow["data"]["nodes"].append(node)
        return node

    chat = add("chat", native_node(proto["chat_input"], "ChatInput-metadata-qa", 0, 0))
    _set_message_storage(chat, True)
    session_loader = add(
        "session_loader",
        custom_node(
            proto["custom"],
            "SessionStateLoader-metadata-qa",
            COMPONENT_ROOT / "session_state_flow" / "00_mongodb_session_state_loader.py",
            300,
            -190,
        ),
    )
    session_loader_template = session_loader["data"]["node"]["template"]
    _set_value(session_loader_template, "mongo_database", "datagov")
    _set_value(session_loader_template, "session_collection_name", "agent_v4_session_states")
    _set_value(session_loader_template, "enabled", "true")
    _set_value(session_loader_template, "preview_row_limit", "5")
    request = add("request", custom_node(proto["custom"], "Request-metadata-qa", folder / "00_metadata_qa_request_loader.py", 600, 0))
    snapshot = add("snapshot", custom_node(proto["custom"], "SnapshotLoader-metadata-qa", folder / "01_mongodb_metadata_snapshot_loader.py", 900, 320))
    context = add("context", custom_node(proto["custom"], "Context-metadata-qa", folder / "02_metadata_qa_context_builder.py", 1260, 0))
    _set_value(context["data"]["node"]["template"], "max_items", "50")
    _set_value(context["data"]["node"]["template"], "max_bytes", "65536")
    variables = add("variables", custom_node(proto["custom"], "Variables-metadata-qa", folder / "03_metadata_qa_variables_builder.py", 1560, 0))
    prompt_text = (folder / "03_metadata_qa_prompt_template_ko.md").read_text(encoding="utf-8")
    prompt = add("prompt", prompt_node(proto["prompt"], "Prompt-metadata-qa", prompt_text, 1860, 0))
    model = add(
        "model",
        language_model_node(
            proto["language_model"],
            "LanguageModel-metadata-qa",
            2160,
            0,
            "Answer only from the supplied metadata context and return the requested JSON object.",
        ),
    )
    normalizer = add("normalizer", custom_node(proto["custom"], "Normalizer-metadata-qa", folder / "04_metadata_qa_response_normalizer.py", 2460, 0))
    session_writer = add(
        "session_writer",
        custom_node(
            proto["custom"],
            "SessionStateWriter-metadata-qa",
            COMPONENT_ROOT / "session_state_flow" / "01_mongodb_session_state_writer.py",
            2760,
            0,
        ),
    )
    session_writer_template = session_writer["data"]["node"]["template"]
    _set_value(session_writer_template, "mongo_database", "datagov")
    _set_value(session_writer_template, "session_collection_name", "agent_v4_session_states")
    _set_value(session_writer_template, "enabled", "true")
    _set_value(session_writer_template, "preview_row_limit", "5")
    _set_value(session_writer_template, "history_limit", "10")
    message = add("message", custom_node(proto["custom"], "Message-metadata-qa", folder / "05_metadata_qa_message_adapter.py", 3060, -100))
    api = add(
        "api",
        custom_node(
            proto["custom"],
            "Api-metadata-qa",
            folder / "06_metadata_qa_api_response_builder.py",
            3360,
            100,
        ),
    )
    output = add("output", native_node(proto["chat_output"], "ChatOutput-metadata-qa", 3360, -160))
    _set_message_storage(output, True)

    add_edge(flow, chat, "message", request, "question")
    add_edge(flow, chat, "message", session_loader, "question")
    add_edge(flow, session_loader, "loaded_state", request, "previous_state")
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
    add_edge(flow, normalizer, "payload_out", session_writer, "response_payload")
    add_edge(flow, session_writer, "payload_out", message, "payload")
    add_edge(flow, session_writer, "payload_out", api, "payload")
    add_edge(flow, message, "message", api, "display_message")
    add_edge(flow, message, "message", output, "input_value")
    return flow


ROUTE_ENDPOINTS = {
    "data_analysis": "metadata-driven-v5-data-analysis",
    "report_followup": "metadata-driven-v5-report-followup",
    "metadata_qa": "metadata-driven-v5-metadata-qa",
    "domain_saving": "metadata-driven-v5-domain-saving",
    "table_catalog_saving": "metadata-driven-v5-table-catalog-saving",
    "main_flow_filter_saving": "metadata-driven-v5-main-flow-filter-saving",
}


TOOL_ROUTE_SPECS = [
    ToolRouteSpec(
        "data_analysis",
        FLOW_DISPLAY_NAMES["data_analysis"],
        "run_data_analysis",
        "실제 제조 데이터 값의 조회와 계산에 사용합니다. 생산량, 재공, 투입/산출, HOLD, 장비 배정, UPH, 제품별 집계와 비교 질문이 대상입니다. 메타데이터 정의 설명이나 등록 요청에는 사용하지 않습니다.",
    ),
    ToolRouteSpec(
        "report_followup",
        FLOW_DISPLAY_NAMES["report_followup"],
        "run_report_followup",
        "같은 세션의 직전 Report Snapshot 또는 Report가 미리 만든 집계 View를 대상으로 컬럼 선택, 필터, 정렬, 상위/하위 N을 수행할 때 사용합니다. 새 groupby 집계, 현재 기준·최신 데이터·다시/새로 조회하거나 다른 데이터셋을 결합하는 요청에는 사용하지 않습니다.",
    ),
    ToolRouteSpec(
        "metadata_qa",
        FLOW_DISPLAY_NAMES["metadata_qa"],
        "run_metadata_qa",
        "등록된 도메인, 테이블 카탈로그, 필수 파라미터, SQL 템플릿, 컬럼과 계산 규칙을 설명하거나 확인할 때 사용합니다. 실제 생산 수치 조회나 메타데이터 저장에는 사용하지 않습니다.",
    ),
    ToolRouteSpec(
        "domain_saving",
        FLOW_DISPLAY_NAMES["domain_saving"],
        "save_domain_metadata",
        "도메인 용어, 별칭, 공정 그룹, 제품 그룹, 분석 규칙을 신규 저장하거나 유사 기존 항목에 merge/replace하라는 명시적 등록 요청에 사용합니다.",
    ),
    ToolRouteSpec(
        "table_catalog_saving",
        FLOW_DISPLAY_NAMES["table_catalog_saving"],
        "save_table_catalog_metadata",
        "데이터셋 또는 테이블의 source type, query template, 필수 파라미터, 컬럼 스키마를 등록하거나 변경하라는 명시적 요청에 사용합니다.",
    ),
    ToolRouteSpec(
        "main_flow_filter_saving",
        FLOW_DISPLAY_NAMES["main_flow_filter_saving"],
        "save_main_flow_filter_metadata",
        "DATE, OPER_NAME, ORG 등 분석 전반에 공통으로 적용할 메인 필터 정의를 등록하거나 변경하라는 명시적 요청에 사용합니다.",
    ),
    ToolRouteSpec(
        "realtime_production_report",
        FLOW_DISPLAY_NAMES["realtime_production_report"],
        "run_realtime_production_report",
        "현재 질문에 '분석'이 포함되고 '실시간 생산 분석', '실시간 분석', '실시간 생산분석' 중 하나가 명시된 경우에만 사용합니다. 일반 생산 조회보다 우선하며, 질문 원문을 그대로 전달해 하위 Flow가 공정그룹을 선택하거나 누락 시 다시 묻도록 합니다.",
        required_all_keywords="분석",
        required_any_phrases="실시간 생산 분석\n실시간 분석\n실시간 생산분석",
        keyword_gate_message=(
            "실시간 생산 Report를 실행하려면 질문에 '분석'을 포함해 주세요. "
            "예: 'W/B 공정그룹 실시간 생산 분석을 해줘'."
        ),
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


def build_agent_tool_router_flow(donor: dict[str, Any]) -> dict[str, Any]:
    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        FLOW_DISPLAY_NAMES["agent_tool_router"],
        "LLM Agent router with seven compact selected-ID-first cached Flow tools, a dedicated same-session Report follow-up path, deterministic realtime-analysis keyword gating, name fallback for standalone imports, shared session propagation, a silent direct-return Agent, a direct-result adapter that removes nested child events, and one final Chat Output.",
        "metadata-driven-v5-agent-tool-router",
        ["v5", "standalone", "agent-router", "tool-mode", "selected-flow-id", "cached-flow", "direct-result-adapter", "optimized"],
    )
    system_prompt = (COMPONENT_ROOT / "route_flow_v2" / "SYSTEM_PROMPT_KO.md").read_text(encoding="utf-8")
    tool_path = COMPONENT_ROOT / "route_flow_v2" / "01_cached_named_run_flow_tool.py"
    result_adapter_path = COMPONENT_ROOT / "route_flow_v2" / "02_agent_direct_tool_result_adapter.py"
    silent_agent_path = COMPONENT_ROOT / "route_flow_v2" / "03_silent_direct_return_router_agent.py"

    chat = native_node(proto["chat_input"], "ChatInput-agent-tool-router", 0, 0)
    _set_message_storage(chat, True)
    agent = silent_router_agent_node(
        proto["custom"],
        "Agent-agent-tool-router",
        silent_agent_path,
        850,
        0,
        system_prompt,
    )
    agent_template = agent["data"]["node"]["template"]
    # Chat Input이 현재 사용자 Message를 먼저 저장하므로 5개 메시지를 조회합니다.
    # LFX Agent는 input_value와 ID가 같은 현재 Message를 제거하고, 이전 4개 메시지
    # (사용자/응답 2턴)만 history로 유지합니다. Native Chat Input Message가 원본
    # ID를 그대로 전달하므로 이 ID 기반 중복 제거가 동작합니다.
    _set_value(agent_template, "max_iterations", 1)
    _set_value(agent_template, "n_messages", 5)
    _set_value(agent_template, "add_current_date_tool", False)
    # LFX 1.11 maps this legacy-looking option to a broad
    # ToolRetryMiddleware(max_retries=2).  The Router also exposes metadata
    # write flows, so replaying an entire child Flow is not a safe recovery
    # boundary.  Each Cached Flow Tool instead returns one sanitized
    # status=error ToolMessage for validation/runtime failures.
    _set_value(agent_template, "handle_parsing_errors", False)
    _set_value(agent_template, "verbose", False)
    result_adapter = custom_node(
        proto["custom"],
        "DirectToolResultAdapter-agent-tool-router",
        result_adapter_path,
        1120,
        0,
    )
    _set_value(result_adapter["data"]["node"]["template"], "prefer_tool_result", True)
    output = native_node(proto["chat_output"], "ChatOutput-agent-tool-router", 1420, 0)
    _set_message_storage(output, True)
    flow["data"]["nodes"].extend([chat, agent, result_adapter, output])
    add_edge(flow, chat, "message", agent, "input_value")

    y_positions = (-780, -520, -260, 0, 260, 520, 780)
    for spec, y in zip(TOOL_ROUTE_SPECS, y_positions, strict=True):
        tool = custom_node(proto["custom"], f"CachedFlowTool-{spec.route_name}", tool_path, 350, y)
        tool_config = tool["data"]["node"]
        tool_config["tool_mode"] = True
        template = tool_config["template"]
        _set_value(template, "flow_name_selected", spec.flow_name)
        _set_value(template, "flow_id_selected", "")
        _set_value(template, "flow_resolution_mode", "Flow ID 우선")
        _set_value(template, "cache_flow", True)
        _set_value(template, "tool_name", spec.tool_name)
        _set_value(template, "tool_description", spec.tool_description)
        _set_value(template, "required_all_keywords", spec.required_all_keywords)
        _set_value(template, "required_any_phrases", spec.required_any_phrases)
        _set_value(template, "keyword_gate_message", spec.keyword_gate_message)
        _set_value(template, "return_direct", True)
        flow["data"]["nodes"].append(tool)
        add_edge(flow, tool, "component_as_tool", agent, "tools")

    # Langflow 1.11 forwards nested child-flow LLM events into the Agent message.
    # Keep return_direct on the Tools, but publish only their successful final output.
    add_edge(flow, agent, "response", result_adapter, "agent_message")
    add_edge(flow, result_adapter, "message", output, "input_value")
    return flow


def build_realtime_production_report_flow(donor: dict[str, Any]) -> dict[str, Any]:
    """후속 분석용 Snapshot을 함께 발행하는 현재 Realtime Report Flow를 만듭니다."""

    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        FLOW_DISPLAY_NAMES["realtime_production_report"],
        "Realtime production report flow with Domain process-group catalog grounding, deterministic explicit process-group selection and row filtering, a Report View Bundle plus shared Context Publisher for no-code follow-up Snapshot contracts, clarification without HTML when no group is specified, and four fixed report sections.",
        "metadata-driven-v5-realtime-production-report",
        ["v5", "standalone", "realtime-production", "process-group", "dummy-data", "html-report", "report-api", "mongodb-collection", "followup-context", "report-bundle", "context-publisher"],
    )
    folder = COMPONENT_ROOT / "realtime_production_report_flow"
    chat = native_node(proto["chat_input"], "ChatInput-realtime-production-report", 0, -180)
    _set_message_storage(chat, True)
    catalog = custom_node(
        proto["custom"],
        "ProcessGroupCatalog-realtime-production-report",
        folder / "00a_process_group_catalog_loader.py",
        350,
        -360,
    )
    catalog_template = catalog["data"]["node"]["template"]
    _set_value(catalog_template, "mongo_database", "datagov")
    _set_value(catalog_template, "collection_name", "agent_v4_domain_items")
    _set_value(catalog_template, "status_filter", "active")
    dummy = custom_node(
        proto["custom"],
        "DummyProductionJudgementData-realtime-production-report",
        folder / "00_dummy_production_judgement_data.py",
        760,
        260,
    )
    dummy_template = dummy["data"]["node"]["template"]
    _set_value(dummy_template, "row_count", "500")
    _set_value(dummy_template, "seed", "20260727")
    _set_value(dummy_template, "work_date", "")
    _set_value(dummy_template, "process_names", "W/B1,W/B2,W/B3,W/B4,B/G1,B/G2,B/G3,D/A1,D/A2,D/A3")
    gate = custom_node(
        proto["custom"],
        "ProcessGroupSelectionGate-realtime-production-report",
        folder / "00c_deterministic_process_group_selection_gate.py",
        1540,
        0,
    )
    context_payload = custom_node(
        proto["custom"],
        "RealtimeReportViewBundle-realtime-production-report",
        folder / "00d_report_context_payload_builder.py",
        1940,
        300,
    )
    context_publisher = custom_node(
        proto["custom"],
        "ReportContextPublisher-realtime-production-report",
        folder / "00e_report_context_publisher.py",
        2340,
        300,
    )
    context_store = custom_node(
        proto["custom"],
        "ReportContextResultStore-realtime-production-report",
        COMPONENT_ROOT / "data_analysis_flow" / "23_mongodb_result_store.py",
        2740,
        300,
    )
    context_store_template = context_store["data"]["node"]["template"]
    _set_value(context_store_template, "mongo_database", "datagov")
    _set_value(context_store_template, "collection_name", "agent_v4_result_store")
    _set_value(context_store_template, "ttl_hours", "4")
    report = custom_node(
        proto["custom"],
        "RealtimeProductionReportBuilder-realtime-production-report",
        folder / "01_realtime_production_report_builder.py",
        3140,
        0,
    )
    report_template = report["data"]["node"]["template"]
    _set_value(report_template, "report_api_url", "http://127.0.0.1:5000")
    _set_value(report_template, "report_ttl_hours", "4")
    _set_value(report_template, "max_html_rows", "1000")
    output = native_node(proto["chat_output"], "ChatOutput-realtime-production-report", 3600, -130)
    _set_message_storage(output, True)
    session_writer = custom_node(
        proto["custom"],
        "ReportSessionStateWriter-realtime-production-report",
        COMPONENT_ROOT / "session_state_flow" / "01_mongodb_session_state_writer.py",
        3600,
        210,
    )
    session_writer_template = session_writer["data"]["node"]["template"]
    _set_value(session_writer_template, "mongo_database", "datagov")
    _set_value(session_writer_template, "session_collection_name", "agent_v4_session_states")
    _set_value(session_writer_template, "enabled", "true")
    _set_value(session_writer_template, "preview_row_limit", "5")
    _set_value(session_writer_template, "history_limit", "10")
    api_terminal = custom_node(
        proto["custom"],
        "RealtimeProductionReportApiTerminal-realtime-production-report",
        folder / "02_realtime_production_report_api_terminal.py",
        4000,
        210,
    )
    flow["data"]["nodes"].extend(
        [
            chat,
            catalog,
            dummy,
            gate,
            context_payload,
            context_publisher,
            context_store,
            report,
            output,
            session_writer,
            api_terminal,
        ]
    )
    add_edge(flow, chat, "message", gate, "question")
    add_edge(flow, catalog, "process_group_catalog", gate, "process_group_catalog")
    add_edge(flow, dummy, "dataset", gate, "dataset")
    add_edge(flow, chat, "message", context_payload, "question")
    add_edge(flow, gate, "selected_dataset", context_payload, "dataset")
    add_edge(flow, chat, "message", context_publisher, "question")
    add_edge(flow, context_payload, "report_bundle", context_publisher, "report_bundle")
    add_edge(flow, context_publisher, "context_payload", context_store, "payload")
    add_edge(flow, chat, "message", report, "question")
    add_edge(flow, gate, "selected_dataset", report, "dataset")
    add_edge(flow, context_store, "payload_out", report, "context_payload")
    add_edge(flow, report, "api_response", session_writer, "response_payload")
    add_edge(flow, session_writer, "payload_out", api_terminal, "report_result")
    add_edge(flow, report, "message", api_terminal, "report_message")
    add_edge(flow, api_terminal, "message", output, "input_value")
    return flow


def build_realtime_production_report_legacy_flow(donor: dict[str, Any]) -> dict[str, Any]:
    """후속 분석 Context가 없던 변경 전 Realtime Report 구조를 1.11로 재현합니다."""

    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        FLOW_DISPLAY_NAMES["realtime_production_report_legacy"],
        "Legacy realtime production report flow preserved for compatibility: Domain process-group selection, deterministic row filtering, direct Report publication, and no follow-up Snapshot or session-state side effects.",
        "metadata-driven-v5-realtime-production-report-legacy",
        ["v5", "standalone", "realtime-production", "legacy-report", "direct-run", "langflow-1.11"],
    )
    shared_folder = COMPONENT_ROOT / "realtime_production_report_flow"
    legacy_folder = COMPONENT_ROOT / "realtime_production_report_legacy_flow"
    suffix = "realtime-production-report-legacy"

    chat = native_node(proto["chat_input"], f"ChatInput-{suffix}", 0, -180)
    _set_message_storage(chat, True)
    catalog = custom_node(
        proto["custom"],
        f"ProcessGroupCatalog-{suffix}",
        shared_folder / "00a_process_group_catalog_loader.py",
        350,
        -360,
    )
    catalog_template = catalog["data"]["node"]["template"]
    _set_value(catalog_template, "mongo_database", "datagov")
    _set_value(catalog_template, "collection_name", "agent_v4_domain_items")
    _set_value(catalog_template, "status_filter", "active")
    prompt = custom_node(
        proto["custom"],
        f"ProcessGroupPrompt-{suffix}",
        shared_folder / "00b_process_group_selection_prompt.py",
        760,
        -380,
    )
    selector_model = language_model_node(
        proto["language_model"],
        f"LanguageModelProcessGroup-{suffix}",
        1160,
        -380,
        "Select only one explicitly evidenced process-group key from the supplied domain catalog. Return exactly one JSON object and never guess a default group.",
    )
    _set_value(selector_model["data"]["node"]["template"], "max_tokens", 700)
    dummy = custom_node(
        proto["custom"],
        f"DummyProductionJudgementData-{suffix}",
        shared_folder / "00_dummy_production_judgement_data.py",
        760,
        260,
    )
    dummy_template = dummy["data"]["node"]["template"]
    _set_value(dummy_template, "row_count", "500")
    _set_value(dummy_template, "seed", "20260727")
    _set_value(dummy_template, "work_date", "")
    _set_value(dummy_template, "process_names", "W/B1,W/B2,W/B3,W/B4,B/G1,B/G2,B/G3,D/A1,D/A2,D/A3")
    gate = custom_node(
        proto["custom"],
        f"ProcessGroupSelectionGate-{suffix}",
        shared_folder / "00c_process_group_selection_gate.py",
        1540,
        0,
    )
    report = custom_node(
        proto["custom"],
        f"RealtimeProductionReportBuilder-{suffix}",
        legacy_folder / "01_realtime_production_report_builder.py",
        1940,
        0,
    )
    report_template = report["data"]["node"]["template"]
    _set_value(report_template, "report_api_url", "http://127.0.0.1:5000")
    _set_value(report_template, "report_ttl_hours", "4")
    _set_value(report_template, "max_html_rows", "1000")
    output = native_node(proto["chat_output"], f"ChatOutput-{suffix}", 2380, -160)
    _set_message_storage(output, True)
    api_terminal = custom_node(
        proto["custom"],
        f"RealtimeProductionReportApiTerminal-{suffix}",
        legacy_folder / "02_realtime_production_report_api_terminal.py",
        2380,
        180,
    )

    flow["data"]["nodes"].extend(
        [chat, catalog, prompt, selector_model, dummy, gate, report, output, api_terminal]
    )
    add_edge(flow, chat, "message", prompt, "question")
    add_edge(flow, catalog, "process_group_catalog", prompt, "process_group_catalog")
    add_edge(flow, prompt, "prompt", selector_model, "input_value")
    add_edge(flow, chat, "message", gate, "question")
    add_edge(flow, catalog, "process_group_catalog", gate, "process_group_catalog")
    add_edge(flow, selector_model, "text_output", gate, "llm_response")
    add_edge(flow, dummy, "dataset", gate, "dataset")
    add_edge(flow, chat, "message", report, "question")
    add_edge(flow, gate, "selected_dataset", report, "dataset")
    add_edge(flow, report, "message", output, "input_value")
    add_edge(flow, report, "api_response", api_terminal, "report_result")
    return flow


def build_report_followup_flow(donor: dict[str, Any]) -> dict[str, Any]:
    """Build the isolated same-session Report Snapshot follow-up Flow 07-2."""

    proto = prototypes(donor)
    flow = empty_flow(
        donor,
        FLOW_DISPLAY_NAMES["report_followup"],
        "Same-session Report follow-up flow that restores a declared raw or pre-aggregated Report Snapshot view, validates its query-source contract, executes only bounded select/filter/sort/top-N operations, and never performs live source retrieval, groupby, joins, or metadata-catalog planning.",
        "metadata-driven-v5-report-followup",
        [
            "v5",
            "standalone",
            "report-followup",
            "snapshot-only",
            "same-session",
            "bounded-analysis",
            "no-live-retrieval",
        ],
    )
    folder = COMPONENT_ROOT / "report_followup_flow"

    chat = native_node(proto["chat_input"], "ChatInput-report-followup", 0, 0)
    _set_message_storage(chat, True)

    session_loader = custom_node(
        proto["custom"],
        "SessionStateLoader-report-followup",
        COMPONENT_ROOT / "session_state_flow" / "00_mongodb_session_state_loader.py",
        360,
        -180,
    )
    session_loader_template = session_loader["data"]["node"]["template"]
    _set_value(session_loader_template, "mongo_database", "datagov")
    _set_value(session_loader_template, "session_collection_name", "agent_v4_session_states")
    _set_value(session_loader_template, "enabled", "true")
    _set_value(session_loader_template, "preview_row_limit", "5")

    prompt_builder = custom_node(
        proto["custom"],
        "PromptBuilder-report-followup",
        folder / "00_report_followup_prompt_builder.py",
        720,
        0,
    )
    native_plan_model = language_model_node(
        proto["language_model"],
        "LanguageModel-report-followup",
        1080,
        0,
        "Plan only from the supplied Report query-source contract. Return exactly one JSON object and never request, infer, or join a live source.",
    )
    _set_value(native_plan_model["data"]["node"]["template"], "max_tokens", 1800)
    guarded_plan_router = custom_node(
        proto["custom"],
        "GuardedPlanRouter-report-followup",
        folder / "00b_report_followup_guarded_plan_router.py",
        1080,
        0,
    )
    native_model_template = native_plan_model["data"]["node"]["template"]
    guarded_model_template = guarded_plan_router["data"]["node"]["template"]
    # Langflow 1.11 native Language Model의 provider 선택 및 runtime override
    # 계약을 custom guarded boundary에서도 그대로 노출합니다.
    for field_name in (
        "model",
        "model_name",
        "provider",
        "api_key",
        "system_message",
        "stream",
        "temperature",
        "max_tokens",
    ):
        if isinstance(native_model_template.get(field_name), dict) and isinstance(guarded_model_template.get(field_name), dict):
            guarded_model_template[field_name] = deepcopy(native_model_template[field_name])

    normalizer = custom_node(
        proto["custom"],
        "PlanNormalizer-report-followup",
        folder / "01_report_followup_plan_normalizer.py",
        1440,
        0,
    )
    result_loader = custom_node(
        proto["custom"],
        "ResultLoader-report-followup",
        COMPONENT_ROOT / "data_analysis_flow" / "05_mongodb_result_loader.py",
        1800,
        0,
    )
    result_loader_template = result_loader["data"]["node"]["template"]
    _set_value(result_loader_template, "mongo_database", "datagov")
    _set_value(result_loader_template, "collection_name", "agent_v4_result_store")

    executor = custom_node(
        proto["custom"],
        "SnapshotExecutor-report-followup",
        folder / "02_report_snapshot_executor.py",
        2160,
        0,
    )
    response_builder = custom_node(
        proto["custom"],
        "ResponseBuilder-report-followup",
        folder / "03_report_followup_response_builder.py",
        2520,
        0,
    )
    _set_value(response_builder["data"]["node"]["template"], "table_preview_limit", 5)

    session_writer = custom_node(
        proto["custom"],
        "SessionStateWriter-report-followup",
        COMPONENT_ROOT / "session_state_flow" / "01_mongodb_session_state_writer.py",
        2880,
        0,
    )
    session_writer_template = session_writer["data"]["node"]["template"]
    _set_value(session_writer_template, "mongo_database", "datagov")
    _set_value(session_writer_template, "session_collection_name", "agent_v4_session_states")
    _set_value(session_writer_template, "enabled", "true")
    _set_value(session_writer_template, "preview_row_limit", "5")
    _set_value(session_writer_template, "history_limit", "10")

    terminal = custom_node(
        proto["custom"],
        "ApiTerminal-report-followup",
        folder / "04_report_followup_api_terminal.py",
        3240,
        0,
    )
    output = native_node(proto["chat_output"], "ChatOutput-report-followup", 3600, -120)
    _set_message_storage(output, True)

    flow["data"]["nodes"].extend(
        [
            chat,
            session_loader,
            prompt_builder,
            guarded_plan_router,
            normalizer,
            result_loader,
            executor,
            response_builder,
            session_writer,
            terminal,
            output,
        ]
    )
    add_edge(flow, chat, "message", session_loader, "question")
    add_edge(flow, chat, "message", prompt_builder, "question")
    add_edge(flow, session_loader, "loaded_state", prompt_builder, "loaded_state")
    add_edge(flow, prompt_builder, "payload_out", guarded_plan_router, "payload")
    add_edge(flow, prompt_builder, "prompt", guarded_plan_router, "prompt")
    add_edge(flow, prompt_builder, "payload_out", normalizer, "payload")
    add_edge(flow, guarded_plan_router, "text_output", normalizer, "llm_response")
    add_edge(flow, normalizer, "payload_out", result_loader, "payload")
    add_edge(flow, result_loader, "payload_out", executor, "payload")
    add_edge(flow, executor, "payload_out", response_builder, "payload")
    add_edge(flow, response_builder, "payload_out", session_writer, "response_payload")
    add_edge(flow, session_writer, "payload_out", terminal, "response_payload")
    add_edge(flow, terminal, "message", output, "input_value")
    return flow


def write_flows() -> list[dict[str, Any]]:
    donor = load_donor()
    outputs = []
    # 07 / 07-1 / 07-2 전환 전에 사용하던 생성물만 정확히 제거합니다.
    for retired_name in (
        "07_realtime_production_report_flow_v5_standalone.json",
        "10_report_followup_flow_v5_standalone.json",
        "11_realtime_production_report_legacy_flow_v5_standalone.json",
    ):
        retired_path = EXPORT_ROOT / retired_name
        if retired_path.exists():
            retired_path.unlink()
    for spec in SAVING_SPECS:
        flow = _stamp_flow_version(build_saving_flow(donor, spec))
        path = EXPORT_ROOT / f"{spec.slug}_saving_flow_v5_standalone.json"
        path.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        outputs.append({"path": str(path), "nodes": len(flow["data"]["nodes"]), "edges": len(flow["data"]["edges"])})
    qa = _stamp_flow_version(build_metadata_qa_flow(donor))
    qa_path = EXPORT_ROOT / "metadata_qa_flow_v5_standalone.json"
    qa_path.write_bytes((json.dumps(qa, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    outputs.append({"path": str(qa_path), "nodes": len(qa["data"]["nodes"]), "edges": len(qa["data"]["edges"])})
    tool_router = _stamp_flow_version(build_agent_tool_router_flow(donor))
    tool_router_path = EXPORT_ROOT / "06_agent_tool_router_flow_v5_standalone.json"
    tool_router_path.write_bytes((json.dumps(tool_router, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    outputs.append(
        {
            "path": str(tool_router_path),
            "nodes": len(tool_router["data"]["nodes"]),
            "edges": len(tool_router["data"]["edges"]),
        }
    )
    realtime_production_report = _stamp_flow_version(build_realtime_production_report_flow(donor))
    realtime_production_report_path = EXPORT_ROOT / "07_1_realtime_production_report_flow_v5_standalone.json"
    realtime_production_report_path.write_bytes(
        (json.dumps(realtime_production_report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    outputs.append(
        {
            "path": str(realtime_production_report_path),
            "nodes": len(realtime_production_report["data"]["nodes"]),
            "edges": len(realtime_production_report["data"]["edges"]),
        }
    )
    report_followup = _stamp_flow_version(build_report_followup_flow(donor))
    report_followup_path = EXPORT_ROOT / "07_2_report_followup_flow_v5_standalone.json"
    report_followup_path.write_bytes(
        (json.dumps(report_followup, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    outputs.append(
        {
            "path": str(report_followup_path),
            "nodes": len(report_followup["data"]["nodes"]),
            "edges": len(report_followup["data"]["edges"]),
        }
    )
    legacy_report = _stamp_flow_version(build_realtime_production_report_legacy_flow(donor))
    legacy_report_path = EXPORT_ROOT / "07_realtime_production_report_legacy_flow_v5_standalone.json"
    legacy_report_path.write_bytes(
        (json.dumps(legacy_report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    outputs.append(
        {
            "path": str(legacy_report_path),
            "nodes": len(legacy_report["data"]["nodes"]),
            "edges": len(legacy_report["data"]["edges"]),
        }
    )
    return outputs


def _stamp_flow_version(flow: dict[str, Any]) -> dict[str, Any]:
    """Flow와 모든 직렬화된 노드에 목표 Langflow 버전을 일관되게 기록합니다."""

    flow["last_tested_version"] = TARGET_LANGFLOW_VERSION
    for node in flow.get("data", {}).get("nodes", []):
        component = node.get("data", {}).get("node")
        if isinstance(component, dict):
            component["lf_version"] = TARGET_LANGFLOW_VERSION
    return flow


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the active metadata saving, QA, Router, current/legacy realtime Report, and Report follow-up flows.")
    parser.parse_args()
    print(json.dumps(write_flows(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
