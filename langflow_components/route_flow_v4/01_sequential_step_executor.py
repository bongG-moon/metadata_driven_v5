# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 순차 Workflow 단계 실행기
# 역할: 기본 Langflow Loop의 현재 step에 지정된 Tool을 정확히 한 번 실행하고 이전 단계의 compact result_ref를 연결합니다.
# 주요 입력: Loop 현재 항목, 연결 Tool 목록, 세션 ID, observation 최대 바이트
# 주요 출력: Loop의 Looping 포트로 되돌릴 compact 단계 결과
# 처리 흐름: step/context 검증 -> dependency 확인 -> 정확한 Tool 선택 -> 단일 invoke -> 결과 축약 -> 다음 단계 context 저장
# 유지보수 포인트: builtins TTL registry는 flow/user/session/run별로 격리하며 전체 rows·trace·artifact.raw를 절대 저장하지 않습니다.
# =============================================================================

from __future__ import annotations

import asyncio
import builtins
import inspect
import json
import time
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, MessageTextInput, Output
from lfx.schema.data import Data

PLAN_CONTRACT_VERSION = "workflow.plan.v1"
TOOL_RESULT_CONTRACT_VERSION = "route_v3.tool_result.v1"
STEP_RESULT_CONTRACT_VERSION = "workflow.step_result.v1"
EXECUTION_CONTRACT_VERSION = "workflow.execution.v1"
DEFAULT_OBSERVATION_BYTES = 8192
MIN_OBSERVATION_BYTES = 1024
MAX_OBSERVATION_BYTES = 32 * 1024
SUMMARY_LIMIT = 1600
RESULT_REF_LIMIT = 1024
REGISTRY_TTL_SECONDS = 60 * 60
REGISTRY_MAX_CONTEXTS = 256
REGISTRY_ATTRIBUTE = "__metadata_route_v4_execution_registry__"


# 주요 함수: 단일 step의 dependency와 Tool 결과 계약을 검증하고 업데이트된 실행 context와 compact 결과를 반환합니다.
async def execute_workflow_step(
    step_value: Any,
    tools_value: Any,
    execution_context: Any = None,
    session_id: Any = "",
    observation_byte_limit: Any = DEFAULT_OBSERVATION_BYTES,
) -> dict[str, Any]:
    step = _step_payload(step_value)
    context = _execution_context(execution_context, step, session_id)
    limit = _bounded_int(observation_byte_limit, DEFAULT_OBSERVATION_BYTES, MIN_OBSERVATION_BYTES, MAX_OBSERVATION_BYTES)
    validation_error = _step_execution_error(step, context)
    if validation_error:
        result = _fit_observation(_error_step_result(step, validation_error, "blocked"), limit)
        _record_result(context, result, stop=True)
        return {"step_result": result, "execution_context": context}

    dependency_error, upstream_ref = _dependency_handoff(step, context)
    if dependency_error:
        result = _fit_observation(_error_step_result(step, dependency_error, "blocked"), limit)
        _record_result(context, result, stop=str(step.get("on_error") or "stop") == "stop")
        return {"step_result": result, "execution_context": context}

    tool, tool_error = _select_exact_tool(tools_value, str(step.get("tool_name") or ""))
    if tool_error:
        result = _fit_observation(_error_step_result(step, tool_error, "error"), limit)
        _record_result(context, result, stop=str(step.get("on_error") or "stop") == "stop")
        return {"step_result": result, "execution_context": context}

    arguments = {"question": str(step.get("question") or "").strip()}
    if str(step.get("handoff")) == "result_ref":
        arguments["upstream_result_ref"] = upstream_ref
    try:
        raw_result = await _invoke_tool_once(tool, arguments)
        contract, contract_error = _tool_result_contract(raw_result)
        if contract_error:
            result = _error_step_result(step, contract_error, "error")
        else:
            result = _compact_success_result(step, contract)
    except Exception as exc:
        result = _error_step_result(
            step,
            _issue("tool_execution_error", f"Tool 실행 중 오류가 발생했습니다: {exc}"),
            "error",
        )

    stop = str(result.get("status")) in {"error", "blocked"} and str(step.get("on_error")) == "stop"
    result = _fit_observation(result, limit)
    _record_result(context, result, stop=stop)
    return {"step_result": result, "execution_context": context}


# 함수 설명: `_step_payload()`는 Loop Data의 text JSON 또는 일반 dict에서 현재 단계 필드를 복원합니다.
def _step_payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if hasattr(data, "to_dict") and not isinstance(data, dict):
        try:
            records = data.to_dict(orient="records")
            data = records[0] if len(records) == 1 else {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        text = getattr(value, "text", value)
        try:
            data = json.loads(str(text or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
    text_value = data.get("text") if isinstance(data, dict) else None
    if isinstance(text_value, str):
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_execution_context()`는 기존 context를 복사하거나 첫 단계 정보를 이용해 새 workflow.execution.v1 context를 만듭니다.
def _execution_context(value: Any, step: dict[str, Any], session_id: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict) and isinstance(data.get("execution_context"), dict):
        data = data["execution_context"]
    if isinstance(data, dict) and data.get("contract_version") == EXECUTION_CONTRACT_VERSION:
        return deepcopy(data)
    return {
        "contract_version": EXECUTION_CONTRACT_VERSION,
        "workflow_run_id": str(step.get("workflow_run_id") or "").strip(),
        "workflow_key": str(step.get("workflow_key") or "").strip(),
        "session_id": str(session_id or "").strip(),
        "original_question": str(step.get("original_question") or "").strip(),
        "total_steps": _bounded_int(step.get("total_steps"), 1, 1, 4),
        "status": "running",
        "stop_requested": False,
        "execution_order": [],
        "results_by_step": {},
    }


# 함수 설명: `_step_execution_error()`는 run ID·순서·중복·중단 상태를 확인해 잘못된 Loop 진행을 fail-closed 처리합니다.
def _step_execution_error(step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    if str(step.get("contract_version") or "") != PLAN_CONTRACT_VERSION:
        return _issue("invalid_step_contract", "Loop 항목의 contract_version이 workflow.plan.v1이 아닙니다.")
    step_id = str(step.get("step_id") or "").strip()
    run_id = str(step.get("workflow_run_id") or "").strip()
    if not step_id or not run_id:
        return _issue("step_identity_missing", "Loop 단계의 step_id 또는 workflow_run_id가 비어 있습니다.")
    if str(context.get("workflow_run_id") or "") != run_id:
        return _issue("workflow_run_mismatch", "다른 workflow_run_id의 실행 context가 전달되었습니다.")
    if step_id in _dict(context.get("results_by_step")):
        return _issue("duplicate_step_execution", f"같은 step_id를 다시 실행할 수 없습니다: {step_id}")
    if context.get("stop_requested") is True:
        return _issue("workflow_already_stopped", "앞 단계의 stop 정책으로 Workflow가 이미 중단되었습니다.")
    expected_index = len(_list(context.get("execution_order"))) + 1
    if _bounded_int(step.get("step_index"), 0, 0, 4) != expected_index:
        return _issue("unexpected_step_order", f"예상 step_index={expected_index}, 실제={step.get('step_index')}")
    return None


# 함수 설명: `_dependency_handoff()`는 모든 dependency 성공 여부와 단일 result_ref 전달 가능 여부를 검사합니다.
def _dependency_handoff(step: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    dependencies = [str(item) for item in _list(step.get("depends_on")) if str(item or "").strip()]
    results = _dict(context.get("results_by_step"))
    for dependency in dependencies:
        result = results.get(dependency) if isinstance(results.get(dependency), dict) else None
        if result is None:
            return _issue("dependency_result_missing", f"dependency 실행 결과가 없습니다: {dependency}"), ""
        if str(result.get("status") or "") not in {"ok", "partial"}:
            return _issue("dependency_failed", f"dependency가 성공하지 않아 현재 단계를 실행하지 않습니다: {dependency}"), ""
    if str(step.get("handoff") or "none") != "result_ref":
        return None, ""
    if len(dependencies) != 1:
        return _issue("ambiguous_result_ref_handoff", "result_ref handoff에는 dependency가 정확히 하나 필요합니다."), ""
    source = _dict(results.get(dependencies[0]))
    result_ref = str(source.get("result_ref") or "").strip()
    if not result_ref or source.get("handoff_usable") is not True:
        return _issue("dependency_result_ref_unavailable", "dependency 결과에 사용할 수 있는 result_ref가 없습니다."), ""
    return None, result_ref


# 함수 설명: `_select_exact_tool()`은 대소문자 보정 없이 tool.name이 정확히 같은 Tool 하나만 허용합니다.
def _select_exact_tool(tools_value: Any, tool_name: str) -> tuple[Any | None, dict[str, Any] | None]:
    tools = _tools(tools_value)
    matches = [tool for tool in tools if str(getattr(tool, "name", "") or "").strip() == tool_name]
    if not matches:
        return None, _issue("tool_not_found", f"연결 Tool 목록에서 정확한 이름을 찾지 못했습니다: {tool_name}")
    if len(matches) > 1:
        return None, _issue("duplicate_tool_name", f"같은 이름의 Tool이 둘 이상 연결되었습니다: {tool_name}")
    return matches[0], None


# 함수 설명: `_tools()`는 단일 Tool, list, toolkit 형태를 평탄화하되 이름 없는 값은 제외합니다.
def _tools(value: Any) -> list[Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        data = data.get("tools") or data.get("toolkit") or []
    values = list(data) if isinstance(data, (list, tuple, set)) else ([data] if data is not None else [])
    result: list[Any] = []
    for item in values:
        nested = getattr(item, "tools", None)
        if isinstance(nested, list):
            result.extend(tool for tool in nested if getattr(tool, "name", None))
        elif getattr(item, "name", None):
            result.append(item)
    return result


# 함수 설명: `_invoke_tool_once()`는 사용 가능한 첫 실행 인터페이스를 선택하고 실패 시 다른 방식으로 재호출하지 않습니다.
async def _invoke_tool_once(tool: Any, arguments: dict[str, Any]) -> Any:
    ainvoke = getattr(tool, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(deepcopy(arguments))
    arun = getattr(tool, "arun", None)
    if callable(arun):
        return await arun(**deepcopy(arguments))
    invoke = getattr(tool, "invoke", None)
    if callable(invoke):
        return await asyncio.to_thread(invoke, deepcopy(arguments))
    if callable(tool):
        result = tool(**deepcopy(arguments))
        return await result if inspect.isawaitable(result) else result
    raise TypeError("선택된 Tool에 ainvoke/arun/invoke/callable 실행 인터페이스가 없습니다.")


# 함수 설명: `_tool_result_contract()`은 Tool 반환값에서 route_v3.tool_result.v1 계약만 꺼내고 자연어 추측 fallback을 금지합니다.
def _tool_result_contract(value: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    data = getattr(value, "data", value)
    if isinstance(data, dict) and isinstance(data.get("tool_result"), dict):
        data = data["tool_result"]
    if not isinstance(data, dict):
        text = getattr(value, "text", value)
        try:
            data = json.loads(str(text or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict) or str(data.get("contract_version") or "") != TOOL_RESULT_CONTRACT_VERSION:
        return {}, _issue("invalid_tool_result_contract", "Tool 결과가 route_v3.tool_result.v1 계약이 아닙니다.")
    status = str(data.get("status") or "").strip().lower()
    if status not in {"ok", "partial", "error"}:
        return {}, _issue("invalid_tool_result_status", f"Tool 결과 status가 올바르지 않습니다: {status}")
    return data, None


# 함수 설명: `_compact_success_result()`는 Tool 결과에서 최종 합성과 handoff에 필요한 필드만 step 결과로 복사합니다.
def _compact_success_result(step: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": STEP_RESULT_CONTRACT_VERSION,
        "workflow_run_id": str(step.get("workflow_run_id") or ""),
        "workflow_key": str(step.get("workflow_key") or ""),
        "step_index": _bounded_int(step.get("step_index"), 0, 0, 4),
        "total_steps": _bounded_int(step.get("total_steps"), 0, 0, 4),
        "step_id": str(step.get("step_id") or ""),
        "tool_name": str(step.get("tool_name") or ""),
        "question": _clip(step.get("question"), 1000),
        "depends_on": deepcopy(_list(step.get("depends_on"))),
        "handoff": str(step.get("handoff") or "none"),
        "on_error": str(step.get("on_error") or "stop"),
        "status": str(contract.get("status") or "error"),
        "summary": _clip(contract.get("summary"), SUMMARY_LIMIT),
        "result_ref": _clip(contract.get("result_ref"), RESULT_REF_LIMIT),
        "result_ref_meta": _compact_ref_meta(contract.get("result_ref_meta")),
        "entity_ids": _compact_entity_ids(contract.get("entity_ids")),
        "handoff_usable": contract.get("handoff_usable") is True,
        "warnings": _compact_issues(contract.get("warnings")),
        "errors": _compact_issues(contract.get("errors")),
    }


# 함수 설명: `_error_step_result()`은 parser/dependency/Tool 오류를 같은 workflow.step_result.v1 구조로 만듭니다.
def _error_step_result(step: dict[str, Any], error: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "contract_version": STEP_RESULT_CONTRACT_VERSION,
        "workflow_run_id": str(step.get("workflow_run_id") or ""),
        "workflow_key": str(step.get("workflow_key") or ""),
        "step_index": _bounded_int(step.get("step_index"), 0, 0, 4),
        "total_steps": _bounded_int(step.get("total_steps"), 0, 0, 4),
        "step_id": str(step.get("step_id") or ""),
        "tool_name": str(step.get("tool_name") or ""),
        "question": _clip(step.get("question"), 1000),
        "depends_on": deepcopy(_list(step.get("depends_on"))),
        "handoff": str(step.get("handoff") or "none"),
        "on_error": str(step.get("on_error") or "stop"),
        "status": status,
        "summary": str(error.get("message") or "Workflow 단계 실행 오류"),
        "result_ref": "",
        "result_ref_meta": {},
        "entity_ids": [],
        "handoff_usable": False,
        "warnings": [],
        "errors": [deepcopy(error)],
    }


# 함수 설명: `_record_result()`는 compact 결과만 context에 기록하고 마지막 단계의 전체 실행 상태를 계산합니다.
def _record_result(context: dict[str, Any], result: dict[str, Any], *, stop: bool) -> None:
    step_id = str(result.get("step_id") or "")
    context.setdefault("results_by_step", {})[step_id] = deepcopy(result)
    context.setdefault("execution_order", []).append(step_id)
    if stop:
        context["stop_requested"] = True
        context["status"] = "stopped"
    total_steps = _bounded_int(context.get("total_steps") or result.get("total_steps"), 0, 0, 4)
    if len(context.get("execution_order", [])) >= total_steps:
        statuses = [str(item.get("status") or "") for item in context.get("results_by_step", {}).values() if isinstance(item, dict)]
        if statuses and all(status == "ok" for status in statuses):
            context["status"] = "complete"
        elif any(status in {"ok", "partial"} for status in statuses):
            context["status"] = "partial"
        else:
            context["status"] = "error"


# 함수 설명: `_fit_observation()`은 summary/entity/issues를 단계적으로 줄여 직렬화 결과가 설정 바이트를 넘지 않게 합니다.
def _fit_observation(value: dict[str, Any], byte_limit: int) -> dict[str, Any]:
    result = deepcopy(value)
    if _json_bytes(result) + 32 <= byte_limit:
        return _stamp_observation_bytes(result, byte_limit)
    result["summary"] = _clip(result.get("summary"), 800)
    result["warnings"] = _compact_issues(result.get("warnings"), count=2, text_limit=200)
    result["errors"] = _compact_issues(result.get("errors"), count=2, text_limit=200)
    result["entity_ids"] = _compact_entity_ids(result.get("entity_ids"), entity_limit=3, value_limit=5)
    if _json_bytes(result) > byte_limit:
        result["summary"] = _clip(result.get("summary"), 300)
        result["result_ref_meta"] = _compact_ref_meta(result.get("result_ref_meta"), column_limit=5)
        result["entity_ids"] = []
        result["warnings"] = _compact_issues(result.get("warnings"), count=1, text_limit=120)
        result["errors"] = _compact_issues(result.get("errors"), count=1, text_limit=120)
    if _json_bytes(result) > byte_limit:
        result = {
            key: result.get(key)
            for key in (
                "contract_version",
                "workflow_run_id",
                "step_index",
                "total_steps",
                "step_id",
                "tool_name",
                "status",
                "summary",
                "result_ref",
                "handoff_usable",
                "errors",
            )
        }
        result["summary"] = _clip(result.get("summary"), 160)
        result["result_ref"] = _clip(result.get("result_ref"), 256)
    return _stamp_observation_bytes(result, byte_limit)


# 함수 설명: `_stamp_observation_bytes()`는 크기 필드 자체를 포함한 최종 JSON이 상한 안에 있도록 마지막 여유를 확보합니다.
def _stamp_observation_bytes(value: dict[str, Any], byte_limit: int) -> dict[str, Any]:
    result = deepcopy(value)
    result["observation_bytes"] = 0
    result["observation_bytes"] = _json_bytes(result)
    if _json_bytes(result) <= byte_limit:
        return result
    result["summary"] = _clip(result.get("summary"), 100)
    result["errors"] = _compact_issues(result.get("errors"), count=1, text_limit=80)
    result["warnings"] = []
    result["observation_bytes"] = _json_bytes(result)
    if _json_bytes(result) <= byte_limit:
        return result
    # 최소 상한 1KB에서도 identity/status/ref는 충분히 들어가며, 마지막 수단으로 크기 표기 필드를 제거합니다.
    result.pop("observation_bytes", None)
    return result


# 함수 설명: `_compact_ref_meta()`는 다음 단계와 최종 설명에 필요한 결과 건수·컬럼 정보만 보존합니다.
def _compact_ref_meta(value: Any, column_limit: int = 20) -> dict[str, Any]:
    meta = value if isinstance(value, dict) else {}
    columns = [str(item) for item in _list(meta.get("columns")) if str(item or "").strip()][:column_limit]
    return {
        key: deepcopy(item)
        for key, item in {
            "role": meta.get("role"),
            "row_count": meta.get("row_count"),
            "columns": columns,
            "source_alias": meta.get("source_alias"),
            "dataset_key": meta.get("dataset_key"),
        }.items()
        if item not in (None, "", [], {})
    }


# 함수 설명: `_compact_entity_ids()`는 식별자 종류와 작은 preview만 남기고 전체 ID 목록을 result_ref 뒤에 숨깁니다.
def _compact_entity_ids(value: Any, entity_limit: int = 8, value_limit: int = 20) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(value)[:entity_limit]:
        if not isinstance(item, dict):
            continue
        values = deepcopy(_list(item.get("values"))[:value_limit])
        result.append(
            {
                "entity_type": _clip(item.get("entity_type"), 80),
                "column": _clip(item.get("column"), 100),
                "values": values,
                "observed_count": len(values),
                "total_count": item.get("total_count"),
                "complete": item.get("complete") is True and len(_list(item.get("values"))) <= value_limit,
            }
        )
    return result


# 함수 설명: `_compact_issues()`는 warning/error를 짧은 type/message object로 제한합니다.
def _compact_issues(value: Any, count: int = 5, text_limit: int = 320) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(value)[:count]:
        if isinstance(item, dict):
            result.append({"type": _clip(item.get("type"), 100), "message": _clip(item.get("message"), text_limit)})
        else:
            result.append({"type": "message", "message": _clip(item, text_limit)})
    return result


# 함수 설명: `_registry()`는 프로세스 builtins에 TTL registry 하나를 만들고 만료·최대 개수 정리를 수행합니다.
def _registry() -> dict[str, dict[str, Any]]:
    registry = getattr(builtins, REGISTRY_ATTRIBUTE, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(builtins, REGISTRY_ATTRIBUTE, registry)
    now = time.time()
    for key in list(registry):
        entry = registry.get(key) if isinstance(registry.get(key), dict) else {}
        if now - float(entry.get("updated_at") or 0) > REGISTRY_TTL_SECONDS:
            registry.pop(key, None)
    if len(registry) > REGISTRY_MAX_CONTEXTS:
        ordered = sorted(registry, key=lambda key: float(_dict(registry.get(key)).get("updated_at") or 0))
        for key in ordered[: len(registry) - REGISTRY_MAX_CONTEXTS]:
            registry.pop(key, None)
    return registry


# 함수 설명: `_registry_key()`는 사용자·Flow·세션·run ID를 결합해 동시 실행 context가 섞이지 않는 key를 만듭니다.
def _registry_key(component: Any, step: dict[str, Any], explicit_session_id: Any) -> str:
    graph = getattr(component, "graph", None)
    user_id = str(getattr(component, "user_id", "") or "anonymous")
    flow_id = str(getattr(graph, "flow_id", "") or "flow")
    session_id = str(explicit_session_id or getattr(graph, "session_id", "") or "session")
    run_id = str(step.get("workflow_run_id") or "run")
    return "|".join((user_id, flow_id, session_id, run_id))


# 함수 설명: `_load_registry_context()`는 첫 단계에서 이전 동명 run을 초기화하고 이후 단계에서는 저장된 compact context만 반환합니다.
def _load_registry_context(component: Any, key: str, step: dict[str, Any], session_id: Any) -> dict[str, Any]:
    registry = _registry()
    if _bounded_int(step.get("step_index"), 0, 0, 4) == 1:
        registry.pop(key, None)
    entry = registry.get(key) if isinstance(registry.get(key), dict) else {}
    context = entry.get("context") if isinstance(entry.get("context"), dict) else None
    return _execution_context(context, step, session_id)


# 함수 설명: `_save_or_cleanup_registry_context()`는 중간 단계만 TTL 저장하고 마지막 Loop 항목 처리 후 즉시 정리합니다.
def _save_or_cleanup_registry_context(key: str, context: dict[str, Any], step: dict[str, Any]) -> None:
    registry = _registry()
    if _bounded_int(step.get("step_index"), 0, 0, 4) >= _bounded_int(step.get("total_steps"), 0, 0, 4):
        registry.pop(key, None)
        return
    registry[key] = {"updated_at": time.time(), "context": deepcopy(context)}


# 함수 설명: `_json_bytes()`는 observation 크기 제한에 사용할 UTF-8 JSON 바이트 수를 계산합니다.
def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


# 함수 설명: `_bounded_int()`는 화면 숫자 입력을 지정한 최소·최대 범위의 정수로 정규화합니다.
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(str(value).strip()) if str(value or "").strip() else default
    except Exception:
        parsed = default
    return max(lower, min(parsed, upper))


# 함수 설명: `_clip()`은 문자열을 지정 길이로 자르고 초과 여부를 말줄임으로 표시합니다.
def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: `_dict()`는 dict가 아닌 값을 빈 dict로 바꿔 안전한 key 접근을 보장합니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 list가 아닌 값을 빈 목록으로 바꿔 반복 경계를 명확히 합니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_issue()`는 executor 오류를 type/message가 있는 표준 dict로 구성합니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": message, **extra}


# Langflow 컴포넌트 클래스: 기본 Loop의 Item과 Looping 사이에서 정확히 한 단계만 실행합니다.
class SequentialWorkflowStepExecutor(Component):
    display_name = "01 순차 Workflow 단계 실행기"
    description = "Loop 현재 step의 정확한 Tool 하나를 실행하고 compact 결과/ref만 다음 단계에 보존합니다."
    name = "SequentialWorkflowStepExecutor"
    icon = "Workflow"

    inputs = [
        HandleInput(
            name="loop_item",
            display_name="Loop 현재 항목",
            info="00 파서의 Loop DataFrame/Data 목록을 기본 Loop가 한 행씩 전달한 Data입니다.",
            input_types=["Data"],
            required=True,
        ),
        HandleInput(
            name="tools",
            display_name="Workflow Tool 목록",
            info="Route V3 compact result 계약을 반환하는 Tool들을 연결합니다.",
            input_types=["Tool"],
            is_list=True,
            required=True,
        ),
        MessageTextInput(
            name="session_id",
            display_name="세션 ID",
            info="비우면 현재 graph session_id를 사용합니다.",
            value="",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="observation_byte_limit",
            display_name="단계 결과 최대 바이트",
            value=str(DEFAULT_OBSERVATION_BYTES),
            required=False,
            advanced=True,
        ),
    ]
    outputs = [Output(name="step_result", display_name="단계 결과", method="build_step_result", types=["Data"])]

    # Langflow 출력 함수: registry에서 현재 run context를 복원하고 한 번 실행한 compact 결과를 Looping 포트로 반환합니다.
    async def build_step_result(self) -> Data:
        step = _step_payload(getattr(self, "loop_item", None))
        explicit_session = str(getattr(self, "session_id", "") or "").strip()
        graph_session = str(getattr(getattr(self, "graph", None), "session_id", "") or "").strip()
        resolved_session = explicit_session or graph_session
        key = _registry_key(self, step, resolved_session)
        context = _load_registry_context(self, key, step, resolved_session)
        execution = await execute_workflow_step(
            step,
            getattr(self, "tools", None),
            context,
            resolved_session,
            getattr(self, "observation_byte_limit", DEFAULT_OBSERVATION_BYTES),
        )
        result = execution["step_result"]
        updated_context = execution["execution_context"]
        _save_or_cleanup_registry_context(key, updated_context, step)
        self.status = result
        return Data(data=result)
