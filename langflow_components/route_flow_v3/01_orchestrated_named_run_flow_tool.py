# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 연계 실행용 이름 기반 Cached Run Flow 도구
# 역할: 같은 사용자의 Flow를 이름으로 찾아 실행하고, 다음 Tool이 재사용할 수 있는 축약 결과 계약을 Agent에 반환합니다.
# 주요 입력: 대상 Flow 이름, 세션 ID, Tool 이름/설명, upstream result ref 지원 여부, result ref 생성 여부, 식별자 컬럼
# 주요 출력: Route V3 Agent에 연결할 Flow 도구 (component_as_tool)
# 처리 흐름: 고정 Tool schema 공개 -> 선택 시 Flow 이름/ID 해석 -> 질문/ref tweak -> 하위 Flow 실행 -> 안전한 축약 결과 반환
# 유지보수 포인트: 하위 Flow 전체 rows·trace·intent·pandas 코드는 Agent에 전달하지 않으며 부모 Router만 채팅을 저장합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from lfx.base.tools.run_flow import RunFlowBaseComponent
from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, MessageTextInput, MultilineInput, Output, StrInput
from lfx.schema.data import Data


CONTRACT_VERSION = "route_v3.tool_result.v1"
SUMMARY_LIMIT = 2000
ISSUE_LIMIT = 5
ISSUE_TEXT_LIMIT = 400
ENTITY_VALUE_LIMIT = 50
COLUMN_LIMIT = 50
REF_TEXT_LIMIT = 1024
OBSERVATION_BYTE_LIMIT = 8192
MAX_UNWRAP_DEPTH = 8


# 함수 설명: datetime 등 시간 값을 Flow 그래프 캐시 갱신 비교에 사용할 ISO 문자열로 변환합니다.
def _as_iso_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


# 함수 설명: Langflow Tool schema가 전달한 Pydantic 객체 또는 dict를 일반 dict로 정규화합니다.
def _tool_values(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return value if isinstance(value, dict) else {}


# 함수 설명: 문자열을 지정한 글자 수까지만 유지해 Agent observation의 예측 가능한 상한을 지킵니다.
def _limited_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: 현재 하위 Flow의 사용자 입력용 Chat Input ID를 실행 시점 그래프에서 정확히 하나 찾습니다.
def _single_chat_input_id(vertices: Any) -> str:
    candidates = [
        str(vertex.id)
        for vertex in list(vertices or [])
        if (getattr(vertex, "data", {}) or {}).get("type") == "ChatInput"
        or getattr(vertex, "display_name", "") == "Chat Input"
    ]
    if len(candidates) != 1:
        raise ValueError("대상 Flow에는 사용자 입력용 Chat Input이 정확히 하나 있어야 합니다.")
    return candidates[0]


# 함수 설명: 현재 하위 Flow의 답변 저장용 Chat Output ID를 실행 시점 그래프에서 정확히 하나 찾습니다.
def _single_chat_output_id(vertices: Any) -> str:
    candidates = [
        str(vertex.id)
        for vertex in list(vertices or [])
        if (getattr(vertex, "data", {}) or {}).get("type") == "ChatOutput"
        or getattr(vertex, "display_name", "") == "Chat Output"
    ]
    if len(candidates) != 1:
        raise ValueError("대상 Flow에는 답변용 Chat Output이 정확히 하나 있어야 합니다.")
    return candidates[0]


# 함수 설명: Vertex에 저장된 standalone component template에서 특정 입력 포트가 있는지 확인합니다.
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


# 함수 설명: upstream_result_ref를 받을 component 입력 위치를 찾아 모호한 fan-out 연결을 차단합니다.
def _single_named_input_vertex_id(vertices: Any, input_name: str, *, required: bool) -> str:
    candidates = [str(vertex.id) for vertex in list(vertices or []) if _vertex_has_input(vertex, input_name)]
    if not candidates and not required:
        return ""
    if len(candidates) != 1:
        raise ValueError(
            f"대상 Flow에는 '{input_name}' 입력을 가진 component가 정확히 하나 있어야 합니다. "
            f"현재 발견 개수={len(candidates)}"
        )
    return candidates[0]


# 함수 설명: successors가 없는 api_response를 런타임 output으로 승격해 Run Flow가 구조화 결과를 실제로 수집하게 합니다.
# 영구 JSON의 is_output은 변경하지 않으므로 같은 child Flow를 사용하는 Route V2의 단일 terminal 계약에는 영향이 없습니다.
def _preferred_graph_output_target(graph: Any) -> tuple[str, str]:
    successor_map = getattr(graph, "successor_map", {}) or {}
    api_targets: list[tuple[Any, str]] = []
    fallback_targets: list[tuple[str, str]] = []
    for vertex in list(getattr(graph, "vertices", []) or []):
        if successor_map.get(vertex.id, []):
            continue
        for output in list(getattr(vertex, "outputs", []) or []):
            output_name = output.get("name") if isinstance(output, dict) else getattr(output, "name", None)
            if output_name == "api_response":
                api_targets.append((vertex, str(output_name)))
            if output_name and getattr(vertex, "is_output", False):
                fallback_targets.append((str(vertex.id), str(output_name)))

    if len(api_targets) == 1:
        api_vertex, output_name = api_targets[0]
        # RunFlowBaseComponent는 output_type="any"여도 vertex.is_output인 결과만 RunOutputs에 담습니다.
        # base cache에는 mutation 이전 graph dump가 저장되므로 이 승격은 현재 Route V3 실행 객체에만 적용됩니다.
        api_vertex.is_output = True
        return str(api_vertex.id), output_name
    if len(api_targets) > 1:
        raise ValueError("대상 Flow에는 terminal api_response 출력이 하나만 있어야 합니다.")
    if len(fallback_targets) != 1:
        raise ValueError("대상 Flow에는 api_response 또는 단일 최종 출력이 있어야 합니다.")
    return fallback_targets[0]


# 함수 설명: Route Agent에 공개할 question과 선택적 upstream_result_ref 고정 schema를 만듭니다.
def _orchestration_tool_fields() -> list[dict[str, Any]]:
    return [
        {
            "name": "question",
            "display_name": "사용자 질문",
            "info": "현재 단계에서 하위 Flow가 수행할 구체적인 요청입니다.",
            "required": True,
            "value": "",
            "tool_mode": True,
            "type": str,
            "input_types": [],
            "is_list": False,
        },
        {
            "name": "upstream_result_ref",
            "display_name": "이전 결과 참조",
            "info": "바로 앞 Tool이 반환한 result_ref입니다. 종속 실행일 때만 정확히 그대로 전달합니다.",
            "required": False,
            "value": "",
            "tool_mode": True,
            "type": str,
            "input_types": [],
            "is_list": False,
        },
    ]


# 함수 설명: Agent Tool 인자를 현재 child graph의 Chat I/O 및 upstream 입력 tweak로 변환합니다.
def _orchestration_tweaks(
    chat_input_id: Any,
    flow_tweak_data: Any,
    chat_output_id: Any = "",
    upstream_input_vertex_id: Any = "",
    accepts_upstream_result_ref: bool = False,
) -> dict[str, dict[str, Any]]:
    node_id = str(chat_input_id or "").strip()
    if not node_id:
        raise ValueError("현재 하위 Flow의 Chat Input ID를 확인할 수 없습니다.")

    values = _tool_values(flow_tweak_data)
    question = str(values.get("question") or "").strip()
    if not question:
        raise ValueError("하위 Flow에 전달할 사용자 질문이 비어 있습니다.")

    upstream_ref = str(values.get("upstream_result_ref") or "").strip()
    if len(upstream_ref) > REF_TEXT_LIMIT:
        raise ValueError("upstream_result_ref가 허용 길이를 초과했습니다.")
    if upstream_ref and not accepts_upstream_result_ref:
        raise ValueError("이 Tool의 대상 Flow는 upstream_result_ref 입력을 지원하지 않습니다.")

    tweaks: dict[str, dict[str, Any]] = {
        node_id: {
            "input_value": question,
            "should_store_message": False,
        }
    }
    output_id = str(chat_output_id or "").strip()
    if output_id:
        tweaks[output_id] = {"should_store_message": False}
    if upstream_ref:
        upstream_node_id = str(upstream_input_vertex_id or "").strip()
        if not upstream_node_id:
            raise ValueError("대상 Flow에서 upstream_result_ref 입력 component를 찾지 못했습니다.")
        tweaks.setdefault(upstream_node_id, {})["upstream_result_ref"] = upstream_ref
    return tweaks


# 함수 설명: 해석할 수 없는 Run Flow wrapper를 정상 완료로 위장하지 않고 compact 오류 계약의 입력으로 변환합니다.
def _adapter_error(error_type: str, message: str) -> dict[str, Any]:
    safe_message = _limited_text(message, ISSUE_TEXT_LIMIT)
    return {
        "status": "error",
        "message": "하위 Flow 실행 결과 형식을 해석하지 못했습니다.",
        "errors": [{"type": error_type, "message": safe_message}],
    }


# 함수 설명: dict가 LFX artifact의 repr/raw/type envelope인지 확인해 일반 API dict와 구분합니다.
def _is_artifact_wrapper(value: dict[str, Any]) -> bool:
    return ("raw" in value and ("repr" in value or "type" in value)) or {"repr", "type"}.issubset(value)


# 함수 설명: Pydantic Data/Message가 직렬화된 text_key/data/default_value envelope인지 확인합니다.
def _is_serialized_data_wrapper(value: dict[str, Any]) -> bool:
    return "data" in value and "text_key" in value and ("default_value" in value or "category" in value)


# 함수 설명: Message 객체 또는 Message.data/serialized wrapper에서 사용자에게 보여 줄 text와 오류 상태만 꺼냅니다.
def _message_payload(value: Any, data: Any = None) -> dict[str, Any] | None:
    message_data = data if isinstance(data, dict) else {}
    class_name = value.__class__.__name__.lower() if value is not None else ""
    category = str(message_data.get("category") or getattr(value, "category", "") or "").strip().lower()
    is_message = class_name == "message" or category == "message"
    if not is_message:
        return None
    text = getattr(value, "text", None) if value is not None else None
    if text in (None, ""):
        text = message_data.get("text")
    text = "" if text is None else str(text).strip()
    if not text:
        return _adapter_error("empty_child_message", "하위 Flow Message에 text가 없습니다.")
    has_error = bool(getattr(value, "error", False) or message_data.get("error"))
    payload: dict[str, Any] = {"status": "error" if has_error else "ok", "message": text}
    if has_error:
        payload["errors"] = [{"type": "child_message_error", "message": _limited_text(text, ISSUE_TEXT_LIMIT)}]
    return payload


# 함수 설명: artifact, 실제/직렬화 Data·Message, JSON 문자열을 재귀적으로 풀되 지원하지 않는 wrapper는 오류로 반환합니다.
def _unwrap_child_payload(value: Any, *, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_UNWRAP_DEPTH:
        return _adapter_error("child_result_wrapper_depth_exceeded", "하위 Flow 결과 wrapper의 중첩 깊이가 너무 큽니다.")
    if value is None:
        return _adapter_error("empty_child_result", "하위 Flow가 결과를 반환하지 않았습니다.")

    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            return _adapter_error(
                "unsupported_child_result_list",
                f"단일 결과만 지원하지만 {len(value)}개의 값이 반환되었습니다.",
            )
        return _unwrap_child_payload(value[0], depth=depth + 1)

    data = getattr(value, "data", None)
    message_payload = _message_payload(value, data)
    if message_payload is not None:
        return message_payload

    # 실제 LFX Data는 .data에 API dict를 보관합니다. Message는 위 분기에서 먼저 처리해야 data.text를 잃지 않습니다.
    if isinstance(value, Data):
        if not isinstance(data, dict) or not data:
            return _adapter_error("empty_child_data", "하위 Flow Data.data가 비어 있거나 dict가 아닙니다.")
        return _unwrap_child_payload(data, depth=depth + 1)

    if isinstance(value, dict):
        if not value:
            return _adapter_error("empty_child_dict", "하위 Flow가 빈 dict를 반환했습니다.")
        direct_message = _message_payload(value, value)
        if direct_message is not None:
            return direct_message
        if _is_artifact_wrapper(value):
            raw = value.get("raw")
            if raw not in (None, ""):
                return _unwrap_child_payload(raw, depth=depth + 1)
            representation = value.get("repr")
            if representation not in (None, "") and str(value.get("type") or "").lower() in {"message", "text"}:
                return _unwrap_child_payload(representation, depth=depth + 1)
            return _adapter_error("empty_child_artifact", "하위 Flow artifact의 raw와 사용 가능한 repr가 비어 있습니다.")
        if "api_response" in value:
            api_response = value.get("api_response")
            if api_response is None:
                return _adapter_error("empty_api_response", "api_response wrapper의 값이 비어 있습니다.")
            return _unwrap_child_payload(api_response, depth=depth + 1)
        if _is_serialized_data_wrapper(value):
            serialized_data = value.get("data")
            serialized_message = _message_payload(value, serialized_data)
            if serialized_message is not None:
                return serialized_message
            if not isinstance(serialized_data, dict) or not serialized_data:
                return _adapter_error("invalid_serialized_data", "직렬화된 Data wrapper의 data가 비어 있거나 dict가 아닙니다.")
            return _unwrap_child_payload(serialized_data, depth=depth + 1)
        # status/message/data/data_refs 등을 가진 기존 direct dict API 계약은 그대로 유지합니다.
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _adapter_error("empty_child_text", "하위 Flow가 빈 문자열을 반환했습니다.")
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"status": "ok", "message": text}
        return _unwrap_child_payload(decoded, depth=depth + 1)

    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
        except Exception as exc:  # noqa: BLE001
            return _adapter_error("child_result_model_dump_error", str(exc))
        return _unwrap_child_payload(dumped, depth=depth + 1)

    # Data 유사 객체는 테스트 double 또는 provider wrapper일 수 있으므로 dict data만 명시적으로 허용합니다.
    if isinstance(data, dict):
        if not data:
            return _adapter_error("empty_child_data_like_result", "하위 Flow data-like 결과의 data가 비어 있습니다.")
        return _unwrap_child_payload(data, depth=depth + 1)
    return _adapter_error("unsupported_child_result", f"지원하지 않는 하위 Flow 결과 타입입니다: {type(value).__name__}")


# 함수 설명: JSON Message 또는 Data 결과에서 API 응답 dict를 꺼내는 공개 호환 helper입니다.
def _child_payload(value: Any) -> dict[str, Any]:
    return _unwrap_child_payload(value)


# 함수 설명: 다양한 하위 Flow 상태 문자열을 Route V3의 ok·partial·error 세 값으로 정규화합니다.
def _normalized_status(payload: dict[str, Any], errors: list[str]) -> str:
    raw = str(payload.get("status") or "").strip().lower()
    if payload.get("success") is False or errors or raw in {"error", "failed", "failure", "invalid", "blocked"}:
        return "error"
    if raw in {"partial", "warning", "degraded"}:
        return "partial"
    return "ok"


# 함수 설명: 오류/경고 객체에서 type과 message만 남기고 원본 JSON 또는 traceback 노출을 막습니다.
def _issue_text(value: Any) -> str:
    if isinstance(value, dict):
        issue_type = str(value.get("type") or value.get("code") or "").strip()
        raw_message = value.get("message") or value.get("detail") or value.get("error") or ""
        message = str(raw_message).strip() if not isinstance(raw_message, (dict, list, tuple, set)) else ""
        text = f"{issue_type}: {message}" if issue_type and message else issue_type or message
        return _limited_text(text, ISSUE_TEXT_LIMIT)
    return _limited_text(value, ISSUE_TEXT_LIMIT)


# 함수 설명: 허용된 상위/단계 필드에서 오류나 경고만 제한적으로 수집하고 trace 전체는 버립니다.
def _collect_issues(payload: dict[str, Any], kind: str) -> list[str]:
    collected: list[str] = []

    # 함수 설명: 중첩된 오류·경고 값을 허용 개수까지 문자열 목록에 추가합니다.
    def append_values(value: Any) -> None:
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = _issue_text(item)
            if text and text not in collected:
                collected.append(text)
            if len(collected) >= ISSUE_LIMIT:
                return

    append_values(payload.get(kind))
    if kind == "errors" and payload.get("error"):
        append_values(payload.get("error"))
    for container_name in ("trace", "analysis", "write_result", "metadata_authoring"):
        if len(collected) >= ISSUE_LIMIT:
            break
        container = payload.get(container_name)
        if isinstance(container, dict):
            append_values(container.get(kind))
            if kind == "errors" and container.get("error"):
                append_values(container.get("error"))
    return collected[:ISSUE_LIMIT]


# 함수 설명: Markdown 표와 코드 fence를 제거하고 첫 답변 문단만 남겨 raw rows·pandas 코드가 observation에 섞이지 않게 합니다.
def _compact_summary_text(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    text = re.sub(r"```[\s\S]*?```", "", text)
    answer_match = re.search(
        r"(?:^|\n)#{1,4}\s*(?:답변|answer)\s*\n(?P<body>[\s\S]*?)(?=\n#{1,4}\s|\Z)",
        text,
        flags=re.IGNORECASE,
    )
    if answer_match:
        text = answer_match.group("body").strip()

    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if line.startswith("|") and line.endswith("|"):
            continue
        if re.match(r"^#{1,4}\s+", line) and lines:
            break
        lines.append(line)
    return _limited_text("\n".join(lines).strip(), SUMMARY_LIMIT)


# 함수 설명: 긴 Markdown 결과를 그대로 보내지 않고 Agent가 다음 판단에 필요한 요약 문자열만 선택합니다.
def _summary(payload: dict[str, Any], status: str) -> str:
    for key in ("summary", "message", "display_message", "answer_message", "answer"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("text") or value.get("message") or ""
        text = _compact_summary_text(value)
        if text:
            return text
    return "하위 Flow 실행 중 오류가 발생했습니다." if status == "error" else "하위 Flow 실행이 완료되었습니다."


# 함수 설명: data_refs에서 분석 결과 ref를 우선 선택하고 MongoDB 내부 위치 정보는 외부 계약에서 제거합니다.
def _result_reference(payload: dict[str, Any], enabled: bool) -> tuple[str, dict[str, Any]]:
    if not enabled:
        return "", {}

    refs = payload.get("data_refs") if isinstance(payload.get("data_refs"), list) else []
    candidates = [item for item in refs if isinstance(item, dict)]
    selected = next((item for item in candidates if str(item.get("role") or "") == "analysis_result"), None)
    if selected is None and candidates:
        selected = candidates[0]

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raw_ref: Any = selected or data.get("data_ref") or payload.get("result_ref") or payload.get("data_ref") or ""
    if isinstance(raw_ref, dict):
        selected = raw_ref
        raw_ref = raw_ref.get("ref_id") or raw_ref.get("result_ref") or raw_ref.get("data_ref") or raw_ref.get("_id") or ""
    ref_id = _limited_text(raw_ref, REF_TEXT_LIMIT)
    if not ref_id:
        return "", {}

    selected = selected if isinstance(selected, dict) else {}
    raw_columns = selected.get("columns") or data.get("columns") or []
    columns = [_limited_text(item, 120) for item in raw_columns[:COLUMN_LIMIT]] if isinstance(raw_columns, list) else []
    raw_row_count = selected.get("row_count", data.get("row_count"))
    try:
        row_count: int | None = int(raw_row_count) if raw_row_count is not None else None
    except (TypeError, ValueError):
        row_count = None
    meta: dict[str, Any] = {
        "role": _limited_text(selected.get("role") or "analysis_result", 80),
        "columns": columns,
    }
    if row_count is not None:
        meta["row_count"] = row_count
    return ref_id, meta


# 함수 설명: 쉼표·세미콜론·줄바꿈으로 입력한 식별자 컬럼을 중복 없이 정규화합니다.
def _entity_columns(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[,;\n\r]+", str(value or ""))
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result[:COLUMN_LIMIT]


# 함수 설명: configured column의 preview 값만 추출하며 ID는 문자열로 보존해 선행 0 손실을 막습니다.
def _entity_ids(payload: dict[str, Any], configured_columns: Any) -> list[dict[str, Any]]:
    columns = _entity_columns(configured_columns)
    if not columns:
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    try:
        source_row_count = int(data.get("row_count")) if data.get("row_count") is not None else len(rows)
    except (TypeError, ValueError):
        source_row_count = len(rows)

    result: list[dict[str, Any]] = []
    for configured in columns:
        values: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            actual_key = next((key for key in row if str(key).casefold() == configured.casefold()), None)
            raw_value = row.get(actual_key) if actual_key is not None else None
            raw_values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
            for item in raw_values:
                if item is None or isinstance(item, dict):
                    continue
                text = _limited_text(item, 160)
                if text and text not in values:
                    values.append(text)
                if len(values) >= ENTITY_VALUE_LIMIT:
                    break
            if len(values) >= ENTITY_VALUE_LIMIT:
                break
        if not values:
            continue
        entity_type = re.sub(r"[^a-z0-9]+", "_", configured.lower()).strip("_")
        entity_type = re.sub(r"_?id$", "", entity_type).strip("_") or "entity"
        result.append(
            {
                "entity_type": entity_type,
                "column": configured,
                "values": values,
                "observed_count": len(values),
                "source_row_count": source_row_count,
                "complete": len(rows) >= source_row_count and len(values) < ENTITY_VALUE_LIMIT,
            }
        )
    return result


# 함수 설명: compact contract가 8KB를 넘으면 ID·이슈·요약 순서로 더 줄여 LLM 입력 폭증을 방지합니다.
def _fit_observation_limit(contract: dict[str, Any]) -> dict[str, Any]:
    # 함수 설명: 현재 compact contract를 JSON으로 직렬화했을 때의 UTF-8 바이트 크기를 계산합니다.
    def byte_size() -> int:
        return len(json.dumps(contract, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))

    if byte_size() <= OBSERVATION_BYTE_LIMIT:
        return contract
    for entity in contract.get("entity_ids", []):
        entity["values"] = entity.get("values", [])[:20]
        entity["observed_count"] = len(entity["values"])
        entity["complete"] = False
    if byte_size() <= OBSERVATION_BYTE_LIMIT:
        return contract
    contract["warnings"] = [_limited_text(item, 200) for item in contract.get("warnings", [])[:2]]
    contract["errors"] = [_limited_text(item, 200) for item in contract.get("errors", [])[:2]]
    contract["summary"] = _limited_text(contract.get("summary"), 1000)
    meta = contract.get("result_ref_meta") if isinstance(contract.get("result_ref_meta"), dict) else {}
    meta["columns"] = list(meta.get("columns") or [])[:20]
    if byte_size() <= OBSERVATION_BYTE_LIMIT:
        return contract
    for entity in contract.get("entity_ids", []):
        entity["values"] = entity.get("values", [])[:5]
        entity["observed_count"] = len(entity["values"])
        entity["complete"] = False
    contract["summary"] = _limited_text(contract.get("summary"), 500)
    meta["columns"] = list(meta.get("columns") or [])[:10]
    if byte_size() <= OBSERVATION_BYTE_LIMIT:
        return contract

    # 식별자 컬럼 설정이 과도하더라도 최종 observation 상한을 반드시 지키는 최소 계약입니다.
    contract["entity_ids"] = list(contract.get("entity_ids") or [])[:5]
    for entity in contract["entity_ids"]:
        entity["values"] = entity.get("values", [])[:2]
        entity["observed_count"] = len(entity["values"])
        entity["complete"] = False
    contract["warnings"] = [_limited_text(item, 160) for item in contract.get("warnings", [])[:1]]
    contract["errors"] = [_limited_text(item, 160) for item in contract.get("errors", [])[:1]]
    contract["summary"] = _limited_text(contract.get("summary"), 300)
    meta["columns"] = list(meta.get("columns") or [])[:5]
    return contract


# 함수 설명: Tool의 ref 입력/출력 지원 여부를 설명에 자동 부착해 Agent의 잘못된 handoff를 줄입니다.
def _description_with_capabilities(description: Any, accepts: bool, produces: bool) -> str:
    base = str(description or "").strip()
    suffix = (
        f" 이전 결과 참조 입력={'지원' if accepts else '미지원'}, "
        f"다음 단계용 결과 참조 생성={'지원' if produces else '미지원'}."
    )
    return base + suffix


# 주요 함수: 하위 Flow 결과에서 다음 Tool에 필요한 최소 정보만 route_v3.tool_result.v1 계약으로 만듭니다.
def normalize_tool_result(
    child_result: Any,
    *,
    tool_name: str,
    entity_id_columns: Any = "",
    can_produce_result_ref: bool = False,
) -> dict[str, Any]:
    payload = _child_payload(child_result)
    errors = _collect_issues(payload, "errors")
    warnings = _collect_issues(payload, "warnings")
    status = _normalized_status(payload, errors)
    result_ref, result_ref_meta = _result_reference(payload, bool(can_produce_result_ref))
    if can_produce_result_ref and status != "error" and not result_ref:
        warning = "result_ref_unavailable: 현재 결과는 다음 Tool의 입력으로 재사용할 수 없습니다."
        if warning not in warnings:
            warnings.append(warning)

    contract = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "tool_name": _limited_text(tool_name, 100),
        "summary": _summary(payload, status),
        "result_ref": result_ref,
        "result_ref_meta": result_ref_meta,
        "entity_ids": _entity_ids(payload, entity_id_columns),
        "handoff_usable": bool(result_ref and status in {"ok", "partial"}),
        "warnings": warnings[:ISSUE_LIMIT],
        "errors": errors[:ISSUE_LIMIT],
    }
    return _fit_observation_limit(contract)


# Langflow 컴포넌트 클래스: 캔버스 입력과 Tool 출력 계약을 정의하며 독립형 JSON 안에 그대로 포함됩니다.
class OrchestratedNamedRunFlowTool(RunFlowBaseComponent):
    display_name = "01 연계 실행용 이름 기반 Cached Run Flow 도구"
    description = "선택된 하위 Flow를 lazy 실행하고 다음 Tool이 사용할 수 있는 compact result contract를 반환합니다."
    name = "OrchestratedNamedRunFlowTool"
    icon = "Workflow"

    inputs = [
        StrInput(
            name="flow_name_selected",
            display_name="대상 Flow 이름",
            info="Import된 하위 Flow의 정확한 이름입니다. 실행 시 현재 사용자 기준 실제 ID로 해석합니다.",
            required=True,
        ),
        StrInput(
            name="flow_id_selected",
            display_name="해석된 Flow ID",
            info="실행 시 이름으로 해석하며 export에는 고정하지 않습니다.",
            value="",
            show=False,
            override_skip=True,
        ),
        MessageTextInput(
            name="session_id",
            display_name="세션 ID",
            info="비우면 부모 Route V3 실행 세션을 자동 상속합니다.",
            value="",
            advanced=True,
        ),
        BoolInput(
            name="cache_flow",
            display_name="Flow 그래프 캐시",
            info="하위 Flow 그래프만 ID 기준으로 캐시하며 데이터와 답변은 매번 다시 실행합니다.",
            value=True,
            advanced=True,
        ),
        StrInput(
            name="tool_name",
            display_name="도구 이름",
            info="Agent가 호출할 영문 도구 이름입니다.",
            required=True,
        ),
        MultilineInput(
            name="tool_description",
            display_name="도구 설명",
            info="Agent가 단일 또는 연계 호출 순서를 판단할 수 있도록 입력과 결과 범위를 설명합니다.",
            required=True,
        ),
        BoolInput(
            name="return_direct",
            display_name="결과 직접 반환",
            info="Route V3 연계 실행에서는 false를 사용해 Tool 결과를 Agent가 다음 단계에서 판단하게 합니다.",
            value=False,
            advanced=True,
        ),
        BoolInput(
            name="accepts_upstream_result_ref",
            display_name="이전 결과 참조 입력 지원",
            info="대상 Flow에 upstream_result_ref 입력 component가 정확히 하나 있을 때만 켭니다.",
            value=False,
            advanced=False,
        ),
        BoolInput(
            name="can_produce_result_ref",
            display_name="결과 참조 생성 가능",
            info="대상 Flow의 api_response가 재사용 가능한 data_refs를 반환할 때 켭니다.",
            value=False,
            advanced=False,
        ),
        MultilineInput(
            name="entity_id_columns",
            display_name="전달 식별자 컬럼",
            info="Agent 관찰값에 preview로 포함할 ID 컬럼입니다. 쉼표 또는 줄바꿈으로 구분합니다.",
            value="",
            required=False,
            advanced=False,
        ),
    ]

    outputs = [
        Output(
            name="component_as_tool",
            display_name="연계 Flow 도구",
            method="to_toolkit",
            types=["Tool"],
            tool_mode=True,
        )
    ]

    # 주요 메서드: 현재 실행 사용자 기준으로 이름/ID를 해석하고 실제 child graph 입력과 출력을 검증합니다.
    async def get_graph(
        self,
        flow_name_selected: str | None = None,
        flow_id_selected: str | None = None,
        updated_at: str | None = None,
    ):
        flow_name = str(flow_name_selected or getattr(self, "flow_name_selected", "") or "").strip()
        if not flow_name:
            raise ValueError("대상 Flow 이름이 필요합니다.")

        runtime_user_id = str(getattr(self, "user_id", "") or "").strip()
        if not runtime_user_id:
            raise ValueError(
                "Router 실행 사용자 ID가 없어 하위 Flow를 조회할 수 없습니다. "
                "Route V3와 하위 Flow를 같은 사용자로 import하고 같은 사용자/API key로 실행하세요."
            )

        resolved_flow = None
        requested_flow_id = str(flow_id_selected or getattr(self, "flow_id_selected", "") or "").strip()
        if requested_flow_id:
            try:
                UUID(requested_flow_id)
            except (TypeError, ValueError, AttributeError):
                requested_flow_id = ""
        if requested_flow_id:
            id_flow = await super().get_flow(flow_name_selected=None, flow_id_selected=requested_flow_id)
            id_flow_data = getattr(id_flow, "data", None) or {}
            id_flow_name = str(id_flow_data.get("name") or "").strip()
            if id_flow_data.get("id") and (not id_flow_name or id_flow_name == flow_name):
                resolved_flow = id_flow

        flow = resolved_flow or await super().get_flow(flow_name_selected=flow_name, flow_id_selected=None)
        flow_data = getattr(flow, "data", None) or {}
        actual_id = str(flow_data.get("id") or "").strip()
        actual_updated_at = _as_iso_text(flow_data.get("updated_at")) or _as_iso_text(updated_at)
        if not actual_id:
            raise ValueError(
                "현재 Router 실행 사용자에게서 대상 Flow를 찾지 못했거나 ID가 없습니다. "
                f"flow_name={flow_name!r}, user_id={runtime_user_id!r}. "
                "이름에 '(1)' 등이 붙지 않았는지와 하위 Flow 소유자가 같은지 확인하세요."
            )

        self.flow_name_selected = flow_name
        self.flow_id_selected = actual_id
        self._attributes["flow_name_selected"] = flow_name
        self._attributes["flow_id_selected"] = actual_id
        self._attributes["flow_name_selected_updated_at"] = actual_updated_at
        self._cached_flow_updated_at = actual_updated_at
        graph = await super().get_graph(
            flow_name_selected=flow_name,
            flow_id_selected=actual_id,
            updated_at=actual_updated_at,
        )
        vertices = getattr(graph, "vertices", [])
        self._resolved_chat_input_id = _single_chat_input_id(vertices)
        self._resolved_chat_output_id = _single_chat_output_id(vertices)
        self._resolved_upstream_input_vertex_id = _single_named_input_vertex_id(
            vertices,
            "upstream_result_ref",
            required=bool(getattr(self, "accepts_upstream_result_ref", False)),
        )
        self._resolved_flow_output_target = _preferred_graph_output_target(graph)
        return graph

    # 주요 메서드: Agent Tool 목록을 만들 때 child graph를 열지 않고 고정된 두 필드만 노출합니다.
    async def get_required_data(self):
        self._sync_flow_outputs(
            [
                Output(
                    name="lazy_flow_result",
                    display_name="축약된 하위 Flow 결과",
                    method="_run_selected_flow",
                    types=["Data"],
                    tool_mode=True,
                )
            ]
        )
        description = str(getattr(self, "tool_description", "") or self.description).strip()
        accepts = bool(getattr(self, "accepts_upstream_result_ref", False))
        produces = bool(getattr(self, "can_produce_result_ref", False))
        return _description_with_capabilities(description, accepts, produces), _orchestration_tool_fields()

    # 주요 메서드: 선택된 child Flow를 한 번만 실행하고 raw 결과를 compact Data 계약으로 변환합니다.
    async def _run_selected_flow(self) -> Data:
        self._last_run_outputs = None
        await self._get_cached_run_outputs(user_id=self.user_id, output_type="any")
        target = getattr(self, "_resolved_flow_output_target", None)
        if not target:
            raise ValueError("대상 Flow의 최종 출력을 확인할 수 없습니다.")
        vertex_id, output_name = target
        child_result = await self._resolve_flow_output(vertex_id=vertex_id, output_name=output_name)
        contract = normalize_tool_result(
            child_result,
            tool_name=str(getattr(self, "tool_name", "") or ""),
            entity_id_columns=getattr(self, "entity_id_columns", ""),
            can_produce_result_ref=bool(getattr(self, "can_produce_result_ref", False)),
        )
        self.status = contract.get("summary", "")
        return Data(data=contract)

    # 주요 메서드: 공개 question/ref 인자를 import 후 실제 node ID에 맞는 runtime tweak로 변환합니다.
    def _build_flow_tweak_data(self) -> dict[str, dict[str, Any]]:
        return _orchestration_tweaks(
            getattr(self, "_resolved_chat_input_id", ""),
            self._attributes.get("flow_tweak_data"),
            getattr(self, "_resolved_chat_output_id", ""),
            getattr(self, "_resolved_upstream_input_vertex_id", ""),
            bool(getattr(self, "accepts_upstream_result_ref", False)),
        )

    # 주요 메서드: Langflow toolkit이 만든 단일 Tool에 안정적인 이름·설명·return_direct 설정을 적용합니다.
    async def _get_tools(self):
        tools = await super()._get_tools()
        if len(tools) != 1:
            raise ValueError("대상 Flow에는 Agent 도구로 사용할 축약 결과 출력이 정확히 하나 있어야 합니다.")

        tool = tools[0]
        tool_name = re.sub(r"[^a-zA-Z0-9_-]", "-", str(self.tool_name or "")).strip("-")
        if not tool_name:
            raise ValueError("도구 이름은 영문, 숫자, 밑줄 또는 하이픈을 포함해야 합니다.")
        tool.name = tool_name
        tool.description = _description_with_capabilities(
            self.tool_description,
            bool(getattr(self, "accepts_upstream_result_ref", False)),
            bool(getattr(self, "can_produce_result_ref", False)),
        )
        tool.tags = [tool_name, "route-v3-orchestrated"]
        tool.return_direct = bool(self.return_direct)
        self.status = f"{tool.name}: {tool.description}"
        return [tool]

    # 주요 메서드: 별도 session edge 없이 부모 graph 세션을 상속하고 child 실행 전 캐시 상태를 준비합니다.
    def _pre_run_setup(self) -> None:
        super()._pre_run_setup()
        explicit = str(getattr(self, "session_id", "") or "").strip()
        parent_session = str(getattr(getattr(self, "graph", None), "session_id", "") or "").strip()
        inherited = explicit or parent_session
        if inherited:
            self.session_id = inherited
            self._attributes["session_id"] = inherited
