# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01D 질문 기반 메타데이터 후보 생성기
# 역할: 도메인은 관련 항목 최대 10건, 테이블은 관련 후보 최소 5/최대 10건, 메인 필터는 전체를 32KB 안에서 선별합니다.
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

DEFAULT_MAX_DOMAIN_ITEMS = 10
DEFAULT_MIN_TABLE_ITEMS = 5
DEFAULT_MAX_TABLE_ITEMS = 10
DEFAULT_MAX_BYTES = 32 * 1024
DOMAIN_MIN_SCORE = 6
NON_RUNTIME_FUNCTION_CASE_MIN_SCORE = 12
MAX_NON_RUNTIME_FUNCTION_CASES = 2

TECHNICAL_IDENTITY_KEYS = ("section", "key", "dataset_key", "filter_key", "function_name")
LABEL_KEYS = ("display_name", "label")
STRUCTURED_SEARCH_KEYS = {
    "aliases",
    "processes",
    "applies_when",
    "apply_conditions",
    "column_candidates",
    "metrics",
    "metric_columns",
    "quantity_columns",
    "join_keys",
    "required_params",
    "semantic_role",
}
KOREAN_SUFFIXES = (
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
    "된",
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
RUNTIME_FUNCTION_HELPERS = [
    {
        "function_name": "match_product_tokens",
        "selection_policy": "product_token_only",
        "selectable_for_intent": True,
        "description": "제품 속성 token 묶음을 실제 조회 DataFrame row와 매칭할 때만 사용한다.",
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
    candidates, byte_fit = _fit_bytes(candidates, byte_limit, table_minimum)

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

    return {
        "metadata_candidates": candidates,
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
            "selected_counts": {
                key: len(value)
                for key, value in candidates.items()
                if isinstance(value, list)
            },
            "selected_counts_before_bytes": selected_counts_before_bytes,
            "matched_counts": selection_stats["matched_counts"],
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
    non_runtime_function_cases = 0
    for score, strong_hits, _, _, item in ranked["domain_items"]:
        if strong_hits < 1 or score < DOMAIN_MIN_SCORE:
            continue
        if _is_non_runtime_function_case(item):
            if score < NON_RUNTIME_FUNCTION_CASE_MIN_SCORE or non_runtime_function_cases >= MAX_NON_RUNTIME_FUNCTION_CASES:
                continue
            non_runtime_function_cases += 1
        selected_domain.append(item)
        if len(selected_domain) >= max_domain_items:
            break

    table_related_count = sum(1 for _, strong_hits, _, _, _ in ranked["table_catalog_items"] if strong_hits > 0)
    table_target = min(
        len(table_items),
        max_table_items,
        max(min_table_items, min(table_related_count, max_table_items)),
    )
    selected = {
        "domain_items": selected_domain,
        "table_catalog_items": [entry[4] for entry in ranked["table_catalog_items"][:table_target]],
        "main_flow_filters": [entry[4] for entry in ranked["main_flow_filters"]],
    }
    return selected, {
        "matched_counts": {
            key: sum(1 for _, strong_hits, _, _, _ in values if strong_hits > 0)
            for key, values in ranked.items()
        }
    }


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
    score = 0
    strong_hits = 0
    for token in tokens:
        if _contains_token(technical_identity, token):
            score += 12
            strong_hits += 1
        elif _contains_token(label_identity, token):
            if token in GENERIC_SEMANTIC_TOKENS:
                score += 1
            else:
                score += 12
                strong_hits += 1
        elif _contains_token(structured, token):
            if token in GENERIC_SEMANTIC_TOKENS:
                score += 1
            else:
                score += 6
                strong_hits += 1
        elif _contains_token(body, token):
            score += 1
    return score, strong_hits


# 함수 설명: `_fit_bytes()`는 bytes이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _fit_bytes(
    candidates: dict[str, Any],
    max_bytes: int,
    min_table_items: int,
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
    for key, floor in phases:
        values = fitted.get(key)
        if not isinstance(values, list):
            continue
        while _json_bytes(fitted) > max_bytes and len(values) > floor:
            values.pop()
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
    return sanitized if isinstance(sanitized, dict) else {}


# 함수 설명: `_sanitize_value()`는 복합 값에서 비밀 필드와 불필요한 내부 값을 제거하고 JSON-safe 형태로 바꿉니다.
def _sanitize_value(value: Any, compact_source_config: bool = False) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in PRUNED_METADATA_KEYS:
                continue
            if compact_source_config and key_text in UNTRUSTED_PROMPT_CONFIG_KEYS:
                continue
            result[key_text] = _sanitize_value(item, compact_source_config)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item, compact_source_config) for item in value]
    return deepcopy(value)


# 함수 설명: `_annotate_runtime_function_cases()`는 선택 가능한 Function Case에 runtime 사용 가능 여부와 선택 근거를 덧붙입니다.
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
    for raw_token in re.findall(r"[0-9A-Za-z가-힣_]+", str(value or "").lower()):
        for token in _token_variants(raw_token):
            if len(token) < 2 or token in stop or token in result:
                continue
            result.append(token)
    return result[:60]


# 함수 설명: `_token_variants()`는 질문 token의 구분자 제거·영숫자 결합 등 비교용 표기 변형을 만듭니다.
def _token_variants(token: str) -> list[str]:
    variants = [token]
    ascii_with_korean_suffix = re.fullmatch(r"([a-z0-9_]+)[가-힣]+", token)
    if ascii_with_korean_suffix:
        variants.append(ascii_with_korean_suffix.group(1))
    for suffix in KOREAN_SUFFIXES:
        if token.endswith(suffix):
            stem = token[: -len(suffix)]
            if len(stem) >= 2:
                if re.fullmatch(r"[가-힣]+", token):
                    variants = [stem]
                else:
                    variants.append(stem)
            break
    expanded = list(variants)
    for value in variants:
        expanded.extend(TOKEN_EXPANSIONS.get(value, ()))
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
    description = "도메인은 관련 항목 최대 10건, 테이블은 관련 후보 최소 5/최대 10건, 메인 필터는 전체를 32KB 안에서 선별합니다."
    inputs = [
        DataInput(name="payload", display_name="질문 페이로드", required=True),
        DataInput(name="domain_items", display_name="도메인 메타데이터", required=False),
        DataInput(name="table_catalog_items", display_name="테이블 카탈로그", required=False),
        DataInput(name="main_flow_filters", display_name="메인 변수", required=False),
        MessageTextInput(name="max_domain_items", display_name="도메인 최대 후보 수", value="10", advanced=True),
        MessageTextInput(name="min_table_items", display_name="테이블 최소 후보 수", value="5", advanced=True),
        MessageTextInput(name="max_table_items", display_name="테이블 최대 후보 수", value="10", advanced=True),
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
