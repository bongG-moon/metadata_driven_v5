# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 Report Snapshot 결정론적 실행기
# 역할: MongoDB에서 복원된 authoritative Report query source에 제한형 연산만 순서대로 적용합니다.
# 주요 입력: 공용 Result Loader를 통과한 payload
# 주요 출력: 분석 결과 행·컬럼·실행 trace가 포함된 payload
# 처리 흐름: 저장 source 계약 재검증 -> filter/sort(null last)/top_n/select 실행 -> bounded 결과 생성
# 유지보수 포인트: prompt의 schema가 아니라 실제 loaded source_results의 query_source_contract를 최종 권위로 사용합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


QUERY_SOURCE_CONTRACT_VERSION = "report.query_source.v1"
PLAN_CONTRACT_VERSION = "report.followup.plan.v1"
SUPPORTED_OPERATIONS = {"filter", "sort", "top_n", "select"}
SUPPORTED_FILTER_OPERATORS = {
    "eq", "ne", "in", "not_in", "contains", "starts_with", "ends_with",
    "gt", "ge", "lt", "le", "is_blank", "not_blank",
}
MAX_RETURNED_ROWS = 500
NULL_TEXT_VALUES = {"", "null", "none", "nan", "nat", "<na>"}


# 함수 설명: 입력 값을 공백이 제거된 문자열로 정규화합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: Langflow Data 또는 dict에서 안전한 payload 사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    if not isinstance(raw, dict):
        return {}
    result = {key: deepcopy(item) for key, item in raw.items() if key not in {"runtime_sources", "_full_result_rows"}}
    if isinstance(raw.get("runtime_sources"), dict):
        result["runtime_sources"] = raw["runtime_sources"]
    if isinstance(raw.get("_full_result_rows"), list):
        result["_full_result_rows"] = raw["_full_result_rows"]
    return result


# 함수 설명: 중복과 빈 값을 제거한 문자열 목록을 만듭니다.
def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: Result Loader가 복원한 source_results에서 지정 alias를 찾습니다.
def _source_result(payload: dict[str, Any], alias: str) -> dict[str, Any]:
    for item in payload.get("source_results", []) if isinstance(payload.get("source_results"), list) else []:
        if isinstance(item, dict) and _text(item.get("source_alias")) == alias:
            return item
    return {}


# 함수 설명: 로드된 source 결과에서 권위 있는 query source 계약을 추출합니다.
def _query_contract(source_result: dict[str, Any]) -> dict[str, Any]:
    contract = source_result.get("query_source_contract")
    return contract if isinstance(contract, dict) else {}


# 함수 설명: query source 계약의 허용 연산을 제한형 연산 이름으로 정규화합니다.
def _allowed_operations(contract: dict[str, Any]) -> set[str]:
    result = {"select"}
    for raw in contract.get("allowed_operations", []) if isinstance(contract.get("allowed_operations"), list) else []:
        value = _text(raw).casefold()
        if value in SUPPORTED_OPERATIONS:
            result.add(value)
        elif value == "apply_filters":
            result.add("filter")
        elif value == "sort_and_top_n":
            result.update({"sort", "top_n"})
        elif value == "select_columns":
            result.add("select")
    return result


# 함수 설명: 정렬과 조건 평가에서 사용할 결측 여부를 판정합니다.
def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    return _text(value).casefold() in NULL_TEXT_VALUES


# 함수 설명: 숫자 비교가 가능한 값을 Decimal로 안전하게 변환합니다.
def _decimal(value: Any) -> Decimal | None:
    if _is_blank(value) or isinstance(value, bool):
        return None
    try:
        return Decimal(_text(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


# 함수 설명: 숫자와 문자열을 모두 고려해 두 값을 비교합니다.
def _equal(left: Any, right: Any) -> bool:
    if _is_blank(left) or _is_blank(right):
        return _is_blank(left) and _is_blank(right)
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return _text(left).casefold() == _text(right).casefold()


# 함수 설명: 숫자 우선 규칙으로 두 값을 삼방 비교합니다.
def _compare(left: Any, right: Any) -> int | None:
    if _is_blank(left) or _is_blank(right):
        return None
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is not None and right_number is not None:
        return -1 if left_number < right_number else 1 if left_number > right_number else 0
    left_text = _text(left).casefold()
    right_text = _text(right).casefold()
    return -1 if left_text < right_text else 1 if left_text > right_text else 0


# 함수 설명: 제한형 filter 연산자로 행 값이 조건을 만족하는지 평가합니다.
def _matches(value: Any, operator: str, expected: Any) -> bool:
    if operator == "is_blank":
        return _is_blank(value)
    if operator == "not_blank":
        return not _is_blank(value)
    if operator == "eq":
        return _equal(value, expected)
    if operator == "ne":
        return not _equal(value, expected)
    if operator in {"in", "not_in"}:
        expected_values = expected if isinstance(expected, list) else [expected]
        included = any(_equal(value, item) for item in expected_values)
        return included if operator == "in" else not included
    if operator in {"contains", "starts_with", "ends_with"}:
        left = _text(value).casefold()
        right = _text(expected).casefold()
        if operator == "contains":
            return right in left
        if operator == "starts_with":
            return left.startswith(right)
        return left.endswith(right)
    compared = _compare(value, expected)
    if compared is None:
        return False
    return {
        "gt": compared > 0,
        "ge": compared >= 0,
        "lt": compared < 0,
        "le": compared <= 0,
    }.get(operator, False)


# 함수 설명: 문자열과 숫자를 안정적으로 정렬할 내부 키를 만듭니다.
def _sort_key(value: Any) -> tuple[int, Any]:
    number = _decimal(value)
    if number is not None:
        return 0, number
    return 1, _text(value).casefold()


# 함수 설명: 결측값을 항상 마지막에 두면서 지정 방향으로 안정 정렬합니다.
def _stable_sort_nulls_last(rows: list[dict[str, Any]], column: str, direction: str) -> list[dict[str, Any]]:
    present = [row for row in rows if not _is_blank(row.get(column))]
    missing = [row for row in rows if _is_blank(row.get(column))]
    return sorted(present, key=lambda row: _sort_key(row.get(column)), reverse=direction == "desc") + missing


# 함수 설명: 정규화 payload에서 Report 실행 계획을 꺼냅니다.
def _plan(payload: dict[str, Any]) -> dict[str, Any]:
    intent = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    plan = intent.get("report_execution_plan")
    return plan if isinstance(plan, dict) else {}


# 함수 설명: 실행 실패 상태와 오류 trace를 일관된 형태로 기록합니다.
def _fail(payload: dict[str, Any], issue_type: str, message: str, **details: Any) -> dict[str, Any]:
    issue = {"type": issue_type, "message": message, **details}
    payload.setdefault("trace", {}).setdefault("errors", []).append(issue)
    payload.setdefault("trace", {}).setdefault("inspection", {})["report_snapshot_execution"] = {
        "stage": "02_report_snapshot_executor",
        "status": "error",
        "errors": [deepcopy(issue)],
    }
    payload["execution_gate"] = {"status": "blocked", "reason": issue_type}
    payload["analysis"] = {"status": "error", "row_count": 0, "columns": [], "error_type": issue_type}
    payload["data"] = {"row_count": 0, "columns": [], "rows": []}
    payload["_full_result_rows"] = []
    return payload


# 함수 설명: 실제 로드된 source의 권위 계약과 계획의 alias, schema, 허용 연산을 대조합니다.
def _validate_loaded_contract(payload: dict[str, Any], plan: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    alias = _text(plan.get("source_alias"))
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    rows = runtime_sources.get(alias)
    if not alias or not isinstance(rows, list):
        return {}, [], {"type": "report_followup_source_not_loaded", "message": f"선택한 Report source를 복원하지 못했습니다: {alias or '(empty)'}"}
    summary = _source_result(payload, alias)
    contract = _query_contract(summary)
    if (
        contract.get("contract_version") != QUERY_SOURCE_CONTRACT_VERSION
        or contract.get("authoritative") is not True
        or _text(contract.get("source_alias")) != alias
    ):
        return {}, [], {"type": "report_followup_loaded_contract_invalid", "message": f"복원된 source의 authoritative query_source_contract를 확인할 수 없습니다: {alias}"}
    planned_dataset = _text(plan.get("dataset_key"))
    stored_dataset = _text(contract.get("dataset_key") or summary.get("dataset_key"))
    if planned_dataset and stored_dataset and planned_dataset != stored_dataset:
        return {}, [], {"type": "report_followup_dataset_contract_mismatch", "message": "계획의 dataset_key와 복원된 Report source 계약이 다릅니다.", "planned": planned_dataset, "stored": stored_dataset}
    contract_columns = _string_list(contract.get("columns"))
    summary_columns = _string_list(summary.get("columns"))
    if not contract_columns or (summary_columns and not set(contract_columns).issubset(set(summary_columns))):
        return {}, [], {"type": "report_followup_loaded_schema_mismatch", "message": "복원된 source schema가 Report query source 계약과 일치하지 않습니다."}
    return contract, [deepcopy(row) for row in rows if isinstance(row, dict)], None


# 주요 함수: 권위 있는 Report 스냅샷에 제한형 filter, sort, top_n, select를 실행합니다.
def execute_report_snapshot(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    gate = payload.get("execution_gate") if isinstance(payload.get("execution_gate"), dict) else {}
    if _text(gate.get("status")).casefold() != "ready":
        payload.setdefault("analysis", {}).update({"status": "skipped", "row_count": 0, "columns": []})
        payload.setdefault("data", {}).update({"row_count": 0, "columns": [], "rows": []})
        return payload
    plan = _plan(payload)
    if plan.get("contract_version") != PLAN_CONTRACT_VERSION:
        return _fail(payload, "report_followup_plan_contract_invalid", "검증된 Report 후속 실행 계획을 확인할 수 없습니다.")

    contract, rows, contract_error = _validate_loaded_contract(payload, plan)
    if contract_error:
        return _fail(payload, contract_error.pop("type"), contract_error.pop("message"), **contract_error)
    columns = _string_list(contract.get("columns"))
    column_set = set(columns)
    allowed = _allowed_operations(contract)
    source_row_count = len(rows)
    matched_row_count = source_row_count
    top_n_applied = False

    for operation in plan.get("operations", []) if isinstance(plan.get("operations"), list) else []:
        if not isinstance(operation, dict):
            return _fail(payload, "report_followup_operation_invalid", "실행 계획 operation이 JSON object가 아닙니다.")
        op = _text(operation.get("operation")).casefold()
        if op not in SUPPORTED_OPERATIONS or op not in allowed:
            return _fail(payload, "report_followup_operation_not_allowed", f"복원된 source 계약이 허용하지 않는 operation입니다: {op or '(empty)'}")

        if op == "filter":
            conditions = operation.get("conditions") if isinstance(operation.get("conditions"), list) else []
            for condition in conditions:
                if not isinstance(condition, dict):
                    return _fail(payload, "report_followup_filter_invalid", "filter condition이 JSON object가 아닙니다.")
                column = _text(condition.get("column"))
                operator = _text(condition.get("operator")).casefold()
                if column not in column_set or operator not in SUPPORTED_FILTER_OPERATORS:
                    return _fail(payload, "report_followup_filter_contract_invalid", "filter column/operator가 복원된 source 계약과 일치하지 않습니다.", column=column, operator=operator)
                expected = condition.get("value")
                rows = [row for row in rows if _matches(row.get(column), operator, expected)]
            matched_row_count = len(rows)
            continue

        if op == "sort":
            column = _text(operation.get("column"))
            direction = _text(operation.get("direction") or "asc").casefold()
            if column not in column_set or direction not in {"asc", "desc"} or _text(operation.get("nulls") or "last").casefold() != "last":
                return _fail(payload, "report_followup_sort_contract_invalid", "sort 계약이 복원된 source schema 또는 nulls-last 정책과 일치하지 않습니다.")
            rows = _stable_sort_nulls_last(rows, column, direction)
            continue

        if op == "top_n":
            try:
                limit = int(operation.get("limit"))
            except (TypeError, ValueError, OverflowError):
                limit = 0
            if not 1 <= limit <= 100:
                return _fail(payload, "report_followup_top_n_invalid", "top_n limit이 허용 범위를 벗어났습니다.")
            matched_row_count = len(rows)
            rows = rows[:limit]
            top_n_applied = True
            continue

        selected = _string_list(operation.get("columns"))
        if not selected or any(column not in column_set for column in selected):
            return _fail(payload, "report_followup_select_contract_invalid", "select 컬럼이 복원된 source schema와 일치하지 않습니다.")
        rows = [{column: row.get(column) for column in selected} for row in rows]
        columns = selected
        column_set = set(columns)

    output_columns = _string_list(plan.get("output_columns"))
    if output_columns:
        if any(column not in set(_string_list(contract.get("columns"))) for column in output_columns):
            return _fail(payload, "report_followup_output_columns_invalid", "결과 컬럼이 authoritative source 계약에 없습니다.")
        rows = [{column: row.get(column) for column in output_columns} for row in rows]
        columns = output_columns
    if not top_n_applied:
        matched_row_count = len(rows)
    truncated = len(rows) > MAX_RETURNED_ROWS
    returned_rows = rows[:MAX_RETURNED_ROWS]
    if truncated:
        payload.setdefault("trace", {}).setdefault("warnings", []).append(
            {
                "type": "report_followup_result_truncated",
                "message": f"Report 후속 결과가 표시 안전 상한 {MAX_RETURNED_ROWS}건을 초과해 일부만 반환했습니다.",
            }
        )

    payload["_full_result_rows"] = returned_rows
    payload["analysis"] = {
        "status": "ok",
        "execution_route": "ReportFollowup",
        "analysis_code": "deterministic_report_snapshot",
        "source_alias": plan.get("source_alias"),
        "source_view_key": plan.get("source_view_key"),
        "source_row_count": source_row_count,
        "matched_row_count": matched_row_count,
        "row_count": len(returned_rows),
        "columns": columns,
        "truncated": truncated,
    }
    payload["data"] = {
        "row_count": len(returned_rows),
        "matched_row_count": matched_row_count,
        "columns": columns,
        "rows": deepcopy(returned_rows),
        "preview_only": truncated,
    }
    payload.setdefault("trace", {}).setdefault("inspection", {})["report_snapshot_execution"] = {
        "stage": "02_report_snapshot_executor",
        "status": "ok",
        "source_alias": plan.get("source_alias"),
        "source_view_key": plan.get("source_view_key"),
        "source_row_count": source_row_count,
        "matched_row_count": matched_row_count,
        "returned_row_count": len(returned_rows),
        "operation_count": len(plan.get("operations", [])),
        "authoritative_contract_validated": True,
        "errors": [],
    }
    return payload


# Langflow 컴포넌트 클래스: 저장된 Report source만 대상으로 결정론적 후속 분석을 실행합니다.
class ReportSnapshotExecutor(Component):
    display_name = "02 Report Snapshot 결정론적 실행기"
    description = "복원된 authoritative Report View에 filter/sort/top_n/select만 적용합니다."
    name = "ReportSnapshotExecutor"
    icon = "TableProperties"
    inputs = [DataInput(name="payload", display_name="복원된 Report 후속 페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="Report 후속 실행 결과", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 실행 결과와 trace가 포함된 payload를 반환합니다.
    def build_payload(self) -> Data:
        result = execute_report_snapshot(getattr(self, "payload", None))
        self.status = result.get("trace", {}).get("inspection", {}).get("report_snapshot_execution", result.get("analysis", {}))
        return Data(data=result)
