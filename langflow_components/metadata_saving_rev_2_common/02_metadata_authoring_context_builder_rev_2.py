# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 메타데이터 등록 Context 생성기 rev_2
# 역할: 원문과 활성 MongoDB 메타데이터를 비교해 정제 LLM용 후보와 결정론적 검증 registry를 만듭니다.
# 주요 입력: 저장 요청 payload, 도메인·테이블·메인 필터 snapshot
# 주요 출력: payload, 원문, metadata context, metadata type
# 유지보수 포인트: query/credential/raw registration trace는 LLM context에서 제외하고 저장 writer에는 transient context를 전달하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

DEFAULT_MAX_CANDIDATES = 16
DEFAULT_MAX_CONTEXT_BYTES = 32768
SECRET_KEY_PARTS = ("password", "passwd", "token", "secret", "api_key", "apikey", "credential", "mongo_uri")
STOP_TOKENS = {
    "등록", "저장", "정보", "내용", "관련", "사용", "데이터", "데이터셋", "테이블", "컬럼", "조건",
    "the", "a", "an", "and", "or", "data", "dataset", "table", "column",
}


def build_authoring_context(
    payload_value: Any,
    domain_items_value: Any = None,
    table_catalog_items_value: Any = None,
    main_flow_filters_value: Any = None,
    max_candidates: Any = str(DEFAULT_MAX_CANDIDATES),
    max_context_bytes: Any = str(DEFAULT_MAX_CONTEXT_BYTES),
) -> dict[str, Any]:
    payload = _payload(payload_value)
    raw_text = str(_dict(payload.get("request")).get("raw_text") or "").strip()
    metadata_type = str(payload.get("metadata_type") or "").strip()
    declared_identity = _declared_identity(raw_text, metadata_type)
    limit = _bounded_int(max_candidates, DEFAULT_MAX_CANDIDATES, 4, 50)
    byte_limit = _bounded_int(max_context_bytes, DEFAULT_MAX_CONTEXT_BYTES, 4096, 65536)

    domain_items, domain_load = _extract_items(domain_items_value, "domain_items")
    table_items, table_load = _extract_items(table_catalog_items_value, "table_catalog_items")
    filter_items, filter_load = _extract_items(main_flow_filters_value, "main_flow_filters")
    registry = _build_registry(domain_items, table_items, filter_items)
    load_summary = {
        "domain_items": _compact_load(domain_load),
        "table_catalog_items": _compact_load(table_load),
        "main_flow_filters": _compact_load(filter_load),
    }
    # Table Catalog authoring has a different namespace boundary from Domain
    # authoring.  The left side of a new ``filter_mappings`` entry is a
    # canonical execution key, while its right side is a physical column owned
    # by the *new* source.  Existing physical column aliases are useful when
    # validating an existing Domain rule, but must not make a new Table
    # Catalog's source column look like a globally ambiguous canonical key.
    canonical_registry = _canonical_registry_for_metadata_type(registry, metadata_type)
    candidates = {
        "datasets": _ranked_candidates(raw_text, registry["datasets"], "dataset_key", limit),
        "canonical_columns": _ranked_candidates(raw_text, canonical_registry, "key", limit),
        "main_filters": _ranked_candidates(raw_text, registry["filters"], "filter_key", limit),
        "domains": (
            _ranked_candidates(raw_text, registry["domains"], "domain_key", limit)
            if _requests_existing_domain_reference(raw_text)
            else []
        ),
    }
    exact_resolutions = _exact_resolutions(raw_text, registry, metadata_type)
    prompt_context = {
        "snapshot_status": load_summary,
        "resolution_policy": "registered_target_with_phrase_local_validation",
        "table_catalog_mapping_policy": (
            "candidate_local_physical_mapping_precedes_global_aliases"
            if metadata_type == "table_catalog"
            else "not_applicable"
        ),
        "declared_identity": declared_identity,
        "datasets": [_prompt_projection(item, "dataset") for item in candidates["datasets"]],
        "canonical_columns": [_prompt_projection(item, "canonical_column") for item in candidates["canonical_columns"]],
        "main_filters": [_prompt_projection(item, "main_filter") for item in candidates["main_filters"]],
        "domains": [_prompt_projection(item, "domain") for item in candidates["domains"]],
        "deterministic_exact_matches": exact_resolutions,
    }
    prompt_context = _fit_context(prompt_context, byte_limit)

    context_status = "ok" if all(item.get("status") == "ok" for item in load_summary.values()) else "error"
    if context_status != "ok":
        diagnostic = {
            "type": "metadata_authoring_context_unavailable",
            "message": "활성 도메인·테이블·메인 필터 metadata snapshot을 모두 읽지 못했습니다.",
            "loads": deepcopy(load_summary),
        }
        # A new Table Catalog can be self-contained: the existing normalizer
        # and writer still verify its source type, query and source-local
        # columns.  A read failure for unrelated active metadata must not
        # preempt that validation.  Explicit references to existing metadata
        # remain unresolved in the later refinement step and still block.
        if metadata_type == "table_catalog":
            diagnostic["message"] += " 새 Table Catalog 후보는 source-local 계약으로 계속 검증합니다."
            diagnostic["severity"] = "warning"
            payload.setdefault("warnings", []).append(diagnostic)
        else:
            diagnostic["message"] += " rev_2 저장을 중단합니다."
            payload.setdefault("errors", []).append(diagnostic)

    context = {
        "status": context_status,
        "load_summary": load_summary,
        "declared_identity": declared_identity,
        "registry": registry,
        "candidates": candidates,
        "exact_resolutions": exact_resolutions,
        "prompt_context": prompt_context,
    }
    payload["metadata_authoring_context"] = context
    payload.setdefault("trace", {})["metadata_contract_snapshot"] = {
        "status": context_status,
        "counts": {
            "domain_items": len(domain_items),
            "table_catalog_items": len(table_items),
            "main_flow_filters": len(filter_items),
        },
        "loads": load_summary,
    }
    return {
        "payload": payload,
        "source_text": raw_text,
        "metadata_type": metadata_type,
        "metadata_context": json.dumps(prompt_context, ensure_ascii=False, separators=(",", ":")),
    }


def _build_registry(domain_items: list[dict[str, Any]], table_items: list[dict[str, Any]], filter_items: list[dict[str, Any]]) -> dict[str, Any]:
    datasets: dict[str, dict[str, Any]] = {}
    filters: dict[str, dict[str, Any]] = {}
    domains: dict[str, dict[str, Any]] = {}
    # ``canonical_sources`` preserves the existing broad registry used by
    # Domain/Main Filter validation.  ``table_catalog_canonical_sources`` is
    # deliberately narrower: it excludes source-local physical columns so a
    # Table Catalog registration cannot be blocked by aliases from unrelated
    # existing datasets.
    canonical_sources: dict[str, dict[str, Any]] = {}
    table_catalog_canonical_sources: dict[str, dict[str, Any]] = {}

    for item in table_items:
        key = str(item.get("dataset_key") or item.get("key") or "").strip()
        if not key:
            continue
        body = _dict(item.get("payload"))
        filter_mappings = _mapping_lists(body.get("filter_mappings"))
        standard_aliases = _mapping_lists(body.get("standard_column_aliases"))
        canonical_columns = _unique_text(
            [
                *filter_mappings,
                *standard_aliases,
                *_string_list(body.get("required_params")),
                *_string_list(body.get("default_detail_columns")),
                *list(_dict(body.get("metric_semantics"))),
            ]
        )
        aliases = _unique_text(
            [
                key,
                body.get("display_name"),
                *_string_list(body.get("aliases")),
            ]
        )
        physical_column_keys = _physical_column_keys(body, filter_mappings)
        column_aliases: dict[str, list[str]] = {}
        for canonical in canonical_columns:
            column_aliases[canonical] = _unique_text(
                [canonical, *filter_mappings.get(canonical, []), *standard_aliases.get(canonical, [])]
            )
            entry = canonical_sources.setdefault(canonical, {"key": canonical, "aliases": [], "datasets": []})
            entry["aliases"] = _unique_text([*entry["aliases"], *column_aliases[canonical]])
            entry["datasets"] = _unique_text([*entry["datasets"], key])

            # A standard alias can be a human/business phrase or a physical
            # source column.  Only the former belongs to the Table Catalog
            # canonical lookup namespace.  Physical values remain visible in
            # the dataset-local ``column_aliases`` contract above.
            semantic_aliases = _unique_text(
                [
                    canonical,
                    *[
                        alias
                        for alias in standard_aliases.get(canonical, [])
                        if _compact(alias) not in physical_column_keys
                    ],
                ]
            )
            table_entry = table_catalog_canonical_sources.setdefault(
                canonical,
                {"key": canonical, "aliases": [], "datasets": []},
            )
            table_entry["aliases"] = _unique_text([*table_entry["aliases"], *semantic_aliases])
            table_entry["datasets"] = _unique_text([*table_entry["datasets"], key])
        datasets[key] = {
            "dataset_key": key,
            "display_name": str(body.get("display_name") or key),
            "aliases": aliases,
            "canonical_columns": canonical_columns,
            "column_aliases": column_aliases,
            "filter_mappings": filter_mappings,
            "standard_column_aliases": standard_aliases,
        }

    for item in filter_items:
        key = str(item.get("filter_key") or item.get("key") or "").strip()
        if not key:
            continue
        body = _dict(item.get("payload"))
        semantic_aliases = _unique_text([key, body.get("display_name"), *_string_list(body.get("aliases"))])
        aliases = _unique_text([*semantic_aliases, *_string_list(body.get("column_candidates"))])
        filters[key] = {
            "filter_key": key,
            "display_name": str(body.get("display_name") or key),
            "aliases": aliases,
            "column_candidates": _string_list(body.get("column_candidates")),
        }
        # Preserve the existing broad registry for Domain/Main Filter paths.
        # The Table Catalog path below deliberately receives only
        # ``semantic_aliases`` so a physical candidate cannot be mistaken for
        # a globally unique source-local mapping during new-table authoring.
        entry = canonical_sources.setdefault(key, {"key": key, "aliases": [], "datasets": []})
        entry["aliases"] = _unique_text([*entry["aliases"], *aliases])
        table_entry = table_catalog_canonical_sources.setdefault(key, {"key": key, "aliases": [], "datasets": []})
        table_entry["aliases"] = _unique_text([*table_entry["aliases"], *semantic_aliases])

    for item in domain_items:
        section = str(item.get("section") or "").strip()
        key = str(item.get("key") or "").strip()
        if not section or not key:
            continue
        body = _dict(item.get("payload"))
        domain_key = f"{section}:{key}"
        domains[domain_key] = {
            "domain_key": domain_key,
            "section": section,
            "key": key,
            "display_name": str(body.get("display_name") or key),
            "aliases": _unique_text([key, body.get("display_name"), *_string_list(body.get("aliases"))]),
        }

    return {
        "datasets": datasets,
        "canonical_columns": canonical_sources,
        "table_catalog_canonical_columns": table_catalog_canonical_sources,
        "filters": filters,
        "domains": domains,
    }


def _ranked_candidates(raw_text: str, entries: dict[str, dict[str, Any]], key_field: str, limit: int) -> list[dict[str, Any]]:
    ranked = []
    for entry in entries.values():
        score = _score(raw_text, _string_list(entry.get("aliases")))
        if score > 0:
            candidate = deepcopy(entry)
            candidate["match_score"] = score
            ranked.append(candidate)
    ranked.sort(key=lambda item: (-int(item.get("match_score") or 0), str(item.get(key_field) or "")))
    return ranked[:limit]


def _score(raw_text: str, aliases: list[str]) -> int:
    raw_compact = _compact(raw_text)
    raw_tokens = set(_tokens(raw_text))
    best = 0
    for alias in aliases:
        alias_compact = _compact(alias)
        if not alias_compact:
            continue
        score = 0
        if len(alias_compact) >= 2 and alias_compact in raw_compact:
            score += 100 + min(len(alias_compact), 30)
        alias_tokens = set(_tokens(alias))
        overlap = raw_tokens & alias_tokens
        score += len(overlap) * 12
        if alias_tokens and alias_tokens <= raw_tokens:
            score += 20
        best = max(best, score)
    return best


def _canonical_registry_for_metadata_type(registry: dict[str, Any], metadata_type: str) -> dict[str, dict[str, Any]]:
    if metadata_type == "table_catalog":
        scoped = _dict(registry.get("table_catalog_canonical_columns"))
        if scoped:
            return scoped
    return _dict(registry.get("canonical_columns"))


def _exact_resolutions(raw_text: str, registry: dict[str, Any], metadata_type: str = "") -> list[dict[str, str]]:
    raw_compact = _compact(raw_text)
    results: list[dict[str, str]] = []
    specs = (
        ("dataset", registry["datasets"], "dataset_key"),
        ("canonical_column", _canonical_registry_for_metadata_type(registry, metadata_type), "key"),
        ("main_filter", registry["filters"], "filter_key"),
    )
    for kind, entries, target_field in specs:
        alias_targets: dict[str, set[str]] = {}
        alias_original: dict[str, str] = {}
        for entry in entries.values():
            target = str(entry.get(target_field) or "").strip()
            for alias in _string_list(entry.get("aliases")):
                compact = _compact(alias)
                if len(compact) < 2:
                    continue
                alias_targets.setdefault(compact, set()).add(target)
                alias_original.setdefault(compact, alias)
        for alias, targets in alias_targets.items():
            target = next(iter(targets)) if len(targets) == 1 else ""
            if alias in raw_compact and target and alias != _compact(target):
                results.append({"kind": kind, "input": alias_original[alias], "target": target, "evidence": "registered_unique_alias"})
    return _dedupe_references(results)


def _declared_identity(raw_text: str, metadata_type: str) -> dict[str, str]:
    """Extract only identifiers that the user explicitly wrote.

    These values are authoritative for rev_2.  They are kept outside the item
    document and later used by the contract guard so an LLM cannot rename a
    requested identity to avoid an existing-key match.
    """

    patterns = {
        "section": r"(?i)(?<![A-Za-z0-9_])section\s*(?:은|는|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_]*)",
        "key": r"(?i)(?<![A-Za-z0-9_])key\s*(?:은|는|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_]*)",
        "dataset_key": r"(?i)(?<![A-Za-z0-9_])dataset_key\s*(?:은|는|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_]*)",
        "filter_key": r"(?i)(?<![A-Za-z0-9_])filter_key\s*(?:은|는|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_]*)",
        "status": r"(?i)(?<![A-Za-z0-9_])status\s*(?:은|는|:|=)?\s*[`'\"]?([A-Za-z][A-Za-z0-9_]*)",
    }
    allowed = {
        "domain": ("section", "key", "status"),
        "table_catalog": ("dataset_key", "status"),
        "main_flow_filter": ("filter_key", "status"),
    }.get(metadata_type, ())
    result: dict[str, str] = {}
    for field in allowed:
        match = re.search(patterns[field], raw_text)
        if match:
            result[field] = match.group(1)
    return result


def _requests_existing_domain_reference(raw_text: str) -> bool:
    lowered = str(raw_text or "").casefold()
    return any(
        cue in lowered
        for cue in (
            "기존 도메인",
            "도메인 참조",
            "도메인 규칙을 참조",
            "domain reference",
            "reuse domain",
        )
    )


def _prompt_projection(item: dict[str, Any], kind: str) -> dict[str, Any]:
    allowed = {
        "dataset": ("dataset_key", "display_name", "aliases", "canonical_columns", "filter_mappings", "standard_column_aliases", "match_score"),
        "canonical_column": ("key", "aliases", "datasets", "match_score"),
        "main_filter": ("filter_key", "display_name", "aliases", "column_candidates", "match_score"),
        "domain": ("domain_key", "section", "key", "display_name", "aliases", "match_score"),
    }[kind]
    return {key: deepcopy(item[key]) for key in allowed if key in item}


def _fit_context(context: dict[str, Any], byte_limit: int) -> dict[str, Any]:
    result = deepcopy(context)
    while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > byte_limit:
        lists = [result.get(name) for name in ("domains", "main_filters", "canonical_columns", "datasets")]
        target = max((value for value in lists if isinstance(value, list) and value), key=len, default=None)
        if target is None:
            break
        target.pop()
    return result


def _extract_items(value: Any, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return [], {"status": "missing", "count": 0, "errors": [{"type": "missing_snapshot_input", "message": f"{key} snapshot 입력이 없습니다."}]}
    items = [deepcopy(item) for item in data.get(key, []) if isinstance(item, dict)] if isinstance(data.get(key), list) else []
    load = deepcopy(data.get("metadata_load")) if isinstance(data.get("metadata_load"), dict) else {}
    load.setdefault("status", "ok" if key in data else "missing")
    load.setdefault("count", len(items))
    load.setdefault("errors", [])
    return items, load


def _compact_load(value: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value.get(key)) for key in ("status", "count", "database", "collection_name", "truncated", "errors") if key in value}


def _mapping_lists(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, raw in value.items():
        canonical = str(key or "").strip()
        if canonical:
            result[canonical] = _unique_text(_string_list(raw) if isinstance(raw, list) else [raw])
    return result


def _physical_column_keys(body: dict[str, Any], filter_mappings: dict[str, list[str]]) -> set[str]:
    """Return source-local physical column names declared by one catalog item.

    Table Catalog's filter mappings and query result columns are source schema,
    not cross-dataset canonical aliases.  Keeping this small helper here makes
    the boundary explicit without changing the existing dataset-local contract.
    """

    names: list[str] = [
        value
        for aliases in filter_mappings.values()
        for value in aliases
    ]
    for field in ("required_param_mappings",):
        names.extend(
            value
            for aliases in _mapping_lists(body.get(field)).values()
            for value in aliases
        )
    columns = body.get("columns")
    values = columns if isinstance(columns, list) else list(columns) if isinstance(columns, dict) else []
    for value in values:
        if isinstance(value, dict):
            value = value.get("column_name") or value.get("name") or value.get("column") or value.get("field")
        text = str(value or "").strip()
        if text:
            names.append(text)
    return {_compact(value) for value in names if _compact(value)}


def _selection_text(value: Any) -> list[str]:
    if isinstance(value, list):
        return _string_list(value)
    if not isinstance(value, dict):
        return []
    return _unique_text([*_string_list(value.get("use_when")), *_string_list(value.get("rules"))])


def _tokens(value: Any) -> list[str]:
    separated = re.sub(
        r"(?<=[A-Za-z0-9_])(?=[가-힣])|(?<=[가-힣])(?=[A-Za-z0-9_])",
        " ",
        str(value or ""),
    )
    return [
        token
        for token in (_normalize_text(item) for item in re.findall(r"[가-힣A-Za-z0-9_]+", separated))
        if len(token) >= 2 and token not in STOP_TOKENS
    ]


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", _normalize_text(value))


def _normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _dedupe_references(values: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for item in values:
        marker = (item.get("kind"), _compact(item.get("input")), item.get("target"))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def _unique_text(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values if isinstance(values, (list, tuple, set)) else []:
        text = str(value or "").strip()
        if text and text not in seen and not _is_secret_key(text):
            seen.add(text)
            result.append(text)
    return result


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _is_secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold())
    return any(part in normalized for part in SECRET_KEY_PARTS)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(maximum, max(minimum, int(value)))
    except Exception:
        return default


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class MetadataAuthoringContextBuilderRev2(Component):
    display_name = "02 메타데이터 등록 Context 생성기 rev_2"
    description = "활성 metadata snapshot에서 원문과 관련된 계약 후보와 검증 registry를 만듭니다."
    inputs = [
        DataInput(name="payload", display_name="저장 요청 페이로드", required=True),
        DataInput(name="domain_items", display_name="도메인 메타데이터", required=True),
        DataInput(name="table_catalog_items", display_name="테이블 카탈로그", required=True),
        DataInput(name="main_flow_filters", display_name="메인 필터", required=True),
        MessageTextInput(name="max_candidates", display_name="종류별 최대 후보 수", value=str(DEFAULT_MAX_CANDIDATES), advanced=True),
        MessageTextInput(name="max_context_bytes", display_name="최대 LLM Context 바이트", value=str(DEFAULT_MAX_CONTEXT_BYTES), advanced=True),
    ]
    outputs = [
        Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="source_text", display_name="사용자 원문", method="build_source_text", types=["Message"], group_outputs=True),
        Output(name="metadata_context", display_name="등록 계약 후보", method="build_metadata_context", types=["Message"], group_outputs=True),
        Output(name="metadata_type", display_name="메타데이터 종류", method="build_metadata_type", types=["Message"], group_outputs=True),
    ]

    def _build_once(self) -> dict[str, Any]:
        cached = getattr(self, "_authoring_context_result", None)
        if isinstance(cached, dict):
            return cached
        result = build_authoring_context(
            getattr(self, "payload", None),
            getattr(self, "domain_items", None),
            getattr(self, "table_catalog_items", None),
            getattr(self, "main_flow_filters", None),
            getattr(self, "max_candidates", str(DEFAULT_MAX_CANDIDATES)),
            getattr(self, "max_context_bytes", str(DEFAULT_MAX_CONTEXT_BYTES)),
        )
        self._authoring_context_result = result
        return result

    def build_payload(self) -> Data:
        return Data(data=deepcopy(self._build_once()["payload"]))

    def build_source_text(self) -> Message:
        return Message(text=self._build_once()["source_text"])

    def build_metadata_context(self) -> Message:
        return Message(text=self._build_once()["metadata_context"])

    def build_metadata_type(self) -> Message:
        return Message(text=self._build_once()["metadata_type"])
