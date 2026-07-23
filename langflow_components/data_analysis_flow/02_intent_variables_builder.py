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
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

RETIRED_DETAIL_CONTRACT_KEYS = {"row_identity_columns", "context_columns"}

# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any, metadata_candidates_value: Any = None) -> dict[str, Any]:
    payload = _payload(payload_value)
    metadata_candidates = _without_retired_table_catalog_contract(
        _compact_metadata_candidates(_payload(metadata_candidates_value) or {})
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
    summary = {
        "request_context": {
            "reference_date": request.get("reference_date", ""),
        },
        "followup_hint": followup_hint,
        "state": state_for_model,
    }
    orchestration = _compact_orchestration(payload.get("orchestration"))
    if orchestration:
        summary["orchestration"] = orchestration
    return summary


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
            "reuse_strategy": "none|previous_result|previous_source|previous_intent_with_new_retrieval|trace_only",
            "condition_resolution": {
                "inherited": {},
                "changed": {},
                "dropped": {},
                "new": {},
            },
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
                    "join_type": "left|inner",
                    "right_value_columns": [],
                    "multi_match_policy": "collect_unique|preserve_rows|first",
                }
            ],
            "retrieval_jobs": [
                {
                    "dataset_key": "string",
                    "source_alias": "string",
                    "required_params": {"DATA_CATALOG_REQUIRED_PARAM": "value"},
                    "filters": {"PANDAS_FILTER_COLUMN": {"operator": "eq|in|contains|not_in", "value": "value or list"}},
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": "aggregate|detail|entity_list|scalar|explanation",
                "required_columns": [],
                "grain_columns": [],
                "metric_columns": [],
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
