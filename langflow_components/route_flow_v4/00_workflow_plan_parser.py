# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 Workflow 계획 파서
# 역할: inline JSON/Markdown 또는 Workflow Registry 로더 후보를 최대 4단계 workflow.plan.v1 계약으로 정규화합니다.
# 주요 입력: Workflow 정의, Workflow 키, Workflow JSON Registry, 사용자 원문 질문, 허용 Tool 이름
# 주요 출력: 검증 결과 Data, 기본 Langflow Loop용 DataFrame, Loop용 Data 목록
# 처리 흐름: 후보 registry 우선 해석 -> JSON/Markdown 파싱 -> 등록 key 재확정 -> 필드 정규화 -> dependency/handoff/on_error 검증 -> Loop 행 생성
# 유지보수 포인트: DB 조회는 00A가 담당하며 이 Parser는 모호한 registry·미래 dependency·4단계 초과를 실행 전에 차단합니다.
# =============================================================================

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.dataframe import DataFrame

PLAN_CONTRACT_VERSION = "workflow.plan.v1"
MAX_WORKFLOW_STEPS = 4
MAX_QUESTION_CHARS = 4000
STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
ALLOWED_HANDOFFS = {"none", "result_ref"}
ALLOWED_ON_ERROR = {"stop", "continue"}


# 주요 함수: 입력 정의와 registry를 해석해 실행 가능 여부, 정규화 계획, 오류를 하나의 표준 결과로 반환합니다.
def parse_workflow_plan(
    workflow_input: Any = "",
    workflow_key: Any = "",
    workflow_registry_json: Any = "",
    user_question: Any = "",
    allowed_tools_value: Any = None,
    workflow_run_id: Any = "",
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    requested_key = str(workflow_key or "").strip()
    source_value = _text(workflow_input)
    registry_value, registry_errors = _parse_registry(workflow_registry_json)
    errors.extend(registry_errors)

    # Playground에 registry key만 입력한 경우에는 계획 모델 출력보다 등록 정의를 우선합니다.
    # 한국어 자연어 질문은 key 형식과 다르고, 영문 단어가 미등록 key이면 기존 모델 계획을 그대로 해석합니다.
    question_text = _text(user_question).strip()
    question_key = _inline_workflow_key(question_text)
    inline_key = _inline_workflow_key(source_value)
    selected_key = requested_key
    raw_plan: Any = None
    source_kind = "inline"
    if requested_key:
        raw_plan, lookup_errors = _lookup_registry_workflow(registry_value, selected_key)
        errors.extend(lookup_errors)
        if isinstance(raw_plan, dict) and raw_plan:
            selected_key = str(raw_plan.get("workflow_key") or selected_key).strip()
        source_kind = "registry"
    elif question_text and not errors:
        registered_plan, question_lookup_errors = _lookup_registry_identifier(registry_value, question_text)
        if not question_lookup_errors:
            selected_key = str(registered_plan.get("workflow_key") or question_key).strip()
            raw_plan = registered_plan
            source_kind = "registry"
    if raw_plan is None and inline_key:
        selected_key = inline_key
        raw_plan, lookup_errors = _lookup_registry_workflow(registry_value, selected_key)
        errors.extend(lookup_errors)
        source_kind = "registry"
    elif raw_plan is None and not errors:
        raw_plan, parse_errors = _parse_inline_workflow(source_value)
        errors.extend(parse_errors)

    # 계획 모델이 등록 key와 함께 단계를 다시 작성했더라도 현재 Registry의 canonical 정의를 실행합니다.
    # 미등록 key나 workflow_key=inline은 동적 inline 계획으로 유지해 Registry 장애를 seed fallback으로 숨기지 않습니다.
    if isinstance(raw_plan, dict) and raw_plan and source_kind == "inline":
        planned_key = str(raw_plan.get("workflow_key") or raw_plan.get("key") or "").strip()
        if planned_key and planned_key.lower() != "inline":
            registered_plan, planned_lookup_errors = _lookup_registry_workflow(registry_value, planned_key)
            if not planned_lookup_errors:
                raw_plan = registered_plan
                selected_key = str(registered_plan.get("workflow_key") or planned_key).strip()
                source_kind = "registry"

    if not isinstance(raw_plan, dict):
        raw_plan = {}
    normalized = normalize_workflow_plan(
        raw_plan,
        user_question=user_question,
        selected_workflow_key=selected_key,
        workflow_run_id=workflow_run_id,
    )
    errors.extend(validate_workflow_plan(normalized, allowed_tools_value))
    status = "error" if errors else "ok"
    if status == "error":
        # invalid step을 Loop로 보내지 않도록 실제 실행용 steps는 빈 목록으로 닫습니다.
        executable_plan = deepcopy(normalized)
        executable_plan["steps"] = []
    else:
        executable_plan = normalized
    return {
        "status": status,
        "contract_version": PLAN_CONTRACT_VERSION,
        "workflow_plan": executable_plan,
        "normalized_plan": normalized,
        "source_kind": source_kind,
        "errors": errors,
    }


# 주요 함수: 여러 입력 형태의 필드를 고정 workflow.plan.v1 이름과 자료형으로 변환합니다.
def normalize_workflow_plan(
    raw_plan: Any,
    *,
    user_question: Any = "",
    selected_workflow_key: Any = "",
    workflow_run_id: Any = "",
) -> dict[str, Any]:
    source = deepcopy(raw_plan) if isinstance(raw_plan, dict) else {}
    question = str(user_question or "").strip()
    raw_steps = source.get("steps") if isinstance(source.get("steps"), list) else []
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        step = raw_step if isinstance(raw_step, dict) else {}
        depends_on = _string_list(step.get("depends_on"))
        step_question = str(step.get("question") or "").strip()
        if question:
            step_question = step_question.replace("{{user_question}}", question).replace("${user_question}", question)
        steps.append(
            {
                "step_index": index + 1,
                "step_id": str(step.get("step_id") or step.get("id") or "").strip(),
                "tool_name": str(step.get("tool_name") or step.get("tool") or "").strip(),
                "question": step_question,
                "depends_on": depends_on,
                "handoff": str(step.get("handoff") or "none").strip().lower(),
                "on_error": str(step.get("on_error") or "stop").strip().lower(),
            }
        )
    key = str(selected_workflow_key or source.get("workflow_key") or source.get("key") or "inline").strip()
    run_id = str(workflow_run_id or "").strip() or f"workflow:{uuid.uuid4().hex}"
    return {
        "contract_version": PLAN_CONTRACT_VERSION,
        "workflow_run_id": run_id,
        "workflow_key": key,
        "title": str(source.get("title") or source.get("display_name") or key).strip(),
        "description": str(source.get("description") or "").strip(),
        "user_question": question,
        "max_steps": MAX_WORKFLOW_STEPS,
        "steps": steps,
    }


# 주요 함수: 단계 수·이름·질문·dependency 순서·handoff·오류 정책을 검사하고 모든 위반을 표준 오류 목록으로 반환합니다.
def validate_workflow_plan(plan: Any, allowed_tools_value: Any = None) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    value = plan if isinstance(plan, dict) else {}
    steps = value.get("steps") if isinstance(value.get("steps"), list) else []
    allowed_tools = set(_allowed_tools(allowed_tools_value))
    if not steps:
        errors.append(_issue("workflow_steps_missing", "Workflow에는 실행 단계가 1개 이상 필요합니다."))
        return errors
    if len(steps) > MAX_WORKFLOW_STEPS:
        errors.append(
            _issue(
                "workflow_step_limit_exceeded",
                f"Workflow 단계는 최대 {MAX_WORKFLOW_STEPS}개까지 허용됩니다.",
                step_count=len(steps),
                max_steps=MAX_WORKFLOW_STEPS,
            )
        )

    seen_ids: set[str] = set()
    for index, step_value in enumerate(steps):
        step = step_value if isinstance(step_value, dict) else {}
        step_id = str(step.get("step_id") or "").strip()
        tool_name = str(step.get("tool_name") or "").strip()
        question = str(step.get("question") or "").strip()
        depends_on = _string_list(step.get("depends_on"))
        handoff = str(step.get("handoff") or "").strip().lower()
        on_error = str(step.get("on_error") or "").strip().lower()
        location = {"index": index, "step_id": step_id}

        if not STEP_ID_PATTERN.fullmatch(step_id):
            errors.append(_issue("invalid_step_id", "step_id는 영문자로 시작하는 영문·숫자·밑줄·하이픈이어야 합니다.", **location))
        elif step_id in seen_ids:
            errors.append(_issue("duplicate_step_id", f"step_id가 중복되었습니다: {step_id}", **location))
        if not TOOL_NAME_PATTERN.fullmatch(tool_name):
            errors.append(_issue("invalid_tool_name", "tool_name 형식이 올바르지 않습니다.", tool_name=tool_name, **location))
        elif allowed_tools and tool_name not in allowed_tools:
            errors.append(_issue("unregistered_tool_name", f"허용 Tool 목록에 없는 tool_name입니다: {tool_name}", tool_name=tool_name, **location))
        if not question:
            errors.append(_issue("step_question_missing", "각 단계에는 실행할 question이 필요합니다.", **location))
        elif len(question) > MAX_QUESTION_CHARS:
            errors.append(_issue("step_question_too_long", f"단계 question은 {MAX_QUESTION_CHARS}자를 초과할 수 없습니다.", **location))
        if handoff not in ALLOWED_HANDOFFS:
            errors.append(_issue("invalid_handoff", f"handoff는 {', '.join(sorted(ALLOWED_HANDOFFS))} 중 하나여야 합니다.", **location))
        if on_error not in ALLOWED_ON_ERROR:
            errors.append(_issue("invalid_on_error", f"on_error는 {', '.join(sorted(ALLOWED_ON_ERROR))} 중 하나여야 합니다.", **location))
        if len(depends_on) != len(set(depends_on)):
            errors.append(_issue("duplicate_dependency", "depends_on에 같은 step_id가 중복되었습니다.", **location))
        for dependency in depends_on:
            if dependency == step_id:
                errors.append(_issue("self_dependency", "단계는 자기 자신에 의존할 수 없습니다.", dependency=dependency, **location))
            elif dependency not in seen_ids:
                errors.append(
                    _issue(
                        "future_or_unknown_dependency",
                        f"depends_on은 앞에서 정의된 step_id만 참조할 수 있습니다: {dependency}",
                        dependency=dependency,
                        **location,
                    )
                )
        if index == 0 and depends_on:
            errors.append(_issue("first_step_dependency_not_allowed", "첫 단계에는 depends_on을 지정할 수 없습니다.", **location))
        if index == 0 and handoff != "none":
            errors.append(_issue("first_step_handoff_not_allowed", "첫 단계의 handoff는 none이어야 합니다.", **location))
        if handoff == "result_ref" and len(depends_on) != 1:
            errors.append(_issue("ambiguous_result_ref_handoff", "result_ref handoff에는 depends_on이 정확히 1개여야 합니다.", **location))
        seen_ids.add(step_id)
    return errors


# 함수 설명: `_parse_inline_workflow()`는 JSON, fenced JSON, 지정된 간단 Markdown 형식을 순서대로 해석합니다.
def _parse_inline_workflow(value: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not value.strip():
        return {}, [_issue("workflow_input_missing", "inline Workflow 정의 또는 workflow_key가 필요합니다.")]
    json_values = _json_candidates(value)
    if len(json_values) == 1 and isinstance(json_values[0], dict):
        return json_values[0], []
    if len(json_values) > 1:
        return {}, [_issue("ambiguous_inline_json", "inline 입력에서 둘 이상의 Workflow JSON을 발견했습니다.")]
    markdown_plan = _parse_markdown_plan(value)
    if markdown_plan.get("steps"):
        return markdown_plan, []
    return {}, [_issue("workflow_parse_error", "Workflow를 JSON 또는 지원 Markdown 형식으로 해석하지 못했습니다.")]


# 함수 설명: `_json_candidates()`는 전체 문자열과 fenced code block에서 object JSON만 중복 없이 찾습니다.
def _json_candidates(value: str) -> list[dict[str, Any]]:
    texts = [value.strip()]
    texts.extend(match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", value, re.IGNORECASE))
    result: list[dict[str, Any]] = []
    markers: set[str] = set()
    for text in texts:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        marker = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if marker not in markers:
            markers.add(marker)
            result.append(parsed)
    return result


# 함수 설명: `_parse_markdown_plan()`은 문서에 명시한 heading/key-value 형식만 보수적으로 Workflow dict로 변환합니다.
def _parse_markdown_plan(value: str) -> dict[str, Any]:
    result: dict[str, Any] = {"steps": []}
    current_step: dict[str, Any] | None = None
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("<!--") or line in {"---", "## steps", "## Steps"}:
            continue
        heading = re.match(r"^#{2,6}\s+(?:step\s*[:：]\s*)?([A-Za-z][A-Za-z0-9_-]{0,63})\s*$", line, re.IGNORECASE)
        if heading:
            if current_step:
                result["steps"].append(current_step)
            current_step = {"step_id": heading.group(1)}
            continue
        cleaned = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line)
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*[:：]\s*(.*)$", cleaned)
        if not match:
            continue
        key, raw_value = match.group(1).lower(), match.group(2).strip()
        parsed_value: Any = _markdown_value(raw_value) if key == "depends_on" else raw_value.strip("`\"")
        if key == "step_id":
            if current_step and current_step.get("step_id"):
                result["steps"].append(current_step)
            current_step = {"step_id": str(parsed_value)}
        elif key in {"tool_name", "question", "depends_on", "handoff", "on_error"}:
            if current_step is None:
                current_step = {}
            current_step[key] = parsed_value
        elif key in {"workflow_key", "title", "description"}:
            result[key] = parsed_value
    if current_step:
        result["steps"].append(current_step)
    return result


# 함수 설명: `_markdown_value()`는 depends_on의 JSON 배열, 쉼표 목록, none 표현을 문자열 목록으로 바꿉니다.
def _markdown_value(value: str) -> list[str]:
    text = value.strip()
    if text.lower() in {"", "none", "null", "[]", "없음"}:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = [item.strip() for item in text.strip("[]").split(",")]
    return _string_list(parsed)


# 함수 설명: `_parse_registry()`는 standalone 화면 입력의 JSON만 읽고 다른 외부 저장소나 환경변수를 참조하지 않습니다.
def _parse_registry(value: Any) -> tuple[Any, list[dict[str, Any]]]:
    text = _text(value).strip()
    if not text:
        return {}, []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, [_issue("workflow_registry_parse_error", f"Workflow Registry JSON 형식이 올바르지 않습니다: {exc}")]
    if not isinstance(parsed, (dict, list)):
        return {}, [_issue("invalid_workflow_registry", "Workflow Registry는 object 또는 array여야 합니다.")]
    return parsed, []


# 함수 설명: `_lookup_registry_workflow()`는 지원 registry 형태에서 정확히 한 개의 workflow_key만 선택합니다.
def _lookup_registry_workflow(registry: Any, workflow_key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    source = registry.get("workflows") if isinstance(registry, dict) and "workflows" in registry else registry
    if isinstance(source, dict):
        direct = source.get(workflow_key)
        if isinstance(direct, dict):
            candidates.append(deepcopy(direct))
        for key, item in source.items():
            if not isinstance(item, dict) or key == workflow_key:
                continue
            item_key = str(item.get("workflow_key") or item.get("key") or "").strip()
            if item_key == workflow_key:
                candidates.append(deepcopy(item))
    elif isinstance(source, list):
        candidates.extend(
            deepcopy(item)
            for item in source
            if isinstance(item, dict)
            and str(item.get("workflow_key") or item.get("key") or "").strip() == workflow_key
        )
    if not candidates:
        return {}, [_issue("workflow_key_not_found", f"Workflow Registry에서 key를 찾지 못했습니다: {workflow_key}")]
    if len(candidates) > 1:
        return {}, [_issue("duplicate_workflow_key", f"Workflow Registry에 같은 key가 둘 이상 있습니다: {workflow_key}")]
    selected = candidates[0]
    selected.setdefault("workflow_key", workflow_key)
    return selected, []


# 함수 설명: `_lookup_registry_identifier()`는 canonical key를 먼저 찾고 없을 때만 정규화된 alias 정확 일치를 허용합니다.
def _lookup_registry_identifier(registry: Any, identifier: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    exact, exact_errors = _lookup_registry_workflow(registry, identifier)
    if not exact_errors:
        return exact, []
    normalized = _normalized_identifier(identifier)
    if not normalized:
        return {}, exact_errors
    source = registry.get("workflows") if isinstance(registry, dict) and "workflows" in registry else registry
    values = list(source.values()) if isinstance(source, dict) else source if isinstance(source, list) else []
    candidates: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        aliases = _string_list(item.get("aliases"))
        if any(_normalized_identifier(alias) == normalized for alias in aliases):
            candidates.append(deepcopy(item))
    if not candidates:
        return {}, exact_errors
    if len(candidates) > 1:
        return {}, [_issue("duplicate_workflow_alias", f"Workflow Registry에 같은 alias가 둘 이상 있습니다: {identifier}")]
    selected = candidates[0]
    canonical = str(selected.get("workflow_key") or selected.get("key") or "").strip()
    if not canonical:
        return {}, [_issue("workflow_key_missing", "alias가 일치한 Workflow에 canonical key가 없습니다.")]
    selected.setdefault("workflow_key", canonical)
    return selected, []


# 함수 설명: `_normalized_identifier()`는 key/alias 정확 비교에서 공백과 영문 대소문자 차이를 제거합니다.
def _normalized_identifier(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


# 함수 설명: `_inline_workflow_key()`는 JSON 본문이 아닌 key-only 입력에서 registry 조회용 workflow_key를 추출합니다.
def _inline_workflow_key(value: str) -> str:
    stripped = value.strip()
    match = re.fullmatch(r"(?:workflow_key\s*[:：]\s*)?([A-Za-z][A-Za-z0-9_.-]{0,127})", stripped, re.IGNORECASE)
    return match.group(1) if match else ""


# 함수 설명: `_allowed_tools()`는 JSON 배열·쉼표·줄바꿈 형식의 허용 Tool 이름을 중복 없는 목록으로 정리합니다.
def _allowed_tools(value: Any) -> list[str]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        data = data.get("tools") or data.get("allowed_tools") or []
    if isinstance(data, str):
        text = data.strip()
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = re.split(r"[,\n]", text)
        data = parsed
    return _string_list(data)


# 함수 설명: `_string_list()`는 문자열·목록 입력을 비어 있지 않은 중복 없는 문자열 목록으로 정규화합니다.
def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_loop_rows()`는 검증된 계획의 각 단계를 Loop가 읽을 text key와 실행 context가 있는 한 행으로 변환합니다.
def _loop_rows(parse_result: dict[str, Any]) -> list[dict[str, Any]]:
    if str(parse_result.get("status")) != "ok":
        return []
    plan = parse_result.get("workflow_plan") if isinstance(parse_result.get("workflow_plan"), dict) else {}
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    rows: list[dict[str, Any]] = []
    for step in steps:
        row = {
            "contract_version": PLAN_CONTRACT_VERSION,
            "workflow_run_id": plan.get("workflow_run_id"),
            "workflow_key": plan.get("workflow_key"),
            "workflow_title": plan.get("title"),
            "original_question": plan.get("user_question"),
            "total_steps": len(steps),
            **deepcopy(step),
        }
        row["text"] = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        rows.append(row)
    return rows


# 함수 설명: `_text()`는 Langflow Message/Data 또는 일반 값을 parser가 읽을 문자열로 변환합니다.
def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if text is not None:
        return str(text)
    data = getattr(value, "data", value)
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False)
    return str(data or "")


# 함수 설명: `_issue()`는 파싱·검증 오류를 type/message와 선택 위치 정보가 있는 dict로 만듭니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": message, **extra}


# Langflow 컴포넌트 클래스: standalone 화면의 registry/inline 입력과 기본 Loop가 사용할 두 자료형 출력을 함께 제공합니다.
class WorkflowPlanParser(Component):
    display_name = "00 Workflow 계획 파서"
    description = "inline JSON/Markdown 또는 화면 JSON Registry를 최대 4단계 workflow.plan.v1로 검증합니다."
    name = "WorkflowPlanParser"
    icon = "ListChecks"

    inputs = [
        MultilineInput(
            name="workflow_input",
            display_name="Inline Workflow 정의",
            info="JSON, fenced JSON 또는 작성 가이드의 Markdown 형식입니다. workflow_key 사용 시 비워도 됩니다.",
            value="",
            required=False,
        ),
        MessageTextInput(
            name="workflow_key",
            display_name="Workflow 키",
            info="Registry에서 실행할 고정 key입니다. 지정하면 inline 정의보다 우선하며 미등록 key는 오류입니다.",
            value="",
            required=False,
        ),
        MessageTextInput(
            name="workflow_registry_json",
            display_name="Workflow 후보 Registry JSON",
            info="00A Registry 로더가 질문 기준으로 제한한 workflow.registry.v1 후보 JSON입니다.",
            value="{}",
            required=False,
        ),
        MessageTextInput(
            name="user_question",
            display_name="사용자 원문 질문",
            info="등록 Workflow의 {{user_question}} 또는 ${user_question} 자리표시자에만 주입합니다.",
            value="",
            required=False,
        ),
        MultilineInput(
            name="allowed_tool_names",
            display_name="허용 Tool 이름",
            info="연결된 Tool 이름을 JSON 배열·쉼표·줄바꿈으로 적습니다. 비우면 이름 형식만 검증합니다.",
            value="",
            required=False,
        ),
    ]
    outputs = [
        Output(name="workflow_plan", display_name="Workflow 계획", method="build_plan", types=["Data"], group_outputs=True),
        Output(name="loop_dataframe", display_name="Loop 반복 DataFrame", method="build_loop_dataframe", types=["DataFrame"], group_outputs=True),
        Output(name="loop_data_list", display_name="Loop Data 목록", method="build_loop_data_list", types=["Data"], group_outputs=True),
    ]

    # 함수 설명: `_result_once()`는 여러 group output이 같은 실행에서 서로 다른 run_id를 만들지 않도록 파싱 결과를 캐시합니다.
    def _result_once(self) -> dict[str, Any]:
        values = (
            getattr(self, "workflow_input", ""),
            getattr(self, "workflow_key", ""),
            getattr(self, "workflow_registry_json", ""),
            getattr(self, "user_question", ""),
            getattr(self, "allowed_tool_names", ""),
        )
        cache_key = tuple(_text(value) for value in values)
        if getattr(self, "_workflow_cache_key", None) != cache_key:
            self._workflow_cache_key = cache_key
            self._workflow_parse_result = parse_workflow_plan(
                values[0],
                values[1],
                values[2],
                values[3],
                values[4],
            )
        return self._workflow_parse_result

    # Langflow 출력 함수: 운영자 진단용 정규화 계획과 오류를 Data로 반환합니다.
    def build_plan(self) -> Data:
        result = deepcopy(self._result_once())
        self.status = result
        return Data(data=result)

    # Langflow 출력 함수: 기본 Loop Inputs 포트에 직접 연결할 한 단계당 한 행의 DataFrame을 반환합니다.
    def build_loop_dataframe(self) -> DataFrame:
        return DataFrame(_loop_rows(self._result_once()))

    # Langflow 출력 함수: Loop 구현 차이를 고려해 같은 단계들을 Data 목록으로도 반환합니다.
    def build_loop_data_list(self) -> list[Data]:
        return [Data(data=row) for row in _loop_rows(self._result_once())]
