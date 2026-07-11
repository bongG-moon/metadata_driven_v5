from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

TABLE_PREVIEW_LIMIT = 10


def build_answer_response(payload_value: Any, answer_text: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    structured_answer = _answer_payload(answer_text)
    message = (_answer_text_from_dict(structured_answer) if structured_answer else _answer_text(answer_text)).strip()
    if not message:
        row_count = payload.get("data", {}).get("row_count", 0)
        message = f"분석 결과 {row_count}건을 확인했습니다." if payload.get("analysis", {}).get("status") == "ok" else "분석을 완료하지 못했습니다. trace의 오류를 확인해 주세요."
    next_payload = deepcopy(payload)
    next_payload["answer_message"] = message
    next_payload["answer_sections"] = _build_answer_sections(next_payload, message, _dict(structured_answer.get("answer_sections")))
    next_payload["state"] = _build_next_turn_state(next_payload)
    return next_payload


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _answer_text(value: Any) -> str:
    if isinstance(value, dict):
        text = _answer_text_from_dict(value)
        return text if text else json.dumps(value, ensure_ascii=False, default=str)

    text = _message_text(value).strip()
    parsed = _json_text(text)
    if parsed:
        parsed_text = _answer_text_from_dict(parsed)
        if parsed_text:
            return parsed_text
    return text


def _answer_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return deepcopy(data)
    text = _message_text(value).strip()
    return _json_text(text)


def _answer_text_from_dict(value: dict[str, Any]) -> str:
    for key in ("answer_message", "answer", "text", "message", "output"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    data = value.get("data")
    if isinstance(data, dict):
        return _answer_text_from_dict(data)
    return ""


def _message_text(value: Any) -> str:
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        extracted = _answer_text_from_dict(data)
        if extracted:
            return extracted
    return str(value or "")


def _json_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    candidate = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
    elif "{" in candidate and "}" in candidate:
        candidate = candidate[candidate.find("{") : candidate.rfind("}") + 1]
    try:
        parsed = json.loads(candidate)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_answer_sections(payload: dict[str, Any], answer_message: str, section_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _dict(payload.get("data"))
    overrides = _dict(section_overrides)
    result_table_overrides = _dict(overrides.get("result_table"))
    rows = _list(data.get("rows"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    display_columns = _string_list(data.get("display_columns")) or _string_list(result_table_overrides.get("display_columns"))
    override_labels = _dict(result_table_overrides.get("column_labels"))
    column_labels = {**override_labels, **_dict(data.get("column_labels"))}
    row_count = _int(data.get("row_count"), len(rows))
    applied_criteria = _applied_criteria(payload)
    evidence = _evidence(payload)
    notices = _notices(payload, row_count, rows)
    downloads = _downloads(payload)
    return {
        "summary": {
            "headline": answer_message,
            "basis": _summary_basis(applied_criteria),
        },
        "result_table": _omit_empty(
            {
                "columns": columns,
                "display_columns": display_columns,
                "column_labels": deepcopy(column_labels),
                "row_source": "data.rows",
                "row_count": row_count,
                "preview_limit": TABLE_PREVIEW_LIMIT,
            }
        ),
        "applied_criteria": applied_criteria,
        "evidence": evidence,
        "notices": notices,
        "downloads": downloads,
        "next_questions": _next_questions(payload),
    }


def _applied_criteria(payload: dict[str, Any]) -> dict[str, Any]:
    plan = _dict(payload.get("intent_plan"))
    retrieval_jobs = _list(plan.get("retrieval_jobs"))
    pandas_plan = _list(plan.get("pandas_execution_plan"))
    source_results = _list(payload.get("source_results"))
    required_params: dict[str, Any] = {}
    analysis_filters: dict[str, Any] = {}
    retrieval_filters: dict[str, Any] = {}
    datasets: list[dict[str, Any]] = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        source_type = str(job.get("source_type") or "").strip()
        if dataset_key or alias:
            datasets.append(_omit_empty({"dataset_key": dataset_key, "source_alias": alias, "source_type": source_type}))
        params = _dict(job.get("required_params"))
        if params:
            required_params[alias or dataset_key or f"job_{len(required_params) + 1}"] = deepcopy(params)
        filters = _dict(job.get("filters"))
        if filters:
            analysis_filters[alias or dataset_key or f"job_{len(analysis_filters) + 1}"] = deepcopy(filters)
    for source in source_results:
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        dataset_key = str(source.get("dataset_key") or "").strip()
        source_type = str(source.get("source_type") or "").strip()
        if dataset_key or alias:
            datasets.append(_omit_empty({"dataset_key": dataset_key, "source_alias": alias, "source_type": source_type, "row_count": source.get("row_count")}))
        params = _dict(source.get("applied_params"))
        if params:
            required_params[alias or dataset_key or f"source_{len(required_params) + 1}"] = deepcopy(params)
        pandas_filters = _dict(source.get("pandas_filters")) or _dict(source.get("applied_filters"))
        if pandas_filters:
            analysis_filters[alias or dataset_key or f"source_{len(analysis_filters) + 1}"] = deepcopy(pandas_filters)
        retriever_filters = _dict(_dict(source.get("source_execution")).get("filters_applied_in_retriever"))
        if retriever_filters:
            retrieval_filters[alias or dataset_key or f"source_{len(retrieval_filters) + 1}"] = deepcopy(retriever_filters)
    return _omit_empty(
        {
            "required_params": required_params,
            "analysis_filters": analysis_filters,
            "retrieval_filters": retrieval_filters,
            "group_by": _group_by_columns(pandas_plan),
            "metrics": _metric_columns(payload),
            "datasets": _dedupe_dicts(datasets),
        }
    )


def _evidence(payload: dict[str, Any]) -> dict[str, Any]:
    analysis = _dict(payload.get("analysis"))
    pandas_execution = _dict(_dict(_dict(payload.get("trace")).get("inspection")).get("pandas_execution"))
    step_outputs = _list(analysis.get("step_outputs")) or _list(pandas_execution.get("step_outputs"))
    function_case_results = _list(analysis.get("function_case_results")) or _list(pandas_execution.get("function_case_results"))
    return _omit_empty(
        {
            "datasets": _compact_source_results(_list(payload.get("source_results"))),
            "calculation_rules": deepcopy(_list(payload.get("metadata_refs")))[:10],
            "step_outputs": deepcopy(step_outputs[:6]),
            "function_case_results": deepcopy(function_case_results[:6]),
        }
    )


def _notices(payload: dict[str, Any], row_count: int, rows: list[Any]) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    if row_count == 0 and not rows:
        notices.append({"type": "empty_result", "message": "조건에 맞는 결과 행이 없습니다."})
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        execution = _dict(source.get("source_execution"))
        if execution.get("used_dummy_data") is True:
            notices.append({"type": "dummy_data", "message": "현재 결과는 더미 데이터 기준입니다."})
            break
    trace = _dict(payload.get("trace"))
    for item in _list(trace.get("warnings"))[:5]:
        if isinstance(item, dict):
            notices.append({"type": str(item.get("type") or "warning"), "message": str(item.get("message") or item)})
    for item in _list(trace.get("errors"))[:5]:
        if isinstance(item, dict):
            notices.append({"type": str(item.get("type") or "error"), "message": str(item.get("message") or item)})
    return notices


def _downloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for ref in _list(payload.get("data_refs")):
        if isinstance(ref, dict):
            refs.append(deepcopy(ref))
    data_ref = _dict(_dict(payload.get("data")).get("data_ref"))
    if data_ref:
        refs.append(deepcopy(data_ref))
    return _dedupe_dicts(refs)


def _summary_basis(applied_criteria: dict[str, Any]) -> list[str]:
    basis = []
    if applied_criteria.get("required_params"):
        basis.append("조회 필수 조건을 적용했습니다.")
    if applied_criteria.get("analysis_filters"):
        basis.append("공정/제품/상태 조건은 분석 단계에서 적용했습니다.")
    if applied_criteria.get("metrics"):
        basis.append("요청 지표를 기준으로 집계했습니다.")
    return basis


def _next_questions(payload: dict[str, Any]) -> list[str]:
    data = _dict(payload.get("data"))
    row_count = _int(data.get("row_count"), len(_list(data.get("rows"))))
    if row_count <= 0:
        return ["조건을 넓혀서 다시 조회할까요?"]
    return ["이 결과를 제품별 또는 공정별로 더 나눠볼까요?", "원본 데이터를 내려받아 상세 Lot/Device를 확인할까요?"]


def _compact_source_results(source_results: list[Any]) -> list[dict[str, Any]]:
    compact = []
    for source in source_results:
        if not isinstance(source, dict):
            continue
        compact.append(
            _omit_empty(
                {
                    "dataset_key": source.get("dataset_key"),
                    "source_alias": source.get("source_alias"),
                    "source_type": source.get("source_type"),
                    "status": source.get("status"),
                    "row_count": source.get("row_count"),
                    "applied_params": source.get("applied_params"),
                    "pandas_filters": source.get("pandas_filters") or source.get("applied_filters"),
                }
            )
        )
    return compact


def _build_next_turn_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = deepcopy(_dict(payload.get("state")))
    state.pop("runtime_sources", None)
    state["last_question"] = _dict(payload.get("request")).get("question", "")
    state["last_answer_message"] = _clip_text(payload.get("answer_message"), 1000)
    state["current_data"] = _current_data_state(payload)
    followup_sources = _followup_source_results(payload)
    if followup_sources:
        state["followup_source_results"] = followup_sources
    runtime_source_refs = _runtime_source_refs(payload)
    if runtime_source_refs:
        state["runtime_source_refs"] = runtime_source_refs
    request = _dict(payload.get("request"))
    if request:
        state["request"] = deepcopy(request)
    intent_plan = _compact_intent_plan(_dict(payload.get("intent_plan")))
    if intent_plan:
        state["last_intent_plan"] = intent_plan
    applied_criteria = _applied_criteria(payload)
    if applied_criteria:
        state["last_applied_criteria"] = applied_criteria
    return _omit_empty(state)


def _current_data_state(payload: dict[str, Any]) -> dict[str, Any]:
    data = _dict(payload.get("data"))
    rows = _list(data.get("rows"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    return _omit_empty(
        {
            "row_count": _int(data.get("row_count"), len(rows)),
            "columns": columns,
            "result_columns": columns,
            "preview_rows": deepcopy(rows[:5]),
            "data_ref": deepcopy(data.get("data_ref")),
            "source_aliases": _source_aliases(payload),
            "source_dataset_keys": _source_dataset_keys(payload),
            "source_columns_by_alias": _source_columns_by_alias(payload),
        }
    )


def _followup_source_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_sources = _dict(payload.get("runtime_sources"))
    result = []
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        rows = _list(runtime_sources.get(alias))
        result.append(
            _omit_empty(
                {
                    "source_alias": alias,
                    "dataset_key": source.get("dataset_key"),
                    "source_type": source.get("source_type"),
                    "row_count": source.get("row_count") if source.get("row_count") is not None else len(rows),
                    "columns": _string_list(source.get("columns")) or _columns_from_rows(rows),
                    "preview_rows": deepcopy(rows[:5]),
                    "data_ref": deepcopy(source.get("data_ref")),
                    "applied_params": deepcopy(source.get("applied_params")),
                    "applied_filters": deepcopy(source.get("applied_filters") or source.get("pandas_filters")),
                }
            )
        )
    return [item for item in result if item]


def _runtime_source_refs(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    for ref in _list(payload.get("data_refs")):
        if not isinstance(ref, dict):
            continue
        if str(ref.get("role") or "") != "source_rows":
            continue
        alias = str(ref.get("source_alias") or "").strip()
        if alias:
            refs[alias] = deepcopy(ref)
    return refs


def _source_aliases(payload: dict[str, Any]) -> list[str]:
    aliases = []
    for source in _list(payload.get("source_results")):
        if isinstance(source, dict):
            alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
            if alias and alias not in aliases:
                aliases.append(alias)
    for alias in _dict(payload.get("runtime_sources")):
        text = str(alias or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def _source_dataset_keys(payload: dict[str, Any]) -> list[str]:
    keys = []
    for source in _list(payload.get("source_results")):
        if isinstance(source, dict):
            key = str(source.get("dataset_key") or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _source_columns_by_alias(payload: dict[str, Any]) -> dict[str, list[str]]:
    runtime_sources = _dict(payload.get("runtime_sources"))
    result: dict[str, list[str]] = {}
    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        if not alias:
            continue
        columns = _string_list(source.get("columns")) or _columns_from_rows(_list(runtime_sources.get(alias)))
        if columns:
            result[alias] = columns
    for alias, rows in runtime_sources.items():
        text = str(alias or "").strip()
        if text and text not in result:
            columns = _columns_from_rows(_list(rows))
            if columns:
                result[text] = columns
    return result


def _compact_intent_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "analysis_kind": plan.get("analysis_kind"),
            "request_scope": plan.get("request_scope"),
            "reuse_strategy": plan.get("reuse_strategy"),
            "condition_resolution": deepcopy(_dict(plan.get("condition_resolution"))),
            "retrieval_jobs": _compact_retrieval_jobs(_list(plan.get("retrieval_jobs"))),
            "pandas_execution_plan": deepcopy(_list(plan.get("pandas_execution_plan"))[:8]),
            "pandas_function_cases": deepcopy(_list(plan.get("pandas_function_cases"))[:5]),
            "output_contract": deepcopy(_dict(plan.get("output_contract"))),
        }
    )


def _compact_retrieval_jobs(jobs: list[Any]) -> list[dict[str, Any]]:
    compact = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        compact.append(
            _omit_empty(
                {
                    "dataset_key": job.get("dataset_key"),
                    "source_alias": job.get("source_alias"),
                    "source_type": job.get("source_type"),
                    "required_params": deepcopy(job.get("required_params")),
                    "filters": deepcopy(job.get("filters")),
                }
            )
        )
    return compact


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


def _metric_columns(payload: dict[str, Any]) -> list[str]:
    data = _dict(payload.get("data"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(_list(data.get("rows")))
    rows = _list(data.get("rows"))
    return [column for column in columns if _column_has_numeric_value(rows, column)]


def _column_has_numeric_value(rows: list[Any], column: str) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None or isinstance(value, bool) or isinstance(value, str):
            continue
        try:
            number = float(value)
        except Exception:
            continue
        if number != number:
            continue
        return True
    return False


def _display_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    if not columns:
        columns = [str(key) for key in row]
    return {column: _display_value(row.get(column, "")) for column in columns}


def _display_value(value: Any) -> Any:
    formatted = _format_number(value)
    if formatted is not None:
        return formatted
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    return value


def _format_number(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or isinstance(value, str):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number != number:
        return None
    if abs(number) >= 10000:
        k_value = number / 1000
        return f"{int(k_value):,}K" if float(k_value).is_integer() else f"{k_value:,.1f}K"
    return f"{int(number):,}" if float(number).is_integer() else f"{number:,.1f}"


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


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


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


class AnswerResponseBuilder(Component):
    display_name = "20 답변 응답 생성기"
    description = "Langflow 에이전트/LLM 답변 문장과 결정된 데이터를 합쳐 페이로드를 완성합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="answer_text", display_name="답변 문장", required=False)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=build_answer_response(getattr(self, "payload", None), getattr(self, "answer_text", "")))
