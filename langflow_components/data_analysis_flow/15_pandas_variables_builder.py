from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    schemas = _source_schemas(payload)
    previews = {alias: rows[:5] for alias, rows in payload.get("runtime_sources", {}).items() if isinstance(rows, list)}
    return {
        "intent_plan_json": json.dumps(payload.get("intent_plan", {}), ensure_ascii=False, indent=2),
        "source_schema_json": json.dumps(schemas, ensure_ascii=False, indent=2),
        "source_preview_json": json.dumps(previews, ensure_ascii=False, indent=2),
        "function_case_selection_json": json.dumps(_function_case_selection(payload), ensure_ascii=False, indent=2),
        "output_contract_json": json.dumps(payload.get("intent_plan", {}).get("output_contract", {}), ensure_ascii=False, indent=2),
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


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


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


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

    def build_intent_plan_json(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["intent_plan_json"])

    def build_source_schema_json(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["source_schema_json"])

    def build_source_preview_json(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["source_preview_json"])

    def build_function_case_selection_json(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["function_case_selection_json"])

    def build_output_contract_json(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["output_contract_json"])

