# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 구조화 continuation 지원 Cached Run Flow 도구
# 역할: Agent에는 question 하나만 공개하고 Data Analysis Flow의 구조화 API 응답을
#       확인해 단일 실행 또는 최대 2단계 종속 실행을 수행합니다.
# 주요 입력: 대상 Flow, 세션 ID, 도구 이름/설명, continuation 실행 상한/timeout
# 주요 출력: return_direct Agent가 그대로 표시할 최종 Message
# 안전 원칙: 답변 문자열을 파싱하지 않으며 rows/trace/code를 Agent observation에
#           전달하지 않습니다. continuation 계약이 불완전하면 2차 호출하지 않습니다.
# =============================================================================

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from lfx.base.tools.run_flow import RunFlowBaseComponent
from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, IntInput, MessageTextInput, MultilineInput, Output, StrInput
from lfx.schema.data import Data
from lfx.schema.message import Message


FLOW_ID_PREFERRED = "Flow ID 우선"
FLOW_ID_ONLY = "선택한 Flow ID만"
FLOW_NAME_ONLY = "Flow 이름으로 조회"
FLOW_RESOLUTION_OPTIONS = [FLOW_ID_PREFERRED, FLOW_ID_ONLY, FLOW_NAME_ONLY]
STRUCTURED_OUTPUT_TYPES = {"data", "dataframe", "table"}
CONTINUATION_INPUT_NAMES = (
    "upstream_result_ref",
    "continuation_ref",
    "continuation_contract",
    "skip_intermediate_answer",
)
MAX_CONTRACT_BYTES = 4_096
MAX_REF_CHARS = 1_024
MAX_STAGE_ID_CHARS = 128


# 함수 설명: `_as_iso_text()`는 datetime 등 시간 값을 캐시 갱신 비교에 사용할 ISO 문자열로 변환합니다.
def _as_iso_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# 함수 설명: `_tool_values()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 값 관련 값을 계산·변환하는 내부 helper입니다.
def _tool_values(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return value if isinstance(value, dict) else {}


# 함수 설명: `_tool_question()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 question 관련 값을 계산·변환하는 내부 helper입니다.
def _tool_question(value: Any) -> str:
    return str(_tool_values(value).get("question") or "").strip()


# 함수 설명: `_gate_items()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 항목 관련 값을 계산·변환하는 내부 helper입니다.
def _gate_items(value: Any) -> list[str]:
    raw = list(value) if isinstance(value, (list, tuple, set)) else re.split(r"[\n,]+", str(value or ""))
    return [str(item).strip() for item in raw if str(item).strip()]


# 함수 설명: `_normalize_gate_text()`는 GATE·문자열의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다.
def _normalize_gate_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


# 함수 설명: `_keyword_gate_error()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 GATE·오류 관련 값을 계산·변환하는 내부 helper입니다.
def _keyword_gate_error(
    question: Any,
    required_all_keywords: Any = "",
    required_any_phrases: Any = "",
    keyword_gate_message: Any = "",
) -> str:
    required_all = _gate_items(required_all_keywords)
    required_any = _gate_items(required_any_phrases)
    if not required_all and not required_any:
        return ""
    normalized = _normalize_gate_text(question)
    missing_all = [item for item in required_all if _normalize_gate_text(item) not in normalized]
    has_any = not required_any or any(_normalize_gate_text(item) in normalized for item in required_any)
    if not missing_all and has_any:
        return ""
    return str(keyword_gate_message or "").strip() or "현재 질문은 이 Flow의 실행 조건을 충족하지 않습니다."


# 함수 설명: `_is_io_vertex()`는 입력값이 IO·vertex 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _is_io_vertex(vertex: Any, role: str) -> bool:
    data = getattr(vertex, "data", {}) or {}
    node_type = str(data.get("type") or "") if isinstance(data, dict) else ""
    display_name = str(getattr(vertex, "display_name", "") or "")
    vertex_id = str(getattr(vertex, "id", "") or "")
    if role == "input":
        return node_type in {"ChatInput", "GaiAInput"} or display_name in {"Chat Input", "GaiA Input"} or vertex_id.startswith("ChatInput-")
    if role == "output":
        return node_type in {"ChatOutput", "GaiAOutput"} or display_name in {"Chat Output", "GaiA Output"} or vertex_id.startswith("ChatOutput-")
    return False


# 함수 설명: `_single_io_id()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 IO·ID 관련 값을 계산·변환하는 내부 helper입니다.
def _single_io_id(vertices: Any, role: str) -> str:
    candidates = [str(vertex.id) for vertex in list(vertices or []) if _is_io_vertex(vertex, role)]
    if len(candidates) != 1:
        raise ValueError(f"대상 Flow에는 {role} I/O가 정확히 하나 있어야 합니다. 현재 발견 개수={len(candidates)}")
    return candidates[0]


# 함수 설명: `_vertex_has_input()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 HAS·input 관련 값을 계산·변환하는 내부 helper입니다.
def _vertex_has_input(vertex: Any, input_name: str) -> bool:
    data = getattr(vertex, "data", {}) or {}
    node = data.get("node") if isinstance(data, dict) else {}
    template = node.get("template") if isinstance(node, dict) else {}
    field_order = node.get("field_order") if isinstance(node, dict) else []
    if isinstance(template, dict) and input_name in template:
        return True
    if isinstance(field_order, list) and input_name in field_order:
        return True
    raw_params = getattr(vertex, "raw_params", {}) or {}
    return isinstance(raw_params, dict) and input_name in raw_params


# 함수 설명: `_single_vertex()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 vertex 관련 값을 계산·변환하는 내부 helper입니다.
def _single_vertex(vertices: Any, vertex_id: Any) -> Any:
    selected = [vertex for vertex in list(vertices or []) if str(getattr(vertex, "id", "") or "") == str(vertex_id or "")]
    if len(selected) != 1:
        raise ValueError("현재 하위 Flow에서 단일 vertex를 확인할 수 없습니다.")
    return selected[0]


# 함수 설명: `_single_named_input_vertex_id()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 named·input·vertex·ID 관련 값을
#        계산·변환하는 내부 helper입니다.
def _single_named_input_vertex_id(vertices: Any, input_name: str) -> str:
    candidates = [str(vertex.id) for vertex in list(vertices or []) if _vertex_has_input(vertex, input_name)]
    if len(candidates) > 1:
        raise ValueError(f"대상 Flow에서 '{input_name}' 입력 component가 여러 개 발견되었습니다.")
    return candidates[0] if candidates else ""


# 함수 설명: `_output_descriptor()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 descriptor 관련 값을 계산·변환하는 내부 helper입니다.
def _output_descriptor(output: Any) -> tuple[str, set[str], bool]:
    if isinstance(output, dict):
        name = str(output.get("name") or "").strip()
        raw_types = output.get("types") or []
        selected = output.get("selected")
        hidden = bool(output.get("hidden", False))
    else:
        name = str(getattr(output, "name", "") or "").strip()
        raw_types = getattr(output, "types", None) or []
        selected = getattr(output, "selected", None)
        hidden = bool(getattr(output, "hidden", False))
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    types = {str(item or "").strip().casefold() for item in raw_types if str(item or "").strip()}
    if selected not in (None, ""):
        types.add(str(selected).strip().casefold())
    return name, types, hidden


# 함수 설명: `_terminal_output_candidates()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 output·후보 관련 값을 계산·변환하는 내부
#        helper입니다.
def _terminal_output_candidates(graph: Any) -> list[dict[str, Any]]:
    successor_map = getattr(graph, "successor_map", {}) or {}
    candidates: list[dict[str, Any]] = []
    for vertex in list(getattr(graph, "vertices", []) or []):
        vertex_id = str(getattr(vertex, "id", "") or "").strip()
        if not vertex_id:
            continue
        successors = successor_map.get(getattr(vertex, "id", None), successor_map.get(vertex_id, []))
        if successors:
            continue
        for output in list(getattr(vertex, "outputs", []) or []):
            name, types, hidden = _output_descriptor(output)
            if name and not hidden:
                candidates.append({"vertex": vertex, "vertex_id": vertex_id, "output_name": name, "output_types": types})
    return candidates


# 함수 설명: `_preferred_graph_output_target()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 graph·output·target 관련 값을
#        계산·변환하는 내부 helper입니다.
def _preferred_graph_output_target(graph: Any, preferred_output_names: Any = "api_response") -> tuple[tuple[str, str], set[str]]:
    candidates = _terminal_output_candidates(graph)
    configured = [item.strip() for item in re.split(r"[,;\n\r]+", str(preferred_output_names or "")) if item.strip()]
    for name in configured:
        matches = [item for item in candidates if item["output_name"].casefold() == name.casefold()]
        if len(matches) == 1:
            selected = matches[0]
            return (selected["vertex_id"], selected["output_name"]), set(selected["output_types"])
        if len(matches) > 1:
            raise ValueError(f"최종 출력 이름 '{name}'이 terminal 여러 곳에 있습니다.")
    if configured:
        available = ", ".join(f"{item['vertex_id']}.{item['output_name']}" for item in candidates)
        raise ValueError(f"설정한 구조화 최종 출력을 찾지 못했습니다. 설정={configured}, 사용 가능={available}")
    structured = [item for item in candidates if item["output_types"].intersection(STRUCTURED_OUTPUT_TYPES)]
    if len(structured) != 1:
        raise ValueError("대상 Flow의 구조화 terminal 출력을 하나로 확정할 수 없습니다.")
    selected = structured[0]
    return (selected["vertex_id"], selected["output_name"]), set(selected["output_types"])


# 함수 설명: `_promote_graph_output()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 graph·output 관련 값을 계산·변환하는 내부
#        helper입니다.
def _promote_graph_output(graph: Any, target: tuple[str, str]) -> None:
    vertex_id, output_name = target
    matches = [item for item in _terminal_output_candidates(graph) if item["vertex_id"] == vertex_id and item["output_name"] == output_name]
    if len(matches) != 1:
        raise ValueError("선택한 구조화 최종 출력이 현재 child graph와 일치하지 않습니다.")
    selected = matches[0]["vertex"]
    for vertex in list(getattr(graph, "vertices", []) or []):
        vertex.is_output = vertex is selected
    if not bool(getattr(selected, "is_output", False)):
        raise ValueError("구조화 child 출력 활성화가 반영되지 않았습니다.")


# 함수 설명: `_question_tool_field()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 TOOL·field 관련 값을 계산·변환하는 내부
#        helper입니다.
def _question_tool_field() -> dict[str, Any]:
    return {
        "name": "question",
        "display_name": "사용자 질문",
        "info": "현재 사용자 질문 원문입니다.",
        "required": True,
        "value": "",
        "tool_mode": True,
        "type": str,
        "input_types": [],
        "is_list": False,
    }


# 함수 설명: `_flow_resolution_mode()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 resolution·실행 모드 관련 값을 계산·변환하는 내부
#        helper입니다.
def _flow_resolution_mode(value: Any) -> str:
    text = str(value or "").strip()
    aliases = {"id_preferred": FLOW_ID_PREFERRED, "prefer_id": FLOW_ID_PREFERRED, "id_only": FLOW_ID_ONLY, "name_only": FLOW_NAME_ONLY}
    normalized = aliases.get(text.lower(), text)
    return normalized if normalized in FLOW_RESOLUTION_OPTIONS else FLOW_ID_PREFERRED


# 함수 설명: `_selected_flow_metadata()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 FLOW·메타데이터 관련 값을 계산·변환하는 내부
#        helper입니다.
def _selected_flow_metadata(build_config: Any) -> dict[str, Any]:
    config = build_config if isinstance(build_config, dict) else {}
    flow_field = config.get("flow_name_selected") if isinstance(config.get("flow_name_selected"), dict) else {}
    metadata = flow_field.get("selected_metadata")
    return metadata if isinstance(metadata, dict) else {}


# 함수 설명: `_structured_payload()`는 페이로드을 후속 비교·표시에서 안정적으로 사용할 수 있는 값으로 구성합니다.
def _structured_payload(value: Any) -> dict[str, Any]:
    value = getattr(value, "data", value)
    if isinstance(value, dict) and isinstance(value.get("api_response"), dict):
        value = value["api_response"]
    if not isinstance(value, dict):
        raise ValueError("하위 Flow의 api_response가 객체 형식이 아닙니다.")
    return value


# 함수 설명: `_normalized_status()`는 여러 실행 결과를 확인해 상태의 최종 상태를 결정합니다.
def _normalized_status(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if text in {"ok", "success", "complete", "completed"}:
        return "ok"
    if text in {"partial", "warning", "degraded"}:
        return "partial"
    return "error"


# 함수 설명: `_continuation_payload()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 페이로드 관련 값을 계산·변환하는 내부 helper입니다.
def _continuation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("continuation")
    if isinstance(direct, dict):
        return direct
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    stored = plan.get("dependent_retrieval_plan") if isinstance(plan.get("dependent_retrieval_plan"), dict) else {}
    runtime = stored.get("runtime") if isinstance(stored.get("runtime"), dict) else {}
    return runtime


# 함수 설명: `_contract_object()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 object 관련 값을 계산·변환하는 내부 helper입니다.
def _contract_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        contract = value
    elif isinstance(value, str) and value.strip():
        if len(value.encode("utf-8")) > MAX_CONTRACT_BYTES:
            raise ValueError("continuation 계약이 허용 크기를 초과했습니다.")
        try:
            contract = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("continuation 계약 JSON이 유효하지 않습니다.") from exc
    else:
        return {}
    if not isinstance(contract, dict):
        raise ValueError("continuation 계약은 JSON object여야 합니다.")
    encoded = json.dumps(contract, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ValueError("continuation 계약이 허용 크기를 초과했습니다.")
    return contract


# 함수 설명: `_contract_json()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 JSON 관련 값을 계산·변환하는 내부 helper입니다.
def _contract_json(value: Any) -> str:
    contract = _contract_object(value)
    return json.dumps(contract, ensure_ascii=False, separators=(",", ":"), default=str) if contract else ""


# 함수 설명: `_has_binding()`는 입력값이 binding 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _has_binding(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().casefold() in {
                "bindings",
                "input_bindings",
                "handoff_bindings",
                "parameter_bindings",
                "upstream_bindings",
            }:
                if isinstance(item, list) and any(isinstance(entry, dict) and entry for entry in item):
                    return True
            if _has_binding(item):
                return True
    elif isinstance(value, list):
        return any(_has_binding(item) for item in value)
    return False


# 함수 설명: `_result_ref()`는 참조에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _result_ref(payload: dict[str, Any], continuation: dict[str, Any]) -> str:
    candidates: list[Any] = [continuation.get("result_ref")]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates.append(data.get("data_ref"))
    refs = payload.get("data_refs") if isinstance(payload.get("data_refs"), list) else []
    candidates.extend(item for item in refs if isinstance(item, dict) and str(item.get("role") or "") == "analysis_result")
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("ref_id") or value.get("result_ref") or value.get("data_ref") or value.get("_id")
        text = str(value or "").strip()
        if text:
            if len(text) > MAX_REF_CHARS:
                raise ValueError("result_ref가 허용 길이를 초과했습니다.")
            return text
    return ""


# 함수 설명: `_row_count()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 count 관련 값을 계산·변환하는 내부 helper입니다.
def _row_count(payload: dict[str, Any], continuation: dict[str, Any]) -> int:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [continuation.get("row_count"), continuation.get("current_stage_row_count"), data.get("row_count")]
    ref = data.get("data_ref") if isinstance(data.get("data_ref"), dict) else {}
    candidates.append(ref.get("row_count"))
    for value in candidates:
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


# 함수 설명: `_session_ids()`는 IDS을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _session_ids(payload: dict[str, Any], continuation: dict[str, Any]) -> set[str]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    return {
        str(value).strip()
        for value in (request.get("session_id"), state.get("session_id"), continuation.get("session_id"))
        if str(value or "").strip()
    }


# 함수 설명: `_continuation_signature()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 signature 관련 값을 계산·변환하는 내부
#        helper입니다.
def _continuation_signature(continuation: dict[str, Any]) -> str:
    return "|".join(
        str(continuation.get(key) or "").strip()
        for key in ("plan_id", "plan_hash", "stage_index", "current_stage_id", "next_stage_id", "continuation_ref")
    )


# 함수 설명: `_bounded_int()`는 INT이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(parsed, upper))


# 함수 설명: `_safe_message_text()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 Message·문자열 관련 값을 계산·변환하는 내부 helper입니다.
def _safe_message_text(payload: dict[str, Any]) -> str:
    for key in ("message", "display_message", "answer_message", "answer"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("text") or value.get("message") or value.get("answer")
        text = str(value or "").strip()
        if text:
            # return_direct 경로이므로 이 문자열은 후속 LLM context로 다시 전달되지 않습니다.
            # 기존 Data Analysis 답변 표를 임의 절단하지 않고 화면에 그대로 반환합니다.
            return text
    return ""


# 함수 설명: `_execution_message()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 Message 관련 값을 계산·변환하는 내부 helper입니다.
def _execution_message(
    text: str,
    *,
    status: str,
    stages_executed: int,
    auto_continued: bool,
    final_stage: str = "",
    failure_reason: str = "",
) -> Message:
    message = Message(text=text)
    trace = {
        "status": status,
        "stages_executed": stages_executed,
        "auto_continued": auto_continued,
        "final_stage": str(final_stage or "")[:MAX_STAGE_ID_CHARS],
    }
    if failure_reason:
        trace["failure_reason"] = str(failure_reason)[:200]
    message.data = {"continuation_execution": trace}
    return message


# 함수 설명: `_blocked_message()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 Message 관련 값을 계산·변환하는 내부 helper입니다.
def _blocked_message(reason: str, stages_executed: int) -> Message:
    labels = {
        "stage_status_not_ok": "첫 단계 분석이 완전하게 성공하지 않아 후속 조회를 실행하지 않았습니다.",
        "empty_stage_result": "첫 단계 결과가 비어 있어 후속 조회를 실행하지 않았습니다.",
        "result_ref_missing": "첫 단계 결과 참조가 없어 후속 조회를 실행하지 않았습니다.",
        "continuation_contract_missing": "후속 조회 계약이 없어 2차 실행을 차단했습니다.",
        "continuation_binding_missing": "후속 조회에 필요한 안전한 파라미터 연결 계약이 없습니다.",
        "continuation_ref_missing": "후속 조회 계약 참조가 없어 2차 실행을 차단했습니다.",
        "continuation_port_missing": "대상 Data Analysis Flow에 continuation 입력 포트가 모두 준비되지 않았습니다.",
        "session_mismatch": "첫 단계와 후속 단계의 세션이 일치하지 않아 연계 실행을 차단했습니다.",
        "continuation_limit_exceeded": "허용된 후속 분석 단계 수를 초과해 실행을 중단했습니다.",
        "duplicate_continuation": "동일한 후속 분석 단계의 중복 실행을 차단했습니다.",
        "continuation_still_pending": "최대 후속 분석 단계 이후에도 작업이 완료되지 않아 실행을 중단했습니다.",
        "continuation_result_error": "후속 조회가 정상 완료되지 않아 첫 단계 결과를 최종 답변으로 사용하지 않았습니다.",
        "child_timeout": "Data Analysis Flow 실행 시간이 제한을 초과했습니다.",
        "structured_response_invalid": "Data Analysis Flow의 구조화 응답 계약을 확인할 수 없습니다.",
    }
    text = labels.get(reason, "후속 분석의 안전 계약을 확인하지 못해 실행을 중단했습니다.")
    return _execution_message(text, status="blocked", stages_executed=stages_executed, auto_continued=stages_executed > 1, failure_reason=reason)


# Langflow 컴포넌트 클래스: 공개 Tool 입력은 question 하나로 유지하고 구조화 응답만으로 최대 두 번 실행합니다.
# 실제 행·trace·생성 코드는 Agent observation에 넣지 않고 최종 Message만 return_direct로 반환합니다.
class CachedContinuationRunFlowTool(RunFlowBaseComponent):
    display_name = "01 구조화 Continuation Cached Run Flow 도구"
    description = "구조화 API 계약으로 Data Analysis Flow를 한 번 또는 최대 두 번 실행하고 최종 답변만 직접 반환합니다."
    name = "CachedContinuationRunFlowTool"
    icon = "Workflow"

    inputs = [
        DropdownInput(
            name="flow_name_selected",
            display_name="대상 Flow",
            info="현재 프로젝트의 하위 Flow입니다. 선택 ID를 우선 사용하고 import 직후에는 이름으로 복구할 수 있습니다.",
            options=[],
            options_metadata=[],
            real_time_refresh=True,
            refresh_button=True,
            value=None,
            required=True,
        ),
        StrInput(name="flow_id_selected", display_name="선택된 Flow ID", value="", show=False, override_skip=True),
        DropdownInput(
            name="flow_resolution_mode",
            display_name="Flow 해석 방식",
            options=FLOW_RESOLUTION_OPTIONS,
            value=FLOW_ID_PREFERRED,
            advanced=True,
        ),
        MessageTextInput(name="session_id", display_name="세션 ID", value="", advanced=True),
        BoolInput(name="cache_flow", display_name="Flow 그래프 캐시", value=True, advanced=True),
        StrInput(name="tool_name", display_name="도구 이름", required=True),
        MultilineInput(name="tool_description", display_name="도구 설명", required=True),
        MultilineInput(name="required_all_keywords", display_name="필수 키워드", value="", advanced=True),
        MultilineInput(name="required_any_phrases", display_name="허용 호출 구문", value="", advanced=True),
        MultilineInput(name="keyword_gate_message", display_name="키워드 차단 안내", value="", advanced=True),
        MultilineInput(
            name="preferred_output_names",
            display_name="구조화 최종 출력 이름",
            value="api_response",
            advanced=True,
        ),
        BoolInput(
            name="enable_auto_continuation",
            display_name="자동 후속 분석",
            info="구조화 API가 pending 계약을 반환할 때만 동일 Flow를 한 번 더 호출합니다.",
            value=True,
            advanced=False,
        ),
        IntInput(
            name="max_continuation_stages",
            display_name="최대 분석 단계",
            info="현재 버전은 안전을 위해 1 또는 2만 허용합니다.",
            value=2,
            advanced=True,
        ),
        IntInput(
            name="continuation_timeout_seconds",
            display_name="전체 실행 제한 시간(초)",
            value=240,
            advanced=True,
        ),
        BoolInput(name="return_direct", display_name="결과 직접 반환", value=True, advanced=True),
    ]

    outputs = [Output(name="component_as_tool", display_name="연계 Flow 도구", method="to_toolkit", types=["Tool"], tool_mode=True)]

    # 함수 설명: `update_outputs()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 outputs 관련 값을 계산·변환하는 내부 helper입니다.
    async def update_outputs(self, frontend_node: dict[str, Any], field_name: str, field_value: Any) -> dict[str, Any]:
        frontend_node["outputs"] = [output.model_dump() for output in type(self).outputs]
        return frontend_node

    # 함수 설명: `update_build_config()`는 모델 선택에 따라 동적 입력 필드를 갱신하는 Langflow 빌드 lifecycle 함수입니다.
    async def update_build_config(self, build_config: dict[str, Any], field_value: Any, field_name: str | None = None) -> dict[str, Any]:
        attributes = getattr(self, "_attributes", None)
        if not isinstance(attributes, dict):
            attributes = {}
            self._attributes = attributes
        flow_field = build_config.setdefault("flow_name_selected", {"options": [], "options_metadata": [], "value": None})
        id_field = build_config.setdefault("flow_id_selected", {"value": ""})
        build_config.setdefault("flow_resolution_mode", {"value": FLOW_ID_PREFERRED})
        if field_name == "flow_name_selected" and (bool(build_config.get("is_refresh")) or field_value in (None, "")):
            flows = await self.alist_flows_by_flow_folder()
            options: list[str] = []
            metadata: list[dict[str, Any]] = []
            selected_id = str(id_field.get("value") or getattr(self, "flow_id_selected", "") or attributes.get("flow_id_selected") or "").strip()
            selected_name = str(field_value or flow_field.get("value") or getattr(self, "flow_name_selected", "") or attributes.get("flow_name_selected") or "").strip()
            match: tuple[str, dict[str, Any]] | None = None
            same_name: list[tuple[str, dict[str, Any]]] = []
            for flow in flows:
                data = getattr(flow, "data", None) or {}
                name = str(data.get("name") or "")
                meta = {"id": str(data.get("id") or ""), "updated_at": _as_iso_text(data.get("updated_at"))}
                options.append(name)
                metadata.append(meta)
                if selected_id and meta["id"] == selected_id:
                    match = (name, meta)
                if selected_name and name == selected_name:
                    same_name.append((name, meta))
            flow_field["options"] = options
            flow_field["options_metadata"] = metadata
            if match is None and not selected_id and len(same_name) == 1:
                match = same_name[0]
            if match is not None:
                name, meta = match
                flow_field["value"] = name
                flow_field["selected_metadata"] = meta
                id_field["value"] = meta["id"]
                self.flow_name_selected = name
                self.flow_id_selected = meta["id"]
                attributes["flow_name_selected"] = name
                attributes["flow_id_selected"] = meta["id"]
                attributes["flow_name_selected_updated_at"] = meta.get("updated_at")
                self._cached_flow_updated_at = meta.get("updated_at")
        elif field_name == "flow_name_selected" and field_value not in (None, ""):
            meta = _selected_flow_metadata(build_config)
            if str(meta.get("id") or "").strip():
                id_field["value"] = str(meta["id"])
                self.flow_id_selected = str(meta["id"])
                attributes["flow_id_selected"] = str(meta["id"])
        return build_config

    # 함수 설명: `get_graph()`는 대상 Flow 이름을 ID로 해석하고 재사용 가능한 그래프를 가져옵니다.
    async def get_graph(self, flow_name_selected: str | None = None, flow_id_selected: str | None = None, updated_at: str | None = None):
        flow_name = str(flow_name_selected or getattr(self, "flow_name_selected", "") or "").strip()
        requested_id = str(flow_id_selected or getattr(self, "flow_id_selected", "") or "").strip()
        mode = _flow_resolution_mode(getattr(self, "flow_resolution_mode", FLOW_ID_PREFERRED))
        if not flow_name and not requested_id:
            raise ValueError("대상 Flow를 선택해야 합니다.")
        runtime_user_id = str(getattr(self, "user_id", "") or "").strip()
        if not runtime_user_id:
            raise ValueError("Router 실행 사용자 ID가 없어 하위 Flow를 조회할 수 없습니다.")

        actual_id = requested_id
        actual_name = flow_name
        actual_updated_at = _as_iso_text(updated_at)
        if requested_id and mode != FLOW_NAME_ONLY:
            flow = await super().get_flow(flow_name_selected=None, flow_id_selected=requested_id)
            data = getattr(flow, "data", None) or {}
            if str(data.get("id") or "").strip():
                actual_id = str(data.get("id"))
                actual_name = str(data.get("name") or flow_name).strip()
                actual_updated_at = _as_iso_text(data.get("updated_at"))
            elif mode == FLOW_ID_ONLY:
                raise ValueError("선택한 Flow ID를 현재 사용자로 조회할 수 없습니다.")
            else:
                actual_id = ""
        if not actual_id:
            if not flow_name:
                raise ValueError("이름으로 복구할 대상 Flow 이름이 없습니다.")
            flow = await super().get_flow(flow_name_selected=flow_name, flow_id_selected=None)
            data = getattr(flow, "data", None) or {}
            actual_id = str(data.get("id") or "").strip()
            actual_name = str(data.get("name") or flow_name).strip()
            actual_updated_at = _as_iso_text(data.get("updated_at"))
            if not actual_id:
                raise ValueError(f"대상 Flow를 찾지 못했습니다: {flow_name}")

        self.flow_name_selected = actual_name
        self.flow_id_selected = actual_id
        self._attributes["flow_name_selected"] = actual_name
        self._attributes["flow_id_selected"] = actual_id
        self._attributes["flow_name_selected_updated_at"] = actual_updated_at
        self._cached_flow_updated_at = actual_updated_at
        graph = await super().get_graph(flow_name_selected=actual_name, flow_id_selected=actual_id, updated_at=actual_updated_at)
        vertices = getattr(graph, "vertices", [])
        self._resolved_chat_input_id = _single_io_id(vertices, "input")
        self._resolved_chat_output_id = _single_io_id(vertices, "output")
        self._input_supports_storage_toggle = _vertex_has_input(_single_vertex(vertices, self._resolved_chat_input_id), "should_store_message")
        self._output_supports_storage_toggle = _vertex_has_input(_single_vertex(vertices, self._resolved_chat_output_id), "should_store_message")
        self._resolved_continuation_input_ids = {name: _single_named_input_vertex_id(vertices, name) for name in CONTINUATION_INPUT_NAMES}
        target, output_types = _preferred_graph_output_target(graph, getattr(self, "preferred_output_names", "api_response"))
        if not output_types.intersection(STRUCTURED_OUTPUT_TYPES):
            raise ValueError("Continuation Tool은 Data 계열 api_response terminal을 사용해야 합니다.")
        _promote_graph_output(graph, target)
        self._resolved_flow_output_target = target
        return graph

    # 함수 설명: `get_required_data()`는 Flow tool 실행에 필요한 그래프와 입력 정보를 준비합니다.
    async def get_required_data(self):
        self._sync_flow_outputs([Output(name="lazy_flow_result", display_name="최종 하위 Flow 답변", method="_run_selected_flow", types=["Message"], tool_mode=True)])
        return str(getattr(self, "tool_description", "") or self.description), [_question_tool_field()]

    # 함수 설명: `_inherit_runtime_session()`는 01 구조화 Continuation Cached Run Flow 도구 처리 중 runtime·세션 관련 값을 계산·변환하는 내부
    #        helper입니다.
    def _inherit_runtime_session(self) -> str:
        explicit = str(getattr(self, "session_id", "") or "").strip()
        configured = str(getattr(self, "_session_id", "") or "").strip()
        parent = str(getattr(getattr(self, "graph", None), "session_id", "") or "").strip()
        inherited = explicit or configured or parent or f"continuation-{uuid.uuid4().hex}"
        self.session_id = inherited
        self._attributes["session_id"] = inherited
        return inherited

    # 함수 설명: `_execute_stage()`는 stage 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    async def _execute_stage(self, child_args: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
        self._active_child_args = dict(child_args)
        self._last_run_outputs = None
        try:
            await asyncio.wait_for(self._get_cached_run_outputs(user_id=self.user_id, output_type="any"), timeout=timeout_seconds)
            target = getattr(self, "_resolved_flow_output_target", None)
            if not target:
                raise ValueError("대상 Flow의 구조화 최종 출력을 확인할 수 없습니다.")
            result = await self._resolve_flow_output(vertex_id=target[0], output_name=target[1])
            return _structured_payload(result)
        finally:
            self._active_child_args = {}

    # 함수 설명: `_run_selected_flow()`는 selected·FLOW 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    async def _run_selected_flow(self) -> Message:
        values = _tool_values((getattr(self, "_attributes", {}) or {}).get("flow_tweak_data"))
        question = _tool_question(values)
        gate_error = _keyword_gate_error(question, getattr(self, "required_all_keywords", ""), getattr(self, "required_any_phrases", ""), getattr(self, "keyword_gate_message", ""))
        if gate_error:
            return _execution_message(gate_error, status="blocked", stages_executed=0, auto_continued=False, failure_reason="route_gate")
        if not question:
            return _blocked_message("structured_response_invalid", 0)

        session_id = self._inherit_runtime_session()
        max_stages = _bounded_int(getattr(self, "max_continuation_stages", 2), 2, 1, 2)
        timeout = _bounded_int(getattr(self, "continuation_timeout_seconds", 240), 240, 10, 600)
        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        try:
            first = await self._execute_stage({"question": question}, max(0.1, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            return _blocked_message("child_timeout", 0)
        except Exception:
            return _blocked_message("structured_response_invalid", 0)

        continuation = _continuation_payload(first)
        first_continuation_status = str(continuation.get("status") or "").strip().casefold()
        if first_continuation_status != "pending":
            if continuation and first_continuation_status not in {"not_applicable", "complete", "empty_complete"}:
                return _blocked_message("continuation_result_error", 1)
            text = _safe_message_text(first) or ("분석을 완료하지 못했습니다." if _normalized_status(first.get("status")) == "error" else "분석이 완료되었습니다.")
            return _execution_message(text, status=_normalized_status(first.get("status")), stages_executed=1, auto_continued=False, final_stage=str(continuation.get("current_stage_id") or "single"))

        if not bool(getattr(self, "enable_auto_continuation", True)) or max_stages < 2:
            return _blocked_message("continuation_limit_exceeded", 1)
        if _normalized_status(first.get("status")) != "ok":
            return _blocked_message("stage_status_not_ok", 1)
        if _row_count(first, continuation) <= 0:
            return _blocked_message("empty_stage_result", 1)
        first_session_ids = _session_ids(first, continuation)
        if not first_session_ids:
            return _blocked_message("session_missing", 1)
        if first_session_ids != {session_id}:
            return _blocked_message("session_mismatch", 1)

        try:
            result_ref = _result_ref(first, continuation)
        except ValueError:
            return _blocked_message("result_ref_invalid", 1)
        if not result_ref:
            return _blocked_message("result_ref_missing", 1)
        continuation_ref = str(continuation.get("continuation_ref") or "").strip()
        if not continuation_ref or len(continuation_ref) > MAX_REF_CHARS:
            return _blocked_message("continuation_ref_missing", 1)
        try:
            contract = _contract_object(continuation.get("continuation_contract"))
        except ValueError:
            return _blocked_message("continuation_contract_missing", 1)
        if not contract:
            return _blocked_message("continuation_contract_missing", 1)
        if not _has_binding(contract):
            return _blocked_message("continuation_binding_missing", 1)
        plan_id = str(continuation.get("plan_id") or contract.get("plan_id") or "").strip()
        plan_hash = str(continuation.get("plan_hash") or contract.get("plan_hash") or "").strip()
        current_stage = str(continuation.get("current_stage_id") or "").strip()
        next_stage = str(continuation.get("next_stage_id") or "").strip()
        if not plan_id or not plan_hash or not current_stage or not next_stage or current_stage == next_stage:
            return _blocked_message("continuation_contract_missing", 1)
        contract_plan_id = str(contract.get("plan_id") or "").strip()
        contract_plan_hash = str(contract.get("plan_hash") or "").strip()
        contract_session_id = str(contract.get("session_id") or "").strip()
        if (
            contract_plan_id != plan_id
            or contract_plan_hash != plan_hash
            or contract_session_id != session_id
        ):
            return _blocked_message("continuation_contract_mismatch", 1)
        try:
            declared_max_stages = int(continuation.get("max_stages") or contract.get("max_stages"))
        except (TypeError, ValueError):
            return _blocked_message("continuation_contract_missing", 1)
        if declared_max_stages != 2 or declared_max_stages > max_stages:
            return _blocked_message("continuation_limit_exceeded", 1)
        expected_continuation_ref = f"continuation:{plan_id}:{plan_hash}"
        embedded_continuation_ref = str(contract.get("continuation_ref") or expected_continuation_ref).strip()
        if continuation_ref != expected_continuation_ref or embedded_continuation_ref != expected_continuation_ref:
            return _blocked_message("continuation_ref_missing", 1)
        signature = _continuation_signature(continuation)
        if not signature.strip("|") or signature in seen:
            return _blocked_message("duplicate_continuation", 1)
        seen.add(signature)

        required_ports = getattr(self, "_resolved_continuation_input_ids", {}) or {}
        if any(not str(required_ports.get(name) or "").strip() for name in CONTINUATION_INPUT_NAMES):
            return _blocked_message("continuation_port_missing", 1)
        second_args = {
            "question": question,
            "upstream_result_ref": result_ref,
            "continuation_ref": continuation_ref,
            "continuation_contract": _contract_json(contract),
            "skip_intermediate_answer": False,
        }
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            second = await self._execute_stage(second_args, remaining)
        except asyncio.TimeoutError:
            return _blocked_message("child_timeout", 1)
        except Exception:
            return _blocked_message("structured_response_invalid", 1)

        final_continuation = _continuation_payload(second)
        final_session_ids = _session_ids(second, final_continuation)
        if not final_session_ids:
            return _blocked_message("session_missing", 2)
        if final_session_ids != {session_id}:
            return _blocked_message("session_mismatch", 2)
        final_continuation_status = str(final_continuation.get("status") or "").strip().casefold()
        if final_continuation_status == "pending":
            return _blocked_message("continuation_still_pending", 2)
        if final_continuation_status not in {"complete", "empty_complete"}:
            return _blocked_message("continuation_result_error", 2)
        final_plan_id = str(final_continuation.get("plan_id") or "").strip()
        final_plan_hash = str(final_continuation.get("plan_hash") or "").strip()
        if final_plan_id != plan_id or final_plan_hash != plan_hash:
            return _blocked_message("continuation_result_error", 2)
        final_stage = str(final_continuation.get("current_stage_id") or "").strip()
        final_next_stage = str(final_continuation.get("next_stage_id") or "").strip()
        try:
            final_stage_index = int(final_continuation.get("stage_index"))
        except (TypeError, ValueError):
            return _blocked_message("continuation_result_error", 2)
        if final_stage != next_stage or final_stage_index != declared_max_stages or final_next_stage:
            return _blocked_message("continuation_result_error", 2)
        final_status = _normalized_status(second.get("status"))
        if final_status != "ok":
            return _blocked_message("continuation_result_error", 2)
        text = _safe_message_text(second)
        if not text:
            return _blocked_message("continuation_result_error", 2)
        return _execution_message(text, status=final_status, stages_executed=2, auto_continued=True, final_stage=final_stage)

    # 함수 설명: `_build_flow_tweak_data()`는 FLOW·tweak·데이터 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
    def _build_flow_tweak_data(self) -> dict[str, dict[str, Any]]:
        child_args = getattr(self, "_active_child_args", {}) or {}
        question = str(child_args.get("question") or "").strip()
        if not question:
            raise ValueError("하위 Flow에 전달할 사용자 질문이 비어 있습니다.")
        input_id = str(getattr(self, "_resolved_chat_input_id", "") or "").strip()
        input_tweak: dict[str, Any] = {"input_value": question}
        if bool(getattr(self, "_input_supports_storage_toggle", False)):
            input_tweak["should_store_message"] = False
        tweaks: dict[str, dict[str, Any]] = {input_id: input_tweak}
        output_id = str(getattr(self, "_resolved_chat_output_id", "") or "").strip()
        if output_id and bool(getattr(self, "_output_supports_storage_toggle", False)):
            tweaks[output_id] = {"should_store_message": False}
        port_ids = getattr(self, "_resolved_continuation_input_ids", {}) or {}
        for name in CONTINUATION_INPUT_NAMES:
            if name not in child_args:
                continue
            vertex_id = str(port_ids.get(name) or "").strip()
            if not vertex_id:
                raise ValueError(f"대상 Flow에서 '{name}' 입력 component를 찾지 못했습니다.")
            tweaks.setdefault(vertex_id, {})[name] = child_args[name]
        return tweaks

    # 함수 설명: `_get_tools()`는 입력 또는 외부 저장소에서 tools을 읽고 호출자가 사용할 형태로 반환합니다.
    async def _get_tools(self):
        tools = await super()._get_tools()
        if len(tools) != 1:
            raise ValueError("대상 Flow에는 Agent 도구로 사용할 최종 출력이 정확히 하나 있어야 합니다.")
        tool = tools[0]
        tool_name = re.sub(r"[^a-zA-Z0-9_-]", "-", str(self.tool_name or "")).strip("-")
        if not tool_name:
            raise ValueError("도구 이름은 영문, 숫자, 밑줄 또는 하이픈을 포함해야 합니다.")
        tool.name = tool_name
        tool.description = str(self.tool_description or "").strip()
        tool.tags = [tool_name, "structured-continuation", "max-two-stages"]
        tool.return_direct = bool(self.return_direct)
        self.status = f"{tool.name}: {tool.description}"
        return [tool]

    # 함수 설명: `_pre_run_setup()`는 명시 session_id가 없으면 부모 graph 세션을 상속하고 Flow tool 실행 전 상태를 준비합니다.
    def _pre_run_setup(self) -> None:
        super()._pre_run_setup()
        self._inherit_runtime_session()
