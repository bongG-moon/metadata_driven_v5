# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 Report 후속 실행 계획 검증기
# 역할: Intent LLM JSON을 Report-local physical schema에 맞는 제한형 실행 계획으로 정규화합니다.
# 주요 입력: 00 요청 payload, Intent LLM 응답
# 주요 출력: 공용 MongoDB Result Loader와 전용 executor가 소비할 payload
# 처리 흐름: JSON 해석 -> source/column/value/operation 검증 -> previous_source 복원 계약 생성
# 유지보수 포인트: LLM이 만든 dataset·column·code를 허용하지 않으며 authoritative query source만 실행합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


PLAN_CONTRACT_VERSION = "report.followup.plan.v1"
QUERY_SOURCE_CONTRACT_VERSION = "report.query_source.v1"
SUPPORTED_OPERATIONS = {"filter", "sort", "top_n", "select"}
SUPPORTED_FILTER_OPERATORS = {
    "eq",
    "ne",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "ends_with",
    "gt",
    "ge",
    "lt",
    "le",
    "is_blank",
    "not_blank",
}
MAX_OPERATIONS = 16
MAX_CONDITIONS = 30
MAX_TOP_N = 100


# 함수 설명: 입력 값을 공백이 제거된 문자열로 정규화합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: Langflow Data 또는 dict에서 안전한 payload 사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return deepcopy(raw) if isinstance(raw, dict) else {}


# 함수 설명: Language Model 응답에서 JSON 후보 문자열을 추출합니다.
def _response_text(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        if isinstance(data.get("report_followup_plan"), dict):
            return json.dumps(data["report_followup_plan"], ensure_ascii=False)
        if isinstance(data.get("intent_plan"), dict):
            return json.dumps(data["intent_plan"], ensure_ascii=False)
        if _text(data.get("text")):
            return _text(data.get("text"))
    return _text(getattr(value, "text", None) or getattr(value, "content", None) or value)


# 함수 설명: 모델 응답을 단일 JSON object로 엄격하게 해석합니다.
def _parse_response(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if isinstance(value.get("report_followup_plan"), dict):
            return deepcopy(value["report_followup_plan"])
        if isinstance(value.get("intent_plan"), dict):
            return deepcopy(value["intent_plan"])
        return deepcopy(value)
    text = _response_text(value)
    if not text:
        return {}
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if isinstance(parsed, dict) and isinstance(parsed.get("report_followup_plan"), dict):
        return parsed["report_followup_plan"]
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: 요청 계약의 query source를 alias 기준 사전으로 구성합니다.
def _sources(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    followup = payload.get("report_followup") if isinstance(payload.get("report_followup"), dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for item in followup.get("query_sources", []) if isinstance(followup.get("query_sources"), list) else []:
        if not isinstance(item, dict):
            continue
        alias = _text(item.get("source_alias"))
        if alias:
            result[alias] = deepcopy(item)
    return result


# 함수 설명: 중복과 빈 값을 제거한 문자열 목록을 만듭니다.
def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: source 계약에서 제한형 실행기가 허용할 연산 집합을 구합니다.
def _allowed_operations(source: dict[str, Any]) -> set[str]:
    result: set[str] = {"select"}
    for value in source.get("allowed_operations", []) if isinstance(source.get("allowed_operations"), list) else []:
        item = _text(value).casefold()
        if item in SUPPORTED_OPERATIONS:
            result.add(item)
        elif item == "apply_filters":
            result.add("filter")
        elif item == "sort_and_top_n":
            result.update({"sort", "top_n"})
        elif item == "select_columns":
            result.add("select")
    return result


# 함수 설명: filter 값 출처 검증에 사용할 정규화된 질문을 반환합니다.
def _normalized_question(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    return re.sub(r"[\s,_-]+", "", _text(request.get("question"))).casefold()


# 함수 설명: 요청한 조건이 Report가 선언한 고정 predicate와 일치하는지 확인합니다.
def _predicate_matches(source: dict[str, Any], column: str, operator: str, value: Any) -> bool:
    for item in source.get("predicates", []) if isinstance(source.get("predicates"), list) else []:
        if not isinstance(item, dict):
            continue
        if (
            _text(item.get("column")) == column
            and _text(item.get("operator")).casefold() == operator
            and item.get("value") == value
        ):
            return True
    return False


# 함수 설명: filter 값이 사용자 질문에 명시적으로 포함됐는지 확인합니다.
def _value_explicit_in_question(question: str, value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    if not values:
        return False
    for item in values:
        if item is None:
            return False
        normalized = re.sub(r"[\s,_-]+", "", _text(item)).casefold()
        if not normalized or normalized not in question:
            return False
    return True


# 함수 설명: 단일 filter 조건의 컬럼과 연산자와 값 출처를 검증합니다.
def _condition(
    raw: dict[str, Any],
    source: dict[str, Any],
    question: str,
    *,
    allow_persisted_value: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    column = _text(raw.get("column") or raw.get("field"))
    operator = _text(raw.get("operator") or "eq").casefold()
    columns = set(_string_list(source.get("columns")))
    if column not in columns:
        return None, {
            "type": "report_followup_column_not_declared",
            "message": f"Report View에 선언되지 않은 filter 컬럼입니다: {column or '(empty)'}",
            "column": column,
        }
    if operator not in SUPPORTED_FILTER_OPERATORS:
        return None, {
            "type": "report_followup_filter_operator_not_allowed",
            "message": f"Report 후속 분석에서 허용하지 않는 filter operator입니다: {operator or '(empty)'}",
            "operator": operator,
        }
    value = deepcopy(raw.get("value"))
    if operator in {"is_blank", "not_blank"}:
        value = None
    elif not (allow_persisted_value or _predicate_matches(source, column, operator, value) or _value_explicit_in_question(question, value)):
        return None, {
            "type": "report_followup_filter_value_not_grounded",
            "message": f"질문 또는 Report 계약에서 근거를 확인할 수 없는 filter 값입니다: {column}",
            "column": column,
            "operator": operator,
        }
    return {"column": column, "operator": operator, "value": value}, None


# 함수 설명: 모델 연산 계획을 허용된 filter, sort, top_n, select 계약으로 정규화합니다.
def _normalize_operations(
    raw_operations: Any,
    source: dict[str, Any],
    question: str,
    *,
    allow_persisted_filter_values: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    operations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    allowed = _allowed_operations(source)
    columns = _string_list(source.get("columns"))
    column_set = set(columns)
    raw_items = raw_operations if isinstance(raw_operations, list) else []
    if len(raw_items) > MAX_OPERATIONS:
        return [], [{"type": "report_followup_operation_limit_exceeded", "message": f"Report 후속 operation은 최대 {MAX_OPERATIONS}개까지 허용합니다."}]

    for raw in raw_items:
        if not isinstance(raw, dict):
            errors.append({"type": "report_followup_operation_invalid", "message": "Report 후속 operation은 JSON object여야 합니다."})
            continue
        operation = _text(raw.get("operation") or raw.get("op")).casefold()
        operation_aliases = {"apply_filters": "filter", "sort_and_top_n": "sort", "select_columns": "select"}
        operation = operation_aliases.get(operation, operation)
        if operation not in SUPPORTED_OPERATIONS or operation not in allowed:
            errors.append(
                {
                    "type": "report_followup_operation_not_allowed",
                    "message": f"선택한 Report View에서 허용하지 않는 operation입니다: {operation or '(empty)'}",
                    "operation": operation,
                }
            )
            continue

        if operation == "filter":
            conditions_raw = raw.get("conditions") if isinstance(raw.get("conditions"), list) else [raw]
            if len(conditions_raw) > MAX_CONDITIONS:
                errors.append({"type": "report_followup_filter_limit_exceeded", "message": f"filter 조건은 최대 {MAX_CONDITIONS}개까지 허용합니다."})
                continue
            conditions: list[dict[str, Any]] = []
            for raw_condition in conditions_raw:
                if not isinstance(raw_condition, dict):
                    errors.append({"type": "report_followup_filter_invalid", "message": "filter condition은 JSON object여야 합니다."})
                    continue
                normalized, error = _condition(
                    raw_condition,
                    source,
                    question,
                    allow_persisted_value=allow_persisted_filter_values,
                )
                if error:
                    errors.append(error)
                elif normalized:
                    conditions.append(normalized)
            if conditions:
                operations.append({"operation": "filter", "conditions": conditions})
            continue

        if operation == "sort":
            column = _text(raw.get("column") or raw.get("sort_by"))
            direction = _text(raw.get("direction") or raw.get("order") or "asc").casefold()
            if column not in column_set:
                errors.append({"type": "report_followup_column_not_declared", "message": f"Report View에 선언되지 않은 sort 컬럼입니다: {column or '(empty)'}", "column": column})
                continue
            if direction not in {"asc", "desc"}:
                errors.append({"type": "report_followup_sort_direction_invalid", "message": f"sort direction은 asc/desc만 허용합니다: {direction}"})
                continue
            operations.append({"operation": "sort", "column": column, "direction": direction, "nulls": "last"})
            continue

        if operation == "top_n":
            try:
                limit = int(raw.get("limit"))
            except (TypeError, ValueError, OverflowError):
                limit = 0
            if not 1 <= limit <= MAX_TOP_N:
                errors.append({"type": "report_followup_top_n_invalid", "message": f"top_n limit은 1~{MAX_TOP_N} 범위여야 합니다."})
                continue
            operations.append({"operation": "top_n", "limit": limit})
            continue

        selected = _string_list(raw.get("columns"))
        missing = [column for column in selected if column not in column_set]
        if not selected or missing:
            errors.append(
                {
                    "type": "report_followup_select_columns_invalid",
                    "message": "select 컬럼은 선택한 Report View의 실제 컬럼만 사용할 수 있습니다.",
                    "missing_columns": missing,
                }
            )
            continue
        operations.append({"operation": "select", "columns": selected})

    return operations, errors


# 함수 설명: 직전 View 계획을 같은 source 계약에 다시 대조해 상속 가능한 연산만 복원합니다.
def _inherited_operations(
    payload: dict[str, Any],
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    followup = payload.get("report_followup") if isinstance(payload.get("report_followup"), dict) else {}
    if followup.get("inherit_current_view") is not True:
        return [], [], {}
    previous = followup.get("current_view_plan") if isinstance(followup.get("current_view_plan"), dict) else {}
    source_alias = _text(source.get("source_alias"))
    dataset_key = _text(source.get("dataset_key") or source_alias)
    if (
        previous.get("contract_version") != PLAN_CONTRACT_VERSION
        or _text(previous.get("source_alias")) != source_alias
        or (_text(previous.get("dataset_key")) and _text(previous.get("dataset_key")) != dataset_key)
    ):
        return [], [
            {
                "type": "report_followup_previous_view_invalid",
                "message": "직전 결과의 검증된 View 계획을 현재 Report source와 연결할 수 없습니다.",
            }
        ], previous
    operations, errors = _normalize_operations(
        previous.get("operations"),
        source,
        "",
        allow_persisted_filter_values=True,
    )
    if not errors and not any(item.get("operation") == "select" for item in operations):
        previous_columns = _string_list(previous.get("output_columns"))
        source_columns = set(_string_list(source.get("columns")))
        if previous_columns and all(column in source_columns for column in previous_columns):
            operations.append({"operation": "select", "columns": previous_columns})
    return operations, errors, previous


# 함수 설명: 이전 subset 연산 뒤에 현재 delta 연산을 붙이고 projection은 맨 마지막으로 이동합니다.
def _compose_operations(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_body = [deepcopy(item) for item in previous if item.get("operation") != "select"]
    current_body = [deepcopy(item) for item in current if item.get("operation") != "select"]
    previous_selects = [deepcopy(item) for item in previous if item.get("operation") == "select"]
    current_selects = [deepcopy(item) for item in current if item.get("operation") == "select"]
    projection = current_selects[-1:] or previous_selects[-1:]
    return [*previous_body, *current_body, *projection]


# 함수 설명: 실행을 차단하고 trace에 계약 오류를 기록한 payload를 만듭니다.
def _blocked(payload: dict[str, Any], issue: dict[str, Any], *, reason: str = "report_followup_plan_invalid") -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("errors", []).append(deepcopy(issue))
    payload["intent_plan"] = {
        "analysis_kind": "report_followup_snapshot_transform",
        "request_scope": "report_followup",
        "reference_mode": "none",
        "reuse_strategy": "none",
        "retrieval_jobs": [],
        "report_execution_plan": {},
    }
    payload["execution_gate"] = {"status": "blocked", "reason": reason}
    payload.setdefault("analysis", {}).update({"status": "skipped", "row_count": 0, "columns": []})
    payload.setdefault("data", {}).update({"row_count": 0, "columns": [], "rows": []})
    return payload


# 주요 함수: 모델 계획을 검증해 Result Loader 호환 Report 실행 계약으로 변환합니다.
def normalize_report_followup_plan(payload_value: Any, llm_response_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    followup = payload.get("report_followup") if isinstance(payload.get("report_followup"), dict) else {}
    upstream_status = _text(followup.get("status"))
    if upstream_status != "ready":
        payload["intent_plan"] = {
            "analysis_kind": "report_followup_snapshot_transform",
            "request_scope": "report_followup",
            "reference_mode": "none",
            "reuse_strategy": "none",
            "retrieval_jobs": [],
            "report_execution_plan": {},
        }
        payload["execution_gate"] = {"status": upstream_status or "blocked", "reason": f"report_followup_{upstream_status or 'blocked'}"}
        return payload

    parsed = _parse_response(llm_response_value)
    if not parsed:
        return _blocked(payload, {"type": "report_followup_plan_response_invalid", "message": "Report 후속 계획 LLM 응답을 JSON object로 해석할 수 없습니다."})
    response_status = _text(parsed.get("status") or "ready").casefold()
    if response_status != "ready":
        issue = {
            "type": "report_followup_clarification_required",
            "message": _text(parsed.get("reason")) or "Report 후속 요청을 안전한 실행 계획으로 확정할 수 없습니다.",
        }
        payload.setdefault("trace", {}).setdefault("errors", []).append(issue)
        payload["intent_plan"] = {
            "analysis_kind": "report_followup_snapshot_transform",
            "request_scope": "clarification",
            "reference_mode": "none",
            "reuse_strategy": "none",
            "retrieval_jobs": [],
            "report_execution_plan": {},
        }
        payload["execution_gate"] = {"status": "clarification_required", "reason": issue["type"]}
        return payload

    sources = _sources(payload)
    candidates = set(_string_list(followup.get("candidate_source_aliases")))
    source_alias = _text(parsed.get("source_alias"))
    if not source_alias and len(candidates) == 1:
        source_alias = next(iter(candidates))
    source = sources.get(source_alias)
    if not source:
        return _blocked(payload, {"type": "report_followup_source_not_declared", "message": f"Report가 선언하지 않은 source_alias입니다: {source_alias or '(empty)'}"})
    if candidates and source_alias not in candidates:
        return _blocked(payload, {"type": "report_followup_source_not_grounded", "message": f"현재 질문에서 선택할 근거가 없는 Report View입니다: {source_alias}"})
    if source.get("contract_version") != QUERY_SOURCE_CONTRACT_VERSION or source.get("authoritative") is not True:
        return _blocked(payload, {"type": "report_followup_source_not_authoritative", "message": f"저장된 Report query source 계약을 신뢰할 수 없습니다: {source_alias}"})

    current_operations, current_errors = _normalize_operations(parsed.get("operations"), source, _normalized_question(payload))
    previous_operations, inheritance_errors, previous_plan = _inherited_operations(payload, source)
    errors = [*inheritance_errors, *current_errors]
    if errors:
        payload.setdefault("trace", {}).setdefault("errors", []).extend(errors)
        payload["execution_gate"] = {"status": "blocked", "reason": "report_followup_plan_validation_failed"}
        payload["intent_plan"] = {
            "analysis_kind": "report_followup_snapshot_transform",
            "request_scope": "report_followup",
            "reference_mode": "none",
            "reuse_strategy": "none",
            "retrieval_jobs": [],
            "report_execution_plan": {},
        }
        return payload

    operations = _compose_operations(previous_operations, current_operations)
    if len(operations) > MAX_OPERATIONS:
        return _blocked(
            payload,
            {
                "type": "report_followup_operation_limit_exceeded",
                "message": f"상속된 연산을 포함한 Report 후속 operation은 최대 {MAX_OPERATIONS}개까지 허용합니다.",
            },
            reason="report_followup_plan_validation_failed",
        )

    default_columns = _string_list(source.get("default_display_columns"))
    selected_columns = next((item["columns"] for item in reversed(operations) if item.get("operation") == "select"), [])
    output_columns = selected_columns or default_columns or _string_list(source.get("columns"))[:20]
    sort_contract = next((deepcopy(item) for item in reversed(operations) if item.get("operation") == "sort"), {})
    top_contract = next((deepcopy(item) for item in reversed(operations) if item.get("operation") == "top_n"), {})
    report_plan = {
        "contract_version": PLAN_CONTRACT_VERSION,
        "source_alias": source_alias,
        "dataset_key": _text(source.get("dataset_key")) or source_alias,
        "source_view_key": _text(source.get("purpose")) or source_alias,
        "grain": deepcopy(source.get("grain") or {}),
        "operations": operations,
        "output_columns": output_columns,
        "inherited_previous_view": bool(previous_operations or previous_plan),
        "reason": _text(parsed.get("reason"))[:500],
    }
    intent_plan = {
        "analysis_kind": "report_followup_snapshot_transform",
        "request_scope": "followup_transform",
        "reference_mode": "previous_source",
        "reuse_strategy": "previous_source",
        "retrieval_jobs": [],
        "pandas_execution_plan": [],
        "report_execution_plan": report_plan,
        "resolved_execution_graph": {
            "external_source_requirements": [
                {
                    "kind": "external_source",
                    "provider": "previous_source",
                    "source_alias": source_alias,
                    "dataset_key": report_plan["dataset_key"],
                }
            ]
        },
        "output_contract": {
            "source_alias": source_alias,
            "columns": output_columns,
            "ordering": sort_contract,
            "limit": top_contract.get("limit", 0),
        },
    }
    payload["intent_plan"] = intent_plan
    payload["execution_gate"] = {"status": "ready", "reason": "report_followup_plan_validated"}
    payload.setdefault("trace", {}).setdefault("inspection", {})["report_followup_plan"] = {
        "stage": "01_report_followup_plan_normalizer",
        "status": "ready",
        "source_alias": source_alias,
        "source_view_key": report_plan["source_view_key"],
        "operation_count": len(operations),
        "inherited_operation_count": len(previous_operations),
        "current_operation_count": len(current_operations),
        "retrieval_job_count": 0,
        "errors": [],
    }
    return payload


# Langflow 컴포넌트 클래스: 모델 응답을 안전한 Report 후속 실행 계획으로 정규화합니다.
class ReportFollowupPlanNormalizer(Component):
    display_name = "01 Report 후속 실행 계획 검증기"
    description = "LLM JSON을 Report가 선언한 물리 source·column·operation 계약으로 재검증합니다."
    name = "ReportFollowupPlanNormalizer"
    icon = "ShieldCheck"
    inputs = [
        DataInput(name="payload", display_name="Report 후속 요청 페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="Report 후속 계획 LLM 응답", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="검증된 실행 페이로드", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 검증과 정규화를 마친 계획 payload를 반환합니다.
    def build_payload(self) -> Data:
        result = normalize_report_followup_plan(getattr(self, "payload", None), getattr(self, "llm_response", None))
        self.status = result.get("trace", {}).get("inspection", {}).get("report_followup_plan", result.get("execution_gate", {}))
        return Data(data=result)
