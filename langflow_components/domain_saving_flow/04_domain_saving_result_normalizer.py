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
    next_payload = payload
    next_payload["items"] = items
    next_payload["refinement"] = _refinement(payload, parsed)
    if not items and not next_payload["refinement"]["needs_more_input"] and not errors:
        errors.append({"type": "no_valid_items", "message": "저장할 수 있는 도메인 후보가 생성되지 않았습니다."})
    next_payload.setdefault("errors", []).extend(errors)
    next_payload.setdefault("trace", {})["generated_items_preview"] = [{"key": _key(item), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    return next_payload


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
