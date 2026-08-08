# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 15 pandas 변수 생성기
# 역할: Langflow 프롬프트 템플릿과 에이전트/LLM에 연결할 pandas 코드 생성 변수를 제공합니다. function case 선택 정보는 16번 Prompt Template에 연결하고, 실제 함수
#     코드는 별도 입력으로 넣습니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 의도 계획 JSON (intent_plan_json), 소스 스키마 JSON (source_schema_json), 소스 미리보기 JSON (source_preview_json),
#        Function Case 선택 정보 JSON (function_case_selection_json), 출력 계약 JSON (output_contract_json)
# 처리 흐름: pandas 코드 LLM에 전달할 의도 계획, source schema/preview, 선택 helper와 출력 계약을 분리해 만듭니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, MultilineInput, Output
from lfx.schema.message import Message

PROMPT_INTERNAL_JOB_KEYS = {
    "source_config",
    "filter_mappings",
    "standard_column_aliases",
    "row_identity_columns",
    "default_detail_columns",
    "context_columns",
    "required_param_names",
    "trusted_catalog",
    "catalog_ref",
}
RETIRED_OUTPUT_CONTRACT_KEYS = {"row_identity_columns", "context_columns", "default_detail_columns"}
PANDAS_PREVIEW_ROW_LIMIT = 2
PANDAS_PREVIEW_COLUMN_LIMIT = 16
PANDAS_PREVIEW_CELL_CHAR_LIMIT = 160
PREVIEW_COLUMN_KEYS = {
    "column",
    "source_column",
    "output_column",
    "sort_by",
    "primary_metric",
    "lhs_metric_column",
    "rhs_metric_column",
    "left_column",
    "right_column",
}
PREVIEW_COLUMN_LIST_KEYS = {
    "columns",
    "required_columns",
    "result_columns",
    "grain_columns",
    "metric_columns",
    "group_by",
    "projection",
    "join_keys",
    "left_keys",
    "right_keys",
    "right_value_columns",
    "default_token_columns",
}

# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    schemas = _source_schemas(payload)
    if isinstance(payload.get("simple_analysis_contract"), dict):
        previews = _source_previews(payload, schemas)
    else:
        # Preserve the V1 legacy model view exactly. V2 adds the typed routing
        # contract before this builder and receives the bounded projection.
        previews = {
            str(alias): deepcopy(rows[:5])
            for alias, rows in payload.get("runtime_sources", {}).items()
            if isinstance(rows, list)
        }
    return {
        "intent_plan_json": _compact_json(_prompt_intent_plan(payload)),
        "source_schema_json": _compact_json(schemas),
        "source_preview_json": _compact_json(previews),
        "function_case_selection_json": _compact_json(_function_case_selection(payload)),
        "output_contract_json": _compact_json(_prompt_output_contract(payload)),
    }


# 함수 설명: Fast/Complex 공통 helper 선택에 필요한 작은 계약만 직렬화합니다.
def build_function_case_selection_only(payload_value: Any) -> str:
    """Build only the small helper-selection contract needed before prompt routing."""

    payload = _payload(payload_value)
    return _compact_json(_function_case_selection(payload))


# 함수 설명: V2 경로가 Complex일 때만 전체 pandas 생성 프롬프트를 조립합니다.
def build_route_aware_pandas_prompt(
    payload_value: Any,
    prompt_template: Any,
    function_case_helper_code: Any = "",
) -> str:
    """Render the pandas prompt only after the V2 resolver selected Complex."""

    payload = _payload(payload_value)
    contract = payload.get("simple_analysis_contract") if isinstance(payload.get("simple_analysis_contract"), dict) else {}
    if str(contract.get("route") or "complex").strip().lower() != "complex":
        return ""
    if contract.get("requires_pandas_llm") is False:
        return ""
    variables = build_variables(payload)
    variables["function_case_helper_code"] = _text(function_case_helper_code)
    return _render_prompt_template(prompt_template, variables)


# 함수 설명: pandas 프롬프트 템플릿과 확정된 변수 계약을 안전하게 결합합니다.
def _render_prompt_template(template_value: Any, variables: dict[str, Any]) -> str:
    template = _text(template_value)
    if not template:
        return ""
    try:
        return template.format(**{str(key): str(value) for key, value in variables.items()})
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"pandas prompt template rendering failed: {exc}") from exc


# 함수 설명: Langflow Message 또는 일반 값을 프롬프트용 문자열로 정규화합니다.
def _text(value: Any) -> str:
    text = getattr(value, "text", value)
    return "" if text is None else str(text)


# 함수 설명: `_compact_json()`은 prompt 의미는 유지하면서 들여쓰기와 구분자 공백을 제거해 입력 token을 줄입니다.
def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# 함수 설명: `_prompt_intent_plan()`은 executor 전용 카탈로그 설정과 별도 출력 계약을 제거해 pandas LLM 입력 token 중복을 줄입니다.
def _prompt_intent_plan(payload: dict[str, Any]) -> dict[str, Any]:
    plan = deepcopy(payload.get("intent_plan")) if isinstance(payload.get("intent_plan"), dict) else {}
    plan.pop("output_contract", None)
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    compact_jobs: list[Any] = []
    for job in jobs:
        if not isinstance(job, dict):
            compact_jobs.append(job)
            continue
        compact_job = {
            str(key): deepcopy(value)
            for key, value in job.items()
            if str(key) not in PROMPT_INTERNAL_JOB_KEYS
        }
        semantics = compact_job.get("metric_semantics")
        if isinstance(semantics, dict):
            compact_job["metric_semantics"] = {
                str(metric): {
                    str(key): deepcopy(item)
                    for key, item in contract.items()
                    if str(key) != "value_transform"
                }
                if isinstance(contract, dict)
                else deepcopy(contract)
                for metric, contract in semantics.items()
            }
        compact_jobs.append(compact_job)
    if jobs:
        plan["retrieval_jobs"] = compact_jobs
    return _canonical_prompt_contract(plan)


# 함수 설명: pandas LLM에는 표준 실행 컬럼만 노출하고 물리 컬럼 후보는
# retrieval/validation lineage 내부에만 남깁니다.
def _canonical_prompt_contract(value: Any) -> Any:
    physical_lineage_keys = {
        "source_candidates",
        "left_candidates",
        "right_candidates",
        "column_mappings",
        "key_mappings",
        "right_value_mappings",
    }
    if isinstance(value, list):
        return [_canonical_prompt_contract(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    return {
        str(key): _canonical_prompt_contract(item)
        for key, item in value.items()
        if str(key) not in physical_lineage_keys
    }


# 함수 설명: `_prompt_output_contract()`는 폐기된 상세 계약 key를 제거한 canonical 출력 계약만 pandas prompt에 전달합니다.
def _prompt_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    return {
        str(key): deepcopy(value)
        for key, value in contract.items()
        if str(key) not in RETIRED_OUTPUT_CONTRACT_KEYS
    }


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return data if isinstance(data, dict) else {}


# 함수 설명: `_source_schemas()`는 schemas 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_schemas(payload: dict[str, Any]) -> dict[str, list[str]]:
    schemas: dict[str, list[str]] = {}
    for source in payload.get("source_results", []) if isinstance(payload.get("source_results"), list) else []:
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        columns = _string_list(source.get("columns"))
        if alias and columns:
            schemas[alias] = columns
    for alias, rows in payload.get("runtime_sources", {}).items() if isinstance(payload.get("runtime_sources"), dict) else []:
        if not isinstance(rows, list):
            continue
        row_columns = sorted({str(column) for row in rows[:20] if isinstance(row, dict) for column in row})
        if row_columns:
            schemas[str(alias)] = row_columns
        else:
            schemas.setdefault(str(alias), [])
    return schemas


# 함수 설명: `_source_previews()`는 previews 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_previews(
    payload: dict[str, Any],
    schemas: dict[str, list[str]],
) -> dict[str, list[dict[str, Any]]]:
    """Build a bounded, execution-relevant model view without changing runtime rows."""

    relevant = _relevant_preview_columns(payload)
    previews: dict[str, list[dict[str, Any]]] = {}
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    for alias, rows in runtime_sources.items():
        alias_text = str(alias)
        if not isinstance(rows, list):
            continue
        schema = _string_list(schemas.get(alias_text))
        selected = [column for column in relevant if column in schema]
        if len(selected) < PANDAS_PREVIEW_COLUMN_LIMIT:
            selected.extend(
                column
                for column in schema
                if column not in selected
            )
        selected = selected[:PANDAS_PREVIEW_COLUMN_LIMIT]
        previews[alias_text] = [
            {
                column: _bounded_preview_value(row.get(column))
                for column in selected
                if column in row
            }
            for row in rows[:PANDAS_PREVIEW_ROW_LIMIT]
            if isinstance(row, dict)
        ]
    return previews


# 함수 설명: `_relevant_preview_columns()`는 15 pandas 변수 생성기 처리 중 미리보기·컬럼 관련 값을 계산·변환하는 내부 helper입니다.
def _relevant_preview_columns(payload: dict[str, Any]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    columns: list[str] = []

    # 함수 설명: `add()`는 여러 ADD 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다.
    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in columns:
            columns.append(text)

    # 함수 설명: `walk()`는 15 pandas 변수 생성기 처리 중 WALK 관련 값을 계산·변환하는 내부 helper입니다.
    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            key_text = str(key)
            if key_text in PREVIEW_COLUMN_KEYS:
                add(item)
            elif key_text in PREVIEW_COLUMN_LIST_KEYS and isinstance(item, list):
                for column in item:
                    add(column)
            elif key_text == "filters" and isinstance(item, dict):
                for column in item:
                    add(column)
            elif key_text == "metric_semantics" and isinstance(item, dict):
                for column in item:
                    add(column)
            walk(item)

    walk(plan.get("pandas_execution_plan"))
    walk(plan.get("output_contract"))
    walk(plan.get("retrieval_jobs"))
    walk(plan.get("pandas_function_cases"))
    return columns


# 함수 설명: `_bounded_preview_value()`는 미리보기·값이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _bounded_preview_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= PANDAS_PREVIEW_CELL_CHAR_LIMIT:
            return value
        return value[: PANDAS_PREVIEW_CELL_CHAR_LIMIT - 3] + "..."
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= PANDAS_PREVIEW_CELL_CHAR_LIMIT:
        return rendered
    return rendered[: PANDAS_PREVIEW_CELL_CHAR_LIMIT - 3] + "..."


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_function_case_selection()`는 Function Case·selection 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _function_case_selection(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    selected_steps = [
        deepcopy(step)
        for step in steps
        if isinstance(step, dict)
        and str(step.get("operation") or "").strip() == "apply_pandas_function_case"
    ]
    selected_cases = _selected_function_cases(plan, selected_steps)
    return {
        "selected_cases": selected_cases,
        "selected_steps": selected_steps,
        "available_helpers": _helpers_from_selected_cases(selected_cases),
    }


# 함수 설명: `_helpers_from_selected_cases()`는 선택 Function Case 항목에서 pandas 프롬프트에 제공할 helper 이름만 추출합니다.
def _helpers_from_selected_cases(selected_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    helpers = []
    for item in selected_cases:
        if not isinstance(item, dict):
            continue
        name = str(item.get("function_name") or "").strip()
        if not name or any(helper.get("function_name") == name for helper in helpers):
            continue
        helper = {"function_name": name}
        for key in (
            "signature",
            "description",
            "usage_rule",
            "default_token_columns",
        ):
            if item.get(key) not in (None, "", [], {}):
                helper[key] = deepcopy(item.get(key))
        helpers.append(helper)
    return helpers


# 함수 설명: `_selected_function_cases()`는 의도 계획에서 실제 pandas 실행에 선택된 Function Case 항목만 정리합니다.
def _selected_function_cases(plan: dict[str, Any], selected_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases = []
    single = plan.get("pandas_function_case")
    if isinstance(single, dict) and single:
        cases.append(deepcopy(single))
    for item in plan.get("pandas_function_cases", []) if isinstance(plan.get("pandas_function_cases"), list) else []:
        if isinstance(item, dict) and item not in cases:
            cases.append(deepcopy(item))
    for step in selected_steps:
        item = {
            "key": step.get("function_case_key", ""),
            "function_name": step.get("function_name", ""),
            "input_text": step.get("input_text", ""),
            "source_alias": step.get("source_alias", ""),
        }
        if item not in cases:
            cases.append(item)
    return _dedupe_cases(cases)


# 함수 설명: `_dedupe_cases()`는 cases의 중복을 제거하고 최초 등장 순서를 유지합니다.
def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in cases:
        marker = (
            str(item.get("function_name") or ""),
            str(item.get("key") or item.get("function_case_key") or ""),
            str(item.get("input_text") or ""),
            str(item.get("source_alias") or ""),
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(deepcopy(item))
    return deduped


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.

# Langflow 컴포넌트 클래스: V2 경로 결정 뒤 pandas 프롬프트를 지연 생성합니다.
class RouteAwarePandasPromptBuilder(Component):
    display_name = "16 V2 경로 인식 pandas Prompt 생성기"
    description = "Complex 경로에서만 pandas prompt 변수를 직렬화하고 Fast/Blocked에서는 빈 prompt를 반환합니다."
    inputs = [
        DataInput(name="payload", display_name="경로 결정 페이로드", required=True),
        MultilineInput(name="prompt_template", display_name="pandas 프롬프트 템플릿", required=True),
        MessageTextInput(
            name="function_case_helper_code",
            display_name="선택 Function Case Helper",
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(name="pandas_prompt", display_name="경로 인식 pandas Prompt", method="build_prompt", types=["Message"])
    ]

    # 함수 설명: Complex 경로에서만 전체 pandas 생성 프롬프트를 실제 문자열로 만듭니다.
    def build_prompt(self) -> Message:
        """Materialize the full prompt only after the resolver selected Complex."""

        return Message(
            text=build_route_aware_pandas_prompt(
                getattr(self, "payload", None),
                getattr(self, "prompt_template", ""),
                getattr(self, "function_case_helper_code", ""),
            )
        )
