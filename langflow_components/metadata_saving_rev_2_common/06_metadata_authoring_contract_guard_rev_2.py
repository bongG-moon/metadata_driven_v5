# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 06 메타데이터 저장 계약 검증기 rev_2
# 역할: 기존 normalizer가 만든 items의 dataset/표준 컬럼 참조를 snapshot 계약으로 canonicalize하고 모호한 저장을 차단합니다.
# 주요 입력: 저장 후보 payload
# 주요 출력: 검증된 payload
# 유지보수 포인트: items에는 기존 저장 schema 필드만 남기고 정제안·근거·재입력 예시는 payload의 transient 영역에만 둡니다.
# =============================================================================

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from itertools import product
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

DATASET_SCALAR_KEYS = {"data_source", "dataset_key", "source_dataset", "left_dataset", "right_dataset"}
DATASET_LIST_KEYS = {"source_datasets", "dataset_keys", "disallowed_dataset_keys"}
COLUMN_SCALAR_KEYS = {"field", "column"}
COLUMN_LIST_KEYS = {
    "join_keys",
    "columns",
    "required_columns",
    "group_by",
    "grouping_columns",
    "grain_columns",
    "metric_columns",
    "result_columns",
    "default_detail_columns",
}
JOIN_PAIR_LEFT_KEYS = ("left_key", "left_on", "left_column")
JOIN_PAIR_RIGHT_KEYS = ("right_key", "right_on", "right_column")
JOIN_PAIR_CANONICAL_KEYS = ("canonical_key", "join_key", "key")
MAX_RETRY_VARIANTS = 4
USER_INPUT_ERROR_TYPES = {
    "unknown_dataset_reference",
    "ambiguous_dataset_reference",
    "unknown_canonical_column",
    "ambiguous_canonical_column",
    "join_key_not_in_dataset_contract",
    "invalid_main_filter_key",
    "ambiguous_main_filter_reference",
    "duplicate_canonical_mapping",
    "unsupported_derived_metric_operator",
    "invalid_derived_metric",
    "invalid_derived_metrics",
    "derived_metric_output_column_missing",
    "invalid_derived_metric_operands",
}


def guard_metadata_contract(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    metadata_type = str(payload.get("metadata_type") or "").strip()
    context = _dict(payload.get("metadata_authoring_context"))
    registry = _dict(context.get("registry"))
    draft = deepcopy(_dict(payload.get("metadata_authoring_draft")))
    items = [deepcopy(item) for item in _list(payload.get("items")) if isinstance(item, dict)]
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    resolutions = [deepcopy(item) for item in _list(draft.get("resolved_references")) if isinstance(item, dict)]
    items, identity_errors, identity_corrections = _apply_declared_identity(
        metadata_type,
        items,
        _dict(context.get("declared_identity")),
    )
    errors.extend(identity_errors)

    guarded_items: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if metadata_type == "domain":
            guarded = _guard_domain_item(item, registry, f"items[{index}]", errors, warnings, resolutions)
        elif metadata_type == "table_catalog":
            guarded = _guard_table_item(item, registry, f"items[{index}]", errors, resolutions)
        elif metadata_type == "main_flow_filter":
            guarded = _guard_filter_item(item, registry, f"items[{index}]", errors, resolutions)
        else:
            guarded = item
            errors.append({"type": "unsupported_metadata_type", "message": f"지원하지 않는 metadata_type입니다: {metadata_type}"})
        guarded_items.append(guarded)

    payload["items"] = guarded_items
    payload.setdefault("errors", []).extend(errors)
    payload.setdefault("warnings", []).extend(warnings)
    all_errors = _unique_errors(_list(payload.get("errors")))
    payload["errors"] = all_errors
    resolutions = _dedupe_references(resolutions)

    missing = _unique_text(
        [
            *_string_list(draft.get("missing_information")),
            *[
                str(error.get("message") or "")
                for error in all_errors
                if str(error.get("type") or "") in USER_INPUT_ERROR_TYPES
            ],
        ]
    )
    needs_more_input = bool(draft.get("needs_more_input")) or bool(missing)
    retry_examples = _retry_examples(
        metadata_type,
        payload,
        guarded_items,
        resolutions,
        _list(draft.get("unresolved_references")),
        missing,
        all_errors,
    )
    retry_example = retry_examples[0] if retry_examples else ""
    draft.update(
        {
            "resolved_references": resolutions,
            "missing_information": missing,
            "needs_more_input": needs_more_input,
            "retry_example": retry_example,
            "retry_examples": retry_examples,
            "contract_validation": {
                "status": "needs_input" if needs_more_input else "error" if all_errors else "validated",
                "error_count": len(all_errors),
                "warning_count": len(_list(payload.get("warnings"))),
            },
        }
    )
    payload["metadata_authoring_draft"] = draft
    refinement = _dict(payload.get("refinement"))
    refinement.update(
        {
            "refined_text": str(draft.get("refined_text") or refinement.get("refined_text") or ""),
            "needs_more_input": needs_more_input,
            "missing_information": missing,
            "assumptions": _unique_text(
                [*_string_list(draft.get("assumptions")), *_string_list(refinement.get("assumptions"))]
            ),
        }
    )
    payload["refinement"] = refinement
    trace = payload.setdefault("trace", {})
    if identity_corrections:
        trace["declared_identity_lock"] = {
            "status": "corrected",
            "correction_count": len(identity_corrections),
            "corrections": deepcopy(identity_corrections),
        }
    prior_resolution = _dict(trace.get("contract_resolution"))
    prior_resolution.update(
        {
            "status": draft["contract_validation"]["status"],
            "resolved_count": len(resolutions),
            "resolved_references": deepcopy(resolutions),
            "contract_error_count": len(all_errors),
            "contract_warning_count": len(_list(payload.get("warnings"))),
        }
    )
    trace["contract_resolution"] = prior_resolution
    if needs_more_input:
        payload["items"] = []
        trace["generated_items_preview"] = []
    return payload


def _apply_declared_identity(
    metadata_type: str,
    items: list[dict[str, Any]],
    declared: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not declared or not items:
        return items, [], []
    identity_fields = {
        "domain": ("section", "key", "status"),
        "table_catalog": ("dataset_key", "status"),
        "main_flow_filter": ("filter_key", "status"),
    }.get(metadata_type, ())
    locked = {field: str(declared.get(field) or "").strip() for field in identity_fields}
    locked = {field: value for field, value in locked.items() if value}
    if not locked:
        return items, [], []
    corrections: list[dict[str, Any]] = []
    selected_items = items
    if len(items) > 1:
        exact_indexes = _declared_identity_matches(metadata_type, items, locked, exact=True)
        fallback_indexes = _declared_identity_matches(metadata_type, items, locked, exact=False)
        candidates = exact_indexes if len(exact_indexes) == 1 else fallback_indexes
        if len(candidates) != 1:
            return (
                items,
                [
                    {
                        "type": "declared_identity_item_count_mismatch",
                        "message": "원문에 하나의 등록 identity가 명시됐지만 그 identity에 해당하는 후보를 "
                        f"{len(items)}건 중 하나로 확정하지 못했습니다.",
                        "declared_identity": deepcopy(locked),
                        "item_count": len(items),
                        "candidate_identities": [_guard_item_identity(item, metadata_type) for item in items],
                    }
                ],
                [],
            )
        selected_index = candidates[0]
        suppressed = [
            _guard_item_identity(item, metadata_type)
            for index, item in enumerate(items)
            if index != selected_index
        ]
        selected_items = [items[selected_index]]
        corrections.append(
            {
                "field": "items",
                "from": len(items),
                "to": 1,
                "reason": "selected_declared_identity",
                "suppressed": suppressed,
            }
        )
    result = [deepcopy(selected_items[0])]
    for field, expected in locked.items():
        current = str(result[0].get(field) or "").strip()
        if current != expected:
            result[0][field] = expected
            corrections.append({"field": field, "from": current, "to": expected})
    return result, [], corrections


def _declared_identity_matches(
    metadata_type: str,
    items: list[dict[str, Any]],
    locked: dict[str, str],
    exact: bool,
) -> list[int]:
    matches = []
    for index, item in enumerate(items):
        if metadata_type == "domain":
            section_matches = not locked.get("section") or str(item.get("section") or "").casefold() == locked["section"].casefold()
            key_matches = not locked.get("key") or str(item.get("key") or "").casefold() == locked["key"].casefold()
            if section_matches and (key_matches if exact else True):
                matches.append(index)
        elif metadata_type == "table_catalog":
            key = str(item.get("dataset_key") or item.get("key") or "")
            if locked.get("dataset_key") and key.casefold() == locked["dataset_key"].casefold():
                matches.append(index)
        elif metadata_type == "main_flow_filter":
            key = str(item.get("filter_key") or item.get("key") or "")
            if locked.get("filter_key") and key.casefold() == locked["filter_key"].casefold():
                matches.append(index)
    return matches


def _guard_item_identity(item: dict[str, Any], metadata_type: str) -> str:
    if metadata_type == "domain":
        return f"{item.get('section') or ''}:{item.get('key') or ''}".strip(":")
    if metadata_type == "table_catalog":
        return str(item.get("dataset_key") or item.get("key") or "").strip()
    return str(item.get("filter_key") or item.get("key") or "").strip()


def _guard_domain_item(
    item: dict[str, Any],
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(item)
    body = deepcopy(_dict(result.get("payload")))
    body = _canonicalize_domain_value(body, registry, f"{path}.payload", errors, resolutions)
    source_datasets = _string_list(body.get("source_datasets"))
    join_keys = _string_list(body.get("join_keys"))
    for dataset_key in source_datasets:
        dataset = _dict(_dict(registry.get("datasets")).get(dataset_key))
        available = _string_list(dataset.get("canonical_columns"))
        if not available:
            warnings.append(
                {
                    "type": "dataset_contract_columns_unavailable",
                    "message": f"'{dataset_key}' Table Catalog에 canonical column 목록이 없어 join key 존재 여부를 추가 검증하지 못했습니다.",
                    "dataset_key": dataset_key,
                }
            )
            continue
        available_folded = {column.casefold() for column in available}
        for join_key in join_keys:
            if join_key.casefold() not in available_folded:
                errors.append(
                    {
                        "type": "join_key_not_in_dataset_contract",
                        "message": f"표준 join key '{join_key}'가 '{dataset_key}'의 활성 Table Catalog 계약에 없습니다.",
                        "path": f"{path}.payload.join_keys",
                        "dataset_key": dataset_key,
                        "value": join_key,
                        "available": available,
                    }
                )
    result["payload"] = body
    return result


def _canonicalize_domain_value(
    value: Any,
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    dataset_hint: str = "",
    inside_derived_metric: bool = False,
) -> Any:
    if isinstance(value, list):
        return [
            _canonicalize_domain_value(item, registry, f"{path}[{index}]", errors, resolutions, dataset_hint, inside_derived_metric)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)

    result = deepcopy(value)
    local_hint = dataset_hint
    for key in DATASET_SCALAR_KEYS:
        if key in result:
            result[key] = _resolve_dataset(result[key], registry, f"{path}.{key}", errors, resolutions)
            if key in {"data_source", "dataset_key"} and isinstance(result[key], str):
                local_hint = result[key]
    for key in DATASET_LIST_KEYS:
        if key in result:
            result[key] = [
                _resolve_dataset(item, registry, f"{path}.{key}[{index}]", errors, resolutions)
                for index, item in enumerate(_string_list(result[key]))
            ]

    if isinstance(result.get("join_keys"), list) and not inside_derived_metric:
        source_datasets = _string_list(result.get("source_datasets") or result.get("dataset_keys"))
        left_hint = str(result.get("left_dataset") or (source_datasets[0] if source_datasets else local_hint) or "")
        right_hint = str(result.get("right_dataset") or (source_datasets[1] if len(source_datasets) > 1 else local_hint) or "")
        result["join_keys"] = _normalize_join_keys(
            result["join_keys"],
            registry,
            f"{path}.join_keys",
            errors,
            resolutions,
            left_hint,
            right_hint,
        )

    for key, item in list(result.items()):
        child_path = f"{path}.{key}"
        if key in DATASET_SCALAR_KEYS or key in DATASET_LIST_KEYS or key == "join_keys":
            continue
        child_inside_derived = inside_derived_metric or key == "derived_metrics"
        if key in COLUMN_SCALAR_KEYS and not child_inside_derived:
            result[key] = _resolve_column(item, registry, child_path, errors, resolutions, local_hint, allow_unknown=False)
        elif key in COLUMN_LIST_KEYS and isinstance(item, list) and not child_inside_derived:
            strict = key == "join_keys" or (key == "columns" and bool(local_hint))
            result[key] = [
                _resolve_column(column, registry, f"{child_path}[{index}]", errors, resolutions, local_hint, allow_unknown=not strict)
                for index, column in enumerate(item)
            ]
        elif isinstance(item, (dict, list)):
            result[key] = _canonicalize_domain_value(
                item,
                registry,
                child_path,
                errors,
                resolutions,
                local_hint,
                child_inside_derived,
            )
    return result


def _normalize_join_keys(
    values: list[Any],
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    left_dataset: str = "",
    right_dataset: str = "",
) -> list[str]:
    """Normalize common LLM join-pair shapes into the existing string-list contract."""

    result: list[str] = []
    seen = set()
    for index, value in enumerate(values):
        entry_path = f"{path}[{index}]"
        canonical = ""
        if isinstance(value, str):
            canonical = _resolve_column(
                value,
                registry,
                entry_path,
                errors,
                resolutions,
                allow_unknown=False,
            )
        elif isinstance(value, dict):
            explicit = _first_join_key(value, JOIN_PAIR_CANONICAL_KEYS)
            if explicit:
                canonical = _resolve_column(
                    explicit,
                    registry,
                    f"{entry_path}.canonical_key",
                    errors,
                    resolutions,
                    allow_unknown=False,
                )
            else:
                left = _first_join_key(value, JOIN_PAIR_LEFT_KEYS)
                right = _first_join_key(value, JOIN_PAIR_RIGHT_KEYS)
                if not left or not right:
                    errors.append(
                        {
                            "type": "invalid_join_key_contract",
                            "message": "생성된 join_keys 객체에는 left_key와 right_key가 모두 필요합니다.",
                            "path": entry_path,
                        }
                    )
                    continue
                prior_error_count = len(errors)
                left_canonical = _resolve_column(
                    left,
                    registry,
                    f"{entry_path}.left_key",
                    errors,
                    resolutions,
                    left_dataset,
                    allow_unknown=False,
                )
                right_canonical = _resolve_column(
                    right,
                    registry,
                    f"{entry_path}.right_key",
                    errors,
                    resolutions,
                    right_dataset,
                    allow_unknown=False,
                )
                if len(errors) != prior_error_count:
                    continue
                if left_canonical.casefold() != right_canonical.casefold():
                    errors.append(
                        {
                            "type": "join_key_pair_canonical_mismatch",
                            "message": (
                                "생성된 좌우 join key가 같은 표준 컬럼으로 해석되지 않습니다: "
                                f"left={left_canonical}, right={right_canonical}"
                            ),
                            "path": entry_path,
                            "left_key": left,
                            "right_key": right,
                            "left_canonical": left_canonical,
                            "right_canonical": right_canonical,
                        }
                    )
                    continue
                canonical = left_canonical
        else:
            errors.append(
                {
                    "type": "invalid_join_key_contract",
                    "message": "생성된 join_keys 항목은 문자열 또는 좌우 key 객체여야 합니다.",
                    "path": entry_path,
                }
            )
            continue

        marker = canonical.casefold()
        if canonical and marker not in seen:
            seen.add(marker)
            result.append(canonical)
    return result


def _first_join_key(value: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _guard_table_item(
    item: dict[str, Any],
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(item)
    body = deepcopy(_dict(result.get("payload")))
    for field in ("filter_mappings", "standard_column_aliases", "required_param_mappings", "metric_semantics"):
        if not isinstance(body.get(field), dict):
            continue
        normalized: dict[str, Any] = {}
        for source_key, source_value in body[field].items():
            target_key = _resolve_column(source_key, registry, f"{path}.payload.{field}.{source_key}", errors, resolutions, allow_unknown=True)
            if target_key in normalized and str(target_key) != str(source_key):
                errors.append(
                    {
                        "type": "duplicate_canonical_mapping",
                        "message": f"'{source_key}'를 '{target_key}'로 바꾸면 {field} 안에서 key가 중복됩니다.",
                        "path": f"{path}.payload.{field}",
                    }
                )
            else:
                normalized[str(target_key)] = deepcopy(source_value)
        body[field] = normalized
    for field in ("required_params", "default_detail_columns"):
        if isinstance(body.get(field), list):
            body[field] = [
                _resolve_column(value, registry, f"{path}.payload.{field}[{index}]", errors, resolutions, allow_unknown=True)
                for index, value in enumerate(body[field])
            ]
    result["payload"] = body
    return result


def _guard_filter_item(
    item: dict[str, Any],
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(item)
    source_key = str(result.get("filter_key") or result.get("key") or "").strip()
    target, candidates = _resolve_alias(source_key, _dict(registry.get("filters")), "filter_key")
    if target:
        if target != source_key:
            resolutions.append(_resolution("main_filter", source_key, target, f"{path}.filter_key"))
        result["filter_key"] = target
    elif len(candidates) > 1:
        errors.append(
            {
                "type": "ambiguous_main_filter_reference",
                "message": f"메인 필터 '{source_key}'가 여러 활성 filter key와 일치합니다: {', '.join(candidates)}",
                "path": f"{path}.filter_key",
                "value": source_key,
                "candidates": candidates,
            }
        )
    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", source_key):
        canonical = source_key.upper()
        result["filter_key"] = canonical
        if canonical != source_key:
            resolutions.append(_resolution("main_filter", source_key, canonical, f"{path}.filter_key", "new_standard_key_normalization"))
    else:
        errors.append(
            {
                "type": "invalid_main_filter_key",
                "message": f"새 main filter key '{source_key}'는 영문 대문자와 숫자, underscore 형태로 명시해 주세요.",
                "path": f"{path}.filter_key",
                "value": source_key,
            }
        )
    result.pop("key", None)
    return result


def _resolve_dataset(
    value: Any,
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
) -> str:
    source = str(value or "").strip()
    target, candidates = _resolve_alias(source, _dict(registry.get("datasets")), "dataset_key")
    if target:
        if target != source:
            resolutions.append(_resolution("dataset", source, target, path))
        return target
    error_type = "ambiguous_dataset_reference" if len(candidates) > 1 else "unknown_dataset_reference"
    message = (
        f"데이터 표현 '{source}'이 여러 활성 dataset과 일치합니다: {', '.join(candidates)}"
        if candidates
        else f"데이터 표현 '{source}'과 일치하는 활성 dataset_key가 없습니다. 실제 dataset_key를 명시해 주세요."
    )
    errors.append({"type": error_type, "message": message, "path": path, "value": source, "candidates": candidates})
    return source


def _resolve_column(
    value: Any,
    registry: dict[str, Any],
    path: str,
    errors: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    dataset_key: str = "",
    allow_unknown: bool = True,
) -> str:
    source = str(value or "").strip()
    entries = _column_entries(registry, dataset_key)
    target, candidates = _resolve_alias(source, entries, "key")
    if target:
        if target != source:
            resolutions.append(_resolution("canonical_column", source, target, path))
        return target
    if len(candidates) > 1:
        errors.append(
            {
                "type": "ambiguous_canonical_column",
                "message": f"컬럼 표현 '{source}'이 여러 표준 컬럼과 일치합니다: {', '.join(candidates)}",
                "path": path,
                "value": source,
                "candidates": candidates,
            }
        )
    elif not allow_unknown or _contains_korean(source):
        errors.append(
            {
                "type": "unknown_canonical_column",
                "message": f"컬럼 표현 '{source}'을 현재 활성 계약의 표준 컬럼으로 확정하지 못했습니다.",
                "path": path,
                "value": source,
                "dataset_key": dataset_key,
            }
        )
    return source


def _column_entries(registry: dict[str, Any], dataset_key: str = "") -> dict[str, dict[str, Any]]:
    if dataset_key:
        dataset = _dict(_dict(registry.get("datasets")).get(dataset_key))
        aliases = _dict(dataset.get("column_aliases"))
        if aliases:
            return {key: {"key": key, "aliases": _unique_text([key, *_string_list(value)])} for key, value in aliases.items()}
    return _dict(registry.get("canonical_columns"))


def _resolve_alias(source: str, entries: dict[str, Any], target_field: str) -> tuple[str, list[str]]:
    folded = source.casefold()
    direct = [str(_dict(entry).get(target_field) or key) for key, entry in entries.items() if str(key).casefold() == folded]
    if len(set(direct)) == 1:
        return direct[0], direct
    compact = _compact(source)
    candidates = []
    for key, raw_entry in entries.items():
        entry = _dict(raw_entry)
        target = str(entry.get(target_field) or key).strip()
        aliases = _unique_text([target, *_string_list(entry.get("aliases"))])
        if compact and any(_compact(alias) == compact for alias in aliases):
            candidates.append(target)
    candidates = _unique_text(candidates)
    return (candidates[0], candidates) if len(candidates) == 1 else ("", candidates)


def _resolution(kind: str, source: str, target: str, path: str, evidence: str = "registered_unique_contract") -> dict[str, Any]:
    return {"kind": kind, "input": source, "target": target, "evidence": evidence, "path": path}


def _retry_examples(
    metadata_type: str,
    payload: dict[str, Any],
    items: list[dict[str, Any]],
    resolutions: list[Any],
    unresolved: list[Any],
    missing: list[str],
    errors: list[dict[str, Any]],
) -> list[str]:
    if not missing and not any(str(error.get("type") or "") in USER_INPUT_ERROR_TYPES for error in errors):
        return []

    base = _retry_base_text(metadata_type, payload, items, resolutions)
    specs = _retry_reference_specs(unresolved, errors)
    option_groups: list[list[str]] = []
    unresolved_without_candidates: list[dict[str, Any]] = []
    for spec in specs:
        candidates = _ordered_retry_candidates(spec)
        if candidates:
            option_groups.append([_retry_reference_line(spec, target) for target in candidates[:MAX_RETRY_VARIANTS]])
        else:
            unresolved_without_candidates.append(spec)

    combinations = list(product(*option_groups))[:MAX_RETRY_VARIANTS] if option_groups else [tuple()]
    guidance = _retry_error_guidance(errors, unresolved_without_candidates)
    covered_missing = {_compact(spec.get("input")) for spec in specs if str(spec.get("input") or "").strip()}
    residual_missing = [
        item
        for item in missing
        if not any(marker and marker in _compact(item) for marker in covered_missing)
        and str(item).strip() not in {str(error.get("message") or "").strip() for error in errors}
    ]
    if residual_missing:
        guidance.extend(f"등록할 때 다음 정보도 구체적인 값으로 포함해줘: {item}" for item in residual_missing[:3])

    examples = []
    for choice in combinations:
        additions = _retry_additions_not_in_base(base, _unique_text([*choice, *guidance]))
        text = base.rstrip()
        if additions:
            text += "\n\n" + "\n".join(additions)
        examples.append(text[:4000])
    return _unique_text(examples)


def _retry_additions_not_in_base(base: str, additions: list[str]) -> list[str]:
    base_markers = {_compact(line) for line in str(base or "").splitlines() if _compact(line)}
    return [line for line in additions if _compact(line) not in base_markers]


def _retry_base_text(
    metadata_type: str,
    payload: dict[str, Any],
    items: list[dict[str, Any]],
    resolutions: list[Any],
) -> str:
    draft = _dict(payload.get("metadata_authoring_draft"))
    refined_text = str(draft.get("refined_text") or _dict(payload.get("refinement")).get("refined_text") or "").strip()
    raw_text = str(_dict(payload.get("request")).get("raw_text") or "").strip()
    base = refined_text or raw_text
    if base:
        base = _dedupe_retry_text(base)
        additions = _retry_contract_clarity_lines(base, resolutions)
        if additions:
            base += "\n\n" + "\n".join(additions)
        return base
    first = _dict(items[0]) if items else {}
    body = _dict(first.get("payload"))
    if metadata_type == "domain":
        section = str(first.get("section") or "[section]")
        key = str(first.get("key") or "[key]")
        return f"도메인 메타데이터를 등록해줘.\nsection은 {section}이고 key는 {key}이며 status는 active야."
    if metadata_type == "table_catalog":
        dataset_key = str(first.get("dataset_key") or first.get("key") or "[dataset_key]")
        display_name = str(body.get("display_name") or "").strip()
        suffix = f" 표시명은 {display_name}이야." if display_name else ""
        return f"테이블 카탈로그 메타데이터를 등록해줘.\ndataset_key는 {dataset_key}이고 status는 active야.{suffix}"
    filter_key = str(first.get("filter_key") or first.get("key") or "[filter_key]")
    return f"메인 플로우 필터 메타데이터를 등록해줘.\nfilter_key는 {filter_key}이고 status는 active야."


def _retry_contract_clarity_lines(base: str, resolutions: list[Any]) -> list[str]:
    lines: list[str] = []
    references = _dedupe_references([deepcopy(item) for item in resolutions if isinstance(item, dict)])
    for reference in references:
        kind = str(reference.get("kind") or "")
        source = str(reference.get("input") or "").strip()
        target = str(reference.get("target") or "").strip()
        if not source or not target or _retry_text_contains_target(base, target):
            continue
        if kind == "dataset":
            lines.append(f"데이터셋 매핑에서 '{source}'의 dataset_key는 {target}이야.")
        elif kind == "canonical_column":
            lines.append(f"컬럼 매핑에서 '{source}'의 표준 컬럼은 {target}이야.")
        elif kind == "main_filter":
            lines.append(f"필터 매핑에서 '{source}'의 filter_key는 {target}이야.")
        elif kind == "domain":
            lines.append(f"도메인 참조에서 '{source}'의 실제 항목은 {target}이야.")
    return _unique_text(lines)


def _retry_text_contains_target(value: str, target: str) -> bool:
    text = str(value or "")
    key = str(target or "").strip()
    if not key:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text, flags=re.IGNORECASE))
    return _compact(key) in _compact(text)


def _dedupe_retry_text(value: str) -> str:
    result: list[str] = []
    seen = set()
    previous_blank = False
    for raw_line in str(value or "").splitlines():
        line = raw_line.rstrip()
        marker = _compact(line)
        if not marker:
            if result and not previous_blank:
                result.append("")
            previous_blank = True
            continue
        previous_blank = False
        if marker in seen:
            continue
        seen.add(marker)
        result.append(line)
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


def _retry_reference_specs(unresolved: list[Any], errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for value in unresolved:
        if not isinstance(value, dict):
            continue
        specs.append(
            {
                "kind": str(value.get("kind") or "unknown"),
                "input": str(value.get("input") or "").strip(),
                "candidates": _string_list(value.get("candidates")),
                "suggested_target": str(value.get("suggested_target") or value.get("target") or "").strip(),
            }
        )
    error_kinds = {
        "ambiguous_dataset_reference": "dataset",
        "unknown_dataset_reference": "dataset",
        "ambiguous_canonical_column": "canonical_column",
        "unknown_canonical_column": "canonical_column",
        "ambiguous_main_filter_reference": "main_filter",
        "invalid_main_filter_key": "main_filter",
        "ambiguous_metadata_reference": "unknown",
        "metadata_reference_mismatch": "unknown",
        "reference_not_in_context": "unknown",
    }
    for error in errors:
        error_type = str(error.get("type") or "")
        kind = str(error.get("kind") or error_kinds.get(error_type) or "")
        source = str(error.get("input") or error.get("value") or "").strip()
        if not kind or not source:
            continue
        specs.append(
            {
                "kind": kind,
                "input": source,
                "candidates": _unique_text(
                    [*_string_list(error.get("candidates")), error.get("registered_target")]
                ),
                "suggested_target": str(error.get("target") or error.get("registered_target") or "").strip(),
            }
        )
    result = []
    seen = set()
    for spec in specs:
        marker = (str(spec.get("kind") or ""), _compact(spec.get("input")))
        if not marker[1] or marker in seen:
            continue
        seen.add(marker)
        result.append(spec)
    return result


def _ordered_retry_candidates(spec: dict[str, Any]) -> list[str]:
    candidates = _unique_text(_string_list(spec.get("candidates")))
    suggested = str(spec.get("suggested_target") or "").strip()
    if suggested and suggested in candidates:
        return [suggested, *[item for item in candidates if item != suggested]]
    return candidates


def _retry_reference_line(spec: dict[str, Any], target: str) -> str:
    source = str(spec.get("input") or "해당 표현").strip()
    kind = str(spec.get("kind") or "")
    if kind == "dataset":
        return f"'{source}' 데이터의 실제 dataset_key는 {target}이야."
    if kind == "canonical_column":
        return f"'{source}'의 표준 컬럼은 {target}이야."
    if kind == "main_filter":
        return f"'{source}'의 실제 filter_key는 {target}이야."
    if kind == "domain":
        return f"'{source}'이 참조하는 기존 도메인 항목은 {target}이야."
    return f"'{source}'의 실제 등록 계약 key는 {target}이야."


def _retry_error_guidance(
    errors: list[dict[str, Any]], unresolved_without_candidates: list[dict[str, Any]]
) -> list[str]:
    lines: list[str] = []
    error_types = {str(error.get("type") or "") for error in errors}
    if any(error_type.startswith(("unsupported_derived", "invalid_derived", "derived_metric")) for error_type in error_types):
        lines.extend(
            [
                "mean, sum, nunique는 산술식이 아니라 각 입력 지표의 집계 기준으로 저장해.",
                "EQP_COUNT는 EQP_ID를 nunique한 결과이고 AVG_UPH는 UPH를 mean한 결과야.",
                "파생 계산은 AVAILABLE_CAPA = EQP_COUNT × AVG_UPH × 24로 저장해.",
            ]
        )
    for spec in unresolved_without_candidates:
        source = str(spec.get("input") or "해당 표현").strip()
        kind = str(spec.get("kind") or "")
        if kind == "dataset":
            lines.append(f"'{source}' 데이터의 실제 dataset_key를 명시해줘.")
        elif kind == "canonical_column":
            lines.append(f"'{source}'의 실제 표준 컬럼을 명시해줘.")
        elif kind == "main_filter":
            lines.append(f"'{source}'의 실제 filter_key를 명시해줘.")
        else:
            lines.append(f"'{source}'의 실제 등록 계약 key를 명시해줘.")
    return _unique_text(lines)


def _contains_korean(value: str) -> bool:
    return bool(re.search(r"[가-힣]", str(value or "")))


def _compact(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", normalized)


def _dedupe_references(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in values:
        marker = (str(item.get("kind") or ""), _compact(item.get("input")), str(item.get("target") or "").casefold())
        if marker not in seen:
            seen.add(marker)
            result.append(deepcopy(item))
    return result


def _unique_errors(values: list[Any]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for value in values:
        item = deepcopy(value) if isinstance(value, dict) else {"type": "metadata_contract_error", "message": str(value)}
        marker = (str(item.get("type") or ""), str(item.get("path") or ""), str(item.get("value") or ""), str(item.get("message") or ""))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


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


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class MetadataAuthoringContractGuardRev2(Component):
    display_name = "06 메타데이터 저장 계약 검증기 rev_2"
    description = "저장 후보의 dataset과 표준 컬럼을 활성 계약으로 확정하고 모호한 경우 저장을 차단합니다."
    inputs = [DataInput(name="payload", display_name="저장 후보 페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="검증 페이로드", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        return Data(data=guard_metadata_contract(getattr(self, "payload", None)))
