# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 Workflow Skill 등록 결과 정규화기
# 역할: LLM 후보 JSON을 저장 가능한 Workflow Skill 문서로 정규화하고 실행 계약을 결정론적으로 검증합니다.
# 주요 입력: 요청 페이로드(payload), LLM 응답(llm_response)
# 주요 출력: 페이로드 출력(payload_out)
# 처리 흐름: JSON 파싱 -> 필드 정규화 -> Tool·단계·dependency·handoff 검증 -> 저장 후보 생성
# 유지보수 포인트: LLM 출력이 규칙을 위반하면 자동 보정으로 숨기지 않고 errors에 기록해 Writer에서 차단합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

SECTION = "workflow_skills"
MAX_STEPS = 4
MAX_STEP_QUESTION_CHARS = 4000
MAX_WORKFLOW_PAYLOAD_BYTES = 32768
WORKFLOW_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
ALLOWED_TOOL_NAMES = {
    "run_data_analysis",
    "run_metadata_qa",
    "run_visualization",
    "save_domain_metadata",
    "save_table_catalog_metadata",
    "save_main_flow_filter_metadata",
}
ALLOWED_HANDOFFS = {"none", "result_ref"}
ALLOWED_ON_ERROR = {"stop", "continue"}
RESULT_REF_PRODUCERS = {"run_data_analysis"}
RESULT_REF_CONSUMERS = {"run_data_analysis", "run_visualization"}


# 주요 함수: LLM 응답에서 Workflow Skill 후보 하나를 추출하고 모든 실행 규칙 위반을 오류로 기록합니다.
def normalize_authoring(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    raw_items = _raw_items(parsed)
    errors = []
    if not parsed:
        errors.append({"type": "llm_response_parse_error", "message": "LLM 등록 응답을 JSON object로 해석하지 못했습니다."})
    if len(raw_items) > 1:
        errors.append({"type": "multiple_workflows_not_allowed", "message": "한 요청에서는 Workflow Skill을 한 건만 등록할 수 있습니다."})
    items = []
    for index, raw in enumerate(raw_items[:1]):
        if not isinstance(raw, dict):
            errors.append({"type": "invalid_workflow_item", "message": f"items[{index}]가 object가 아닙니다.", "index": index})
            continue
        item = _normalize_item(raw)
        errors.extend(validate_workflow_item(item, index))
        items.append(item)
    refinement = _refinement(payload, parsed)
    if not items and not refinement["needs_more_input"] and not errors:
        errors.append({"type": "no_valid_workflow", "message": "저장할 Workflow Skill 후보가 생성되지 않았습니다."})
    payload["items"] = items
    payload["refinement"] = refinement
    payload.setdefault("errors", []).extend(errors)
    payload.setdefault("trace", {})["generated_items_preview"] = [
        {
            "key": str(item.get("key") or ""),
            "display_name": str(_dict(item.get("payload")).get("display_name") or ""),
            "step_count": len(_list(_dict(item.get("payload")).get("steps"))),
            "tool_names": [str(_dict(step).get("tool_name") or "") for step in _list(_dict(item.get("payload")).get("steps"))],
        }
        for item in items
    ]
    payload["trace"]["workflow_contract"] = {
        "max_steps": MAX_STEPS,
        "allowed_tools": sorted(ALLOWED_TOOL_NAMES),
        "result_ref_producers": sorted(RESULT_REF_PRODUCERS),
        "result_ref_consumers": sorted(RESULT_REF_CONSUMERS),
    }
    return payload


# 주요 함수: 정규화된 Workflow Skill의 필수 필드와 순차 실행 계약을 결정론적으로 검사합니다.
def validate_workflow_item(item: dict[str, Any], item_index: int = 0) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    key = str(item.get("key") or "")
    item_payload = _dict(item.get("payload"))
    steps = [_dict(step) for step in _list(item_payload.get("steps"))]
    if item.get("section") != SECTION:
        errors.append(_issue("invalid_section", f"section은 {SECTION}이어야 합니다.", item_index=item_index, key=key))
    if not WORKFLOW_KEY_PATTERN.fullmatch(key):
        errors.append(_issue("invalid_workflow_key", "workflow key는 영문 소문자로 시작하는 3~64자의 영문 소문자·숫자·밑줄·하이픈이어야 합니다.", item_index=item_index, key=key))
    if not str(item_payload.get("display_name") or "").strip():
        errors.append(_issue("missing_display_name", "Workflow 표시 이름이 없습니다.", item_index=item_index, key=key))
    if not str(item_payload.get("description") or "").strip():
        errors.append(_issue("missing_description", "Workflow 설명이 없습니다.", item_index=item_index, key=key))
    if not steps:
        errors.append(_issue("missing_steps", "Workflow에는 실행 단계가 한 개 이상 필요합니다.", item_index=item_index, key=key))
    if len(steps) > MAX_STEPS:
        errors.append(_issue("too_many_steps", f"Workflow 실행 단계는 최대 {MAX_STEPS}개입니다.", item_index=item_index, key=key, step_count=len(steps)))
    seen_step_ids: set[str] = set()
    step_by_id: dict[str, dict[str, Any]] = {}
    for step_index, step in enumerate(steps):
        step_id = str(step.get("step_id") or "")
        tool_name = str(step.get("tool_name") or "")
        depends_on = [str(value) for value in _list(step.get("depends_on"))]
        handoff = str(step.get("handoff") or "")
        location = {"item_index": item_index, "key": key, "step_index": step_index, "step_id": step_id}
        if not STEP_ID_PATTERN.fullmatch(step_id):
            errors.append(_issue("invalid_step_id", "step_id는 영문자로 시작하는 1~64자의 영문·숫자·밑줄·하이픈이어야 합니다.", **location))
        if step_id in seen_step_ids:
            errors.append(_issue("duplicate_step_id", "같은 Workflow 안에서 step_id가 중복되었습니다.", **location))
        if tool_name not in ALLOWED_TOOL_NAMES:
            errors.append(_issue("unsupported_tool", f"지원하지 않는 Tool입니다: {tool_name}", **location))
        question = str(step.get("question") or "").strip()
        if not question:
            errors.append(_issue("missing_step_question", "각 단계에는 실행할 question이 필요합니다.", **location))
        elif len(question) > MAX_STEP_QUESTION_CHARS:
            errors.append(_issue("step_question_too_long", f"단계 question은 {MAX_STEP_QUESTION_CHARS}자를 초과할 수 없습니다.", **location))
        if handoff not in ALLOWED_HANDOFFS:
            errors.append(_issue("invalid_handoff", "handoff는 none 또는 result_ref여야 합니다.", **location))
        if str(step.get("on_error") or "") not in ALLOWED_ON_ERROR:
            errors.append(_issue("invalid_on_error", "on_error는 stop 또는 continue여야 합니다.", **location))
        if len(depends_on) != len(set(depends_on)):
            errors.append(_issue("duplicate_dependency", "depends_on에 같은 step_id가 중복되었습니다.", **location))
        for dependency in depends_on:
            if dependency not in seen_step_ids:
                errors.append(_issue("dependency_not_prior", f"depends_on은 앞에서 정의된 step_id만 사용할 수 있습니다: {dependency}", dependency=dependency, **location))
        if step_index == 0 and depends_on:
            errors.append(_issue("first_step_dependency_not_allowed", "첫 단계의 depends_on은 빈 배열이어야 합니다.", **location))
        if step_index == 0 and handoff != "none":
            errors.append(_issue("first_step_handoff_not_allowed", "첫 단계의 handoff는 none이어야 합니다.", **location))
        if handoff == "result_ref":
            if len(depends_on) != 1:
                errors.append(_issue("ambiguous_result_ref_handoff", "result_ref handoff에는 dependency가 정확히 한 개 필요합니다.", **location))
            elif depends_on[0] in step_by_id:
                source_tool = str(step_by_id[depends_on[0]].get("tool_name") or "")
                if source_tool not in RESULT_REF_PRODUCERS:
                    errors.append(_issue("result_ref_source_not_supported", f"{source_tool}은 result_ref를 생성하지 않습니다.", dependency=depends_on[0], **location))
            if tool_name not in RESULT_REF_CONSUMERS:
                errors.append(_issue("result_ref_target_not_supported", f"{tool_name}은 upstream_result_ref 입력을 지원하지 않습니다.", **location))
        seen_step_ids.add(step_id)
        step_by_id[step_id] = step
    payload_bytes = len(json.dumps(item_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if payload_bytes > MAX_WORKFLOW_PAYLOAD_BYTES:
        errors.append(
            _issue(
                "workflow_payload_too_large",
                f"Workflow Skill payload는 UTF-8 {MAX_WORKFLOW_PAYLOAD_BYTES}바이트를 초과할 수 없습니다.",
                item_index=item_index,
                key=key,
                payload_bytes=payload_bytes,
            )
        )
    return _unique_errors(errors)


# 함수 설명: `_raw_items()`는 허용된 LLM 응답 변형에서 Workflow 후보 목록을 꺼냅니다.
def _raw_items(parsed: dict[str, Any]) -> list[Any]:
    if isinstance(parsed.get("items"), list):
        return parsed["items"]
    if isinstance(parsed.get("workflow"), dict):
        return [parsed["workflow"]]
    if isinstance(parsed.get("workflows"), list):
        return parsed["workflows"]
    if parsed.get("workflow_key") or parsed.get("key"):
        return [parsed]
    return []


# 함수 설명: `_normalize_item()`은 LLM의 여러 필드 별칭을 고정된 MongoDB Workflow Skill 문서 형태로 바꿉니다.
def _normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    raw_payload = _dict(raw.get("payload"))
    key = _text(raw.get("key") or raw.get("workflow_key") or raw_payload.get("workflow_key"))
    display_name = _text(raw_payload.get("display_name") or raw_payload.get("title") or raw.get("display_name") or raw.get("title"))
    description = _text(raw_payload.get("description") or raw.get("description"))
    steps_source = raw_payload.get("steps") if isinstance(raw_payload.get("steps"), list) else raw.get("steps")
    steps = [_normalize_step(step, index) for index, step in enumerate(_list(steps_source)) if isinstance(step, dict)]
    return {
        "section": SECTION,
        "key": key,
        "status": "inactive" if str(raw.get("status") or "").strip().lower() == "inactive" else "active",
        "payload": {
            "display_name": display_name,
            "description": description,
            "aliases": _string_list(raw_payload.get("aliases") or raw.get("aliases"), 20, 120),
            "intent_examples": _string_list(raw_payload.get("intent_examples") or raw.get("intent_examples"), 20, 500),
            "keywords": _string_list(raw_payload.get("keywords") or raw.get("keywords"), 30, 80),
            "excluded_keywords": _string_list(raw_payload.get("excluded_keywords") or raw.get("excluded_keywords"), 30, 80),
            "priority": _bounded_int(raw_payload.get("priority", raw.get("priority", 100)), 100, -1000, 1000),
            "steps": steps,
        },
    }


# 함수 설명: `_normalize_step()`은 단계 필드를 08 Workflow Orchestrator가 소비하는 고정 step 계약으로 변환합니다.
def _normalize_step(raw: dict[str, Any], index: int) -> dict[str, Any]:
    del index
    dependencies = raw.get("depends_on")
    if isinstance(dependencies, str):
        dependencies = [dependencies]
    return {
        "step_id": _text(raw.get("step_id") or raw.get("id")),
        "tool_name": _text(raw.get("tool_name") or raw.get("tool")).lower(),
        "question": _text(raw.get("question")),
        "depends_on": _string_list(dependencies, MAX_STEPS, 64),
        "handoff": _text(raw.get("handoff") or "none").lower(),
        "on_error": _text(raw.get("on_error") or "stop").lower(),
    }


# 함수 설명: `_refinement()`은 LLM의 보완 필요 정보와 기존 보정 상태를 손실 없이 합칩니다.
def _refinement(payload: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(_dict(payload.get("refinement")))
    supplied = _dict(parsed.get("refinement"))
    missing = _string_list(supplied.get("missing_information") or parsed.get("missing_information"), 20, 500)
    assumptions = _string_list(supplied.get("assumptions") or parsed.get("assumptions"), 20, 500)
    return {
        "refined_text": _text(supplied.get("refined_text") or current.get("refined_text")),
        "needs_more_input": _truthy(supplied.get("needs_more_input", parsed.get("needs_more_input"))) or bool(missing),
        "missing_information": missing,
        "assumptions": assumptions,
    }


# 함수 설명: `_issue()`는 검증 오류를 type·message와 위치 정보가 있는 표준 dict로 만듭니다.
def _issue(error_type: str, message: str, **details: Any) -> dict[str, Any]:
    return {"type": error_type, "message": message, **details}


# 함수 설명: `_unique_errors()`는 같은 위치에서 반복된 오류를 최초 순서대로 하나만 남깁니다.
def _unique_errors(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for value in values:
        marker = (value.get("type"), value.get("key"), value.get("step_id"), value.get("dependency"), value.get("message"))
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


# 함수 설명: `_string_list()`는 문자열 목록을 공백·중복·길이 제한이 적용된 안전한 목록으로 정리합니다.
def _string_list(value: Any, item_limit: int, text_limit: int) -> list[str]:
    raw = value if isinstance(value, list) else []
    result = []
    seen = set()
    for item in raw:
        text = _text(item)
        text = text[:text_limit]
        marker = text.casefold()
        if text and marker not in seen:
            seen.add(marker)
            result.append(text)
        if len(result) >= item_limit:
            break
    return result


# 함수 설명: `_bounded_int()`는 priority 값을 허용 범위 안의 정수로 제한합니다.
def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except Exception:
        return default


# 함수 설명: `_truthy()`는 LLM의 bool 또는 문자열 표현을 안전한 참·거짓 값으로 해석합니다.
def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


# 함수 설명: `_text()`는 Message 또는 일반 값을 앞뒤 공백이 없는 문자열로 변환합니다.
def _text(value: Any) -> str:
    text = getattr(value, "text", value)
    return "" if text is None else str(text).strip()


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 후속 변경에 안전한 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_json()`은 Message·dict·Markdown JSON 응답에서 object 하나를 안전하게 파싱합니다.
def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = _text(value)
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    elif "{" in text and "}" in text:
        text = text[text.find("{") : text.rfind("}") + 1]
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: `_dict()`는 값이 dict일 때만 그대로 반환하고 다른 형식은 빈 dict로 바꿉니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 값이 list일 때만 그대로 반환하고 다른 형식은 빈 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# Langflow 컴포넌트 클래스: LLM 초안을 결정론적으로 검증한 Workflow Skill 페이로드로 변환합니다.
class WorkflowSkillSavingResultNormalizer(Component):
    display_name = "04 Workflow Skill 등록 결과 정규화기"
    description = "LLM 후보를 08 Workflow Orchestrator 실행 계약에 맞춰 정규화하고 잘못된 단계·Tool·handoff를 차단합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="LLM 응답", required=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 현재 요청과 LLM 응답을 정규화한 Data를 반환합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_authoring(getattr(self, "payload", None), getattr(self, "llm_response", "")))
