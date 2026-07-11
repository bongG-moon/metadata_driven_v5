"""Dry-run first metadata saving runtime.

This module does not call an LLM and does not write to MongoDB by default.
It provides the deterministic parts around the current single extraction
prompt, LLM JSON normalization, duplicate checks, writer validation, and
dry-run write results.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    ALLOWED_DOMAIN_SECTIONS,
    ALLOWED_DUPLICATE_ACTIONS,
    ALLOWED_METADATA_TYPES,
    ALLOWED_SOURCE_TYPES,
    compact_preview,
    contains_secret,
    deep_merge,
    ensure_dict,
    ensure_list,
    make_error,
    metadata_item_key,
    query_is_truncated,
)


@dataclass
class InMemoryMetadataStore:
    """Tiny store used by tests and dry-run simulations.

    Production MongoDB integration should implement the same get/upsert shape
    and stay outside the default dry-run path.
    """

    items_by_type: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {name: {} for name in ALLOWED_METADATA_TYPES}
    )

    def list_items(self, metadata_type: str) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self.items_by_type.get(metadata_type, {}).values()]

    def get_item(self, metadata_type: str, key: str) -> dict[str, Any] | None:
        item = self.items_by_type.get(metadata_type, {}).get(key)
        return deepcopy(item) if item else None

    def upsert_item(self, metadata_type: str, item: dict[str, Any]) -> None:
        key = metadata_item_key(metadata_type, item)
        self.items_by_type.setdefault(metadata_type, {})[key] = deepcopy(item)


def build_authoring_payload(
    metadata_type: str,
    raw_text: str,
    duplicate_action: str = "skip",
    dry_run: bool = True,
    operator_id: str = "",
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    if metadata_type not in ALLOWED_METADATA_TYPES:
        errors.append(make_error("unknown_metadata_type", f"지원하지 않는 metadata_type입니다: {metadata_type}"))
    requested_action = str(duplicate_action or "skip").strip().lower()
    action = requested_action if requested_action in ALLOWED_DUPLICATE_ACTIONS else "skip"
    if requested_action not in ALLOWED_DUPLICATE_ACTIONS and requested_action != "ask":
        errors.append(make_error("invalid_duplicate_action", f"지원하지 않는 duplicate_action입니다: {duplicate_action}"))
    if not raw_text.strip():
        errors.append(make_error("empty_raw_text", "등록할 자연어 원문이 비어 있습니다."))

    return {
        "metadata_type": metadata_type,
        "request": {
            "raw_text": raw_text,
            "duplicate_action": action,
            "operator_id": operator_id,
            "dry_run": dry_run,
        },
        "items": [],
        "existing_matches": [],
        "conflict_warnings": [],
        "duplicate_decision": {"action": action, "target_key": ""},
        "review": {},
        "write_result": {},
        "trace": {
            "raw_text_preview": compact_preview(raw_text),
            "generated_items_preview": [],
        },
        "errors": errors,
        "warnings": [],
    }


def build_authoring_json_prompt(
    metadata_type: str,
    source_text: str,
    existing_summaries: list[dict[str, Any]] | None = None,
) -> str:
    """Build Korean saving JSON prompt text."""

    existing_preview = json.dumps(existing_summaries or [], ensure_ascii=False, indent=2)
    type_rule = {
        "domain": "`section + key + payload`를 가진 domain item list",
        "table_catalog": "`dataset_key + payload`를 가진 table catalog item list",
        "main_flow_filter": "`filter_key + payload`를 가진 main flow filter item list",
    }.get(metadata_type, "metadata item list")
    return f"""너는 제조 AI agent의 metadata saving JSON 작성자다.

목표:
- 정제된 설명을 MongoDB 저장 후보 item JSON으로 변환한다.
- 원문에 없는 업무 조건, 물리 컬럼, SQL, credential을 새로 만들지 않는다.
- 정제문에만 있고 원문 근거가 없는 값도 새로 만들지 않는다.
- SQL/query_template은 원문 그대로 보존하고 `...`로 줄이지 않는다.
- domain에는 SQL/source_config를 넣지 않는다.
- table catalog에만 source_config/query/filter_mappings를 넣는다.
- main_flow_filter에는 표준 filter 의미만 넣고 dataset별 물리 mapping은 넣지 않는다.

metadata_type: {metadata_type}
작성 대상: {type_rule}

기존 metadata 관련 요약:
```json
{existing_preview}
```

반환 형식:
{{
  "items": [
    {{
      "status": "active",
      "payload": {{}}
    }}
  ],
  "missing_information": [],
  "assumptions": []
}}

등록 원문:
```text
{source_text}
```
"""


def parse_llm_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a raw LLM response."""

    if not text or not text.strip():
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    if not candidate.strip().startswith("{"):
        first = candidate.find("{")
        last = candidate.rfind("}")
        if first >= 0 and last > first:
            candidate = candidate[first : last + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def normalize_authoring_result(payload: dict[str, Any], llm_response: str | dict[str, Any]) -> dict[str, Any]:
    data = llm_response if isinstance(llm_response, dict) else parse_llm_json(llm_response)
    raw_items = data.get("items") or data.get("datasets") or data.get("main_flow_filters") or []
    items = [normalize_item(payload["metadata_type"], item) for item in ensure_list(raw_items)]
    items = [item for item in items if item]

    payload = deepcopy(payload)
    payload["items"] = items
    payload["trace"]["generated_items_preview"] = [
        {
            "key": metadata_item_key(payload["metadata_type"], item),
            "status": item.get("status", "active"),
            "payload_keys": sorted(ensure_dict(item.get("payload")).keys()),
        }
        for item in items
    ]
    payload["warnings"].extend(
        make_error("authoring_missing_information", str(info))
        for info in ensure_list(data.get("missing_information"))
    )
    payload["warnings"].extend(make_error("authoring_assumption", str(item)) for item in ensure_list(data.get("assumptions")))
    return payload


def normalize_item(metadata_type: str, raw_item: Any) -> dict[str, Any]:
    item = deepcopy(raw_item) if isinstance(raw_item, dict) else {}
    if not item:
        return {}
    item.setdefault("status", "active")
    item["payload"] = ensure_dict(item.get("payload"))

    if metadata_type == "domain":
        if "gbn" in item and "section" not in item:
            item["section"] = item["gbn"]
        if "section" in item:
            item["section"] = str(item["section"]).strip()
        if "key" in item:
            item["key"] = str(item["key"]).strip()
        return item

    if metadata_type == "table_catalog":
        if "dataset_key" not in item and "key" in item:
            item["dataset_key"] = item["key"]
        item["dataset_key"] = str(item.get("dataset_key") or "").strip()
        payload = item["payload"]
        source_config = ensure_dict(payload.get("source_config"))
        for key in ("sql", "query", "oracle_sql", "query_template"):
            if key in payload and "query_template" not in source_config:
                source_config["query_template"] = payload.pop(key)
        if source_config:
            payload["source_config"] = source_config
        return item

    if metadata_type == "main_flow_filter":
        if "filter_key" not in item and "key" in item:
            item["filter_key"] = item["key"]
        item["filter_key"] = str(item.get("filter_key") or "").strip()
        return item

    return item


def find_duplicate_matches(
    metadata_type: str,
    items: list[dict[str, Any]],
    existing_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_by_key = {metadata_item_key(metadata_type, item).lower(): item for item in existing_items}
    matches: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for item in items:
        key = metadata_item_key(metadata_type, item)
        if key and key.lower() in existing_by_key:
            existing = existing_by_key[key.lower()]
            match = {
                "new_key": key,
                "existing_key": metadata_item_key(metadata_type, existing),
                "match_type": "same_key",
                "similarity_level": "high",
                "identity_resolution": "unique",
                "reason": "같은 저장 기준 key가 이미 존재합니다.",
                "recommended_action": "merge",
                "existing_item": deepcopy(existing),
            }
            matches.append(match)
            warnings.append(
                {
                    "severity": "warning",
                    "message": "같은 key가 있습니다. 기본 skip이면 기존 항목을 유지합니다.",
                    "new_item_key": key,
                    "existing_item_key": match["existing_key"],
                }
            )
            continue

        new_aliases = set(_aliases(item))
        if not new_aliases:
            continue
        identity_matches = [(existing, new_aliases.intersection(_aliases(existing))) for existing in existing_items]
        identity_matches = [(existing, overlap) for existing, overlap in identity_matches if overlap]
        if len(identity_matches) == 1:
            existing, overlap = identity_matches[0]
            matches.append(
                {
                    "new_key": key,
                    "existing_key": metadata_item_key(metadata_type, existing),
                    "match_type": "alias_overlap",
                    "similarity_level": "high",
                    "identity_resolution": "unique",
                    "reason": f"alias가 겹칩니다: {', '.join(sorted(overlap))}",
                    "recommended_action": "merge",
                    "existing_item": deepcopy(existing),
                }
            )
            warnings.append(
                {
                    "severity": "warning",
                    "message": f"비슷한 alias가 있습니다: {', '.join(sorted(overlap))}",
                    "new_item_key": key,
                    "existing_item_key": metadata_item_key(metadata_type, existing),
                }
            )
        elif len(identity_matches) > 1:
            matches.append(
                {
                    "new_key": key,
                    "existing_key": "",
                    "match_type": "ambiguous_identity",
                    "similarity_level": "ambiguous",
                    "identity_resolution": "ambiguous",
                    "reason": "alias가 겹치는 기존 항목이 여러 건입니다.",
                    "existing_candidate_keys": [metadata_item_key(metadata_type, existing) for existing, _overlap in identity_matches],
                }
            )
            warnings.append({"severity": "blocker", "message": "기존 항목 후보가 여러 건이라 대상을 확정할 수 없습니다.", "new_item_key": key})
    return matches, warnings


def _aliases(item: dict[str, Any]) -> set[str]:
    payload = ensure_dict(item.get("payload"))
    aliases = ensure_list(payload.get("aliases"))
    display_name = payload.get("display_name")
    key = item.get("key") or item.get("dataset_key") or item.get("filter_key")
    values = [*aliases, display_name, key]
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def apply_duplicate_check(
    payload: dict[str, Any],
    existing_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    matches, conflict_warnings = find_duplicate_matches(
        payload["metadata_type"], payload.get("items", []), existing_items or []
    )
    payload = deepcopy(payload)
    payload["existing_matches"] = matches
    payload["conflict_warnings"] = conflict_warnings
    return payload


def build_deterministic_review(payload: dict[str, Any]) -> dict[str, Any]:
    item_reviews = [review_item(payload["metadata_type"], item) for item in payload.get("items", [])]
    errors = [error for review in item_reviews for error in review["errors"]]
    supplement_requests = [
        {
            "item_key": review["key"],
            "field": error.get("field", ""),
            "reason": error["message"],
            "example_user_input": error.get("example_user_input", ""),
        }
        for review in item_reviews
        for error in review["errors"]
        if error["type"] in {"missing_required_field", "missing_source_config", "truncated_query"}
    ]
    ready_to_save = bool(item_reviews) and not errors
    if contains_secret(payload.get("items")):
        ready_to_save = False
        errors.append(make_error("secret_detected", "secret/token/password로 보이는 값이 있어 저장하지 않습니다."))
    return {
        "ready_to_save": ready_to_save,
        "supplement_requests": supplement_requests,
        "item_reviews": item_reviews,
        "errors": errors,
    }


def review_item(metadata_type: str, item: dict[str, Any]) -> dict[str, Any]:
    key = metadata_item_key(metadata_type, item)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    payload = ensure_dict(item.get("payload"))

    if not key:
        errors.append(make_error("missing_required_field", "저장 기준 key가 없습니다.", field="key"))
    if not payload:
        errors.append(make_error("missing_required_field", "payload가 비어 있습니다.", field="payload"))

    if metadata_type == "domain":
        section = item.get("section") or item.get("gbn")
        if not section:
            errors.append(make_error("missing_required_field", "domain section이 없습니다.", field="section"))
        elif section not in ALLOWED_DOMAIN_SECTIONS:
            errors.append(make_error("unsupported_section", f"지원하지 않는 domain section입니다: {section}", field="section"))
        if "source_config" in payload or "query_template" in payload:
            errors.append(make_error("domain_source_config_forbidden", "domain에는 source/query config를 저장하지 않습니다."))

    if metadata_type == "table_catalog":
        source_type = payload.get("source_type") or ensure_dict(payload.get("source_config")).get("source_type")
        source_config = ensure_dict(payload.get("source_config"))
        if not source_type:
            errors.append(make_error("missing_required_field", "payload.source_type이 없습니다.", field="payload.source_type"))
        elif source_type not in ALLOWED_SOURCE_TYPES:
            errors.append(make_error("unsupported_source_type", f"지원하지 않는 source_type입니다: {source_type}", field="payload.source_type"))
        if not source_config:
            errors.append(make_error("missing_source_config", "payload.source_config가 없습니다.", field="payload.source_config"))
        if source_type in {"oracle", "datalake"}:
            query = source_config.get("query_template")
            if not query:
                errors.append(
                    make_error(
                        "missing_source_config",
                        "Oracle/Datalake dataset에는 source_config.query_template이 필요합니다.",
                        field="source_config.query_template",
                        example_user_input="이 데이터는 SELECT ... FROM TABLE WHERE DATE = {DATE} 로 조회해.",
                    )
                )
            elif query_is_truncated(query):
                errors.append(
                    make_error(
                        "truncated_query",
                        "query_template이 생략 또는 축약되어 있어 저장하지 않습니다.",
                        field="source_config.query_template",
                    )
                )
        if source_type == "goodocs" and not source_config.get("doc_id"):
            errors.append(make_error("missing_source_config", "Goodocs dataset에는 source_config.doc_id가 필요합니다.", field="source_config.doc_id"))
        if payload.get("required_params") and not payload.get("required_param_mappings"):
            errors.append(
                make_error(
                    "missing_required_field",
                    "required_params가 있으면 required_param_mappings가 필요합니다.",
                    field="required_param_mappings",
                )
            )

    if metadata_type == "main_flow_filter":
        if not (payload.get("display_name") or payload.get("aliases")):
            errors.append(make_error("missing_required_field", "display_name 또는 aliases가 필요합니다.", field="display_name"))
        if not payload.get("operator"):
            errors.append(make_error("missing_required_field", "operator가 필요합니다.", field="operator"))
        if not payload.get("value_type"):
            errors.append(make_error("missing_required_field", "value_type이 필요합니다.", field="value_type"))
        if not payload.get("value_shape"):
            errors.append(make_error("missing_required_field", "value_shape가 필요합니다.", field="value_shape"))

    return {"key": key, "ready_to_save": not errors, "warnings": warnings, "errors": errors}


def apply_review_and_write(
    payload: dict[str, Any],
    store: InMemoryMetadataStore | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = deepcopy(payload)
    review_result = review or build_deterministic_review(payload)
    payload["review"] = review_result
    dry_run = bool(payload["request"].get("dry_run", True))
    action = payload["duplicate_decision"]["action"]

    if not review_result.get("ready_to_save"):
        payload["write_result"] = {
            "success": False,
            "ready_to_save": False,
            "saved_count": 0,
            "message": _not_saved_message(payload),
            "supplement_requests": review_result.get("supplement_requests", []),
            "errors": review_result.get("errors", []),
        }
        return payload

    if dry_run:
        match_by_new = _reference_match_by_new_key(payload)
        operations = []
        ambiguity_errors = []
        for item in payload.get("items", []):
            key = metadata_item_key(payload["metadata_type"], item)
            match = ensure_dict(match_by_new.get(key.lower()))
            if match.get("identity_resolution") == "ambiguous" and action != "create_new":
                ambiguity_errors.append(make_error("ambiguous_identity_target", f"기존 항목 후보가 여러 건입니다: {key}"))
                continue
            target_key = str(match.get("existing_key") or key) if action in {"skip", "merge", "replace"} else key
            has_match = bool(match.get("existing_item"))
            operation = "skipped" if has_match and action == "skip" else action if has_match else "created"
            operation_record = {"key": target_key, "operation": operation}
            if target_key != key:
                operation_record["requested_key"] = key
            operations.append(operation_record)
        if ambiguity_errors:
            payload["write_result"] = {"success": False, "ready_to_save": False, "dry_run": True, "saved_count": 0, "would_save_count": 0, "operation_by_key": operations, "message": "대상을 확정하지 못해 저장 계획을 중단했습니다.", "errors": ambiguity_errors}
            return payload
        would_save_count = sum(1 for operation in operations if operation["operation"] != "skipped")
        payload["write_result"] = {
            "success": True,
            "ready_to_save": True,
            "dry_run": True,
            "saved_count": 0,
            "would_save_count": would_save_count,
            "skipped_count": len(operations) - would_save_count,
            "operation_by_key": operations,
            "message": "드라이런입니다. MongoDB에는 저장하지 않았습니다.",
            "keys": [metadata_item_key(payload["metadata_type"], item) for item in payload.get("items", [])],
            "errors": [],
        }
        return payload

    if store is None:
        payload["write_result"] = {
            "success": False,
            "ready_to_save": False,
            "saved_count": 0,
            "message": "저장소가 연결되지 않아 저장하지 않았습니다.",
            "errors": [make_error("missing_store", "non-dry-run writer requires a store")],
        }
        return payload

    operation_by_key = []
    match_by_new = _reference_match_by_new_key(payload)
    if any(ensure_dict(match).get("identity_resolution") == "ambiguous" for match in match_by_new.values()) and action != "create_new":
        payload["write_result"] = {"success": False, "ready_to_save": False, "saved_count": 0, "operation_by_key": [], "errors": [make_error("ambiguous_identity_target", "기존 항목 후보가 여러 건이라 대상을 확정할 수 없습니다.")]}
        return payload
    for item in payload.get("items", []):
        key = metadata_item_key(payload["metadata_type"], item)
        match = ensure_dict(match_by_new.get(key.lower()))
        target_key = str(match.get("existing_key") or key) if action in {"skip", "merge", "replace"} else key
        existing = store.get_item(payload["metadata_type"], target_key)
        target_item = deepcopy(item)
        if existing and payload["metadata_type"] == "domain" and ":" in target_key:
            section, canonical_key = target_key.split(":", 1)
            target_item["section"] = section
            target_item["key"] = canonical_key
        if existing and action == "skip":
            operation = "skipped"
        elif existing and action == "merge":
            merged = deep_merge(existing, target_item)
            store.upsert_item(payload["metadata_type"], merged)
            operation = "merged"
        elif existing and action == "replace":
            store.upsert_item(payload["metadata_type"], target_item)
            operation = "replaced"
        elif existing and action == "create_new":
            payload["errors"].append(make_error("create_new_key_conflict", f"새 항목 key가 이미 존재합니다: {key}"))
            continue
        else:
            store.upsert_item(payload["metadata_type"], target_item)
            operation = "created"
        operation_record = {"key": target_key, "operation": operation}
        if target_key != key:
            operation_record["requested_key"] = key
        operation_by_key.append(operation_record)

    saved_count = sum(1 for item in operation_by_key if item["operation"] != "skipped")
    payload["write_result"] = {
        "success": not payload["errors"],
        "ready_to_save": not payload["errors"],
        "saved_count": saved_count,
        "skipped_count": len(operation_by_key) - saved_count,
        "operation_by_key": operation_by_key,
        "duplicate_action": action,
        "errors": payload["errors"],
    }
    return payload


def _reference_match_by_new_key(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(match.get("new_key") or "").lower(): match
        for match in payload.get("existing_matches", [])
        if isinstance(match, dict) and str(match.get("new_key") or "").strip()
    }


def _not_saved_message(payload: dict[str, Any]) -> str:
    return "아직 저장하지 않았습니다. 아래 정보를 더 알려주세요."


def build_authoring_response(payload: dict[str, Any]) -> dict[str, Any]:
    write_result = ensure_dict(payload.get("write_result"))
    response = {
        "metadata_type": payload.get("metadata_type"),
        "success": bool(write_result.get("success")),
        "message": write_result.get("message") or ("저장했습니다." if write_result.get("success") else "저장하지 않았습니다."),
        "write_result": write_result,
        "trace": {
            "raw_text_preview": payload.get("trace", {}).get("raw_text_preview", ""),
            "generated_items_preview": payload.get("trace", {}).get("generated_items_preview", []),
            "existing_matches": payload.get("existing_matches", []),
            "conflict_warnings": payload.get("conflict_warnings", []),
            "review_summary": {
                "ready_to_save": ensure_dict(payload.get("review")).get("ready_to_save", False),
                "item_count": len(payload.get("items", [])),
            },
        },
    }
    if write_result.get("supplement_requests"):
        response["supplement_requests"] = write_result["supplement_requests"]
    return response


def split_raw_text_blocks(raw_text: str, metadata_type: str = "") -> list[str]:
    """Split raw metadata text without rewriting its contents."""

    text = raw_text.replace("\r\n", "\n")
    if metadata_type == "table_catalog":
        return _split_table_catalog_raw_blocks(text)

    marker_pattern = re.compile(r"<!--\s*single_[^:]+:start\s*-->.*?<!--\s*single_[^:]+:end\s*-->", re.DOTALL)
    blocks: list[str] = []
    consumed: list[tuple[int, int]] = []
    for match in marker_pattern.finditer(text):
        block = match.group(0).strip()
        if block:
            blocks.append(block)
            consumed.append(match.span())

    remainder_parts: list[str] = []
    last = 0
    for start, end in consumed:
        remainder_parts.append(text[last:start])
        last = end
    remainder_parts.append(text[last:])
    remainder = "\n".join(remainder_parts)

    remainder_blocks = []
    for part in re.split(r"\n\s*\n", remainder):
        block = part.strip()
        if block and not _is_non_actionable_raw_block(block):
            remainder_blocks.append(block)
    blocks.extend(_merge_related_raw_blocks(remainder_blocks))
    return blocks


def _split_table_catalog_raw_blocks(text: str) -> list[str]:
    """Split table catalog input by dataset text blocks, keeping SQL intact."""

    lines = text.splitlines()
    starts: list[int] = []
    for index, line in enumerate(lines):
        if line.strip() != "text":
            continue
        start = index
        previous = _previous_non_empty_line(lines, index - 1)
        if previous and _looks_like_short_title(previous[1]):
            start = previous[0]
            marker = _previous_non_empty_line(lines, previous[0] - 1)
            if marker and re.match(r"<!--\s*single_[^:]+:start\s*-->", marker[1]):
                start = marker[0]
        starts.append(start)

    if not starts:
        return split_raw_text_blocks(text)

    blocks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end]).strip()
        if block and not _is_non_actionable_raw_block(block):
            blocks.append(block)
    return blocks


def _previous_non_empty_line(lines: list[str], start_index: int) -> tuple[int, str] | None:
    for index in range(start_index, -1, -1):
        text = lines[index].strip()
        if text:
            return index, text
    return None


def _merge_related_raw_blocks(blocks: list[str]) -> list[str]:
    """Keep SQL query_template sections attached to their dataset text."""

    merged: list[str] = []
    for block in blocks:
        if merged and _should_append_to_previous_raw_block(merged[-1], block):
            merged[-1] = f"{merged[-1]}\n\n{block}"
        else:
            merged.append(block)
    return merged


def _should_append_to_previous_raw_block(previous: str, current: str) -> bool:
    current_l = current.lstrip().lower()
    previous_l = previous.lower()
    if current_l.startswith("query_template:") or current_l.startswith("filter_mappings"):
        return True
    if _looks_like_short_title(previous) and current_l.startswith("text\n"):
        return True
    if "query_template:" in previous_l and _looks_like_sql_fragment(current):
        return True
    return False


def _looks_like_short_title(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return len(lines) == 1 and len(lines[0]) <= 80 and ":" not in lines[0]


def _looks_like_sql_fragment(text: str) -> bool:
    stripped = text.lstrip()
    lower = stripped.lower()
    if lower.startswith(("--", "/*", "select", "with ", "from ", "where ", "and ", "or ")):
        return True
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    if first_line.startswith(","):
        return True
    return bool(re.match(r"^[A-Z_][A-Z0-9_ ]*(,|\))", first_line))


def _is_non_actionable_raw_block(block: str) -> bool:
    text = block.strip()
    if not text or text in {"text", "```"}:
        return True
    if text.startswith("## "):
        return True
    if text in {"아래 블록들은 Domain Saving Flow에 하나씩 넣기 위한 기준 입력입니다.", "## 단일 항목 예시"}:
        return True
    return False


def run_authoring_dry_run(
    metadata_type: str,
    raw_text: str,
    authoring_response: str | dict[str, Any] | None = None,
    existing_items: list[dict[str, Any]] | None = None,
    duplicate_action: str = "skip",
) -> dict[str, Any]:
    """Run deterministic authoring stages around an optional mocked LLM result."""

    payload = build_authoring_payload(metadata_type, raw_text, duplicate_action=duplicate_action, dry_run=True)
    if authoring_response is None:
        payload["warnings"].append(
            make_error("llm_required", "저장 후보 item 생성을 위해 한국어 saving prompt와 LLM 응답이 필요합니다.")
        )
        payload["prompts"] = {
            "authoring_json_prompt_ko": build_authoring_json_prompt(metadata_type, raw_text, existing_items),
        }
        payload["write_result"] = {
            "success": False,
            "ready_to_save": False,
            "saved_count": 0,
            "message": "드라이런입니다. LLM saving JSON 응답이 없어 저장 후보를 만들지 않았습니다.",
            "errors": [make_error("llm_required", "authoring_response is required for item normalization")],
        }
        payload["api_response"] = build_authoring_response(payload)
        return payload

    payload = normalize_authoring_result(payload, authoring_response)
    payload = apply_duplicate_check(payload, existing_items)
    payload = apply_review_and_write(payload)
    payload["prompts"] = {
        "authoring_json_prompt_ko": build_authoring_json_prompt(metadata_type, raw_text, existing_items),
    }
    payload["api_response"] = build_authoring_response(payload)
    return payload


def write_json_report(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
