from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    return {
        "question": payload.get("request", {}).get("question", ""),
        "result_summary_json": _json_dumps(payload.get("data", {})),
        "applied_scope_json": _json_dumps(_compact_applied_scope(payload)),
        "answer_context_json": _json_dumps(_answer_context(payload)),
        "warnings_errors_json": _json_dumps({"warnings": payload.get("trace", {}).get("warnings", []), "errors": payload.get("trace", {}).get("errors", [])}),
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _compact_applied_scope(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    inspection = payload.get("trace", {}).get("inspection", {}) if isinstance(payload.get("trace"), dict) else {}
    if not isinstance(inspection, dict):
        inspection = {}

    retrieval_jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    pandas_plan = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    result: dict[str, Any] = {
        "intent": _omit_empty(
            {
                "analysis_kind": plan.get("analysis_kind") or _dict(inspection.get("intent")).get("analysis_kind"),
                "retrieval_job_count": len(retrieval_jobs),
                "pandas_step_count": len(pandas_plan),
                "metadata_ref_count": len(payload.get("metadata_refs", [])) if isinstance(payload.get("metadata_refs"), list) else 0,
            }
        ),
        "retrieval": [_compact_source_result(item) for item in _list(payload.get("source_results"))],
        "pandas_execution": _compact_pandas_execution(payload, _dict(inspection.get("pandas_execution"))),
    }
    result_store = _compact_result_store(_dict(inspection.get("result_store")))
    if result_store:
        result["result_store"] = result_store
    return result


def _compact_source_result(value: Any) -> dict[str, Any]:
    source = _dict(value)
    source_execution = _dict(source.get("source_execution"))
    return _omit_empty(
        {
            "source_alias": source.get("source_alias"),
            "dataset_key": source.get("dataset_key"),
            "source_type": source.get("source_type"),
            "status": source.get("status"),
            "row_count": source.get("row_count"),
            "columns": source.get("columns"),
            "applied_params": source.get("applied_params"),
            "pandas_filters": source.get("pandas_filters"),
            "used_dummy_data": source_execution.get("used_dummy_data"),
            "adapter": source_execution.get("adapter"),
            "params_applied_in_retriever": source_execution.get("params_applied_in_retriever"),
            "filters_applied_in_retriever": source_execution.get("filters_applied_in_retriever"),
        }
    )


def _compact_pandas_execution(payload: dict[str, Any], pandas_execution: dict[str, Any]) -> dict[str, Any]:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    execution_result = _dict(pandas_execution.get("execution_result"))
    return _omit_empty(
        {
            "stage": pandas_execution.get("stage"),
            "status": pandas_execution.get("status") or analysis.get("status"),
            "row_count": execution_result.get("row_count", analysis.get("row_count")),
            "columns": execution_result.get("columns", analysis.get("columns")),
            "used_helpers": pandas_execution.get("used_helpers", analysis.get("used_helpers")),
            "pandas_filter_plan": pandas_execution.get("pandas_filter_plan"),
            "error": _compact_error(pandas_execution.get("error") or analysis.get("error")),
        }
    )


def _compact_error(value: Any) -> Any:
    error = _dict(value)
    if not error:
        return None
    return _omit_empty({"type": error.get("type"), "message": error.get("message")})


def _compact_result_store(result_store: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "status": result_store.get("status"),
            "data_ref": result_store.get("data_ref"),
            "ttl_hours": result_store.get("ttl_hours"),
            "expires_at": result_store.get("expires_at"),
        }
    )


def _answer_context(payload: dict[str, Any]) -> dict[str, Any]:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    pandas_execution = _dict(_dict(payload.get("trace")).get("inspection")).get("pandas_execution")
    pandas_execution = _dict(pandas_execution)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    step_outputs = _list(analysis.get("step_outputs")) or _list(pandas_execution.get("step_outputs"))
    function_case_results = _list(analysis.get("function_case_results")) or _list(pandas_execution.get("function_case_results"))
    rows = _list(data.get("rows"))
    columns = _list(data.get("columns")) or _columns_from_rows(rows)
    metric_columns = _metric_columns(columns, rows)
    return {
        "number_display_policy": {
            "under_10000": "comma_full_number",
            "gte_10000": "k_unit",
            "display_only": True,
        },
        "result_shape": _omit_empty(
            {
                "row_count": data.get("row_count", analysis.get("row_count")),
                "columns": columns or analysis.get("columns"),
            }
        ),
        "applied_criteria": _applied_criteria(payload),
        "result_interpretation_hints": _omit_empty(
            {
                "is_empty_result": _int(data.get("row_count"), len(rows)) == 0 and not rows,
                "has_zero_values": _has_zero_values(rows),
                "primary_metric_columns": metric_columns,
                "primary_dimension_columns": _dimension_columns(columns, metric_columns),
            }
        ),
        "step_outputs": deepcopy(step_outputs),
        "function_case_results": deepcopy(function_case_results),
        "next_question_candidates": _next_question_candidates(data),
    }


def _applied_criteria(payload: dict[str, Any]) -> dict[str, Any]:
    plan = _dict(payload.get("intent_plan"))
    required_params: dict[str, Any] = {}
    analysis_filters: dict[str, Any] = {}
    datasets: list[dict[str, Any]] = []
    for job in _list(plan.get("retrieval_jobs")):
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        if dataset_key or alias:
            datasets.append(_omit_empty({"dataset_key": dataset_key, "source_alias": alias, "source_type": job.get("source_type")}))
        params = _dict(job.get("required_params"))
        if params:
            required_params[alias or dataset_key or f"job_{len(required_params) + 1}"] = deepcopy(params)
        filters = _dict(job.get("filters"))
        if filters:
            analysis_filters[alias or dataset_key or f"job_{len(analysis_filters) + 1}"] = deepcopy(filters)
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        dataset_key = str(source.get("dataset_key") or "").strip()
        params = _dict(source.get("applied_params"))
        if params:
            required_params[alias or dataset_key or f"source_{len(required_params) + 1}"] = deepcopy(params)
        filters = _dict(source.get("pandas_filters")) or _dict(source.get("applied_filters"))
        if filters:
            analysis_filters[alias or dataset_key or f"source_{len(analysis_filters) + 1}"] = deepcopy(filters)
    return _omit_empty(
        {
            "required_params": required_params,
            "analysis_filters": analysis_filters,
            "datasets": _dedupe_dicts(datasets),
            "group_by": _group_by_columns(_list(plan.get("pandas_execution_plan"))),
        }
    )


def _columns_from_rows(rows: list[Any]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def _has_zero_values(rows: list[Any]) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row.values():
            if isinstance(value, bool):
                continue
            try:
                if float(value) == 0:
                    return True
            except Exception:
                continue
    return False


def _metric_columns(columns: list[Any], rows: list[Any]) -> list[str]:
    result: list[str] = []
    for column in columns:
        text = str(column)
        if _column_has_numeric_value(rows, text):
            result.append(text)
    return result


def _dimension_columns(columns: list[Any], metric_columns: list[str]) -> list[str]:
    metric_set = set(metric_columns)
    return [str(column) for column in columns if str(column) not in metric_set]


def _column_has_numeric_value(rows: list[Any], column: str) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            continue
        try:
            number = float(value)
        except Exception:
            continue
        if number != number:
            continue
        return True
    return False


def _group_by_columns(pandas_plan: list[Any]) -> list[str]:
    columns: list[str] = []
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        for key in ("groupby_columns", "group_by", "group_by_columns", "group_columns"):
            value = step.get(key)
            if isinstance(value, list):
                for item in value:
                    text = str(item or "").strip()
                    if text and text not in columns:
                        columns.append(text)
            elif isinstance(value, str) and value.strip() and value.strip() not in columns:
                columns.append(value.strip())
    return columns


def _next_question_candidates(data: dict[str, Any]) -> list[str]:
    row_count = _int(data.get("row_count"), len(_list(data.get("rows"))))
    if row_count <= 0:
        return ["조건을 넓혀서 다시 조회할까요?"]
    return ["이 결과를 제품별 또는 공정별로 더 나눠볼까요?", "원본 데이터 기준으로 상세 행을 확인할까요?"]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if _has_compact_value(item)}


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(item)
    return result


def _has_compact_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, indent=2)


def _json_ready(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return None if value != value or value in (float("inf"), -float("inf")) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_ready(item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_ready(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item_value) for item_value in value]
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


class AnswerVariablesBuilder(Component):
    display_name = "18 답변 생성 변수 생성기"
    description = "Langflow 프롬프트 템플릿과 에이전트/LLM에 연결할 답변 생성 변수를 제공합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="question", display_name="사용자 질문", method="build_question", types=["Message"], group_outputs=True),
        Output(name="result_summary_json", display_name="결과 요약 JSON", method="build_result_summary", types=["Message"], group_outputs=True),
        Output(name="applied_scope_json", display_name="적용 범위 JSON", method="build_applied_scope", types=["Message"], group_outputs=True),
        Output(name="answer_context_json", display_name="답변 컨텍스트 JSON", method="build_answer_context", types=["Message"], group_outputs=True),
        Output(name="warnings_errors_json", display_name="경고/오류 JSON", method="build_warnings_errors", types=["Message"], group_outputs=True),
    ]

    def build_question(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["question"])

    def build_result_summary(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["result_summary_json"])

    def build_applied_scope(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["applied_scope_json"])

    def build_answer_context(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["answer_context_json"])

    def build_warnings_errors(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["warnings_errors_json"])

