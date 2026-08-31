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
# 작업자가 기존 카탈로그의 설명만 바꿀 때에는 SQL·컬럼을 다시 쓰게 하지 않는다.
# key를 명시한 경우에만 이후 merge 단계에서 기존 실행 계약과 합칠 수 있도록
# 최소 변경 후보를 표시한다. `db_key` 같은 내부 필드와 혼동하지 않도록
# 일반적인 "key" 단독 표현은 허용하지 않는다.
WORKER_DATASET_KEY_PATTERN = re.compile(
    r"(?im)(?:데이터\s*(?:셋\s*)?키|카탈로그\s*키|dataset\s*key)\s*[:：은는]?\s*([A-Za-z][A-Za-z0-9_-]*)"
)
CATALOG_UPDATE_CUES = ("기존", "수정", "변경", "바꿔", "업데이트", "update")
# value_transform은 source 숫자 단위 자체를 바꿀 때만 필요한 선택 계약이다. 일반
# metric 예시가 prompt에 있었던 탓에 근거 없는 빈/placeholder transform이 생성되는
# 경우가 있어, 원문에 명시적인 변환 근거가 있을 때만 보존한다.
VALUE_TRANSFORM_EVIDENCE_PATTERN = re.compile(
    r"(?ix)"
    r"value\s*[_-]?\s*transform|coerce\s*[_-]?\s*numeric|multiplier|"
    r"\b(?:multiply|multiplied|scale|scaled|times)\b|"
    r"(?:단위\s*(?:변환|환산)|(?:천|만|백만)\s*단위)|"
    r"(?:\d[\d,]*(?:\.\d+)?\s*배)|"
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:을|를|으로)?\s*곱)"
)
# 정제안에 명시된 ``CANONICAL -> PHYSICAL`` 표현은 새 Table Catalog의
# source-local 실행 계약이다. 약한 추출 모델이 오른쪽 physical column까지
# canonical 이름으로 되풀이해도, SQL 최종 SELECT가 physical column을 실제로
# 반환한다는 것이 증명될 때만 아래 단서로 복원한다. 이 패턴만으로 임의의
# 별칭을 추측하지 않도록, 복원 후보는 이후 projection 결과와 다시 대조한다.
EXPLICIT_COLUMN_MAPPING_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9_])([A-Z][A-Z0-9_]*)\s*(?:->|→)\s*([A-Z][A-Z0-9_]*)(?![A-Z0-9_])"
)

# 주요 함수: LLM 등록 후보 JSON을 추출·검증해 저장 전 표준 items 배열로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def normalize_authoring(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    items = []
    errors = []
    assumptions: list[str] = []
    sql_projection_traces: list[dict[str, Any]] = []
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
        item_assumptions, projection_trace = _normalize_natural_catalog_contract(
            item["payload"],
            source_text,
        )
        assumptions.extend(item_assumptions)
        if projection_trace:
            sql_projection_traces.append(
                {
                    "dataset_key": str(item.get("dataset_key") or "").strip(),
                    **projection_trace,
                }
            )
        partial_update = _usage_description_partial_update(item, source_text)
        if partial_update is not None:
            item = partial_update
            # 기존 실행 계약은 05의 exact-key 조회 결과와 07의 merge 단계에서만
            # 복원한다. 여기에서 LLM의 unknown/빈 source 설정을 저장하면 안 된다.
            assumptions.append(
                "기존 카탈로그의 사용·제외 설명만 갱신하도록 최소 병합 후보로 정리했습니다."
            )
        items.append(item)
    if not items:
        recovered = _recover_usage_description_partial_update(source_text, parsed)
        if recovered is not None:
            items.append(recovered)
            assumptions.append(
                "기존 카탈로그의 사용·제외 설명만 갱신하도록 최소 병합 후보로 정리했습니다."
            )
    next_payload = payload
    next_payload["items"] = items
    next_payload["refinement"] = _refinement(payload, parsed)
    if any(_is_partial_usage_update(item) for item in items):
        # 정확한 기존 key와 선택 설명이 있는 merge 후보는 원본 schema를 다시
        # 요구할 필요가 없다. 07이 exact existing item을 확인하지 못하면 저장은
        # 계속 fail-closed 된다.
        next_payload["refinement"]["needs_more_input"] = False
        next_payload["refinement"]["missing_information"] = []
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
    trace = next_payload.setdefault("trace", {})
    trace["generated_items_preview"] = [{"key": item.get("dataset_key", ""), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    trace["sql_result_projection"] = sql_projection_traces
    return next_payload


# 함수 설명: `_usage_description_partial_update()`는 작업자가 기존 데이터의 사용 설명만
# 바꾸려는 경우 모델이 만든 빈 SQL/unknown source skeleton을 저장하지 않도록 최소 변경으로 접습니다.
def _usage_description_partial_update(
    item: dict[str, Any], source_text: str
) -> dict[str, Any] | None:
    dataset_key = _worker_dataset_key(source_text)
    if not dataset_key or not _is_usage_description_update(source_text):
        return None
    item_key = str(item.get("dataset_key") or item.get("key") or "").strip()
    if item_key and _column_key(item_key) != _column_key(dataset_key):
        return None
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    criteria = _selection_criteria_update(payload.get("selection_criteria"))
    if not criteria:
        return None
    return {
        "dataset_key": dataset_key,
        "status": "active",
        "payload": {"selection_criteria": criteria},
        "_partial_update": {"fields": ["selection_criteria"], "reason": "worker_usage_description"},
    }


# 함수 설명: `_recover_usage_description_partial_update()`는 약한 모델이 items를 비워도
# 원문에 명시된 key와 이미 구조화된 selection_criteria가 있을 때만 최소 update를 복원합니다.
def _recover_usage_description_partial_update(
    source_text: str, parsed: dict[str, Any]
) -> dict[str, Any] | None:
    if not _is_usage_description_update(source_text):
        return None
    dataset_key = _worker_dataset_key(source_text)
    if not dataset_key:
        return None
    raw_criteria = parsed.get("selection_criteria") if isinstance(parsed, dict) else None
    criteria = _selection_criteria_update(raw_criteria)
    if not criteria:
        return None
    return {
        "dataset_key": dataset_key,
        "status": "active",
        "payload": {"selection_criteria": criteria},
        "_partial_update": {"fields": ["selection_criteria"], "reason": "worker_usage_description"},
    }


# 함수 설명: 작업자 입력의 명시적인 기존 카탈로그 설명 갱신 의도인지 판정합니다.
def _is_usage_description_update(source_text: Any) -> bool:
    text = str(source_text or "")
    lowered = text.casefold()
    if not _worker_dataset_key(text):
        return False
    if not any(cue in lowered for cue in CATALOG_UPDATE_CUES):
        return False
    return any(cue in lowered for cue in ("사용", "조회", "물어", "제외", "쓰지", "않아", "설명"))


# 함수 설명: 자연어에서 명시한 dataset key만 안전하게 읽습니다.
def _worker_dataset_key(source_text: Any) -> str:
    match = WORKER_DATASET_KEY_PATTERN.search(str(source_text or ""))
    return str(match.group(1) or "").strip() if match else ""


# 함수 설명: 기존 카탈로그의 실행 계약을 건드리지 않고 사용·제외 문구만 보존합니다.
def _selection_criteria_update(value: Any) -> dict[str, list[str]]:
    raw = value if isinstance(value, dict) else {}
    result: dict[str, list[str]] = {}
    for field in ("use_when", "exclude_when"):
        values = _as_string_list(raw.get(field))
        if values:
            result[field] = _unique_texts(values)
    return result


# 함수 설명: 07 writer가 partial merge인지 판정할 때 사용하는 내부 marker입니다.
def _is_partial_usage_update(item: Any) -> bool:
    marker = item.get("_partial_update") if isinstance(item, dict) else {}
    return (
        isinstance(marker, dict)
        and marker.get("reason") == "worker_usage_description"
        and marker.get("fields") == ["selection_criteria"]
    )


# 함수 설명: `_normalize_natural_catalog_contract()`는 작업자가 기술 구조를 쓰지 않아도
# 명확한 자연어의 파생 지표와 직전 결과 연계 의도를 실행 가능한 Catalog 계약으로 보강합니다.
def _normalize_natural_catalog_contract(
    payload: dict[str, Any],
    source_text: str,
) -> tuple[list[str], dict[str, Any]]:
    # LLM authoring responses occasionally repeat SELECT columns.  Keep the
    # Catalog schema deterministic before derived metric/default validation so
    # a repeated field cannot create a different contract for the same table.
    payload["columns"] = _catalog_columns(payload.get("columns"))
    # Keep the LLM-declared identifier set as an ambiguity guard for natural
    # count recovery.  Projection reconciliation may remove a stale/inner
    # identifier, but that must not turn an otherwise ambiguous "건수" request
    # into an unrequested nunique inference.
    declared_identifier_guard_columns = list(payload["columns"])
    projection_assumptions, projection_trace = _reconcile_sql_result_columns(payload, source_text)
    # The projection reconciliation is intentionally first: metric/detail and
    # follow-up binding inference must see the same final result schema that
    # the retrieval query returns, not an inner CTE/subquery column list.
    assumptions = _normalize_metric_semantics_from_natural_text(
        payload,
        source_text,
        identifier_guard_columns=declared_identifier_guard_columns,
    )
    _infer_previous_result_binding(payload, source_text)
    assumptions.extend(projection_assumptions)
    return _unique_texts(assumptions), projection_trace


# 함수 설명: `_normalize_metric_semantics_from_natural_text()`는 "LOT_ID 고유 건수"처럼
# 결과 이름이 아닌 실제 source 컬럼을 기준으로 metric_semantics를 정규화합니다.
def _normalize_metric_semantics_from_natural_text(
    payload: dict[str, Any],
    source_text: str,
    *,
    identifier_guard_columns: list[str] | None = None,
) -> list[str]:
    raw_semantics = payload.get("metric_semantics")
    if raw_semantics not in (None, "", {}) and not isinstance(raw_semantics, dict):
        return []

    prose = _authoring_prose(source_text)
    execution_columns = _execution_column_index(payload)
    if not execution_columns:
        return []

    count_columns = _count_identifier_columns(
        prose,
        execution_columns,
        identifier_guard_columns=identifier_guard_columns,
    )
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
        metric_contract, transform_assumption = _normalize_metric_value_transform(
            raw_contract,
            prose,
            metric,
        )
        if transform_assumption:
            assumptions.append(transform_assumption)
        target = execution_columns.get(_column_key(metric))
        source_hint = str(
            metric_contract.get("source_column")
            or metric_contract.get("column")
            or metric_contract.get("source")
            or ""
        ).strip()
        if not target and source_hint:
            target = execution_columns.get(_column_key(source_hint))
        if not target and _looks_like_count_metric(metric, metric_contract) and len(count_columns) == 1:
            target = count_columns[0]
        if not target and _looks_like_quantity_metric(metric, metric_contract) and len(sum_columns) == 1:
            target = sum_columns[0]
        if target:
            _merge_metric_contract(normalized, target, _sanitized_metric_contract(metric_contract))
            continue
        if _looks_like_derived_metric(metric, metric_contract) and (count_columns or sum_mentions):
            assumptions.append(
                f"'{metric}'은 결과 이름이므로 실제 조회 컬럼으로 저장하지 않았습니다. "
                "쿼리에 포함된 실제 컬럼으로만 계산 규칙을 연결했습니다."
            )
            continue
        normalized[metric] = deepcopy(metric_contract)

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


# 함수 설명: `_normalize_metric_value_transform()`은 LLM이 일반 JSON 예시를 따라
# value_transform을 채웠더라도 원문에 변환 근거가 없으면 제거합니다. 반대로 작업자가
# 단위 환산/배수를 명시했으면 값이 잘못되어도 조용히 버리지 않고 Review Writer가
# 정확한 계약 오류로 안내하도록 보존합니다.
def _normalize_metric_value_transform(
    raw_contract: dict[str, Any],
    source_text: str,
    metric: str,
) -> tuple[dict[str, Any], str]:
    contract = deepcopy(raw_contract)
    if "value_transform" not in contract:
        return contract, ""
    if _source_text_declares_value_transform(source_text):
        return contract, ""
    contract.pop("value_transform", None)
    metric_name = str(metric or "해당 metric").strip() or "해당 metric"
    return (
        contract,
        f"'{metric_name}'의 value_transform은 원문에 단위 변환 근거가 없어 적용하지 않았습니다.",
    )


# 함수 설명: 단순 수량/집계 표현과 실제 source 값 변환 지시를 구분합니다. SQL 본문을
# 제외한 작업자 설명에서만 확인하므로 SELECT 수식이나 컬럼명 때문에 transform을 유지하지 않습니다.
def _source_text_declares_value_transform(source_text: str) -> bool:
    return bool(VALUE_TRANSFORM_EVIDENCE_PATTERN.search(str(source_text or "")))


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
            # An explicit filter mapping is this source's execution contract.
            # Let it override the physical SELECT name above, so a model that
            # emits both ``EQUIP_ID`` and ``EQP_ID`` produces one metric
            # contract rather than two aliases for the same source column.
            # Unmapped SELECT columns keep their original physical names.
            result[_column_key(alias)] = canonical_text
    return result


# 함수 설명: `_reconcile_sql_result_columns()`는 SQL 안의 모든 식별자를 섞어 쓰지 않고,
# 가장 바깥 SELECT가 실제로 반환하는 컬럼만 안전하게 추출해 Table Catalog 결과 스키마를
# 정리합니다. SELECT *·동적 SQL·해석 불가 표현은 기존 선언을 보존하는 fail-soft 경로입니다.
def _reconcile_sql_result_columns(
    payload: dict[str, Any],
    source_text: str = "",
) -> tuple[list[str], dict[str, Any]]:
    source_config = _dict(payload.get("source_config"))
    query = str(source_config.get("query_template") or "").strip()
    if not query:
        return [], {"status": "not_applicable"}

    projection = _outer_select_projection(query)
    if projection.get("status") != "resolved":
        reason = str(projection.get("reason") or "unresolved").strip()
        return (
            [],
            {"status": "preserved", "reason": reason},
        )

    projected_columns = _catalog_columns(projection.get("columns"))
    if not projected_columns:
        return [], {"status": "preserved", "reason": "projection_empty"}
    declared_columns = _catalog_columns(payload.get("columns"))
    source_to_output = _dict(projection.get("source_to_output"))
    ambiguous_source_columns = _as_string_list(projection.get("ambiguous_source_columns"))
    proposed_mappings, mapping_changes, query_only_changes, unresolved_mappings = _reconcile_filter_mappings_to_projection(
        payload,
        projected_columns,
        source_to_output,
        source_text,
    )
    unresolved_contract_columns = _projection_contract_columns_not_resolved(
        payload,
        projected_columns,
        proposed_mappings,
    )
    if unresolved_mappings or unresolved_contract_columns:
        details = _unique_texts([*unresolved_mappings, *unresolved_contract_columns])
        return (
            [],
            {
                "status": "preserved",
                "reason": "unproven_output_contract",
                "projected_columns": projected_columns,
                "unresolved_columns": details,
                "ambiguous_source_columns": ambiguous_source_columns,
            },
        )

    assumptions: list[str] = []
    # The final SELECT alias is the actual DataFrame schema.  A normalized-key
    # comparison would incorrectly retain e.g. ``OPERNAME`` when the SQL
    # returns ``OPER_NAME``; use the exact ordered projection instead.
    if declared_columns != projected_columns:
        payload["columns"] = projected_columns
        assumptions.append(
            "SQL의 최종 SELECT 반환 컬럼을 기준으로 columns 계약을 정리했습니다: "
            + ", ".join(projected_columns)
        )
    if isinstance(payload.get("filter_mappings"), dict):
        payload["filter_mappings"] = proposed_mappings
    if mapping_changes:
        assumptions.append(
            "최종 SELECT alias를 반영해 조회 후 filter_mappings를 정리했습니다: "
            + "; ".join(mapping_changes)
        )
    if query_only_changes:
        assumptions.append(
            "SQL 내부 필수 조회 조건은 최종 결과 컬럼이 아니어도 사용 가능하므로 "
            "required_param_mappings에 유지하고 조회 후 filter_mappings에서는 제외했습니다: "
            + "; ".join(query_only_changes)
        )
    return (
        assumptions,
        {
            "status": "reconciled",
            "projected_columns": projected_columns,
            "mapping_changes": mapping_changes,
            "query_only_mapping_changes": query_only_changes,
            "ambiguous_source_columns": ambiguous_source_columns,
        },
    )


# 함수 설명: `_reconcile_filter_mappings_to_projection()`는 조회 후 pandas filter에 필요한
# mapping만 최종 SELECT 결과 컬럼으로 맞춥니다. SQL placeholder/WHERE용 required param은
# 내부 subquery 컬럼을 참조할 수 있으므로 결과 스키마와 강제로 동일시하지 않습니다.
def _reconcile_filter_mappings_to_projection(
    payload: dict[str, Any],
    projected_columns: list[str],
    source_to_output: dict[str, Any],
    source_text: str = "",
) -> tuple[dict[str, list[str]], list[str], list[str], list[str]]:
    raw_mappings = payload.get("filter_mappings")
    if not isinstance(raw_mappings, dict):
        return {}, [], [], []

    output_by_key = {
        _column_key(column): column
        for column in projected_columns
        if _column_key(column)
    }
    required_aliases = _required_param_aliases_by_canonical(payload)
    explicit_source_mappings = _explicit_source_local_filter_mappings(source_text)
    normalized: dict[str, list[str]] = {}
    mapping_changes: list[str] = []
    query_only_changes: list[str] = []
    unresolved_mappings: list[str] = []

    for raw_canonical, raw_aliases in raw_mappings.items():
        canonical = str(raw_canonical or "").strip()
        aliases = _as_string_list(raw_aliases)
        if not canonical or not aliases:
            normalized[canonical] = aliases
            continue

        canonical_key = _column_key(canonical)
        query_param_keys = required_aliases.get(canonical_key, set())
        explicit_output_aliases = _unique_texts(
            [
                output_by_key[_column_key(source_column)]
                for source_column in explicit_source_mappings.get(canonical_key, [])
                if _column_key(source_column) in output_by_key
            ]
        )
        retained: list[str] = []
        removed_query_only: list[str] = []
        for alias in aliases:
            alias_key = _column_key(alias)
            output_alias = output_by_key.get(alias_key)
            if output_alias:
                retained.append(output_alias)
                continue

            projected_alias = str(source_to_output.get(alias_key) or "").strip()
            if projected_alias and _column_key(projected_alias) in output_by_key:
                output_alias = output_by_key[_column_key(projected_alias)]
                retained.append(output_alias)
                mapping_changes.append(f"{canonical}: {alias} → {output_alias}")
                continue

            # Some weak extraction responses preserve the canonical key but
            # incorrectly repeat it on the right side (for example
            # ``DEN -> DEN`` instead of ``DEN -> DENSITY``).  The refined
            # authoring text can repair that safely only when it declares one
            # source-local physical column and the final SELECT actually
            # returns that column.  Ambiguous or unproven text is deliberately
            # left to the normal validation path below.
            if len(explicit_output_aliases) == 1:
                output_alias = explicit_output_aliases[0]
                retained.append(output_alias)
                mapping_changes.append(
                    f"{canonical}: {alias} → {output_alias} (정제안의 명시 source mapping)"
                )
                continue

            if alias_key and alias_key in query_param_keys:
                removed_query_only.append(alias)
                continue
            retained.append(alias)
            unresolved_mappings.append(f"{canonical} -> {alias}")

        retained = _unique_texts(retained)
        if retained:
            normalized[canonical] = retained
        elif removed_query_only:
            query_only_changes.append(f"{canonical}: {', '.join(removed_query_only)}")
        else:
            normalized[canonical] = aliases

    return (
        normalized,
        _unique_texts(mapping_changes),
        _unique_texts(query_only_changes),
        _unique_texts(unresolved_mappings),
    )


# 함수 설명: `_explicit_source_local_filter_mappings()`는 정제안에 그대로 남은
# ``CANONICAL -> PHYSICAL`` 매핑만 읽습니다. 이 값은 LLM 후보의 물리 컬럼이
# canonical 이름으로 잘못 반복되었을 때의 복원 단서일 뿐, SQL projection으로
# 확인되지 않으면 실행 계약을 변경하지 않습니다.
def _explicit_source_local_filter_mappings(source_text: Any) -> dict[str, list[str]]:
    mappings: dict[str, list[str]] = {}
    for match in EXPLICIT_COLUMN_MAPPING_PATTERN.finditer(str(source_text or "")):
        canonical = str(match.group(1) or "").strip()
        physical = str(match.group(2) or "").strip()
        canonical_key = _column_key(canonical)
        if not canonical_key or not physical:
            continue
        values = mappings.setdefault(canonical_key, [])
        if not any(_column_key(value) == _column_key(physical) for value in values):
            values.append(physical)
    return mappings


# 함수 설명: `_projection_contract_columns_not_resolved()`는 최종 SELECT로 columns를
# 바꿨을 때 기존 metric/detail 계약이 새 결과 schema에서 사라지는지 사전에 확인합니다.
# 확실히 매핑할 수 없는 값은 오류를 새로 만들지 않고 기존 선언을 보존합니다.
def _projection_contract_columns_not_resolved(
    payload: dict[str, Any],
    projected_columns: list[str],
    proposed_mappings: dict[str, list[str]],
) -> list[str]:
    available_keys = {
        _column_key(column)
        for column in projected_columns
        if _column_key(column)
    }
    canonical_keys = {
        _column_key(canonical)
        for canonical in proposed_mappings
        if _column_key(canonical)
    }
    unresolved: list[str] = []
    semantics = payload.get("metric_semantics")
    if isinstance(semantics, dict):
        for metric in semantics:
            metric_key = _column_key(metric)
            if metric_key and metric_key not in available_keys and metric_key not in canonical_keys:
                unresolved.append(str(metric))
    for column in _as_string_list(payload.get("default_detail_columns")):
        column_key = _column_key(column)
        if column_key and column_key not in available_keys and column_key not in canonical_keys:
            unresolved.append(column)
    return _unique_texts(unresolved)


# 함수 설명: `_required_param_aliases_by_canonical()`는 SQL 조회 입력 계약만 모읍니다.
# 이 목록은 최종 SELECT 결과에 없더라도 유효한 nested WHERE/placeholder 컬럼입니다.
def _required_param_aliases_by_canonical(payload: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    contracts = [payload, _dict(payload.get("source_config"))]
    for contract in contracts:
        mappings = contract.get("required_param_mappings")
        if not isinstance(mappings, dict):
            continue
        for raw_canonical, raw_aliases in mappings.items():
            canonical_key = _column_key(raw_canonical)
            if not canonical_key:
                continue
            values = result.setdefault(canonical_key, set())
            values.update(
                _column_key(alias)
                for alias in _as_string_list(raw_aliases)
                if _column_key(alias)
            )
    return result


# 함수 설명: `_outer_select_projection()`는 괄호·문자열·주석 깊이를 인식해 CTE/subquery
# 내부 SELECT가 아닌 최상위 결과 SELECT의 projection만 읽습니다. 정확도가 보장되지 않는
# 경우에는 상태만 반환해 호출자가 기존 Catalog 선언을 보존할 수 있게 합니다.
def _outer_select_projection(query: str) -> dict[str, Any]:
    select_index = _top_level_sql_keyword(query, "SELECT")
    if select_index < 0:
        return {"status": "unresolved", "reason": "최상위 SELECT를 찾지 못함"}
    from_index = _top_level_sql_keyword(query, "FROM", start=select_index + len("SELECT"))
    if from_index < 0:
        return {"status": "unresolved", "reason": "최상위 FROM을 찾지 못함"}

    select_body = query[select_index + len("SELECT") : from_index]
    items = _split_top_level_sql_list(select_body)
    if not items:
        return {"status": "unresolved", "reason": "SELECT projection이 비어 있음"}

    columns: list[str] = []
    source_to_output: dict[str, str] = {}
    ambiguous_source_keys: set[str] = set()
    for item in items:
        parsed = _select_item_projection(item)
        if parsed is None:
            return {
                "status": "unresolved",
                "reason": "SELECT * 또는 별칭을 확정할 수 없는 projection 포함",
            }
        output = str(parsed.get("output") or "").strip()
        output_key = _column_key(output)
        if not output or not output_key or any(_column_key(column) == output_key for column in columns):
            return {
                "status": "unresolved",
                "reason": "중복되었거나 해석 불가한 최종 SELECT 컬럼",
            }
        columns.append(output)
        source = str(parsed.get("source") or "").strip()
        source_key = _column_key(source)
        if source_key and source_key not in ambiguous_source_keys:
            prior_output = source_to_output.get(source_key)
            if prior_output and _column_key(prior_output) != output_key:
                # The qualifier is intentionally removed for normal aliases,
                # but that makes ``a.STATUS`` and ``b.STATUS`` indistinguish-
                # able.  Do not guess which final alias a legacy STATUS
                # mapping means; leave that mapping untouched instead.
                source_to_output.pop(source_key, None)
                ambiguous_source_keys.add(source_key)
            else:
                source_to_output.setdefault(source_key, output)

    return {
        "status": "resolved",
        "columns": columns,
        "source_to_output": source_to_output,
        "ambiguous_source_columns": sorted(ambiguous_source_keys),
    }


# 함수 설명: `_top_level_sql_keyword()`는 quoted text/comment/괄호 안의 키워드를 무시하고
# 0-depth SQL 키워드 위치만 반환합니다. 정규식으로 모든 SELECT를 훑어 서브쿼리 컬럼을
# 결과 schema로 오인하던 문제를 피하기 위한 작은 parser입니다.
def _top_level_sql_keyword(sql: str, keyword: str, start: int = 0) -> int:
    target = str(keyword or "").upper()
    index = max(0, int(start or 0))
    depth = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in {"'", '"', "`"}:
            index = _skip_sql_quote(sql, index, character)
            continue
        if character == "[":
            index = _skip_sql_bracket_identifier(sql, index)
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
            continue
        if character == "(":
            depth += 1
            index += 1
            continue
        if character == ")":
            depth -= 1
            if depth < 0:
                return -1
            index += 1
            continue
        if depth == 0 and (character.isalpha() or character == "_"):
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] in {"_", "$", "#"}):
                end += 1
            if sql[index:end].upper() == target:
                return index
            index = end
            continue
        index += 1
    return -1


# 함수 설명: `_split_top_level_sql_list()`는 함수/CASE/subquery의 쉼표를 보존하면서
# 최상위 SELECT projection 항목만 분리합니다.
def _split_top_level_sql_list(value: str) -> list[str]:
    result: list[str] = []
    start = 0
    index = 0
    depth = 0
    length = len(value)
    while index < length:
        character = value[index]
        if character in {"'", '"', "`"}:
            index = _skip_sql_quote(value, index, character)
            continue
        if character == "[":
            index = _skip_sql_bracket_identifier(value, index)
            continue
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            closing = value.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return []
        elif character == "," and depth == 0:
            item = value[start:index].strip()
            if item:
                result.append(item)
            start = index + 1
        index += 1
    if depth != 0:
        return []
    item = value[start:].strip()
    if item:
        result.append(item)
    return result


# 함수 설명: `_select_item_projection()`는 한 projection의 최종 alias와 직접 source 컬럼
# (확실한 단순 identifier일 때만)를 분리합니다. 복합 expression은 AS/암시 alias가 있어야
# 안전하게 최종 결과명을 알 수 있습니다.
def _select_item_projection(item: str) -> dict[str, str] | None:
    expression = _strip_sql_comments(item).strip()
    expression = re.sub(r"(?is)^(?:DISTINCT|ALL)\s+", "", expression).strip()
    if not expression or _is_select_wildcard(expression):
        return None

    as_index = _top_level_sql_keyword(expression, "AS")
    if as_index >= 0:
        source_expression = expression[:as_index].strip()
        output = _parse_sql_identifier(expression[as_index + len("AS") :])
        if not source_expression or not output or _is_select_wildcard(source_expression):
            return None
        return {"output": output, "source": _simple_sql_source_column(source_expression)}

    source = _simple_sql_source_column(expression)
    if source:
        return {"output": source, "source": source}

    implicit = _implicit_sql_alias(expression)
    if implicit is None:
        return None
    source_expression, output = implicit
    if _is_select_wildcard(source_expression):
        return None
    return {"output": output, "source": _simple_sql_source_column(source_expression)}


def _implicit_sql_alias(expression: str) -> tuple[str, str] | None:
    text = expression.strip()
    match = re.match(
        r'(?is)^(.*\S)\s+((?:"(?:[^"]|"")+")|(?:\[(?:[^\]]|\]\])+\])|(?:`[^`]+`)|(?:[A-Za-z_][A-Za-z0-9_$#]*))$',
        text,
    )
    if not match:
        return None
    source_expression = str(match.group(1) or "").strip()
    output = _parse_sql_identifier(match.group(2))
    if not source_expression or not output:
        return None
    if re.search(r"[+\-*/=<>|,]\s*$", source_expression):
        return None
    return source_expression, output


def _simple_sql_source_column(expression: str) -> str:
    text = _strip_sql_comments(expression).strip()
    pattern = r'(?:"(?:[^"]|"")+"|\[(?:[^\]]|\]\])+\]|`[^`]+`|[A-Za-z_][A-Za-z0-9_$#]*)(?:\s*\.\s*(?:"(?:[^"]|"")+"|\[(?:[^\]]|\]\])+\]|`[^`]+`|[A-Za-z_][A-Za-z0-9_$#]*))*'
    if not re.fullmatch(pattern, text):
        return ""
    last = re.split(r"\s*\.\s*", text)[-1]
    return _parse_sql_identifier(last)


def _parse_sql_identifier(value: Any) -> str:
    text = _strip_sql_comments(str(value or "")).strip()
    if not text:
        return ""
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1].replace('""', '"').strip()
    if text.startswith("[") and text.endswith("]") and len(text) >= 2:
        return text[1:-1].replace("]]", "]").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1].strip()
    return text if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", text) else ""


def _is_select_wildcard(expression: str) -> bool:
    text = _strip_sql_comments(expression).strip()
    if text == "*":
        return True
    return bool(
        re.fullmatch(
            r'(?:"(?:[^"]|"")+"|\[(?:[^\]]|\]\])+\]|`[^`]+`|[A-Za-z_][A-Za-z0-9_$#]*)\s*\.\s*\*',
            text,
        )
    )


def _strip_sql_comments(value: str) -> str:
    result: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            result.append(" ")
            continue
        if value.startswith("/*", index):
            closing = value.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
            result.append(" ")
            continue
        quote = value[index]
        if quote in {"'", '"', "`"}:
            end = _skip_sql_quote(value, index, quote)
            result.append(value[index:end])
            index = end
            continue
        result.append(value[index])
        index += 1
    return "".join(result)


def _skip_sql_quote(value: str, index: int, quote: str) -> int:
    length = len(value)
    index += 1
    while index < length:
        if value[index] != quote:
            index += 1
            continue
        if index + 1 < length and value[index + 1] == quote:
            index += 2
            continue
        return index + 1
    return length


def _skip_sql_bracket_identifier(value: str, index: int) -> int:
    length = len(value)
    index += 1
    while index < length:
        if value[index] != "]":
            index += 1
            continue
        if index + 1 < length and value[index + 1] == "]":
            index += 2
            continue
        return index + 1
    return length


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
def _count_identifier_columns(
    prose: str,
    execution_columns: dict[str, str],
    *,
    identifier_guard_columns: list[str] | None = None,
) -> list[str]:
    result: list[str] = []
    lowered = prose.casefold()
    for match in IDENTIFIER_PATTERN.finditer(prose):
        context = lowered[max(0, match.start() - 80) : min(len(prose), match.end() + 80)]
        if not any(cue in context for cue in COUNT_CUES):
            continue
        target = execution_columns.get(_column_key(match.group(1)))
        if target and target not in result:
            result.append(target)
    if result or not any(cue in lowered for cue in COUNT_CUES):
        return result

    # Workers often say "LOT 번호를 중복 없이" rather than spelling the
    # physical identifier as LOT_ID.  Recover only when the natural identifier
    # word uniquely points at one registered *_ID execution column.  This is a
    # schema-backed fallback, not a dataset- or domain-specific rule.
    normalized_prose = _column_key(prose)
    identifier_columns: list[tuple[str, str]] = []
    seen: set[str] = set()
    for physical in execution_columns.values():
        normalized = _column_key(physical)
        if not normalized.endswith("ID") or normalized in seen:
            continue
        seen.add(normalized)
        identifier_columns.append((normalized[:-2], physical))
    matches = [
        column
        for stem, column in identifier_columns
        if stem and stem in normalized_prose
    ]
    if len(matches) == 1:
        return matches
    raw_guard_columns = (
        identifier_guard_columns
        if isinstance(identifier_guard_columns, list) and any(
            _column_key(column).endswith("ID") for column in identifier_guard_columns
        )
        else list(execution_columns.values())
    )
    guard_identifiers = {
        _column_key(column)
        for column in raw_guard_columns
        if _column_key(column).endswith("ID")
    }
    # A final SELECT correction must not erase a second identifier that was
    # declared by the candidate and thereby make an unmentioned count target
    # look uniquely determined.  Explicit LOT/DEVICE wording above still
    # resolves against the actual final result schema.
    if len(identifier_columns) == 1 and len(guard_identifiers) == 1:
        return [identifier_columns[0][1]]
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
    # Gemini와 일부 Langflow provider는 content를 text block 배열로 반환한다.
    # 첫 JSON text block만 읽어 Message.text 변환 유무에 따른 저장 Flow 차이를 없앤다.
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "text":
                continue
            text = str(item.get("text") or "")
            if text.strip():
                return _json(text)
        return {}
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
