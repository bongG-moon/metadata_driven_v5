# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 메타데이터 등록 정제 결과 검증기 rev_2
# 역할: 정제 LLM 응답을 파싱하고 실제 snapshot 후보에 존재하는 유일한 참조만 승인합니다.
# 주요 입력: Context payload, 정제 LLM 응답
# 주요 출력: 정제안과 검증 근거가 포함된 payload
# 유지보수 포인트: LLM이 제안한 key가 실제 registry/candidate에 없으면 저장 단계로 신뢰 전달하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
import re
import textwrap
import unicodedata
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

KIND_ALIASES = {
    "dataset": "dataset",
    "dataset_key": "dataset",
    "table": "dataset",
    "canonical_column": "canonical_column",
    "column": "canonical_column",
    "standard_column": "canonical_column",
    "main_filter": "main_filter",
    "filter": "main_filter",
    "filter_key": "main_filter",
    "domain": "domain",
    "domain_item": "domain",
}


def normalize_refinement(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json_object(llm_response)
    context = _dict(payload.get("metadata_authoring_context"))
    registry = _dict(context.get("registry"))
    raw_text = str(_dict(payload.get("request")).get("raw_text") or "").strip()
    metadata_type = str(payload.get("metadata_type") or "").strip()
    errors: list[dict[str, Any]] = []
    advisory_references: list[dict[str, Any]] = []

    if not parsed:
        errors.append(
            {
                "type": "metadata_refinement_parse_error",
                "message": "메타데이터 정제 응답을 JSON object로 해석하지 못했습니다.",
            }
        )

    resolved: list[dict[str, Any]] = []
    for raw in _list(parsed.get("resolved_references")):
        advisory = _table_catalog_optional_reference_advisory(
            raw,
            metadata_type,
            raw_text,
            from_unresolved=False,
        )
        if advisory:
            advisory_references.append(advisory)
            continue
        checked, error = _validate_reference(raw, context, registry, metadata_type=metadata_type)
        if checked and _keep_reference(checked, context, raw_text):
            resolved.append(checked)
        if error:
            errors.append(error)
    resolved = _dedupe_references(resolved)

    unresolved = []
    for raw in _normalize_unresolved(parsed.get("unresolved_references")):
        advisory = _table_catalog_optional_reference_advisory(
            raw,
            metadata_type,
            raw_text,
            from_unresolved=True,
        )
        if advisory:
            advisory_references.append(advisory)
            continue
        unresolved.append(raw)
    for error in errors:
        if str(error.get("type") or "") not in {
            "unknown_metadata_reference",
            "reference_not_in_context",
            "ambiguous_metadata_reference",
            "metadata_reference_mismatch",
        }:
            continue
        source = str(error.get("input") or "").strip()
        if source:
            unresolved.append(
                {
                    "kind": str(error.get("kind") or "unknown"),
                    "input": source,
                    "candidates": _unique_text(_string_list(error.get("candidates")) + [error.get("registered_target")]),
                    "suggested_target": str(error.get("target") or error.get("registered_target") or "").strip(),
                    "reason": str(error.get("message") or "등록 계약을 하나로 확정하지 못했습니다."),
                }
            )
    conflicts = _reference_conflicts(resolved)
    for conflict in conflicts:
        errors.append(
            {
                "type": "ambiguous_metadata_reference",
                "message": f"'{conflict['input']}' 표현이 여러 계약으로 해석되어 자동 확정하지 않았습니다: {', '.join(conflict['targets'])}",
                "input": conflict["input"],
                "candidates": conflict["targets"],
            }
        )
        unresolved.append(
            {
                "kind": conflict["kind"],
                "input": conflict["input"],
                "candidates": conflict["targets"],
                "reason": "동일 표현에 둘 이상의 등록 계약이 연결되었습니다.",
            }
        )
    if conflicts:
        conflicted = {(item["kind"], _compact(item["input"])) for item in conflicts}
        resolved = [item for item in resolved if (item["kind"], _compact(item["input"])) not in conflicted]

    unresolved = _drop_resolved_unresolved(unresolved, resolved)
    parsed_missing = _drop_table_catalog_optional_missing_information(
        _string_list(parsed.get("missing_information")),
        metadata_type,
        advisory_references,
    )
    parsed_missing = _drop_resolved_missing(parsed_missing, resolved)
    missing = _unique_text(
        [
            *parsed_missing,
            *[f"'{item.get('input', '')}'의 실제 계약을 하나로 지정해 주세요." for item in unresolved if str(item.get("input") or "").strip()],
            *[str(error.get("message") or "") for error in errors if error.get("type") in {"unknown_metadata_reference", "reference_not_in_context"}],
        ]
    )
    assumptions = _unique_text(_string_list(parsed.get("assumptions")))
    refined_text = str(parsed.get("refined_text") or raw_text).strip()
    unsafe_refined_text = _contains_unapproved_target(refined_text, unresolved, errors)
    if unsafe_refined_text:
        refined_text = raw_text
    refined_text = _lock_declared_identity(refined_text, _dict(context.get("declared_identity")))
    formatted_refined_text = refined_text if unsafe_refined_text else _format_refined_text(refined_text)
    formatting_applied = formatted_refined_text != refined_text
    refined_text = formatted_refined_text
    # A Table Catalog model occasionally marks ``needs_more_input`` solely
    # because it treated a source-local column mapping as an unregistered
    # main_filter/canonical reference.  Once every such reference has been
    # converted to an advisory and no concrete missing detail remains, do not
    # suppress the candidate.  The downstream writer still validates source
    # type, query, declared columns and duplicate physical mappings.
    reported_needs_input = _truthy(parsed.get("needs_more_input"))
    advisory_only_needs_input = (
        metadata_type == "table_catalog"
        and bool(advisory_references)
        and not missing
        and not unresolved
    )
    needs_more_input = (reported_needs_input and not advisory_only_needs_input) or bool(missing) or bool(unresolved)

    current_refinement = _dict(payload.get("refinement"))
    current_refinement.update(
        {
            "refined_text": refined_text,
            "needs_more_input": needs_more_input,
            "missing_information": missing,
            "assumptions": assumptions,
        }
    )
    payload["refinement"] = current_refinement
    payload["metadata_authoring_draft"] = {
        "original_text": raw_text,
        "refined_text": refined_text,
        "resolved_references": resolved,
        "unresolved_references": unresolved,
        "missing_information": missing,
        "assumptions": assumptions,
        "needs_more_input": needs_more_input,
    }
    payload.setdefault("errors", []).extend(errors)
    if advisory_references:
        payload.setdefault("warnings", []).extend(advisory_references)
    payload.setdefault("trace", {})["contract_resolution"] = {
        "status": "needs_input" if needs_more_input else "resolved",
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "resolved_references": deepcopy(resolved),
        "unresolved_references": deepcopy(unresolved),
        "refined_text_source": "original_text_fallback" if unsafe_refined_text else "llm_refined_text",
        "refined_text_formatting": {
            "applied": formatting_applied,
            "style": "readable_multiline_v1",
        },
        "table_catalog_reference_scope": {
            "status": "advisory_local_mapping" if advisory_references else "not_needed",
            "advisory_count": len(advisory_references),
            "advisories": deepcopy(advisory_references),
        },
    }
    return payload


def _validate_reference(
    value: Any,
    context: dict[str, Any],
    registry: dict[str, Any],
    metadata_type: str = "",
    deterministic: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(value, dict):
        return None, {"type": "invalid_metadata_reference", "message": "resolved_references 항목은 object여야 합니다."}
    kind = KIND_ALIASES.get(str(value.get("kind") or "").strip().lower(), "")
    source = str(value.get("input") or "").strip()
    target = str(value.get("target") or "").strip()
    if not kind or not source or not target:
        return None, {"type": "invalid_metadata_reference", "message": "확정 참조에는 kind, input, target이 모두 필요합니다."}
    entries, target_field = _registry_spec(registry, kind, metadata_type)
    actual_target = _case_insensitive_key(entries, target)
    if not actual_target:
        return None, {
            "type": "unknown_metadata_reference",
            "message": f"'{source}'을(를) 연결한 '{target}'은 현재 활성 {kind} registry에 없습니다.",
            "kind": kind,
            "input": source,
            "target": target,
        }
    entry = _dict(entries.get(actual_target))
    canonical_target = str(entry.get(target_field) or actual_target).strip()
    allowed = _candidate_targets(context, kind, target_field)
    source_targets = _exact_alias_targets(entries, target_field, source)
    if len(source_targets) > 1:
        return None, {
            "type": "ambiguous_metadata_reference",
            "message": f"'{source}' 표현이 여러 활성 {kind} 계약과 정확히 일치합니다: {', '.join(source_targets)}",
            "kind": kind,
            "input": source,
            "target": canonical_target,
            "candidates": source_targets,
        }
    if len(source_targets) == 1 and source_targets[0].casefold() != canonical_target.casefold():
        return None, {
            "type": "metadata_reference_mismatch",
            "message": f"'{source}'의 등록 계약은 '{source_targets[0]}'이지만 LLM이 '{canonical_target}'을 제안했습니다.",
            "kind": kind,
            "input": source,
            "target": canonical_target,
            "registered_target": source_targets[0],
        }
    phrase_targets = _phrase_alias_targets(entries, target_field, source) if not source_targets else []
    if len(phrase_targets) > 1:
        return None, {
            "type": "ambiguous_metadata_reference",
            "message": f"'{source}' 표현이 여러 활성 {kind} 계약과 같은 수준으로 일치합니다: {', '.join(phrase_targets)}",
            "kind": kind,
            "input": source,
            "target": canonical_target,
            "candidates": phrase_targets,
        }
    if len(phrase_targets) == 1 and phrase_targets[0].casefold() != canonical_target.casefold():
        return None, {
            "type": "metadata_reference_mismatch",
            "message": f"'{source}' 문구는 '{phrase_targets[0]}'에 가장 가깝지만 LLM이 '{canonical_target}'을 제안했습니다.",
            "kind": kind,
            "input": source,
            "target": canonical_target,
            "registered_target": phrase_targets[0],
        }
    allowed_folded = {item.casefold() for item in allowed}
    has_phrase_evidence = bool(source_targets or phrase_targets)
    if not deterministic and not has_phrase_evidence and canonical_target.casefold() not in allowed_folded:
        return None, {
            "type": "reference_not_in_context",
            "message": f"'{source}' → '{canonical_target}' 연결은 이번 원문과 매칭된 등록 후보에 없어 자동 확정하지 않았습니다.",
            "kind": kind,
            "input": source,
            "target": canonical_target,
            "candidates": allowed,
        }
    return (
        {
            "kind": kind,
            "input": source,
            "target": canonical_target,
            "evidence": str(value.get("evidence") or ("registered_unique_alias" if deterministic else "registered_candidate")).strip(),
        },
        None,
    )


def _registry_spec(registry: dict[str, Any], kind: str, metadata_type: str = "") -> tuple[dict[str, Any], str]:
    if kind == "dataset":
        return _dict(registry.get("datasets")), "dataset_key"
    if kind == "canonical_column":
        if metadata_type == "table_catalog":
            scoped = _dict(registry.get("table_catalog_canonical_columns"))
            if scoped:
                return scoped, "key"
        return _dict(registry.get("canonical_columns")), "key"
    if kind == "main_filter":
        return _dict(registry.get("filters")), "filter_key"
    return _dict(registry.get("domains")), "domain_key"


def _candidate_targets(context: dict[str, Any], kind: str, target_field: str) -> list[str]:
    key = {"dataset": "datasets", "canonical_column": "canonical_columns", "main_filter": "main_filters", "domain": "domains"}[kind]
    return _unique_text([_dict(item).get(target_field) for item in _list(_dict(context.get("candidates")).get(key))])


def _case_insensitive_key(entries: dict[str, Any], target: str) -> str:
    folded = target.casefold()
    return next((key for key in entries if str(key).casefold() == folded), "")


def _exact_alias_targets(entries: dict[str, Any], target_field: str, source: str) -> list[str]:
    compact = _compact(source)
    targets = []
    for key, raw_entry in entries.items():
        entry = _dict(raw_entry)
        target = str(entry.get(target_field) or key).strip()
        aliases = _unique_text([target, *_string_list(entry.get("aliases"))])
        if compact and any(_compact(alias) == compact for alias in aliases):
            targets.append(target)
    return _unique_text(targets)


def _phrase_alias_targets(entries: dict[str, Any], target_field: str, source: str) -> list[str]:
    scored: list[tuple[int, str]] = []
    for key, raw_entry in entries.items():
        entry = _dict(raw_entry)
        target = str(entry.get(target_field) or key).strip()
        aliases = _unique_text([target, *_string_list(entry.get("aliases"))])
        score = max((_phrase_score(source, alias) for alias in aliases), default=0)
        if score > 0:
            scored.append((score, target))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return _unique_text([target for score, target in scored if score == best])


def _phrase_score(source: str, alias: str) -> int:
    source_compact = _compact(source)
    alias_compact = _compact(alias)
    if min(len(source_compact), len(alias_compact)) >= 4 and (
        source_compact in alias_compact or alias_compact in source_compact
    ):
        return 100 + min(len(source_compact), len(alias_compact))
    source_tokens = set(_tokens(source))
    alias_tokens = set(_tokens(alias))
    overlap = source_tokens & alias_tokens
    return len(overlap) * 10 if len(overlap) >= 2 else 0


def _normalize_unresolved(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        source = str(item.get("input") or "").strip()
        if not source:
            continue
        result.append(
            {
                "kind": KIND_ALIASES.get(str(item.get("kind") or "").strip().lower(), str(item.get("kind") or "unknown")),
                "input": source,
                "candidates": _unique_text(_string_list(item.get("candidates"))),
                "suggested_target": str(item.get("suggested_target") or item.get("target") or "").strip(),
                "reason": str(item.get("reason") or "등록 계약을 하나로 확정하지 못했습니다.").strip(),
            }
        )
    return result


def _table_catalog_optional_reference_advisory(
    value: Any,
    metadata_type: str,
    raw_text: str,
    *,
    from_unresolved: bool,
) -> dict[str, Any] | None:
    """Downgrade source-local mapping noise only for Table Catalog authoring.

    ``filter_mappings`` establishes a new table's own canonical-to-physical
    binding.  It is not a request to register or resolve a reusable
    ``main_filter``.  Likewise, a physical identifier such as ``WORK_DT`` is
    not a global canonical reference merely because an older dataset happened
    to use it under multiple keys.  Domain and Main Flow Filter authoring keep
    the existing strict reference behavior.
    """

    if metadata_type != "table_catalog" or not isinstance(value, dict):
        return None
    kind = KIND_ALIASES.get(str(value.get("kind") or "").strip().lower(), "")
    source = str(value.get("input") or "").strip()
    target = str(value.get("target") or value.get("suggested_target") or "").strip()
    if not kind or not source:
        return None
    if kind == "main_filter" and not _requests_explicit_main_filter_reference(raw_text):
        return {
            "type": "table_catalog_optional_main_filter_reference",
            "message": (
                f"'{source}' → '{target or '미지정'}'은(는) 신규 Table Catalog의 컬럼 매핑으로 처리했습니다. "
                "기존 main_filter 등록 여부는 저장을 막지 않습니다."
            ),
            "kind": kind,
            "input": source,
            "target": target,
            "scope": "candidate_local_filter_mapping",
        }
    if (
        from_unresolved
        and kind == "canonical_column"
        and _looks_like_source_identifier(source)
        and _has_table_mapping_context(raw_text)
        and not _requests_explicit_global_canonical_reference(raw_text)
    ):
        return {
            "type": "table_catalog_source_column_reference",
            "message": (
                f"'{source}'은(는) 신규 Table Catalog의 source column 후보로 처리했습니다. "
                "기존 데이터셋의 canonical alias 충돌은 저장을 막지 않습니다."
            ),
            "kind": kind,
            "input": source,
            "target": target,
            "scope": "candidate_local_source_schema",
        }
    return None


def _requests_explicit_main_filter_reference(raw_text: str) -> bool:
    lowered = str(raw_text or "").casefold()
    return any(
        cue in lowered
        for cue in (
            "기존 main filter",
            "기존 main_flow_filter",
            "등록된 main filter",
            "등록된 main_flow_filter",
            "기존 메인 필터",
            "등록된 메인 필터",
            "활성 메인 필터",
            "main_filter를 참조",
            "main filter를 참조",
        )
    )


def _requests_explicit_global_canonical_reference(raw_text: str) -> bool:
    lowered = str(raw_text or "").casefold()
    return any(
        cue in lowered
        for cue in (
            "기존 표준 컬럼",
            "등록된 표준 컬럼",
            "활성 표준 컬럼",
            "canonical_column을 참조",
            "canonical column을 참조",
        )
    )


def _has_table_mapping_context(raw_text: str) -> bool:
    lowered = str(raw_text or "").casefold()
    return any(
        cue in lowered
        for cue in (
            "filter_mappings",
            "filter mapping",
            "컬럼 매핑",
            "매핑",
            "대응",
            "실제 컬럼",
            "source column",
            "query_template",
            "조회 sql",
            "select ",
        )
    )


def _looks_like_source_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(value or "").strip()))


def _drop_table_catalog_optional_missing_information(
    values: list[str],
    metadata_type: str,
    advisories: list[dict[str, Any]],
) -> list[str]:
    if metadata_type != "table_catalog" or not advisories:
        return values
    advisory_inputs = [_compact(item.get("input")) for item in advisories if _compact(item.get("input"))]
    result = []
    for value in values:
        compact = _compact(value)
        refers_to_advisory = any(source and source in compact for source in advisory_inputs)
        registry_only_reason = any(
            token in str(value or "").casefold()
            for token in (
                "main_filter",
                "main filter",
                "메인 필터",
                "canonical",
                "canonical alias",
                "활성 계약",
                "등록 계약",
            )
        )
        if refers_to_advisory and registry_only_reason:
            continue
        result.append(value)
    return result


def _reference_conflicts(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in resolved:
        marker = (str(item.get("kind") or ""), _compact(item.get("input")))
        group = grouped.setdefault(marker, {"kind": marker[0], "input": str(item.get("input") or ""), "targets": []})
        group["targets"] = _unique_text([*group["targets"], item.get("target")])
    return [item for item in grouped.values() if len(item["targets"]) > 1]


def _append_reference_block(text: str, resolved: list[dict[str, Any]], unresolved: list[dict[str, Any]]) -> str:
    sections = [text.strip()] if text.strip() else []
    if resolved:
        lines = ["[확정된 기존 메타데이터 참조]"]
        lines.extend(f"- {item['kind']}: {item['input']} -> {item['target']}" for item in resolved)
        sections.append("\n".join(lines))
    if unresolved:
        lines = ["[저장 전 확인이 필요한 참조]"]
        for item in unresolved:
            candidates = ", ".join(_string_list(item.get("candidates"))) or "등록 후보 없음"
            lines.append(f"- {item['input']}: {candidates} ({item.get('reason', '')})")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _keep_reference(reference: dict[str, Any], context: dict[str, Any], raw_text: str) -> bool:
    source = str(reference.get("input") or "").strip()
    target = str(reference.get("target") or "").strip()
    if _compact(source) == _compact(target):
        return False
    if str(reference.get("kind") or "") != "domain":
        return True
    declared = _dict(context.get("declared_identity"))
    declared_target = f"{declared.get('section', '')}:{declared.get('key', '')}".strip(":")
    if declared_target and target.casefold() == declared_target.casefold():
        return False
    lowered = raw_text.casefold()
    return any(token in lowered for token in ("기존", "참조", "재사용", "reuse", "reference"))


def _drop_resolved_unresolved(
    unresolved: list[dict[str, Any]], resolved: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    resolved_markers = {
        (str(item.get("kind") or ""), _compact(item.get("input")))
        for item in resolved
    }
    return [
        item
        for item in unresolved
        if (str(item.get("kind") or ""), _compact(item.get("input"))) not in resolved_markers
    ]


def _drop_resolved_missing(values: list[str], resolved: list[dict[str, Any]]) -> list[str]:
    resolved_inputs = [_compact(item.get("input")) for item in resolved if str(item.get("input") or "").strip()]
    result = []
    for value in values:
        compact = _compact(value)
        is_resolution_request = any(token in value for token in ("실제 계약", "하나로 지정", "하나를 선택", "확정"))
        if is_resolution_request and any(source and source in compact for source in resolved_inputs):
            continue
        result.append(value)
    return result


def _lock_declared_identity(text: str, declared: dict[str, Any]) -> str:
    result = str(text or "").strip()
    missing: dict[str, str] = {}
    for field in ("dataset_key", "filter_key", "section", "key", "status"):
        expected = str(declared.get(field) or "").strip()
        if not expected:
            continue
        pattern = re.compile(
            rf"(?i)(?<![A-Za-z0-9_])({re.escape(field)}\s*(?:은|는|:|=)\s*[`'\"]?)[A-Za-z][A-Za-z0-9_]*"
        )
        if pattern.search(result):
            result = pattern.sub(lambda match: match.group(1) + expected, result)
        else:
            missing[field] = expected
    if missing:
        labels = {
            "dataset_key": "dataset_key는",
            "filter_key": "filter_key는",
            "section": "section은",
            "key": "key는",
            "status": "status는",
        }
        order = ("dataset_key", "filter_key", "section", "key", "status")
        identity = ", ".join(f"{labels[field]} {missing[field]}" for field in order if field in missing)
        result = "\n\n".join(part for part in (result, f"{identity}로 등록해.") if part)
    return result


_INLINE_QUOTED_SQL = re.compile(
    r"(?is)(?P<label>(?:조회\s*)?(?:SQL|쿼리)(?:문)?(?:은|는))\s*"
    r"(?P<quote>['\"])(?P<sql>(?:SELECT|WITH)\b.*?)(?P=quote)\s*"
    r"(?P<ending>이다|입니다|야|이야|예요)?(?P<period>\.)"
)
_INLINE_UNQUOTED_SQL = re.compile(
    r"(?is)(?P<label>(?:조회\s*)?(?:SQL|쿼리)(?:문)?(?:은|는))\s*"
    r"(?P<sql>(?:SELECT|WITH)\b.*?)(?P<ending>이다|입니다|야|이야|예요)(?P<period>\.)"
)
_FENCED_SQL = re.compile(r"(?is)```(?:sql)?\s*(?P<sql>(?:SELECT|WITH)\b.*?)\s*```")
_SQL_PROTECTED_SEGMENT = re.compile(
    r"(?ms)--[^\r\n]*(?:\n|$)|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\""
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[`'\"(\[A-Za-z0-9가-힣])")
_IDENTITY_SENTENCE = re.compile(
    r"(?i)(?:dataset_key|filter_key|section|(?:^|\s)key\s*(?:은|는|:|=)|status|표시명|"
    r"db_key|같은\s+(?:필터\s+)?표현|같은\s+의미)"
)


def _format_refined_text(text: str) -> str:
    """읽기 어려운 LLM 한 줄 출력을 의미 변경 없이 여러 줄로 정리합니다."""

    original = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not original:
        return ""

    protected, sql_blocks, sql_was_reformatted = _protect_inline_sql(original)
    nonempty_lines = [line for line in protected.splitlines() if line.strip()]
    sentence_count = len(_SENTENCE_BOUNDARY.split(" ".join(nonempty_lines)))
    has_compact_sql_line = any(
        re.match(r"(?i)^(?:SELECT|WITH)\b", line.strip())
        and re.search(r"(?i)\s(?:FROM|WHERE|JOIN|GROUP\s+BY|ORDER\s+BY)\s", line)
        for line in nonempty_lines
    )
    needs_prose_format = (
        any(len(line) > 140 for line in nonempty_lines)
        or (len(nonempty_lines) == 1 and sentence_count >= 3)
        or has_compact_sql_line
    )

    if needs_prose_format:
        paragraphs = re.split(r"\n\s*\n", protected)
        protected = "\n\n".join(
            formatted
            for paragraph in paragraphs
            if (formatted := _format_prose_paragraph(paragraph))
        )

    for marker, sql in sql_blocks:
        protected = protected.replace(marker, sql)

    if not needs_prose_format and not sql_was_reformatted:
        return original
    return _normalize_blank_lines(protected)


def _protect_inline_sql(text: str) -> tuple[str, list[tuple[str, str]], bool]:
    blocks: list[tuple[str, str]] = []

    def replace_fence(match: re.Match[str]) -> str:
        marker = f"<<<METADATA_REFINED_SQL_{len(blocks)}>>>"
        blocks.append((marker, _format_sql(match.group("sql"))))
        return f"\n\n{marker}\n\n"

    def replace(match: re.Match[str]) -> str:
        marker = f"<<<METADATA_REFINED_SQL_{len(blocks)}>>>"
        label = str(match.group("label") or "조회 SQL은").strip()
        ending = str(match.group("ending") or "").strip()
        if ending == "입니다":
            introduction = f"{label} 아래와 같습니다."
        elif ending in {"야", "이야", "예요"}:
            introduction = f"{label} 아래와 같아."
        else:
            introduction = f"{label} 아래와 같다."
        block = f"{introduction}\n\n{_format_sql(match.group('sql'))}"
        blocks.append((marker, block))
        return f"\n\n{marker}\n\n"

    protected = _FENCED_SQL.sub(replace_fence, text)
    protected = _INLINE_QUOTED_SQL.sub(replace, protected)
    protected = _INLINE_UNQUOTED_SQL.sub(replace, protected)
    return protected, blocks, bool(blocks)


def _format_sql(value: Any) -> str:
    sql = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not sql:
        return ""

    protected_segments: list[str] = []

    def protect_segment(match: re.Match[str]) -> str:
        marker = f"<<<SQL_PROTECTED_{len(protected_segments)}>>>"
        protected_segments.append(match.group(0))
        return marker

    protected = _SQL_PROTECTED_SEGMENT.sub(protect_segment, sql)
    clauses = (
        r"LEFT\s+OUTER\s+JOIN",
        r"RIGHT\s+OUTER\s+JOIN",
        r"FULL\s+OUTER\s+JOIN",
        r"LEFT\s+JOIN",
        r"RIGHT\s+JOIN",
        r"FULL\s+JOIN",
        r"INNER\s+JOIN",
        r"CROSS\s+JOIN",
        r"GROUP\s+BY",
        r"ORDER\s+BY",
        r"UNION\s+ALL",
        r"UNION",
        r"FROM",
        r"WHERE",
        r"HAVING",
        r"LIMIT",
        r"OFFSET",
    )
    protected = re.sub(
        rf"[ \t]+(?=(?:{'|'.join(clauses)})\b)",
        "\n",
        protected,
        flags=re.IGNORECASE,
    )
    protected = re.sub(r"(?i)^SELECT[ \t]+", "SELECT\n    ", protected)
    protected = re.sub(r"(?i)^WITH[ \t]+", "WITH\n    ", protected)
    for index, segment in enumerate(protected_segments):
        protected = protected.replace(f"<<<SQL_PROTECTED_{index}>>>", segment)
    return "\n".join(line.rstrip() for line in protected.splitlines()).strip()


def _format_prose_paragraph(paragraph: str) -> str:
    lines = [line.strip() for line in str(paragraph or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if re.match(r"(?i)^(?:SELECT|WITH)\b", " ".join(lines)):
        return _format_sql(" ".join(lines))
    if len(lines) > 1:
        formatted_lines: list[str] = []
        for line in lines:
            parts = _format_content_line(line)
            if formatted_lines and parts and re.match(r"(?i)^(?:SELECT|WITH)\b", parts[0]):
                formatted_lines.append("")
            formatted_lines.extend(parts)
        return "\n".join(formatted_lines)

    sentences = [item.strip() for item in _SENTENCE_BOUNDARY.split(lines[0]) if item.strip()]
    if len(sentences) <= 1:
        return "\n".join(_wrap_long_line(lines[0]))

    groups: list[list[str]] = []
    current_group: list[str] = []
    current_kind = ""
    for index, sentence in enumerate(sentences):
        kind = _prose_group(sentence, index)
        if current_group and kind != current_kind:
            groups.append(current_group)
            current_group = []
        current_group.extend(_format_content_line(sentence))
        current_kind = kind
    if current_group:
        groups.append(current_group)
    return "\n\n".join("\n".join(group) for group in groups)


def _prose_group(sentence: str, index: int) -> str:
    compact = str(sentence or "").strip()
    if index == 0 and "등록" in compact and "메타데이터" in compact:
        return "registration"
    if _IDENTITY_SENTENCE.search(compact):
        return "identity"
    return "details"


def _wrap_long_line(line: str) -> list[str]:
    value = str(line or "").strip()
    if len(value) <= 140 or value.startswith("<<<METADATA_REFINED_SQL_"):
        return [value]
    return textwrap.wrap(
        value,
        width=120,
        break_long_words=False,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=True,
    ) or [value]


def _format_content_line(line: str) -> list[str]:
    value = str(line or "").strip()
    if re.match(r"(?i)^(?:SELECT|WITH)\b", value):
        return _format_sql(value).splitlines()
    return _wrap_long_line(value)


def _normalize_blank_lines(text: str) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines()]
    result: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and (previous_blank or not result):
            continue
        result.append("" if blank else line)
        previous_blank = blank
    while result and not result[-1]:
        result.pop()
    return "\n".join(result).strip()


def _contains_unapproved_target(
    text: str,
    unresolved: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> bool:
    targets: list[str] = []
    for item in unresolved:
        targets.extend(_string_list(item.get("candidates")))
        targets.extend(
            str(item.get(field) or "").strip()
            for field in ("target", "suggested_target")
            if str(item.get(field) or "").strip()
        )
    for error in errors:
        targets.extend(_string_list(error.get("candidates")))
        targets.extend(
            str(error.get(field) or "").strip()
            for field in ("target", "registered_target")
            if str(error.get(field) or "").strip()
        )
    return any(_text_contains_target(text, target) for target in _unique_text(targets))


def _text_contains_target(text: str, target: str) -> bool:
    key = str(target or "").strip()
    if not key:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", key):
        return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", text, flags=re.IGNORECASE))
    return _compact(key) in _compact(text)


def _dedupe_references(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in values:
        marker = (item.get("kind"), _compact(item.get("input")), str(item.get("target") or "").casefold())
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


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


def _compact(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", normalized)


def _tokens(value: Any) -> list[str]:
    stop = {"등록", "저장", "정보", "내용", "관련", "사용", "데이터", "데이터셋", "테이블", "컬럼", "조건"}
    separated = re.sub(
        r"(?<=[A-Za-z0-9_])(?=[가-힣])|(?<=[가-힣])(?=[A-Za-z0-9_])",
        " ",
        str(value or ""),
    )
    return [
        token
        for token in (_compact(item) for item in re.findall(r"[가-힣A-Za-z0-9_]+", separated))
        if len(token) >= 2 and token not in stop
    ]


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


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class MetadataAuthoringRefinementNormalizerRev2(Component):
    display_name = "04 메타데이터 등록 정제 결과 검증기 rev_2"
    description = "정제 LLM의 참조를 활성 metadata registry와 대조하고 원문과 분리된 정제안을 만듭니다."
    inputs = [
        DataInput(name="payload", display_name="Context 페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="정제 LLM 응답", required=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        return Data(data=normalize_refinement(getattr(self, "payload", None), getattr(self, "llm_response", "")))
