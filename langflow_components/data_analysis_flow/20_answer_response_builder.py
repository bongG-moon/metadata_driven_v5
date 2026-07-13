# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 20 답변 응답 생성기
# 역할: Langflow 에이전트/LLM 답변 문장과 결정된 데이터를 합쳐 페이로드를 완성합니다.
# 주요 입력: 페이로드 (payload) · 필수, 답변 문장 (answer_text)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: LLM 답변과 결정론적 분석 결과를 합쳐 answer sections, evidence, 현재 상태와 후속 상태를 구성합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from math import isclose
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

TABLE_PREVIEW_LIMIT = 10


# 주요 함수: LLM 문장과 분석 결과를 합쳐 최종 구조화 답변과 다음 상태를 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_answer_response(payload_value: Any, answer_text: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    received_structured_answer = _answer_payload(answer_text)
    received_message = (
        _answer_text_from_dict(received_structured_answer)
        if received_structured_answer
        else _answer_text(answer_text)
    ).strip()
    blocked = _execution_blocked(payload)
    structured_answer = {} if blocked else received_structured_answer
    message = str(payload.get("answer_message") or "").strip() if blocked else received_message
    if not message:
        message = str(payload.get("answer_message") or "").strip()
    if not message:
        row_count = payload.get("data", {}).get("row_count", 0)
        message = f"분석 결과 {row_count}건을 확인했습니다." if payload.get("analysis", {}).get("status") == "ok" else "분석을 완료하지 못했습니다. trace의 오류를 확인해 주세요."
    next_payload = payload
    next_payload.setdefault("trace", {}).setdefault("inspection", {})["answer_model_response"] = {
        "stage": "20_answer_response_builder",
        "received": bool(received_message or received_structured_answer),
        "used": not blocked and bool(received_message or received_structured_answer),
        "ignored": blocked and bool(received_message or received_structured_answer),
        "policy": "ignore" if blocked else "use",
    }
    message, grounding = _ground_answer_message(next_payload, message)
    if grounding:
        trace = next_payload.setdefault("trace", {})
        trace.setdefault("warnings", []).append(
            {
                "type": "answer_value_grounded",
                "message": "LLM 답변의 수치가 실제 결과 행과 일치하지 않아 결과 행 기준 문장으로 교정했습니다.",
            }
        )
        trace.setdefault("inspection", {})["answer_grounding"] = grounding
    next_payload["answer_message"] = message
    next_payload["answer_sections"] = _build_answer_sections(next_payload, message, _dict(structured_answer.get("answer_sections")))
    next_payload["state"] = _build_next_turn_state(next_payload)
    return next_payload


# 함수 설명: `_execution_blocked()`는 필수 조회 실패 시 기본 Language Model 응답을 최종 답변에 사용하지 않도록 판정합니다.
def _execution_blocked(payload: dict[str, Any]) -> bool:
    gate = _dict(payload.get("execution_gate"))
    return str(gate.get("status") or "").strip().lower() == "blocked"


# 함수 설명: `_ground_answer_message()`는 LLM 문장에 결과 행으로 확인되지 않는 수치가 있을 때 재호출 없이 결정론적으로 교정합니다.
def _ground_answer_message(payload: dict[str, Any], message: str) -> tuple[str, dict[str, Any]]:
    analysis = _dict(payload.get("analysis"))
    data = _dict(payload.get("data"))
    rows = _list(data.get("rows"))
    if analysis.get("status") != "ok" or not rows or not message:
        return message, {}

    unsupported = _unsupported_numeric_claims(payload, message)
    if not unsupported:
        return message, {}

    grounded_message = _authoritative_result_message(data)
    if not grounded_message:
        return message, {}
    return grounded_message, {
        "stage": "20_answer_response_builder",
        "status": "corrected",
        "unsupported_numeric_claims": unsupported,
        "policy": "deterministic_data_rows",
    }


# 함수 설명: `_unsupported_numeric_claims()`는 질문 조건이나 실제 결과에 없는 LLM 수치 주장만 선별합니다.
def _unsupported_numeric_claims(payload: dict[str, Any], message: str) -> list[str]:
    known_values = _known_numeric_values(payload)
    unsupported: list[str] = []
    for raw, number in _numeric_claims(message):
        claim_values = [number, number * 100] if raw.endswith("%") else [number]
        if any(
            isclose(claim, known, rel_tol=1e-9, abs_tol=1e-9)
            for claim in claim_values
            for known in known_values
        ):
            continue
        unsupported.append(raw)
    return unsupported


# 함수 설명: `_known_numeric_values()`는 결과 행과 질문 조건에서 답변에 나타나도 되는 수치 집합을 구성합니다.
def _known_numeric_values(payload: dict[str, Any]) -> list[float]:
    data = _dict(payload.get("data"))
    values: list[float] = []
    for row in _list(data.get("rows")):
        if not isinstance(row, dict):
            continue
        for value in row.values():
            values.extend(_numbers_from_value(value))
    values.extend(_numbers_from_value(data.get("row_count")))
    values.extend(_numbers_from_value(_dict(payload.get("request"))))
    values.extend(_numbers_from_value(_dict(payload.get("intent_plan")).get("retrieval_jobs")))
    return _dedupe_numbers(values)


# 함수 설명: `_numbers_from_value()`는 숫자와 날짜형 문자열을 재귀적으로 읽어 비교 가능한 실수 값으로 바꿉니다.
def _numbers_from_value(value: Any) -> list[float]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except Exception:
            return []
        return [] if number != number else [number]
    if isinstance(value, dict):
        return [number for item in value.values() for number in _numbers_from_value(item)]
    if isinstance(value, (list, tuple, set)):
        return [number for item in value for number in _numbers_from_value(item)]
    text = str(value or "").strip()
    if not text:
        return []
    numbers = [number for _, number in _numeric_claims(text)]
    for date_text in re.findall(r"(?<!\d)(\d{8})(?!\d)", text):
        numbers.extend([float(date_text[:4]), float(date_text[4:6]), float(date_text[6:8])])
    return numbers


# 함수 설명: `_numeric_claims()`는 제품 코드 안의 숫자는 제외하고 일반 수치·K/M 단위·퍼센트 표현을 파싱합니다.
def _numeric_claims(text: str) -> list[tuple[str, float]]:
    pattern = re.compile(r"(?<![A-Za-z0-9_/.-])([+-]?\d[\d,]*(?:\.\d+)?)([KkMm]?)(%)?(?![A-Za-z0-9_/.-])")
    claims: list[tuple[str, float]] = []
    for match in pattern.finditer(str(text or "")):
        raw = match.group(0).strip()
        try:
            number = float(match.group(1).replace(",", ""))
        except Exception:
            continue
        unit = match.group(2).lower()
        if unit == "k":
            number *= 1000
        elif unit == "m":
            number *= 1_000_000
        if match.group(3):
            number /= 100
        claims.append((raw, number))
    return claims


# 함수 설명: `_authoritative_result_message()`는 실제 data.rows만 이용해 수치 모순이 없는 짧은 대체 문장을 만듭니다.
def _authoritative_result_message(data: dict[str, Any]) -> str:
    rows = [row for row in _list(data.get("rows")) if isinstance(row, dict)]
    if not rows:
        return ""
    columns = _string_list(data.get("columns")) or _columns_from_rows(rows)
    first_row = rows[0]
    facts = []
    for column in columns[:6]:
        if column not in first_row:
            continue
        value = _display_value(first_row.get(column))
        facts.append(f"{column}={value}")
    row_count = _int(data.get("row_count"), len(rows))
    if row_count <= 1:
        return f"분석 결과 {', '.join(facts)}입니다." if facts else "분석 결과 1건입니다."
    prefix = f"분석 결과 총 {row_count:,}건입니다."
    return f"{prefix} 첫 번째 결과는 {', '.join(facts)}입니다." if facts else prefix


# 함수 설명: `_dedupe_numbers()`는 부동소수 비교 오차를 고려해 숫자 목록의 중복을 제거합니다.
def _dedupe_numbers(values: list[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if not any(isclose(value, existing, rel_tol=1e-12, abs_tol=1e-12) for existing in result):
            result.append(value)
    return result


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_answer_text()`는 문자열에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
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


# 함수 설명: `_answer_payload()`는 페이로드에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _answer_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return deepcopy(data)
    text = _message_text(value).strip()
    return _json_text(text)


# 함수 설명: `_answer_text_from_dict()`는 문자열·원본·DICT에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _answer_text_from_dict(value: dict[str, Any]) -> str:
    for key in ("answer_message", "answer", "text", "message", "output"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    data = value.get("data")
    if isinstance(data, dict):
        return _answer_text_from_dict(data)
    return ""


# 함수 설명: `_message_text()`는 문자열에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
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


# 함수 설명: `_json_text()`는 LLM 답변에서 Markdown fence를 제거하고 JSON object 문자열만 추출합니다.
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


# 함수 설명: `_build_answer_sections()`는 답변·응답 section 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
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


# 함수 설명: `_applied_criteria()`는 조회 작업과 pandas 계획에서 실제 적용된 날짜·제품·공정·지표 조건을 구성합니다.
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


# 함수 설명: `_evidence()`는 조회·pandas 실행 trace에서 답변 수치의 데이터셋과 조건 근거를 구성합니다.
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


# 함수 설명: `_notices()`는 warnings와 errors를 사용자에게 보여 줄 중복 없는 안내 목록으로 정리합니다.
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


# 함수 설명: `_downloads()`는 저장된 data_ref에서 최종 답변에 제공할 다운로드 항목을 구성합니다.
def _downloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    for ref in _list(payload.get("data_refs")):
        if isinstance(ref, dict):
            refs.append(deepcopy(ref))
    data_ref = _dict(_dict(payload.get("data")).get("data_ref"))
    if data_ref:
        refs.append(deepcopy(data_ref))
    return _dedupe_dicts(refs)


# 함수 설명: `_summary_basis()`는 답변 요약이 어떤 rows·지표·조건을 기준으로 작성됐는지 근거를 구성합니다.
def _summary_basis(applied_criteria: dict[str, Any]) -> list[str]:
    basis = []
    if applied_criteria.get("required_params"):
        basis.append("조회 필수 조건을 적용했습니다.")
    if applied_criteria.get("analysis_filters"):
        basis.append("공정/제품/상태 조건은 분석 단계에서 적용했습니다.")
    if applied_criteria.get("metrics"):
        basis.append("요청 지표를 기준으로 집계했습니다.")
    return basis


# 함수 설명: `_next_questions()`는 questions 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _next_questions(payload: dict[str, Any]) -> list[str]:
    data = _dict(payload.get("data"))
    row_count = _int(data.get("row_count"), len(_list(data.get("rows"))))
    if row_count <= 0:
        return ["조건을 넓혀서 다시 조회할까요?"]
    return ["이 결과를 제품별 또는 공정별로 더 나눠볼까요?", "원본 데이터를 내려받아 상세 Lot/Device를 확인할까요?"]


# 함수 설명: `_compact_source_results()`는 데이터 소스·결과에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_build_next_turn_state()`는 다음 단계·TURN·상태 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
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


# 함수 설명: `_current_data_state()`는 현재 결과의 rows·columns·row_count·data_ref를 다음 질문용 작은 상태로 만듭니다.
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


# 함수 설명: `_followup_source_results()`는 후속 질문이 재사용할 source result를 preview와 참조 중심으로 압축합니다.
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


# 함수 설명: `_runtime_source_refs()`는 메모리의 runtime source를 직접 저장하지 않고 재조회 가능한 source 참조만 구성합니다.
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


# 함수 설명: `_source_aliases()`는 aliases 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
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


# 함수 설명: `_source_dataset_keys()`는 데이터셋·key 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_dataset_keys(payload: dict[str, Any]) -> list[str]:
    keys = []
    for source in _list(payload.get("source_results")):
        if isinstance(source, dict):
            key = str(source.get("dataset_key") or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


# 함수 설명: `_source_columns_by_alias()`는 컬럼·BY·alias 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
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


# 함수 설명: `_compact_intent_plan()`는 의도 계획·PLAN에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_compact_retrieval_jobs()`는 데이터 조회·조회 작업에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_group_by_columns()`는 의도 계획의 pandas 단계에서 실제 그룹 기준 컬럼을 추출합니다.
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


# 함수 설명: `_metric_columns()`는 결과 컬럼 중 수량·실적·비율처럼 답변 지표로 사용할 컬럼을 선별합니다.
def _metric_columns(payload: dict[str, Any]) -> list[str]:
    data = _dict(payload.get("data"))
    columns = _string_list(data.get("columns")) or _columns_from_rows(_list(data.get("rows")))
    rows = _list(data.get("rows"))
    return [column for column in columns if _column_has_numeric_value(rows, column)]


# 함수 설명: `_column_has_numeric_value()`는 HAS·numeric·값 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
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


# 함수 설명: `_display_row()`는 행을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다.
def _display_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    if not columns:
        columns = [str(key) for key in row]
    return {column: _display_value(row.get(column, "")) for column in columns}


# 함수 설명: `_display_value()`는 None·숫자·복합 값을 사용자에게 읽기 좋은 짧은 문자열로 표시합니다.
def _display_value(value: Any) -> Any:
    formatted = _format_number(value)
    if formatted is not None:
        return formatted
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    return value


# 함수 설명: `_format_number()`는 number을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다.
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


# 함수 설명: `_columns_from_rows()`는 행 목록의 key 등장 순서를 유지하면서 결과 테이블의 컬럼 목록을 계산합니다.
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


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 함수 설명: `_clip_text()`는 문자열을 허용 길이 안으로 자르되 비어 있는 값과 말줄임 표시를 일관되게 처리합니다.
def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


# 함수 설명: `_omit_empty()`는 dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.
def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


# 함수 설명: `_dedupe_dicts()`는 dicts의 중복을 제거하고 최초 등장 순서를 유지합니다.
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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class AnswerResponseBuilder(Component):
    display_name = "20 답변 응답 생성기"
    description = "Langflow 에이전트/LLM 답변 문장과 결정된 데이터를 합쳐 페이로드를 완성합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="answer_text", display_name="답변 문장", required=False)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_answer_response(getattr(self, "payload", None), getattr(self, "answer_text", "")))
