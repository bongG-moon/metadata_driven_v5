from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "flow_exports" / "data_analysis_flow_v4_reference.json"
DEFAULT_TARGET = ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json"
REPAIR_PROMPT_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "17b_pandas_repair_prompt_template_ko.md"
HELPER_LIBRARY_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"
REPAIR_PROMPT_NODE_ID = "TextInput-v5RepairPrompt"

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
    "CustomComponent-v5Helper": {
        "file": "data_analysis_flow/15a_selected_helper_code_builder.py",
        "position": {"x": -1420.0, "y": 2290.0},
        "inputs": [
            ("message", "function_case_selection_json", "Function Case 선택 JSON", True, ""),
            ("message", "helper_library", "전체 helper library", False, ""),
        ],
        "outputs": [("Message", "selected_helper_code", "선택 helper 코드", "build_code")],
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

    _apply_standalone_defaults(nodes)

    removals = {
        ("CustomComponent-5o0CN", "payload_out", "CustomComponent-O8vfz", "payload"),
        ("TextInput-AXG9a", "text", "Prompt Template-xtzD5", "function_case_helper_code"),
        ("CustomComponent-BVItv", "payload_out", "CustomComponent-A5y0b", "payload"),
        ("CustomComponent-BVItv", "payload_out", "CustomComponent-3eVde", "payload"),
    }
    edges[:] = [edge for edge in edges if _edge_key(edge) not in removals]

    additions = [
        ("CustomComponent-HFsYn", "payload_out", "CustomComponent-DXrpf", "payload"),
        ("CustomComponent-5o0CN", "payload_out", "CustomComponent-v5Hydrate", "payload"),
        ("MongoDBDomainMetadataLoader-OM3Hg", "table_catalog_items", "CustomComponent-v5Hydrate", "table_catalog_items"),
        ("CustomComponent-v5Hydrate", "payload_out", "CustomComponent-O8vfz", "payload"),
        ("CustomComponent-fc0Vb", "function_case_selection_json", "CustomComponent-v5Helper", "function_case_selection_json"),
        ("TextInput-AXG9a", "text", "CustomComponent-v5Helper", "helper_library"),
        ("CustomComponent-v5Helper", "selected_helper_code", "Prompt Template-xtzD5", "function_case_helper_code"),
        ("CustomComponent-v5Helper", "selected_helper_code", "CustomComponent-s3mf1", "function_case_helper_code"),
        (REPAIR_PROMPT_NODE_ID, "text", "CustomComponent-s3mf1", "repair_prompt_template"),
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

    flow["name"] = "metadata_driven_v5_data_analysis_standalone"
    flow["description"] = (
        "v5 standalone flow (dummy default, live retrievers included): bounded metadata candidates, "
        "trusted catalog hydration, thin retrieval branches, selected helper code, visible raw Repair Prompt, failure-only one-attempt pandas repair, "
        "one finalization path, and compact API payload with explicit repair audit details."
    )
    flow["endpoint_name"] = "metadata-driven-v5-data-analysis"
    flow["tags"] = sorted(set([*flow.get("tags", []), "v5", "dummy-default", "live-ready", "standalone"]))
    return flow


def _component_path(relative_path: str) -> Path:
    return ROOT / "langflow_components" / relative_path


def _apply_standalone_defaults(nodes: list[dict[str, Any]]) -> None:
    for node in nodes:
        template = node.get("data", {}).get("node", {}).get("template", {})
        if not isinstance(template, dict):
            continue
        for field_name, field in template.items():
            if not isinstance(field, dict):
                continue
            if field_name == "mongo_uri":
                field["value"] = ""
                field["load_from_db"] = False
            value = field.get("value")
            if isinstance(value, str) and "agent_v5" in value:
                field["value"] = value.replace("agent_v5", "agent_v4")


def _refresh_component_node(node: dict[str, Any], path: Path) -> None:
    code = path.read_text(encoding="utf-8")
    class_match = re.search(r"^class\s+(\w+)\(Component\):", code, flags=re.MULTILINE)
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
    args.output.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "nodes": len(flow["data"]["nodes"]), "edges": len(flow["data"]["edges"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
