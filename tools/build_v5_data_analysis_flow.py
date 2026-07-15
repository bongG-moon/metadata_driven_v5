from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "flow_exports" / "data_analysis_flow_v4_reference.json"
DEFAULT_TARGET = ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json"
REPAIR_PROMPT_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md"
HELPER_LIBRARY_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"
REPAIR_PROMPT_NODE_ID = "TextInput-v5RepairPrompt"
LANGUAGE_MODEL_NODE_IDS = {
    "Agent-mevnw": "LanguageModel-intent",
    "Agent-SRcFc": "LanguageModel-pandas",
    "Agent-ynb4D": "LanguageModel-answer",
}
LANGUAGE_MODEL_SYSTEM_MESSAGES = {
    "LanguageModel-intent": "Follow the supplied prompt exactly and return only the requested JSON object.",
    "LanguageModel-pandas": "Follow the supplied prompt exactly and return only executable pandas code without markdown fences.",
    "LanguageModel-answer": "Follow the supplied prompt exactly and return only the requested answer text.",
}
MONGO_GLOBAL_VARIABLE = "MONGO_URL"

COMPONENT_FILES = {
    "CustomComponent-xpbhS": "data_analysis_flow/00_analysis_request_loader.py",
    "CustomComponent-i0jbh": "data_analysis_flow/01a_mongodb_domain_metadata_loader.py",
    "MongoDBDomainMetadataLoader-OM3Hg": "data_analysis_flow/01b_mongodb_table_catalog_loader.py",
    "CustomComponent-kzlcF": "data_analysis_flow/01c_mongodb_main_variable_loader.py",
    "CustomComponent-DXrpf": "data_analysis_flow/01d_metadata_candidates_builder.py",
    "CustomComponent-HFsYn": "data_analysis_flow/01e_followup_hint_builder.py",
    "CustomComponent-B1hbh": "data_analysis_flow/02_intent_variables_builder.py",
    "CustomComponent-5o0CN": "data_analysis_flow/04_intent_plan_normalizer.py",
    "CustomComponent-O8vfz": "data_analysis_flow/05_mongodb_result_loader.py",
    "CustomComponent-vVkhs": "data_analysis_flow/06_retrieval_job_validator.py",
    "CustomComponent-x6NXu": "data_analysis_flow/07_retrieval_job_router.py",
    "CustomComponent-Pp7d0": "data_analysis_flow/08_dummy_data_retriever.py",
    "MongoDBDomainMetadataLoader-geCh1": "data_analysis_flow/13_source_retrieval_merger.py",
    "CustomComponent-bhiAG": "data_analysis_flow/14_retrieval_payload_adapter.py",
    "CustomComponent-fc0Vb": "data_analysis_flow/15_pandas_variables_builder.py",
    "CustomComponent-s3mf1": "data_analysis_flow/17_pandas_code_executor.py",
    "CustomComponent-aKrkH": "data_analysis_flow/18_answer_variables_builder.py",
    "CustomComponent-BVItv": "data_analysis_flow/20_answer_response_builder.py",
    "CustomComponent-A5y0b": "data_analysis_flow/21_answer_message_adapter.py",
    "CustomComponent-3eVde": "data_analysis_flow/22_api_response_builder.py",
    "CustomComponent-AUrFb": "data_analysis_flow/23_mongodb_result_store.py",
    "CustomComponent-Fti0r": "session_state_flow/00_mongodb_session_state_loader.py",
    "CustomComponent-fXdS4": "session_state_flow/01_mongodb_session_state_writer.py",
}

PROMPT_FILES = {
    "Prompt Template-AUpQz": "03_intent_prompt_template_ko.md",
    "Prompt Template-xtzD5": "16_pandas_prompt_template_ko.md",
    "Prompt Template-ELVKc": "19_answer_prompt_template_ko.md",
}

REMOVED_REPAIR_NODES = {
    "CustomComponent-ZUhxo",
    "Prompt Template-ej9jd",
    "Agent-nSPco",
    "PandasCodeExecutor-kRbBG",
    "CustomComponent-QJwmh",
}

NEW_COMPONENTS = {
    "CustomComponent-v5Hydrate": {
        "file": "data_analysis_flow/04a_trusted_retrieval_job_hydrator.py",
        "position": {"x": 1900.0, "y": 720.0},
        "inputs": [
            ("data", "payload", "의도 페이로드", True, None),
            ("data", "table_catalog_items", "전체 테이블 카탈로그", True, None),
            ("dropdown", "retrieval_mode", "데이터 조회 모드", False, "dummy"),
        ],
        "outputs": [("Data", "payload_out", "신뢰 조회 작업 페이로드", "build_payload")],
    },
    "CustomComponent-v5UpstreamBinder": {
        "file": "data_analysis_flow/05a_upstream_entity_parameter_binder.py",
        "position": {"x": 2290.0, "y": 720.0},
        "inputs": [("data", "payload", "상위 결과 복원 페이로드", True, None)],
        "outputs": [("Data", "payload_out", "상위 엔터티 바인딩 페이로드", "build_payload")],
    },
    "CustomComponent-v5Helper": {
        "file": "data_analysis_flow/15a_selected_helper_code_builder.py",
        "position": {"x": -1420.0, "y": 2290.0},
        "inputs": [
            ("message", "function_case_selection_json", "Function Case 선택 JSON", True, ""),
            ("message", "helper_library", "전체 helper library", False, ""),
        ],
        "outputs": [("Message", "selected_helper_code", "선택 helper 코드", "build_code")],
    },
    "CustomComponent-v5ExecutionGate": {
        "file": "data_analysis_flow/14a_retrieval_execution_gate.py",
        "position": {"x": 1690.0, "y": 1510.0},
        "inputs": [("data", "payload", "조회 페이로드", True, None)],
        "outputs": [("Data", "payload_out", "실행 제어 페이로드", "build_payload")],
    },
    "CustomComponent-v5Oracle": {
        "file": "data_analysis_flow/09_oracle_query_retriever.py",
        "position": {"x": 470.0, "y": 1510.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "oracle_config", "Oracle 설정/TNS", False, ""),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5HApi": {
        "file": "data_analysis_flow/10_h_api_retriever.py",
        "position": {"x": 470.0, "y": 1690.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "api_token", "H-API 토큰", False, ""),
            ("message", "timeout_seconds", "요청 제한 시간(초)", False, "30"),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5Datalake": {
        "file": "data_analysis_flow/11_datalake_retriever.py",
        "position": {"x": 860.0, "y": 1640.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "module_name", "Datalake 모듈명", False, "lakes"),
            ("message", "class_name", "Datalake 클래스명", False, "LakeHouse"),
            ("message", "user_id", "LakeHouse 사용자 ID", False, ""),
            ("message", "token", "LakeHouse 토큰", False, ""),
            ("message", "s3_access_key", "S3 접근 키", False, ""),
            ("message", "s3_secret_key", "S3 보안 키", False, ""),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
    "CustomComponent-v5Goodocs": {
        "file": "data_analysis_flow/12_goodocs_retriever.py",
        "position": {"x": 860.0, "y": 1840.0},
        "inputs": [
            ("data", "payload", "페이로드", True, None),
            ("message", "user_id", "Goodocs 사용자 ID", False, ""),
            ("message", "token_source", "Goodocs 토큰 소스", False, ""),
            ("message", "token_key", "Goodocs 토큰 키", False, ""),
            ("message", "fetch_limit", "조회 제한 건수", False, "5000"),
        ],
        "outputs": [("Data", "retrieval_payload", "조회 페이로드", "build_payload")],
    },
}


def build_flow(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    flow = json.loads(source.read_text(encoding="utf-8-sig"))
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {node["id"]: node for node in nodes}

    for node_id, relative_path in COMPONENT_FILES.items():
        _refresh_component_node(node_index[node_id], _component_path(relative_path))

    for node_id, relative_path in PROMPT_FILES.items():
        prompt = (ROOT / "langflow_components" / "data_analysis_flow" / relative_path).read_text(encoding="utf-8")
        node_index[node_id]["data"]["node"]["template"]["template"]["value"] = prompt

    node_index["TextInput-AXG9a"]["data"]["node"]["template"]["input_value"]["value"] = (
        HELPER_LIBRARY_SOURCE.read_text(encoding="utf-8")
    )

    _apply_component_spec(
        node_index["CustomComponent-xpbhS"],
        [
            ("message", "question", "사용자 질문", True, ""),
            ("message", "upstream_result_ref", "상위 결과 참조", False, ""),
            ("data", "previous_state", "이전 분석 상태", False, None),
        ],
        [("Data", "payload_out", "분석 요청 페이로드", "build_payload")],
        node_index,
    )

    _apply_component_spec(
        node_index["CustomComponent-DXrpf"],
        [
            ("data", "payload", "질문 페이로드", True, None),
            ("data", "domain_items", "도메인 메타데이터", False, None),
            ("data", "table_catalog_items", "테이블 카탈로그", False, None),
            ("data", "main_flow_filters", "메인 변수", False, None),
            ("message", "max_domain_items", "도메인 최대 후보 수", False, "10"),
            ("message", "min_table_items", "테이블 최소 후보 수", False, "5"),
            ("message", "max_table_items", "테이블 최대 후보 수", False, "10"),
            ("message", "max_bytes", "최대 후보 바이트", False, "32768"),
        ],
        [("Data", "metadata_candidates", "메타데이터 후보", "build_payload")],
        node_index,
    )

    prototype = node_index["CustomComponent-5o0CN"]
    for node_id, spec in NEW_COMPONENTS.items():
        node = deepcopy(prototype)
        node["id"] = node_id
        node["data"]["id"] = node_id
        node["position"] = deepcopy(spec["position"])
        node["selected"] = False
        _refresh_component_node(node, _component_path(spec["file"]))
        _apply_component_spec(node, spec["inputs"], spec["outputs"], node_index)
        nodes.append(node)
        node_index[node_id] = node

    repair_prompt = REPAIR_PROMPT_SOURCE.read_text(encoding="utf-8")
    repair_prompt_node = deepcopy(node_index["TextInput-AXG9a"])
    repair_prompt_node["id"] = REPAIR_PROMPT_NODE_ID
    repair_prompt_node["data"]["id"] = REPAIR_PROMPT_NODE_ID
    repair_prompt_node["position"] = {"x": -680.0, "y": 2440.0}
    repair_prompt_node["selected"] = False
    repair_prompt_component = repair_prompt_node["data"]["node"]
    repair_prompt_component["display_name"] = "17B pandas 복구 프롬프트 템플릿"
    repair_prompt_component["description"] = "실행 오류가 발생했을 때 executor가 동적 오류 문맥을 채워 사용하는 편집 가능한 raw Repair Prompt입니다."
    repair_prompt_component["template"]["input_value"]["display_name"] = "Repair Prompt Template"
    repair_prompt_component["template"]["input_value"]["value"] = repair_prompt
    repair_prompt_component["template"]["input_value"]["advanced"] = False
    if isinstance(repair_prompt_component["template"].get("use_global_variable"), dict):
        repair_prompt_component["template"]["use_global_variable"]["value"] = False
    nodes.append(repair_prompt_node)
    node_index[REPAIR_PROMPT_NODE_ID] = repair_prompt_node

    _apply_component_spec(
        node_index["CustomComponent-s3mf1"],
        [
            ("data", "payload", "페이로드", True, None),
            ("message", "llm_response", "pandas 코드 LLM 응답", True, ""),
            ("message", "function_case_helper_code", "선택 Function Case Helper", False, ""),
            ("message", "repair_prompt_template", "pandas Repair Prompt", True, ""),
            ("model", "model", "Repair Language Model", True, None),
            ("secret", "api_key", "Repair API Key", False, "GOOGLE_API_KEY"),
            ("dropdown", "max_repair_attempts", "최대 Repair 횟수", False, "1"),
        ],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )
    node_index["CustomComponent-s3mf1"]["data"]["node"]["template"]["max_repair_attempts"]["options"] = ["0", "1"]

    _apply_component_spec(
        node_index["CustomComponent-AUrFb"],
        [
            ("data", "payload", "페이로드", True, None),
            ("message", "mongo_uri", "MongoDB 연결 URI", False, ""),
            ("message", "mongo_database", "MongoDB 데이터베이스", False, "datagov"),
            ("message", "collection_name", "결과 컬렉션", False, "agent_v4_result_store"),
            ("message", "ttl_hours", "데이터 보관 시간(시간)", False, "24"),
            ("message", "max_result_rows", "저장 결과 최대 행 수", False, "20000"),
            ("message", "max_source_rows_per_alias", "소스별 저장 최대 행 수", False, "10000"),
            ("message", "max_document_bytes", "결과 문서 최대 바이트", False, "8388608"),
        ],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )

    _apply_component_spec(
        node_index["CustomComponent-x6NXu"],
        [("data", "payload", "페이로드", True, None)],
        [
            ("Data", "dummy_jobs", "더미 작업", "dummy_jobs_out"),
            ("Data", "oracle_jobs", "Oracle 작업", "oracle_jobs_out"),
            ("Data", "h_api_jobs", "H-API 작업", "h_api_jobs_out"),
            ("Data", "datalake_jobs", "데이터레이크 작업", "datalake_jobs_out"),
            ("Data", "goodocs_jobs", "Goodocs 작업", "goodocs_jobs_out"),
        ],
        node_index,
    )

    # A stopped Langflow branch can recursively inactivate shared descendants.
    # Remove the donor's always-on repair branch. The refreshed single-output
    # pandas executor invokes the visible Repair model at most once and only
    # after an actual execution error, then returns one selected payload.
    nodes[:] = [node for node in nodes if node["id"] not in REMOVED_REPAIR_NODES]
    for node_id in REMOVED_REPAIR_NODES:
        node_index.pop(node_id, None)
    edges[:] = [
        edge
        for edge in edges
        if edge["source"] not in REMOVED_REPAIR_NODES and edge["target"] not in REMOVED_REPAIR_NODES
    ]

    # Tool이 없는 세 LLM 단계는 Langflow 기본 Agent 대신 기본 Language Model을 사용합니다.
    # 이 노드는 bind_tools를 호출하지 않으므로 Tool 호출을 지원하지 않는 외부 모델도 연결할 수 있습니다.
    language_model_config = _load_native_component("Language Model")
    for old_node_id, new_node_id in LANGUAGE_MODEL_NODE_IDS.items():
        node = _rename_node(node_index, edges, old_node_id, new_node_id)
        _apply_native_language_model(
            node,
            language_model_config,
            LANGUAGE_MODEL_SYSTEM_MESSAGES[new_node_id],
        )
        _replace_edge_source_output(edges, new_node_id, "response", "text_output")

    _apply_standalone_defaults(nodes)

    removals = {
        ("CustomComponent-5o0CN", "payload_out", "CustomComponent-O8vfz", "payload"),
        ("CustomComponent-v5Hydrate", "payload_out", "CustomComponent-O8vfz", "payload"),
        ("CustomComponent-O8vfz", "payload_out", "CustomComponent-vVkhs", "payload"),
        ("TextInput-AXG9a", "text", "Prompt Template-xtzD5", "function_case_helper_code"),
        ("CustomComponent-BVItv", "payload_out", "CustomComponent-A5y0b", "payload"),
        ("CustomComponent-BVItv", "payload_out", "CustomComponent-3eVde", "payload"),
        ("CustomComponent-bhiAG", "payload_out", "CustomComponent-fc0Vb", "payload"),
        ("CustomComponent-bhiAG", "payload_out", "CustomComponent-s3mf1", "payload"),
    }
    edges[:] = [edge for edge in edges if _edge_key(edge) not in removals]

    additions = [
        ("CustomComponent-HFsYn", "payload_out", "CustomComponent-DXrpf", "payload"),
        ("CustomComponent-5o0CN", "payload_out", "CustomComponent-v5Hydrate", "payload"),
        ("MongoDBDomainMetadataLoader-OM3Hg", "table_catalog_items", "CustomComponent-v5Hydrate", "table_catalog_items"),
        ("CustomComponent-v5Hydrate", "payload_out", "CustomComponent-O8vfz", "payload"),
        ("CustomComponent-O8vfz", "payload_out", "CustomComponent-v5UpstreamBinder", "payload"),
        ("CustomComponent-v5UpstreamBinder", "payload_out", "CustomComponent-vVkhs", "payload"),
        ("CustomComponent-fc0Vb", "function_case_selection_json", "CustomComponent-v5Helper", "function_case_selection_json"),
        ("TextInput-AXG9a", "text", "CustomComponent-v5Helper", "helper_library"),
        ("CustomComponent-v5Helper", "selected_helper_code", "Prompt Template-xtzD5", "function_case_helper_code"),
        ("CustomComponent-v5Helper", "selected_helper_code", "CustomComponent-s3mf1", "function_case_helper_code"),
        (REPAIR_PROMPT_NODE_ID, "text", "CustomComponent-s3mf1", "repair_prompt_template"),
        ("CustomComponent-bhiAG", "payload_out", "CustomComponent-v5ExecutionGate", "payload"),
        ("CustomComponent-v5ExecutionGate", "payload_out", "CustomComponent-fc0Vb", "payload"),
        ("CustomComponent-v5ExecutionGate", "payload_out", "CustomComponent-s3mf1", "payload"),
        ("CustomComponent-s3mf1", "payload_out", "CustomComponent-AUrFb", "payload"),
        ("CustomComponent-fXdS4", "payload_out", "CustomComponent-A5y0b", "payload"),
        ("CustomComponent-fXdS4", "payload_out", "CustomComponent-3eVde", "payload"),
        ("CustomComponent-x6NXu", "oracle_jobs", "CustomComponent-v5Oracle", "payload"),
        ("CustomComponent-v5Oracle", "retrieval_payload", "MongoDBDomainMetadataLoader-geCh1", "oracle_retrieval"),
        ("CustomComponent-x6NXu", "h_api_jobs", "CustomComponent-v5HApi", "payload"),
        ("CustomComponent-v5HApi", "retrieval_payload", "MongoDBDomainMetadataLoader-geCh1", "h_api_retrieval"),
        ("CustomComponent-x6NXu", "datalake_jobs", "CustomComponent-v5Datalake", "payload"),
        ("CustomComponent-v5Datalake", "retrieval_payload", "MongoDBDomainMetadataLoader-geCh1", "datalake_retrieval"),
        ("CustomComponent-x6NXu", "goodocs_jobs", "CustomComponent-v5Goodocs", "payload"),
        ("CustomComponent-v5Goodocs", "retrieval_payload", "MongoDBDomainMetadataLoader-geCh1", "goodocs_retrieval"),
    ]
    for source_id, source_name, target_id, target_name in additions:
        edges.append(_make_edge(node_index, source_id, source_name, target_id, target_name))
    _refresh_edge_source_types(edges, node_index)

    flow["name"] = "metadata_driven_v5_data_analysis_standalone"
    flow["description"] = (
        "v5 standalone flow (dummy default, live retrievers included): bounded metadata candidates, "
        "trusted catalog hydration, explicit same-session upstream result restoration, metadata-declared entity binding, "
        "thin retrieval branches, selected helper code, visible raw Repair Prompt, failure-only one-attempt pandas repair, "
        "native tool-free Language Model stages, deterministic required-source execution gating, one finalization path, and compact API payload with explicit repair audit details."
    )
    flow["endpoint_name"] = "metadata-driven-v5-data-analysis"
    flow["tags"] = sorted(set([*flow.get("tags", []), "v5", "dummy-default", "live-ready", "standalone"]))
    return flow


def _component_path(relative_path: str) -> Path:
    return ROOT / "langflow_components" / relative_path


def _load_native_component(display_name: str) -> dict[str, Any]:
    """현재 Langflow/LFX 설치본에서 기본 컴포넌트 템플릿을 읽습니다."""

    try:
        spec = find_spec("lfx")
    except (ImportError, ModuleNotFoundError, ValueError):
        # 단위 테스트의 경량 Langflow stub처럼 __spec__이 없는 module이 이미
        # 등록돼 있어도 아래 standalone Desktop component index 경로를 사용합니다.
        spec = None
    candidates = []
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
        raise RuntimeError("Langflow/LFX component_index.json is required to build the standalone Flow")
    index = json.loads(component_index.read_text(encoding="utf-8"))
    component = _find_native_component(index, display_name)
    if not component:
        raise RuntimeError(f"Langflow component template not found: {display_name}")
    component["lf_version"] = "1.8.2"
    return component


def _find_native_component(value: Any, display_name: str) -> dict[str, Any]:
    """중첩 component index에서 표시 이름이 일치하는 기본 노드를 찾습니다."""

    if isinstance(value, dict):
        if value.get("display_name") == display_name and isinstance(value.get("template"), dict):
            return deepcopy(value)
        for child in value.values():
            found = _find_native_component(child, display_name)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_native_component(child, display_name)
            if found:
                return found
    return {}


def _rename_node(
    node_index: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    old_node_id: str,
    new_node_id: str,
) -> dict[str, Any]:
    """노드 ID와 연결의 source/target ID를 함께 바꿔 이전 Agent 흔적을 제거합니다."""

    node = node_index.pop(old_node_id)
    node["id"] = new_node_id
    node["data"]["id"] = new_node_id
    node_index[new_node_id] = node
    for edge in edges:
        if edge.get("source") == old_node_id:
            edge["source"] = new_node_id
        if edge.get("target") == old_node_id:
            edge["target"] = new_node_id
    return node


def _apply_native_language_model(
    node: dict[str, Any],
    component_config: dict[str, Any],
    system_message: str,
) -> None:
    """기존 provider 선택값을 보존한 Langflow 기본 Language Model 노드로 교체합니다."""

    previous_template = node["data"]["node"]["template"]
    config = deepcopy(component_config)
    template = config["template"]
    for field_name in ("model", "api_key"):
        previous = previous_template.get(field_name)
        current = template.get(field_name)
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        for attribute in ("value", "load_from_db", "advanced", "show"):
            if attribute in previous:
                current[attribute] = deepcopy(previous[attribute])
    template["system_message"]["value"] = system_message
    template["stream"]["value"] = False
    template["temperature"]["value"] = 0.1
    template["max_tokens"]["value"] = 8192
    node["data"]["type"] = "LanguageModelComponent"
    node["data"]["node"] = config


def _replace_edge_source_output(
    edges: list[dict[str, Any]],
    source_id: str,
    old_output: str,
    new_output: str,
) -> None:
    """기본 Agent의 response 포트를 Language Model의 text_output 포트로 바꿉니다."""

    for edge in edges:
        source_handle = edge.get("data", {}).get("sourceHandle", {})
        if edge.get("source") == source_id and source_handle.get("name") == old_output:
            source_handle["name"] = new_output


def _apply_standalone_defaults(nodes: list[dict[str, Any]]) -> None:
    for node in nodes:
        node_type = str(node.get("data", {}).get("type") or "")
        template = node.get("data", {}).get("node", {}).get("template", {})
        if not isinstance(template, dict):
            continue
        if node_type == "LanguageModelComponent":
            # Tool이 없는 모델 실행은 기본 Language Model로 처리해 tools 필드를 전송하지 않습니다.
            for field_name, value in (
                ("max_tokens", 8192),
                ("stream", False),
                ("temperature", 0.1),
            ):
                field = template.get(field_name)
                if isinstance(field, dict):
                    field["value"] = value
        for field_name, field in template.items():
            if not isinstance(field, dict):
                continue
            if field_name == "should_store_message":
                # 직접 Playground에서는 Langflow message 저장이 꺼지면 완성된 ChatOutput도 화면에 나타나지 않습니다.
                # Router의 nested 호출만 request tweak로 저장을 끄고, child Flow 기본값은 direct 실행을 위해 켭니다.
                field["value"] = True
            if field_name == "mongo_uri":
                # 실제 URI를 JSON에 넣지 않고 Langflow Credential Global Variable을
                # standalone 노드 입력으로 명시 바인딩합니다. OS 환경변수는 사용하지 않습니다.
                field["value"] = MONGO_GLOBAL_VARIABLE
                field["load_from_db"] = True
                field["advanced"] = False
                field["show"] = True
            if field_name in {"mongo_database", "collection_name", "session_collection_name"}:
                field["load_from_db"] = False
                field["advanced"] = False
                field["show"] = True
            value = field.get("value")
            if isinstance(value, str) and "agent_v5" in value:
                field["value"] = value.replace("agent_v5", "agent_v4")


def _refresh_component_node(node: dict[str, Any], path: Path) -> None:
    code = path.read_text(encoding="utf-8")
    class_match = re.search(r"^class\s+(\w+)\([^\n)]*Component\):", code, flags=re.MULTILINE)
    display_match = re.search(r'^\s+display_name\s*=\s*"([^"]+)"', code, flags=re.MULTILINE)
    description_match = re.search(r'^\s+description\s*=\s*"([^"]+)"', code, flags=re.MULTILINE)
    if not class_match or not display_match:
        raise ValueError(f"component metadata parse failed: {path}")
    component = node["data"]["node"]
    component["template"]["code"]["value"] = code
    component["display_name"] = display_match.group(1)
    component["description"] = description_match.group(1) if description_match else ""
    component.setdefault("metadata", {})["code_hash"] = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    component["metadata"]["module"] = f"custom_components.{path.stem}"
    node["data"]["type"] = class_match.group(1)


def _refresh_edge_source_types(edges: list[dict[str, Any]], node_index: dict[str, dict[str, Any]]) -> None:
    """현재 node 계약으로 edge의 data·문자열 handle·ID를 함께 다시 직렬화합니다."""

    # Langflow JSON은 같은 handle을 edge.data, sourceHandle/targetHandle 문자열,
    # edge ID에 중복 보관합니다. 기본 컴포넌트 type이나 output port가 바뀌면 세 위치를
    # 모두 갱신해야 import 시 연결이 제거되지 않습니다.
    for index, edge in enumerate(list(edges)):
        source_id = str(edge.get("source") or "")
        target_id = str(edge.get("target") or "")
        source_data = edge.get("data", {}).get("sourceHandle", {})
        target_data = edge.get("data", {}).get("targetHandle", {})
        source_name = str(source_data.get("name") or "") if isinstance(source_data, dict) else ""
        target_name = str(target_data.get("fieldName") or "") if isinstance(target_data, dict) else ""
        if source_id not in node_index or target_id not in node_index or not source_name or not target_name:
            continue
        edges[index] = _make_edge(node_index, source_id, source_name, target_id, target_name)


def _apply_component_spec(
    node: dict[str, Any],
    inputs: list[tuple[str, str, str, bool, Any]],
    outputs: list[tuple[str, str, str, str]],
    node_index: dict[str, dict[str, Any]],
) -> None:
    component = node["data"]["node"]
    code_template = component["template"]["code"]
    type_template = component["template"]["_type"]
    template: dict[str, Any] = {"_type": type_template, "code": code_template}
    for kind, name, display_name, required, value in inputs:
        template[name] = _input_template(kind, name, display_name, required, value, node_index)
    component["template"] = template
    component["field_order"] = [name for _, name, _, _, _ in inputs]
    component["outputs"] = [
        _output_template(output_type, name, display_name, method, node_index)
        for output_type, name, display_name, method in outputs
    ]
    if len(component["outputs"]) > 1:
        for output in component["outputs"]:
            output["group_outputs"] = True
    component["base_classes"] = list(dict.fromkeys(output_type for output_type, *_ in outputs))


def _input_template(
    kind: str,
    name: str,
    display_name: str,
    required: bool,
    value: Any,
    node_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if kind == "data":
        template = deepcopy(node_index["CustomComponent-5o0CN"]["data"]["node"]["template"]["payload"])
    elif kind == "message":
        template = deepcopy(node_index["CustomComponent-xpbhS"]["data"]["node"]["template"]["question"])
        template.pop("options", None)
        template["tool_mode"] = False
    elif kind == "multiline":
        template = deepcopy(node_index["Agent-nSPco"]["data"]["node"]["template"]["system_prompt"])
    elif kind == "model":
        template = deepcopy(node_index["Agent-nSPco"]["data"]["node"]["template"]["model"])
    elif kind == "secret":
        template = deepcopy(node_index["Agent-nSPco"]["data"]["node"]["template"]["api_key"])
    elif kind == "dropdown":
        template = deepcopy(node_index["CustomComponent-x6NXu"]["data"]["node"]["template"]["retrieval_mode"])
        template["options"] = ["0", "1"] if name == "max_repair_attempts" else ["dummy", "live"]
    else:
        raise ValueError(kind)
    template.update({"name": name, "display_name": display_name, "required": required})
    if value is not None or kind not in {"model", "secret"}:
        template["value"] = "" if value is None else value
    template["advanced"] = name in {
        "max_domain_items",
        "min_table_items",
        "max_table_items",
        "max_bytes",
        "max_attempts",
        "max_repair_attempts",
        "ttl_hours",
        "max_result_rows",
        "max_source_rows_per_alias",
        "max_document_bytes",
        "api_key",
    }
    return template


def _output_template(
    output_type: str,
    name: str,
    display_name: str,
    method: str,
    node_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if output_type == "Message":
        template = deepcopy(node_index["CustomComponent-fc0Vb"]["data"]["node"]["outputs"][0])
    else:
        template = deepcopy(node_index["CustomComponent-5o0CN"]["data"]["node"]["outputs"][0])
    template.update(
        {
            "name": name,
            "display_name": display_name,
            "method": method,
            "selected": output_type,
            "types": [output_type],
            "group_outputs": False,
        }
    )
    return template


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        edge["source"],
        edge["data"]["sourceHandle"]["name"],
        edge["target"],
        edge["data"]["targetHandle"]["fieldName"],
    )


def _make_edge(
    node_index: dict[str, dict[str, Any]],
    source_id: str,
    source_name: str,
    target_id: str,
    target_name: str,
) -> dict[str, Any]:
    source_node = node_index[source_id]
    target_node = node_index[target_id]
    source_output = next(item for item in source_node["data"]["node"]["outputs"] if item["name"] == source_name)
    target_input = target_node["data"]["node"]["template"][target_name]
    output_types = source_output.get("types") or [source_output.get("selected") or "Data"]
    input_types = target_input.get("input_types") or (["Message"] if target_input.get("type") == "str" else ["Data"])
    source_handle = {
        "dataType": source_node["data"]["type"],
        "id": source_id,
        "name": source_name,
        "output_types": output_types,
    }
    target_handle = {
        "fieldName": target_name,
        "id": target_id,
        "inputTypes": input_types,
        "type": target_input.get("type") or "other",
    }
    source_text = _source_handle_text(source_handle)
    target_text = _target_handle_text(target_handle)
    return {
        "animated": False,
        "className": "",
        "data": {"sourceHandle": source_handle, "targetHandle": target_handle},
        "id": f"xy-edge__{source_id}{source_text}-{target_id}{target_text}",
        "selected": False,
        "source": source_id,
        "sourceHandle": source_text,
        "target": target_id,
        "targetHandle": target_text,
    }


def _source_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _target_handle_text(value: dict[str, Any]) -> str:
    return _handle_text(value)


def _handle_text(value: dict[str, Any]) -> str:
    """Mirror Langflow frontend's stable stringify + quote substitution."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace('"', "œ")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the v5 standalone Langflow export from the audited v4 export.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    flow = build_flow(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"output": str(args.output), "nodes": len(flow["data"]["nodes"]), "edges": len(flow["data"]["edges"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
