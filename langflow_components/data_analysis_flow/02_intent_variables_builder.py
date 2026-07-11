from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

def build_variables(payload_value: Any, metadata_candidates_value: Any = None) -> dict[str, Any]:
    payload = _payload(payload_value)
    metadata_candidates = _compact_metadata_candidates(_payload(metadata_candidates_value) or {})
    return {
        "question": payload.get("request", {}).get("question", ""),
        "state_summary": _compact_json(_state_summary(payload)),
        "metadata_candidates": _compact_json(metadata_candidates),
        "output_schema": _compact_json(_schema()),
    }


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _state_summary(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    followup_hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    return {
        "request_context": {
            "reference_date": request.get("reference_date", ""),
        },
        "followup_hint": followup_hint,
        "state": _compact_state(payload.get("state", {}) if isinstance(payload.get("state"), dict) else {}),
    }


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
            "retrieval_jobs": [
                {
                    "dataset_key": "string",
                    "source_alias": "string",
                    "required_params": {"DATA_CATALOG_REQUIRED_PARAM": "value"},
                    "filters": {"PANDAS_FILTER_COLUMN": {"operator": "eq|in|contains|not_in", "value": "value or list"}},
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": {},
        },
        "metadata_refs": [{"section": "string", "key": "string"}],
        "trace": {"decision_reason": []},
    }


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


def _compact_source_columns(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(alias): _string_list(columns)[:80]
        for alias, columns in value.items()
        if str(alias or "").strip() and _string_list(columns)
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


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

    def build_question(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None), getattr(self, "metadata_candidates_in", None))["question"])

    def build_state_summary(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None), getattr(self, "metadata_candidates_in", None))["state_summary"])

    def build_metadata_candidates(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None), getattr(self, "metadata_candidates_in", None))["metadata_candidates"])

    def build_output_schema(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None), getattr(self, "metadata_candidates_in", None))["output_schema"])

