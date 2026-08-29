"""PTMORE PKG Agent management portal.

The dashboard, employee, history, and scheduling screens remain a local
design preview.  Metadata authoring can instead call a separately configured
external Flow API; this portal never connects to CUBE or GAIA directly.
"""

from __future__ import annotations

import copy
import json
import os
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol
from urllib import error as url_error
from urllib import request as url_request
from urllib.parse import urlparse
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv


ROOT = Path(__file__).parent
STATIC_ROOT = ROOT / "static"

# Hosting platforms provide environment variables directly.  Loading `.env`
# here additionally makes the same configuration usable for a local preview;
# existing process-level values always take precedence.
load_dotenv(ROOT / ".env", override=False)


_METADATA_TYPES = ("table_catalog", "main_flow_filters", "domain")
_METADATA_TYPE_LABELS = {
    "table_catalog": "테이블 카탈로그",
    "main_flow_filters": "메인 플로우 필터",
    "domain": "도메인 정보",
}
_METADATA_COLLECTION_SUFFIXES = {
    "table_catalog": "table_catalog_items",
    "main_flow_filters": "main_flow_filters",
    "domain": "domain_items",
}

# Langflow applies tweaks by either node ID or display name.  We use stable,
# human-readable display names by default so a Flow re-import does not require
# configuration updates. The environment can override a key with an ID only
# when an operator intentionally renamed a node.
_DEFAULT_METADATA_COMPONENT_MAP = {
    "table_catalog": {
        "request_loader": "00 테이블 카탈로그 등록 요청 로더",
        "snapshot_loader": "01 메타데이터 QA 통합 Snapshot 로더",
        "writer": "07 테이블 카탈로그 검수/저장 처리기",
        "api_terminal": "10 테이블 카탈로그 등록 API 응답 생성기",
    },
    "main_flow_filters": {
        "request_loader": "00 메인 플로우 필터 등록 요청 로더",
        "snapshot_loader": "01 메타데이터 QA 통합 Snapshot 로더",
        "writer": "07 메인 플로우 필터 검수/저장 처리기",
        "api_terminal": "10 메인 플로우 필터 등록 API 응답 생성기",
    },
    "domain": {
        "request_loader": "00 도메인 등록 요청 로더",
        "snapshot_loader": "01 메타데이터 QA 통합 Snapshot 로더",
        "writer": "07 도메인 검수/저장 처리기",
        "api_terminal": "10 도메인 등록 API 응답 생성기",
    },
}


class MetadataAuthoringRequest(BaseModel):
    """The portal's stable request shape for all three metadata authoring Flows."""

    metadata_type: Literal["table_catalog", "main_flow_filters", "domain"]
    raw_text: str = Field(..., min_length=1, max_length=20_000)
    duplicate_action: Literal["skip", "merge", "replace", "create_new"] = "skip"
    dry_run: bool = True


class MetadataApiClient(Protocol):
    """Small HTTP boundary so tests never need a live metadata API."""

    def post_json(
        self,
        url: str,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
        verify_tls: bool,
    ) -> dict[str, Any]:
        ...


class MetadataApiCallError(RuntimeError):
    """A safe, endpoint-neutral error from the configured external Flow API."""

    def __init__(self, message: str, *, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status


class UrlLibMetadataApiClient:
    """POST JSON without introducing a runtime dependency beyond the standard library."""

    def post_json(
        self,
        url: str,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
        verify_tls: bool,
    ) -> dict[str, Any]:
        request = url_request.Request(
            url,
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        context = None if verify_tls else ssl._create_unverified_context()

        try:
            with url_request.urlopen(
                request,
                timeout=timeout_seconds,
                context=context,
            ) as response:
                body = response.read().decode("utf-8")
        except url_error.HTTPError as exc:
            raise MetadataApiCallError(
                "The configured metadata API returned an HTTP error.",
                upstream_status=exc.code,
            ) from exc
        except (url_error.URLError, TimeoutError, OSError) as exc:
            raise MetadataApiCallError("The configured metadata API could not be reached.") from exc

        try:
            decoded = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError as exc:
            raise MetadataApiCallError("The configured metadata API did not return JSON.") from exc
        if not isinstance(decoded, dict):
            raise MetadataApiCallError("The configured metadata API returned an unexpected JSON shape.")
        return decoded


@dataclass(frozen=True)
class MetadataAuthoringSettings:
    """Non-secret runtime settings for the metadata API adapter.

    Values are read for each request so a deployed process only needs a restart
    when its platform updates environment variables.  Sensitive values are
    deliberately never returned by the status endpoint.
    """

    mode: str
    api_url: str
    api_urls: Mapping[str, str]
    auth_header: str
    auth_key: str
    user_id_header: str
    user_id: str
    bearer_token: str
    extra_headers: Mapping[str, str]
    timeout_seconds: float
    verify_tls: bool
    payload_mode: str
    input_type: str
    output_type: str
    component_map: Mapping[str, Mapping[str, str]]
    mongo_uri: str
    mongo_database: str
    mongo_collection_prefix: str
    mongo_collections: Mapping[str, str]
    send_mongodb_tweaks: bool
    configuration_errors: tuple[str, ...]

    def endpoint_for(self, metadata_type: str) -> str:
        return str(self.api_urls.get(metadata_type) or self.api_url or "").strip()

    def collection_for(self, metadata_type: str) -> str:
        configured = str(self.mongo_collections.get(metadata_type) or "").strip()
        if configured:
            return configured
        suffix = _METADATA_COLLECTION_SUFFIXES[metadata_type]
        return f"{self.mongo_collection_prefix}{suffix}" if self.mongo_collection_prefix else suffix

    def component_for(self, metadata_type: str, name: str) -> str:
        component = self.component_map.get(metadata_type, {})
        return str(component.get(name) or "").strip()


def _environment_value(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _bool_from_environment(name: str, default: bool = True) -> bool:
    value = _environment_value(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _json_object_from_environment(
    name: str,
    *,
    default: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    raw = _environment_value(name)
    if not raw:
        return copy.deepcopy(dict(default or {})), []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return copy.deepcopy(dict(default or {})), [name]
    if not isinstance(parsed, Mapping):
        return copy.deepcopy(dict(default or {})), [name]
    return dict(parsed), []


def _metadata_settings_from_env() -> MetadataAuthoringSettings:
    """Read the API adapter configuration without ever exposing secret values."""

    errors: list[str] = []
    configured_mode = _environment_value("PTMORE_METADATA_API_MODE", "preview").lower()
    mode = {"mock": "preview", "preview": "preview", "api": "api"}.get(configured_mode, "invalid")
    if mode == "invalid":
        errors.append("PTMORE_METADATA_API_MODE")

    configured_payload_mode = _environment_value(
        "PTMORE_METADATA_API_PAYLOAD_MODE", "langflow"
    ).lower()
    payload_mode = configured_payload_mode if configured_payload_mode in {"langflow", "direct"} else "invalid"
    if payload_mode == "invalid":
        errors.append("PTMORE_METADATA_API_PAYLOAD_MODE")

    try:
        timeout_seconds = float(_environment_value("PTMORE_METADATA_API_TIMEOUT_SECONDS", "60"))
        if not 0 < timeout_seconds <= 600:
            raise ValueError
    except ValueError:
        timeout_seconds = 60.0
        errors.append("PTMORE_METADATA_API_TIMEOUT_SECONDS")

    extra_headers, header_errors = _json_object_from_environment(
        "PTMORE_METADATA_API_EXTRA_HEADERS_JSON"
    )
    errors.extend(header_errors)
    safe_extra_headers = {
        str(key): str(value)
        for key, value in extra_headers.items()
        if str(key).strip() and value is not None
    }

    configured_components, component_errors = _json_object_from_environment(
        "PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON",
        default=_DEFAULT_METADATA_COMPONENT_MAP,
    )
    errors.extend(component_errors)
    component_map = copy.deepcopy(_DEFAULT_METADATA_COMPONENT_MAP)
    for metadata_type, configured_value in configured_components.items():
        if metadata_type not in component_map or not isinstance(configured_value, Mapping):
            continue
        for component_name in ("request_loader", "snapshot_loader", "writer", "api_terminal"):
            value = configured_value.get(component_name)
            if value:
                component_map[metadata_type][component_name] = str(value)

    configured_collections, collection_errors = _json_object_from_environment(
        "PTMORE_METADATA_MONGODB_COLLECTION_MAP_JSON"
    )
    errors.extend(collection_errors)
    mongo_collections = {
        metadata_type: str(value)
        for metadata_type, value in configured_collections.items()
        if metadata_type in _METADATA_TYPES and value
    }

    return MetadataAuthoringSettings(
        mode=mode,
        api_url=_environment_value("PTMORE_METADATA_API_URL"),
        api_urls={
            "table_catalog": _environment_value("PTMORE_METADATA_TABLE_CATALOG_API_URL"),
            "main_flow_filters": _environment_value("PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL"),
            "domain": _environment_value("PTMORE_METADATA_DOMAIN_API_URL"),
        },
        auth_header=_environment_value("PTMORE_METADATA_API_AUTH_HEADER", "X-API-Key"),
        auth_key=_environment_value("PTMORE_METADATA_API_AUTH_KEY"),
        user_id_header=_environment_value("PTMORE_METADATA_API_USER_ID_HEADER", "X-Gaia-User-Id"),
        user_id=_environment_value("PTMORE_METADATA_API_USER_ID"),
        bearer_token=_environment_value("PTMORE_METADATA_API_BEARER_TOKEN"),
        extra_headers=safe_extra_headers,
        timeout_seconds=timeout_seconds,
        verify_tls=_bool_from_environment("PTMORE_METADATA_API_VERIFY_TLS", True),
        payload_mode=payload_mode,
        input_type=_environment_value("PTMORE_METADATA_API_INPUT_TYPE", "chat"),
        # `any` lets Langflow return the `10 ... API 응답 생성기` terminal
        # alongside Chat Output.  A chat-only result does not expose the
        # structured metadata authoring envelope needed by this portal.
        output_type=_environment_value("PTMORE_METADATA_API_OUTPUT_TYPE", "any"),
        component_map=component_map,
        mongo_uri=_environment_value("MONGODB_URI"),
        mongo_database=_environment_value("MONGODB_DATABASE"),
        mongo_collection_prefix=_environment_value("MONGODB_COLLECTION_PREFIX", "agent_v4_"),
        mongo_collections=mongo_collections,
        send_mongodb_tweaks=_bool_from_environment("PTMORE_METADATA_SEND_MONGODB_TWEAKS", False),
        configuration_errors=tuple(dict.fromkeys(errors)),
    )


def _metadata_api_headers(settings: MetadataAuthoringSettings) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        **settings.extra_headers,
    }
    if settings.auth_key and settings.auth_header:
        headers.setdefault(settings.auth_header, settings.auth_key)
    if settings.user_id and settings.user_id_header:
        headers.setdefault(settings.user_id_header, settings.user_id)
    if settings.bearer_token:
        headers.setdefault("Authorization", f"Bearer {settings.bearer_token}")
    return headers


def _metadata_api_payload(
    settings: MetadataAuthoringSettings,
    *,
    metadata_type: str,
    raw_text: str,
    duplicate_action: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Build either the standard Langflow Run API body or a generic JSON body."""

    request_loader = settings.component_for(metadata_type, "request_loader")
    snapshot_loader = settings.component_for(metadata_type, "snapshot_loader")
    writer = settings.component_for(metadata_type, "writer")
    request_tweak = {
        "duplicate_action": duplicate_action,
        "dry_run": dry_run,
    }
    mongo_tweaks = {
        "mongo_uri": settings.mongo_uri,
        "mongo_database": settings.mongo_database,
        "table_collection_name": settings.collection_for("table_catalog"),
        "filter_collection_name": settings.collection_for("main_flow_filters"),
        "domain_collection_name": settings.collection_for("domain"),
    }
    mongo_tweaks = {key: value for key, value in mongo_tweaks.items() if value}
    writer_tweak = {
        key: value
        for key, value in {
            "mongo_uri": settings.mongo_uri,
            "mongo_database": settings.mongo_database,
            "collection_name": settings.collection_for(metadata_type),
        }.items()
        if value
    }

    if settings.payload_mode == "direct":
        return {
            "metadata_type": metadata_type,
            "raw_text": raw_text,
            "duplicate_action": duplicate_action,
            "dry_run": dry_run,
            "mongodb": {
                "database": settings.mongo_database,
                "collection_name": settings.collection_for(metadata_type),
            },
        }

    # Default: Langflow's /api/v1/run/<flow-id> request shape.  The top-level
    # input_value drives Chat Input and its connected `raw_text` input.  Tweaks
    # supplement the request loader and writer; Langflow applies them by node
    # ID even when the fields are not displayed as API-editable in the canvas.
    tweaks: dict[str, dict[str, Any]] = {}
    if request_loader:
        tweaks[request_loader] = request_tweak
    if settings.send_mongodb_tweaks and snapshot_loader and mongo_tweaks:
        tweaks[snapshot_loader] = mongo_tweaks
    if settings.send_mongodb_tweaks and writer and writer_tweak:
        tweaks[writer] = writer_tweak
    return {
        "input_value": raw_text,
        "input_type": settings.input_type,
        "output_type": settings.output_type,
        "tweaks": tweaks,
    }


def _is_metadata_authoring_response(value: Any) -> bool:
    return isinstance(value, Mapping) and (
        value.get("response_type") == "metadata_authoring"
        or ("metadata_authoring" in value and "write_result" in value)
    )


def _find_metadata_authoring_response(value: Any, *, depth: int = 0) -> dict[str, Any] | None:
    """Extract the API builder result from direct, wrapped, or Langflow output."""

    if depth > 12:
        return None
    if _is_metadata_authoring_response(value):
        return dict(value)
    if isinstance(value, Mapping):
        api_response = value.get("api_response")
        found = _find_metadata_authoring_response(api_response, depth=depth + 1)
        if found:
            return found
        # Prefer common Langflow wrapper fields before a generic recursive walk.
        for key in ("results", "outputs", "data", "output", "message", "value"):
            if key in value:
                found = _find_metadata_authoring_response(value[key], depth=depth + 1)
                if found:
                    return found
        for nested_value in value.values():
            found = _find_metadata_authoring_response(nested_value, depth=depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_metadata_authoring_response(item, depth=depth + 1)
            if found:
                return found
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                return _find_metadata_authoring_response(json.loads(text), depth=depth + 1)
            except json.JSONDecodeError:
                return None
    return None


def _find_api_terminal_response(
    value: Any,
    *,
    api_terminal: str,
    depth: int = 0,
) -> dict[str, Any] | None:
    """Prefer the configured `10 ... API 응답 생성기` over Chat Output text."""

    if not api_terminal or depth > 12:
        return None
    if isinstance(value, Mapping):
        component_id = str(value.get("component_id") or "")
        component_display_name = str(value.get("component_display_name") or "")
        if api_terminal in {component_id, component_display_name}:
            results = value.get("results")
            if isinstance(results, Mapping):
                # Langflow serializes the Data port as
                # results.api_response.data.  The second lookup supports a
                # gateway that already unwraps the Data object.
                api_response = results.get("api_response")
                found = _find_metadata_authoring_response(api_response, depth=depth + 1)
                if found:
                    return found
        for nested_value in value.values():
            found = _find_api_terminal_response(
                nested_value,
                api_terminal=api_terminal,
                depth=depth + 1,
            )
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_api_terminal_response(item, api_terminal=api_terminal, depth=depth + 1)
            if found:
                return found
    return None


def _normalize_metadata_authoring_response(
    api_response: Mapping[str, Any],
    *,
    metadata_type: str,
    raw_text: str,
) -> dict[str, Any]:
    """Add only missing UI-contract fields; keep the Flow's API result intact."""

    result = copy.deepcopy(dict(api_response))
    metadata_authoring = result.get("metadata_authoring")
    if not isinstance(metadata_authoring, Mapping):
        metadata_authoring = {}
    else:
        metadata_authoring = dict(metadata_authoring)

    write_result = result.get("write_result")
    if not isinstance(write_result, Mapping):
        write_result = {}
    else:
        write_result = dict(write_result)

    data = result.get("data")
    if not isinstance(data, Mapping):
        data = {}
    else:
        data = dict(data)
    rows = data.get("rows")
    if not isinstance(rows, list):
        rows = []
    columns = data.get("columns")
    if not isinstance(columns, list):
        columns = []
    data["rows"] = rows
    data["columns"] = columns
    data.setdefault("row_count", len(rows))

    flow_status = str(result.get("status") or write_result.get("status") or "completed")
    result.setdefault("response_type", "metadata_authoring")
    result.setdefault("metadata_type", metadata_type)
    result.setdefault("metadata_label", _METADATA_TYPE_LABELS[metadata_type])
    result.setdefault("status", flow_status)
    result.setdefault("success", bool(write_result.get("success", True)))
    result.setdefault("direct_response_ready", True)
    result.setdefault("message", "메타데이터 등록 Flow 응답을 받았습니다.")
    result.setdefault("answer_sections", {})
    metadata_authoring.setdefault("original_text", raw_text)
    metadata_authoring.setdefault("metadata_type", result["metadata_type"])
    metadata_authoring.setdefault("metadata_label", result["metadata_label"])
    metadata_authoring.setdefault("status", result["status"])
    write_result.setdefault("status", result["status"])
    result["data"] = data
    result["metadata_authoring"] = metadata_authoring
    result["write_result"] = write_result
    result.setdefault("trace", {})
    return result


def _metadata_preview_response(
    *,
    metadata_type: str,
    raw_text: str,
    duplicate_action: str,
    requested_dry_run: bool,
) -> dict[str, Any]:
    """Use sample data only when preview/mock mode was explicitly selected."""

    preview = _metadata_authoring_preview()["examples"][metadata_type]["result"]
    response = copy.deepcopy(preview)
    response["message"] = "미리보기 모드입니다. 외부 API와 MongoDB에는 요청하지 않았습니다."
    response["metadata_authoring"]["original_text"] = raw_text
    response["metadata_authoring"]["duplicate_action"] = duplicate_action
    response["metadata_authoring"]["requested_dry_run"] = requested_dry_run
    response["trace"]["mode"] = "preview"
    return response


def _metadata_api_status(settings: MetadataAuthoringSettings) -> dict[str, Any]:
    endpoint_configured = {
        metadata_type: bool(settings.endpoint_for(metadata_type))
        for metadata_type in _METADATA_TYPES
    }
    missing: list[str] = list(settings.configuration_errors)
    if settings.mode == "api":
        missing.extend(
            f"endpoint:{metadata_type}"
            for metadata_type, configured in endpoint_configured.items()
            if not configured
        )
        if settings.send_mongodb_tweaks:
            if not settings.mongo_uri:
                missing.append("MONGODB_URI")
            if not settings.mongo_database:
                missing.append("MONGODB_DATABASE")
    mongo_tweaks_ready = (
        not settings.send_mongodb_tweaks
        or bool(settings.mongo_uri and settings.mongo_database)
    )
    base_api_configuration_ready = not settings.configuration_errors and mongo_tweaks_ready
    any_endpoint_configured = any(endpoint_configured.values())
    ready = (settings.mode == "preview" and not settings.configuration_errors) or (
        settings.mode == "api" and base_api_configuration_ready and any_endpoint_configured
    )
    all_metadata_types_ready = (settings.mode == "preview" and not settings.configuration_errors) or (
        settings.mode == "api" and base_api_configuration_ready and all(endpoint_configured.values())
    )
    return {
        "mode": settings.mode,
        "configured": ready,
        "ready": ready,
        "all_metadata_types_ready": all_metadata_types_ready,
        "preview_only": settings.mode == "preview",
        "missing": list(dict.fromkeys(missing)),
        "api": {
            "endpoint_configured": endpoint_configured,
            "auth_key_configured": bool(settings.auth_key),
            "user_id_configured": bool(settings.user_id),
            "bearer_token_configured": bool(settings.bearer_token),
            "timeout_seconds": settings.timeout_seconds,
            "verify_tls": settings.verify_tls,
            "payload_mode": settings.payload_mode,
            "component_map_configured": {
                metadata_type: bool(settings.component_for(metadata_type, "request_loader"))
                for metadata_type in _METADATA_TYPES
            },
            "api_terminal_configured": {
                metadata_type: bool(settings.component_for(metadata_type, "api_terminal"))
                for metadata_type in _METADATA_TYPES
            },
        },
        "mongodb": {
            "uri_configured": bool(settings.mongo_uri),
            "database": settings.mongo_database or None,
            "collection_prefix": settings.mongo_collection_prefix or None,
            "tweaks_enabled": settings.send_mongodb_tweaks,
            "writer_tweaks_configured": bool(
                settings.send_mongodb_tweaks and settings.mongo_uri and settings.mongo_database
            ),
            "collections": {
                metadata_type: settings.collection_for(metadata_type)
                for metadata_type in _METADATA_TYPES
            },
        },
    }


_metadata_http_client: MetadataApiClient = UrlLibMetadataApiClient()

application = FastAPI(
    title="PTMORE PKG Agent Portal",
    description="Portal preview with an optional external metadata authoring API adapter.",
    version="0.2.0-preview",
)
application.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


_USAGE_USERS = [
    ("2069026", "문봉건"),
    ("2071044", "김민서"),
    ("2093012", "이도윤"),
    ("2084501", "박서윤"),
    ("2057802", "최현우"),
    ("2089017", "정하린"),
    ("2076603", "윤태호"),
    ("2098130", "한지민"),
    ("2041218", "김도윤"),
    ("2060049", "오수빈"),
    ("2072206", "배지훈"),
    ("2081442", "서유진"),
    ("2059211", "임성호"),
    ("2090842", "장예린"),
    ("2075400", "류민재"),
    ("2068804", "신예은"),
    ("2083771", "권도형"),
    ("2056173", "조아라"),
]

_USAGE_QUESTIONS = [
    "오늘 DA 공정의 생산량과 목표 대비 달성률을 알려줘.",
    "현재 WIP 체류 시간이 긴 LOT을 공정별로 보여줘.",
    "설비 DOWN 현황과 예상 복구 시간을 정리해줘.",
    "당일 수율 변동이 큰 제품과 원인을 분석해줘.",
    "PKG 공정의 생산 현황을 요약해줘.",
    "HOLD 상태 LOT의 주요 사유를 알려줘.",
]

_CHANNELS = ("CUBE", "CUBE", "CUBE", "CUBE_SCHEDULING", "ADMIN_TEST")


def _build_dummy_usage_history() -> list[dict[str, str]]:
    """Create deterministic 21-day records with employee, question, and date fields."""

    end_day = date(2026, 8, 29)
    core_users = _USAGE_USERS[:8]
    occasional_users = _USAGE_USERS[8:]
    history: list[dict[str, str]] = []
    record_number = 0

    for day_index in range(21):
        usage_day = end_day - timedelta(days=20 - day_index)
        # Core users are repeatedly active; the remaining users are occasional.
        selected_users = [
            core_users[(day_index + offset) % len(core_users)]
            for offset in range(6 + (day_index % 3))
        ]
        selected_users.extend(
            occasional_users[(day_index * 2 + offset) % len(occasional_users)]
            for offset in range(2 + (day_index % 3))
        )

        for user_index, (employee_id, user_name) in enumerate(selected_users):
            # One question is always present; a few records model repeat questions.
            repeat_count = 1 + int((day_index + user_index) % 3 == 0)
            if user_index < 3 and day_index % 2 == 0:
                repeat_count += 1

            for repeat_index in range(repeat_count):
                record_number += 1
                hour = 8 + ((user_index + repeat_index) % 9)
                minute = (10 + day_index * 3 + repeat_index * 17) % 60
                history.append(
                    {
                        "id": f"USE-2026-{record_number:04d}",
                        "employee_id": employee_id,
                        "user_name": user_name,
                        "question": _USAGE_QUESTIONS[
                            (day_index + user_index + repeat_index) % len(_USAGE_QUESTIONS)
                        ],
                        "date": usage_day.isoformat(),
                        "occurred_at": f"{usage_day.isoformat()}T{hour:02d}:{minute:02d}:00+09:00",
                        "channel": _CHANNELS[(day_index + user_index + repeat_index) % len(_CHANNELS)],
                    }
                )
    return history


def _channel_label(channel: str) -> str:
    return {
        "CUBE": "CUBE 직접 질의",
        "CUBE_SCHEDULING": "정기 스케줄",
        "ADMIN_TEST": "관리자 테스트",
    }.get(channel, channel)


def _build_usage_dashboard(
    usage_history: list[dict[str, str]], usage_policy: dict[str, int]
) -> dict[str, Any]:
    """Aggregate the exact user/question/date history used by the preview UI."""

    day_records: dict[str, list[dict[str, str]]] = {}
    user_activity: dict[str, dict[str, Any]] = {}
    channel_counts: dict[str, int] = {
        "CUBE": 0,
        "CUBE_SCHEDULING": 0,
        "ADMIN_TEST": 0,
    }

    for record in usage_history:
        day_records.setdefault(record["date"], []).append(record)
        activity = user_activity.setdefault(
            record["employee_id"],
            {
                "employee_id": record["employee_id"],
                "user_name": record["user_name"],
                "distinct_dates": set(),
                "chat_count": 0,
            },
        )
        activity["distinct_dates"].add(record["date"])
        activity["chat_count"] += 1
        channel_counts[record["channel"]] = channel_counts.get(record["channel"], 0) + 1

    usage_by_day = []
    for usage_date in sorted(day_records):
        records = day_records[usage_date]
        usage_by_day.append(
            {
                "date": usage_date,
                "label": f"{int(usage_date[5:7])}/{int(usage_date[8:])}",
                "unique_users": len({record["employee_id"] for record in records}),
                "chat_count": len(records),
            }
        )

    day_count = len(usage_by_day)
    total_chats = len(usage_history)
    cumulative_users = len(user_activity)
    min_distinct_days = int(usage_policy["active_user_min_distinct_days"])
    min_chat_count = int(usage_policy["active_user_min_chat_count"])
    active_users = [
        {
            "employee_id": activity["employee_id"],
            "user_name": activity["user_name"],
            "distinct_days": len(activity["distinct_dates"]),
            "chat_count": activity["chat_count"],
        }
        for activity in user_activity.values()
        if len(activity["distinct_dates"]) >= min_distinct_days
        and activity["chat_count"] >= min_chat_count
    ]
    active_users.sort(key=lambda item: (-item["chat_count"], -item["distinct_days"], item["employee_id"]))

    max_users = max((item["unique_users"] for item in usage_by_day), default=1)
    max_chats = max((item["chat_count"] for item in usage_by_day), default=1)
    for item in usage_by_day:
        item["user_height"] = round(item["unique_users"] / max_users * 100, 1)
        item["chat_height"] = round(item["chat_count"] / max_chats * 100, 1)

    mix_order = ("CUBE", "CUBE_SCHEDULING", "ADMIN_TEST")
    mix_colors = ("#2563eb", "#14b8a6", "#f59e0b")
    channel_mix = []
    remaining = 100
    for index, (channel, color) in enumerate(zip(mix_order, mix_colors)):
        if index == len(mix_order) - 1:
            percent = remaining
        else:
            percent = round(channel_counts[channel] / total_chats * 100) if total_chats else 0
            remaining -= percent
        channel_mix.append({"label": _channel_label(channel), "value": percent, "color": color})

    first_date = usage_by_day[0]["date"].replace("-", ".") if usage_by_day else "-"
    last_date = usage_by_day[-1]["date"].replace("-", ".") if usage_by_day else "-"
    average_daily_users = sum(item["unique_users"] for item in usage_by_day) / day_count if day_count else 0
    average_daily_chats = total_chats / day_count if day_count else 0

    return {
        "period_label": f"최근 {day_count}일",
        "range_label": f"{first_date} ~ {last_date}",
        "day_count": day_count,
        "cumulative_user_count": cumulative_users,
        "total_chat_count": total_chats,
        "kpis": [
            {
                "label": "일 평균 사용자",
                "value": f"{average_daily_users:.1f}명",
                "change": "일별 고유 사번",
                "tone": "accent",
                "detail": f"최근 {day_count}일 기준",
            },
            {
                "label": "일 평균 채팅",
                "value": f"{average_daily_chats:.1f}건",
                "change": "질문 입력 기준",
                "tone": "positive",
                "detail": f"최근 {day_count}일 기준",
            },
            {
                "label": "누적 사용자",
                "value": f"{cumulative_users}명",
                "change": "기간 내 고유 사번",
                "tone": "accent",
                "detail": "중복 사용자 제외",
            },
            {
                "label": "누적 채팅",
                "value": f"{total_chats:,}건",
                "change": "사용자 질문 기준",
                "tone": "positive",
                "detail": "스케줄 질문 포함",
            },
            {
                "label": "활성 사용자",
                "value": f"{len(active_users)}명",
                "change": "활성 기준 충족",
                "tone": "positive",
                "detail": f"{min_distinct_days}일 이상 · {min_chat_count}건 이상",
            },
        ],
        "usage_by_day": usage_by_day,
        "channel_mix": channel_mix,
        "active_users": active_users[:5],
        "active_user_count": len(active_users),
        "active_user_rule": {
            "min_distinct_days": min_distinct_days,
            "min_chat_count": min_chat_count,
        },
        "recent_usage_history": sorted(
            usage_history, key=lambda item: item["occurred_at"], reverse=True
        )[:8],
        "recent_runs": [
            {
                "time": "09:30",
                "name": "DA 공정 오전 생산 현황",
                "owner": "2069026",
                "status": "성공",
                "target": "개인 DM",
            },
            {
                "time": "09:15",
                "name": "WIP 이상 LOT 알림",
                "owner": "2071044",
                "status": "성공",
                "target": "PKG 생산관리 채널",
            },
            {
                "time": "09:00",
                "name": "설비 DOWN 현황",
                "owner": "2093012",
                "status": "재시도 예정",
                "target": "개인 DM",
            },
            {
                "time": "08:30",
                "name": "일일 수율 요약",
                "owner": "2069026",
                "status": "성공",
                "target": "PKG 품질 채널",
            },
        ],
    }


def _authoring_preview_response(
    *,
    metadata_type: str,
    metadata_label: str,
    raw_text: str,
    refined_text: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    keys: list[str],
    database: str,
    collection_name: str,
    resolved_references: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return a safe, representative `10 ... API 응답 생성기` result.

    The portal is intentionally a dummy preview.  Its result object mirrors the
    stable fields already returned by the three rev_2 metadata authoring Flows
    so the UI can be connected to a real Flow response without redesigning the
    screen later.
    """

    operation_by_key = [{"key": key, "operation": "inserted"} for key in keys]
    return {
        "response_type": "metadata_authoring",
        "metadata_type": metadata_type,
        "metadata_label": metadata_label,
        "status": "dry_run",
        "success": True,
        "direct_response_ready": True,
        "message": (
            f"{metadata_label} 메타데이터 {len(keys)}건을 저장 전 검토했습니다. "
            "현재는 테스트 실행 결과이므로 MongoDB에는 반영하지 않았습니다."
        ),
        "answer_sections": {
            "summary": {
                "headline": f"{metadata_label} 등록 후보를 검토했습니다.",
                "description": "실제 저장 전에 후보, 계약 검증, 중복 처리 계획을 확인합니다.",
            },
            "key_points": [
                f"생성된 등록 후보는 {len(rows)}건입니다.",
                "테스트 실행 모드라 실제 MongoDB 저장은 수행하지 않았습니다.",
            ],
            "notices": [],
            "next_steps": [
                "후보와 중복 처리 계획을 확인합니다.",
                "실제 운영에서는 테스트 실행을 해제한 뒤 같은 등록 Flow를 다시 실행합니다.",
            ],
        },
        "data": {"columns": columns, "rows": rows, "row_count": len(rows)},
        "metadata_authoring": {
            "contract_version": "metadata_authoring.rev_2.v1",
            "metadata_type": metadata_type,
            "metadata_label": metadata_label,
            "status": "dry_run",
            "generated_count": len(rows),
            "saved_count": 0,
            "would_save_count": len(keys),
            "existing_match_count": 0,
            "dry_run": True,
            "keys": keys,
            "original_text": raw_text,
            "refined_text": refined_text,
            "resolved_references": resolved_references or [],
            "unresolved_references": [],
            "missing_information": [],
            "assumptions": [],
            "retry_example": "",
            "retry_examples": [],
            "contract_validation": {
                "status": "validated",
                "errors": [],
                "warnings": [],
            },
        },
        "write_result": {
            "success": True,
            "ready_to_save": True,
            "dry_run": True,
            "saved_count": 0,
            "would_save_count": len(keys),
            "skipped_count": 0,
            "database": database,
            "collection_name": collection_name,
            "keys": keys,
            "operation_by_key": operation_by_key,
            "status": "dry_run",
            "message": "테스트 실행입니다. MongoDB에는 저장하지 않았습니다.",
            "errors": [],
        },
        "trace": {
            "raw_text_preview": raw_text[:500],
            "write_status": "dry_run",
            "errors": [],
        },
    }


def _metadata_authoring_preview() -> dict[str, Any]:
    """Build examples from the input/output contract of the rev_2 saving Flows."""

    table_raw_text = (
        "dataset_key는 production_weekly 입니다. 표시명은 Weekly Production Summary, "
        "분류는 생산입니다. Oracle PNT_RPT의 주간 생산 실적 조회 결과를 사용하며, "
        "필수 조건은 DATE와 PROCESS_GROUP입니다. DATE는 WORK_DT, PROCESS_GROUP은 "
        "OPER_NAME에 매핑합니다. 생산 수량 컬럼은 PROD_QTY이며 기본 집계는 sum입니다."
    )
    filter_raw_text = (
        "filter_key는 PROCESS_GROUP 입니다. 표시명은 공정 그룹이고, 사용자가 DA, "
        "WB, SG라고 입력할 때 사용합니다. 값 형식은 string, 값 형태는 scalar, "
        "기본 연산자는 eq이며 후보 표준 컬럼은 OPER_NAME입니다."
    )
    domain_raw_text = (
        "section은 process_groups, key는 DA 입니다. 표시명은 DA 공정 그룹이며, "
        "aliases는 DA와 Die Attach입니다. field는 OPER_NAME이고 processes는 "
        "DA1, DA2, DA3입니다. DA 공정 또는 DA 생산량 질문을 해석할 때 사용합니다."
    )

    table_response = _authoring_preview_response(
        metadata_type="table_catalog",
        metadata_label="테이블 카탈로그",
        raw_text=table_raw_text,
        refined_text=(
            "dataset_key=production_weekly인 생산 데이터셋을 등록한다. Oracle PNT_RPT의 "
            "주간 생산 실적을 사용하고 DATE→WORK_DT, PROCESS_GROUP→OPER_NAME을 매핑한다."
        ),
        columns=["데이터셋 키", "데이터셋", "분류", "연결 방식", "필수 조건", "상태"],
        rows=[
            {
                "데이터셋 키": "production_weekly",
                "데이터셋": "Weekly Production Summary",
                "분류": "생산",
                "연결 방식": "Oracle",
                "필수 조건": "DATE, PROCESS_GROUP",
                "상태": "저장 예정",
            }
        ],
        keys=["production_weekly"],
        database="datagov",
        collection_name="agent_v4_table_catalog_items",
        resolved_references=[
            {"kind": "canonical_column", "input": "DATE", "target": "DATE", "evidence": "declared"},
            {"kind": "canonical_column", "input": "PROCESS_GROUP", "target": "PROCESS_GROUP", "evidence": "declared"},
        ],
    )
    filter_response = _authoring_preview_response(
        metadata_type="main_flow_filter",
        metadata_label="메인 플로우 필터",
        raw_text=filter_raw_text,
        refined_text=(
            "filter_key=PROCESS_GROUP 표준 Filter를 등록한다. 공정 그룹 질문에 사용하며 "
            "aliases는 DA, WB, SG, operator는 eq, value_type은 string, value_shape은 scalar이다."
        ),
        columns=["필터 키", "표시명", "연산자", "값 타입", "값 형태", "상태"],
        rows=[
            {
                "필터 키": "PROCESS_GROUP",
                "표시명": "공정 그룹",
                "연산자": "eq",
                "값 타입": "string",
                "값 형태": "scalar",
                "상태": "저장 예정",
            }
        ],
        keys=["PROCESS_GROUP"],
        database="datagov",
        collection_name="agent_v4_main_flow_filters",
    )
    domain_response = _authoring_preview_response(
        metadata_type="domain",
        metadata_label="도메인 정보",
        raw_text=domain_raw_text,
        refined_text=(
            "process_groups section에 key=DA 도메인을 등록한다. field=OPER_NAME에 DA1, DA2, "
            "DA3을 적용하고 aliases는 DA, Die Attach로 유지한다."
        ),
        columns=["구분", "키", "표시명", "상태", "처리"],
        rows=[
            {
                "구분": "process_groups",
                "키": "DA",
                "표시명": "DA 공정 그룹",
                "상태": "저장 예정",
                "처리": "inserted",
            }
        ],
        keys=["process_groups:DA"],
        database="datagov",
        collection_name="agent_v4_domain_items",
    )

    examples = {
        "table_catalog": {
            "flow_label": "테이블 카탈로그 저장 Flow",
            "endpoint_name": "metadata-driven-v5-table-catalog-saving-rev-2",
            "chat_input_id": "ChatInput-table_catalog-rev-2",
            "request_loader_id": "Request-table_catalog-rev-2",
            "required_input": ["dataset_key", "연결 소스", "필수 조건", "필터·컬럼 매핑"],
            "raw_text": table_raw_text,
            "duplicate_action": "skip",
            "dry_run": True,
            "result": table_response,
        },
        "main_flow_filters": {
            "flow_label": "메인 플로우 필터 저장 Flow",
            "endpoint_name": "metadata-driven-v5-main-flow-filter-saving-rev-2",
            "chat_input_id": "ChatInput-main_flow_filter-rev-2",
            "request_loader_id": "Request-main_flow_filter-rev-2",
            "required_input": ["filter_key", "사용자 표현", "operator", "value_type·value_shape", "후보 컬럼"],
            "raw_text": filter_raw_text,
            "duplicate_action": "skip",
            "dry_run": True,
            "result": filter_response,
        },
        "domain": {
            "flow_label": "도메인 저장 Flow",
            "endpoint_name": "metadata-driven-v5-domain-saving-rev-2",
            "chat_input_id": "ChatInput-domain-rev-2",
            "request_loader_id": "Request-domain-rev-2",
            "required_input": ["section", "key", "업무 표현·별칭", "적용 규칙 또는 설명"],
            "raw_text": domain_raw_text,
            "duplicate_action": "skip",
            "dry_run": True,
            "result": domain_response,
        },
    }

    return {
        "contract": {
            "version": "metadata_authoring.rev_2.v1",
            "request": {
                "chat_input": "input_value",
                "request_loader": ["raw_text", "duplicate_action", "dry_run"],
                "defaults": {"duplicate_action": "skip", "dry_run": True},
            },
            "response": [
                "status",
                "data.columns/data.rows",
                "metadata_authoring",
                "write_result",
                "trace",
            ],
        },
        "examples": examples,
        "recent_results": [
            {
                "id": "RUN-META-042",
                "metadata_type": "table_catalog",
                "requested_at": "오늘 10:22",
                "requested_by": "문봉건 (2069026)",
                "result": table_response,
            },
            {
                "id": "RUN-META-041",
                "metadata_type": "main_flow_filters",
                "requested_at": "어제 16:40",
                "requested_by": "최은서 (2079411)",
                "result": filter_response,
            },
        ],
    }


def _portal_data(preview_role: str = "admin") -> dict[str, Any]:
    """Return fresh dummy data so browser-only edits never become server state."""

    is_standard_preview = str(preview_role or "").strip().lower() in {"user", "member", "standard"}
    usage_policy = {
        "history_window_days": 21,
        "active_user_min_distinct_days": 3,
        "active_user_min_chat_count": 10,
    }
    usage_history = _build_dummy_usage_history()
    viewer = (
        {
            "employee_id": "2071044",
            "name": "김민서",
            "role": "일반 사용자",
            "is_admin": False,
        }
        if is_standard_preview
        else {
            "employee_id": "2069026",
            "name": "문봉건",
            "role": "관리자",
            "is_admin": True,
        }
    )

    return {
        "viewer": viewer,
        "dashboard": _build_usage_dashboard(usage_history, usage_policy),
        "usage_history": usage_history,
        "schedules": [
            {
                "id": "SCH-2026-081",
                "title": "DA 공정 오전 생산 현황",
                "question": "오늘 DA 공정의 생산량, 목표 대비 달성률, 주요 이슈를 요약해줘.",
                "repeat": "평일",
                "time": "09:30",
                "rule_label": "평일 · 오전 09:30",
                "next_run": "오늘 09:30",
                "target": "개인 DM",
                "owner": "2069026",
                "status": "활성",
                "last_run": "오늘 09:30 · 성공",
            },
            {
                "id": "SCH-2026-064",
                "title": "WIP 이상 LOT 알림",
                "question": "현재 WIP 체류 시간이 기준을 초과한 LOT을 공정별로 알려줘.",
                "repeat": "매일",
                "time": "10:00",
                "rule_label": "매일 · 오전 10:00, 오후 16:00",
                "next_run": "오늘 16:00",
                "target": "PKG 생산관리 채널",
                "owner": "2069026",
                "status": "활성",
                "last_run": "오늘 10:00 · 성공",
            },
            {
                "id": "SCH-2026-042",
                "title": "주간 품질 리포트",
                "question": "이번 주 주요 수율 지표와 전주 대비 변동 원인을 정리해줘.",
                "repeat": "매주",
                "time": "08:30",
                "rule_label": "매주 월요일 · 오전 08:30",
                "next_run": "9월 1일 08:30",
                "target": "PKG 품질 채널",
                "owner": "2071044",
                "status": "일시중지",
                "last_run": "8월 25일 · 성공",
            },
            {
                "id": "SCH-2026-029",
                "title": "설비 DOWN 현황",
                "question": "현재 DOWN 상태인 설비와 예상 복구 시간을 알려줘.",
                "repeat": "매일",
                "time": "08:00",
                "rule_label": "매일 · 오전 08:00",
                "next_run": "내일 08:00",
                "target": "개인 DM",
                "owner": "2069026",
                "status": "활성",
                "last_run": "오늘 08:00 · 성공",
            },
        ],
        "metadata": {
            "table_catalog": [
                {
                    "id": "CAT-014",
                    "dataset_key": "production_today",
                    "display_name": "Production Today",
                    "dataset_family": "생산",
                    "source_type": "Oracle",
                    "source_name": "PNT_RPT.PKG_PRODUCTION_TODAY",
                    "required_filters": ["DATE", "PROCESS_GROUP"],
                    "column_count": 12,
                    "owner": "PKG 생산기술",
                    "updated_at": "2026.08.27",
                    "status": "활성",
                    "description": "당일 공정·제품별 생산량과 목표 대비 실적을 조회합니다.",
                },
                {
                    "id": "CAT-023",
                    "dataset_key": "wip_today",
                    "display_name": "WIP Today",
                    "dataset_family": "재공",
                    "source_type": "Oracle",
                    "source_name": "PNT_RPT.PKG_WIP_TODAY",
                    "required_filters": ["DATE"],
                    "column_count": 16,
                    "owner": "PKG 생산관리",
                    "updated_at": "2026.08.26",
                    "status": "활성",
                    "description": "현재 재공 수량과 공정별 체류 현황을 조회합니다.",
                },
                {
                    "id": "CAT-031",
                    "dataset_key": "eqp_down_list",
                    "display_name": "Equipment Down List",
                    "dataset_family": "설비",
                    "source_type": "Oracle",
                    "source_name": "GMS_DB.EQP_DOWN_LIST",
                    "required_filters": [],
                    "column_count": 10,
                    "owner": "PKG 장비기술",
                    "updated_at": "2026.08.24",
                    "status": "검토 필요",
                    "description": "DOWN 설비, 원인 코드, 담당 조직을 조회합니다.",
                },
                {
                    "id": "CAT-044",
                    "dataset_key": "target",
                    "display_name": "PKG Target Goodocs Plan",
                    "dataset_family": "계획",
                    "source_type": "Goodocs",
                    "source_name": "Goodocs.PKG_TARGET_PLAN",
                    "required_filters": [],
                    "column_count": 8,
                    "owner": "PKG 전략",
                    "updated_at": "2026.08.21",
                    "status": "활성",
                    "description": "월간 목표와 주요 실행 계획을 확인합니다.",
                },
            ],
            "main_flow_filters": [
                {
                    "id": "FLT-001",
                    "filter_key": "DATE",
                    "display_name": "기준 일자",
                    "semantic_role": "reference_date",
                    "value_type": "date",
                    "column_candidates": ["WORK_DT", "WORK_DATE"],
                    "aliases": ["오늘", "금일", "작업일"],
                    "owner": "PKG 데이터운영",
                    "updated_at": "2026.08.27",
                    "status": "활성",
                },
                {
                    "id": "FLT-004",
                    "filter_key": "PROCESS_GROUP",
                    "display_name": "공정 그룹",
                    "semantic_role": "process_group",
                    "value_type": "string",
                    "column_candidates": ["OPER_NAME", "OPER_NM"],
                    "aliases": ["DA", "WB", "SG"],
                    "owner": "PKG 생산기술",
                    "updated_at": "2026.08.25",
                    "status": "활성",
                },
                {
                    "id": "FLT-009",
                    "filter_key": "LOT_ID",
                    "display_name": "LOT 식별자",
                    "semantic_role": "lot_identifier",
                    "value_type": "string",
                    "column_candidates": ["LOT_ID", "LOT_NO"],
                    "aliases": ["LOT", "로트"],
                    "owner": "PKG 품질",
                    "updated_at": "2026.08.19",
                    "status": "검토 필요",
                },
            ],
            "domain": [
                {
                    "id": "DOM-011",
                    "section": "process_groups",
                    "section_label": "공정 그룹",
                    "key": "DA",
                    "display_name": "DA 공정 그룹",
                    "aliases": ["DA", "Die Attach"],
                    "question_cues": ["DA 공정", "DA 생산량"],
                    "summary": "DA1부터 DA6까지의 실제 공정명을 하나의 업무 공정 그룹으로 연결합니다.",
                    "owner": "PKG 생산기술",
                    "updated_at": "2026.08.27",
                    "status": "활성",
                },
                {
                    "id": "DOM-024",
                    "section": "quantity_terms",
                    "section_label": "수량 기준",
                    "key": "production_qty",
                    "display_name": "생산량",
                    "aliases": ["생산 수량", "투입량"],
                    "question_cues": ["생산량", "생산 실적"],
                    "summary": "생산 수량 컬럼과 SUM 집계를 연결해 생산량 질문에 사용합니다.",
                    "owner": "PKG 데이터운영",
                    "updated_at": "2026.08.23",
                    "status": "활성",
                },
                {
                    "id": "DOM-036",
                    "section": "analysis_recipes",
                    "section_label": "분석 레시피",
                    "key": "wip_long_stay",
                    "display_name": "장기 체류 WIP 분석",
                    "aliases": ["장기 재공", "체류 LOT"],
                    "question_cues": ["체류 시간이 긴 LOT", "장기 WIP"],
                    "summary": "WIP 조회, 기준 초과 필터, 공정별 집계 순서로 실행하는 다단계 분석 레시피입니다.",
                    "owner": "PKG 생산관리",
                    "updated_at": "2026.08.20",
                    "status": "활성",
                },
                {
                    "id": "DOM-051",
                    "section": "product_key_columns",
                    "section_label": "제품 키 컬럼",
                    "key": "product_join_key",
                    "display_name": "제품 조인 키",
                    "aliases": ["제품 코드", "제품명"],
                    "question_cues": ["제품별", "제품 단위"],
                    "summary": "서로 다른 데이터셋의 제품 정보를 연결할 때 사용하는 기준 컬럼입니다.",
                    "owner": "PKG 데이터운영",
                    "updated_at": "2026.08.17",
                    "status": "초안",
                },
            ],
        },
        "metadata_authoring": _metadata_authoring_preview(),
        "settings": {
            "usage_policy": usage_policy,
            "access_policy": {
                "metadata_page": "admin_only",
                "metadata_registration": "admin_only",
                "schedule_update": "owner_or_admin",
                "all_schedule_view": "all_users",
            },
            "api": {
                "gaia_endpoint": "GAIA External API",
                "cube_endpoint": "CUBE Rich Notification API",
                "callback_endpoint": "/api/v1/receiver",
                "metadata_endpoint": "/api/v1/metadata",
                "status": "정상",
                "last_checked": "오늘 09:42",
            },
            "admins": [
                {
                    "employee_id": "2069026",
                    "name": "문봉건",
                    "role": "Super Admin",
                    "scope": "전체 설정 · 관리자 관리",
                    "status": "활성",
                },
                {
                    "employee_id": "2079411",
                    "name": "최은서",
                    "role": "Metadata Admin",
                    "scope": "메타데이터 등록 · 검토",
                    "status": "활성",
                },
                {
                    "employee_id": "2093012",
                    "name": "이도윤",
                    "role": "Schedule Admin",
                    "scope": "스케줄 모니터링 · 재실행",
                    "status": "활성",
                },
            ],
        },
    }


@application.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@application.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "dummy-preview"}


@application.get("/api/metadata-authoring/status")
async def metadata_authoring_status() -> dict[str, Any]:
    """Report safe configuration readiness without exposing API or Mongo secrets."""

    return _metadata_api_status(_metadata_settings_from_env())


@application.post("/api/metadata-authoring")
def submit_metadata_authoring(request_body: MetadataAuthoringRequest) -> dict[str, Any]:
    """Submit a natural-language authoring request through the configured Flow API.

    API mode never silently turns into a mock response: missing configuration,
    call failures, and an unrecognised upstream response are returned as errors.
    Preview/mock mode is the only branch that uses the current sample response.
    """

    raw_text = request_body.raw_text.strip()
    if not raw_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_raw_text", "message": "등록할 자연어 원문을 입력해 주세요."},
        )

    settings = _metadata_settings_from_env()
    request_missing = list(settings.configuration_errors)
    if settings.mode == "api" and not settings.endpoint_for(request_body.metadata_type):
        request_missing.append(f"endpoint:{request_body.metadata_type}")
    if settings.mode == "api" and settings.send_mongodb_tweaks:
        if not settings.mongo_uri:
            request_missing.append("MONGODB_URI")
        if not settings.mongo_database:
            request_missing.append("MONGODB_DATABASE")
    if settings.mode not in {"preview", "api"}:
        request_missing.append("PTMORE_METADATA_API_MODE")
    if request_missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_api_not_ready",
                "message": "메타데이터 API 연결 설정이 완료되지 않았습니다.",
                "missing": list(dict.fromkeys(request_missing)),
            },
        )

    run_id = f"META-{uuid4()}"
    requested_at = datetime.now(timezone.utc).isoformat()
    if settings.mode == "preview":
        response = _metadata_preview_response(
            metadata_type=request_body.metadata_type,
            raw_text=raw_text,
            duplicate_action=request_body.duplicate_action,
            requested_dry_run=request_body.dry_run,
        )
        preview_only = True
    else:
        endpoint = settings.endpoint_for(request_body.metadata_type)
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "invalid_metadata_api_endpoint",
                    "message": "메타데이터 API 주소 형식을 확인해 주세요.",
                },
            )

        outbound_payload = _metadata_api_payload(
            settings,
            metadata_type=request_body.metadata_type,
            raw_text=raw_text,
            duplicate_action=request_body.duplicate_action,
            dry_run=request_body.dry_run,
        )
        try:
            upstream_response = _metadata_http_client.post_json(
                endpoint,
                payload=outbound_payload,
                headers=_metadata_api_headers(settings),
                timeout_seconds=settings.timeout_seconds,
                verify_tls=settings.verify_tls,
            )
        except MetadataApiCallError as exc:
            upstream_status = exc.upstream_status
            response_status = (
                status.HTTP_504_GATEWAY_TIMEOUT
                if upstream_status in {408, 504}
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(
                status_code=response_status,
                detail={
                    "code": "metadata_api_request_failed",
                    "message": "메타데이터 등록 API 호출에 실패했습니다.",
                    "upstream_status": upstream_status,
                },
            ) from exc

        authoring_response = _find_api_terminal_response(
            upstream_response,
            api_terminal=settings.component_for(request_body.metadata_type, "api_terminal"),
        ) or _find_metadata_authoring_response(upstream_response)
        if authoring_response is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "metadata_api_response_unrecognized",
                    "message": "메타데이터 API 응답에서 rev_2 등록 결과를 찾지 못했습니다.",
                },
            )
        response = _normalize_metadata_authoring_response(
            authoring_response,
            metadata_type=request_body.metadata_type,
            raw_text=raw_text,
        )
        preview_only = False

    return {
        "run_id": run_id,
        "requested_at": requested_at,
        # Identity lookup is intentionally left on the current portal path.
        # A future SSO/employee adapter can populate this without changing the
        # external Flow request contract.
        "requested_by": None,
        "metadata_type": request_body.metadata_type,
        "preview_only": preview_only,
        "requested_dry_run": request_body.dry_run,
        "response": response,
    }


@application.get("/api/mock/portal")
async def portal_preview_data(preview_role: str = "admin") -> dict[str, Any]:
    return _portal_data(preview_role=preview_role)


@application.get("/api/mock/dashboard")
async def dashboard_preview_data() -> dict[str, Any]:
    return _portal_data()["dashboard"]


@application.get("/api/mock/schedules")
async def schedules_preview_data() -> list[dict[str, Any]]:
    return _portal_data()["schedules"]


@application.get("/api/mock/metadata")
async def metadata_preview_data() -> dict[str, list[dict[str, Any]]]:
    return _portal_data()["metadata"]


@application.get("/api/mock/metadata-authoring")
async def metadata_authoring_preview_data() -> dict[str, Any]:
    return _portal_data()["metadata_authoring"]


@application.get("/api/mock/usage-history")
async def usage_history_preview_data() -> list[dict[str, str]]:
    return _portal_data()["usage_history"]


@application.get("/api/mock/settings")
async def settings_preview_data() -> dict[str, Any]:
    return _portal_data()["settings"]


if __name__ == "__main__":
    uvicorn.run("app:application", host="127.0.0.1", port=8002, reload=False)
