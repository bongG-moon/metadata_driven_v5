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


def build_saving_flow_rev_2(donor: dict[str, Any], spec: Any) -> dict[str, Any]:
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
    return _stamp_flow_version(flow)


def _validate_flow(flow: dict[str, Any]) -> None:
    nodes = flow.get("data", {}).get("nodes", [])
    edges = flow.get("data", {}).get("edges", [])
    if len(nodes) != 20 or len(edges) != 28:
        raise ValueError(f"rev_2 graph size mismatch: {flow.get('name')} nodes={len(nodes)} edges={len(edges)}")
    if sum(node.get("data", {}).get("type") == "ChatInput" for node in nodes) != 1:
        raise ValueError(f"rev_2 flow must contain one ChatInput: {flow.get('name')}")
    if sum(node.get("data", {}).get("type") == "ChatOutput" for node in nodes) != 1:
        raise ValueError(f"rev_2 flow must contain one ChatOutput: {flow.get('name')}")
    if flow.get("last_tested_version") != TARGET_LANGFLOW_VERSION:
        raise ValueError(f"rev_2 flow version mismatch: {flow.get('name')}")
    for node in nodes:
        component = node.get("data", {}).get("node")
        if isinstance(component, dict) and component.get("lf_version") != TARGET_LANGFLOW_VERSION:
            raise ValueError(f"rev_2 node version mismatch: {flow.get('name')}:{node.get('id')}")


def _readme(manifest_flows: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        f"| {item['display_order']} | `{item['file']}` | `{item['endpoint_name']}` | {item['nodes']} | {item['edges']} |"
        for item in manifest_flows
    )
    return f"""# metadata saving rev_2 import bundle

이 디렉터리는 기존 02/03/04 저장 Flow를 교체하지 않는 독립 검증용 rev_2 Flow 3개를 포함합니다. 기존 9개 complete bundle과 Flow 06 Router는 변경되지 않습니다.

## Import

Langflow Desktop 1.11.0에서 `{REV2_BUNDLE_FILE}` 하나를 import하거나 아래 파일을 개별 import합니다.

| 번호 | 파일 | endpoint_name | 노드 | 엣지 |
| ---: | --- | --- | ---: | ---: |
{rows}

## 동작 계약

- 기본값은 기존과 동일하게 테스트 실행입니다. 테스트 실행 중에는 MongoDB에 저장하지 않습니다.
- `MONGO_URL` Credential Global Variable과 기존 `datagov` 컬렉션 3종을 읽어 활성 계약을 확인합니다.
- 사용자 원문, Flow 정제안, 확정된 dataset/표준 컬럼 변환은 응답에 분리되어 표시됩니다.
- 모호하거나 등록되지 않은 참조는 `needs_input`으로 저장 0건 처리하고 복사 가능한 재입력 예시를 반환합니다.
- 실제 저장은 기존 writer를 그대로 사용하므로 collection, `_id`, item payload, `registration_trace.raw_text` 형태는 기존과 같습니다.
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
                "nodes": len(import_flow["data"]["nodes"]),
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
