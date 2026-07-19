# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 18 답변 생성 변수 생성기
# 역할: Langflow 프롬프트 템플릿과 에이전트/LLM에 연결할 답변 생성 변수를 제공합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 사용자 질문 (question), 결과 요약 JSON (result_summary_json), 적용 범위 JSON (applied_scope_json), 답변 컨텍스트 JSON
#        (answer_context_json), 경고/오류 JSON (warnings_errors_json)
# 처리 흐름: 최종 답변 LLM에 필요한 질문·결과 요약·적용 조건·근거·경고만 안전한 크기로 압축합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    return {
        "question": payload.get("request", {}).get("question", ""),
        "result_summary_json": _json_dumps(payload.get("data", {})),
        "applied_scope_json": _json_dumps(_compact_applied_scope(payload)),
        "answer_context_json": _json_dumps(_answer_context(payload)),
        "warnings_errors_json": _json_dumps({"warnings": payload.get("trace", {}).get("warnings", []), "errors": payload.get("trace", {}).get("errors", [])}),
    }


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_compact_applied_scope()`는 적용 조건·분석 범위에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_compact_source_result()`는 데이터 소스·결과에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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
            "applied_params": source.get("applied_params"),
            "pandas_filters": source.get("pandas_filters"),
            "used_dummy_data": source_execution.get("used_dummy_data"),
            "adapter": source_execution.get("adapter"),
            "params_applied_in_retriever": source_execution.get("params_applied_in_retriever"),
            "filters_applied_in_retriever": source_execution.get("filters_applied_in_retriever"),
        }
    )


# 함수 설명: `_compact_pandas_execution()`는 pandas 실행·execution에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
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


# 함수 설명: `_compact_error()`는 오류에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_error(value: Any) -> Any:
    error = _dict(value)
    if not error:
        return None
    return _omit_empty({"type": error.get("type"), "message": error.get("message")})


# 함수 설명: `_compact_result_store()`는 결과·store에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_result_store(result_store: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "status": result_store.get("status"),
            "data_ref": result_store.get("data_ref"),
            "ttl_hours": result_store.get("ttl_hours"),
            "expires_at": result_store.get("expires_at"),
        }
    )


# 함수 설명: `_answer_context()`는 문맥에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
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


# 함수 설명: `_applied_criteria()`는 조회 작업과 pandas 계획에서 실제 적용된 날짜·제품·공정·지표 조건을 구성합니다.
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


# 함수 설명: `_has_zero_values()`는 입력값이 ZERO·값 조건에 해당하는지 부작용 없이 bool로 판정합니다.
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


# 함수 설명: `_metric_columns()`는 결과 컬럼 중 수량·실적·비율처럼 답변 지표로 사용할 컬럼을 선별합니다.
def _metric_columns(columns: list[Any], rows: list[Any]) -> list[str]:
    result: list[str] = []
    for column in columns:
        text = str(column)
        if _column_has_numeric_value(rows, text):
            result.append(text)
    return result


# 함수 설명: `_dimension_columns()`는 결과 컬럼 중 제품·공정·장비처럼 그룹 기준으로 사용할 컬럼을 선별합니다.
def _dimension_columns(columns: list[Any], metric_columns: list[str]) -> list[str]:
    metric_set = set(metric_columns)
    return [str(column) for column in columns if str(column) not in metric_set]


# 함수 설명: `_column_has_numeric_value()`는 HAS·numeric·값 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
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


# 함수 설명: `_next_question_candidates()`는 question·후보 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _next_question_candidates(data: dict[str, Any]) -> list[str]:
    row_count = _int(data.get("row_count"), len(_list(data.get("rows"))))
    if row_count <= 0:
        return ["조건을 넓혀서 다시 조회할까요?"]
    return ["이 결과를 제품별 또는 공정별로 더 나눠볼까요?", "원본 데이터 기준으로 상세 행을 확인할까요?"]


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 함수 설명: `_omit_empty()`는 dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.
def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if _has_compact_value(item)}


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


# 함수 설명: `_has_compact_value()`는 입력값이 compact·값 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _has_compact_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


# 함수 설명: `_json_dumps()`는 datetime·Decimal 같은 값까지 JSON-safe 형태로 바꾼 뒤 문자열로 직렬화합니다.
def _json_dumps(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, indent=2)


# 함수 설명: `_json_ready()`는 datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.
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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
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

    # 함수 설명: `_variables_once()`는 결과 요약·적용 범위·경고 변수를 동일 payload에서 한 번만 계산해 대용량 행 복사를 줄입니다.
    def _variables_once(self) -> dict[str, Any]:
        payload = getattr(self, "payload", None)
        cache_key = id(payload)
        if getattr(self, "_variables_cache_key", None) != cache_key:
            self._variables_cache_key = cache_key
            self._variables_cache = build_variables(payload)
        return self._variables_cache

    # Langflow 출력 함수: '사용자 질문 (question)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_question(self) -> Message:
        return Message(text=self._variables_once()["question"])

    # Langflow 출력 함수: '결과 요약 JSON (result_summary_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_result_summary(self) -> Message:
        return Message(text=self._variables_once()["result_summary_json"])

    # Langflow 출력 함수: '적용 범위 JSON (applied_scope_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_applied_scope(self) -> Message:
        return Message(text=self._variables_once()["applied_scope_json"])

    # Langflow 출력 함수: '답변 컨텍스트 JSON (answer_context_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_answer_context(self) -> Message:
        return Message(text=self._variables_once()["answer_context_json"])

    # Langflow 출력 함수: '경고/오류 JSON (warnings_errors_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_warnings_errors(self) -> Message:
        return Message(text=self._variables_once()["warnings_errors_json"])
