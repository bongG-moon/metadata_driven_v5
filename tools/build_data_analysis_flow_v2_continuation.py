#!/usr/bin/env python3
"""Build the isolated continuation-enabled Data Analysis V2 flow."""

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

from tools.build_data_analysis_flow_v2 import (  # noqa: E402
    TARGET_LANGFLOW_VERSION,
    _apply_extended_component_spec,
    _remove_gaia_adapters,
    _set_embedded_source,
)
from tools.build_v5_data_analysis_flow import (  # noqa: E402
    _apply_standalone_defaults,
    _edge_key,
    _make_edge,
    _refresh_edge_source_types,
)

DEFAULT_SOURCE = ROOT / "flow_exports" / "data_analysis_flow_v2_standalone.json"
DEFAULT_TARGET = ROOT / "flow_exports" / "08_data_analysis_flow_v2_continuation_standalone.json"
COMPONENT_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2_continuation"
V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"

REQUEST_NODE_ID = "CustomComponent-xpbhS"
INTENT_VARIABLES_NODE_ID = "CustomComponent-B1hbh"
INTENT_PROMPT_NODE_ID = "Prompt Template-AUpQz"
INTENT_MODEL_NODE_ID = "LanguageModel-intent"
INTENT_NORMALIZER_NODE_ID = "CustomComponent-5o0CN"
METADATA_CANDIDATES_NODE_ID = "CustomComponent-DXrpf"
TABLE_CATALOG_LOADER_NODE_ID = "MongoDBDomainMetadataLoader-OM3Hg"
RESULT_LOADER_NODE_ID = "CustomComponent-O8vfz"
EXECUTOR_NODE_ID = "CustomComponent-s3mf1"
ANSWER_NODE_ID = "CustomComponent-BVItv"
API_NODE_ID = "CustomComponent-3eVde"
COMPILER_NODE_ID = "CustomComponent-v2ContinuationCompiler"
UPSTREAM_BINDER_NODE_ID = "CustomComponent-v5UpstreamBinder"
ALIAS_NORMALIZER_NODE_ID = "CustomComponent-v2ContinuationAliasNormalizer"
CONTINUATION_RULES_NODE_ID = "TextInput-v2ContinuationRules"
CATALOG_CLOSURE_NODE_ID = "CustomComponent-v2ContinuationCatalogClosure"
HELPER_LIBRARY_NODE_ID = "TextInput-AXG9a"
HELPER_LIBRARY_SOURCE = ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"


def _component(filename: str) -> Path:
    return COMPONENT_ROOT / filename


def _replace_node(nodes: list[dict[str, Any]], old: dict[str, Any], new: dict[str, Any]) -> None:
    nodes[nodes.index(old)] = new


def _continuation_helper_library() -> str:
    """Embed the canonical helper library while repairing only the legacy X-number ORG typo."""

    source = HELPER_LIBRARY_SOURCE.read_text(encoding="utf-8")
    legacy = "return _match(['ORG'], token, 'exact')"
    corrected = "return _match(['ORG'], token[1:], 'exact')"
    if legacy in source:
        source = source.replace(legacy, corrected)
    if corrected not in source:
        raise ValueError("match_product_tokens X-number ORG contract is missing")
    return source


def build_flow(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    flow = json.loads(source.read_text(encoding="utf-8-sig"))
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {node["id"]: node for node in nodes}
    _remove_gaia_adapters(flow)
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {node["id"]: node for node in nodes}

    _set_embedded_source(node_index[REQUEST_NODE_ID], _component("00_continuation_analysis_request_loader.py"))
    _apply_extended_component_spec(
        node_index[REQUEST_NODE_ID],
        [
            ("message", "question", "사용자 질문", True, ""),
            ("message", "upstream_result_ref", "상위 결과 참조", False, ""),
            ("message", "continuation_ref", "Continuation 참조", False, ""),
            ("multiline", "continuation_contract", "Continuation 계약 JSON", False, ""),
            ("bool", "skip_intermediate_answer", "중간 답변 생략", False, False),
            ("data", "previous_state", "이전 상태", False, None),
        ],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )
    node_index[REQUEST_NODE_ID]["data"]["node"].update(
        {
            "display_name": "00 Continuation 분석 요청 로더",
            "description": "일반 질문과 구조화된 2단계 재개 입력을 하나의 요청 계약으로 만듭니다.",
        }
    )

    _set_embedded_source(node_index[INTENT_VARIABLES_NODE_ID], _component("02_intent_variables_builder.py"))
    node_index[HELPER_LIBRARY_NODE_ID]["data"]["node"]["template"]["input_value"]["value"] = (
        _continuation_helper_library()
    )
    base_prompt = (V2_ROOT / "03_intent_prompt_template_ko.md").read_text(encoding="utf-8")
    continuation_rules = _component("03_continuation_rules_prompt_ko.md").read_text(encoding="utf-8")
    node_index[INTENT_PROMPT_NODE_ID]["data"]["node"]["template"]["template"]["value"] = (
        base_prompt.rstrip() + "\n\n{tool_placeholder}\n"
    )
    continuation_rules_node = deepcopy(node_index["TextInput-GRnAm"])
    continuation_rules_node["id"] = CONTINUATION_RULES_NODE_ID
    continuation_rules_node["data"]["id"] = CONTINUATION_RULES_NODE_ID
    continuation_rules_node["position"] = {"x": -600.0, "y": 1125.0}
    continuation_rules_node["positionAbsolute"] = deepcopy(continuation_rules_node["position"])
    continuation_rules_node["selected"] = False
    continuation_rules_component = continuation_rules_node["data"]["node"]
    continuation_rules_component["display_name"] = "03A Continuation 계획 규칙"
    continuation_rules_component["description"] = (
        "Catalog 근거가 있는 최대 2단계 종속 조회의 Typed IR 작성 규칙을 intent prompt에 제공합니다."
    )
    continuation_rules_component["template"]["input_value"]["value"] = continuation_rules
    continuation_rules_component["template"]["input_value"]["display_name"] = "Continuation 계획 규칙"
    nodes.append(continuation_rules_node)
    node_index[CONTINUATION_RULES_NODE_ID] = continuation_rules_node

    catalog_closure = deepcopy(node_index[METADATA_CANDIDATES_NODE_ID])
    catalog_closure["id"] = CATALOG_CLOSURE_NODE_ID
    catalog_closure["data"]["id"] = CATALOG_CLOSURE_NODE_ID
    catalog_closure["position"] = {"x": 0.0, "y": 1375.0}
    catalog_closure["positionAbsolute"] = deepcopy(catalog_closure["position"])
    catalog_closure["selected"] = False
    _set_embedded_source(
        catalog_closure,
        _component("01e_dependency_catalog_candidate_closure.py"),
    )
    _apply_extended_component_spec(
        catalog_closure,
        [
            ("data", "metadata_candidates", "메타데이터 후보", True, None),
            ("data", "table_catalog_items", "전체 Table Catalog", True, None),
            ("message", "max_table_items", "테이블 최대 후보 수", False, "5"),
        ],
        [
            (
                "Data",
                "metadata_candidates_out",
                "폐쇄 적용 메타데이터 후보",
                "build_payload",
            )
        ],
        node_index,
    )
    catalog_closure["data"]["node"].update(
        {
            "display_name": "01E 종속 Table Catalog 후보 폐쇄기",
            "description": "선택 Domain과 Catalog에 명시된 종속 dataset을 5개 후보 안에서 우선 보존합니다.",
        }
    )
    nodes.append(catalog_closure)
    node_index[CATALOG_CLOSURE_NODE_ID] = catalog_closure

    old_intent_model = node_index[INTENT_MODEL_NODE_ID]
    intent_model = deepcopy(node_index[ANSWER_NODE_ID])
    intent_model["id"] = INTENT_MODEL_NODE_ID
    intent_model["data"]["id"] = INTENT_MODEL_NODE_ID
    intent_model["position"] = deepcopy(old_intent_model.get("position", {}))
    intent_model["selected"] = False
    _set_embedded_source(intent_model, _component("03b_continuation_aware_intent_router.py"))
    # The V2 donor no longer contains the original pandas model node. The
    # shared spec helper only needs a node carrying standard model/api fields.
    spec_index = dict(node_index)
    spec_index["LanguageModel-pandas"] = node_index[EXECUTOR_NODE_ID]
    _apply_extended_component_spec(
        intent_model,
        [
            ("data", "payload", "요청 페이로드", True, None),
            ("data", "metadata_candidates", "메타데이터 후보", False, None),
            ("message", "intent_prompt", "의도 분석 프롬프트", False, ""),
            ("model", "model", "의도 분석 언어 모델", False, None),
            ("secret", "api_key", "의도 모델 API 키", False, "GOOGLE_API_KEY"),
            ("message", "mongo_uri", "MongoDB 연결 URI", False, ""),
            ("message", "mongo_database", "MongoDB 데이터베이스", False, "datagov"),
            ("message", "collection_name", "결과 컬렉션", False, "agent_v4_result_store"),
        ],
        [("Message", "text_output", "의도 응답", "build_response")],
        spec_index,
    )
    old_template = old_intent_model["data"]["node"].get("template", {})
    if isinstance(old_template.get("model"), dict):
        intent_model["data"]["node"]["template"]["model"] = deepcopy(old_template["model"])
        intent_model["data"]["node"]["template"]["model"].update(
            {"name": "model", "display_name": "의도 분석 언어 모델", "required": False}
        )
    if isinstance(old_template.get("api_key"), dict):
        intent_model["data"]["node"]["template"]["api_key"] = deepcopy(old_template["api_key"])
        intent_model["data"]["node"]["template"]["api_key"].update(
            {"name": "api_key", "display_name": "의도 모델 API 키", "required": False}
        )
    result_loader_template = node_index[RESULT_LOADER_NODE_ID]["data"]["node"].get("template", {})
    for field_name, display_name in (
        ("mongo_uri", "MongoDB 연결 URI"),
        ("mongo_database", "MongoDB 데이터베이스"),
        ("collection_name", "결과 컬렉션"),
    ):
        if isinstance(result_loader_template.get(field_name), dict):
            intent_model["data"]["node"]["template"][field_name] = deepcopy(
                result_loader_template[field_name]
            )
            intent_model["data"]["node"]["template"][field_name].update(
                {"name": field_name, "display_name": display_name, "required": False}
            )
    intent_model["data"]["node"].update(
        {
            "display_name": "03B Continuation 인식 의도 라우터",
            "description": "일반 실행만 모델을 호출하고 continuation 재개는 저장된 Typed IR로 우회합니다.",
        }
    )
    _replace_node(nodes, old_intent_model, intent_model)
    node_index[INTENT_MODEL_NODE_ID] = intent_model

    compiler = deepcopy(node_index[INTENT_NORMALIZER_NODE_ID])
    compiler["id"] = COMPILER_NODE_ID
    compiler["data"]["id"] = COMPILER_NODE_ID
    old_position = old_intent_model.get("position", {})
    compiler["position"] = {
        "x": float(old_position.get("x", 0)) + 300.0,
        "y": float(old_position.get("y", 0)) + 650.0,
    }
    compiler["selected"] = False
    _set_embedded_source(compiler, _component("04b_dependent_retrieval_plan_compiler.py"))
    _apply_extended_component_spec(
        compiler,
        [
            ("data", "payload", "요청 페이로드", True, None),
            ("message", "llm_response", "의도 분석 응답", True, ""),
            ("data", "metadata_candidates", "메타데이터 후보", False, None),
        ],
        [("Message", "compiled_response", "컴파일된 의도 응답", "build_response")],
        node_index,
    )
    compiler["data"]["node"].update(
        {
            "display_name": "04B 종속 조회 계획 컴파일러",
            "description": "Trusted Catalog 근거로 최대 2단계 계약을 검증하고 현재 stage만 flat plan으로 투영합니다.",
        }
    )
    nodes.append(compiler)
    node_index[COMPILER_NODE_ID] = compiler

    alias_normalizer = deepcopy(node_index[UPSTREAM_BINDER_NODE_ID])
    alias_normalizer["id"] = ALIAS_NORMALIZER_NODE_ID
    alias_normalizer["data"]["id"] = ALIAS_NORMALIZER_NODE_ID
    alias_normalizer["position"] = {"x": 1875.0, "y": 750.0}
    alias_normalizer["selected"] = False
    _set_embedded_source(
        alias_normalizer,
        _component("05a_continuation_binding_alias_normalizer.py"),
    )
    _apply_extended_component_spec(
        alias_normalizer,
        [("data", "payload", "페이로드", True, None)],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )
    alias_normalizer["data"]["node"].update(
        {
            "display_name": "05A Continuation 참조 별칭 정규화기",
            "description": "trusted previous_result 예약 별칭을 명시적 continuation의 upstream_result로 통일합니다.",
        }
    )
    nodes.append(alias_normalizer)
    node_index[ALIAS_NORMALIZER_NODE_ID] = alias_normalizer

    _set_embedded_source(node_index[RESULT_LOADER_NODE_ID], _component("05_continuation_mongodb_result_loader.py"))
    node_index[RESULT_LOADER_NODE_ID]["data"]["node"].update(
        {
            "display_name": "05 Continuation MongoDB 결과 로더",
            "description": "동일 세션의 complete 결과와 저장된 plan hash를 검증한 뒤 upstream_result를 복원합니다.",
        }
    )
    _set_embedded_source(node_index[EXECUTOR_NODE_ID], _component("17_continuation_hybrid_analysis_executor.py"))
    node_index[EXECUTOR_NODE_ID]["data"]["node"].update(
        {
            "display_name": "17 Continuation Hybrid 분석 실행기",
            "description": "기존 Fast/Complex 실행과 same-row extreme selection 및 left enrichment를 지원합니다.",
        }
    )
    _set_embedded_source(node_index[ANSWER_NODE_ID], _component("20_continuation_hybrid_answer_builder.py"))
    node_index[ANSWER_NODE_ID]["data"]["node"].update(
        {
            "display_name": "20 Continuation Hybrid 답변 생성기",
            "description": "pending 중간 단계는 답변 LLM 없이 종료하고 final 단계만 기존 정책으로 답합니다.",
        }
    )
    _set_embedded_source(node_index[API_NODE_ID], _component("22_continuation_api_response_builder.py"))
    node_index[API_NODE_ID]["data"]["node"].update(
        {
            "display_name": "22 Continuation API 응답 생성기",
            "description": "답변과 함께 bounded continuation contract 및 result_ref를 구조화해 반환합니다.",
        }
    )

    removals = {
        (INTENT_PROMPT_NODE_ID, "prompt", INTENT_MODEL_NODE_ID, "input_value"),
        (INTENT_MODEL_NODE_ID, "text_output", INTENT_NORMALIZER_NODE_ID, "llm_response"),
        (RESULT_LOADER_NODE_ID, "payload_out", UPSTREAM_BINDER_NODE_ID, "payload"),
        (METADATA_CANDIDATES_NODE_ID, "metadata_candidates", INTENT_VARIABLES_NODE_ID, "metadata_candidates_in"),
        (METADATA_CANDIDATES_NODE_ID, "metadata_candidates", INTENT_NORMALIZER_NODE_ID, "metadata_candidates"),
    }
    edges[:] = [edge for edge in edges if _edge_key(edge) not in removals]
    additions = [
        ("CustomComponent-HFsYn", "payload_out", INTENT_MODEL_NODE_ID, "payload"),
        (INTENT_PROMPT_NODE_ID, "prompt", INTENT_MODEL_NODE_ID, "intent_prompt"),
        (CONTINUATION_RULES_NODE_ID, "text", INTENT_PROMPT_NODE_ID, "tool_placeholder"),
        ("CustomComponent-HFsYn", "payload_out", COMPILER_NODE_ID, "payload"),
        (INTENT_MODEL_NODE_ID, "text_output", COMPILER_NODE_ID, "llm_response"),
        (METADATA_CANDIDATES_NODE_ID, "metadata_candidates", CATALOG_CLOSURE_NODE_ID, "metadata_candidates"),
        (TABLE_CATALOG_LOADER_NODE_ID, "table_catalog_items", CATALOG_CLOSURE_NODE_ID, "table_catalog_items"),
        (CATALOG_CLOSURE_NODE_ID, "metadata_candidates_out", INTENT_VARIABLES_NODE_ID, "metadata_candidates_in"),
        (CATALOG_CLOSURE_NODE_ID, "metadata_candidates_out", INTENT_NORMALIZER_NODE_ID, "metadata_candidates"),
        (CATALOG_CLOSURE_NODE_ID, "metadata_candidates_out", INTENT_MODEL_NODE_ID, "metadata_candidates"),
        (CATALOG_CLOSURE_NODE_ID, "metadata_candidates_out", COMPILER_NODE_ID, "metadata_candidates"),
        (COMPILER_NODE_ID, "compiled_response", INTENT_NORMALIZER_NODE_ID, "llm_response"),
        (RESULT_LOADER_NODE_ID, "payload_out", ALIAS_NORMALIZER_NODE_ID, "payload"),
        (ALIAS_NORMALIZER_NODE_ID, "payload_out", UPSTREAM_BINDER_NODE_ID, "payload"),
    ]
    existing = {_edge_key(edge) for edge in edges}
    for source_id, source_name, target_id, target_name in additions:
        key = (source_id, source_name, target_id, target_name)
        if key not in existing:
            edges.append(_make_edge(node_index, source_id, source_name, target_id, target_name))
            existing.add(key)

    continuation_note = {
        "data": {
            "id": "note-data-analysis-continuation-contract",
            "node": {
                "description": (
                    "## Continuation 2단계 실행\n\n"
                    "1. **03B/04B**: Catalog 근거가 있는 종속 조회만 2단계 계획으로 확정합니다.\n"
                    "2. **Stage 1 → 23 저장소**: 1차 결과와 전체 Typed IR을 저장하고 `result_ref`와 4 KiB 이하 공개 계약만 반환합니다.\n"
                    "3. **Stage 2 재개**: 03B가 같은 MongoDB 저장소에서 계획을 검증·복원하므로 의도 LLM을 호출하지 않습니다.\n"
                    "4. **05/05A**: 같은 저장 결과 행을 `upstream_result`로 복원하고 trusted binding에만 전달합니다.\n\n"
                    "03B, 05, 23 노드는 반드시 같은 Mongo URI·database·result collection을 사용해야 합니다."
                ),
                "display_name": "",
                "documentation": "",
                "template": {"backgroundColor": "amber"},
                "lf_version": TARGET_LANGFLOW_VERSION,
            },
            "type": "note",
        },
        "dragging": False,
        "height": 600,
        "id": "note-data-analysis-continuation-contract",
        "position": {"x": 650.0, "y": 2100.0},
        "positionAbsolute": {"x": 650.0, "y": 2100.0},
        "resizing": False,
        "selected": False,
        "style": {"height": 600, "width": 1300},
        "type": "noteNode",
        "width": 1300,
    }
    nodes.append(continuation_note)

    _apply_standalone_defaults(nodes)
    _refresh_edge_source_types(edges, node_index)
    endpoint_name = "metadata-driven-v5-data-analysis-continuation"
    flow["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, endpoint_name))
    flow["name"] = "08. v5_data_analysis_continuation"
    flow["endpoint_name"] = endpoint_name
    flow["description"] = (
        "Isolated Data Analysis V2 continuation flow. Ordinary requests retain the canonical V2 graph. "
        "Catalog-proven dependent retrieval can run in at most two fail-closed stages using result_ref, "
        "with no intermediate answer model and no intent model call during resume."
    )
    flow["tags"] = sorted(set([*flow.get("tags", []), "v2-continuation", "dependent-retrieval", "max-two-stages"]))
    flow["last_tested_version"] = TARGET_LANGFLOW_VERSION
    for node in nodes:
        component = node.get("data", {}).get("node")
        if isinstance(component, dict):
            component["lf_version"] = TARGET_LANGFLOW_VERSION
    flow.setdefault("metadata", {})["continuation_contract"] = {
        "version": "analysis.dependent_retrieval.v1",
        "max_stages": 2,
        "public_inputs": [
            "upstream_result_ref",
            "continuation_ref",
            "continuation_contract",
            "skip_intermediate_answer",
        ],
        "api_field": "continuation",
    }
    return flow


def _write(path: Path, flow: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build continuation-enabled Data Analysis V2.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args()
    flow = build_flow(args.source)
    _write(args.output, flow)
    print(json.dumps({"output": str(args.output), "nodes": len(flow["data"]["nodes"]), "edges": len(flow["data"]["edges"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
