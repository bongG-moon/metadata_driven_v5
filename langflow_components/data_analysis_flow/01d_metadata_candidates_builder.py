# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01D 질문 기반 메타데이터 후보 생성기
# 역할: 도메인은 관련 항목 최대 20건, 테이블은 관련 후보 5건, 메인 필터는 전체를 32KB 안에서 선별합니다.
# 주요 입력: 질문 페이로드 (payload) · 필수, 도메인 메타데이터 (domain_items), 테이블 카탈로그 (table_catalog_items), 메인 변수
#        (main_flow_filters), 도메인 최대 후보 수 (max_domain_items), 테이블 최소 후보 수 (min_table_items), 테이블 최대 후보 수
#        (max_table_items), 최대 후보 바이트 (max_bytes)
# 주요 출력: 메타데이터 후보 (metadata_candidates)
# 처리 흐름: 질문 토큰으로 도메인·테이블을 각각 점수화하고, 테이블 최소 후보와 전체 메인 필터를 보장한 뒤 바이트 제한에 맞게 압축합니다.
# 유지보수 포인트: 도메인/테이블/메인 필터 quota는 서로 독립적이며, 테이블 최소 후보와 max_bytes 계약을 함께 지켜야 합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_MAX_DOMAIN_ITEMS = 20
DEFAULT_MIN_TABLE_ITEMS = 5
DEFAULT_MAX_TABLE_ITEMS = 5
DEFAULT_MAX_BYTES = 32 * 1024
DOMAIN_MIN_SCORE = 6
NON_RUNTIME_FUNCTION_CASE_MIN_SCORE = 12
MAX_NON_RUNTIME_FUNCTION_CASES = 2
MAX_AUTO_JOIN_RECIPE_DEPENDENCIES = 4
INTENT_SELECTION_HINT_MAX_PHRASES = 3
INTENT_SELECTION_HINT_MAX_METRICS = 8
INTENT_SELECTION_HINT_MAX_TEXT = 120

# ``section`` is a taxonomy shared by many unrelated records (for example every
# analysis recipe).  Treating it as a strong identity made the question token
# ``recipe`` score every item in ``analysis_recipes`` equally, which could push
# an exact registered alias out of the bounded candidate view.  The section is
# still present in the weak full-body score and in the stable reference key.
TECHNICAL_IDENTITY_KEYS = ("key", "dataset_key", "filter_key", "function_name")
LABEL_KEYS = ("display_name", "label")
STRUCTURED_SEARCH_KEYS = {
    "aliases",
    "processes",
    "applies_when",
    "apply_conditions",
    "columns",
    "column_candidates",
    "group_by",
    "grain_columns",
    "product_key_columns",
    "comparison_columns",
    "metrics",
    "metric_columns",
    "metric_semantics",
    "temporal_semantics",
    "quantity_columns",
    "join_keys",
    "required_params",
    "semantic_role",
}
CANONICAL_MAPPING_KEYS = {
    "condition",
    "conditions",
    "condition_by_family",
    "condition_by_dataset",
    "filters",
    "processes",
    "process_groups",
    "members",
    "temporal_semantics",
}
DOMAIN_FILTER_CONTAINER_KEYS = {
    "condition",
    "conditions",
    "condition_by_family",
    "condition_by_dataset",
}
LEGACY_NOT_BLANK_OPERATORS = {
    "is_not_null_or_empty",
    "is_not_null_and_not_empty",
    "not_null_or_empty",
    "not_null_and_not_empty",
}
FILTER_OPERATOR_ALIASES = {
    ">": "gt",
    ">=": "ge",
    "gte": "ge",
    "greater_than": "gt",
    "greater_than_or_equal": "ge",
    "<": "lt",
    "<=": "le",
    "lte": "le",
    "less_than": "lt",
    "less_than_or_equal": "le",
    "startswith": "starts_with",
    "endswith": "ends_with",
    "isnull": "is_null",
    "isempty": "is_empty",
    "is_null_or_empty": "null_or_empty",
    "null_or_empty": "null_or_empty",
}
DEPRECATED_TABLE_CATALOG_KEYS = {"row_identity_columns", "context_columns"}
KOREAN_SUFFIXES = (
    "조회해주세요",
    "알려주세요",
    "보여주세요",
    "조회해줘",
    "알려줘",
    "보여줘",
    "으로부터",
    "에게서",
    "에서는",
    "으로",
    "에서",
    "에게",
    "한테",
    "까지",
    "부터",
    "처럼",
    "보다",
    "하고",
    "이며",
    "이고",
    "이랑",
    "현황",
    "된",
    "들",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "와",
    "과",
    "도",
    "만",
    "로",
    "랑",
    "별",
)
TOKEN_EXPANSIONS = {
    "오늘": ("today",),
    "금일": ("today",),
    "현재": ("today",),
    "현시간": ("today",),
    "어제": ("history",),
    "전일": ("history",),
    "장비": ("equipment", "equip", "eqp"),
    "설비": ("equipment", "equip", "eqp"),
    "배정": ("assign", "assignment"),
    "모델": ("model",),
    "생산": ("production",),
    "생산량": ("production",),
    "실적": ("production",),
    "재공": ("wip",),
    "재공수량": ("wip",),
    "계획": ("target", "plan"),
    "목표": ("target",),
    "제품": ("product",),
    "레시피": ("recipe",),
    "로트": ("lot",),
    "랏": ("lot",),
    "홀드": ("hold",),
    "보류": ("hold",),
}
GENERIC_SEMANTIC_TOKENS = {
    "공정",
    "제품",
    "수량",
    "물량",
    "데이터",
    "분석",
    "합계",
    "전체",
    "장비",
    "모델",
    "생산",
    "실적",
    "재공",
    "계획",
    "목표",
}
GENERIC_CANONICAL_ALIAS_TOKENS = GENERIC_SEMANTIC_TOKENS | {
    "product",
    "production",
    "output",
    "wip",
    "equipment",
    "equip",
    "eqp",
    "model",
    "target",
    "plan",
    "input",
    "lot",
}
RUNTIME_FUNCTION_HELPERS = [
    {
        "function_name": "match_product_tokens",
        "selection_policy": "product_token_only",
        "selectable_for_intent": True,
        "description": "제품 속성 token 묶음을 실제 조회 DataFrame row와 매칭할 때만 사용한다.",
    },
    {
        "function_name": "filter_ordered_range",
        "selection_policy": "ordered_range_only",
        "selectable_for_intent": True,
        "description": "두 label 끝점의 숫자 order 최소·최대 사이를 포함 범위로 필터링할 때만 사용한다.",
    },
    {
        "function_name": "sample_passthrough_helper",
        "selection_policy": "demo_only",
        "selectable_for_intent": False,
        "description": "helper 전달 형식 확인용이며 실제 분석에서는 선택하지 않는다.",
    },
]

PRUNED_METADATA_KEYS = {
    "_id",
    "registration_trace",
    "raw_trace",
    "raw_text",
    "raw_text_preview",
    "refined_text",
    "review",
    "write_result",
    "llm_response",
    "existing_matches",
    "duplicate_decision",
    "created_by_prompt",
    "created_at",
    "updated_at",
    "text",
}

UNTRUSTED_PROMPT_CONFIG_KEYS = {
    "query_template",
    "sql_template",
    "oracle_sql",
    "sql",
    "query",
    "endpoint",
    "url",
    "api_url",
    "headers",
    "credential",
    "credentials",
    "password",
    "token",
    "api_key",
}


# 주요 함수: 질문과 세 종류의 메타데이터에서 관련 후보를 독립 정책으로 선택합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_metadata_candidates(
    payload_value: Any = None,
    domain_items_value: Any = None,
    table_catalog_items_value: Any = None,
    main_flow_filters_value: Any = None,
    *,
    max_domain_items: Any = DEFAULT_MAX_DOMAIN_ITEMS,
    min_table_items: Any = DEFAULT_MIN_TABLE_ITEMS,
    max_table_items: Any = DEFAULT_MAX_TABLE_ITEMS,
    max_bytes: Any = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    if _looks_like_legacy_metadata_call(payload_value, domain_items_value, table_catalog_items_value, main_flow_filters_value):
        main_flow_filters_value = table_catalog_items_value
        table_catalog_items_value = domain_items_value
        domain_items_value = payload_value
        payload_value = None
    payload = _payload(payload_value)
    question = str(_dict(payload.get("request")).get("question") or "").strip()
    state = _dict(payload.get("state"))
    followup_hint = _dict(payload.get("followup_hint"))
    search_parts = [question]
    if followup_hint.get("followup_candidate") is True:
        search_parts.extend((str(state.get("last_question") or ""), _compact_state_terms(state)))
    search_text = " ".join(item for item in search_parts if item)

    domain_items, domain_load = _extract(domain_items_value, "domain_items")
    table_items, table_load = _extract(table_catalog_items_value, "table_catalog_items")
    filter_items, filter_load = _extract(main_flow_filters_value, "main_flow_filters")

    domain_items = _annotate_runtime_function_cases(_sanitize_items(domain_items, "domain"))
    table_items = _sanitize_items(table_items, "table_catalog")
    filter_items = _sanitize_items(filter_items, "main_flow_filter")

    domain_limit = _bounded_int(max_domain_items, DEFAULT_MAX_DOMAIN_ITEMS, 1, 50)
    table_minimum = _bounded_int(min_table_items, DEFAULT_MIN_TABLE_ITEMS, 1, 50)
    table_limit = _bounded_int(max_table_items, DEFAULT_MAX_TABLE_ITEMS, table_minimum, 50)
    byte_limit = _bounded_int(max_bytes, DEFAULT_MAX_BYTES, 4096, 64 * 1024)
    selected, selection_stats = _select_candidates(
        search_text,
        domain_items,
        table_items,
        filter_items,
        domain_limit,
        table_minimum,
        table_limit,
    )
    selected["domain_items"] = _annotate_process_detail_matches(
        selected["domain_items"],
        question,
    )
    # Table Catalog의 selection_criteria는 저장 계약 자체이지만, 후보가 여러 개일
    # 때에는 약한 모델이 이 정보를 단순 본문으로만 읽고 놓칠 수 있습니다. 후보
    # 객체에 작은 선택 힌트를 붙여 intent prompt가 사용할 수 있게 하되, dataset을
    # 고정하거나 실행 계약을 바꾸지는 않습니다.
    selected["table_catalog_items"] = _annotate_table_intent_selection_hints(
        selected["table_catalog_items"],
        question,
    )
    candidates = {
        "domain_items": selected["domain_items"],
        "table_catalog_items": selected["table_catalog_items"],
        "main_flow_filters": selected["main_flow_filters"],
        "runtime_function_helpers": deepcopy(RUNTIME_FUNCTION_HELPERS),
    }
    selected_counts_before_bytes = {
        key: len(value)
        for key, value in candidates.items()
        if isinstance(value, list)
    }
    # The model receives a bounded view, but a Domain which is matched by an
    # exact worker-written alias has already proved that its source is needed
    # for this request.  Preserve that source while trimming prompt bytes.  We
    # do not raise the byte cap or add prompt text; lower-priority candidates
    # are removed first instead.
    protected_table_dataset_keys = {
        str(value).strip().casefold()
        for value in selection_stats.get("domain_dataset_dependencies", {}).get(
            "included_dataset_keys", []
        )
        if str(value or "").strip()
    }
    protected_domain_identities = {
        str(item.get("identity") or "")
        for item in selection_stats.get("protected_domain_candidates", [])
        if str(item.get("identity") or "")
    }
    candidates, byte_fit = _fit_bytes(
        candidates,
        byte_limit,
        table_minimum,
        protected_table_dataset_keys=protected_table_dataset_keys,
        protected_domain_identities=protected_domain_identities,
    )

    retained_domain_identities = {
        _stable_identity(item)
        for item in _list(candidates.get("domain_items"))
        if isinstance(item, dict)
    }
    protected_domain_trace = [
        {
            "section": str(item.get("section") or ""),
            "key": str(item.get("key") or ""),
            "matched_aliases": list(item.get("matched_aliases") or []),
            "retained_after_byte_fit": str(item.get("identity") or "")
            in retained_domain_identities,
        }
        for item in selection_stats.get("protected_domain_candidates", [])
        if isinstance(item, dict)
    ]

    loads = {
        "domain_items": domain_load,
        "table_catalog_items": table_load,
        "main_flow_filters": filter_load,
    }
    errors = [
        deepcopy(error)
        for load in loads.values()
        if isinstance(load, dict)
        for error in load.get("errors", [])
        if isinstance(error, dict)
    ]
    table_floor = min(table_minimum, len(table_items))
    policy_preserved = {
        "table_minimum": len(candidates["table_catalog_items"]) >= table_floor,
        "main_filters_complete": len(candidates["main_flow_filters"]) == len(filter_items),
    }
    policy_warnings = []
    if not policy_preserved["table_minimum"]:
        policy_warnings.append(
            {
                "type": "table_minimum_unmet_due_to_byte_cap",
                "message": "전체 바이트 상한 때문에 테이블 카탈로그 최소 후보 수를 유지하지 못했습니다.",
            }
        )
    if not policy_preserved["main_filters_complete"]:
        policy_warnings.append(
            {
                "type": "main_filters_truncated_due_to_byte_cap",
                "message": "전체 바이트 상한 때문에 메인 필터 일부가 제거되었습니다.",
            }
        )

    execution_catalog_registry = _table_catalog_execution_registry(table_items)
    return {
        "metadata_candidates": candidates,
        # LLM에는 위의 제한된 후보만 전달한다. 반면 실행 단계는 후보 밖의
        # 정상 등록 dataset을 '미등록'으로 오판하면 안 되므로, 전체 활성
        # Catalog는 모델 비노출 registry로 별도 보존한다.
        "table_catalog_registry": execution_catalog_registry,
        "metadata_load": {
            "status": _combined_status(loads),
            "loaded_counts": {
                "domain_items": len(domain_items),
                "table_catalog_items": len(table_items),
                "main_flow_filters": len(filter_items),
            },
            "counts": {
                "domain_items": len(domain_items),
                "table_catalog_items": len(table_items),
                "main_flow_filters": len(filter_items),
            },
            "registered_dataset_count": len(execution_catalog_registry.get("dataset_keys", [])),
            "selected_counts": {
                key: len(value)
                for key, value in candidates.items()
                if isinstance(value, list)
            },
            "selected_counts_before_bytes": selected_counts_before_bytes,
            "matched_counts": selection_stats["matched_counts"],
            "temporal_family_companions": selection_stats.get(
                "temporal_family_companions", {}
            ),
            "domain_dataset_dependencies": selection_stats.get(
                "domain_dataset_dependencies", {}
            ),
            "auto_join_recipe_dependencies": selection_stats.get(
                "auto_join_recipe_dependencies", []
            ),
            "protected_domain_candidates": protected_domain_trace,
            "candidate_bytes_by_pool": {
                key: _json_bytes(value)
                for key, value in candidates.items()
                if isinstance(value, list)
            },
            "candidate_bytes": _json_bytes(candidates),
            "selection_policy": {
                "domain_items": {"mode": "relevant_only", "max_items": domain_limit},
                "table_catalog_items": {
                    "mode": "relevant_with_minimum",
                    "min_items": table_minimum,
                    "max_items": table_limit,
                },
                "main_flow_filters": {"mode": "all_relevant_first"},
            },
            "max_bytes": byte_limit,
            "truncated_by_bytes": byte_fit["truncated"],
            "byte_trimmed_counts": byte_fit["trimmed_counts"],
            "policy_preserved": policy_preserved,
            "warnings": policy_warnings,
            "loads": loads,
            "errors": errors,
        },
    }


# 함수 설명: `_table_catalog_execution_registry()`는 LLM 후보 축소와 별개로 전체 활성 Catalog의
# 실행 권한·정확한 문서 보강에만 쓰는 비노출 registry를 만듭니다.
def _table_catalog_execution_registry(table_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep authoritative active Catalog entries out of the model candidate view."""

    items: list[dict[str, Any]] = []
    dataset_keys: list[str] = []
    seen: set[str] = set()
    for item in table_items:
        if not isinstance(item, dict):
            continue
        dataset_key = _table_catalog_dataset_key(item)
        normalized = dataset_key.casefold()
        if not dataset_key or normalized in seen:
            continue
        seen.add(normalized)
        dataset_keys.append(dataset_key)
        items.append(deepcopy(item))
    return {"dataset_keys": dataset_keys, "items": items}


# 함수 설명: wrapper와 payload 어느 쪽에 저장되어도 Table Catalog의 dataset_key를 읽습니다.
def _table_catalog_dataset_key(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
    return str(
        item.get("dataset_key")
        or item.get("key")
        or payload.get("dataset_key")
        or payload.get("key")
        or ""
    ).strip()


# 함수 설명: `_select_candidates()`는 조건과 우선순위에 맞는 후보만 골라 원래 순서를 유지해 반환합니다.
def _select_candidates(
    search_text: str,
    domain_items: list[dict[str, Any]],
    table_items: list[dict[str, Any]],
    filter_items: list[dict[str, Any]],
    max_domain_items: int,
    min_table_items: int,
    max_table_items: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    tokens = _tokens(search_text)
    ranked = {
        "domain_items": _rank_entries(domain_items, tokens),
        "table_catalog_items": _rank_entries(table_items, tokens),
        "main_flow_filters": _rank_entries(filter_items, tokens),
    }

    selected_domain: list[dict[str, Any]] = []
    selected_domain_ids: set[str] = set()
    non_runtime_function_cases = 0
    direct_phrase_matches = _question_matched_domain_phrases(
        search_text,
        domain_items,
    )
    dependency_matches = _question_matched_dataset_domains(
        search_text,
        domain_items,
    )
    # A domain that explicitly names its backing dataset is an execution
    # dependency, not merely prompt context. Keep exact registered alias
    # matches ahead of the relevance quota. Preserve this established priority
    # before applying the new direct-phrase rescue so an existing executable
    # dependency is never displaced by the additive candidate protection.
    for item in dependency_matches:
        identity = _stable_identity(item)
        if identity in selected_domain_ids:
            continue
        selected_domain.append(item)
        selected_domain_ids.add(identity)
        if len(selected_domain) >= max_domain_items:
            break
    # A worker-written alias/display name that occurs verbatim in the question
    # is stronger evidence than taxonomy or body keyword overlap.  Add only
    # non-generic phrases here: a lone alias such as ``제품``/``장비``/``LOT``
    # must not activate or protect an unrelated business rule.
    for match in direct_phrase_matches:
        if len(selected_domain) >= max_domain_items:
            break
        item = match["item"]
        identity = _stable_identity(item)
        if identity in selected_domain_ids:
            continue
        selected_domain.append(item)
        selected_domain_ids.add(identity)
    # canonical condition/process mapping과 직접 alias가 함께 일치한 후보를 먼저 보존합니다.
    # 이후 일반 점수 후보로 남은 자리를 채우므로 max_domain_items를 늘리지 않아도 실행 조건이 유실되지 않습니다.
    for _, _, _, _, item in ranked["domain_items"]:
        if len(selected_domain) >= max_domain_items:
            break
        if _canonical_alias_match_count(item, tokens) < 1:
            continue
        identity = _stable_identity(item)
        if identity in selected_domain_ids:
            continue
        selected_domain.append(item)
        selected_domain_ids.add(identity)
        if len(selected_domain) >= max_domain_items:
            break

    # A calculation recipe can own a two-source analysis while a separate
    # registered recipe owns the executable row-enrichment join.  Keep that
    # join recipe in the bounded prompt view when (and only when) both recipes
    # declare the same exact two-dataset pair.  This is metadata dependency
    # closure, not keyword expansion: no unrelated recipe is promoted and the
    # ordinary quota remains in force.
    auto_join_recipe_dependencies = _join_recipe_dependencies_for_selected_domains(
        selected_domain,
        domain_items,
    )
    for item in auto_join_recipe_dependencies:
        if len(selected_domain) >= max_domain_items:
            break
        identity = _stable_identity(item)
        if identity in selected_domain_ids:
            continue
        selected_domain.append(item)
        selected_domain_ids.add(identity)

    for score, strong_hits, _, _, item in ranked["domain_items"]:
        if len(selected_domain) >= max_domain_items:
            break
        identity = _stable_identity(item)
        if identity in selected_domain_ids:
            continue
        if strong_hits < 1 or score < DOMAIN_MIN_SCORE:
            continue
        if _is_non_runtime_function_case(item):
            if score < NON_RUNTIME_FUNCTION_CASE_MIN_SCORE or non_runtime_function_cases >= MAX_NON_RUNTIME_FUNCTION_CASES:
                continue
            non_runtime_function_cases += 1
        selected_domain.append(item)
        selected_domain_ids.add(identity)
        if len(selected_domain) >= max_domain_items:
            break

    table_related_count = sum(1 for _, strong_hits, _, _, _ in ranked["table_catalog_items"] if strong_hits > 0)
    table_target = min(
        len(table_items),
        max_table_items,
        max(min_table_items, min(table_related_count, max_table_items)),
    )
    # A domain can name an exact ``dataset_key`` (for example ``target``), or
    # it can describe a reusable data family (for example ``equipment``).  The
    # latter is candidate guidance only: keep every active catalog in that
    # family visible to the intent model, then let the normalizer validate the
    # final schema/metric plan.  Treating a family name as a missing exact key
    # used to evict valid catalogs from the five-item model view.
    domain_dataset_resolution = _resolve_domain_catalog_references(
        selected_domain,
        [entry[4] for entry in ranked["table_catalog_items"]],
    )
    exact_dataset_keys = set(domain_dataset_resolution["exact_dataset_keys"])
    family_dataset_keys = set(domain_dataset_resolution["family_dataset_keys"])
    required_tables = [
        entry[4]
        for entry in ranked["table_catalog_items"]
        if _table_catalog_dataset_key(entry[4]).casefold() in exact_dataset_keys
    ]
    required_tables.extend(
        entry[4]
        for entry in ranked["table_catalog_items"]
        if _table_catalog_dataset_key(entry[4]).casefold() in family_dataset_keys
        and _table_catalog_dataset_key(entry[4]).casefold() not in exact_dataset_keys
    )
    initial_tables: list[dict[str, Any]] = []
    initial_table_ids: set[str] = set()
    for item in [*required_tables, *[entry[4] for entry in ranked["table_catalog_items"][:table_target]]]:
        identity = _stable_identity(item)
        if identity in initial_table_ids:
            continue
        initial_tables.append(item)
        initial_table_ids.add(identity)
    selected_tables, temporal_companion_stats = _select_temporal_family_companions(
        initial_tables,
        [entry[4] for entry in ranked["table_catalog_items"]],
        max_table_items,
        protected_items=required_tables,
    )
    selected = {
        "domain_items": selected_domain,
        "table_catalog_items": selected_tables,
        "main_flow_filters": [entry[4] for entry in ranked["main_flow_filters"]],
    }
    protected_domain_candidates = [
        {
            "identity": _stable_identity(match["item"]),
            "section": str(match["item"].get("section") or ""),
            "key": str(match["item"].get("key") or ""),
            "matched_aliases": list(match["matched_aliases"]),
        }
        for match in direct_phrase_matches
        if _stable_identity(match["item"]) in selected_domain_ids
    ]
    return selected, {
        "matched_counts": {
            key: sum(1 for _, strong_hits, _, _, _ in values if strong_hits > 0)
            for key, values in ranked.items()
        },
        "temporal_family_companions": temporal_companion_stats,
        "domain_dataset_dependencies": {
            "dataset_keys": domain_dataset_resolution["raw_references"],
            "exact_dataset_keys": domain_dataset_resolution["exact_dataset_keys"],
            "family_reference_values": domain_dataset_resolution["family_reference_values"],
            "family_included_dataset_keys": domain_dataset_resolution["family_included_dataset_keys"],
            "unresolved_reference_values": domain_dataset_resolution["unresolved_reference_values"],
            "included_dataset_keys": sorted(
                _table_catalog_dataset_key(item) for item in required_tables
            ),
            "matched_domain_keys": [
                f"{item.get('section')}:{item.get('key')}"
                for item in dependency_matches
            ],
        },
        "auto_join_recipe_dependencies": [
            {
                "section": str(item.get("section") or ""),
                "key": str(item.get("key") or ""),
            }
            for item in auto_join_recipe_dependencies
            if _stable_identity(item) in selected_domain_ids
        ],
        "protected_domain_candidates": protected_domain_candidates,
    }


# 함수 설명: 선택된 분석 recipe의 정확한 두 source pair를 실행하는 join recipe만 후보 의존성으로 보강합니다.
def _join_recipe_dependencies_for_selected_domains(
    selected_items: list[dict[str, Any]],
    all_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return bounded, exact-pair join recipe dependencies in stable order.

    A generic source-pair relation such as ``equipment_assign`` + ``eqp_uph``
    is useful only after another selected recipe has explicitly declared that
    same pair.  We do not infer joins from table families, field names, or a
    question token.  This keeps the candidate builder permissive for existing
    requests while exposing the reusable contract needed by a downstream
    normalizer when a model emits an impossible join key.
    """

    selected_ids = {
        _stable_identity(item)
        for item in selected_items
        if isinstance(item, dict)
    }
    selected_pairs = {
        _domain_two_source_pair(item)
        for item in selected_items
        if isinstance(item, dict) and _domain_two_source_pair(item)
    }
    if not selected_pairs:
        return []

    result: list[dict[str, Any]] = []
    for item in all_items:
        if not isinstance(item, dict):
            continue
        if _stable_identity(item) in selected_ids:
            continue
        if str(item.get("section") or "").strip() != "analysis_recipes":
            continue
        payload = _dict(item.get("payload"))
        if _domain_item_is_explicitly_inactive(item):
            continue
        if not _list(payload.get("join_keys")):
            continue
        pair = _domain_two_source_pair(item)
        if not pair or pair not in selected_pairs:
            continue
        result.append(item)
        if len(result) >= MAX_AUTO_JOIN_RECIPE_DEPENDENCIES:
            break
    return result


# 함수 설명: source_datasets 또는 legacy dataset_keys에서 순서와 무관한 정확한 두 dataset pair를 읽습니다.
def _domain_two_source_pair(item: dict[str, Any]) -> tuple[str, str] | tuple[()]:
    payload = _dict(item.get("payload"))
    values = _list(payload.get("source_datasets")) or _list(
        payload.get("dataset_keys")
    )
    datasets = [str(value or "").strip() for value in values if str(value or "").strip()]
    if len(datasets) != 2 or datasets[0].casefold() == datasets[1].casefold():
        return ()
    return tuple(sorted((datasets[0].casefold(), datasets[1].casefold())))


# 함수 설명: 비활성 상태의 domain은 후보 의존성으로 다시 살리지 않습니다.
def _domain_item_is_explicitly_inactive(item: dict[str, Any]) -> bool:
    payload = _dict(item.get("payload"))
    status = str(item.get("status") or payload.get("status") or "").strip().casefold()
    return bool(
        item.get("is_active") is False
        or payload.get("is_active") is False
        or status in {"inactive", "disabled", "deleted", "archived", "draft"}
    )


# 함수 설명: `_domain_dataset_references()`는 데이터셋·references 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
# 함수 설명: `_question_matched_dataset_domains()`는 질문에 직접 언급된 등록 alias 중 dataset 의존성이 있는 Domain을 우선 선택합니다.
def _question_matched_dataset_domains(
    question: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return exact registered domain aliases that carry dataset dependencies."""
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for item in items:
        if not _domain_dataset_references([item]):
            continue
        payload = _dict(item.get("payload"))
        phrases = _list(payload.get("aliases"))
        phrases.extend(
            value
            for value in (
                payload.get("display_name"),
                item.get("display_name"),
                item.get("key"),
            )
            if value not in (None, "")
        )
        matched_lengths = [
            len(_normalized_match_text(phrase))
            for phrase in phrases
            if _registered_phrase_matches(question, str(phrase or ""))
        ]
        if matched_lengths:
            matches.append((max(matched_lengths), _stable_identity(item), item))
    matches.sort(key=lambda value: (-value[0], value[1]))
    return [item for _, _, item in matches]


# 함수 설명: `_question_matched_domain_phrases()`는 질문에 직접 등장한 비일반 등록 alias/display name을 찾습니다.
def _question_matched_domain_phrases(
    question: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return exact, non-generic Domain phrases together with their items."""

    matches: list[tuple[int, str, dict[str, Any], list[str]]] = []
    for item in items:
        # A Function Case explicitly marked as non-selectable may remain in
        # the ordinary ranked pool for legacy compatibility, but an exact
        # alias must not promote it ahead of normal Domain candidates or make
        # it byte-trim protected.
        if _is_non_runtime_function_case(item):
            continue
        payload = _dict(item.get("payload"))
        phrases = _list(payload.get("aliases"))
        phrases.extend(
            value
            for value in (
                payload.get("display_name"),
                item.get("display_name"),
            )
            if value not in (None, "")
        )
        matched_aliases: list[str] = []
        seen: set[str] = set()
        for phrase in phrases:
            phrase_text = str(phrase or "").strip()
            normalized = _normalized_match_text(phrase_text)
            if (
                not normalized
                or normalized in seen
                or not _protectable_registered_phrase(phrase_text)
                or not _registered_phrase_matches(question, phrase_text)
            ):
                continue
            seen.add(normalized)
            matched_aliases.append(phrase_text)
        if matched_aliases:
            matches.append(
                (
                    max(len(_normalized_match_text(value)) for value in matched_aliases),
                    _stable_identity(item),
                    item,
                    matched_aliases,
                )
            )
    # A phrase shared by multiple Domain documents is still useful for normal
    # relevance scoring, but it is not strong enough to receive protected
    # quota/byte priority.  Protect only phrases that identify one item in the
    # current metadata view so common terms such as ``UPH`` cannot crowd out
    # established execution dependencies.
    phrase_owner_counts: dict[str, int] = {}
    for _, _, _, matched_aliases in matches:
        for value in {
            _normalized_match_text(alias)
            for alias in matched_aliases
            if _normalized_match_text(alias)
        }:
            phrase_owner_counts[value] = phrase_owner_counts.get(value, 0) + 1
    unique_matches: list[tuple[int, str, dict[str, Any], list[str]]] = []
    for _, identity, item, matched_aliases in matches:
        unique_aliases = [
            alias
            for alias in matched_aliases
            if phrase_owner_counts.get(_normalized_match_text(alias), 0) == 1
        ]
        if not unique_aliases:
            continue
        unique_matches.append(
            (
                max(len(_normalized_match_text(value)) for value in unique_aliases),
                identity,
                item,
                unique_aliases,
            )
        )
    unique_matches.sort(key=lambda value: (-value[0], value[1]))
    return [
        {"item": item, "matched_aliases": matched_aliases}
        for _, _, item, matched_aliases in unique_matches
    ]


# 함수 설명: `_protectable_registered_phrase()`는 일반 단일 entity alias가 후보 보호 신호가 되지 않게 합니다.
def _protectable_registered_phrase(phrase: str) -> bool:
    normalized = _normalized_match_text(phrase)
    if not normalized:
        return False
    generic_phrases = {
        _normalized_match_text(value)
        for value in GENERIC_CANONICAL_ALIAS_TOKENS
        if _normalized_match_text(value)
    }
    if normalized in generic_phrases:
        return False
    lexical_tokens = re.findall(r"[0-9A-Za-z가-힣_]+", str(phrase or "").casefold())
    if len(lexical_tokens) == 1:
        variants = set(_token_variants(lexical_tokens[0]))
        generic_variants = variants.intersection(GENERIC_CANONICAL_ALIAS_TOKENS)
        non_generic_variants = variants.difference(GENERIC_CANONICAL_ALIAS_TOKENS)
        # Korean suffix forms such as ``제품별`` retain the original surface
        # token alongside the generic stem (``제품``/``product``).  The surface
        # form alone does not make the phrase specific.  A second meaningful
        # token (for example ``제품UPH`` -> ``uph``) still remains protectable.
        if generic_variants and non_generic_variants.issubset({lexical_tokens[0]}):
            return False
    return True


# 함수 설명: `_registered_phrase_matches()`는 등록된 표현이 질문에 경계를 지켜 포함되는지 판정합니다.
def _registered_phrase_matches(question: str, phrase: str) -> bool:
    target = _normalized_match_text(phrase)
    source = _normalized_match_text(question)
    if not target or not source:
        return False
    if target.isascii() and target.replace("_", "").isalnum() and len(target) <= 3:
        return target in re.findall(r"[a-z0-9_]+", source)
    if target in source:
        return True

    # 등록 alias의 단어가 질문에서 복수형·조사·조회 표현과 결합되어도 동일한
    # 의미 표현으로 회수한다. 단일 token은 기존 exact 정책을 유지하고, 두 단어
    # 이상의 alias가 모두 일치할 때만 허용하여 일반 명사 하나가 규칙을 과도하게
    # 활성화하지 않게 한다.
    phrase_tokens = re.findall(r"[0-9A-Za-z가-힣_]+", str(phrase or "").casefold())
    # Slash로 구분된 짧은 공정 코드는 ``D/C공정`` -> ``D`` + ``C공정``처럼
    # 분리된다. 한 글자 token을 느슨하게 비교하면 질문의 ``D/A공정``이 다른
    # D 계열 공정 alias까지 활성화할 수 있으므로 이 경우에는 위 exact compact
    # 비교만 허용한다.
    if len(phrase_tokens) < 2 or any(len(token) < 2 for token in phrase_tokens):
        return False
    question_variants = {
        variant
        for token in re.findall(r"[0-9A-Za-z가-힣_]+", str(question or "").casefold())
        for variant in _token_variants(token)
    }
    if not question_variants:
        return False
    return all(
        any(variant in question_variants for variant in _token_variants(token))
        for token in phrase_tokens
    )


# 함수 설명: `_normalized_match_text()`는 공백과 구분자 차이를 제거해 등록 표현 비교용 문자열을 만듭니다.
def _normalized_match_text(value: Any) -> str:
    return re.sub(r"[\s/\\._-]+", "", str(value or "").casefold())


# 함수 설명: `_domain_dataset_references()`는 Domain payload에 선언된 dataset 의존성 키를 재귀적으로 수집합니다.
def _domain_dataset_references(items: list[dict[str, Any]]) -> set[str]:
    reference_keys = {
        "data_source",
        "dataset_key",
        "dataset_keys",
        "dataset_ref",
        "dataset_refs",
        "source_dataset",
        "source_datasets",
        "target_dataset",
    }
    result: set[str] = set()

    # 함수 설명: `visit()`는 01D 질문 기반 메타데이터 후보 생성기 처리 중 visit 관련 값을 계산·변환하는 내부 helper입니다.
    def visit(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if key_text in reference_keys:
                    raw_values = item if isinstance(item, list) else [item]
                    for raw in raw_values:
                        if isinstance(raw, dict):
                            raw = raw.get("dataset_key") or raw.get("key")
                        text = str(raw or "").strip()
                        if text:
                            result.add(text)
                elif isinstance(item, (dict, list)):
                    visit(item, key_text)
        elif isinstance(value, list):
            for item in value:
                visit(item, parent_key)

    for item in items:
        visit(_dict(item.get("payload")))
    return result


# 함수 설명: `_resolve_domain_catalog_references()`는 Domain의 dataset key와 dataset family 힌트를
# 구분해, 실제 key는 직접 보호하고 family는 후보 Catalog 묶음으로만 확장합니다.
def _resolve_domain_catalog_references(
    items: list[dict[str, Any]],
    table_items: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Resolve domain references without turning a family hint into a forced dataset.

    ``data_source`` is intentionally allowed to contain either a dataset key
    or a reusable family label.  Exact keys remain the strongest signal.  A
    non-key label is matched only against registered ``dataset_family`` values
    and returns every matching catalog as an LLM candidate; it never selects a
    single dataset or changes the eventual execution plan.
    """

    raw_references = sorted(
        {
            str(value or "").strip()
            for value in _domain_dataset_references(items)
            if str(value or "").strip()
        },
        key=str.casefold,
    )
    by_key = {
        _table_catalog_dataset_key(item).casefold(): _table_catalog_dataset_key(item)
        for item in table_items
        if _table_catalog_dataset_key(item)
    }
    by_family: dict[str, list[str]] = {}
    for item in table_items:
        dataset_key = _table_catalog_dataset_key(item)
        family = _table_dataset_family(item).casefold()
        if dataset_key and family:
            by_family.setdefault(family, []).append(dataset_key)

    exact_dataset_keys: list[str] = []
    family_reference_values: list[str] = []
    family_included_dataset_keys: list[str] = []
    unresolved_reference_values: list[str] = []
    for reference in raw_references:
        normalized = reference.casefold()
        exact = by_key.get(normalized)
        if exact:
            exact_dataset_keys.append(exact)
            continue
        family_members = by_family.get(normalized, [])
        if family_members:
            family_reference_values.append(reference)
            for dataset_key in family_members:
                if dataset_key not in family_included_dataset_keys:
                    family_included_dataset_keys.append(dataset_key)
            continue
        unresolved_reference_values.append(reference)

    return {
        "raw_references": raw_references,
        "exact_dataset_keys": exact_dataset_keys,
        "family_reference_values": family_reference_values,
        "family_included_dataset_keys": family_included_dataset_keys,
        "family_dataset_keys": family_included_dataset_keys,
        "unresolved_reference_values": unresolved_reference_values,
    }


# 함수 설명: `_table_time_scope()`는 TIME·분석 범위을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _table_time_scope(item: dict[str, Any]) -> str:
    payload = _dict(item.get("payload"))
    criteria = _dict(payload.get("selection_criteria"))
    return str(criteria.get("time_scope") or payload.get("time_scope") or "").strip().lower()


# 함수 설명: `_table_dataset_family()`는 데이터셋·데이터셋 분류을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _table_dataset_family(item: dict[str, Any]) -> str:
    return str(_dict(item.get("payload")).get("dataset_family") or "").strip()


# 함수 설명: `_select_temporal_family_companions()`는 조건과 우선순위에 맞는 temporal·데이터셋 분류·companions만 골라 원래 순서를 유지해 반환합니다.
def _select_temporal_family_companions(
    initially_selected: list[dict[str, Any]],
    ranked_items: list[dict[str, Any]],
    max_items: int,
    *,
    protected_items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep execution-proven catalogs before expanding temporal siblings.

    A bounded candidate view can contain both an exact Domain dependency and a
    family with multiple time-scope variants.  The exact dependency is needed
    to make the intended source visible to the model, whereas a sibling is
    only disambiguation guidance.  Preserve the former first; otherwise the
    current/history pair can consume the entire quota and silently evict a
    required second source from a valid multi-source question.
    """
    members_by_family: dict[str, list[dict[str, Any]]] = {}
    scopes_by_family: dict[str, set[str]] = {}
    for item in ranked_items:
        family = _table_dataset_family(item)
        scope = _table_time_scope(item)
        if not family or not scope:
            continue
        members_by_family.setdefault(family, []).append(item)
        scopes_by_family.setdefault(family, set()).add(scope)
    temporal_families = {
        family for family, scopes in scopes_by_family.items() if len(scopes) >= 2
    }
    selected_families = {
        _table_dataset_family(item) for item in initially_selected
    }
    expanded_families = temporal_families.intersection(selected_families)
    required_ids = {
        _stable_identity(item)
        for family in expanded_families
        for item in members_by_family.get(family, [])
    }
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    protected = [item for item in (protected_items or []) if isinstance(item, dict)]
    protected_ids = {_stable_identity(item) for item in protected}
    for item in protected:
        identity = _stable_identity(item)
        if identity in selected_ids or len(selected) >= max_items:
            continue
        selected.append(item)
        selected_ids.add(identity)
    for item in ranked_items:
        identity = _stable_identity(item)
        if identity not in required_ids or identity in selected_ids or len(selected) >= max_items:
            continue
        selected.append(item)
        selected_ids.add(identity)
    for item in initially_selected:
        identity = _stable_identity(item)
        if identity in selected_ids or len(selected) >= max_items:
            continue
        selected.append(item)
        selected_ids.add(identity)
    added_ids = selected_ids.difference(
        {_stable_identity(item) for item in initially_selected}
    )
    return selected[:max_items], {
        "status": "expanded" if added_ids else "not_needed",
        "expanded_families": sorted(expanded_families),
        "added_dataset_keys": sorted(
            str(item.get("dataset_key") or "")
            for item in selected
            if _stable_identity(item) in added_ids
        ),
        "protected_dataset_keys": sorted(
            str(item.get("dataset_key") or "")
            for item in selected
            if _stable_identity(item) in protected_ids
        ),
    }


# 함수 설명: 질문의 숫자 세부 공정을 공정 그룹 payload.processes의 유일한 canonical 값과 연결해 후보에 표시합니다.
def _annotate_process_detail_matches(
    items: list[dict[str, Any]],
    question: str,
) -> list[dict[str, Any]]:
    mentioned = {
        re.sub(r"[/\\]+", "", value).lower()
        for value in re.findall(
            r"[A-Za-z]+(?:[/\\][A-Za-z]+)*\d+",
            str(question or ""),
        )
    }
    if not mentioned:
        return items
    annotated: list[dict[str, Any]] = []
    for item in items:
        next_item = deepcopy(item)
        payload = _dict(next_item.get("payload"))
        if str(next_item.get("section") or "") == "process_groups":
            process_by_compact: dict[str, list[str]] = {}
            for process in _list(payload.get("processes")):
                process_text = str(process or "").strip()
                if not process_text:
                    continue
                compact = re.sub(r"[/\\]+", "", process_text).lower()
                process_by_compact.setdefault(compact, []).append(process_text)
            matches = [
                values[0]
                for compact, values in process_by_compact.items()
                if compact in mentioned and len(values) == 1
            ]
            if matches:
                next_payload = deepcopy(payload)
                next_payload["processes"] = matches
                next_item["payload"] = next_payload
                next_item["question_match"] = {
                    "processes": matches,
                    "match_type": "numeric_detail_canonical",
                    "original_process_count": len(_list(payload.get("processes"))),
                }
        annotated.append(next_item)
    return annotated


# 함수 설명: 선택된 Table Catalog에만 짧은 후보 선택 힌트를 붙여 Intent LLM의 dataset 후보 비교를 돕습니다.
def _annotate_table_intent_selection_hints(
    items: list[dict[str, Any]],
    question: str,
) -> list[dict[str, Any]]:
    """Expose catalog-owned usage evidence without turning it into a source lock.

    The persisted catalog remains untouched.  This is an input-only projection
    that deliberately contains only bounded ``use_when``/``exclude_when`` and
    metric ownership information, so it improves candidate comparison without
    increasing the model's output contract.
    """

    result: list[dict[str, Any]] = []
    for item in items:
        next_item = deepcopy(item)
        payload = _dict(next_item.get("payload"))
        criteria = _dict(payload.get("selection_criteria"))
        use_when = _bounded_hint_strings(criteria.get("use_when"), question)
        exclude_when = _bounded_hint_strings(criteria.get("exclude_when"), question)
        semantics = (
            payload.get("metric_semantics")
            if isinstance(payload.get("metric_semantics"), dict)
            else {}
        )
        metric_columns = [
            str(column).strip()
            for column in semantics
            if str(column or "").strip()
        ][:INTENT_SELECTION_HINT_MAX_METRICS]
        metric_rollups = {
            column: str(semantics[column].get("default_rollup") or "").strip()
            for column in metric_columns
            if isinstance(semantics.get(column), dict)
            and str(semantics[column].get("default_rollup") or "").strip()
        }
        hint: dict[str, Any] = {}
        if use_when:
            hint["use_when"] = use_when
            matched = [phrase for phrase in use_when if _selection_hint_matches(question, phrase)]
            if matched:
                hint["matched_use_when"] = matched
        if exclude_when:
            hint["exclude_when"] = exclude_when
            matched = [phrase for phrase in exclude_when if _selection_hint_matches(question, phrase)]
            if matched:
                hint["matched_exclude_when"] = matched
        if metric_columns:
            hint["metric_columns"] = metric_columns
        if metric_rollups:
            hint["metric_default_rollups"] = metric_rollups
        if hint:
            next_item["intent_selection_hint"] = hint
        result.append(next_item)
    return result


# 함수 설명: 후보 힌트에 저장할 문자열을 개수와 길이로 제한합니다.
def _bounded_hint_strings(value: Any, question: Any = "") -> list[str]:
    """Return a small, question-relevant projection of author-written hints.

    Workers often write a natural list such as ``A, B, C일 때 사용`` in a
    single sentence.  The full sentence stays in persisted metadata; this
    input-only projection also considers comma-separated clauses so a relevant
    late clause is not silently lost behind the three-hint bound.  It is still
    only a candidate hint and never an execution source lock.
    """

    raw = value if isinstance(value, list) else [value]
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for source_index, item in enumerate(raw):
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        fragments = _selection_hint_fragments(text)
        for fragment_index, fragment in enumerate(fragments):
            normalized = re.sub(r"\s+", " ", fragment).strip()
            if not normalized:
                continue
            identity = normalized.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            score = _selection_hint_match_score(question, normalized)
            # Keep the original natural order for equal relevance while
            # prioritizing fragments whose terms actually occur in the
            # question.  Shortening is applied only after this ranking.
            candidates.append((score, -(source_index * 100 + fragment_index), normalized))
    candidates.sort(key=lambda item: (-item[0], -item[1]))
    return [
        text[:INTENT_SELECTION_HINT_MAX_TEXT]
        for _, _, text in candidates[:INTENT_SELECTION_HINT_MAX_PHRASES]
    ]


# 함수 설명: 사람이 쉼표로 나열한 사용·제외 대상을 작은 후보 힌트로 나눕니다.
def _selection_hint_fragments(text: str) -> list[str]:
    fragments = [part.strip() for part in re.split(r"[,，;；]", text) if part.strip()]
    return fragments or [text]


# 함수 설명: 질문과 Catalog 사용 문구가 직접 겹칠 때만 후보 힌트의 match를 표시합니다.
def _selection_hint_matches(question: Any, phrase: Any) -> bool:
    return _selection_hint_match_score(question, phrase) > 0


# 함수 설명: `_selection_hint_match_score()`는 후보 힌트와 질문의 직접·의미 토큰 겹침을
# 점수화하여, 긴 자연어 목록의 관련 절이 max hint 제한에서 밀리지 않게 합니다.
def _selection_hint_match_score(question: Any, phrase: Any) -> int:
    question_text = re.sub(r"[\s/\\._-]+", "", str(question or "").casefold())
    phrase_text = re.sub(r"[\s/\\._-]+", "", str(phrase or "").casefold())
    if not question_text or not phrase_text:
        return 0
    if phrase_text in question_text:
        return max(8, len(phrase_text))
    question_variants: set[str] = set()
    for token in re.findall(r"[\w가-힣]+", str(question or "").casefold()):
        question_variants.update(_token_variants(token))
    tokens = [
        token
        for token in re.findall(r"[\w가-힣]+", str(phrase or "").casefold())
        if len(token) >= 2 and token not in {
            "데이터", "조회", "질문", "사용", "때", "경우", "결과", "함께",
            "물어볼", "물으면", "알려줘", "보여줘", "하는", "할", "만",
        }
    ]
    unique_tokens = list(dict.fromkeys(tokens))
    if not unique_tokens:
        return 0
    matched = sum(
        1
        for token in unique_tokens
        if any(variant in question_variants for variant in _token_variants(token))
    )
    minimum = 1 if len(unique_tokens) == 1 else 2
    if matched < minimum:
        return 0
    # Two meaningful tokens are enough for a short worker-written clause.
    # Longer clauses require a modest overlap so generic words such as
    # "장비" alone cannot mark an unrelated catalog as matched.
    if len(unique_tokens) >= 4 and matched / len(unique_tokens) < 0.4:
        return 0
    return matched * 3


# 함수 설명: `_rank()`는 RANK의 일치도나 건수를 계산해 후보 비교와 요약에 사용합니다.
def _rank(items: list[dict[str, Any]], tokens: list[str]) -> list[dict[str, Any]]:
    return [entry[4] for entry in _rank_entries(items, tokens)]


# 함수 설명: `_rank_entries()`는 entries의 일치도나 건수를 계산해 후보 비교와 요약에 사용합니다.
def _rank_entries(
    items: list[dict[str, Any]],
    tokens: list[str],
) -> list[tuple[int, int, str, int, dict[str, Any]]]:
    ranked = []
    for index, item in enumerate(items):
        score, strong_hits = _score_details(item, tokens)
        ranked.append((score, strong_hits, _stable_identity(item), index, item))
    ranked.sort(key=lambda value: (-value[0], -value[1], value[2], value[3]))
    return ranked


# 함수 설명: `_score()`는 질문 token과 후보 메타데이터의 일치 정도를 점수로 계산합니다.
def _score(item: dict[str, Any], tokens: list[str]) -> int:
    return _score_details(item, tokens)[0]


# 함수 설명: 일반 entity 명사만으로 활성화하면 안 되는 조건부 실행 규칙인지 판정합니다.
def _has_conditional_execution_contract(item: dict[str, Any]) -> bool:
    payload = _dict(item.get("payload"))
    if any(payload.get(key) not in (None, "", [], {}) for key in ("conditions", "filters")):
        return True
    criteria = _dict(payload.get("selection_criteria"))
    if any(
        criteria.get(key) not in (None, "", [], {})
        for key in (
            "required_all_aliases",
            "required_any_aliases",
            "required_terms_all",
            "required_terms_any",
            "exclude_when",
        )
    ):
        return True
    for key, value in payload.items():
        if not str(key).strip().casefold().endswith("_selection"):
            continue
        stage = _dict(value)
        if any(stage.get(field) not in (None, "", [], {}) for field in ("filter", "filters", "conditions")):
            return True
    return False


# 함수 설명: Recipe의 generic entity key는 업무 규칙 활성화 증거가 아니며 Catalog key는 기존 신호를 유지합니다.
def _generic_technical_identity_is_weak(item: dict[str, Any], token: str) -> bool:
    if token not in GENERIC_CANONICAL_ALIAS_TOKENS:
        return False
    section = str(item.get("section") or "").strip().casefold()
    return section == "analysis_recipes" or _has_conditional_execution_contract(item)


# 함수 설명: `_score_details()`는 details의 일치도나 건수를 계산해 후보 비교와 요약에 사용합니다.
def _score_details(item: dict[str, Any], tokens: list[str]) -> tuple[int, int]:
    if not tokens:
        return 0, 0
    technical_identity = " ".join(
        str(item.get(key) or "")
        for key in TECHNICAL_IDENTITY_KEYS
    ).lower()
    payload = _dict(item.get("payload"))
    technical_identity = " ".join(
        value
        for value in (
            technical_identity,
            str(payload.get("function_name") or ""),
        )
        if value
    ).lower()
    label_identity = " ".join(
        str(container.get(key) or "")
        for container in (item, payload)
        for key in LABEL_KEYS
    ).lower()
    structured = " ".join(_structured_search_values(item)).lower()
    body = json.dumps(item, ensure_ascii=False, default=str).lower()
    # condition/processes처럼 실행 가능한 canonical mapping이 등록된 항목은 alias가 질문에
    # 직접 등장했을 때 일반 본문 keyword보다 우선합니다. A조·B조뿐 아니라 상태/제품/공정
    # 도메인 모두 같은 정책을 사용하며 특정 현업 표현을 코드에 하드코딩하지 않습니다.
    canonical_alias_hits = _canonical_alias_match_count(item, tokens)
    score = canonical_alias_hits * 24
    strong_hits = canonical_alias_hits
    for token in tokens:
        if _contains_token(technical_identity, token):
            # A technical key is useful as a tiebreaker, but a generic entity
            # noun inside that key (for example ``lot`` in a status recipe)
            # is not evidence that the recipe's business condition was asked
            # for.  Registered aliases/structured contracts still provide a
            # strong hit, and non-generic technical identifiers keep the
            # existing behavior.
            if _generic_technical_identity_is_weak(item, token):
                score += 1
            else:
                score += 12
                strong_hits += 1
        elif _contains_token(label_identity, token):
            if token in GENERIC_SEMANTIC_TOKENS or _generic_technical_identity_is_weak(item, token):
                score += 1
            else:
                score += 12
                strong_hits += 1
        elif _contains_token(structured, token):
            if token in GENERIC_SEMANTIC_TOKENS or _generic_technical_identity_is_weak(item, token):
                score += 1
            else:
                score += 6
                strong_hits += 1
        elif _contains_token(body, token):
            score += 1
    return score, strong_hits


# 함수 설명: 등록 alias와 질문 token이 일치하고 canonical 실행 mapping이 있는 도메인인지 판정합니다.
def _canonical_alias_match_count(item: dict[str, Any], tokens: list[str]) -> int:
    payload = _dict(item.get("payload"))
    if not any(payload.get(key) not in (None, "", [], {}) for key in CANONICAL_MAPPING_KEYS):
        return 0

    aliases = _list(payload.get("aliases"))
    aliases.extend(
        value
        for value in (
            payload.get("display_name"),
            item.get("display_name"),
            item.get("key"),
        )
        if value not in (None, "")
    )
    alias_tokens: set[str] = set()
    for alias in aliases:
        alias_tokens.update(_tokens(str(alias)))
    question_tokens = {
        token
        for token in tokens
        if token and token not in GENERIC_CANONICAL_ALIAS_TOKENS
    }
    canonical_alias_tokens = alias_tokens.difference(GENERIC_CANONICAL_ALIAS_TOKENS)
    return len(canonical_alias_tokens.intersection(question_tokens))


# 함수 설명: `_fit_bytes()`는 bytes이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _fit_bytes(
    candidates: dict[str, Any],
    max_bytes: int,
    min_table_items: int,
    *,
    protected_table_dataset_keys: set[str] | None = None,
    protected_domain_identities: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fitted = deepcopy(candidates)
    trimmed_counts = {"domain_items": 0, "table_catalog_items": 0, "main_flow_filters": 0}
    table_floor = min(min_table_items, len(_list(fitted.get("table_catalog_items"))))

    phases = (
        ("domain_items", 1 if _list(fitted.get("domain_items")) else 0),
        ("table_catalog_items", table_floor),
        ("main_flow_filters", 1 if _list(fitted.get("main_flow_filters")) else 0),
        ("domain_items", 0),
        ("main_flow_filters", 0),
        ("table_catalog_items", 0),
    )
    protected_keys = {
        str(value).strip().casefold()
        for value in (protected_table_dataset_keys or set())
        if str(value or "").strip()
    }
    protected_domain_keys = {
        str(value).strip()
        for value in (protected_domain_identities or set())
        if str(value or "").strip()
    }

    # 함수 설명: 바이트 제한 시 실행에 필요한 Domain 연결 카탈로그는 마지막까지 보존할 삭제 위치를 계산합니다.
    def removable_index(key: str, values: list[Any]) -> int:
        """Prefer dropping a non-dependency catalog candidate under the byte cap."""

        if key == "domain_items" and protected_domain_keys:
            for index in range(len(values) - 1, -1, -1):
                item = values[index]
                identity = _stable_identity(item) if isinstance(item, dict) else ""
                if identity not in protected_domain_keys:
                    return index
            # Every remaining Domain candidate is protected. The hard byte
            # ceiling still wins, so the bounded legacy fallback below may
            # remove one only when no unprotected option remains.
            return len(values) - 1
        if key == "table_catalog_items" and protected_keys:
            for index in range(len(values) - 1, -1, -1):
                item = values[index]
                dataset_key = (
                    _table_catalog_dataset_key(item).casefold()
                    if isinstance(item, dict)
                    else ""
                )
                if dataset_key not in protected_keys:
                    return index
            # The caller may still need to satisfy a hard byte cap when every
            # remaining candidate is execution-critical.  In that exceptional
            # case retain the existing bounded behaviour rather than exceeding the
            # token budget.
            return len(values) - 1
        return len(values) - 1

    for key, floor in phases:
        values = fitted.get(key)
        if not isinstance(values, list):
            continue
        while _json_bytes(fitted) > max_bytes and len(values) > floor:
            values.pop(removable_index(key, values))
            trimmed_counts[key] += 1
        if _json_bytes(fitted) <= max_bytes:
            break
    return fitted, {
        "truncated": any(trimmed_counts.values()),
        "trimmed_counts": trimmed_counts,
    }


# 함수 설명: `_is_non_runtime_function_case()`는 입력값이 NON·runtime·함수·Function Case 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _is_non_runtime_function_case(item: dict[str, Any]) -> bool:
    if str(item.get("section") or "") != "pandas_function_cases":
        return False
    runtime_helper = _dict(item.get("runtime_helper"))
    return not bool(runtime_helper.get("selectable_for_intent"))


# 함수 설명: `_structured_search_values()`는 메타데이터 항목의 key·별칭·payload에서 질문 검색에 쓸 구조화 문자열을 재귀 수집합니다.
def _structured_search_values(value: Any, parent_key: str = "") -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            if key_text in STRUCTURED_SEARCH_KEYS:
                result.extend(_scalar_texts(item))
            elif isinstance(item, (dict, list)):
                result.extend(_structured_search_values(item, key_text))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_structured_search_values(item, parent_key))
        return result
    return []


# 함수 설명: `_scalar_texts()`는 복합 입력 안의 문자열·숫자·불리언 값을 검색 가능한 문자열 목록으로 평탄화합니다.
def _scalar_texts(value: Any) -> list[str]:
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            result.append(str(key))
            result.extend(_scalar_texts(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_scalar_texts(item))
        return result
    return [str(value)] if value not in (None, "") else []


# 함수 설명: `_stable_identity()`는 메타데이터 후보의 section과 key로 순서가 변하지 않는 중복 제거 식별자를 만듭니다.
def _stable_identity(item: dict[str, Any]) -> str:
    payload = _dict(item.get("payload"))
    parts = [
        str(item.get("dataset_key") or ""),
        str(item.get("filter_key") or ""),
        str(item.get("section") or ""),
        str(item.get("key") or ""),
        str(payload.get("display_name") or ""),
    ]
    return "|".join(part.strip().lower() for part in parts)


# 함수 설명: `_contains_token()`는 입력값이 token 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _contains_token(text: str, token: str) -> bool:
    if not text or not token:
        return False
    if token.isascii() and token.replace("_", "").isalnum() and len(token) <= 3:
        return token in re.findall(r"[a-z0-9]+", text.lower())
    return token in text.lower()


# 함수 설명: `_extract()`는 복합 입력이나 응답에서 extract을 찾아 검증 가능한 기본 Python 값으로 변환합니다.
def _extract(value: Any, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        items = data.get(key)
        if not isinstance(items, list) and isinstance(data.get("metadata_candidates"), dict):
            items = data["metadata_candidates"].get(key)
        load = data.get("metadata_load") if isinstance(data.get("metadata_load"), dict) else {}
        return ([deepcopy(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []), deepcopy(load)
    if isinstance(data, list):
        return [deepcopy(item) for item in data if isinstance(item, dict)], {}
    return [], {}


# 함수 설명: `_sanitize_items()`는 항목에서 비밀값·내부 필드·직렬화 불가 값을 제거하거나 마스킹합니다.
def _sanitize_items(items: list[dict[str, Any]], metadata_type: str) -> list[dict[str, Any]]:
    return [
        _sanitize_metadata_item(item, metadata_type)
        for item in items
        if isinstance(item, dict)
    ]


# 함수 설명: `_sanitize_metadata_item()`는 메타데이터·항목에서 비밀값·내부 필드·직렬화 불가 값을 제거하거나 마스킹합니다.
def _sanitize_metadata_item(item: dict[str, Any], metadata_type: str) -> dict[str, Any]:
    sanitized = _sanitize_value(item, metadata_type == "table_catalog")
    if metadata_type == "domain" and isinstance(sanitized, dict):
        sanitized = _normalize_domain_filter_contracts(sanitized)
    return sanitized if isinstance(sanitized, dict) else {}


# 함수 설명: `_sanitize_value()`는 복합 값에서 비밀 필드와 불필요한 내부 값을 제거하고 JSON-safe 형태로 바꿉니다.
def _sanitize_value(value: Any, compact_source_config: bool = False) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in PRUNED_METADATA_KEYS:
                continue
            if compact_source_config and key_text in DEPRECATED_TABLE_CATALOG_KEYS:
                continue
            if compact_source_config and key_text in UNTRUSTED_PROMPT_CONFIG_KEYS:
                continue
            result[key_text] = _sanitize_value(item, compact_source_config)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item, compact_source_config) for item in value]
    return deepcopy(value)


# 함수 설명: `_annotate_runtime_function_cases()`는 선택 가능한 Function Case에 runtime 사용 가능 여부와 선택 근거를 덧붙입니다.
# 함수 설명: 기존 Domain 저장본의 legacy blank operator를 prompt 전달 전에 canonical 값으로 바꿉니다.
def _normalize_domain_filter_contracts(item: dict[str, Any]) -> dict[str, Any]:
    """기존 Domain 저장본의 legacy blank operator를 prompt 전달 전에 canonical 값으로 바꿉니다."""
    normalized = deepcopy(item)
    payload = normalized.get("payload")
    if not isinstance(payload, dict):
        return normalized
    next_payload = deepcopy(payload)
    for key, value in list(next_payload.items()):
        if str(key) in DOMAIN_FILTER_CONTAINER_KEYS:
            next_payload[key] = _normalize_domain_filter_value(value)
    normalized["payload"] = next_payload
    return normalized


# 함수 설명: 조건 컨테이너 안의 legacy operator를 공통 canonical 이름으로 변환합니다.
def _normalize_domain_filter_value(value: Any) -> Any:
    """조건 컨테이너 안의 legacy operator를 공통 canonical 이름으로 변환합니다."""
    if isinstance(value, list):
        return [_normalize_domain_filter_value(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    normalized = deepcopy(value)
    operator_key = "operator" if "operator" in normalized else ("op" if "op" in normalized else "")
    if operator_key:
        operator = re.sub(r"[\s-]+", "_", str(normalized.get(operator_key) or "").strip()).lower()
        if operator in LEGACY_NOT_BLANK_OPERATORS:
            normalized["operator"] = "not_blank"
            normalized.pop("op", None)
            normalized.pop("value", None)
            normalized.pop("values", None)
        elif operator in FILTER_OPERATOR_ALIASES:
            normalized["operator"] = FILTER_OPERATOR_ALIASES[operator]
            normalized.pop("op", None)
    for key, item in list(normalized.items()):
        if key in {"operator", "op", "value", "values"}:
            continue
        if isinstance(item, (dict, list)):
            normalized[key] = _normalize_domain_filter_value(item)
    return _collapse_same_field_null_or_empty(normalized)


# 함수 설명: 같은 field의 is_null OR is_empty 조건을 실행 가능한 단일 canonical 연산자로 축약합니다.
def _collapse_same_field_null_or_empty(value: dict[str, Any]) -> dict[str, Any]:
    if str(value.get("operator") or "").strip().lower() != "or":
        return value
    operands = value.get("operands")
    if not isinstance(operands, list) or len(operands) != 2:
        return value
    normalized_operands = [item for item in operands if isinstance(item, dict)]
    if len(normalized_operands) != 2:
        return value
    field_values = [
        str(item.get("field") or item.get("column") or "").strip()
        for item in normalized_operands
    ]
    operators = {
        str(item.get("operator") or "").strip().lower()
        for item in normalized_operands
    }
    if not field_values[0] or len(set(field_values)) != 1:
        return value
    if operators != {"is_null", "is_empty"}:
        return value
    field_key = (
        "field"
        if all("field" in item for item in normalized_operands)
        else "column"
    )
    return {
        field_key: field_values[0],
        "operator": "null_or_empty",
    }


# 함수 설명: 선택 가능한 Function Case의 runtime 사용 가능 여부와 선택 근거를 명시합니다.
def _annotate_runtime_function_cases(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    helper_by_name = {item["function_name"]: item for item in RUNTIME_FUNCTION_HELPERS}
    annotated = []
    for item in items:
        next_item = deepcopy(item)
        if str(next_item.get("section") or "") == "pandas_function_cases":
            function_name = _function_name(next_item)
            helper = helper_by_name.get(function_name)
            selectable = bool(helper and helper.get("selectable_for_intent"))
            next_item["runtime_helper"] = {
                "function_name": function_name,
                "available": bool(helper),
                "selectable_for_intent": selectable,
                "selection_policy": helper.get("selection_policy", "not_registered_runtime_helper") if helper else "not_registered_runtime_helper",
            }
            if not selectable:
                next_item["selection_note"] = (
                    "이 항목은 intent_plan.pandas_function_cases로 선택하지 않는다. "
                    "일반 pandas_execution_plan 또는 analysis guidance로만 참고한다."
                )
        annotated.append(next_item)
    return annotated


# 함수 설명: `_function_name()`는 Function Case 항목의 여러 호환 필드에서 실제 helper 함수 이름을 결정합니다.
def _function_name(item: dict[str, Any]) -> str:
    payload = _dict(item.get("payload"))
    explicit = str(item.get("function_name") or payload.get("function_name") or payload.get("helper_name") or "").strip()
    if explicit:
        return explicit
    text = " ".join(str(payload.get(key) or "") for key in ("description", "pseudocode", "usage_rule", "io_contract"))
    for helper in RUNTIME_FUNCTION_HELPERS:
        if helper["function_name"] in text:
            return helper["function_name"]
    return str(item.get("key") or "").strip()


# 함수 설명: `_looks_like_legacy_metadata_call()`는 입력값이 LIKE·legacy·메타데이터·CALL 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _looks_like_legacy_metadata_call(
    payload_value: Any,
    domain_items_value: Any,
    table_catalog_items_value: Any,
    main_flow_filters_value: Any,
) -> bool:
    if main_flow_filters_value is not None:
        return False
    data = getattr(payload_value, "data", payload_value)
    return isinstance(data, dict) and "domain_items" in data and "request" not in data


# 함수 설명: `_tokens()`는 문자열을 비교 가능한 검색 token 목록으로 분리·정규화합니다.
def _tokens(value: str) -> list[str]:
    stop = {
        "알려줘",
        "보여줘",
        "확인",
        "분석",
        "데이터",
        "현재",
        "오늘",
        "어제",
        "대한",
        "기준",
        "해당",
        "함께",
    }
    result: list[str] = []
    if _looks_like_ordered_range(value):
        # 구간 표현은 원문 끝점이 metadata에 직접 없더라도 runtime helper/OPER_SEQ recipe가 후보가 되게 한다.
        result.extend(("filter_ordered_range", "ordered_range", "oper_seq", "공정구간", "범위"))
    if _looks_like_product_token_expression(value):
        # 구조화 제품 token은 실제 값 자체가 metadata alias에 없으므로 helper의 기술 식별자를
        # 검색 token으로 보강해 등록된 product_token_match case가 후보 제한에서 밀리지 않게 한다.
        result.extend(("match_product_tokens", "product_token_match", "제품token"))
    for token in _process_detail_group_tokens(value):
        if token not in result:
            result.append(token)
    for token in _separator_normalized_tokens(value):
        if len(token) >= 2 and token not in result:
            result.append(token)
    for token in _compact_korean_phrase_tokens(value):
        if len(token) >= 2 and token not in result:
            result.append(token)
    for raw_token in re.findall(r"[0-9A-Za-z가-힣_]+", str(value or "").lower()):
        for token in _token_variants(raw_token):
            if len(token) < 2 or token in stop or token in result:
                continue
            result.append(token)
    return result[:60]


# 함수 설명: 공백이 있는 한국어 복합 alias를 붙여 쓴 질문과도 동일하게 비교할 compact 변형으로 만듭니다.
def _compact_korean_phrase_tokens(value: str) -> list[str]:
    result: list[str] = []
    for match in re.finditer(r"[가-힣]+(?:\s+[가-힣]+)+", str(value or "").lower()):
        compact = re.sub(r"\s+", "", match.group(0))
        if 2 <= len(compact) <= 20 and compact not in result:
            result.append(compact)
    return result


# 함수 설명: 숫자 세부 공정 뒤에 공정 접미사가 붙은 표현에서 metadata 공정 그룹을 찾을 stem을 추출합니다.
def _process_detail_group_tokens(value: str) -> list[str]:
    result: list[str] = []
    for match in re.finditer(
        r"([A-Za-z]+(?:[/\\][A-Za-z]+)*)\d+\s*(?:차)?\s*공정",
        str(value or ""),
        flags=re.IGNORECASE,
    ):
        normalized = re.sub(r"[/\\]+", "", match.group(1)).lower()
        if len(normalized) >= 2 and normalized not in result:
            result.append(normalized)
    return result


# 함수 설명: `_separator_normalized_tokens()`는 slash 등 label 내부 구분자를 제거한 값과 한국어 조사·공정 표현을 제거한 stem을 후보 token으로 만듭니다.
def _separator_normalized_tokens(value: str) -> list[str]:
    result: list[str] = []
    for raw_token in re.findall(r"[0-9A-Za-z가-힣]+(?:[/\\][0-9A-Za-z가-힣]+)+", str(value or "").lower()):
        normalized = re.sub(r"[/\\]+", "", raw_token)
        stem = re.sub(r"\d+$", "", normalized)
        # W/B공정에서처럼 구분자 뒤에 한국어가 이어지는 표현도 WB, WB공정 token으로 확장한다.
        # 공정 그룹별 예외를 만들지 않고 slash/backslash가 포함된 모든 영숫자 label에 같은 규칙을 적용한다.
        for candidate in (normalized, stem):
            for token in _token_variants(candidate):
                if len(token) >= 2 and token not in result:
                    result.append(token)
    return result


# 함수 설명: `_looks_like_ordered_range()`는 두 label 사이의 구간 기호·의미 표현을 찾되 MCP_NO 내부 hyphen은 제외합니다.
def _looks_like_ordered_range(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False

    # 물결표 계열은 양쪽 label을 명시하는 강한 범위 신호다. 두 MCP token 사이 표기는 공정 순서로 오인하지 않는다.
    for match in re.finditer(
        r"([0-9A-Za-z가-힣][0-9A-Za-z가-힣/_-]*)\s*[~∼～]\s*([0-9A-Za-z가-힣][0-9A-Za-z가-힣/_-]*)",
        text,
    ):
        endpoints = (match.group(1), match.group(2))
        if not all(_looks_like_mcp_token(endpoint) for endpoint in endpoints):
            return True

    # 일반 hyphen은 양쪽이 각각 문자와 숫자를 가진 label일 때만 구간 구분자로 인정한다.
    # 따라서 영문 1자리-숫자 3자리인 단일 MCP_NO 표기는 이 경로에 들어오지 않는다.
    for match in re.finditer(
        r"([0-9A-Za-z가-힣][0-9A-Za-z가-힣/_]*)\s*-\s*([0-9A-Za-z가-힣][0-9A-Za-z가-힣/_]*)",
        text,
    ):
        if all(_looks_like_ordered_endpoint(match.group(index)) for index in (1, 2)):
            return True

    # `시작부터 끝까지`는 label 자체 표기와 무관한 일반 범위 의미다.
    for match in re.finditer(
        r"([0-9A-Za-z가-힣][0-9A-Za-z가-힣/_-]*)\s*부터\s*([0-9A-Za-z가-힣][0-9A-Za-z가-힣/_-]*)\s*까지",
        text,
    ):
        endpoints = (match.group(1), match.group(2))
        if not all(_looks_like_mcp_token(endpoint) for endpoint in endpoints):
            return True

    # 내부 slash가 있는 두 차수 label을 붙여 쓴 표현도 두 끝점을 잇는 구간 후보로 본다.
    compact = re.sub(r"\s+", "", text)
    if re.search(r"(?:[A-Za-z가-힣]+/[A-Za-z가-힣]*\d+){2}", compact):
        return True

    lowered = text.lower()
    semantic_cue = any(
        cue in lowered
        for cue in ("공정 구간", "공정구간", "공정 범위", "공정범위", "공정 순서", "ordered range", "oper_seq 범위")
    )
    if not semantic_cue:
        return False
    endpoints = [
        token
        for token in re.findall(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣/_-]*", text)
        if _looks_like_ordered_endpoint(token) and not _looks_like_mcp_token(token)
    ]
    return len(endpoints) >= 2 or "oper_seq" in lowered


# 함수 설명: `_looks_like_ordered_endpoint()`는 범위 hyphen 양쪽 값이 문자와 숫자를 모두 가진 순서 label 형태인지 판정합니다.
def _looks_like_ordered_endpoint(value: str) -> bool:
    text = str(value or "")
    return bool(re.search(r"[A-Za-z가-힣]", text) and re.search(r"\d", text))


# 함수 설명: `_looks_like_mcp_token()`는 label 범위 구분자와 혼동하기 쉬운 영문 1자리-숫자 3자리 제품 token을 판정합니다.
def _looks_like_mcp_token(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]-\d{3}[A-Za-z0-9]*", str(value or "").strip()))


# 함수 설명: `_looks_like_product_token_expression()`은 metadata 값 자체가 아닌 구조화 제품 식별 표현이 질문에 있는지 판정합니다.
def _looks_like_product_token_expression(value: str) -> bool:
    text = str(value or "")
    if re.search(r"(?<![0-9A-Za-z])[A-Za-z]-\d{3}[A-Za-z0-9]*(?![0-9A-Za-z])", text):
        return True
    if re.search(r"(?<![0-9A-Za-z])(?:FC|F|X)\d+(?![0-9A-Za-z])", text, flags=re.IGNORECASE):
        return True
    return bool(
        re.search(
            r"(?<![0-9A-Za-z])\d+\s*(?:lead|ball)(?![0-9A-Za-z])",
            text,
            flags=re.IGNORECASE,
        )
    )


# 함수 설명: `_token_variants()`는 질문 token의 한국어 조사·접미 표현을 단계적으로 제거해 등록 alias와 비교할 변형을 만듭니다.
def _token_variants(token: str) -> list[str]:
    variants = [token]
    # Natural worker questions frequently attach a Korean semantic word to an
    # ASCII product/process abbreviation (for example ``PKG계획``).  A single
    # mixed-script token hides the Korean term from the generic expansion map,
    # so split script runs without assigning any dataset-specific meaning.
    # The normal expansion table below still decides whether a split term has
    # a reusable semantic alias such as ``plan`` or ``production``.
    for match in re.finditer(r"[a-z0-9_]+|[가-힣]+", token):
        segment = match.group(0)
        if segment != token and len(segment) >= 2 and segment not in variants:
            variants.append(segment)
    ascii_with_korean_suffix = re.fullmatch(r"([a-z0-9_]+)[가-힣]+", token)
    if ascii_with_korean_suffix:
        variants.append(ascii_with_korean_suffix.group(1))
    current = token
    while True:
        matched = False
        for suffix in KOREAN_SUFFIXES:
            if not current.endswith(suffix):
                continue
            stem = current[: -len(suffix)]
            if len(stem) >= 2 and stem not in variants:
                variants.append(stem)
                current = stem
                matched = True
            break
        if not matched:
            break
    expanded = list(variants)
    for value in variants:
        expanded.extend(TOKEN_EXPANSIONS.get(value, ()))
        # 현업 질문에는 표준 맞춤법인 ``가동률``과 관용 표기인 ``가동율``이
        # 혼용된다. 어느 한쪽을 특정 metric으로 하드코딩하지 않고 두 표기를
        # 같은 한국어 token의 철자 변형으로만 제공한다.
        if "율" in value:
            expanded.append(value.replace("율", "률"))
        if "률" in value:
            expanded.append(value.replace("률", "율"))
    return list(dict.fromkeys(expanded))


# 함수 설명: `_compact_state_terms()`는 상태·terms에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_state_terms(state: dict[str, Any]) -> str:
    current = _dict(state.get("current_data"))
    plan = _dict(state.get("last_intent_plan"))
    values = [
        str(plan.get("analysis_kind") or ""),
        " ".join(str(item) for item in current.get("source_dataset_keys", []) if item),
        " ".join(str(item) for item in current.get("columns", []) if item),
    ]
    return " ".join(item for item in values if item)


# 함수 설명: `_combined_status()`는 여러 MongoDB 로드 결과의 오류·성공·생략 상태를 하나의 최종 상태로 합칩니다.
def _combined_status(loads: dict[str, dict[str, Any]]) -> str:
    statuses = [str(load.get("status") or "") for load in loads.values() if isinstance(load, dict)]
    if any(status == "error" for status in statuses):
        return "error"
    if any(status == "ok" for status in statuses):
        return "ok"
    return "skipped"


# 함수 설명: `_json_bytes()`는 현재 값을 UTF-8 JSON으로 직렬화했을 때의 실제 바이트 크기를 계산합니다.
def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


# 함수 설명: `_bounded_int()`는 INT이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(parsed, maximum))


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataCandidatesBuilder(Component):
    display_name = "01D 질문 기반 메타데이터 후보 생성기"
    description = "도메인은 관련 항목 최대 20건, 테이블은 관련 후보 5건, 메인 필터는 전체를 32KB 안에서 선별합니다."
    inputs = [
        DataInput(name="payload", display_name="질문 페이로드", required=True),
        DataInput(name="domain_items", display_name="도메인 메타데이터", required=False),
        DataInput(name="table_catalog_items", display_name="테이블 카탈로그", required=False),
        DataInput(name="main_flow_filters", display_name="메인 변수", required=False),
        MessageTextInput(name="max_domain_items", display_name="도메인 최대 후보 수", value="20", advanced=True),
        MessageTextInput(name="min_table_items", display_name="테이블 최소 후보 수", value="5", advanced=True),
        MessageTextInput(name="max_table_items", display_name="테이블 최대 후보 수", value="5", advanced=True),
        MessageTextInput(name="max_bytes", display_name="최대 후보 바이트", value="32768", advanced=True),
    ]
    outputs = [Output(name="metadata_candidates", display_name="메타데이터 후보", method="build_payload")]

    # Langflow 출력 함수: '메타데이터 후보 (metadata_candidates)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=build_metadata_candidates(
                getattr(self, "payload", None),
                getattr(self, "domain_items", None),
                getattr(self, "table_catalog_items", None),
                getattr(self, "main_flow_filters", None),
                max_domain_items=getattr(self, "max_domain_items", DEFAULT_MAX_DOMAIN_ITEMS),
                min_table_items=getattr(self, "min_table_items", DEFAULT_MIN_TABLE_ITEMS),
                max_table_items=getattr(self, "max_table_items", DEFAULT_MAX_TABLE_ITEMS),
                max_bytes=getattr(self, "max_bytes", DEFAULT_MAX_BYTES),
            )
        )
