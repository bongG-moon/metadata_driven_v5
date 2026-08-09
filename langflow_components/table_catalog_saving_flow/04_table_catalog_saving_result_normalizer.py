# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 테이블 카탈로그 등록 결과 정규화기
# 역할: 테이블 카탈로그 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다.
# 주요 입력: 페이로드 (payload) · 필수, LLM 응답 (llm_response) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: Markdown code fence를 제거하고 LLM JSON의 호환 key를 테이블 카탈로그 저장 스키마로 정규화합니다.
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


# 저장 Flow가 허용하는 실제 metric 계약 필드입니다. 업무 표현이나 결과 별칭은
# 실행 컬럼 계약에 저장하지 않고, 실제 조회 컬럼에만 연결합니다.
METRIC_SEMANTIC_FIELDS = {
    "semantic_type",
    "additive",
    "default_rollup",
    "allowed_rollups",
    "source_already_aggregated",
    "value_transform",
}
DEFAULT_UPSTREAM_MAX_VALUES = 200
IDENTIFIER_PATTERN = re.compile(r"(?i)(?<![A-Z0-9_])([A-Z][A-Z0-9_]*_ID)(?![A-Z0-9_])")
VALUE_SUM_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9_])([A-Z][A-Z0-9_]*)\s*(?:값)?\s*(?:의)?\s*(?:합계|합|sum)(?![A-Z0-9_])"
)
COUNT_CUES = ("count", "unique", "nunique", "고유", "중복", "건수", "개수")
PREVIOUS_RESULT_CUES = (
    "직전 결과",
    "이전 결과",
    "앞에서 조회한",
    "앞서 조회한",
    "방금 조회한",
    "같은 세션",
)
FOLLOWUP_CUES = ("후속", "다음 조회", "이력 조회", "이어", "연결")

# 주요 함수: LLM 등록 후보 JSON을 추출·검증해 저장 전 표준 items 배열로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def normalize_authoring(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    items = []
    errors = []
    assumptions: list[str] = []
    request = _dict(payload.get("request"))
    refinement = _dict(payload.get("refinement"))
    source_text = str(refinement.get("refined_text") or request.get("raw_text") or "")
    if not parsed:
        errors.append({"type": "llm_response_parse_error", "message": "LLM 등록 응답을 JSON object로 해석하지 못했습니다."})
    elif not isinstance(parsed.get("items"), list):
        errors.append({"type": "invalid_items", "message": "LLM 등록 응답의 items는 배열이어야 합니다."})
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append({"type": "invalid_item", "message": f"items[{index}]가 object가 아닙니다."})
            continue
        item = deepcopy(raw)
        if "dataset_key" not in item and "key" in item:
            item["dataset_key"] = item["key"]
        item.setdefault("status", "active")
        item["payload"] = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        source_config = item["payload"].get("source_config") if isinstance(item["payload"].get("source_config"), dict) else {}
        for key in ("sql", "query", "oracle_sql", "query_template"):
            if key in item["payload"] and "query_template" not in source_config:
                source_config["query_template"] = item["payload"].pop(key)
        if source_config:
            item["payload"]["source_config"] = source_config
        assumptions.extend(_normalize_natural_catalog_contract(item["payload"], source_text))
        items.append(item)
    next_payload = payload
    next_payload["items"] = items
    next_payload["refinement"] = _refinement(payload, parsed)
    if assumptions:
        next_payload["refinement"]["assumptions"] = _unique_texts(
            [
                *_string_list(next_payload["refinement"].get("assumptions")),
                *assumptions,
            ]
        )
    if not items and not next_payload["refinement"]["needs_more_input"] and not errors:
        errors.append({"type": "no_valid_items", "message": "저장할 수 있는 테이블 카탈로그 후보가 생성되지 않았습니다."})
    next_payload.setdefault("errors", []).extend(errors)
    next_payload.setdefault("trace", {})["generated_items_preview"] = [{"key": item.get("dataset_key", ""), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    return next_payload


# 함수 설명: `_normalize_natural_catalog_contract()`는 작업자가 기술 구조를 쓰지 않아도
# 명확한 자연어의 파생 지표와 직전 결과 연계 의도를 실행 가능한 Catalog 계약으로 보강합니다.
def _normalize_natural_catalog_contract(payload: dict[str, Any], source_text: str) -> list[str]:
    assumptions = _normalize_metric_semantics_from_natural_text(payload, source_text)
    _infer_previous_result_binding(payload, source_text)
    return assumptions


# 함수 설명: `_normalize_metric_semantics_from_natural_text()`는 "LOT_ID 고유 건수"처럼
# 결과 이름이 아닌 실제 source 컬럼을 기준으로 metric_semantics를 정규화합니다.
def _normalize_metric_semantics_from_natural_text(payload: dict[str, Any], source_text: str) -> list[str]:
    raw_semantics = payload.get("metric_semantics")
    if raw_semantics not in (None, "", {}) and not isinstance(raw_semantics, dict):
        return []

    prose = _authoring_prose(source_text)
    execution_columns = _execution_column_index(payload)
    if not execution_columns:
        return []

    count_columns = _count_identifier_columns(prose, execution_columns)
    sum_mentions = _sum_column_mentions(prose)
    sum_columns = [
        execution_columns[_column_key(column)]
        for column in sum_mentions
        if _column_key(column) in execution_columns
    ]
    missing_sum_columns = [
        column for column in sum_mentions if _column_key(column) not in execution_columns
    ]
    normalized: dict[str, dict[str, Any]] = {}
    assumptions: list[str] = []

    for raw_metric, raw_contract in (raw_semantics or {}).items():
        metric = str(raw_metric or "").strip()
        if not metric or not isinstance(raw_contract, dict):
            # 형식 자체가 잘못된 계약은 Review Writer가 기존처럼 명확히 차단한다.
            normalized[metric] = deepcopy(raw_contract)
            continue
        target = execution_columns.get(_column_key(metric))
        source_hint = str(
            raw_contract.get("source_column")
            or raw_contract.get("column")
            or raw_contract.get("source")
            or ""
        ).strip()
        if not target and source_hint:
            target = execution_columns.get(_column_key(source_hint))
        if not target and _looks_like_count_metric(metric, raw_contract) and len(count_columns) == 1:
            target = count_columns[0]
        if not target and _looks_like_quantity_metric(metric, raw_contract) and len(sum_columns) == 1:
            target = sum_columns[0]
        if target:
            _merge_metric_contract(normalized, target, _sanitized_metric_contract(raw_contract))
            continue
        if _looks_like_derived_metric(metric, raw_contract) and (count_columns or sum_mentions):
            assumptions.append(
                f"'{metric}'은 결과 이름이므로 실제 조회 컬럼으로 저장하지 않았습니다. "
                "쿼리에 포함된 실제 컬럼으로만 계산 규칙을 연결했습니다."
            )
            continue
        normalized[metric] = deepcopy(raw_contract)

    for column in count_columns:
        _merge_metric_contract(
            normalized,
            column,
            {
                "semantic_type": "count",
                "additive": False,
                "default_rollup": "nunique",
                "allowed_rollups": ["nunique"],
                "source_already_aggregated": False,
            },
            replace=True,
        )
    for column in sum_columns:
        _merge_metric_contract(
            normalized,
            column,
            {
                "semantic_type": "quantity",
                "additive": True,
                "default_rollup": "sum",
                "allowed_rollups": ["sum"],
                "source_already_aggregated": False,
            },
            replace=True,
        )
    for column in missing_sum_columns:
        assumptions.append(
            f"수량 계산에 언급된 '{column}' 컬럼은 현재 query_template/columns에 없으므로 "
            "실행 가능한 수량 계산 규칙으로 저장하지 않았습니다. 해당 컬럼을 SELECT에 포함한 뒤 다시 등록해 주세요."
        )

    if normalized:
        payload["metric_semantics"] = normalized
    else:
        payload.pop("metric_semantics", None)
    return _unique_texts(assumptions)


# 함수 설명: `_infer_previous_result_binding()`은 "직전 결과의 LOT 번호로 후속 조회"처럼
# 명확한 자연어 관계가 있고 단일 식별자 파라미터가 확인될 때만 previous_result binding을 만듭니다.
def _infer_previous_result_binding(payload: dict[str, Any], source_text: str) -> None:
    prose = _authoring_prose(source_text)
    lowered = prose.casefold()
    if not any(cue in lowered for cue in PREVIOUS_RESULT_CUES):
        return
    if not any(cue in lowered for cue in FOLLOWUP_CUES):
        return

    source_config = payload.get("source_config") if isinstance(payload.get("source_config"), dict) else {}
    existing_bindings = source_config.get("upstream_bindings")
    if isinstance(existing_bindings, list) and existing_bindings:
        return
    if existing_bindings not in (None, "", []):
        return

    execution_columns = _execution_column_index(payload)
    required_params = _catalog_required_params(payload, source_config)
    if not required_params:
        return
    mentioned_identifiers = {
        _column_key(column) for column in _identifier_mentions(prose)
    }
    candidates = [
        parameter
        for parameter in required_params
        if _column_key(parameter) in mentioned_identifiers
        and _column_key(parameter) in execution_columns
    ]
    if not candidates:
        id_params = [
            parameter
            for parameter in required_params
            if _column_key(parameter).endswith("ID")
            and _column_key(parameter) in execution_columns
        ]
        if len(id_params) == 1:
            candidates = id_params
    if len(candidates) != 1:
        return

    target_param = candidates[0]
    source_column = execution_columns[_column_key(target_param)]
    source_config["upstream_bindings"] = [
        {
            "entity_type": _entity_type_from_identifier(target_param),
            "source_alias": "previous_result",
            "source_column": source_column,
            "target_param": target_param,
            "operator": "in",
            "max_values": DEFAULT_UPSTREAM_MAX_VALUES,
        }
    ]
    payload["source_config"] = source_config


# 함수 설명: `_authoring_prose()`는 SQL 본문을 제외한 작업자 설명만 뽑아 컬럼명이
# query 안에 있다는 이유만으로 파생 지표가 잘못 매핑되는 일을 막습니다.
def _authoring_prose(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return re.sub(
        r"(?is)\bquery_template\s*:\s*.*?(?=\bfilter_mappings\b|\bdefault_detail_columns\b|같은\s*세션|직전\s*결과|이전\s*결과|앞에서\s*조회|후속\s*(?:이력|조회)|$)",
        " ",
        text,
    )


# 함수 설명: `_execution_column_index()`는 실제 query columns와 canonical filter mapping을
# 같은 실행 컬럼 namespace로 찾아 실제 metric 계약을 안전하게 정규화합니다.
def _execution_column_index(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for column in _catalog_columns(payload.get("columns")):
        result.setdefault(_column_key(column), column)
    mappings = payload.get("filter_mappings") if isinstance(payload.get("filter_mappings"), dict) else {}
    for canonical, aliases in mappings.items():
        canonical_text = str(canonical or "").strip()
        if not canonical_text:
            continue
        result.setdefault(_column_key(canonical_text), canonical_text)
        for alias in _as_string_list(aliases):
            result.setdefault(_column_key(alias), canonical_text)
    return result


# 함수 설명: `_identifier_mentions()`는 작업자 설명에 직접 적힌 식별자 컬럼 이름을
# 중복 없이 추출해 후속 조회 연결과 고유 건수 규칙의 근거로 사용합니다.
def _identifier_mentions(prose: str) -> list[str]:
    result: list[str] = []
    for match in IDENTIFIER_PATTERN.finditer(prose):
        column = str(match.group(1) or "").strip()
        if column and column not in result:
            result.append(column)
    return result


# 함수 설명: `_count_identifier_columns()`는 고유/건수 표현 주변의 실제 *_ID 컬럼만 찾아
# nunique metric으로 바꿉니다.
def _count_identifier_columns(prose: str, execution_columns: dict[str, str]) -> list[str]:
    result: list[str] = []
    lowered = prose.casefold()
    for match in IDENTIFIER_PATTERN.finditer(prose):
        context = lowered[max(0, match.start() - 80) : min(len(prose), match.end() + 80)]
        if not any(cue in context for cue in COUNT_CUES):
            continue
        target = execution_columns.get(_column_key(match.group(1)))
        if target and target not in result:
            result.append(target)
    return result


# 함수 설명: `_sum_column_mentions()`는 "SOURCE_QTY 값의 합"처럼 작업자가 명시한
# 실제 컬럼 후보만 뽑아, 컬럼이 query에 없을 때는 추측하지 않고 안내로 남깁니다.
def _sum_column_mentions(prose: str) -> list[str]:
    result: list[str] = []
    for match in VALUE_SUM_PATTERN.finditer(prose):
        column = str(match.group(1) or "").strip()
        if column and column not in result:
            result.append(column)
    return result


# 함수 설명: `_catalog_required_params()`는 payload와 source_config 양쪽에 선언된
# 조회 필수 파라미터를 순서 유지로 합칩니다.
def _catalog_required_params(payload: dict[str, Any], source_config: dict[str, Any]) -> list[str]:
    return _unique_texts(
        [
            *_as_string_list(payload.get("required_params")),
            *_as_string_list(source_config.get("required_params")),
            *[
                str(key).strip()
                for key in (payload.get("required_param_mappings") or {})
                if str(key).strip()
            ],
        ]
    )


# 함수 설명: `_entity_type_from_identifier()`는 LOT_ID처럼 표준적인 식별자 이름에서
# binding entity_type을 기계적으로 도출해 작업자가 내부 계약명을 알 필요가 없게 합니다.
def _entity_type_from_identifier(value: Any) -> str:
    normalized = _column_key(value)
    if normalized.endswith("ID"):
        normalized = normalized[:-2]
    return normalized.lower() or "entity"


# 함수 설명: `_looks_like_count_metric()`은 결과 별칭이 아닌 고유 건수 계산 의도인지
# 판별해 실제 ID 컬럼으로만 안전하게 접습니다.
def _looks_like_count_metric(metric: str, contract: dict[str, Any]) -> bool:
    text = str(metric or "").casefold()
    rollup = str(contract.get("default_rollup") or "").casefold()
    return any(token in text for token in ("count", "cnt", "건수")) or rollup in {"count", "nunique"}


# 함수 설명: `_looks_like_quantity_metric()`은 합계 수량 결과 별칭을 실제 source 수량
# 컬럼으로 접을 수 있는지 판단합니다.
def _looks_like_quantity_metric(metric: str, contract: dict[str, Any]) -> bool:
    text = str(metric or "").casefold()
    rollup = str(contract.get("default_rollup") or "").casefold()
    return any(token in text for token in ("qty", "quantity", "수량")) or rollup == "sum"


# 함수 설명: `_looks_like_derived_metric()`은 실행 스키마에 없는 결과 별칭을 일반 컬럼
# 오류와 구분해, 원문에 파생 계산 근거가 있을 때만 보수적으로 제거합니다.
def _looks_like_derived_metric(metric: str, contract: dict[str, Any]) -> bool:
    return _looks_like_count_metric(metric, contract) or _looks_like_quantity_metric(metric, contract)


# 함수 설명: `_sanitized_metric_contract()`는 LLM이 준 도움용 source_column/label 같은
# 비저장 필드를 버리고 Table Catalog가 지원하는 metric 계약만 남깁니다.
def _sanitized_metric_contract(contract: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): deepcopy(value)
        for key, value in contract.items()
        if str(key) in METRIC_SEMANTIC_FIELDS
    }


# 함수 설명: `_merge_metric_contract()`는 같은 실제 컬럼에 모인 metric 계약을 한 번만
# 저장하며 자연어에서 명시한 rollup은 기존 약한 모델 출력을 덮어씁니다.
def _merge_metric_contract(
    target: dict[str, dict[str, Any]],
    column: str,
    contract: dict[str, Any],
    *,
    replace: bool = False,
) -> None:
    if not column:
        return
    if replace or column not in target:
        target[column] = deepcopy(contract)
        return
    target[column].update(deepcopy(contract))


# 함수 설명: `_catalog_columns()`는 list/object 형태의 columns에서 실제 조회 컬럼명을
# 하나의 순서 보존 문자열 목록으로 정리합니다.
def _catalog_columns(value: Any) -> list[str]:
    values = value if isinstance(value, list) else list(value) if isinstance(value, dict) else []
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("column_name") or item.get("name") or item.get("column") or item.get("key")
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_as_string_list()`는 단일 문자열과 배열을 같은 정규화 목록으로 바꿔
# metadata authoring 입력 형식 차이로 인한 추론 누락을 막습니다.
def _as_string_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


# 함수 설명: `_column_key()`는 대소문자·구분자 차이를 제거한 비교 key를 만듭니다.
def _column_key(value: Any) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


# 함수 설명: `_unique_texts()`는 안내 문구나 파라미터 목록의 빈 값·중복을 제거합니다.
def _unique_texts(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_refinement()`는 LLM이 반환한 보완 필요 정보와 가정을 저장 전 검수 단계까지 보존합니다.
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


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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
class TableCatalogSavingResultNormalizer(Component):
    display_name = "04 테이블 카탈로그 등록 결과 정규화기"
    description = "테이블 카탈로그 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="llm_response", display_name="LLM 응답", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_authoring(getattr(self, "payload", None), getattr(self, "llm_response", "")))
