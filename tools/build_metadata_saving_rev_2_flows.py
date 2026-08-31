"""Build isolated rev_2 variants of the three metadata saving flows.

The canonical nine-flow bundle and its Router remain unchanged.  These outputs
live under ``flow_exports/rev_2`` and ``import_ready_flows/rev_2`` so operators
can import and test them independently before switching any Tool target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_v5_auxiliary_flows import (
    COMPONENT_ROOT,
    TARGET_LANGFLOW_VERSION,
    SAVING_SPECS,
    _sticky_note_node,
    _set_message_storage,
    _set_value,
    _stamp_flow_version,
    add_edge,
    custom_node,
    empty_flow,
    language_model_node,
    load_donor,
    native_node,
    prompt_node,
    prototypes,
)


REV2_COMPONENT_ROOT = COMPONENT_ROOT / "metadata_saving_rev_2_common"
REV2_EXPORT_ROOT = ROOT / "flow_exports" / "rev_2"
REV2_IMPORT_ROOT = ROOT / "import_ready_flows" / "rev_2"
REV2_ZIP_PATH = ROOT / "import_ready_flows_rev_2.zip"
REV2_BUNDLE_FILE = "00_metadata_saving_rev_2_ALL_FLOWS.json"
REV2_CONTRACT_VERSION = "metadata_authoring.rev_2.v1"
REV2_NOTE_PREFIX = "note-rev2-metadata-saving-"
REV2_NOTE_COUNT = 5

REV2_DISPLAY_NAMES = {
    "domain": "02. v5_domain_saving_rev_2",
    "table_catalog": "03. v5_table_catalog_saving_rev_2",
    "main_flow_filter": "04. v5_main_flow_filter_saving_rev_2",
}
REV2_FILE_NAMES = {
    "domain": "02_domain_saving_flow_v5_rev_2_standalone.json",
    "table_catalog": "03_table_catalog_saving_flow_v5_rev_2_standalone.json",
    "main_flow_filter": "04_main_flow_filter_saving_flow_v5_rev_2_standalone.json",
}


def _rev2_sticky_note_specs(flow_key: str) -> list[dict[str, Any]]:
    """Describe the rev_2 authoring canvas without adding executable nodes."""

    # Table Catalog authoring is deliberately lighter than the domain/main
    # filter rev_2 paths.  Its operators already have a proven legacy writer
    # flow, so rev_2 only adds a best-effort wording transform before that
    # existing path.  Do not describe snapshot/guard nodes that are not on
    # this graph: the canvas itself should make the non-blocking contract clear.
    if flow_key == "table_catalog":
        return [
            {
                "id": "01-request",
                "description": (
                    "## ① 테이블 카탈로그 등록 요청\n\n"
                    "- **Chat Input / 요청 로더**: 자연어 등록 요청과 테스트 실행 여부를 정리합니다.\n"
                    "- **중복 처리 정책**: skip·merge·replace·create_new 중 선택한 방식을 기존 저장 경로에 전달합니다."
                ),
                "x": -80.0,
                "y": -760.0,
                "width": 610,
                "height": 300,
                "color": "blue",
            },
            {
                "id": "02-initial-transform",
                "description": (
                    "## ② 비차단 초기 변환\n\n"
                    "- **초기 변환 Prompt / LLM / 변환기**: 설명 문장을 읽기 쉽게 정리합니다.\n"
                    "- SQL·placeholder·컬럼 mapping이 있으면 원문을 그대로 보존하며, 이 단계는 저장을 차단하지 않습니다."
                ),
                "x": 570.0,
                "y": -760.0,
                "width": 800,
                "height": 320,
                "color": "blue",
            },
            {
                "id": "03-legacy-extract",
                "description": (
                    "## ③ 기존 후보 생성·정규화\n\n"
                    "- **기존 Variables / Prompt / Language Model / 정규화기**: 기존 03번과 같은 방식으로 dataset·source·컬럼 후보를 생성합니다.\n"
                    "- 새 사전 계약 Gate나 후보 차단 단계를 추가하지 않습니다."
                ),
                "x": 1410.0,
                "y": -760.0,
                "width": 920,
                "height": 330,
                "color": "amber",
            },
            {
                "id": "04-legacy-write",
                "description": (
                    "## ④ 기존 중복 검토·저장\n\n"
                    "- **유사도 확인기 / Writer**: 기존 03번의 실제 필수 검증과 중복 처리 정책만 적용합니다.\n"
                    "- 테스트 실행 또는 실제 저장은 기존 MongoDB 계약을 그대로 사용합니다."
                ),
                "x": 2370.0,
                "y": -760.0,
                "width": 880,
                "height": 320,
                "color": "amber",
            },
            {
                "id": "05-response",
                "description": (
                    "## ⑤ 결과·채널 출력\n\n"
                    "- **Response / Portal 계약 보강 / Message / API / Chat Output**: 기존 03번과 같은 등록 결과를 채팅과 API 응답으로 전달합니다.\n"
                    "- Portal이 사용하는 `metadata_authoring`·`data`·`write_result`·`trace` 필드는 출력 단계에서만 보강하며 저장 결과는 바꾸지 않습니다.\n"
                    "- 초기 변환 실패는 trace에만 남기며 결과를 막지 않습니다."
                ),
                "x": 3290.0,
                "y": -760.0,
                "width": 940,
                "height": 320,
                "color": "blue",
            },
        ]

    details = {
        "domain": ("도메인", "도메인 identity·별칭"),
        "table_catalog": ("테이블 카탈로그", "dataset_key·물리 컬럼 매핑"),
        "main_flow_filter": ("Main Flow Filter", "filter_key·표준 컬럼 후보"),
    }
    label, identity = details[flow_key]
    return [
        {
            "id": "01-request",
            "description": (
                f"## ① {label} 등록 요청·실행 모드\n\n"
                "- **Chat Input / 요청 로더**: 자연어 등록 요청과 테스트 실행 여부를 정리합니다.\n"
                "- **중복 처리 정책**: skip·merge·replace·create_new 중 선택한 방식을 다음 단계에 전달합니다."
            ),
            "x": -80.0,
            "y": -760.0,
            "width": 610,
            "height": 300,
            "color": "blue",
        },
        {
            "id": "02-context",
            "description": (
                "## ② 활성 메타데이터 문맥\n\n"
                "- **Metadata Snapshot / Authoring Context**: 현재 활성 도메인·Table Catalog·Main Flow Filter를 읽어 등록 문맥을 만듭니다.\n"
                "- 읽기 실패는 진단으로 남기며, 이후 계약 검증에서 처리합니다."
            ),
            "x": 570.0,
            "y": -760.0,
            "width": 800,
            "height": 320,
            "color": "blue",
        },
        {
            "id": "03-refine-extract",
            "description": (
                "## ③ 원문 정제·후보 추출·복구\n\n"
                "- **Prompt / Language Model / 정제기**: 원문을 읽기 쉬운 등록 초안과 참조 정보로 정리합니다.\n"
                "- **추출기 / Candidate Repair**: 초안에서 구조화 후보를 만들고 형식 오류만 복구합니다."
            ),
            "x": 1410.0,
            "y": -760.0,
            "width": 900,
            "height": 330,
            "color": "amber",
        },
        {
            "id": "04-contract-write",
            "description": (
                "## ④ 정규화·계약 검증·저장\n\n"
                f"- **정규화기 / Contract Guard**: 후보의 `{identity}`와 저장 계약을 확인합니다.\n"
                "- **유사도 확인기 / Writer**: 중복 처리 정책에 따라 테스트 실행 또는 실제 저장을 수행합니다."
            ),
            "x": 2350.0,
            "y": -760.0,
            "width": 900,
            "height": 320,
            "color": "amber",
        },
        {
            "id": "05-response",
            "description": (
                "## ⑤ 결과 보강·채널 출력\n\n"
                "- **Response / Response Enricher**: 등록 후보, 검수 결과, 다음 조치를 사람이 읽기 쉽게 정리합니다.\n"
                "- **Message / API / Chat Output**: 동일한 최종 결과를 채팅과 API 응답으로 전달합니다."
            ),
            "x": 3290.0,
            "y": -760.0,
            "width": 1150,
            "height": 320,
            "color": "blue",
        },
    ]


def _apply_rev2_sticky_notes(flow: dict[str, Any], flow_key: str) -> dict[str, Any]:
    """Attach five documentation-only notes without changing runtime graph behavior."""

    prefix = f"{REV2_NOTE_PREFIX}{flow_key}-"
    nodes = flow.get("data", {}).get("nodes", [])
    nodes[:] = [node for node in nodes if not str(node.get("id") or "").startswith(prefix)]
    for spec in _rev2_sticky_note_specs(flow_key):
        nodes.append(
            _sticky_note_node(
                f"{prefix}{spec['id']}",
                spec["description"],
                x=spec["x"],
                y=spec["y"],
                width=spec["width"],
                height=spec["height"],
                color=spec["color"],
            )
        )
    flow["data"]["viewport"] = {"x": 165.0, "y": 300.0, "zoom": 0.27}
    return flow


def _is_note_node(node: dict[str, Any]) -> bool:
    return str(node.get("type") or "") == "noteNode"


def _build_table_catalog_saving_flow_rev_2(donor: dict[str, Any], spec: Any) -> dict[str, Any]:
    """Build the non-blocking Table Catalog variant on top of the proven legacy flow.

    Unlike the domain and main-filter rev_2 variants, Table Catalog requests often
    contain a complete source-local SQL and mapping contract.  A second snapshot /
    repair / global guard chain added more rejection points than value.  Keep the
    original Request -> Variables -> Extract -> Normalizer -> Matcher -> Writer
    path intact, with only an advisory text transform before it.
    """

    proto = prototypes(donor)
    display_name = REV2_DISPLAY_NAMES[spec.slug]
    endpoint = f"metadata-driven-v5-{spec.slug.replace('_', '-')}-saving-rev-2"
    flow = empty_flow(
        donor,
        display_name,
        (
            "Isolated rev_2 Table Catalog saving flow: a non-blocking initial request "
            "transform followed by the existing legacy extraction, normalizer, matcher, "
            "writer, and response path."
        ),
        endpoint,
        ["v5", "standalone", "metadata-authoring", "rev-2", "legacy-writer", "nonblocking-transform"],
    )
    base_folder = COMPONENT_ROOT / spec.folder
    suffix = f"{spec.slug}-rev-2"
    nodes: dict[str, dict[str, Any]] = {}

    def add(name: str, node: dict[str, Any]) -> dict[str, Any]:
        nodes[name] = node
        flow["data"]["nodes"].append(node)
        return node

    chat = add("chat", native_node(proto["chat_input"], f"ChatInput-{suffix}", 0, 0))
    _set_message_storage(chat, True)
    request = add("request", custom_node(proto["custom"], f"Request-{suffix}", base_folder / spec.request, 320, 0))
    _set_value(request["data"]["node"]["template"], "dry_run", True)
    duplicate_action = request["data"]["node"]["template"].get("duplicate_action")
    if isinstance(duplicate_action, dict):
        duplicate_action["options"] = ["skip", "merge", "replace", "create_new"]
        duplicate_action["value"] = "skip"

    # Two instances keep the graph acyclic.  The first only exposes the
    # untouched source text to the initial prompt; the second consumes the LLM
    # response and produces the advisory transformed payload.
    initial_source = add(
        "initial_source",
        custom_node(
            proto["custom"],
            f"InitialSource-{suffix}",
            base_folder / "01_table_catalog_initial_transformer.py",
            650,
            0,
        ),
    )
    # Langflow 1.11 restores the node-level selected output while importing a
    # custom component.  This source instance only feeds the initial prompt,
    # so pin its text output instead of letting import select implicitly.
    initial_source["data"]["selected_output"] = "source_text"
    initial_prompt = add(
        "initial_prompt",
        prompt_node(
            proto["prompt"],
            f"PromptInitialTransform-{suffix}",
            (base_folder / "01_table_catalog_initial_transform_prompt_ko.md").read_text(encoding="utf-8"),
            960,
            0,
        ),
    )
    initial_model = add(
        "initial_model",
        language_model_node(
            proto["language_model"],
            f"LanguageModelInitialTransform-{suffix}",
            1270,
            0,
            "Return only the requested initial-transform JSON. Do not validate, reject, or invent metadata contracts.",
        ),
    )
    _set_value(initial_model["data"]["node"]["template"], "max_tokens", 2048)
    initial_transformer = add(
        "initial_transformer",
        custom_node(
            proto["custom"],
            f"InitialTransformer-{suffix}",
            base_folder / "01_table_catalog_initial_transformer.py",
            1580,
            0,
        ),
    )
    # This instance has two outputs, but only ``payload_out`` is used
    # downstream.  Pin the node-level selected output explicitly: without it
    # Langflow 1.11 can auto-select ``source_text`` (the first output) on
    # import and discard both payload_out -> payload connections as invalid.
    initial_transformer["data"]["selected_output"] = "payload_out"

    # These are the same source files and order used by the stable legacy 03
    # Table Catalog Flow.  Only their upstream payload is new.
    variables = add("variables", custom_node(proto["custom"], f"Variables-{suffix}", base_folder / spec.variables, 1890, 0))
    extraction_prompt = add(
        "extraction_prompt",
        prompt_node(
            proto["prompt"],
            f"PromptExtract-{suffix}",
            (base_folder / spec.prompt).read_text(encoding="utf-8"),
            2190,
            0,
        ),
    )
    extraction_model = add(
        "extraction_model",
        language_model_node(
            proto["language_model"],
            f"LanguageModelExtract-{suffix}",
            2490,
            0,
            "Return only the JSON object requested by the prompt. Do not add markdown or prose.",
        ),
    )
    normalizer = add("normalizer", custom_node(proto["custom"], f"Normalizer-{suffix}", base_folder / spec.normalizer, 2790, 0))
    matcher = add("matcher", custom_node(proto["custom"], f"Matcher-{suffix}", base_folder / spec.matcher, 3090, 0))
    writer = add("writer", custom_node(proto["custom"], f"Writer-{suffix}", base_folder / spec.writer, 3390, 0))
    response = add("response", custom_node(proto["custom"], f"Response-{suffix}", base_folder / spec.response, 3690, 0))
    portal_contract = add(
        "portal_contract",
        custom_node(
            proto["custom"],
            f"PortalContract-{suffix}",
            base_folder / "08a_table_catalog_portal_contract_enricher.py",
            3990,
            0,
        ),
    )
    message = add("message", custom_node(proto["custom"], f"Message-{suffix}", base_folder / spec.message, 4290, -100))
    api = add("api", custom_node(proto["custom"], f"Api-{suffix}", base_folder / spec.api, 4590, 100))
    output = add("chat_output", native_node(proto["chat_output"], f"ChatOutput-{suffix}", 4590, -180))
    _set_message_storage(output, True)

    add_edge(flow, chat, "message", request, "raw_text")
    add_edge(flow, request, "payload_out", initial_source, "payload")
    add_edge(flow, initial_source, "source_text", initial_prompt, "source_text")
    add_edge(flow, initial_prompt, "prompt", initial_model, "input_value")
    add_edge(flow, request, "payload_out", initial_transformer, "payload")
    add_edge(flow, initial_model, "text_output", initial_transformer, "llm_response")
    add_edge(flow, initial_transformer, "payload_out", variables, "payload")
    add_edge(flow, variables, "source_text", extraction_prompt, "source_text")
    add_edge(flow, extraction_prompt, "prompt", extraction_model, "input_value")
    add_edge(flow, initial_transformer, "payload_out", normalizer, "payload")
    add_edge(flow, extraction_model, "text_output", normalizer, "llm_response")
    add_edge(flow, normalizer, "payload_out", matcher, "payload")
    add_edge(flow, matcher, "payload_out", writer, "payload")
    add_edge(flow, writer, "payload_out", response, "payload")
    add_edge(flow, response, "payload_out", portal_contract, "payload")
    add_edge(flow, writer, "payload_out", portal_contract, "authoring_payload")
    add_edge(flow, portal_contract, "payload_out", message, "payload")
    add_edge(flow, portal_contract, "payload_out", api, "payload")
    add_edge(flow, message, "message", api, "display_message")
    add_edge(flow, message, "message", output, "input_value")
    return _stamp_flow_version(_apply_rev2_sticky_notes(flow, spec.slug))


def build_saving_flow_rev_2(donor: dict[str, Any], spec: Any) -> dict[str, Any]:
    if spec.slug == "table_catalog":
        return _build_table_catalog_saving_flow_rev_2(donor, spec)

    proto = prototypes(donor)
    display_name = REV2_DISPLAY_NAMES[spec.slug]
    endpoint = f"metadata-driven-v5-{spec.slug.replace('_', '-')}-saving-rev-2"
    flow = empty_flow(
        donor,
        display_name,
        (
            f"Isolated rev_2 {spec.label} metadata saving flow: active MongoDB contract snapshot, "
            "natural-language refinement, existing-schema extraction, deterministic contract guard, "
            "existing writer, and copy-ready retry guidance."
        ),
        endpoint,
        ["v5", "standalone", "metadata-authoring", "rev-2", "contract-aware"],
    )
    base_folder = COMPONENT_ROOT / spec.folder
    suffix = f"{spec.slug}-rev-2"
    nodes: dict[str, dict[str, Any]] = {}

    def add(name: str, node: dict[str, Any]) -> dict[str, Any]:
        nodes[name] = node
        flow["data"]["nodes"].append(node)
        return node

    chat = add("chat", native_node(proto["chat_input"], f"ChatInput-{suffix}", 0, 0))
    _set_message_storage(chat, True)
    request = add("request", custom_node(proto["custom"], f"Request-{suffix}", base_folder / spec.request, 320, 0))
    _set_value(request["data"]["node"]["template"], "dry_run", True)
    duplicate_action = request["data"]["node"]["template"].get("duplicate_action")
    if isinstance(duplicate_action, dict):
        duplicate_action["options"] = ["skip", "merge", "replace", "create_new"]
        duplicate_action["value"] = "skip"

    snapshot = add(
        "snapshot",
        custom_node(
            proto["custom"],
            f"MetadataSnapshot-{suffix}",
            COMPONENT_ROOT / "metadata_qa_flow" / "01_mongodb_metadata_snapshot_loader.py",
            620,
            360,
        ),
    )
    for field, value in (("domain_limit", "1000"), ("table_limit", "1000"), ("filter_limit", "1000"), ("status_filter", "active"), ("cache_ttl_seconds", "15")):
        _set_value(snapshot["data"]["node"]["template"], field, value)

    context = add(
        "context",
        custom_node(
            proto["custom"],
            f"AuthoringContext-{suffix}",
            REV2_COMPONENT_ROOT / "02_metadata_authoring_context_builder_rev_2.py",
            930,
            0,
        ),
    )
    refinement_prompt = add(
        "refinement_prompt",
        prompt_node(
            proto["prompt"],
            f"PromptRefine-{suffix}",
            (REV2_COMPONENT_ROOT / "03_metadata_authoring_refinement_prompt_ko.md").read_text(encoding="utf-8"),
            1260,
            0,
        ),
    )
    refinement_model = add(
        "refinement_model",
        language_model_node(
            proto["language_model"],
            f"LanguageModelRefine-{suffix}",
            1570,
            0,
            "Return only the requested refinement JSON. Resolve references only from the supplied active metadata candidates.",
        ),
    )
    _set_value(refinement_model["data"]["node"]["template"], "max_tokens", 4096)
    refinement_normalizer = add(
        "refinement_normalizer",
        custom_node(
            proto["custom"],
            f"RefinementNormalizer-{suffix}",
            REV2_COMPONENT_ROOT / "04_metadata_authoring_refinement_normalizer_rev_2.py",
            1880,
            0,
        ),
    )
    extraction_variables = add(
        "extraction_variables",
        custom_node(
            proto["custom"],
            f"ExtractionVariables-{suffix}",
            REV2_COMPONENT_ROOT / "05_metadata_authoring_extraction_variables_rev_2.py",
            2190,
            0,
        ),
    )
    extraction_prompt_text = (base_folder / spec.prompt).read_text(encoding="utf-8").rstrip()
    extraction_prompt_text += "\n\n" + (
        REV2_COMPONENT_ROOT / "05_metadata_authoring_extraction_addendum_ko.md"
    ).read_text(encoding="utf-8").strip()
    extraction_prompt = add(
        "extraction_prompt",
        prompt_node(
            proto["prompt"],
            f"PromptExtract-{suffix}",
            extraction_prompt_text,
            2500,
            0,
        ),
    )
    extraction_model = add(
        "extraction_model",
        language_model_node(
            proto["language_model"],
            f"LanguageModelExtract-{suffix}",
            2810,
            0,
            "Return only the JSON object requested by the prompt. Do not add markdown or prose.",
        ),
    )
    candidate_repair = add(
        "candidate_repair",
        custom_node(
            proto["custom"],
            f"CandidateRepair-{suffix}",
            REV2_COMPONENT_ROOT / "05b_metadata_authoring_candidate_repair_rev_2.py",
            3120,
            0,
        ),
    )
    normalizer = add(
        "normalizer",
        custom_node(proto["custom"], f"Normalizer-{suffix}", base_folder / spec.normalizer, 3430, 0),
    )
    contract_guard = add(
        "contract_guard",
        custom_node(
            proto["custom"],
            f"ContractGuard-{suffix}",
            REV2_COMPONENT_ROOT / "06_metadata_authoring_contract_guard_rev_2.py",
            3740,
            0,
        ),
    )
    matcher = add("matcher", custom_node(proto["custom"], f"Matcher-{suffix}", base_folder / spec.matcher, 4050, 0))
    writer = add("writer", custom_node(proto["custom"], f"Writer-{suffix}", base_folder / spec.writer, 4360, 0))
    response = add("response", custom_node(proto["custom"], f"Response-{suffix}", base_folder / spec.response, 4670, 0))
    response_enricher = add(
        "response_enricher",
        custom_node(
            proto["custom"],
            f"ResponseEnricher-{suffix}",
            REV2_COMPONENT_ROOT / "08_metadata_authoring_response_enricher_rev_2.py",
            4980,
            0,
        ),
    )
    message = add(
        "message",
        custom_node(
            proto["custom"],
            f"Message-{suffix}",
            REV2_COMPONENT_ROOT / "09_metadata_authoring_message_adapter_rev_2.py",
            5290,
            -100,
        ),
    )
    api = add("api", custom_node(proto["custom"], f"Api-{suffix}", base_folder / spec.api, 5600, 100))
    output = add("chat_output", native_node(proto["chat_output"], f"ChatOutput-{suffix}", 5600, -180))
    _set_message_storage(output, True)

    add_edge(flow, chat, "message", request, "raw_text")
    add_edge(flow, request, "payload_out", context, "payload")
    add_edge(flow, snapshot, "domain_items", context, "domain_items")
    add_edge(flow, snapshot, "table_catalog_items", context, "table_catalog_items")
    add_edge(flow, snapshot, "main_flow_filters", context, "main_flow_filters")
    add_edge(flow, context, "source_text", refinement_prompt, "source_text")
    add_edge(flow, context, "metadata_context", refinement_prompt, "metadata_context")
    add_edge(flow, context, "metadata_type", refinement_prompt, "metadata_type")
    add_edge(flow, refinement_prompt, "prompt", refinement_model, "input_value")
    add_edge(flow, context, "payload_out", refinement_normalizer, "payload")
    add_edge(flow, refinement_model, "text_output", refinement_normalizer, "llm_response")
    add_edge(flow, refinement_normalizer, "payload_out", extraction_variables, "payload")
    add_edge(flow, extraction_variables, "source_text", extraction_prompt, "source_text")
    add_edge(flow, extraction_prompt, "prompt", extraction_model, "input_value")
    add_edge(flow, refinement_normalizer, "payload_out", candidate_repair, "payload")
    add_edge(flow, extraction_model, "text_output", candidate_repair, "llm_response")
    add_edge(flow, candidate_repair, "payload_out", normalizer, "payload")
    add_edge(flow, candidate_repair, "llm_response_out", normalizer, "llm_response")
    add_edge(flow, normalizer, "payload_out", contract_guard, "payload")
    add_edge(flow, contract_guard, "payload_out", matcher, "payload")
    add_edge(flow, matcher, "payload_out", writer, "payload")
    add_edge(flow, writer, "payload_out", response, "payload")
    add_edge(flow, response, "payload_out", response_enricher, "payload")
    add_edge(flow, writer, "payload_out", response_enricher, "authoring_payload")
    add_edge(flow, response_enricher, "payload_out", message, "payload")
    add_edge(flow, response_enricher, "payload_out", api, "payload")
    add_edge(flow, message, "message", api, "display_message")
    add_edge(flow, message, "message", output, "input_value")
    return _stamp_flow_version(_apply_rev2_sticky_notes(flow, spec.slug))


def _validate_flow(flow: dict[str, Any]) -> None:
    nodes = flow.get("data", {}).get("nodes", [])
    edges = flow.get("data", {}).get("edges", [])
    runtime_nodes = [node for node in nodes if not _is_note_node(node)]
    note_nodes = [node for node in nodes if _is_note_node(node)]
    note_ids = {str(node.get("id") or "") for node in note_nodes}
    endpoint_name = str(flow.get("endpoint_name") or "")
    is_lightweight_table_catalog = endpoint_name.endswith("table-catalog-saving-rev-2")
    expected_runtime_nodes, expected_edges = (17, 20) if is_lightweight_table_catalog else (20, 28)
    if (
        len(runtime_nodes) != expected_runtime_nodes
        or len(note_nodes) != REV2_NOTE_COUNT
        or len(note_ids) != REV2_NOTE_COUNT
        or len(edges) != expected_edges
    ):
        raise ValueError(
            "rev_2 graph size mismatch: "
            f"{flow.get('name')} runtime_nodes={len(runtime_nodes)} note_nodes={len(note_nodes)} edges={len(edges)}"
        )
    if any(not note_id.startswith(REV2_NOTE_PREFIX) for note_id in note_ids):
        raise ValueError(f"rev_2 Flow has an unexpected note node: {flow.get('name')}")
    if any(
        str(edge.get("source") or "") in note_ids or str(edge.get("target") or "") in note_ids
        for edge in edges
    ):
        raise ValueError(f"rev_2 note node must not have execution edges: {flow.get('name')}")
    if sum(node.get("data", {}).get("type") == "ChatInput" for node in runtime_nodes) != 1:
        raise ValueError(f"rev_2 flow must contain one ChatInput: {flow.get('name')}")
    if sum(node.get("data", {}).get("type") == "ChatOutput" for node in runtime_nodes) != 1:
        raise ValueError(f"rev_2 flow must contain one ChatOutput: {flow.get('name')}")
    runtime_ids = {str(node.get("id") or "") for node in runtime_nodes}
    if is_lightweight_table_catalog:
        if not any(node_id.startswith("InitialTransformer-") for node_id in runtime_ids):
            raise ValueError(f"lightweight Table Catalog Flow is missing its initial transformer: {flow.get('name')}")
        if not any(node_id.startswith("InitialSource-") for node_id in runtime_ids):
            raise ValueError(f"lightweight Table Catalog Flow is missing its initial source adapter: {flow.get('name')}")
        if not any(node_id.startswith("PortalContract-") for node_id in runtime_ids):
            raise ValueError(f"lightweight Table Catalog Flow is missing its Portal response adapter: {flow.get('name')}")
        prohibited_prefixes = (
            "MetadataSnapshot-",
            "AuthoringContext-",
            "RefinementNormalizer-",
            "ExtractionVariables-",
            "CandidateRepair-",
            "ContractGuard-",
            "ResponseEnricher-",
        )
        if any(node_id.startswith(prohibited_prefixes) for node_id in runtime_ids):
            raise ValueError(f"lightweight Table Catalog Flow contains a removed pre-write gate: {flow.get('name')}")
        initial_source = next(node for node in runtime_nodes if str(node.get("id") or "").startswith("InitialSource-"))
        initial_transformer = next(
            node for node in runtime_nodes if str(node.get("id") or "").startswith("InitialTransformer-")
        )
        if initial_source.get("data", {}).get("selected_output") != "source_text":
            raise ValueError(f"initial source output selection is not pinned: {flow.get('name')}")
        if initial_transformer.get("data", {}).get("selected_output") != "payload_out":
            raise ValueError(f"initial transformer output selection is not pinned: {flow.get('name')}")
        transformer_edges = [edge for edge in edges if edge.get("source") == initial_transformer.get("id")]
        if len(transformer_edges) != 2 or any(
            edge.get("data", {}).get("sourceHandle", {}).get("name") != "payload_out" for edge in transformer_edges
        ):
            raise ValueError(f"initial transformer payload fan-out is incomplete: {flow.get('name')}")
    if flow.get("last_tested_version") != TARGET_LANGFLOW_VERSION:
        raise ValueError(f"rev_2 flow version mismatch: {flow.get('name')}")
    for node in nodes:
        component = node.get("data", {}).get("node")
        if isinstance(component, dict) and component.get("lf_version") != TARGET_LANGFLOW_VERSION:
            raise ValueError(f"rev_2 node version mismatch: {flow.get('name')}:{node.get('id')}")


def _readme(manifest_flows: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"| {item['display_order']} | `{item['file']}` | `{item['endpoint_name']}` | {item['nodes']} | {item['note_nodes']} | {item['edges']} |"
        for item in manifest_flows
    )
    return f"""# metadata saving rev_2 import bundle

이 디렉터리는 기존 02/03/04 저장 Flow를 교체하지 않는 독립 검증용 rev_2 Flow 3개를 포함합니다. 기존 9개 complete bundle과 Flow 06 Router는 변경되지 않습니다.

## Import

Langflow Desktop 1.11.0에서 `{REV2_BUNDLE_FILE}` 하나를 import하거나 아래 파일을 개별 import합니다.

| 번호 | 파일 | endpoint_name | 실행 노드 | 설명 Note | 엣지 |
| ---: | --- | --- | ---: | ---: | ---: |
{rows}

## 동작 계약

- 기본값은 기존과 동일하게 테스트 실행입니다. 테스트 실행 중에는 MongoDB에 저장하지 않습니다.
- `02` Domain 및 `04` Main Flow Filter rev_2는 `MONGO_URL` Credential Global Variable과 기존 `datagov` 컬렉션을 읽어 활성 계약을 확인합니다.
- `03` Table Catalog rev_2는 기존 03번 저장 경로에 비차단 초기 문장 변환과 출력 전용 Portal 계약 보강만 추가한 경로입니다. 초기 변환은 SQL·컬럼 mapping 원문을 보존하며, snapshot·후보 복구·공통 Contract Guard로 저장을 선차단하지 않습니다.
- 실제 저장 가능 여부는 각 Flow의 기존 normalizer와 writer가 판단합니다. `03`은 기존 03번의 필수 검증과 중복 처리 정책만 적용합니다.
- 실제 저장은 기존 writer를 그대로 사용하므로 collection, `_id`, item payload, `registration_trace.raw_text` 형태는 기존과 같습니다.
- `03`의 API terminal은 Portal 계약인 `status`, `data`, `metadata_authoring`, `write_result`, `trace`를 유지합니다. Portal 보강기는 Writer의 status·message·저장 결과를 변경하지 않습니다.
- 각 Flow에는 실행과 연결되지 않은 5개 설명 Note가 있습니다. Note는 캔버스 안내용이며 실행 노드·엣지·저장 동작에는 영향을 주지 않습니다.
- Sub Agent 내부에서 HITL resume을 사용하지 않습니다. 보완 응답을 받은 사용자가 입력을 수정해 새 요청으로 다시 실행합니다.
- Router Tool은 계속 기존 02/03/04 Flow를 가리킵니다. rev_2 운영 전환은 별도 검증 후 명시적으로 수행해야 합니다.
"""


def write_rev_2_flows(
    export_root: Path = REV2_EXPORT_ROOT,
    import_root: Path = REV2_IMPORT_ROOT,
    zip_path: Path = REV2_ZIP_PATH,
) -> dict[str, Any]:
    export_root = export_root.resolve()
    import_root = import_root.resolve()
    zip_path = zip_path.resolve()
    export_root.mkdir(parents=True, exist_ok=True)
    import_root.mkdir(parents=True, exist_ok=True)
    donor = load_donor()
    manifest_flows: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []

    expected_files = set(REV2_FILE_NAMES.values())
    for directory in (export_root, import_root):
        for path in directory.glob("*_rev_2_standalone.json"):
            if path.name not in expected_files:
                path.unlink()

    for order, spec in enumerate(SAVING_SPECS, start=1):
        flow = build_saving_flow_rev_2(donor, spec)
        _validate_flow(flow)
        file_name = REV2_FILE_NAMES[spec.slug]
        export_path = export_root / file_name
        export_path.write_bytes((json.dumps(flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

        import_flow = deepcopy(flow)
        import_flow["tags"] = sorted(set([*import_flow.get("tags", []), "import-ready", "isolated-rev-2"]))
        import_path = import_root / file_name
        import_path.write_bytes((json.dumps(import_flow, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        flows.append(import_flow)
        manifest_flows.append(
            {
                "order": order,
                "display_order": file_name.split("_", 1)[0],
                "file": file_name,
                "name": import_flow["name"],
                "endpoint_name": import_flow["endpoint_name"],
                "nodes": sum(not _is_note_node(node) for node in import_flow["data"]["nodes"]),
                "note_nodes": sum(_is_note_node(node) for node in import_flow["data"]["nodes"]),
                "canvas_nodes": len(import_flow["data"]["nodes"]),
                "edges": len(import_flow["data"]["edges"]),
                "sha256": hashlib.sha256(import_path.read_bytes()).hexdigest(),
            }
        )

    combined_path = import_root / REV2_BUNDLE_FILE
    combined_path.write_bytes(json.dumps({"flows": flows}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    manifest = {
        "bundle": "metadata_saving_rev_2",
        "contract_version": REV2_CONTRACT_VERSION,
        "langflow_version": TARGET_LANGFLOW_VERSION,
        "flow_count": len(flows),
        "single_file_ui_import": REV2_BUNDLE_FILE,
        "single_file_ui_import_sha256": hashlib.sha256(combined_path.read_bytes()).hexdigest(),
        "canonical_bundle_unchanged": True,
        "router_targets_rev_2": False,
        "mongodb_document_shape": "unchanged_existing_writer_contract",
        "flows": manifest_flows,
    }
    (import_root / "manifest.json").write_bytes((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (import_root / "README_IMPORT.md").write_bytes(_readme(manifest_flows).encode("utf-8"))

    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=import_root.parent, base_dir=import_root.name)
    return {"export_root": str(export_root), "import_root": str(import_root), "zip": str(zip_path), **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the isolated metadata saving rev_2 flows without changing the canonical nine-flow bundle.")
    parser.add_argument("--export-root", type=Path, default=REV2_EXPORT_ROOT)
    parser.add_argument("--import-root", type=Path, default=REV2_IMPORT_ROOT)
    parser.add_argument("--zip-path", type=Path, default=REV2_ZIP_PATH)
    args = parser.parse_args()
    result = write_rev_2_flows(args.export_root, args.import_root, args.zip_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
