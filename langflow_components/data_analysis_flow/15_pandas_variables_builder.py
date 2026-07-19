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
from lfx.io import DataInput, Output
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

# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    schemas = _source_schemas(payload)
    previews = {alias: rows[:5] for alias, rows in payload.get("runtime_sources", {}).items() if isinstance(rows, list)}
    return {
        "intent_plan_json": json.dumps(_prompt_intent_plan(payload), ensure_ascii=False, indent=2),
        "source_schema_json": json.dumps(schemas, ensure_ascii=False, indent=2),
        "source_preview_json": json.dumps(previews, ensure_ascii=False, indent=2),
        "function_case_selection_json": json.dumps(_function_case_selection(payload), ensure_ascii=False, indent=2),
        "output_contract_json": json.dumps(_prompt_output_contract(payload), ensure_ascii=False, indent=2),
    }


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
        compact_jobs.append(
            {
                str(key): value
                for key, value in job.items()
                if str(key) not in PROMPT_INTERNAL_JOB_KEYS
            }
        )
    if jobs:
        plan["retrieval_jobs"] = compact_jobs
    return plan


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
    return deepcopy(data) if isinstance(data, dict) else {}


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
class PandasVariablesBuilder(Component):
    display_name = "15 pandas 변수 생성기"
    description = "Langflow 프롬프트 템플릿과 에이전트/LLM에 연결할 pandas 코드 생성 변수를 제공합니다. function case 선택 정보는 16번 Prompt Template에 연결하고, 실제 함수 코드는 별도 입력으로 넣습니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="intent_plan_json", display_name="의도 계획 JSON", method="build_intent_plan_json", types=["Message"], group_outputs=True),
        Output(name="source_schema_json", display_name="소스 스키마 JSON", method="build_source_schema_json", types=["Message"], group_outputs=True),
        Output(name="source_preview_json", display_name="소스 미리보기 JSON", method="build_source_preview_json", types=["Message"], group_outputs=True),
        Output(name="function_case_selection_json", display_name="Function Case 선택 정보 JSON", method="build_function_case_selection_json", types=["Message"], group_outputs=True),
        Output(name="output_contract_json", display_name="출력 계약 JSON", method="build_output_contract_json", types=["Message"], group_outputs=True),
    ]

    # 함수 설명: `_variables_once()`는 다섯 Prompt 변수가 같은 runtime source를 반복 deepcopy·JSON 변환하지 않도록 한 번만 계산합니다.
    def _variables_once(self) -> dict[str, Any]:
        payload = getattr(self, "payload", None)
        cache_key = id(payload)
        if getattr(self, "_variables_cache_key", None) != cache_key:
            self._variables_cache_key = cache_key
            self._variables_cache = build_variables(payload)
        return self._variables_cache

    # Langflow 출력 함수: '의도 계획 JSON (intent_plan_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_intent_plan_json(self) -> Message:
        return Message(text=self._variables_once()["intent_plan_json"])

    # Langflow 출력 함수: '소스 스키마 JSON (source_schema_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_source_schema_json(self) -> Message:
        return Message(text=self._variables_once()["source_schema_json"])

    # Langflow 출력 함수: '소스 미리보기 JSON (source_preview_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_source_preview_json(self) -> Message:
        return Message(text=self._variables_once()["source_preview_json"])

    # Langflow 출력 함수: 'Function Case 선택 정보 JSON (function_case_selection_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_function_case_selection_json(self) -> Message:
        return Message(text=self._variables_once()["function_case_selection_json"])

    # Langflow 출력 함수: '출력 계약 JSON (output_contract_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_contract_json(self) -> Message:
        return Message(text=self._variables_once()["output_contract_json"])
