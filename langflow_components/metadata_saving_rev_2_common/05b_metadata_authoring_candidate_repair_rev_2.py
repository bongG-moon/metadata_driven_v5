# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05b 메타데이터 저장 후보 사전 복구기 rev_2
# 역할: Domain recipe와 Table Catalog 후보를 사용자 원문·확정 계약 기준으로 기존 저장 schema에 맞게 보정합니다.
# 주요 입력: 정제 payload, 저장 후보 LLM 응답
# 주요 출력: 복구 trace가 포함된 payload, 기존 normalizer가 읽을 JSON Message
# 유지보수 포인트: 원문과 활성 metadata로 유일하게 확정된 값만 자동 복구하고 충돌하거나 추측이 필요한 mapping은 fail-closed 처리합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

ARITHMETIC_OPERATORS = {"add", "subtract", "multiply", "divide"}
DERIVED_NULL_POLICIES = {"zero", "propagate"}
ZERO_NULL_POLICY_ALIASES = {
    "0",
    "fill0",
    "fill_zero",
    "zero",
    "zero_fill",
    "zerofill",
}
PROPAGATE_NULL_POLICY_ALIASES = {
    "as_is",
    "keep",
    "keep_null",
    "nan",
    "none",
    "null",
    "preserve",
    "preserve_null",
    "propagate",
    "propagate_null",
}
AGGREGATE_OPERATOR_ALIASES = {
    "avg": "mean",
    "average": "mean",
    "mean": "mean",
    "sum": "sum",
    "nunique": "nunique",
    "unique": "nunique",
    "distinct": "nunique",
    "distinct_count": "nunique",
    "count_distinct": "nunique",
    "count": "count",
    "min": "min",
    "max": "max",
    "median": "median",
    "first": "first",
    "last": "last",
    "collect_unique": "collect_unique",
}


def repair_candidate_response(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json_object(llm_response)
    repairs: list[dict[str, Any]] = []
    draft = payload.get("metadata_authoring_draft") if isinstance(payload.get("metadata_authoring_draft"), dict) else {}
    unresolved = draft.get("unresolved_references") if isinstance(draft.get("unresolved_references"), list) else []
    missing = _string_list(draft.get("missing_information"))
    needs_input = bool(draft.get("needs_more_input")) or bool(unresolved) or bool(missing)
    if parsed and needs_input:
        suppressed_count = len(parsed.get("items")) if isinstance(parsed.get("items"), list) else 0
        parsed["items"] = []
        parsed["missing_information"] = _unique_text(
            [*_string_list(parsed.get("missing_information")), *missing]
        )
        payload.setdefault("trace", {})["authoring_candidate_repair"] = {
            "status": "suppressed_needs_input",
            "repair_count": 0,
            "suppressed_item_count": suppressed_count,
            "repairs": [],
        }
        return {
            "payload": payload,
            "llm_response": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
        }
    metadata_type = str(payload.get("metadata_type") or "").strip()
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    raw_text = str(request.get("raw_text") or "")
    if metadata_type == "domain" and parsed:
        requested_null_policy, explicit_null_policy = _requested_null_policy(raw_text)
        items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or str(item.get("section") or item.get("gbn") or "") != "analysis_recipes":
                continue
            body = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            metrics = body.get("derived_metrics")
            if not isinstance(metrics, list):
                continue
            arithmetic = []
            rules = []
            for metric_index, metric in enumerate(metrics):
                if not isinstance(metric, dict):
                    arithmetic.append(metric)
                    continue
                operator = str(metric.get("operator") or metric.get("aggregation") or metric.get("method") or "").strip().lower()
                if operator in ARITHMETIC_OPERATORS:
                    current_policy = _canonical_null_policy(metric.get("null_policy"))
                    desired_policy = requested_null_policy if explicit_null_policy else "propagate"
                    if desired_policy in DERIVED_NULL_POLICIES and current_policy != desired_policy:
                        previous_policy = str(metric.get("null_policy") or "").strip()
                        metric["null_policy"] = desired_policy
                        repairs.append(
                            {
                                "item_index": index,
                                "metric_index": metric_index,
                                "field": "null_policy",
                                "from": previous_policy,
                                "to": desired_policy,
                                "reason": (
                                    "explicit_user_null_policy"
                                    if explicit_null_policy
                                    else "default_when_not_user_specified"
                                ),
                            }
                        )
                    elif desired_policy in DERIVED_NULL_POLICIES:
                        metric["null_policy"] = desired_policy
                    arithmetic.append(metric)
                    continue
                aggregation = AGGREGATE_OPERATOR_ALIASES.get(operator)
                source_column = _aggregate_source_column(metric)
                output_column = str(metric.get("output_column") or "").strip()
                if not aggregation or not source_column or not output_column:
                    arithmetic.append(metric)
                    continue
                rule = f"집계 결과 {output_column}은(는) {source_column} 컬럼을 {aggregation} 방식으로 집계한다."
                rules.append(rule)
                repairs.append(
                    {
                        "item_index": index,
                        "metric_index": metric_index,
                        "source_column": source_column,
                        "aggregation": aggregation,
                        "output_column": output_column,
                        "lowered_to": "selection_criteria",
                    }
                )
            if rules:
                if arithmetic:
                    body["derived_metrics"] = arithmetic
                else:
                    body.pop("derived_metrics", None)
                body["selection_criteria"] = _append_rules(body.get("selection_criteria"), rules)
                item["payload"] = body
    elif metadata_type == "table_catalog" and parsed:
        _repair_table_catalog_candidates(payload, parsed, raw_text, repairs)

    identity_selection = _select_declared_identity_item(payload, parsed)
    if identity_selection:
        payload.setdefault("trace", {})["declared_identity_candidate_selection"] = deepcopy(identity_selection)
        draft["candidate_selection"] = deepcopy(identity_selection)
        payload["metadata_authoring_draft"] = draft

    if repairs:
        trace = payload.setdefault("trace", {})
        trace["authoring_candidate_repair"] = {
            "status": "repaired",
            "repair_count": len(repairs),
            "repairs": deepcopy(repairs),
        }
        draft["candidate_repairs"] = deepcopy(repairs)
        payload["metadata_authoring_draft"] = draft
    else:
        payload.setdefault("trace", {})["authoring_candidate_repair"] = {"status": "unchanged", "repair_count": 0, "repairs": []}
    return {
        "payload": payload,
        "llm_response": json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) if parsed else str(getattr(llm_response, "text", llm_response) or ""),
    }


def _select_declared_identity_item(payload: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any] | None:
    items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    if len(items) <= 1:
        return None
    context = payload.get("metadata_authoring_context") if isinstance(payload.get("metadata_authoring_context"), dict) else {}
    declared = context.get("declared_identity") if isinstance(context.get("declared_identity"), dict) else {}
    metadata_type = str(payload.get("metadata_type") or "").strip()
    if not declared:
        return None

    selected_index = -1
    reason = ""
    if metadata_type == "domain":
        section = str(declared.get("section") or "").strip()
        key = str(declared.get("key") or "").strip()
        exact = [
            index
            for index, item in enumerate(items)
            if isinstance(item, dict)
            and str(item.get("section") or item.get("gbn") or "").strip().casefold() == section.casefold()
            and str(item.get("key") or "").strip().casefold() == key.casefold()
        ]
        same_section = [
            index
            for index, item in enumerate(items)
            if isinstance(item, dict)
            and section
            and str(item.get("section") or item.get("gbn") or "").strip().casefold() == section.casefold()
        ]
        if len(exact) == 1:
            selected_index = exact[0]
            reason = "exact_declared_section_and_key"
        elif len(same_section) == 1:
            selected_index = same_section[0]
            reason = "unique_declared_section_candidate"
    elif metadata_type == "table_catalog":
        key = str(declared.get("dataset_key") or "").strip()
        exact = [
            index
            for index, item in enumerate(items)
            if isinstance(item, dict)
            and str(item.get("dataset_key") or item.get("key") or "").strip().casefold() == key.casefold()
        ]
        if len(exact) == 1:
            selected_index = exact[0]
            reason = "exact_declared_dataset_key"
    elif metadata_type == "main_flow_filter":
        key = str(declared.get("filter_key") or "").strip()
        exact = [
            index
            for index, item in enumerate(items)
            if isinstance(item, dict)
            and str(item.get("filter_key") or item.get("key") or "").strip().casefold() == key.casefold()
        ]
        if len(exact) == 1:
            selected_index = exact[0]
            reason = "exact_declared_filter_key"

    if selected_index < 0:
        return None
    selected = deepcopy(items[selected_index])
    suppressed = [
        _item_identity(item, metadata_type)
        for index, item in enumerate(items)
        if index != selected_index and isinstance(item, dict)
    ]
    parsed["items"] = [selected]
    return {
        "status": "selected_single_declared_item",
        "reason": reason,
        "selected": _item_identity(selected, metadata_type),
        "suppressed_count": len(suppressed),
        "suppressed": suppressed,
    }


def _item_identity(item: dict[str, Any], metadata_type: str) -> str:
    if metadata_type == "domain":
        return f"{item.get('section') or item.get('gbn') or ''}:{item.get('key') or ''}".strip(":")
    if metadata_type == "table_catalog":
        return str(item.get("dataset_key") or item.get("key") or "").strip()
    return str(item.get("filter_key") or item.get("key") or "").strip()


def _aggregate_source_column(metric: dict[str, Any]) -> str:
    for key in ("source_column", "input_column", "column"):
        value = str(metric.get(key) or "").strip()
        if value:
            return value
    operands = metric.get("operands")
    if isinstance(operands, list):
        columns = [str(item.get("column") or "").strip() for item in operands if isinstance(item, dict) and str(item.get("column") or "").strip()]
        constants = [item for item in operands if isinstance(item, dict) and "constant" in item]
        if len(columns) == 1 and not constants:
            return columns[0]
    return ""


def _repair_table_catalog_candidates(
    payload: dict[str, Any],
    parsed: dict[str, Any],
    raw_text: str,
    repairs: list[dict[str, Any]],
) -> None:
    """Repair only explicit, registry-validated physical-to-canonical mappings."""

    draft = payload.get("metadata_authoring_draft") if isinstance(payload.get("metadata_authoring_draft"), dict) else {}
    refined_text = str(draft.get("refined_text") or "")
    mapping_text = "\n".join(value for value in (raw_text, refined_text) if str(value or "").strip())
    resolutions = [
        value
        for value in draft.get("resolved_references", [])
        if isinstance(value, dict) and str(value.get("kind") or "") == "canonical_column"
    ]
    keep_default_details = _default_detail_columns_requested(raw_text)
    items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        body = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        columns = _catalog_column_names(body.get("columns"))
        column_by_key = {_column_key(column): column for column in columns if _column_key(column)}
        mappings = deepcopy(body.get("filter_mappings")) if isinstance(body.get("filter_mappings"), dict) else {}

        for resolution in resolutions:
            source = str(resolution.get("input") or "").strip()
            canonical = str(resolution.get("target") or "").strip()
            physical = column_by_key.get(_column_key(source), "")
            if not source or not canonical or not physical:
                continue
            if not _explicit_column_mapping(mapping_text, physical, canonical):
                continue
            owner = _physical_mapping_owner(mappings, physical)
            if owner and _column_key(owner) != _column_key(canonical):
                # A conflicting existing mapping must remain visible to the
                # downstream guard/writer instead of being silently replaced.
                continue
            canonical_key = _case_insensitive_mapping_key(mappings, canonical) or canonical
            aliases = _mapping_values(mappings.get(canonical_key))
            if any(_column_key(alias) == _column_key(physical) for alias in aliases):
                continue
            mappings[canonical_key] = _unique_text([*aliases, physical])
            repairs.append(
                {
                    "item_index": item_index,
                    "field": "filter_mappings",
                    "canonical_column": canonical,
                    "source_column": physical,
                    "action": "add_explicit_resolved_mapping",
                }
            )

        if mappings or isinstance(body.get("filter_mappings"), dict):
            body["filter_mappings"] = mappings
        if "default_detail_columns" in body and not keep_default_details:
            removed = deepcopy(body.pop("default_detail_columns"))
            if removed not in (None, "", []):
                repairs.append(
                    {
                        "item_index": item_index,
                        "field": "default_detail_columns",
                        "from": removed,
                        "action": "remove_unrequested_optional_field",
                    }
                )
        item["payload"] = body


def _default_detail_columns_requested(raw_text: str) -> bool:
    text = str(raw_text or "")
    lowered = text.casefold()
    if "default_detail_columns" in lowered:
        return True
    patterns = (
        r"기본\s*(?:상세|표시|출력)\s*컬럼",
        r"기본으로\s*(?:보여|표시|출력)",
        r"출력\s*컬럼을\s*(?:따로\s*)?지정하지\s*않",
        r"상세\s*조회(?:에서는|시에는|할\s*때에는?).{0,40}(?:보여|표시|출력)",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _explicit_column_mapping(text: str, source_column: str, canonical_column: str) -> bool:
    source = str(source_column or "").strip()
    canonical = str(canonical_column or "").strip()
    if not source or not canonical:
        return False
    relation_cues = r"연결|매핑|대응|표준\s*컬럼|사용"
    source_pattern = re.escape(source)
    canonical_pattern = re.escape(canonical)
    patterns = (
        rf"(?i){source_pattern}[^\r\n.!?]{{0,80}}(?:{relation_cues})",
        rf"(?i)(?:{relation_cues})[^\r\n.!?]{{0,80}}{source_pattern}",
        rf"(?i){source_pattern}[^\r\n.!?]{{0,80}}{canonical_pattern}",
        rf"(?i){canonical_pattern}[^\r\n.!?]{{0,80}}{source_pattern}",
    )
    return any(re.search(pattern, str(text or "")) for pattern in patterns)


def _catalog_column_names(value: Any) -> list[str]:
    raw_values = list(value) if isinstance(value, dict) else value if isinstance(value, list) else []
    result: list[str] = []
    for raw in raw_values:
        if isinstance(raw, dict):
            column = str(
                raw.get("name")
                or raw.get("column")
                or raw.get("column_name")
                or raw.get("field")
                or ""
            ).strip()
        else:
            column = str(raw or "").strip()
        if column and _column_key(column) not in {_column_key(item) for item in result}:
            result.append(column)
    return result


def _physical_mapping_owner(mappings: dict[str, Any], physical: str) -> str:
    physical_key = _column_key(physical)
    for canonical, aliases in mappings.items():
        if any(_column_key(alias) == physical_key for alias in _mapping_values(aliases)):
            return str(canonical or "").strip()
    return ""


def _case_insensitive_mapping_key(mappings: dict[str, Any], target: str) -> str:
    target_key = _column_key(target)
    return next((str(key) for key in mappings if _column_key(key) == target_key), "")


def _mapping_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return _unique_text(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _column_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _requested_null_policy(raw_text: str) -> tuple[str, bool]:
    """Return only a null policy explicitly supported by the user's original text.

    The extraction model is not allowed to invent zero-fill behavior.  When the
    user says nothing about missing values, the caller applies ``propagate``.
    """

    text = str(raw_text or "").strip()
    lowered = text.casefold()
    direct = re.search(
        r"null[_\s-]*policy\s*(?:은|는|을|를|=|:)?\s*([a-z0-9_\-]+)",
        lowered,
        flags=re.IGNORECASE,
    )
    if direct:
        return _canonical_null_policy(direct.group(1)), True

    clauses = [
        clause.strip()
        for clause in re.split(r"[\r\n.!?]+", lowered)
        if clause.strip()
        and re.search(r"결측|누락값|누락 값|\bnull\b|\bnan\b", clause, flags=re.IGNORECASE)
    ]
    for clause in clauses:
        if re.search(r"(?:0|zero|제로)\s*(?:으로|로)?", clause) and re.search(
            r"처리|채우|대체|계산|간주", clause
        ):
            return "zero", True
        if re.search(r"그대로|유지|전파|propagate|preserve|keep", clause) or re.search(
            r"(?:null|결측)\s*(?:로|으로)", clause
        ):
            return "propagate", True
        if re.search(r"정책|처리|무시|제외|삭제|채우|대체|계산|간주", clause):
            return "", True
    return "", False


def _canonical_null_policy(value: Any) -> str:
    policy = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if policy in ZERO_NULL_POLICY_ALIASES:
        return "zero"
    if policy in PROPAGATE_NULL_POLICY_ALIASES:
        return "propagate"
    return policy if policy in DERIVED_NULL_POLICIES else ""


def _append_rules(value: Any, rules: list[str]) -> Any:
    if isinstance(value, dict):
        result = deepcopy(value)
        current = _string_list(result.get("rules"))
        result["rules"] = _unique_text([*current, *rules])
        return result
    if isinstance(value, list):
        return _unique_text([*_string_list(value), *rules])
    return _unique_text(rules)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = str(getattr(value, "text", value) or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _unique_text(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values if isinstance(values, (list, tuple, set)) else []:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


class MetadataAuthoringCandidateRepairRev2(Component):
    display_name = "05b 메타데이터 저장 후보 사전 복구기 rev_2"
    description = "Domain 계산과 Table Catalog 컬럼 mapping을 기존 저장 계약으로 안전하게 보정합니다."
    inputs = [
        DataInput(name="payload", display_name="정제 페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="저장 후보 LLM 응답", required=True),
    ]
    outputs = [
        Output(name="payload_out", display_name="복구 페이로드", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="llm_response_out", display_name="복구된 LLM JSON", method="build_llm_response", types=["Message"], group_outputs=True),
    ]

    def _build_once(self) -> dict[str, Any]:
        cached = getattr(self, "_candidate_repair_result", None)
        if isinstance(cached, dict):
            return cached
        result = repair_candidate_response(getattr(self, "payload", None), getattr(self, "llm_response", ""))
        self._candidate_repair_result = result
        return result

    def build_payload(self) -> Data:
        return Data(data=deepcopy(self._build_once()["payload"]))

    def build_llm_response(self) -> Message:
        return Message(text=self._build_once()["llm_response"])
