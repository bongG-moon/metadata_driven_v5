#!/usr/bin/env python3
"""Build the isolated Data Analysis Flow V2 from the current audited V5 flow.

The V2 graph keeps the metadata/retrieval/state pipeline intact, but replaces
the always-on pandas and answer Language Model nodes with standalone hybrid
components.  Those components invoke a model only after the deterministic
route resolver has selected the complex route.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_v5_data_analysis_flow import (  # noqa: E402
    TARGET_LANGFLOW_VERSION,
    _apply_component_spec,
    _apply_standalone_defaults,
    _edge_key,
    _input_template,
    _make_edge,
    _output_template,
    _refresh_component_node,
    _refresh_edge_source_types,
    apply_data_analysis_canvas,
)


DEFAULT_SOURCE = ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json"
DEFAULT_TARGET = ROOT / "flow_exports" / "data_analysis_flow_v2_standalone.json"
DEFAULT_IMPORT_TARGET = ROOT / "import_ready_flows" / "12_data_analysis_flow_v2_standalone.json"
V2_COMPONENT_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"
COMMON_COMPONENT_ROOT = ROOT / "langflow_components" / "data_analysis_flow"

RESOLVER_NODE_ID = "CustomComponent-v2FastResolver"
INTENT_VARIABLES_NODE_ID = "CustomComponent-B1hbh"
INTENT_PROMPT_NODE_ID = "Prompt Template-AUpQz"
INTENT_NORMALIZER_NODE_ID = "CustomComponent-5o0CN"
EXECUTION_GATE_NODE_ID = "CustomComponent-v5ExecutionGate"
PANDAS_VARIABLES_NODE_ID = "CustomComponent-fc0Vb"
PANDAS_PROMPT_NODE_ID = "Prompt Template-xtzD5"
HYBRID_EXECUTOR_NODE_ID = "CustomComponent-s3mf1"
RESULT_STORE_NODE_ID = "CustomComponent-AUrFb"
ANSWER_VARIABLES_NODE_ID = "CustomComponent-aKrkH"
ANSWER_PROMPT_NODE_ID = "Prompt Template-ELVKc"
HYBRID_ANSWER_NODE_ID = "CustomComponent-BVItv"
MESSAGE_ADAPTER_NODE_ID = "CustomComponent-A5y0b"
REMOVED_MODEL_NODE_IDS = {"LanguageModel-pandas", "LanguageModel-answer"}


def _component_path(filename: str) -> Path:
    return V2_COMPONENT_ROOT / filename


def _common_component_path(filename: str) -> Path:
    return COMMON_COMPONENT_ROOT / filename


def _apply_extended_component_spec(
    node: dict[str, Any],
    inputs: list[tuple[str, str, str, bool, Any]],
    outputs: list[tuple[str, str, str, str]],
    node_index: dict[str, dict[str, Any]],
) -> None:
    """Apply a component spec including BoolInput and IntInput templates."""

    supported = {"data", "message", "dropdown"}
    if all(kind in supported for kind, *_ in inputs):
        _apply_component_spec(node, inputs, outputs, node_index)
        return

    component = node["data"]["node"]
    current = component["template"]
    template: dict[str, Any] = {
        "_type": current["_type"],
        "code": current["code"],
    }
    for kind, name, display_name, required, value in inputs:
        if kind == "dropdown" and isinstance(current.get(name), dict):
            field = deepcopy(current[name])
            field.update(
                {
                    "name": name,
                    "display_name": display_name,
                    "required": required,
                    "value": "" if value is None else value,
                }
            )
        elif kind == "multiline":
            field = deepcopy(node_index["TextInput-v5RepairPrompt"]["data"]["node"]["template"]["input_value"])
            field.update(
                {
                    "name": name,
                    "display_name": display_name,
                    "required": required,
                    "value": "" if value is None else value,
                    "advanced": True,
                    "show": True,
                }
            )
        elif kind in supported:
            field = _input_template(kind, name, display_name, required, value, node_index)
        elif kind == "model":
            field = deepcopy(node_index["LanguageModel-pandas"]["data"]["node"]["template"]["model"])
            field.update({"name": name, "display_name": display_name, "required": required})
            if value is not None:
                field["value"] = value
        elif kind == "secret":
            field = deepcopy(node_index["LanguageModel-pandas"]["data"]["node"]["template"]["api_key"])
            field.update(
                {
                    "name": name,
                    "display_name": display_name,
                    "required": required,
                    "value": "" if value is None else value,
                    "advanced": True,
                }
            )
        elif kind == "bool":
            field = deepcopy(node_index[INTENT_PROMPT_NODE_ID]["data"]["node"]["template"]["use_double_brackets"])
            field.update(
                {
                    "name": name,
                    "display_name": display_name,
                    "required": required,
                    "value": bool(value),
                    "advanced": name != "fast_path_enabled",
                    "show": True,
                    "type": "bool",
                    "_input_type": "BoolInput",
                }
            )
        elif kind == "int":
            field = deepcopy(node_index["CustomComponent-A5y0b"]["data"]["node"]["template"]["table_preview_limit"])
            field.update(
                {
                    "name": name,
                    "display_name": display_name,
                    "required": required,
                    "value": int(value),
                    "advanced": True,
                    "show": True,
                    "type": "int",
                    "_input_type": "IntInput",
                }
            )
        else:
            raise ValueError(f"unsupported V2 input kind: {kind}")
        template[name] = field

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


def _set_embedded_source(node: dict[str, Any], source_path: Path) -> None:
    _refresh_component_node(node, source_path)
    code = source_path.read_text(encoding="utf-8")
    node["data"]["node"].setdefault("metadata", {})["code_hash"] = hashlib.sha256(
        code.encode("utf-8")
    ).hexdigest()[:12]


def build_flow(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    # The donor is the already-built, audited V5 standalone export. Re-running
    # the V5 builder against that export would apply its graph migration twice.
    flow = json.loads(source.read_text(encoding="utf-8-sig"))
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {node["id"]: node for node in nodes}

    # Keep the existing intent contract, but expose the V2 recipe/calculation
    # schema to the intent model. No dataset, process, product, or physical
    # column is introduced here.
    _set_embedded_source(node_index[INTENT_VARIABLES_NODE_ID], _component_path("02_intent_variables_builder.py"))
    _set_embedded_source(node_index[INTENT_NORMALIZER_NODE_ID], _component_path("04_intent_plan_normalizer.py"))
    node_index[INTENT_PROMPT_NODE_ID]["data"]["node"]["template"]["template"]["value"] = (
        _component_path("03_intent_prompt_template_ko.md").read_text(encoding="utf-8")
    )

    # Add the resolver from a generic Data->Data custom component shell.
    resolver = deepcopy(node_index["CustomComponent-5o0CN"])
    resolver["id"] = RESOLVER_NODE_ID
    resolver["data"]["id"] = RESOLVER_NODE_ID
    resolver["position"] = {"x": 1980.0, "y": 1510.0}
    resolver["selected"] = False
    _set_embedded_source(resolver, _component_path("14b_simple_analysis_contract_resolver.py"))
    _apply_extended_component_spec(
        resolver,
        [
            ("data", "payload", "조회 페이로드", True, None),
            ("bool", "fast_path_enabled", "Fast Path 사용", False, True),
            ("int", "detail_row_limit", "상세 조회 최대 행", False, 5000),
            ("int", "max_pivot_columns", "Pivot 최대 컬럼", False, 50),
        ],
        [("Data", "payload_out", "경로 결정 페이로드", "build_payload")],
        node_index,
    )
    nodes.append(resolver)
    node_index[RESOLVER_NODE_ID] = resolver

    # V2 keeps helper selection small and renders the full pandas prompt only
    # after the resolver has selected Complex. The former Prompt Template node
    # becomes a standalone route-aware materializer so Fast never serializes
    # plan/schema/preview prompt variables.
    _set_embedded_source(
        node_index[PANDAS_VARIABLES_NODE_ID],
        _component_path("15_function_case_selection_builder.py"),
    )
    _apply_extended_component_spec(
        node_index[PANDAS_VARIABLES_NODE_ID],
        [("data", "payload", "조회 페이로드", True, None)],
        [("Message", "function_case_selection_json", "Function Case 선택 정보 JSON", "build_selection")],
        node_index,
    )
    node_index[PANDAS_VARIABLES_NODE_ID]["data"]["node"].update(
        {
            "display_name": "15 V2 Function Case 선택 정보 생성기",
            "description": "Fast/Complex 공통 helper 선택에 필요한 작은 계약만 생성합니다.",
        }
    )

    old_pandas_prompt = node_index[PANDAS_PROMPT_NODE_ID]
    lazy_pandas_prompt = deepcopy(node_index[INTENT_NORMALIZER_NODE_ID])
    lazy_pandas_prompt["id"] = PANDAS_PROMPT_NODE_ID
    lazy_pandas_prompt["data"]["id"] = PANDAS_PROMPT_NODE_ID
    lazy_pandas_prompt["position"] = deepcopy(old_pandas_prompt.get("position", {}))
    lazy_pandas_prompt["selected"] = False
    _set_embedded_source(
        lazy_pandas_prompt,
        _component_path("16_route_aware_pandas_prompt_builder.py"),
    )
    _apply_extended_component_spec(
        lazy_pandas_prompt,
        [
            ("data", "payload", "경로 결정 페이로드", True, None),
            (
                "multiline",
                "prompt_template",
                "pandas Prompt Template",
                True,
                _common_component_path("16_pandas_prompt_template_ko.md").read_text(encoding="utf-8"),
            ),
            ("message", "function_case_helper_code", "선택 Function Case Helper", False, ""),
        ],
        [("Message", "pandas_prompt", "경로 인식 pandas Prompt", "build_prompt")],
        node_index,
    )
    lazy_pandas_prompt["data"]["node"].update(
        {
            "display_name": "16 V2 경로 인식 pandas Prompt 생성기",
            "description": "Complex일 때만 pandas prompt 변수를 직렬화하고 Fast/Blocked에서는 빈 prompt를 반환합니다.",
        }
    )
    prompt_index = nodes.index(old_pandas_prompt)
    nodes[prompt_index] = lazy_pandas_prompt
    node_index[PANDAS_PROMPT_NODE_ID] = lazy_pandas_prompt

    # The answer-variable node likewise owns lazy rendering. Its V2 context
    # keeps result shape in result_summary and criteria in applied_scope only.
    _set_embedded_source(
        node_index[ANSWER_VARIABLES_NODE_ID],
        _component_path("18_route_aware_answer_prompt_builder.py"),
    )
    _apply_extended_component_spec(
        node_index[ANSWER_VARIABLES_NODE_ID],
        [
            ("data", "payload", "분석 결과 페이로드", True, None),
            (
                "multiline",
                "prompt_template",
                "Answer Prompt Template",
                True,
                _common_component_path("19_answer_prompt_template_ko.md").read_text(encoding="utf-8"),
            ),
            ("message", "domain_answer_guidance", "도메인 특화 답변 지침", False, ""),
        ],
        [("Message", "answer_prompt", "경로 인식 Answer Prompt", "build_prompt")],
        node_index,
    )
    node_index[ANSWER_VARIABLES_NODE_ID]["data"]["node"].update(
        {
            "display_name": "18 V2 경로 인식 Answer Prompt 생성기",
            "description": "Complex일 때만 중복 제거된 답변 context와 prompt를 생성합니다.",
        }
    )

    # Preserve the provider selections from the current model nodes while
    # moving them into the lazy hybrid components.
    pandas_model_template = deepcopy(
        node_index["LanguageModel-pandas"]["data"]["node"]["template"]["model"]
    )
    answer_model_template = deepcopy(
        node_index["LanguageModel-answer"]["data"]["node"]["template"]["model"]
    )

    _set_embedded_source(node_index[HYBRID_EXECUTOR_NODE_ID], _component_path("17_hybrid_analysis_executor.py"))
    _apply_extended_component_spec(
        node_index[HYBRID_EXECUTOR_NODE_ID],
        [
            ("data", "payload", "페이로드", True, None),
            ("message", "pandas_prompt", "pandas 생성 프롬프트", True, ""),
            ("message", "function_case_helper_code", "선택 Function Case Helper", False, ""),
            ("message", "repair_prompt_template", "pandas Repair Prompt", True, ""),
            ("model", "model", "pandas 생성/복구 언어 모델", True, None),
            ("secret", "api_key", "pandas 모델 API 키", False, "GOOGLE_API_KEY"),
            ("dropdown", "max_repair_attempts", "최대 Repair 횟수", False, "1"),
            ("bool", "fallback_to_complex_on_internal_error", "Fast 내부 오류 시 Complex 재시도", False, False),
        ],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )
    node_index[HYBRID_EXECUTOR_NODE_ID]["data"]["node"]["template"]["model"] = pandas_model_template
    node_index[HYBRID_EXECUTOR_NODE_ID]["data"]["node"]["template"]["model"].update(
        {"name": "model", "display_name": "pandas 생성/복구 언어 모델", "required": True}
    )
    node_index[HYBRID_EXECUTOR_NODE_ID]["data"]["node"]["template"]["max_repair_attempts"]["options"] = ["0", "1"]

    _set_embedded_source(node_index[HYBRID_ANSWER_NODE_ID], _component_path("20_hybrid_answer_builder.py"))
    _apply_extended_component_spec(
        node_index[HYBRID_ANSWER_NODE_ID],
        [
            ("data", "payload", "페이로드", True, None),
            ("message", "answer_prompt", "답변 생성 프롬프트", False, ""),
            ("model", "model", "답변 언어 모델", False, None),
            ("secret", "api_key", "답변 모델 API 키", False, "GOOGLE_API_KEY"),
        ],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )
    node_index[HYBRID_ANSWER_NODE_ID]["data"]["node"]["template"]["model"] = answer_model_template
    node_index[HYBRID_ANSWER_NODE_ID]["data"]["node"]["template"]["model"].update(
        {"name": "model", "display_name": "답변 언어 모델", "required": False}
    )

    _set_embedded_source(
        node_index[MESSAGE_ADAPTER_NODE_ID],
        _component_path("21_v2_answer_message_adapter.py"),
    )

    # Remove the two always-on model nodes and the now-redundant answer Prompt
    # Template node. The pandas Prompt node ID is retained by the lazy custom
    # component above so existing canvas references remain stable.
    removed_node_ids = {*REMOVED_MODEL_NODE_IDS, ANSWER_PROMPT_NODE_ID}
    nodes[:] = [node for node in nodes if node["id"] not in removed_node_ids]
    for node_id in removed_node_ids:
        node_index.pop(node_id, None)
    edges[:] = [
        edge
        for edge in edges
        if edge["source"] not in removed_node_ids and edge["target"] not in removed_node_ids
    ]

    removals = {
        (EXECUTION_GATE_NODE_ID, "payload_out", PANDAS_VARIABLES_NODE_ID, "payload"),
        (EXECUTION_GATE_NODE_ID, "payload_out", HYBRID_EXECUTOR_NODE_ID, "payload"),
        (PANDAS_VARIABLES_NODE_ID, "intent_plan_json", PANDAS_PROMPT_NODE_ID, "intent_plan_json"),
        (PANDAS_VARIABLES_NODE_ID, "source_schema_json", PANDAS_PROMPT_NODE_ID, "source_schema_json"),
        (PANDAS_VARIABLES_NODE_ID, "source_preview_json", PANDAS_PROMPT_NODE_ID, "source_preview_json"),
        (PANDAS_VARIABLES_NODE_ID, "output_contract_json", PANDAS_PROMPT_NODE_ID, "output_contract_json"),
        (PANDAS_VARIABLES_NODE_ID, "function_case_selection_json", PANDAS_PROMPT_NODE_ID, "function_case_selection_json"),
        ("CustomComponent-v5Helper", "selected_helper_code", PANDAS_PROMPT_NODE_ID, "function_case_helper_code"),
        (PANDAS_PROMPT_NODE_ID, "prompt", HYBRID_EXECUTOR_NODE_ID, "pandas_prompt"),
    }
    edges[:] = [edge for edge in edges if _edge_key(edge) not in removals]

    additions = [
        (EXECUTION_GATE_NODE_ID, "payload_out", RESOLVER_NODE_ID, "payload"),
        (RESOLVER_NODE_ID, "payload_out", PANDAS_VARIABLES_NODE_ID, "payload"),
        (RESOLVER_NODE_ID, "payload_out", PANDAS_PROMPT_NODE_ID, "payload"),
        (RESOLVER_NODE_ID, "payload_out", HYBRID_EXECUTOR_NODE_ID, "payload"),
        ("CustomComponent-v5Helper", "selected_helper_code", PANDAS_PROMPT_NODE_ID, "function_case_helper_code"),
        (PANDAS_PROMPT_NODE_ID, "pandas_prompt", HYBRID_EXECUTOR_NODE_ID, "pandas_prompt"),
        ("TextInput-VFbHh", "text", ANSWER_VARIABLES_NODE_ID, "domain_answer_guidance"),
        (ANSWER_VARIABLES_NODE_ID, "answer_prompt", HYBRID_ANSWER_NODE_ID, "answer_prompt"),
    ]
    existing = {_edge_key(edge) for edge in edges}
    for source_id, source_name, target_id, target_name in additions:
        key = (source_id, source_name, target_id, target_name)
        if key not in existing:
            edges.append(_make_edge(node_index, source_id, source_name, target_id, target_name))
            existing.add(key)

    _apply_standalone_defaults(nodes)
    _refresh_edge_source_types(edges, node_index)

    endpoint_name = "metadata-driven-v5-data-analysis-v2"
    flow["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, endpoint_name))
    flow["name"] = "12. data_analysis_flow_v2"
    flow["endpoint_name"] = endpoint_name
    flow["description"] = (
        "Standalone Data Analysis Flow V2: the existing metadata, trusted catalog, retrieval, state, download, "
        "and complex pandas path are preserved. Single-source analyses with complete canonical contracts use a "
        "deterministic Fast Path with no pandas-generation or answer-model call; all other supported requests use "
        "the existing safe pandas, one-attempt repair, and answer-model path."
    )
    flow["tags"] = sorted(set([*flow.get("tags", []), "v2-fast-path", "hybrid-analysis"]))
    flow["last_tested_version"] = TARGET_LANGFLOW_VERSION
    for node in nodes:
        component = node.get("data", {}).get("node")
        if isinstance(component, dict):
            component["lf_version"] = TARGET_LANGFLOW_VERSION
    apply_data_analysis_canvas(flow, "v2")
    return flow


def _write_flow(path: Path, flow: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the isolated Data Analysis Flow V2 standalone exports.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--import-output", type=Path, default=DEFAULT_IMPORT_TARGET)
    args = parser.parse_args()
    flow = build_flow(args.source)
    _write_flow(args.output, flow)
    _write_flow(args.import_output, flow)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "import_output": str(args.import_output),
                "nodes": len(flow["data"]["nodes"]),
                "edges": len(flow["data"]["edges"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
