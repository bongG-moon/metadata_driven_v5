# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 도메인 등록 결과 정규화기
# 역할: 도메인 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다.
# 주요 입력: 페이로드 (payload) · 필수, LLM 응답 (llm_response) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: Markdown code fence를 제거하고 LLM JSON의 호환 key를 도메인 저장 스키마로 정규화합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

ALLOWED_SECTIONS = {"process_groups", "product_terms", "quantity_terms", "metric_terms", "analysis_recipes", "status_terms", "product_key_columns", "pandas_function_cases"}
FILTER_CONTAINER_KEYS = {"condition", "conditions", "condition_by_family", "condition_by_dataset"}
FUNCTION_CASE_SOURCE_FILTER_ORDERS = {"before_helper", "after_helper"}
SUPPORTED_FILTER_OPERATORS = {
    "eq",
    "in",
    "ne",
    "not_in",
    "gt",
    "ge",
    "lt",
    "le",
    "contains",
    "like",
    "starts_with",
    "ends_with",
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
    "or",
    "any",
}
FILTER_OPERATOR_ALIASES = {
    "=": "eq",
    "==": "eq",
    "!=": "ne",
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
    "notin": "not_in",
    "not in": "not_in",
    "startswith": "starts_with",
    "prefix": "starts_with",
    "endswith": "ends_with",
    "suffix": "ends_with",
    "isnull": "is_null",
    "isempty": "is_empty",
    "is_null_or_empty": "null_or_empty",
    "notnull": "not_null",
    "notempty": "not_empty",
    "notblank": "not_blank",
    "is_not_blank": "not_blank",
    # 이전 저장본과 LLM이 사용하던 두 표현은 모두 "null 또는 empty가 아님"을 뜻합니다.
    "is_not_null_or_empty": "not_blank",
    "is_not_null_and_not_empty": "not_blank",
    "not_null_or_empty": "not_blank",
    "not_null_and_not_empty": "not_blank",
}
WORKER_DATASET_KEY_PATTERN = re.compile(r"[（(]\s*([a-z][a-z0-9_]{2,})\s*[)）]", re.IGNORECASE)
WORKER_COLUMN_PATTERN = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9_]*_(?:ID|QTY|CNT))(?![A-Z0-9_])")
WORKER_RECIPE_KEY_PATTERN = re.compile(
    r"(?:규칙\s*키|recipe\s*key|rule\s*key|키)\s*[:：]\s*[`'\"]?([a-z][a-z0-9_]{2,})[`'\"]?",
    re.IGNORECASE,
)
WORKER_NUNIQUE_CUES = ("중복 없이", "중복제거", "중복 제거", "고유", "unique", "nunique")
WORKER_SUM_CUES = ("합계", "합을", "합산", "합계로", "sum")


# 주요 함수: LLM 등록 후보 JSON을 추출·검증해 저장 전 표준 items 배열로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def normalize_authoring(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    items = []
    errors = []
    if not parsed:
        errors.append({"type": "llm_response_parse_error", "message": "LLM 등록 응답을 JSON object로 해석하지 못했습니다."})
    elif not isinstance(parsed.get("items"), list):
        errors.append({"type": "invalid_items", "message": "LLM 등록 응답의 items는 배열이어야 합니다."})
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append({"type": "invalid_item", "message": f"items[{index}]가 object가 아닙니다."})
            continue
        item = deepcopy(raw)
        if "gbn" in item and "section" not in item:
            item["section"] = item["gbn"]
        item.setdefault("status", "active")
        item["payload"] = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        item["payload"], operator_errors = _normalize_domain_filter_contracts(
            item["payload"],
            f"items[{index}].payload",
        )
        errors.extend(operator_errors)
        if item.get("section") == "pandas_function_cases":
            item["payload"], execution_contract_errors = (
                _normalize_function_case_execution_contract(
                    item["payload"],
                    f"items[{index}].payload.execution_contract",
                )
            )
            errors.extend(execution_contract_errors)
        if item.get("section") not in ALLOWED_SECTIONS:
            errors.append({"type": "unsupported_section", "message": f"지원하지 않는 domain section입니다: {item.get('section')}", "index": index})
        items.append(item)
    # Worker-facing rules are deliberately short natural language.  A weak
    # authoring model may still classify them as status_terms,
    # product_key_columns, or pandas_function_cases.  Repair only the two
    # strongly identifiable rule families before similarity lookup or Mongo
    # write; ordinary metadata items are left untouched.
    items = _canonicalize_worker_rule_items(payload, items)
    items = _normalize_recipe_activation_contracts(payload, items)
    # Gemini can occasionally answer an empty item list for a sufficiently
    # concrete worker rule because it does not know which domain section to
    # choose.  Recover only a schema-backed generic quantity rule: a literal
    # dataset key, a literal execution column, and an explicit aggregation
    # cue must all be present.  This keeps natural authoring usable without
    # inventing a dataset, column, or business-specific recipe.
    if not items:
        recovered_recipe = _recover_recipe_update_from_worker_text(payload)
        if recovered_recipe is not None:
            items = [recovered_recipe]
            payload.setdefault("trace", {})["worker_rule_recovery"] = {
                "status": "recovered",
                "section": recovered_recipe["section"],
                "key": recovered_recipe["key"],
                "reason": "empty_llm_items_with_explicit_existing_recipe_key_and_restrictive_activation",
            }
    if not items:
        recovered = _recover_quantity_term_from_worker_text(payload)
        if recovered is not None:
            items = [recovered]
            payload.setdefault("trace", {})["worker_rule_recovery"] = {
                "status": "recovered",
                "section": recovered["section"],
                "key": recovered["key"],
                "reason": "empty_llm_items_with_explicit_dataset_column_and_aggregation",
            }
    next_payload = payload
    next_payload["items"] = items
    next_payload["refinement"] = _refinement(payload, parsed)
    if not items and not next_payload["refinement"]["needs_more_input"] and not errors:
        errors.append({"type": "no_valid_items", "message": "저장할 수 있는 도메인 후보가 생성되지 않았습니다."})
    next_payload.setdefault("errors", []).extend(errors)
    next_payload.setdefault("trace", {})["generated_items_preview"] = [{"key": _key(item), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    return next_payload


# 함수 설명: 기존 규칙의 사용 조건만 고치는 자연어 요청은 최소 변경 recipe 후보로 복원합니다.
def _recover_recipe_update_from_worker_text(payload: dict[str, Any]) -> dict[str, Any] | None:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    raw_text = str(request.get("raw_text") or "").strip()
    key = _explicit_worker_recipe_key(raw_text)
    terms = _worker_restricted_activation_terms(raw_text)
    lowered = raw_text.casefold()
    update_signal = any(token in raw_text for token in ("기존", "수정", "변경", "바꿔")) or any(
        token in lowered for token in ("update", "change", "modify")
    )
    if not key or not terms or not update_signal:
        return None
    return {
        "section": "analysis_recipes",
        "key": key,
        "status": "active",
        # Do not invent data sources, joins, or display fields.  On a merge,
        # the existing recipe retains its execution contract and only receives
        # the worker-owned activation condition below.
        "payload": {
            "selection_criteria": {
                "required_all_aliases": terms,
                "rules": _worker_rule_sentences(raw_text),
            }
        },
    }


# 함수 설명: `_refinement()`는 LLM이 반환한 보완 필요 정보와 가정을 저장 전 검수 단계까지 보존합니다.
# 함수 설명: Domain payload의 조건 컨테이너만 공통 filter operator 계약으로 정규화합니다.
RECIPE_RULE_KEY = "recipe_id_starts_with"
HOLD_RULE_KEY = "current_hold_lot_selection"


# 함수 설명: 작업자 자연어의 HOLD·RECIPE 강한 신호를 canonical analysis recipe 항목으로 정규화합니다.
def _canonicalize_worker_rule_items(payload: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map simple worker prose to canonical analysis-recipe identities.

    A weak authoring model may classify HOLD prose as a status term or a
    latest-history function case, and RECIPE prose as a product key.  Repair
    only these strong signals before similarity lookup; ordinary metadata is
    preserved unchanged.
    """
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    raw_text = str(request.get("raw_text") or "").strip()
    recipe_signal = _has_recipe_prefix_signal(raw_text)
    hold_signal = _has_hold_selection_signal(raw_text)
    if not raw_text or not (recipe_signal or hold_signal):
        return items
    output: list[dict[str, Any]] = []
    recipe_sources: list[dict[str, Any]] = []
    hold_sources: list[dict[str, Any]] = []
    for item in items:
        section = str(item.get("section") or "").strip()
        key = str(item.get("key") or "").strip()
        item_text = _item_text(item)
        if recipe_signal and _is_recipe_rule_item(section, key, item_text):
            recipe_sources.append(item)
        elif hold_signal and _is_hold_rule_item(section, key, item_text):
            hold_sources.append(item)
        else:
            output.append(item)
    if recipe_signal:
        output.append(_build_recipe_rule_item(raw_text, recipe_sources))
    if hold_signal:
        output.append(_build_hold_rule_item(raw_text, hold_sources))
    return output


# 함수 설명: 작업자가 “A를 함께 물어볼 때만”이라고 쓴 제한 조건을 recipe 적용 계약으로 정규화합니다.
def _normalize_recipe_activation_contracts(
    payload: dict[str, Any],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve worker prose while compiling explicit restrictive recipe cues.

    This intentionally handles only a narrow, generic ownership signal: a
    source or recipe is usable *only when* a stated concept is requested.  It
    does not infer datasets, joins, columns, or business-specific aliases.
    """

    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    raw_text = str(request.get("raw_text") or "").strip()
    if not raw_text:
        return items
    restricted_terms = _worker_restricted_activation_terms(raw_text)
    if not restricted_terms:
        return items
    explicit_recipe_key = _explicit_worker_recipe_key(raw_text)

    recipe_count = sum(
        1
        for item in items
        if isinstance(item, dict)
        and str(item.get("section") or "").strip() == "analysis_recipes"
    )
    normalized: list[dict[str, Any]] = []
    for raw_item in items:
        item = deepcopy(raw_item)
        if str(item.get("section") or "").strip() != "analysis_recipes":
            normalized.append(item)
            continue
        if recipe_count > 1 and not _recipe_mentions_activation_term(item, restricted_terms):
            normalized.append(item)
            continue
        if explicit_recipe_key and recipe_count == 1:
            # An author can name an existing rule for an update without
            # exposing any storage mechanics.  The writer still resolves and
            # validates that key against MongoDB before a merge is allowed.
            item["key"] = explicit_recipe_key
        item_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        criteria = item_payload.get("selection_criteria")
        if isinstance(criteria, dict):
            compiled = deepcopy(criteria)
        else:
            compiled = {}
        # A restrictive worker rule is an ownership contract.  Keep the
        # worker's sentences, rather than model-invented explanations, as its
        # human-readable record.  Existing structured keys remain untouched.
        rules = _worker_rule_sentences(raw_text)

        existing_required = _unique_text(
            _string_values(compiled.get("required_all_aliases"))
            + _string_values(compiled.get("required_terms_all"))
            + _string_values(compiled.get("required_aliases_all"))
        )
        compiled["required_all_aliases"] = _unique_text(
            existing_required + restricted_terms
        )
        if rules:
            compiled["rules"] = rules
        item_payload["selection_criteria"] = compiled
        item["payload"] = item_payload
        normalized.append(item)
    return normalized


# 함수 설명: 사람이 입력한 문장만 selection rules에 보존해 모델의 부가 설명을 실행 기준으로 쓰지 않습니다.
def _worker_rule_sentences(raw_text: str) -> list[str]:
    values = [
        sentence.strip(" -\t")
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", raw_text)
        if sentence.strip()
    ]
    return _unique_text(values)[:12]


# 함수 설명: 기존 규칙을 자연어로 수정할 때만 명시한 안전한 snake_case key를 읽습니다.
def _explicit_worker_recipe_key(raw_text: str) -> str:
    matches = _unique_text(match.group(1) for match in WORKER_RECIPE_KEY_PATTERN.finditer(raw_text))
    return matches[0] if len(matches) == 1 else ""


# 함수 설명: 여러 recipe가 함께 생성된 경우 제한 표현과 같은 개념을 가진 recipe에만 조건을 붙입니다.
def _recipe_mentions_activation_term(item: dict[str, Any], terms: list[str]) -> bool:
    item_text = _item_text(item)
    return any(str(term).casefold() in item_text for term in terms)


# 함수 설명: 자연어의 “~일 때만” 앞 개념을 짧고 검증 가능한 적용 조건으로 추출합니다.
def _worker_restricted_activation_terms(raw_text: str) -> list[str]:
    terms: list[str] = []
    pattern = re.compile(
        r"(?P<term>[A-Za-z0-9가-힣][A-Za-z0-9가-힣 _/\-]{0,64}?)"
        r"(?:을|를|은|는|이|가)\s*"
        r"(?:함께\s*)?"
        r"(?:물을|물어볼|물어보|질문할|질문하|요청할|요청하|포함할|포함하|조회할|조회하|사용할|사용하|확인할|확인하)"
        r"(?:\s*경우)?\s*때만",
        re.IGNORECASE,
    )
    for match in pattern.finditer(raw_text):
        candidate = _clean_worker_activation_term(match.group("term"))
        if candidate:
            terms.append(candidate)
    return _unique_text(terms)[:8]


# 함수 설명: 앞 문장까지 붙은 자연어 후보에서 실제 적용 키워드만 보수적으로 분리합니다.
def _clean_worker_activation_term(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ,.;:()[]{}")
    if not text:
        return ""
    # Structured identifiers are the least ambiguous worker-facing concepts.
    identifiers = re.findall(r"\b[A-Z][A-Z0-9_/-]{1,}\b", text)
    if identifiers:
        return identifiers[-1]
    text = re.split(r"(?:,|;|그리고|또는|이면|하면|물으면|에서)\s*", text)[-1].strip()
    text = re.sub(r"^(?:이|그|해당)\s+", "", text)
    text = re.sub(r"\s*(?:데이터|정보|값|항목|내용)$", "", text).strip()
    if not text or len(text) > 48:
        return ""
    return text


# 함수 설명: 문자열·문자열 배열·간단한 aliases 컨테이너를 보존 가능한 텍스트 배열로 바꿉니다.
def _string_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("aliases") or value.get("terms") or value.get("values") or value.get("rules")
    if isinstance(value, (str, int, float)):
        return [str(value).strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


# 함수 설명: `_recover_quantity_term_from_worker_text()`는 LLM이 빈 후보를 반환했을 때도
# 명시된 dataset/컬럼/집계 근거가 모두 있는 일반 수량 규칙만 안전하게 복원합니다.
def _recover_quantity_term_from_worker_text(payload: dict[str, Any]) -> dict[str, Any] | None:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    refinement = payload.get("refinement") if isinstance(payload.get("refinement"), dict) else {}
    raw_text = str(refinement.get("refined_text") or request.get("raw_text") or "").strip()
    if not raw_text:
        return None
    dataset_matches = list(WORKER_DATASET_KEY_PATTERN.finditer(raw_text))
    column_matches = WORKER_COLUMN_PATTERN.findall(raw_text)
    lowered = raw_text.casefold()
    if not dataset_matches or not column_matches:
        return None
    aggregation = ""
    if any(cue in lowered for cue in WORKER_NUNIQUE_CUES):
        aggregation = "nunique"
    elif any(cue in lowered for cue in WORKER_SUM_CUES):
        aggregation = "sum"
    if not aggregation:
        return None

    dataset_key = str(dataset_matches[0].group(1) or "").strip()
    columns = _unique_text(column_matches)
    if not dataset_key or len(columns) != 1:
        return None
    aliases = _worker_aliases_for_dataset_sentence(raw_text, dataset_matches[0])
    if not aliases:
        return None
    display_name = aliases[0]
    suffix = "count" if aggregation == "nunique" else "sum"
    key = _safe_domain_key(f"{dataset_key}_{suffix}")
    if not key:
        return None
    optional_criteria = _worker_optional_source_conditions(raw_text, dataset_key)
    candidate_payload: dict[str, Any] = {
        "display_name": display_name,
        "aliases": aliases,
        "data_source": dataset_key,
        "columns": columns,
        "aggregation_method": aggregation,
    }
    if optional_criteria:
        candidate_payload["selection_criteria"] = optional_criteria
    return {
        "section": "quantity_terms",
        "key": key,
        "status": "active",
        "payload": candidate_payload,
    }


# 함수 설명: `_worker_aliases_for_dataset_sentence()`는 실제 dataset key가 적힌 문장에서
# 작업자가 나열한 자연어 표현만 별칭으로 추출합니다.
def _worker_aliases_for_dataset_sentence(raw_text: str, match: re.Match[str]) -> list[str]:
    sentence_start = max(raw_text.rfind(".", 0, match.start()), raw_text.rfind("\n", 0, match.start())) + 1
    sentence = raw_text[sentence_start:match.start()]
    # "장비 대수, 설비 수는 ..."처럼 dataset 앞에 있는 주어부만 사용한다.
    subject = re.split(r"(?:은|는|이|가)\s+", sentence.strip(), maxsplit=1)[0].strip()
    subject = re.sub(r"(?:을|를)\s*(?:계산|등록|정의).*?$", "", subject).strip()
    values = re.split(r"[,/]|\s+또는\s+|\s+및\s+", subject)
    aliases = _unique_text([value.strip(" -:·") for value in values])
    return [value for value in aliases if 1 < len(value) <= 48][:12]


# 함수 설명: `_worker_optional_source_conditions()`는 보조 데이터 사용 조건처럼 실행을
# 강제하지 않는 자연어 조건만 selection_criteria로 보존합니다.
def _worker_optional_source_conditions(raw_text: str, dataset_key: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", raw_text)
    return _unique_text(
        sentence.strip()
        for sentence in sentences
        if sentence.strip()
        and dataset_key.casefold() not in sentence.casefold()
        and any(token in sentence for token in ("함께", "때만", "추가", "보조"))
    )[:4]


# 함수 설명: `_safe_domain_key()`는 자연어 복구 후보의 key를 ASCII snake_case로 제한합니다.
def _safe_domain_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return key[:80]


# 함수 설명: RECIPE 번호와 시작·접두·포함 표현이 함께 있는지 확인합니다.
def _has_recipe_prefix_signal(text: str) -> bool:
    has_recipe_word = "recipe" in text.casefold() or "레시피" in text
    has_token = bool(re.search(r"\bR\d{3,}[A-Za-z0-9_-]*\b", text, re.IGNORECASE))
    has_prefix_word = any(token in text for token in ("시작", "접두", "포함", "완전히", "번호"))
    return has_recipe_word and (has_token or has_prefix_word)


# 함수 설명: HOLD와 LOT·사유·코드·이력 등 선택 기준 표현이 함께 있는지 확인합니다.
def _has_hold_selection_signal(text: str) -> bool:
    lowered = text.casefold()
    if "hold" not in lowered and "홀드" not in text:
        return False
    # A process-range function-case example may mention a "Hold 된 Lot" as a
    # sample question.  That is not an authoring request for the HOLD recipe;
    # keep the mapping scoped to the worker's HOLD selection rule block.
    if "function case" in lowered or "pandas_function_cases" in lowered or "ordered_process_range" in lowered:
        return False
    return any(token in text for token in ("lot", "LOT", "사유", "코드", "이력", "최근", "목록", "ID"))


# 함수 설명: Domain 항목의 section·key·별칭·payload를 identity 판정용 문자열로 합칩니다.
def _item_text(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    values: list[Any] = [item.get("section"), item.get("key"), item.get("display_name")]
    values.extend(item.get("aliases") if isinstance(item.get("aliases"), list) else [])
    values.append(payload)
    return json.dumps(values, ensure_ascii=False, default=str).casefold()


# 함수 설명: 기존 LLM 항목이 RECIPE prefix 규칙을 소유하는지 판정합니다.
def _is_recipe_rule_item(section: str, key: str, item_text: str) -> bool:
    if section == "analysis_recipes" and key in {RECIPE_RULE_KEY, "raw_data_display_policy"}:
        return True
    if section == "product_key_columns" and key.casefold() in {"recipe", "recipe_id", RECIPE_RULE_KEY}:
        return True
    return "recipe" in item_text and any(token in item_text for token in ("시작", "starts_with", "prefix", "완전히"))


# 함수 설명: 기존 LLM 항목이 HOLD 현재 상태·최신 이력 선택 규칙을 소유하는지 판정합니다.
def _is_hold_rule_item(section: str, key: str, item_text: str) -> bool:
    if section == "analysis_recipes" and key == HOLD_RULE_KEY:
        return True
    if section == "status_terms" and (key.casefold() == "hold" or "hold" in item_text):
        return True
    if section == "pandas_function_cases":
        # Older prompts sometimes put the latest-HOLD selection in a
        # function-case item with a different key.  It still belongs to the
        # HOLD analysis recipe when the item clearly mentions HOLD plus a
        # LOT/history/reason/code concept; never keep a second function-case
        # owner for the same rule.
        return "hold" in item_text and any(
            token in item_text
            for token in (
                "latest_hold",
                "latest hold",
                "최근 hold",
                "최신 hold",
                "hold_history",
                "lot",
                "history",
                "이력",
                "사유",
                "코드",
            )
        )
    return section == "analysis_recipes" and "hold" in item_text


# 함수 설명: 작업자 RECIPE 설명을 starts_with 분석 recipe payload로 구성합니다.
def _build_recipe_rule_item(raw_text: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "display_name": "RECIPE 번호 시작값 조회 규칙",
        "aliases": ["RECIPE", "레시피", "RECIPE 번호", "레시피 번호"],
        "selection_criteria": _rule_criteria(raw_text, sources, [
            "RECIPE_ID는 입력한 번호와 완전히 같은 값이 아니라 해당 번호로 시작하는 값을 조회한다.",
            "예: R1234이면 R1234, R1234-A, R1234-01을 모두 포함한다.",
            "조회 operator는 starts_with이며 eq 또는 contains로 완전 일치/부분 포함 조회를 하지 않는다.",
        ]),
    }
    _merge_rule_payload(payload, sources)
    return {"section": "analysis_recipes", "key": RECIPE_RULE_KEY, "status": "active", "payload": payload}


# 함수 설명: 작업자 HOLD 설명을 lot_status 선조회와 LOT별 최신 hold_history 기준을 가진 recipe로 구성합니다.
def _build_hold_rule_item(raw_text: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "display_name": "현재 HOLD LOT 및 최신 HOLD 이력 조회 규칙",
        "aliases": ["현재 HOLD LOT", "HOLD LOT 목록", "HOLD 사유", "HOLD 코드", "HOLD 이력"],
        "source_datasets": ["lot_status", "hold_history"],
        "selection_criteria": _rule_criteria(raw_text, sources, [
            "LOT 목록이나 LOT ID만 물으면 lot_status에서 현재 HOLD LOT만 조회하고 HOLD 이력은 조회하지 않는다.",
            "현재 HOLD 사유·코드·상세 사유·최근 HOLD 이력을 물으면 lot_status에서 HOLD_STAT=OnHold인 LOT를 먼저 조회한다.",
            "선행 결과의 LOT_ID를 hold_history.required_params.LOT_ID로 전달한다.",
            "LOT_ID required_params가 빈 문자열인 상태에서 선행 결과의 result_ref와 LOT_ID를 전달해 후속 조회한다.",
            "hold_history는 LOT별 HOLD_TM 내림차순으로 정렬하고 LOT별 최신 한 건만 선택한다.",
            "결과는 LOT_ID, 공정, HOLD_TM, HOLD_CD, HOLD_DESC를 보여주며 이력이 없으면 이력 없음으로 표시한다.",
            "HOLD라는 단어만으로 hold_history를 선택하지 않는다.",
        ]),
        "current_selection": {"dataset_key": "lot_status", "filter": {"HOLD_STAT": {"operator": "eq", "value": "OnHold"}}},
        # This is an execution-neutral recipe activation contract.  The
        # analysis flow reads the structured fields below; it does not infer a
        # dependent query merely from a dataset name or a hard-coded question
        # pattern.  A plain current-list request therefore remains one stage,
        # while a request containing one of these history-detail concepts can
        # continue to the recipe's declared history selection.
        "dependent_selection": {
            "when_question_includes_any": ["사유", "코드", "상세", "이력", "시간"],
            "current_stage": "current_selection",
            "next_stage": "history_selection",
        },
        "history_selection": {
            "dataset_key": "hold_history", "required_params": {"LOT_ID": ""},
            "upstream_binding": {"source_alias": "previous_result", "source_column": "LOT_ID", "target_param": "LOT_ID", "operator": "in"},
            "latest_per_group": {
                "partition_by": ["LOT_ID"], "order_by": [{"column": "HOLD_TM", "direction": "desc"}],
                "limit_per_group": 1, "tie_policy": "error",
            },
            "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
        },
    }
    _merge_rule_payload(payload, sources)
    return {"section": "analysis_recipes", "key": HOLD_RULE_KEY, "status": "active", "payload": payload}


# 함수 설명: 원문과 기존 후보의 selection criteria를 중복 없이 canonical recipe에 합칩니다.
def _rule_criteria(raw_text: str, sources: list[dict[str, Any]], defaults: list[str]) -> list[str]:
    values: list[Any] = []
    for item in sources:
        item_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if isinstance(item_payload.get("selection_criteria"), list):
            values.extend(item_payload["selection_criteria"])
    values.extend(line.strip(" -•\t") for line in raw_text.splitlines() if line.strip())
    values.extend(defaults)
    return _unique_text(values)


# 함수 설명: 기존 후보의 source dataset·별칭만 canonical rule payload에 안전하게 병합합니다.
def _merge_rule_payload(target: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    for item in sources:
        source = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        for key in ("source_datasets", "aliases"):
            values = source.get(key)
            if isinstance(values, list):
                target[key] = _unique_text(list(target.get(key) or []) + values)


# 함수 설명: 빈 문자열과 중복을 제거한 순서 보존 문자열 목록을 만듭니다.
def _unique_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


# 함수 설명: Domain payload 내부의 filter operator 표현을 실행 계약의 canonical 형태로 정규화합니다.
def _normalize_domain_filter_contracts(
    payload: dict[str, Any],
    path: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Domain payload의 조건 컨테이너만 공통 filter operator 계약으로 정규화합니다."""
    normalized = deepcopy(payload)
    errors: list[dict[str, Any]] = []
    for key, value in list(normalized.items()):
        if str(key) not in FILTER_CONTAINER_KEYS:
            continue
        normalized[key] = _normalize_filter_contract_value(
            value,
            f"{path}.{key}",
            errors,
        )
    return normalized, errors


# 함수 설명: Function Case 실행 순서 metadata를 공통 허용값으로 검증하고 표준화합니다.
def _normalize_function_case_execution_contract(
    payload: dict[str, Any],
    path: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized = deepcopy(payload)
    raw_contract = normalized.get("execution_contract")
    if raw_contract in (None, "", {}):
        normalized.pop("execution_contract", None)
        return normalized, []
    if not isinstance(raw_contract, dict):
        return normalized, [
            {
                "type": "invalid_function_case_execution_contract",
                "message": "pandas function case의 execution_contract는 object여야 합니다.",
                "path": path,
            }
        ]
    source_filter_order = str(
        raw_contract.get("source_filter_order") or ""
    ).strip()
    if source_filter_order not in FUNCTION_CASE_SOURCE_FILTER_ORDERS:
        return normalized, [
            {
                "type": "invalid_function_case_source_filter_order",
                "message": (
                    "source_filter_order는 before_helper 또는 "
                    "after_helper만 사용할 수 있습니다."
                ),
                "path": f"{path}.source_filter_order",
            }
        ]
    normalized["execution_contract"] = {
        "source_filter_order": source_filter_order,
    }
    return normalized, []


# 함수 설명: condition 내부 operator만 정규화하고 field/value 업무 조건은 보존합니다.
def _normalize_filter_contract_value(
    value: Any,
    path: str,
    errors: list[dict[str, Any]],
) -> Any:
    """condition 내부 operator만 정규화하고 field/value 업무 조건은 보존합니다."""
    if isinstance(value, list):
        return [
            _normalize_filter_contract_value(item, f"{path}[{index}]", errors)
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)

    normalized = deepcopy(value)
    operator_key = "operator" if "operator" in normalized else ("op" if "op" in normalized else "")
    if operator_key:
        raw_operator = normalized.get(operator_key)
        canonical = _canonical_filter_operator(raw_operator)
        if canonical not in SUPPORTED_FILTER_OPERATORS:
            errors.append(
                {
                    "type": "unsupported_filter_operator",
                    "message": f"지원하지 않는 domain filter operator입니다: {raw_operator}",
                    "path": f"{path}.{operator_key}",
                }
            )
        else:
            normalized["operator"] = canonical
            normalized.pop("op", None)
            if canonical == "not_blank":
                normalized.pop("value", None)
                normalized.pop("values", None)

    for key, item in list(normalized.items()):
        if key in {"operator", "op", "value", "values"}:
            continue
        if isinstance(item, (dict, list)):
            normalized[key] = _normalize_filter_contract_value(
                item,
                f"{path}.{key}",
                errors,
            )
    return normalized


# 함수 설명: 여러 filter operator 표기를 Data Analysis runtime의 canonical 이름으로 바꿉니다.
def _canonical_filter_operator(value: Any) -> str:
    """여러 filter operator 표기를 Data Analysis runtime의 canonical 이름으로 바꿉니다."""
    text = re.sub(r"[\s-]+", "_", str(value or "eq").strip()).lower()
    return FILTER_OPERATOR_ALIASES.get(text, text)


# 함수 설명: LLM이 반환한 보완 필요 정보와 가정을 저장 전 검토 단계까지 보존합니다.
def _refinement(payload: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(payload.get("refinement")) if isinstance(payload.get("refinement"), dict) else {}
    parsed_refinement = parsed.get("refinement") if isinstance(parsed.get("refinement"), dict) else {}
    missing = _string_list(parsed.get("missing_information")) or _string_list(parsed_refinement.get("missing_information"))
    assumptions = _string_list(parsed.get("assumptions")) or _string_list(parsed_refinement.get("assumptions"))
    current.update(
        {
            "refined_text": str(parsed_refinement.get("refined_text") or current.get("refined_text") or ""),
            "needs_more_input": _truthy(parsed.get("needs_more_input")) or _truthy(parsed_refinement.get("needs_more_input")) or bool(missing),
            "missing_information": missing,
            "assumptions": assumptions,
        }
    )
    return current


# 함수 설명: `_string_list()`는 보완 질문과 가정을 빈 문자열 없이 표준 문자열 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_truthy()`는 LLM의 bool 또는 문자열 값을 안전한 참/거짓 값으로 해석합니다.
def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


# 함수 설명: `_key()`는 메타데이터 항목에서 비교·표시에 사용할 논리 key를 안전하게 꺼냅니다.
def _key(item: dict[str, Any]) -> str:
    return f"{item.get('section', '')}:{item.get('key', '')}" if item.get("section") else str(item.get("key", ""))


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_json()`는 Message·dict·JSON 문자열에서 Markdown fence를 제거하고 JSON object를 안전하게 추출합니다.
def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = str(value or "")
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class DomainSavingResultNormalizer(Component):
    display_name = "04 도메인 등록 결과 정규화기"
    description = "도메인 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="llm_response", display_name="LLM 응답", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_authoring(getattr(self, "payload", None), getattr(self, "llm_response", "")))
