from __future__ import annotations

import json
import os
import time
import uuid
from copy import deepcopy
from typing import Any

import requests
from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output, SecretStrInput
from lfx.schema.data import Data
from lfx.schema.message import Message

_HTTP_SESSION = requests.Session()
DEFAULT_LANGFLOW_BASE_URL = "http://127.0.0.1:7860"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_READ_TIMEOUT_SECONDS = 240


def run_flow_api_message(
    flow_input_value: Any,
    *,
    api_url: str = "",
    api_key: str = "",
    session_id: str = "",
    session_source_value: Any = None,
    timeout_seconds: Any = None,
    connect_timeout_seconds: Any = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    read_timeout_seconds: Any = DEFAULT_READ_TIMEOUT_SECONDS,
    route_name: str = "",
    post_func: Any = None,
) -> dict[str, Any]:
    flow_input = _input_text(flow_input_value, preserve=True)
    session_source = session_source_value if session_source_value is not None else flow_input_value
    session_id_value = _session_id(session_source, session_id)
    api_url_value = _resolve_api_url(api_url)
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not flow_input.strip():
        errors.append({"type": "empty_input", "message": "하위 flow로 전달할 입력이 비어 있습니다."})
    if _looks_like_route_message(flow_input):
        errors.append(
            {
                "type": "route_message_used_as_input",
                "message": (
                    "Smart Router Route Message가 사용자 질문 대신 전달되었습니다. "
                    "API 호출 route의 Route Message를 비우고 Smart Router route output이 원문을 그대로 보내도록 설정하세요."
                ),
            }
        )
    if not _clean(api_url):
        errors.append({"type": "missing_api_url", "message": "하위 flow의 Langflow Run API URL이 비어 있습니다."})

    if errors:
        return _message_result(
            status="error",
            message=_format_errors(errors),
            flow_input=flow_input,
            api_url=api_url_value,
            session_id=session_id_value,
            warnings=warnings,
            errors=errors,
            raw_response={},
            duration_ms=0,
            route_name=_clean(route_name),
        )

    request_body = {
        "input_value": flow_input,
        "input_type": "chat",
        "output_type": "chat",
    }
    if session_id_value:
        request_body["session_id"] = session_id_value
    headers = {"Content-Type": "application/json"}
    api_key_value = _secret_text(api_key) or _clean(os.getenv("LANGFLOW_API_KEY", ""))
    if api_key_value:
        headers["x-api-key"] = api_key_value

    started = time.monotonic()
    post = post_func or _HTTP_SESSION.post
    timeout = _timeout_value(timeout_seconds, connect_timeout_seconds, read_timeout_seconds)
    try:
        response = post(api_url_value, json=request_body, headers=headers, timeout=timeout)
        http_status = int(getattr(response, "status_code", 200) or 200)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        parsed = response.json() if callable(getattr(response, "json", None)) else response
    except Exception as exc:
        return _message_result(
            status="error",
            message=f"하위 flow API 호출에 실패했습니다: {exc}",
            flow_input=flow_input,
            api_url=api_url_value,
            session_id=session_id_value,
            warnings=warnings,
            errors=[{"type": "api_call_failed", "message": str(exc)}],
            raw_response={},
            duration_ms=_duration_ms(started),
            route_name=_clean(route_name),
        )

    raw_response = parsed if isinstance(parsed, dict) else {"response": parsed}
    message = _extract_message_text(raw_response)
    child_status = _extract_child_status(raw_response)
    child_failed = child_status in {"error", "failed", "failure"}
    if not message:
        message = "하위 flow가 표시 메시지를 반환하지 않았습니다."
        errors.append({"type": "missing_downstream_message", "message": message})
    result = _message_result(
        status="error" if child_failed or errors else "ok",
        message=message,
        flow_input=flow_input,
        api_url=api_url_value,
        session_id=session_id_value,
        warnings=warnings,
        errors=errors,
        raw_response=raw_response,
        duration_ms=_duration_ms(started),
        route_name=_clean(route_name),
    )
    result["request_body"] = request_body
    result["http_status"] = http_status
    result["downstream_status"] = child_status or "unknown"
    return result


def _message_result(
    *,
    status: str,
    message: str,
    flow_input: str,
    api_url: str,
    session_id: str,
    warnings: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    raw_response: dict[str, Any],
    duration_ms: int,
    route_name: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "flow_input": flow_input,
        "api_url": api_url,
        "session_id": session_id,
        "warnings": warnings,
        "errors": errors,
        "raw_response": raw_response,
        "duration_ms": duration_ms,
        "route_name": route_name,
    }


def _looks_like_route_message(value: Any) -> bool:
    parsed = _parse_json_dict(str(value or ""))
    if not parsed:
        return False
    keys = {str(key) for key in parsed}
    return bool(keys & {"route", "selected_route", "route_name"}) and len(keys) <= 3


def _format_errors(errors: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {error.get('message', '')}" for error in errors if error.get("message"))


def _extract_message_text(value: Any) -> str:
    return _extract_message_text_inner(value, set())


def _extract_child_status(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    api_response = value.get("api_response")
    if isinstance(api_response, dict) and str(api_response.get("status") or "").strip():
        return str(api_response.get("status")).strip().lower()
    if str(value.get("status") or "").strip():
        return str(value.get("status")).strip().lower()
    for key in ("outputs", "results", "data", "artifacts"):
        nested = value.get(key)
        values = nested if isinstance(nested, list) else [nested]
        for item in values:
            status = _extract_child_status(item)
            if status:
                return status
    return ""


def _extract_message_text_inner(value: Any, seen: set[int]) -> str:
    if value is None:
        return ""
    value_id = id(value)
    if value_id in seen:
        return ""
    seen.add(value_id)
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    if isinstance(value, str):
        parsed = _parse_json_dict(value)
        if parsed:
            return _extract_message_text_inner(parsed, seen)
        return value.strip()
    if isinstance(value, dict):
        for key in ("api_response", "display_message", "answer_message", "answer", "text", "content", "output", "response", "message"):
            text = _extract_message_text_inner(value.get(key), seen)
            if text:
                return text
        for key in ("results", "artifacts", "outputs", "data", "messages"):
            text = _extract_message_text_inner(value.get(key), seen)
            if text:
                return text
    if isinstance(value, list):
        for item in value:
            text = _extract_message_text_inner(item, seen)
            if text:
                return text
    return ""


def _parse_json_dict(value: str) -> dict[str, Any]:
    text = _clean(value)
    if not text:
        return {}
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return deepcopy(parsed) if isinstance(parsed, dict) else {}


def _input_text(value: Any, *, preserve: bool = False) -> str:
    if value is None:
        return ""
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text if preserve else text.strip()
    if isinstance(value, str):
        return value if preserve else value.strip()
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        for key in ("input_value", "question", "raw_text", "message", "text"):
            text = data.get(key)
            if isinstance(text, str):
                return text if preserve else text.strip()
    return ""


def _session_id(source_value: Any, explicit_session_id: Any) -> str:
    explicit = _clean(explicit_session_id)
    if explicit:
        return explicit
    for attr in ("session_id", "sessionId"):
        candidate = getattr(source_value, attr, None)
        if _clean(candidate):
            return _clean(candidate)
    data = getattr(source_value, "data", source_value)
    if isinstance(data, dict):
        for key in ("session_id", "sessionId"):
            candidate = data.get(key)
            if _clean(candidate):
                return _clean(candidate)
        request = data.get("request")
        if isinstance(request, dict):
            candidate = request.get("session_id") or request.get("sessionId")
            if _clean(candidate):
                return _clean(candidate)
    if _input_text(source_value, preserve=False):
        return f"route_flow_{uuid.uuid4().hex}"
    return ""


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _resolve_api_url(value: Any) -> str:
    configured = _clean(value)
    if not configured:
        return ""
    if configured.lower().startswith(("http://", "https://")):
        return configured
    base_url = (
        _clean(os.getenv("LANGFLOW_BASE_URL", ""))
        or _clean(os.getenv("LANGFLOW_API_BASE_URL", ""))
        or DEFAULT_LANGFLOW_BASE_URL
    ).rstrip("/")
    if configured.startswith("/"):
        return f"{base_url}{configured}"
    if configured.startswith("api/"):
        return f"{base_url}/{configured}"
    return f"{base_url}/api/v1/run/{configured}"


def _secret_text(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return _clean(getter())
    return _clean(value)


def _safe_int(value: Any, default: int) -> int:
    try:
        return max(1, int(str(value or "").strip()))
    except Exception:
        return default


def _timeout_value(timeout_seconds: Any, connect_timeout_seconds: Any, read_timeout_seconds: Any) -> Any:
    if timeout_seconds not in (None, ""):
        return _safe_int(timeout_seconds, default=DEFAULT_READ_TIMEOUT_SECONDS)
    return (
        _safe_int(connect_timeout_seconds, default=DEFAULT_CONNECT_TIMEOUT_SECONDS),
        _safe_int(read_timeout_seconds, default=DEFAULT_READ_TIMEOUT_SECONDS),
    )


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


class FlowApiMessageCaller(Component):
    display_name = "01 선택 Flow API 메시지 호출기"
    description = "Smart Router가 선택한 branch의 원문 메시지를 하위 Langflow Run API로 전달하고, 하위 flow의 표시 메시지만 반환합니다."
    inputs = [
        MessageTextInput(name="flow_input", display_name="Flow 입력", required=True),
        MessageTextInput(
            name="api_url",
            display_name="하위 Flow endpoint/URL",
            value="",
            required=True,
            info="endpoint_name, /api/v1/run/... 경로 또는 전체 URL을 입력합니다. 상대값은 LANGFLOW_BASE_URL 기준으로 해석합니다.",
        ),
        SecretStrInput(name="api_key", display_name="Langflow API 키", value="", required=False, advanced=True),
        MessageTextInput(name="session_id", display_name="세션 ID", value="", required=False, advanced=True),
        MessageTextInput(name="route_name", display_name="Route 이름", value="", required=False, advanced=True),
        MessageTextInput(
            name="connect_timeout_seconds",
            display_name="연결 제한 시간(초)",
            value=str(DEFAULT_CONNECT_TIMEOUT_SECONDS),
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="read_timeout_seconds",
            display_name="응답 제한 시간(초)",
            value=str(DEFAULT_READ_TIMEOUT_SECONDS),
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(name="message", display_name="메시지", method="build_message", types=["Message"], group_outputs=True),
        Output(name="status_data", display_name="호출 상태", method="build_status", types=["Data"], group_outputs=True),
    ]

    def _run_once(self) -> dict[str, Any]:
        if not hasattr(self, "_cached_result"):
            session_id = getattr(self, "session_id", "")
            if not _clean(session_id):
                session_id = getattr(getattr(self, "graph", None), "session_id", "") or ""
            self._cached_result = run_flow_api_message(
                getattr(self, "flow_input", ""),
                api_url=getattr(self, "api_url", ""),
                api_key=getattr(self, "api_key", ""),
                session_id=session_id,
                connect_timeout_seconds=getattr(
                    self, "connect_timeout_seconds", str(DEFAULT_CONNECT_TIMEOUT_SECONDS)
                ),
                read_timeout_seconds=getattr(self, "read_timeout_seconds", str(DEFAULT_READ_TIMEOUT_SECONDS)),
                route_name=getattr(self, "route_name", ""),
            )
        return self._cached_result

    def build_message(self) -> Message:
        result = self._run_once()
        return Message(text=_clean(result.get("message")))

    def build_status(self) -> Data:
        result = self._run_once()
        return Data(
            data={
                "status": result.get("status"),
                "route_name": result.get("route_name"),
                "http_status": result.get("http_status"),
                "downstream_status": result.get("downstream_status"),
                "duration_ms": result.get("duration_ms"),
                "session_id": result.get("session_id"),
                "errors": result.get("errors", []),
                "warnings": result.get("warnings", []),
            }
        )
