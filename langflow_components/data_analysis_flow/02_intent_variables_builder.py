# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 의도 분석 변수 생성기
# 역할: Langflow 프롬프트 템플릿과 에이전트/LLM에 연결할 의도 분석 변수를 제공합니다.
# 주요 입력: 페이로드 (payload) · 필수, 메타데이터 후보 (metadata_candidates_in)
# 주요 출력: 사용자 질문 (question), 상태/요청 컨텍스트 JSON (state_summary), 메타데이터 후보 JSON (metadata_candidates), 출력 스키마 JSON
#        (output_schema)
# 처리 흐름: 의도 LLM에 필요한 질문·이전 상태·후보 메타데이터·출력 스키마만 각각의 Message로 분리합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

RETIRED_DETAIL_CONTRACT_KEYS = {"row_identity_columns", "context_columns"}
MODEL_INTERNAL_CATALOG_KEYS = {
    "columns",
    "filter_mappings",
    "required_param_mappings",
    "standard_column_aliases",
    "source_config",
}
MODEL_INTERNAL_ITEM_KEYS = {
    "_id",
    "created_at",
    "registration_trace",
    "status",
    "updated_at",
}

# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any, metadata_candidates_value: Any = None) -> dict[str, Any]:
    payload = _payload(payload_value)
    metadata_candidates = _metadata_candidates_for_model(
        _without_retired_table_catalog_contract(
            _compact_metadata_candidates(_payload(metadata_candidates_value) or {})
        )
    )
    return {
        "question": payload.get("request", {}).get("question", ""),
        "state_summary": _compact_json(_without_retired_intent_contract(_state_summary(payload))),
        "metadata_candidates": _compact_json(metadata_candidates),
        "output_schema": _compact_json(_schema()),
    }


# 함수 설명: `_compact_json()`는 JSON에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# 함수 설명: `_state_summary()`는 요약의 건수·조건·상태를 진단과 답변에 쓸 짧은 요약으로 만듭니다.
def _state_summary(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    followup_hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    previous_state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    # 독립 질문에는 직전 retrieval job·source alias·data_ref를 모델에 노출하지 않습니다.
    # 세션 상태는 실제 후속 후보일 때만 전달해 이전 데이터셋의 무의미한 재조회를 차단합니다.
    state_for_model = _compact_state(previous_state) if followup_hint.get("followup_candidate") is True else {}
    request_context = {
        "reference_date": request.get("reference_date", ""),
        "previous_date": _previous_date(request.get("reference_date")),
    }
    date_mentions = _date_mentions_from_followup_hint(followup_hint)
    if date_mentions:
        request_context["date_mentions"] = date_mentions
    summary = {
        "request_context": request_context,
        "followup_hint": followup_hint,
        "state": state_for_model,
    }
    orchestration = _compact_orchestration(payload.get("orchestration"))
    if orchestration:
        summary["orchestration"] = orchestration
    return summary


# 함수 설명: 기준일이 유효한 YYYYMMDD일 때 LLM이 날짜 산술을 추정하지 않도록 전일을 함께 제공합니다.
def _previous_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return (datetime.strptime(text, "%Y%m%d") - timedelta(days=1)).strftime(
            "%Y%m%d"
        )
    except ValueError:
        return ""


# 함수 설명: 01E가 해석한 질문 날짜와 각 날짜의 전일을 의도 LLM에 구조화해 전역 previous_date 오사용을 막습니다.
def _date_mentions_from_followup_hint(followup_hint: dict[str, Any]) -> list[dict[str, Any]]:
    changed = (
        followup_hint.get("changed_conditions_hint")
        if isinstance(followup_hint.get("changed_conditions_hint"), dict)
        else {}
    )
    date_hint = changed.get("date") if isinstance(changed.get("date"), dict) else {}
    raw_mentions = date_hint.get("mentions") if isinstance(date_hint.get("mentions"), list) else []
    if not raw_mentions and date_hint:
        raw_mentions = [date_hint]
    result: list[dict[str, Any]] = []
    for item in raw_mentions:
        if not isinstance(item, dict):
            continue
        mention = {
            key: deepcopy(item.get(key))
            for key in ("expression", "resolved_value", "previous_value", "position")
            if item.get(key) not in (None, "")
        }
        if mention and mention not in result:
            result.append(mention)
    return result


# 함수 설명: `_compact_orchestration()`은 상위 Tool 결과가 있다는 사실과 고정 alias만 의도 LLM에 짧게 알립니다.
# 전체 이전 결과 행이나 MongoDB 설정은 노출하지 않아 입력 토큰과 민감한 실행 정보를 함께 줄입니다.
def _compact_orchestration(value: Any) -> dict[str, Any]:
    orchestration = value if isinstance(value, dict) else {}
    ref = str(orchestration.get("upstream_result_ref") or "").strip()
    if not ref:
        return {}
    return _omit_empty(
        {
            "has_upstream_result": True,
            "source_alias": str(orchestration.get("source_alias") or "upstream_result").strip(),
            "status": orchestration.get("status"),
        }
    )


# 함수 설명: `_schema()`는 의도 분석 LLM이 반환해야 할 JSON 스키마를 작은 dict로 구성합니다.
def _schema() -> dict[str, Any]:
    return {
        "intent_plan": {
            "analysis_kind": "string",
            "request_scope": "new_analysis|followup_requery|followup_transform|followup_expand_source|followup_explain|clarification",
            "reference_mode": "none|previous_result_rows|previous_result_transform|previous_source|previous_filters|previous_trace",
            "condition_resolution": {
                "inherited": {},
                "changed": {},
                "dropped": {},
                "new": {},
                "effective_filters": {
                    "PREVIOUS_SOURCE_ALIAS": {
                        "dataset_key": "직전 source의 dataset_key",
                        "filters": {
                            "PANDAS_FILTER_COLUMN": {
                                "operator": "eq|in|ne|not_in|gt|ge|lt|le|contains|like|starts_with|ends_with|is_null|is_empty|null_or_empty|not_null|not_empty|not_blank",
                                "value": "value or list; omit for valueless operators",
                            }
                        },
                    }
                },
            },
            "temporal_semantics": [
                {
                    "metric": "요청 지표",
                    "business_timepoint": "선택된 Domain의 업무 시점",
                    "requested_date": "YYYYMMDD",
                    "query_date": "YYYYMMDD",
                    "dataset_key": "선택된 Domain의 dataset_key",
                    "date_param": "선택된 Domain의 날짜 파라미터",
                    "requested_date_offset_days": "선택된 Domain의 정수 일수 offset",
                }
            ],
            "pandas_function_cases": [],
            "grain_plan": {
                "metadata_ref": {"section": "string", "key": "string"},
                "source_alias": "string",
            },
            "join_plan": [
                {
                    "metadata_ref": {"section": "string", "key": "string"},
                    "left_source_alias": "string",
                    "right_source_alias": "string",
                    "join_type": "outer|left|inner",
                    "population_policy": "preserve_all_metric_source_keys|left_source_only",
                    "right_value_columns": [],
                    "multi_match_policy": "collect_unique|preserve_rows|first",
                }
            ],
            "retrieval_jobs": [
                {
                    "dataset_key": "string",
                    "source_alias": "string",
                    "required_params": {"DATA_CATALOG_REQUIRED_PARAM": "value"},
                    "filters": {
                        "PANDAS_FILTER_COLUMN": {
                            "operator": "eq|in|ne|not_in|gt|ge|lt|le|contains|like|starts_with|ends_with|is_null|is_empty|null_or_empty|not_null|not_empty|not_blank",
                            "value": "value or list; omit for valueless operators",
                        }
                    },
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "선택적 고유 pandas 단계 ID",
                    "operation": "apply_filters|groupby_and_aggregate|sort_and_top_n|join|compare_presence|compare_group_attributes|find_duplicate_groups|apply_row_match_groups|apply_pandas_function_case",
                    "inputs": [
                        {
                            "kind": "external_source|node_output",
                            "ref": "retrieval source_alias 또는 선행 node_id",
                        }
                    ],
                    "output_alias": "선택적 파생 결과 alias",
                    "source_alias": "분석할 DataFrame alias",
                    "left_source_alias": "존재 기준 또는 join 왼쪽 DataFrame alias",
                    "right_source_alias": "부재 확인 또는 join 오른쪽 DataFrame alias",
                    "left_metric_column": "왼쪽 source의 존재 여부를 판단할 수량 컬럼",
                    "right_metric_column": "오른쪽 source의 존재 여부를 판단할 수량 컬럼",
                    "join_type": "outer|left|inner",
                    "population_policy": "preserve_all_metric_source_keys|left_source_only",
                    "presence_rule": "left_positive_right_missing_or_zero",
                    "group_by": ["같아야 하는 기준 컬럼 또는 중복을 판단할 컬럼"],
                    "comparison_columns": ["기준 그룹 안에서 값 차이를 확인할 컬럼"],
                    "comparison_rule": "any|all",
                    "reference_source_alias": "row match 조건 행을 제공할 reference DataFrame alias",
                    "match_columns": ["previous_result가 아닌 일반 reference source에만 명시할 실제 identity 컬럼"],
                    "blank_policy": "normalize_blank",
                    "agg_column": "집계할 실제 metric 컬럼",
                    "agg_method": "sum|mean|nunique|count|min|max",
                    "aggregations": [
                        {
                            "column": "같은 group_by에서 집계할 실제 컬럼",
                            "method": "sum|mean|nunique|count|min|max|collect_unique",
                            "output_column": "서로 구분되는 결과 컬럼명",
                        }
                    ],
                    "sort_by": "정렬할 결과 metric 컬럼",
                    "order": "asc|desc",
                    "limit": 0,
                }
            ],
            "output_contract": {
                "result_mode": "aggregate|detail|entity_list|scalar|explanation",
                "required_columns": [],
                "grain_columns": [],
                "metric_columns": [],
                "metric_bindings": [
                    {
                        "output_column": "결과 metric 컬럼",
                        "source_alias": "retrieval source alias",
                        "dataset_key": "catalog dataset_key",
                        "source_column": "실제 source metric 컬럼",
                        "aggregation": "sum|mean|nunique|count|min|max|collect_unique",
                    }
                ],
                "result_columns": [],
                "strict_result_columns": True,
                "primary_metric": "답변과 정렬의 대표 metric 컬럼",
                "ordering": {
                    "sort_by": "정렬할 결과 metric 컬럼",
                    "order": "asc|desc",
                    "limit": 0,
                },
                "column_labels": {
                    "RESULT_COLUMN": "질문의 조건과 의미를 반영한 사용자 표시명"
                },
                "result_segments": [
                    {
                        "label": "string",
                        "operation": "top_n|bottom_n|filter|comparison",
                        "limit": 0,
                        "sort_by": "string",
                        "order": "asc|desc",
                    }
                ],
                "null_group_policy": "preserve_as_blank",
                "metric_null_policy": "display_zero",
            },
        },
        "metadata_refs": [{"section": "string", "key": "string"}],
        "trace": {"decision_reason": []},
    }


# 함수 설명: `_compact_metadata_candidates()`는 메타데이터·후보에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_metadata_candidates(value: dict[str, Any]) -> dict[str, Any]:
    candidates = value.get("metadata_candidates") if isinstance(value.get("metadata_candidates"), dict) else value
    result: dict[str, Any] = {}
    for key in ("domain_items", "table_catalog_items", "main_flow_filters", "runtime_function_helpers"):
        item = candidates.get(key) if isinstance(candidates, dict) else None
        if item not in (None, "", [], {}):
            result[key] = deepcopy(item)
    if result:
        return result
    return {
        str(key): deepcopy(item)
        for key, item in candidates.items()
        if key not in {"metadata_candidates", "metadata_load"} and item not in (None, "", [], {})
    } if isinstance(candidates, dict) else {}


# 함수 설명: `_without_retired_table_catalog_contract()`는 table catalog의 이전 상세 표시 필드만 제거하고 Domain metadata는 보존합니다.
def _without_retired_table_catalog_contract(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    items = result.get("table_catalog_items")
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in RETIRED_DETAIL_CONTRACT_KEYS:
            item.pop(key, None)
        payload = item.get("payload")
        if isinstance(payload, dict):
            for key in RETIRED_DETAIL_CONTRACT_KEYS:
                payload.pop(key, None)
    return result


# 함수 설명: `_metadata_candidates_for_model()`은 실행 단계가 받는 원본 메타데이터를 건드리지 않고 LLM에 필요한 의미 계약만 투영합니다.
def _metadata_candidates_for_model(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    table_items = result.get("table_catalog_items")
    if isinstance(table_items, list):
        result["table_catalog_items"] = [
            _table_catalog_item_for_model(item) if isinstance(item, dict) else deepcopy(item)
            for item in table_items
        ]
    main_filters = result.get("main_flow_filters")
    if isinstance(main_filters, list):
        result["main_flow_filters"] = [
            _main_filter_item_for_model(item) if isinstance(item, dict) else deepcopy(item)
            for item in main_filters
        ]
    domain_items = result.get("domain_items")
    if isinstance(domain_items, list):
        result["domain_items"] = [
            _without_internal_item_keys(item) if isinstance(item, dict) else deepcopy(item)
            for item in domain_items
        ]
    return result


# 함수 설명: `_table_catalog_item_for_model()`은 물리 컬럼 매핑의 반복을 canonical 컬럼 목록으로 접고 업무·시간·단위 계약은 보존합니다.
def _table_catalog_item_for_model(item: dict[str, Any]) -> dict[str, Any]:
    projected = _without_internal_item_keys(item)
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    source_config = payload.get("source_config") if isinstance(payload.get("source_config"), dict) else {}
    projected_payload = {
        str(key): deepcopy(raw_value)
        for key, raw_value in payload.items()
        if str(key) not in MODEL_INTERNAL_CATALOG_KEYS
    }

    canonical_columns = _catalog_model_columns(payload)
    if canonical_columns:
        projected_payload["canonical_columns"] = canonical_columns

    required_params = _merge_strings(
        _string_list(payload.get("required_params")),
        _string_list(source_config.get("required_params")),
    )
    if required_params:
        projected_payload["required_params"] = required_params

    upstream_bindings = payload.get("upstream_bindings")
    if upstream_bindings in (None, "", [], {}):
        upstream_bindings = source_config.get("upstream_bindings")
    if upstream_bindings not in (None, "", [], {}):
        projected_payload["upstream_bindings"] = deepcopy(upstream_bindings)

    if projected_payload:
        projected["payload"] = projected_payload
    else:
        projected.pop("payload", None)
    return projected


# 함수 설명: `_catalog_model_columns()`은 카탈로그 매핑의 canonical key와 매핑되지 않은 선언 컬럼을 중복 없이 모델용 목록으로 만듭니다.
def _catalog_model_columns(payload: dict[str, Any]) -> list[str]:
    filter_mappings = payload.get("filter_mappings") if isinstance(payload.get("filter_mappings"), dict) else {}
    standard_aliases = (
        payload.get("standard_column_aliases")
        if isinstance(payload.get("standard_column_aliases"), dict)
        else {}
    )
    metric_semantics = payload.get("metric_semantics") if isinstance(payload.get("metric_semantics"), dict) else {}
    canonical = _merge_strings(
        list(filter_mappings.keys()),
        list(standard_aliases.keys()),
        list(metric_semantics.keys()),
    )
    mapped_physical = {
        _column_signature(name)
        for mapping in (filter_mappings, standard_aliases)
        for raw_value in mapping.values()
        for name in _string_list(raw_value)
        if _column_signature(name)
    }
    unmapped_columns = [
        name
        for name in _declared_column_names(payload.get("columns"))
        if _column_signature(name) not in mapped_physical
    ]
    return _merge_strings(canonical, unmapped_columns)


# 함수 설명: `_declared_column_names()`는 문자열 또는 object 형태의 catalog columns에서 표시 가능한 컬럼명을 읽습니다.
def _declared_column_names(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, (list, tuple, set)) else [value]:
        if isinstance(item, dict):
            name = next(
                (
                    item.get(key)
                    for key in ("canonical_key", "standard_name", "column_name", "name")
                    if str(item.get(key) or "").strip()
                ),
                "",
            )
        else:
            name = item
        text = str(name or "").strip()
        if text:
            result = _merge_strings(result, [text])
    return result


# 함수 설명: `_main_filter_item_for_model()`은 실행용 물리 컬럼 후보만 제외하고 사용자 용어·연산자 의미는 유지합니다.
def _main_filter_item_for_model(item: dict[str, Any]) -> dict[str, Any]:
    projected = _without_internal_item_keys(item)
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if payload:
        projected["payload"] = {
            str(key): deepcopy(raw_value)
            for key, raw_value in payload.items()
            if str(key) != "column_candidates"
        }
    return projected


# 함수 설명: `_without_internal_item_keys()`는 후보 선택 과정의 내부 상태만 모델 입력에서 제거합니다.
def _without_internal_item_keys(item: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): deepcopy(raw_value)
        for key, raw_value in item.items()
        if str(key) not in MODEL_INTERNAL_ITEM_KEYS
    }


# 함수 설명: `_column_signature()`는 물리 컬럼 중복 판별에만 쓰는 대소문자·공백 무시 signature를 만듭니다.
def _column_signature(value: Any) -> str:
    return "".join(str(value or "").strip().casefold().split())


# 함수 설명: `_merge_strings()`는 여러 문자열 목록을 최초 등장 순서대로 합칩니다.
def _merge_strings(*values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _string_list(value):
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


# 함수 설명: `_without_retired_intent_contract()`는 state의 output_contract와 retrieval_jobs에서만 이전 상세 필드를 제거합니다.
def _without_retired_intent_contract(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    state = result.get("state")
    if not isinstance(state, dict):
        return result
    plan = state.get("last_intent_plan")
    if not isinstance(plan, dict):
        return result
    output_contract = plan.get("output_contract")
    if isinstance(output_contract, dict):
        for key in RETIRED_DETAIL_CONTRACT_KEYS:
            output_contract.pop(key, None)
    retrieval_jobs = plan.get("retrieval_jobs")
    if isinstance(retrieval_jobs, list):
        for job in retrieval_jobs:
            if not isinstance(job, dict):
                continue
            for key in RETIRED_DETAIL_CONTRACT_KEYS:
                job.pop(key, None)
    return result


# 함수 설명: `_compact_state()`는 상태에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_state(state: dict[str, Any]) -> dict[str, Any]:
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    result: dict[str, Any] = {}
    if state.get("last_question") or isinstance(state.get("request"), dict):
        result["last_question"] = state.get("last_question") or state.get("request", {}).get("question", "")
    if state.get("last_answer_message"):
        result["last_answer_message"] = _clip_text(state.get("last_answer_message"), 700)
    if current_data:
        result["current_data"] = _omit_empty(
            {
                "row_count": current_data.get("row_count"),
                "columns": _string_list(current_data.get("columns"))[:60],
                "result_columns": _string_list(current_data.get("result_columns"))[:60],
                "source_aliases": _string_list(current_data.get("source_aliases"))[:30],
                "source_dataset_keys": _string_list(current_data.get("source_dataset_keys"))[:30],
                "source_columns_by_alias": _compact_source_columns(current_data.get("source_columns_by_alias")),
                "data_ref": current_data.get("data_ref"),
                "preview_rows": current_data.get("preview_rows") if isinstance(current_data.get("preview_rows"), list) else [],
            }
        )
    for key in ("last_intent_plan", "last_applied_criteria", "runtime_source_refs"):
        value = state.get(key)
        if value not in (None, "", [], {}):
            result[key] = deepcopy(value)
    followup_sources = state.get("followup_source_results")
    if isinstance(followup_sources, list):
        result["followup_source_results"] = deepcopy(followup_sources[:6])
    return _omit_empty(result)


# 함수 설명: `_compact_source_columns()`는 데이터 소스·컬럼에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_source_columns(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(alias): _string_list(columns)[:80]
        for alias, columns in value.items()
        if str(alias or "").strip() and _string_list(columns)
    }


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_clip_text()`는 문자열을 허용 길이 안으로 자르되 비어 있는 값과 말줄임 표시를 일관되게 처리합니다.
def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


# 함수 설명: `_omit_empty()`는 dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.
def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class IntentVariablesBuilder(Component):
    display_name = "02 의도 분석 변수 생성기"
    description = "Langflow 프롬프트 템플릿과 에이전트/LLM에 연결할 의도 분석 변수를 제공합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        DataInput(name="metadata_candidates_in", display_name="메타데이터 후보", required=False),
    ]
    outputs = [
        Output(name="question", display_name="사용자 질문", method="build_question", types=["Message"], group_outputs=True),
        Output(name="state_summary", display_name="상태/요청 컨텍스트 JSON", method="build_state_summary", types=["Message"], group_outputs=True),
        Output(name="metadata_candidates", display_name="메타데이터 후보 JSON", method="build_metadata_candidates", types=["Message"], group_outputs=True),
        Output(name="output_schema", display_name="출력 스키마 JSON", method="build_output_schema", types=["Message"], group_outputs=True),
    ]

    # 함수 설명: `_variables_once()`는 한 vertex 실행에서 여러 group output이 같은 payload를 반복 직렬화하지 않도록 결과를 재사용합니다.
    def _variables_once(self) -> dict[str, Any]:
        payload = getattr(self, "payload", None)
        metadata = getattr(self, "metadata_candidates_in", None)
        cache_key = (id(payload), id(metadata))
        if getattr(self, "_variables_cache_key", None) != cache_key:
            self._variables_cache_key = cache_key
            self._variables_cache = build_variables(payload, metadata)
        return self._variables_cache

    # Langflow 출력 함수: '사용자 질문 (question)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_question(self) -> Message:
        return Message(text=self._variables_once()["question"])

    # Langflow 출력 함수: '상태/요청 컨텍스트 JSON (state_summary)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_state_summary(self) -> Message:
        return Message(text=self._variables_once()["state_summary"])

    # Langflow 출력 함수: '메타데이터 후보 JSON (metadata_candidates)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_metadata_candidates(self) -> Message:
        return Message(text=self._variables_once()["metadata_candidates"])

    # Langflow 출력 함수: '출력 스키마 JSON (output_schema)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_schema(self) -> Message:
        return Message(text=self._variables_once()["output_schema"])
