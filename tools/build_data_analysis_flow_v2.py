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
    _apply_native_prompt_template,
    _apply_standalone_defaults,
    _edge_key,
    _input_template,
    _make_edge,
    _output_template,
    _refresh_component_node,
    _refresh_edge_source_types,
    apply_data_analysis_canvas,
    native_component_config,
)


DEFAULT_SOURCE = ROOT / "tools" / "assets" / "data_analysis_flow_v2_donor.json"
DEFAULT_TARGET = ROOT / "flow_exports" / "data_analysis_flow_v2_standalone.json"
DEFAULT_IMPORT_TARGET = ROOT / "import_ready_flows" / "01_data_analysis_flow_v2_standalone.json"
V2_COMPONENT_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"
COMMON_COMPONENT_ROOT = ROOT / "langflow_components" / "data_analysis_flow"
HELPER_LIBRARY_NODE_ID = "TextInput-AXG9a"
HELPER_LIBRARY_SOURCE = COMMON_COMPONENT_ROOT / "function_case_helper_code_input_example.py"
SPECIALIZED_PROMPT_NODE_ID = "TextInput-GRnAm"
SPECIALIZED_PROMPT_SOURCE = COMMON_COMPONENT_ROOT / "specialized_prompt_input_example_ko.md"
REPAIR_PROMPT_NODE_ID = "TextInput-v5RepairPrompt"
REPAIR_PROMPT_SOURCE = COMMON_COMPONENT_ROOT / "17b_pandas_repair_prompt_template_ko.md"

RESOLVER_NODE_ID = "CustomComponent-v2FastResolver"
INTENT_VARIABLES_NODE_ID = "CustomComponent-B1hbh"
INTENT_PROMPT_NODE_ID = "Prompt Template-AUpQz"
INTENT_MODEL_NODE_ID = "LanguageModel-intent"
INTENT_NORMALIZER_NODE_ID = "CustomComponent-5o0CN"
METADATA_CANDIDATES_NODE_ID = "CustomComponent-DXrpf"
EXECUTION_GATE_NODE_ID = "CustomComponent-v5ExecutionGate"
DUMMY_RETRIEVER_NODE_ID = "CustomComponent-Pp7d0"
PANDAS_VARIABLES_NODE_ID = "CustomComponent-fc0Vb"
PANDAS_PROMPT_NODE_ID = "Prompt Template-xtzD5"
HYBRID_EXECUTOR_NODE_ID = "CustomComponent-s3mf1"
RESULT_STORE_NODE_ID = "CustomComponent-AUrFb"
RUNTIME_CLEANUP_NODE_ID = "CustomComponent-v5RuntimeCleanup"
ANSWER_VARIABLES_NODE_ID = "CustomComponent-aKrkH"
ANSWER_PROMPT_NODE_ID = "Prompt Template-ELVKc"
HYBRID_ANSWER_NODE_ID = "CustomComponent-BVItv"
MESSAGE_ADAPTER_NODE_ID = "CustomComponent-A5y0b"
API_RESPONSE_NODE_ID = "CustomComponent-3eVde"
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

    # New dropdown fields do not exist in the donor component's current
    # template, so they must pass through the extended fallback below rather
    # than the V5 helper's fixed retrieval-mode donor lookup.
    supported = {"data", "message"}
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
        if kind == "dropdown":
            field = deepcopy(current.get(name)) if isinstance(current.get(name), dict) else None
            if not isinstance(field, dict):
                field = next(
                    (
                        deepcopy(candidate)
                        for candidate_node in node_index.values()
                        for candidate in candidate_node.get("data", {}).get("node", {}).get("template", {}).values()
                        if isinstance(candidate, dict) and candidate.get("_input_type") == "DropdownInput"
                    ),
                    None,
                )
            if not isinstance(field, dict):
                raise ValueError(f"dropdown template을 찾을 수 없습니다: {name}")
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
                    "advanced": name not in {"fast_path_enabled", "use_llm_answer"},
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


def _edge_port(edge: dict[str, Any], side: str) -> str:
    handle = edge.get("data", {}).get(f"{side}Handle", {})
    key = "name" if side == "source" else "fieldName"
    return str(handle.get(key) or "") if isinstance(handle, dict) else ""


# 함수 설명: 이전 donor에 남은 입출력 경계 어댑터를 제거하고 native Chat edge를 직접 연결합니다.
def _remove_retired_boundary_adapters(flow: dict[str, Any]) -> None:
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {str(node["id"]): node for node in nodes}
    adapter_ids = {
        str(node["id"])
        for node in nodes
        if node.get("data", {}).get("type") in {"GaiAInputAdapter", "GaiAOutputAdapter"}
    }
    if not adapter_ids:
        return

    bridged: list[tuple[str, str, str, str]] = []
    for adapter_id in adapter_ids:
        incoming = [edge for edge in edges if str(edge.get("target") or "") == adapter_id]
        outgoing = [edge for edge in edges if str(edge.get("source") or "") == adapter_id]
        for in_edge in incoming:
            source_id = str(in_edge.get("source") or "")
            source_name = _edge_port(in_edge, "source")
            if source_id not in node_index or not source_name:
                continue
            for out_edge in outgoing:
                target_id = str(out_edge.get("target") or "")
                target_name = _edge_port(out_edge, "target")
                if target_id in node_index and target_id not in adapter_ids and target_name:
                    bridged.append((source_id, source_name, target_id, target_name))

    edges[:] = [
        edge
        for edge in edges
        if str(edge.get("source") or "") not in adapter_ids
        and str(edge.get("target") or "") not in adapter_ids
    ]
    nodes[:] = [node for node in nodes if str(node["id"]) not in adapter_ids]
    node_index = {str(node["id"]): node for node in nodes}
    existing = {_edge_key(edge) for edge in edges}
    for source_id, source_name, target_id, target_name in bridged:
        if source_id not in node_index or target_id not in node_index:
            continue
        edge = _make_edge(node_index, source_id, source_name, target_id, target_name)
        key = _edge_key(edge)
        if key not in existing:
            edges.append(edge)
            existing.add(key)


def build_flow(source: Path = DEFAULT_SOURCE) -> dict[str, Any]:
    # The donor is the already-built, audited V5 standalone export. Re-running
    # the V5 builder against that export would apply its graph migration twice.
    flow = json.loads(source.read_text(encoding="utf-8-sig"))
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {node["id"]: node for node in nodes}
    _remove_retired_boundary_adapters(flow)
    nodes = flow["data"]["nodes"]
    edges = flow["data"]["edges"]
    node_index = {node["id"]: node for node in nodes}

    # LFX 1.11 marks the 1.9 Prompt Template as a breaking upgrade. Recreate
    # the one prompt node that remains in the V2 graph from the 1.11 native
    # template, retaining its dynamic input names and current prompt text.
    _apply_native_prompt_template(
        node_index[INTENT_PROMPT_NODE_ID],
        native_component_config("Prompt Template"),
    )

    # The donor export has a text-input helper library. Keep it synchronized
    # with the canonical source so intent-normalizer arguments always match the
    # Helper signature after a fresh import.
    node_index[HELPER_LIBRARY_NODE_ID]["data"]["node"]["template"]["input_value"]["value"] = (
        HELPER_LIBRARY_SOURCE.read_text(encoding="utf-8")
    )
    # The product-token instruction belongs to the visible specialized prompt
    # input, not to the common intent normalizer. Keep this Text Input synced
    # so imported Flow 01 files carry the same small specialized contract.
    node_index[SPECIALIZED_PROMPT_NODE_ID]["data"]["node"]["template"]["input_value"]["value"] = (
        SPECIALIZED_PROMPT_SOURCE.read_text(encoding="utf-8")
    )
    # Repair is a plain Text Input node rather than a Prompt Template node, so
    # it must be refreshed explicitly when the canonical prompt file changes.
    node_index[REPAIR_PROMPT_NODE_ID]["data"]["node"]["template"]["input_value"]["value"] = (
        REPAIR_PROMPT_SOURCE.read_text(encoding="utf-8")
    )

    # Flow 01은 일반 질문과 검증된 세션 상태만 받습니다. 외부에서 임의의
    # 상위 결과 참조를 주입하지 않고, 저장된 세션 계약으로 후속 질문을 처리합니다.
    _set_embedded_source(
        node_index["CustomComponent-xpbhS"],
        _common_component_path("00_analysis_request_loader.py"),
    )
    _apply_component_spec(
        node_index["CustomComponent-xpbhS"],
        [
            ("message", "question", "사용자 질문", True, ""),
            ("data", "previous_state", "이전 상태", False, None),
        ],
        [("Data", "payload_out", "분석 요청 페이로드", "build_payload")],
        node_index,
    )

    # 01D는 Common 후보 선별 계약을 실제 Intent LLM에 전달하는 활성 노드다.
    # donor export에 남은 예전 embedded source를 그대로 쓰면 저장소 Python과
    # Flow JSON이 달라져 후보 선택/토큰 정책이 서로 다른 상태가 된다.
    _set_embedded_source(
        node_index[METADATA_CANDIDATES_NODE_ID],
        _common_component_path("01d_metadata_candidates_builder.py"),
    )
    # Report Context and ordinary multi-turn questions both pass through the
    # shared follow-up hint and result-loader nodes.  Refresh them explicitly
    # instead of inheriting the donor's embedded revision so snapshot routing,
    # expiry, session and completeness guards stay identical in source/export.
    _set_embedded_source(
        node_index["CustomComponent-HFsYn"],
        _common_component_path("01e_followup_hint_builder.py"),
    )
    _set_embedded_source(
        node_index["CustomComponent-O8vfz"],
        _common_component_path("05_mongodb_result_loader.py"),
    )

    # Intent defaults to the existing Legacy contract. Compact is an explicit
    # opt-in and publishes its exact expected dialect to the Prompt, Router,
    # and Normalizer so the same request can never silently fall back.
    _set_embedded_source(node_index[INTENT_VARIABLES_NODE_ID], _component_path("02_intent_variables_builder.py"))
    _set_embedded_source(node_index[INTENT_NORMALIZER_NODE_ID], _component_path("04_intent_plan_normalizer.py"))
    _apply_extended_component_spec(
        node_index[INTENT_VARIABLES_NODE_ID],
        [
            ("data", "payload", "페이로드", True, None),
            ("data", "metadata_candidates_in", "메타데이터 후보", False, None),
            ("dropdown", "intent_contract_mode", "Intent 계약 모드", False, "legacy"),
        ],
        [
            ("Message", "question", "사용자 질문", "build_question"),
            ("Message", "state_summary", "상태/요청 컨텍스트 JSON", "build_state_summary"),
            ("Message", "metadata_candidates", "메타데이터 후보 JSON", "build_metadata_candidates"),
            ("Message", "output_schema", "출력 스키마 JSON", "build_output_schema"),
            ("Message", "expected_dialect", "예상 Intent Dialect", "build_expected_dialect"),
        ],
        node_index,
    )
    intent_mode_field = node_index[INTENT_VARIABLES_NODE_ID]["data"]["node"]["template"]["intent_contract_mode"]
    intent_mode_field["options"] = ["legacy", "compact"]
    intent_mode_field["advanced"] = False
    intent_mode_field["info"] = (
        "legacy가 기본값입니다. compact는 manufacturing.intent.compact.v1 최소 계약을 엄격 검증합니다."
    )
    _apply_extended_component_spec(
        node_index[INTENT_NORMALIZER_NODE_ID],
        [
            ("data", "payload", "페이로드", True, None),
            ("message", "llm_response", "의도 LLM 응답", True, ""),
            ("message", "expected_dialect", "예상 Intent Dialect", False, "manufacturing.intent.legacy.v1"),
            ("data", "metadata_candidates", "메타데이터 후보", False, None),
        ],
        [("Data", "payload_out", "페이로드 출력", "build_payload")],
        node_index,
    )
    # Keep shared retrieval contracts embedded at the same revision as the
    # repository sources.  V2 is built from a donor export, so refreshing only
    # V2 nodes would otherwise leave stale common hydrator/binder code behind.
    _set_embedded_source(
        node_index["CustomComponent-v5Hydrate"],
        _common_component_path("04a_trusted_retrieval_job_hydrator.py"),
    )
    _set_embedded_source(
        node_index["CustomComponent-v5UpstreamBinder"],
        _common_component_path("05a_upstream_entity_parameter_binder.py"),
    )
    _set_embedded_source(
        node_index[EXECUTION_GATE_NODE_ID],
        _common_component_path("14a_retrieval_execution_gate.py"),
    )
    # The dummy retriever is part of the standalone validation/runtime switch.
    # It must be refreshed together with the live retrieval contract so an
    # imported Flow cannot run a stale fixture schema after source updates.
    _set_embedded_source(
        node_index[DUMMY_RETRIEVER_NODE_ID],
        _common_component_path("08_dummy_data_retriever.py"),
    )
    # The result store and cleanup node carry runtime-only checkpoint rows.
    # Refresh both shared sources whenever V2 is rebuilt so downloaded
    # intermediate artifacts cannot diverge from the standalone Python files.
    _set_embedded_source(
        node_index[RESULT_STORE_NODE_ID],
        _common_component_path("23_mongodb_result_store.py"),
    )
    _set_embedded_source(
        node_index[RUNTIME_CLEANUP_NODE_ID],
        _common_component_path("24_runtime_payload_cleanup.py"),
    )
    # The API response node owns the public table contract. Refresh it with
    # the shared result store and cleanup sources so the standalone source,
    # Flow export, and import-ready bundle stay byte-for-byte synchronized.
    _set_embedded_source(
        node_index[API_RESPONSE_NODE_ID],
        _common_component_path("22_api_response_builder.py"),
    )
    _apply_extended_component_spec(
        node_index[API_RESPONSE_NODE_ID],
        [
            ("data", "payload", "페이로드", True, None),
            ("message", "display_message", "채팅 표시 메시지", False, ""),
            ("int", "intermediate_preview_limit", "중간 결과 미리보기 행 수", False, 5),
        ],
        [("Data", "api_response", "API 응답", "build_payload")],
        node_index,
    )
    # The session-state loader and writer remain part of the active follow-up
    # contract. Refresh both shared sources; otherwise Report-context expiry or
    # CAS revisions added to the standalone files can silently diverge from the
    # embedded Flow 01 implementation.
    _set_embedded_source(
        node_index["CustomComponent-Fti0r"],
        ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py",
    )
    _set_embedded_source(
        node_index["CustomComponent-fXdS4"],
        ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py",
    )
    node_index[INTENT_PROMPT_NODE_ID]["data"]["node"]["template"]["template"]["value"] = (
        _component_path("03_intent_prompt_template_ko.md").read_text(encoding="utf-8")
    )
    intent_prompt_component = node_index[INTENT_PROMPT_NODE_ID]["data"]["node"]
    intent_prompt_template = intent_prompt_component["template"]
    if "expected_dialect" not in intent_prompt_template:
        expected_dialect_field = deepcopy(intent_prompt_template["question"])
        expected_dialect_field.update(
            {
                "name": "expected_dialect",
                "display_name": "expected_dialect",
                "value": "manufacturing.intent.legacy.v1",
            }
        )
        intent_prompt_template["expected_dialect"] = expected_dialect_field
    prompt_dynamic_fields = intent_prompt_component.setdefault("custom_fields", {}).setdefault("template", [])
    if "expected_dialect" not in prompt_dynamic_fields:
        prompt_dynamic_fields.append("expected_dialect")

    # Intent planning is allowed only after 01D has proved that a Table Catalog
    # dataset is available. This replaces the native model node in place while
    # preserving the stable node ID and model settings.
    old_intent_model = node_index[INTENT_MODEL_NODE_ID]
    catalog_guarded_intent = deepcopy(node_index[HYBRID_ANSWER_NODE_ID])
    catalog_guarded_intent["id"] = INTENT_MODEL_NODE_ID
    catalog_guarded_intent["data"]["id"] = INTENT_MODEL_NODE_ID
    catalog_guarded_intent["position"] = deepcopy(old_intent_model.get("position", {}))
    catalog_guarded_intent["selected"] = False
    _set_embedded_source(
        catalog_guarded_intent,
        _component_path("03b_catalog_guarded_intent_router.py"),
    )
    _apply_extended_component_spec(
        catalog_guarded_intent,
        [
            ("data", "payload", "요청 페이로드", True, None),
            ("data", "metadata_candidates", "메타데이터 후보", True, None),
            ("message", "intent_prompt", "의도 분석 프롬프트", False, ""),
            ("message", "expected_dialect", "예상 Intent Dialect", False, "manufacturing.intent.legacy.v1"),
            ("model", "model", "의도 분석 언어 모델", False, None),
            ("secret", "api_key", "의도 분석 모델 API Key", False, "GOOGLE_API_KEY"),
        ],
        [("Message", "text_output", "의도 분석 응답", "build_response")],
        node_index,
    )
    old_intent_template = old_intent_model["data"]["node"].get("template", {})
    for field_name, display_name in (
        ("model", "의도 분석 언어 모델"),
        ("api_key", "의도 분석 모델 API Key"),
    ):
        if isinstance(old_intent_template.get(field_name), dict):
            catalog_guarded_intent["data"]["node"]["template"][field_name] = deepcopy(
                old_intent_template[field_name]
            )
            catalog_guarded_intent["data"]["node"]["template"][field_name].update(
                {"name": field_name, "display_name": display_name, "required": False}
            )
    catalog_guarded_intent["data"]["node"].update(
        {
            "display_name": "03B Catalog 검증 Intent Router",
            "description": "등록된 Table Catalog가 확인된 경우에만 Intent LLM을 호출하고, 메타데이터 연결 실패 시 추측 계획 없이 중단합니다.",
        }
    )
    nodes[nodes.index(old_intent_model)] = catalog_guarded_intent
    node_index[INTENT_MODEL_NODE_ID] = catalog_guarded_intent

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
            ("bool", "use_llm_answer", "Complex 답변 LLM 사용", False, True),
            ("dropdown", "answer_policy", "Complex 답변 호출 정책", False, "auto"),
            (
                "multiline",
                "answer_prompt_template",
                "답변 프롬프트 템플릿",
                False,
                _common_component_path("19_answer_prompt_template_ko.md").read_text(encoding="utf-8"),
            ),
            ("message", "domain_answer_guidance", "도메인 특화 답변 지침", False, ""),
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
    node_index[HYBRID_ANSWER_NODE_ID]["data"]["node"]["template"]["use_llm_answer"]["info"] = (
        "활성화하면 Complex만 답변 LLM을 호출하고, 비활성화하면 Fast와 Complex 모두 고정 로직으로 답변합니다."
    )
    answer_policy_field = node_index[HYBRID_ANSWER_NODE_ID]["data"]["node"]["template"]["answer_policy"]
    answer_policy_field.update(
        {
            "options": ["always", "auto", "never"],
            "value": "auto",
            "advanced": False,
            "show": True,
            "info": (
                "always는 기존처럼 Complex 답변 LLM을 항상 호출하고, auto는 검증된 표준 결과만 고정 답변으로 처리하며, "
                "never는 Complex에서도 답변 LLM을 호출하지 않습니다."
            ),
        }
    )

    _set_embedded_source(
        node_index[MESSAGE_ADAPTER_NODE_ID],
        _component_path("21_v2_answer_message_adapter.py"),
    )
    # Keep diagnostics opt-in while the user-facing intermediate-result
    # preview remains a separate, bounded display option.
    adapter_template = node_index[MESSAGE_ADAPTER_NODE_ID]["data"]["node"].get("template", {})
    # These legacy body sections were intentionally removed.  Follow-up
    # questions remain Message metadata, and curated intermediate tables have
    # their own display switch below.
    for obsolete_field in ("show_analysis_evidence", "show_next_questions"):
        adapter_template.pop(obsolete_field, None)
    if isinstance(adapter_template.get("show_pandas_code"), dict):
        adapter_template["show_pandas_code"]["value"] = True
    if not isinstance(adapter_template.get("show_intermediate_results"), dict):
        # Older donor exports predate the optional intermediate-preview input.
        # Add the schema from the existing BoolInput template so the source
        # component and the generated Flow keep the same visible contract.
        pandas_code_field = adapter_template.get("show_pandas_code")
        if isinstance(pandas_code_field, dict):
            intermediate_field = deepcopy(pandas_code_field)
        else:
            intermediate_field = {
                "_input_type": "BoolInput",
                "type": "bool",
                "show": True,
                "advanced": True,
            }
        intermediate_field.update(
            {
                "name": "show_intermediate_results",
                "display_name": "중간 결과 표시",
                "required": False,
                "value": False,
                "type": "bool",
                "_input_type": "BoolInput",
                "advanced": True,
                "show": True,
            }
        )
        adapter_template["show_intermediate_results"] = intermediate_field
    else:
        adapter_template["show_intermediate_results"]["value"] = False
    if not isinstance(adapter_template.get("intermediate_preview_limit"), dict):
        # Keep a dedicated user-visible cap for checkpoint tables.  The
        # executor retains at most five rows, so the display cap defaults to
        # three and is bounded by the standalone component at runtime.
        preview_field = adapter_template.get("table_preview_limit")
        if isinstance(preview_field, dict):
            intermediate_preview_field = deepcopy(preview_field)
        else:
            intermediate_preview_field = {
                "_input_type": "IntInput",
                "type": "int",
                "show": True,
                "advanced": True,
            }
        intermediate_preview_field.update(
            {
                "name": "intermediate_preview_limit",
                "display_name": "중간 결과 미리보기 행 수",
                "info": "단계별 표에 표시할 최대 행 수입니다. 1~5 범위로 적용되며 답변 LLM 프롬프트에는 포함하지 않습니다.",
                "required": False,
                "value": 3,
                "type": "int",
                "_input_type": "IntInput",
                "advanced": True,
                "show": True,
            }
        )
        adapter_template["intermediate_preview_limit"] = intermediate_preview_field
    else:
        adapter_template["intermediate_preview_limit"]["value"] = 3
    adapter_node = node_index[MESSAGE_ADAPTER_NODE_ID]["data"]["node"]
    # The serialized frontend order must be identical to AnswerMessageAdapter.inputs.
    # A donor flow predating this option used to append it after show_pandas_code,
    # which made Langflow reject the node even though the source itself was valid.
    field_order = [
        name
        for name in list(adapter_node.get("field_order") or [])
        if name
        not in {
            "show_analysis_evidence",
            "show_next_questions",
            "show_intermediate_results",
            "intermediate_preview_limit",
        }
    ]
    insertion_index = (
        field_order.index("table_preview_limit") + 1
        if "table_preview_limit" in field_order
        else field_order.index("show_result_table") + 1
        if "show_result_table" in field_order
        else len(field_order)
    )
    field_order[insertion_index:insertion_index] = [
        "show_intermediate_results",
        "intermediate_preview_limit",
    ]
    adapter_node["field_order"] = field_order

    # Node 20 owns AnswerEvidence and renders its prompt only after the visible
    # BoolInput branch. Removing the upstream answer materializer guarantees
    # that answer LLM OFF never serializes rows into a discarded prompt.
    removed_node_ids = {*REMOVED_MODEL_NODE_IDS, ANSWER_PROMPT_NODE_ID, ANSWER_VARIABLES_NODE_ID}
    nodes[:] = [node for node in nodes if node["id"] not in removed_node_ids]
    for node_id in removed_node_ids:
        node_index.pop(node_id, None)
    edges[:] = [
        edge
        for edge in edges
        if edge["source"] not in removed_node_ids and edge["target"] not in removed_node_ids
    ]

    removals = {
        (INTENT_PROMPT_NODE_ID, "prompt", INTENT_MODEL_NODE_ID, "input_value"),
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
        ("CustomComponent-HFsYn", "payload_out", INTENT_MODEL_NODE_ID, "payload"),
        (METADATA_CANDIDATES_NODE_ID, "metadata_candidates", INTENT_MODEL_NODE_ID, "metadata_candidates"),
        (INTENT_PROMPT_NODE_ID, "prompt", INTENT_MODEL_NODE_ID, "intent_prompt"),
        (INTENT_VARIABLES_NODE_ID, "expected_dialect", INTENT_PROMPT_NODE_ID, "expected_dialect"),
        (INTENT_VARIABLES_NODE_ID, "expected_dialect", INTENT_MODEL_NODE_ID, "expected_dialect"),
        (INTENT_VARIABLES_NODE_ID, "expected_dialect", INTENT_NORMALIZER_NODE_ID, "expected_dialect"),
        (EXECUTION_GATE_NODE_ID, "payload_out", RESOLVER_NODE_ID, "payload"),
        (RESOLVER_NODE_ID, "payload_out", PANDAS_VARIABLES_NODE_ID, "payload"),
        (RESOLVER_NODE_ID, "payload_out", PANDAS_PROMPT_NODE_ID, "payload"),
        (RESOLVER_NODE_ID, "payload_out", HYBRID_EXECUTOR_NODE_ID, "payload"),
        ("CustomComponent-v5Helper", "selected_helper_code", PANDAS_PROMPT_NODE_ID, "function_case_helper_code"),
        (PANDAS_PROMPT_NODE_ID, "pandas_prompt", HYBRID_EXECUTOR_NODE_ID, "pandas_prompt"),
        ("TextInput-VFbHh", "text", HYBRID_ANSWER_NODE_ID, "domain_answer_guidance"),
    ]
    existing = {_edge_key(edge) for edge in edges}
    for source_id, source_name, target_id, target_name in additions:
        key = (source_id, source_name, target_id, target_name)
        if key not in existing:
            edges.append(_make_edge(node_index, source_id, source_name, target_id, target_name))
            existing.add(key)

    _apply_standalone_defaults(nodes)
    _refresh_edge_source_types(edges, node_index)

    endpoint_name = "metadata-driven-v5-data-analysis"
    flow["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, endpoint_name))
    flow["name"] = "01. v5_data_analysis"
    flow["endpoint_name"] = endpoint_name
    flow["description"] = (
        "Standalone Data Analysis Flow V2: the existing metadata, trusted catalog, retrieval, state, download, "
        "and complex pandas path are preserved. Single-source analyses with complete canonical contracts use a "
        "deterministic Fast Path with no pandas-generation or answer-model call; Complex requests preserve the "
        "existing safe pandas and one-attempt repair path, with a visible option to disable answer-model synthesis."
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
