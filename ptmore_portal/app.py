"""PTMORE PKG Agent management portal.

The dashboard can explicitly read recent Phoenix usage history when configured;
employee and scheduling screens remain local design preview data for now.
Metadata authoring can call a separately configured external Flow API; this
portal never connects to CUBE or GAIA directly.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import os
import re
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol
from urllib import error as url_error
from urllib import request as url_request
from urllib.parse import urlparse
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
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


_METADATA_TYPES = ("domain", "table_catalog", "main_flow_filters")
_METADATA_TYPE_LABELS = {
    "domain": "도메인 정보",
    "table_catalog": "테이블 카탈로그",
    "main_flow_filters": "메인 플로우 필터",
}
_METADATA_COLLECTION_SUFFIXES = {
    "domain": "domain_items",
    "table_catalog": "table_catalog_items",
    "main_flow_filters": "main_flow_filters",
}

# Portal-owned MongoDB collections are intentionally separate from the
# Langflow metadata collections above.  Operators can use a test prefix or a
# production-specific name without changing application code.  The schedule
# collections are declared now, but schedule CRUD is not enabled yet.
_DEFAULT_PORTAL_SETTINGS_COLLECTION = "portal_settings"
_DEFAULT_PORTAL_AUDIT_COLLECTION = "portal_audit_log"
_DEFAULT_SCHEDULE_COLLECTION = "portal_schedules"
_DEFAULT_SCHEDULE_RUN_COLLECTION = "portal_schedule_runs"
_PORTAL_MONGODB_COLLECTION_ENVIRONMENTS = {
    "settings_collection": (
        "PTMORE_PORTAL_SETTINGS_COLLECTION",
        _DEFAULT_PORTAL_SETTINGS_COLLECTION,
    ),
    "audit_collection": (
        "PTMORE_PORTAL_AUDIT_COLLECTION",
        _DEFAULT_PORTAL_AUDIT_COLLECTION,
    ),
    "schedule_collection": (
        "PTMORE_SCHEDULE_COLLECTION",
        _DEFAULT_SCHEDULE_COLLECTION,
    ),
    "schedule_run_collection": (
        "PTMORE_SCHEDULE_RUN_COLLECTION",
        _DEFAULT_SCHEDULE_RUN_COLLECTION,
    ),
}

# A live metadata list is intentionally opt-in.  The portal must never guess
# that a similarly named collection (for example ``agent_v4_*`` instead of a
# configured ``agent_v4_test_*``) is the source of truth.
_METADATA_LIVE_READ_MODES = {"disabled", "configured"}
_METADATA_LIVE_READ_DEFAULT_LIMIT = 200
_METADATA_LIVE_READ_MAX_LIMIT = 500
_METADATA_RECORD_ID_MAX_LENGTH = 512
_METADATA_RECORD_TOKEN_MAX_LENGTH = 1_024
_METADATA_DETAIL_MAX_DEPTH = 8
_METADATA_DETAIL_MAX_ITEMS = 200
_METADATA_DETAIL_MAX_STRING_LENGTH = 50_000
_METADATA_LIVE_READ_RESERVED_COLLECTIONS = {
    _DEFAULT_PORTAL_SETTINGS_COLLECTION,
    _DEFAULT_PORTAL_AUDIT_COLLECTION,
    _DEFAULT_SCHEDULE_COLLECTION,
    _DEFAULT_SCHEDULE_RUN_COLLECTION,
}

# The caller ID is a non-secret, administrator-managed value.  It is kept in
# MongoDB settings rather than `.env` so an administrator can change it
# without a server restart.  The API key remains an environment/secret value.
_GAIA_CALLER_ID_HEADER = "X-Gaia-User-Id"
_PORTAL_EMPLOYEE_ID_HEADER = "X-PTMORE-Employee-Id"
_PORTAL_EMPLOYEE_NAME_HEADER = "X-PTMORE-Employee-Name"
_PORTAL_SETTINGS_DOCUMENT_ID = "global"

# 스케줄 실행 결과는 현재 스케줄을 등록한 사용자에게만 개인 DM으로
# 전달한다. 채널 발송은 이 Portal의 스케줄 계약에 포함하지 않는다.
_SCHEDULE_DELIVERY_TARGET = "개인 DM"

# Dashboard usage data is intentionally independent of the metadata-authoring
# Flow adapter.  ``phoenix`` must be explicitly selected in the environment;
# otherwise the Portal remains in its clearly labelled local preview mode.
_USAGE_HISTORY_WINDOW_DAYS = 21
_USAGE_HISTORY_MODES = {"preview", "phoenix"}
_KST = timezone(timedelta(hours=9), name="Asia/Seoul")

logger = logging.getLogger(__name__)

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


class MetadataStatusUpdateRequest(BaseModel):
    """The only mutation allowed for one live metadata record.

    The metadata authoring Portal never removes Flow metadata.  Administrators
    can only decide whether an already registered item participates in the
    normal ``status=active`` lookup path.
    """

    status: Literal["active", "inactive"]


class ActiveUserPolicyUpdate(BaseModel):
    """The administrator-editable thresholds used by the dashboard KPI."""

    active_user_min_distinct_days: int = Field(..., ge=1, le=365)
    active_user_min_chat_count: int = Field(..., ge=1, le=100_000)


class PortalSettingsUpdateRequest(BaseModel):
    """Safe, non-secret values an administrator may change in the portal."""

    gaia_api_caller_employee_id: str | None = Field(default=None, max_length=64)
    usage_policy: ActiveUserPolicyUpdate | None = None


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

    def endpoint_source_for(self, metadata_type: str) -> str:
        """Return the configured endpoint source without exposing its URL."""

        if str(self.api_urls.get(metadata_type) or "").strip():
            return "type_specific_url"
        if self.api_url:
            return "common_url"
        return "not_configured"

    def collection_for(self, metadata_type: str) -> str:
        configured = str(self.mongo_collections.get(metadata_type) or "").strip()
        if configured:
            return configured
        suffix = _METADATA_COLLECTION_SUFFIXES[metadata_type]
        return f"{self.mongo_collection_prefix}{suffix}" if self.mongo_collection_prefix else suffix

    def collection_source_for(self, metadata_type: str) -> str:
        """Describe how the portal calculated a collection name."""

        if str(self.mongo_collections.get(metadata_type) or "").strip():
            return "explicit_collection_map"
        if self.mongo_collection_prefix:
            return "collection_prefix"
        return "built_in_suffix"

    def component_for(self, metadata_type: str, name: str) -> str:
        component = self.component_map.get(metadata_type, {})
        return str(component.get(name) or "").strip()


def _environment_value(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


class PhoenixUsageUnavailableError(RuntimeError):
    """A safe Portal-level failure while loading Phoenix usage records.

    The original Phoenix exception can contain endpoint or GraphQL details.
    This boundary intentionally keeps those details out of the browser API.
    """


# These narrow seams keep the Portal route testable without a real Phoenix
# server.  The defaults are lazy imports so the normal preview server can
# still start before the optional live-integration dependency is installed.
_phoenix_usage_config_factory: Callable[[], Any] | None = None
_phoenix_usage_fetcher: Callable[..., list[Mapping[str, Any]]] | None = None


def _usage_history_mode_from_env() -> str:
    """Return the explicit dashboard source mode without guessing a fallback."""

    configured = _environment_value("PTMORE_USAGE_HISTORY_MODE", "preview").lower()
    # ``mock`` was used in a few early Portal notes; accepting it only as an
    # explicit preview alias keeps deployed preview environments compatible.
    if configured == "mock":
        return "preview"
    return configured


def _phoenix_usage_config_from_env() -> Any:
    if _phoenix_usage_config_factory is not None:
        return _phoenix_usage_config_factory()

    try:
        from phoenix_usage import PhoenixUsageConfig, PhoenixUsageError
    except ImportError as exc:
        raise PhoenixUsageUnavailableError(
            "Phoenix 사용 이력 모듈을 불러올 수 없습니다."
        ) from exc

    try:
        return PhoenixUsageConfig.from_env()
    except (PhoenixUsageError, TypeError, ValueError) as exc:
        raise PhoenixUsageUnavailableError(
            "Phoenix 사용 이력 설정을 해석할 수 없습니다."
        ) from exc


def _fetch_phoenix_usage_history(
    configuration: Any,
    *,
    days: int,
    today: date,
) -> list[Mapping[str, Any]]:
    """Fetch live records through the dedicated Phoenix client module."""

    if _phoenix_usage_fetcher is not None:
        return list(_phoenix_usage_fetcher(configuration, days=days, today=today))

    try:
        from phoenix_usage import PhoenixUsageError, fetch_recent_usage
    except ImportError as exc:
        raise PhoenixUsageUnavailableError(
            "Phoenix 사용 이력 모듈을 불러올 수 없습니다."
        ) from exc

    try:
        return list(fetch_recent_usage(configuration, days=days, today=today))
    except PhoenixUsageError as exc:
        raise PhoenixUsageUnavailableError(
            "Phoenix 사용 이력을 조회할 수 없습니다."
        ) from exc


def _phoenix_configuration_errors(configuration: Any) -> list[str]:
    """Expose only configuration field names, never values or credentials."""

    raw_errors = getattr(configuration, "configuration_errors", ())
    if not isinstance(raw_errors, (list, tuple, set)):
        return []
    return [str(item) for item in raw_errors if str(item).strip()]


def _configured_phoenix_project_count(configuration: Any) -> int:
    projects = getattr(configuration, "projects", ())
    if not isinstance(projects, (list, tuple, set)):
        return 0
    return len([project for project in projects if str(project).strip()])


def _recent_kst_period(*, days: int = _USAGE_HISTORY_WINDOW_DAYS) -> tuple[date, date]:
    """Return an inclusive KST calendar range for the dashboard graph."""

    if days < 1:
        raise ValueError("days must be at least one")
    end_day = datetime.now(_KST).date()
    return end_day - timedelta(days=days - 1), end_day


def _iso_usage_date(value: Any) -> str:
    """Read one ISO date safely from a Phoenix date/time field."""

    text = str(value or "").strip()
    if len(text) < 10:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def _normalise_phoenix_usage_history(
    records: list[Mapping[str, Any]],
    *,
    start_day: date,
    end_day: date,
) -> list[dict[str, str]]:
    """Adapt Phoenix ``GaiA Input`` rows to the existing Portal UI contract.

    Phoenix currently supplies a user ID, not the employee directory display
    name.  Until that directory adapter is added, the user ID is intentionally
    shown in both fields instead of inventing a name.
    """

    normalized: list[dict[str, str]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue

        query_time = str(
            record.get("query_time") or record.get("occurred_at") or ""
        ).strip()
        occurred_at = str(
            record.get("occurred_at") or query_time or ""
        ).strip()
        usage_date = _iso_usage_date(record.get("date") or occurred_at or query_time)
        if not usage_date:
            continue
        try:
            parsed_day = date.fromisoformat(usage_date)
        except ValueError:  # Defensive; _iso_usage_date already checks this.
            continue
        if parsed_day < start_day or parsed_day > end_day:
            continue

        employee_id = str(record.get("user_id") or "").strip() or "미확인"
        platform = str(record.get("platform") or "").strip() or "미분류"
        question = str(record.get("question") or "").strip() or "(질문 정보 없음)"
        normalized.append(
            {
                "id": f"PHX-{index + 1}",
                "employee_id": employee_id,
                "user_name": employee_id,
                "question": question,
                "date": usage_date,
                "query_time": query_time or occurred_at,
                "occurred_at": occurred_at or query_time,
                "channel": platform,
                "platform": platform,
            }
        )

    return sorted(
        normalized,
        key=lambda item: (item["occurred_at"], item["id"]),
    )


def _valid_mongodb_collection_name(value: str) -> bool:
    """Accept a conservative, portable MongoDB collection name.

    Collection names are configuration, not user input, but validating them
    before a MongoDB handle is opened prevents an accidental environment value
    from selecting a system collection or an unexpected namespace.
    """

    name = str(value or "").strip()
    if not name or len(name) > 120 or name.startswith("system."):
        return False
    return all(character.isalnum() or character in {"_", "-", "."} for character in name)


@dataclass(frozen=True)
class PortalMongoCollectionSettings:
    """Safe, named Portal MongoDB collections resolved from environment values."""

    settings_collection: str
    audit_collection: str
    schedule_collection: str
    schedule_run_collection: str
    configuration_errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.configuration_errors

    @property
    def all_collections(self) -> tuple[str, ...]:
        return (
            self.settings_collection,
            self.audit_collection,
            self.schedule_collection,
            self.schedule_run_collection,
        )


def _portal_mongodb_collection_settings_from_env() -> PortalMongoCollectionSettings:
    """Resolve Portal collection names without silently accepting unsafe values.

    Blank values intentionally use the documented defaults.  If a supplied
    value is invalid or duplicates another Portal role, all runtime handles
    fall back to the known-safe defaults *and* the returned configuration is
    marked invalid.  The settings store then refuses writes rather than
    guessing which collection an operator intended.
    """

    names: dict[str, str] = {}
    errors: list[str] = []
    for field_name, (environment_name, default_name) in _PORTAL_MONGODB_COLLECTION_ENVIRONMENTS.items():
        configured_name = _environment_value(environment_name)
        candidate = configured_name or default_name
        if not _valid_mongodb_collection_name(candidate):
            errors.append(environment_name)
        names[field_name] = candidate

    seen_names: set[str] = set()
    for field_name, (environment_name, _) in _PORTAL_MONGODB_COLLECTION_ENVIRONMENTS.items():
        candidate = names[field_name]
        if candidate in seen_names:
            errors.append(environment_name)
        seen_names.add(candidate)

    if errors:
        names = {
            field_name: default_name
            for field_name, (_, default_name) in _PORTAL_MONGODB_COLLECTION_ENVIRONMENTS.items()
        }

    return PortalMongoCollectionSettings(
        settings_collection=names["settings_collection"],
        audit_collection=names["audit_collection"],
        schedule_collection=names["schedule_collection"],
        schedule_run_collection=names["schedule_run_collection"],
        configuration_errors=tuple(dict.fromkeys(errors)),
    )


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


@dataclass(frozen=True)
class LiveMetadataReadSettings:
    """Explicit, read-only source settings for the Portal metadata list.

    This is deliberately separate from the authoring Flow writer configuration.
    A portal operator may use a read-only MongoDB account or a different source
    database without changing the values passed to a Langflow Flow.
    """

    mode: str
    mongo_uri: str
    mongo_database: str
    collections: Mapping[str, str]
    collection_sources: Mapping[str, str]
    item_limit: int
    configuration_errors: tuple[str, ...]

    @property
    def enabled(self) -> bool:
        return self.mode == "configured"

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self.mongo_uri
            and self.mongo_database
            and not self.configuration_errors
        )

    def collection_for(self, metadata_type: str) -> str:
        return str(self.collections.get(metadata_type) or "").strip()

    def collection_source_for(self, metadata_type: str) -> str:
        return str(self.collection_sources.get(metadata_type) or "").strip()


def _live_metadata_read_mode() -> tuple[str, list[str]]:
    """Read the opt-in switch without accepting arbitrary mode names."""

    raw_mode = _environment_value("PTMORE_METADATA_LIVE_READ_MODE", "disabled").lower()
    if raw_mode in _METADATA_LIVE_READ_MODES:
        return raw_mode, []
    if raw_mode in {"", "disabled", "off", "false", "0", "no"}:
        return "disabled", []
    if raw_mode in {"configured", "live", "mongo", "on", "true", "1", "yes"}:
        return "configured", []
    return "invalid", ["PTMORE_METADATA_LIVE_READ_MODE"]


def _live_metadata_item_limit() -> tuple[int, list[str]]:
    raw_limit = _environment_value(
        "PTMORE_METADATA_LIVE_READ_LIMIT",
        str(_METADATA_LIVE_READ_DEFAULT_LIMIT),
    )
    try:
        item_limit = int(raw_limit)
    except ValueError:
        return _METADATA_LIVE_READ_DEFAULT_LIMIT, ["PTMORE_METADATA_LIVE_READ_LIMIT"]
    if not 1 <= item_limit <= _METADATA_LIVE_READ_MAX_LIMIT:
        return _METADATA_LIVE_READ_DEFAULT_LIMIT, ["PTMORE_METADATA_LIVE_READ_LIMIT"]
    return item_limit, []


def _valid_live_metadata_collection_name(
    value: str,
    *,
    reserved_collections: tuple[str, ...] = (),
) -> bool:
    """Accept ordinary Mongo collection names but reject Portal-only storage."""

    name = str(value or "").strip()
    if not _valid_mongodb_collection_name(name):
        return False
    return name not in {
        *_METADATA_LIVE_READ_RESERVED_COLLECTIONS,
        *reserved_collections,
    }


def _live_metadata_read_settings_from_env(
    metadata_settings: MetadataAuthoringSettings | None = None,
) -> LiveMetadataReadSettings:
    """Resolve a read source without guessing a different collection prefix.

    ``PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON`` is the explicit way to select
    a live collection set.  When it is omitted, the currently configured
    Portal MongoDB collection map/prefix is used exactly as-is.
    """

    metadata_settings = metadata_settings or _metadata_settings_from_env()
    mode, errors = _live_metadata_read_mode()
    item_limit, limit_errors = _live_metadata_item_limit()
    errors.extend(limit_errors)

    configured_collections, collection_errors = _json_object_from_environment(
        "PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON"
    )
    errors.extend(collection_errors)
    portal_collections = _portal_mongodb_collection_settings_from_env()
    collections: dict[str, str] = {}
    collection_sources: dict[str, str] = {}
    for metadata_type in _METADATA_TYPES:
        explicit_name = str(configured_collections.get(metadata_type) or "").strip()
        collection_name = explicit_name or metadata_settings.collection_for(metadata_type)
        collections[metadata_type] = collection_name
        collection_sources[metadata_type] = (
            "live_collection_map"
            if explicit_name
            else "portal_collection_configuration"
        )
        if mode == "configured" and not _valid_live_metadata_collection_name(
            collection_name,
            reserved_collections=portal_collections.all_collections,
        ):
            errors.append(f"collection:{metadata_type}")

    live_uri = (
        _environment_value("PTMORE_METADATA_LIVE_MONGODB_URI")
        or metadata_settings.mongo_uri
    )
    live_database = (
        _environment_value("PTMORE_METADATA_LIVE_MONGODB_DATABASE")
        or metadata_settings.mongo_database
    )
    if mode == "configured":
        if not live_uri:
            errors.append("MONGODB_URI")
        if not live_database:
            errors.append("MONGODB_DATABASE")

    return LiveMetadataReadSettings(
        mode=mode,
        mongo_uri=live_uri,
        mongo_database=live_database,
        collections=collections,
        collection_sources=collection_sources,
        item_limit=item_limit,
        configuration_errors=tuple(dict.fromkeys(errors)),
    )


class MetadataLiveReadError(RuntimeError):
    """A safe error raised when the Portal cannot read its live metadata view."""


class MetadataLiveMutationError(RuntimeError):
    """A safe error raised when one configured metadata status cannot be updated."""


class InvalidMetadataRecordIdError(ValueError):
    """Raised when a caller supplies a malformed opaque metadata record token."""


class MetadataLiveReader(Protocol):
    """Read-only MongoDB boundary, kept injectable for hermetic tests."""

    def read_collection(
        self,
        *,
        metadata_type: str,
        collection_name: str,
        item_limit: int,
    ) -> tuple[int, list[Mapping[str, Any]], bool]:
        ...

    def close(self) -> None:
        ...


class MetadataLiveStatusUpdater(Protocol):
    """Narrow mutation boundary for changing one configured record's status."""

    def update_document_status(
        self,
        *,
        metadata_type: str,
        collection_name: str,
        record_id: Any,
        status_value: Literal["active", "inactive"],
    ) -> bool:
        ...

    def close(self) -> None:
        ...


class MetadataLiveDetailReader(Protocol):
    """Narrow read boundary for a single, already-listed metadata document."""

    def read_document(
        self,
        *,
        metadata_type: str,
        collection_name: str,
        record_id: Any,
    ) -> Mapping[str, Any] | None:
        ...

    def close(self) -> None:
        ...


_METADATA_LIVE_MONGO_PROJECTIONS: dict[str, dict[str, int]] = {
    "table_catalog": {
        "_id": 1,
        "dataset_key": 1,
        "key": 1,
        "display_name": 1,
        "dataset_family": 1,
        "source_type": 1,
        "status": 1,
        "payload.display_name": 1,
        "payload.source_type": 1,
        "payload.dataset_family": 1,
        "payload.required_filters": 1,
        "payload.required_params": 1,
        "payload.filter_mappings": 1,
        "payload.source_config.source_type": 1,
    },
    "main_flow_filters": {
        "_id": 1,
        "filter_key": 1,
        "key": 1,
        "display_name": 1,
        "status": 1,
        "payload.display_name": 1,
        "payload.operator": 1,
        "payload.value_type": 1,
        "payload.value_shape": 1,
    },
    "domain": {
        "_id": 1,
        "section": 1,
        "section_label": 1,
        "key": 1,
        "display_name": 1,
        "status": 1,
        "payload.display_name": 1,
    },
}

# The list endpoint intentionally uses a compact projection.  A detail request
# is still bounded to a Flow-contract allowlist: it may show SQL and mapping
# information needed by an administrator, but not raw registration input,
# connection settings, credentials, or arbitrary MongoDB fields.
_METADATA_LIVE_DETAIL_MONGO_PROJECTIONS: dict[str, dict[str, int]] = {
    "table_catalog": {
        "_id": 1,
        "dataset_key": 1,
        "key": 1,
        "display_name": 1,
        "dataset_family": 1,
        "source_type": 1,
        "status": 1,
        "payload.display_name": 1,
        "payload.dataset_family": 1,
        "payload.source_type": 1,
        "payload.source_config.source_type": 1,
        "payload.source_config.db_key": 1,
        "payload.source_config.query_template": 1,
        "payload.source_config.doc_id": 1,
        "payload.source_config.sheet_name": 1,
        "payload.source_config.endpoint_id": 1,
        "payload.source_config.response_path": 1,
        "payload.source_config.method": 1,
        "payload.source_config.reference": 1,
        "payload.source_config.ref": 1,
        "payload.source_config.source_ref": 1,
        "payload.source_config.upstream_bindings": 1,
        "payload.required_params": 1,
        "payload.required_filters": 1,
        "payload.required_param_mappings": 1,
        "payload.filter_mappings": 1,
        "payload.standard_column_aliases": 1,
        "payload.columns": 1,
        "payload.selection_criteria": 1,
        "payload.default_detail_columns": 1,
        "payload.metric_semantics": 1,
    },
    "main_flow_filters": {
        "_id": 1,
        "filter_key": 1,
        "key": 1,
        "display_name": 1,
        "status": 1,
        "payload.display_name": 1,
        "payload.aliases": 1,
        "payload.operator": 1,
        "payload.value_type": 1,
        "payload.value_shape": 1,
        "payload.description": 1,
        "payload.value_examples": 1,
        "payload.column_candidates": 1,
        "payload.candidate_columns": 1,
        "payload.standard_column_aliases": 1,
        "payload.selection_criteria": 1,
    },
    "domain": {
        "_id": 1,
        "section": 1,
        "key": 1,
        "display_name": 1,
        "status": 1,
        "payload.display_name": 1,
        "payload.aliases": 1,
        "payload.field": 1,
        "payload.processes": 1,
        "payload.values": 1,
        "payload.description": 1,
        "payload.summary": 1,
        "payload.rules": 1,
        "payload.steps": 1,
        "payload.columns": 1,
        "payload.metric_semantics": 1,
        "payload.default_detail_columns": 1,
        "payload.analysis_steps": 1,
        "payload.function_cases": 1,
    },
}

_TABLE_CATALOG_DETAIL_SOURCE_CONFIG_KEYS = {
    "source_type",
    "db_key",
    "query_template",
    "doc_id",
    "sheet_name",
    "endpoint_id",
    "response_path",
    "method",
    "reference",
    "ref",
    "source_ref",
    "upstream_bindings",
}

_METADATA_DETAIL_PAYLOAD_FIELDS: Mapping[str, tuple[str, ...]] = {
    "table_catalog": (
        "display_name",
        "dataset_family",
        "source_type",
        "source_config",
        "required_params",
        "required_filters",
        "required_param_mappings",
        "filter_mappings",
        "standard_column_aliases",
        "columns",
        "selection_criteria",
        "default_detail_columns",
        "metric_semantics",
    ),
    "main_flow_filters": (
        "display_name",
        "aliases",
        "operator",
        "value_type",
        "value_shape",
        "description",
        "value_examples",
        "column_candidates",
        "candidate_columns",
        "standard_column_aliases",
        "selection_criteria",
    ),
    "domain": (
        "display_name",
        "aliases",
        "field",
        "processes",
        "values",
        "description",
        "summary",
        "rules",
        "steps",
        "columns",
        "metric_semantics",
        "default_detail_columns",
        "analysis_steps",
        "function_cases",
    ),
}

_METADATA_DETAIL_SENSITIVE_KEY_NAMES = {
    "api_key",
    "authorization",
    "connection_string",
    "connection_uri",
    "endpoint",
    "headers",
    "mongo_uri",
    "raw_text",
    "registration_trace",
    "token_source",
    "token_key",
}
_METADATA_DETAIL_CONNECTION_URI_PATTERN = re.compile(
    r"(?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|oracle)://"
)
_METADATA_DETAIL_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_ -]?key|authorization|credential)"
    r"\s*([:=])\s*(['\"]?)[^\s,;\]\}\"']+"
)


class MongoMetadataLiveReader:
    """Read a bounded projection from configured collections; never writes."""

    def __init__(self, *, uri: str, database: str) -> None:
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise MetadataLiveReadError(
                "MongoDB 메타데이터 읽기를 위해 pymongo 패키지가 필요합니다."
            ) from exc

        self._mongo_error = PyMongoError
        try:
            self._client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
            )
            self._database = self._client[database]
        except PyMongoError as exc:
            raise MetadataLiveReadError(
                "MongoDB 메타데이터 읽기 연결을 초기화할 수 없습니다."
            ) from exc

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._mongo_error as exc:
            raise MetadataLiveReadError(
                "MongoDB 메타데이터를 읽을 수 없습니다."
            ) from exc

    def read_collection(
        self,
        *,
        metadata_type: str,
        collection_name: str,
        item_limit: int,
    ) -> tuple[int, list[Mapping[str, Any]], bool]:
        projection = _METADATA_LIVE_MONGO_PROJECTIONS[metadata_type]
        collection = self._database[collection_name]
        total_count = int(self._run(lambda: collection.count_documents({})))
        # ``limit + 1`` lets the response state that the UI data was bounded
        # without transferring any unbounded collection contents.
        cursor = self._run(
            lambda: collection.find({}, projection).sort("updated_at", -1).limit(item_limit + 1)
        )
        documents = self._run(lambda: list(cursor))
        truncated = len(documents) > item_limit
        return (
            total_count,
            [dict(document) for document in documents[:item_limit] if isinstance(document, Mapping)],
            truncated,
        )

    def read_document(
        self,
        *,
        metadata_type: str,
        collection_name: str,
        record_id: Any,
    ) -> Mapping[str, Any] | None:
        """Read one exact ``_id`` using the narrow detail projection only."""

        projection = _METADATA_LIVE_DETAIL_MONGO_PROJECTIONS[metadata_type]
        collection = self._database[collection_name]
        document = self._run(
            lambda: collection.find_one({"_id": record_id}, projection)
        )
        return dict(document) if isinstance(document, Mapping) else None

    def close(self) -> None:
        try:
            self._client.close()
        except self._mongo_error:
            return None


class MongoMetadataLiveStatusUpdater:
    """Update only ``status`` on one configured metadata document.

    The Portal does not accept a collection, database, query, or MongoDB
    operator from the browser.  The collection comes from
    :class:`LiveMetadataReadSettings`, the filter is always an exact ``_id``
    equality match decoded from a server-issued opaque token, and the only
    MongoDB mutation is a ``$set`` of the validated status value.
    """

    def __init__(self, *, uri: str, database: str) -> None:
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise MetadataLiveMutationError(
                "MongoDB 메타데이터 상태 변경을 위해 pymongo 패키지가 필요합니다."
            ) from exc

        self._mongo_error = PyMongoError
        try:
            self._client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
            )
            self._database = self._client[database]
        except PyMongoError as exc:
            raise MetadataLiveMutationError(
                "MongoDB 메타데이터 상태 변경 연결을 초기화할 수 없습니다."
            ) from exc

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._mongo_error as exc:
            raise MetadataLiveMutationError(
                "MongoDB 메타데이터 상태를 변경할 수 없습니다."
            ) from exc

    def update_document_status(
        self,
        *,
        metadata_type: str,
        collection_name: str,
        record_id: Any,
        status_value: Literal["active", "inactive"],
    ) -> bool:
        # Both fields are deliberately accepted at this boundary so tests can
        # assert that the server selected the configured type/collection. The
        # actual MongoDB filter is intentionally one exact primary-key value.
        if status_value not in {"active", "inactive"}:
            raise ValueError("invalid metadata status")
        del metadata_type
        collection = self._database[collection_name]
        result = self._run(
            lambda: collection.update_one(
                {"_id": record_id},
                {"$set": {"status": status_value}},
            )
        )
        return int(getattr(result, "matched_count", 0) or 0) == 1

    def close(self) -> None:
        try:
            self._client.close()
        except self._mongo_error:
            return None


_metadata_live_reader_factory: Callable[[LiveMetadataReadSettings], MetadataLiveReader] | None = None
_metadata_live_status_updater_factory: Callable[
    [LiveMetadataReadSettings], MetadataLiveStatusUpdater
] | None = None
_metadata_live_detail_reader_factory: Callable[[LiveMetadataReadSettings], MetadataLiveDetailReader] | None = None


def _get_metadata_live_reader(settings: LiveMetadataReadSettings) -> MetadataLiveReader:
    if _metadata_live_reader_factory is not None:
        return _metadata_live_reader_factory(settings)
    return MongoMetadataLiveReader(uri=settings.mongo_uri, database=settings.mongo_database)


def _get_metadata_live_status_updater(
    settings: LiveMetadataReadSettings,
) -> MetadataLiveStatusUpdater:
    if _metadata_live_status_updater_factory is not None:
        return _metadata_live_status_updater_factory(settings)
    return MongoMetadataLiveStatusUpdater(uri=settings.mongo_uri, database=settings.mongo_database)


def _get_metadata_live_detail_reader(
    settings: LiveMetadataReadSettings,
) -> MetadataLiveDetailReader:
    if _metadata_live_detail_reader_factory is not None:
        return _metadata_live_detail_reader_factory(settings)
    return MongoMetadataLiveReader(uri=settings.mongo_uri, database=settings.mongo_database)


def _safe_metadata_text(value: Any, *, max_length: int = 240) -> str:
    """Convert a simple display value without serialising arbitrary Mongo data."""

    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    text = str(value).strip().replace("\x00", "")
    return text[:max_length]


def _safe_metadata_list(value: Any, *, max_items: int = 16, max_length: int = 96) -> list[str]:
    """Return a small scalar-only list for UI chips and table cells."""

    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = _safe_metadata_text(item, max_length=max_length)
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _safe_metadata_status(value: Any) -> str:
    raw = _safe_metadata_text(value, max_length=32)
    normalized = raw.casefold()
    if normalized in {"active", "enabled", "saved", "등록됨"}:
        return "활성"
    if normalized in {"inactive", "disabled", "deactivated", "비활성", "미사용"}:
        return "비활성"
    if normalized in {"draft", "초안"}:
        return "초안"
    if normalized in {"review", "needs_input", "검토", "검토 필요"}:
        return "검토 필요"
    return raw or "등록됨"


def _is_safe_metadata_document_id(value: str) -> bool:
    """Allow only bounded printable strings as a MongoDB string primary key."""

    if not value or len(value) > _METADATA_RECORD_ID_MAX_LENGTH:
        return False
    return all(character.isprintable() for character in value)


def _is_object_id_hex(value: str) -> bool:
    return len(value) == 24 and all(character in "0123456789abcdefABCDEF" for character in value)


def _encode_metadata_record_id(value: Any) -> str:
    """Return an opaque, URL-safe token for one MongoDB ``_id`` value.

    Existing Flow writers use string primary keys (for example
    ``table_catalog:production_today``).  The typed encoding keeps that value
    opaque in the browser and avoids confusing a 24-character string with a
    BSON ``ObjectId``.  ObjectId support is retained for older collections.
    Unsupported BSON identifier types are intentionally not mutable through
    the Portal.
    """

    if isinstance(value, str):
        if not _is_safe_metadata_document_id(value):
            return ""
        encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
        return f"s.{encoded}"

    if value.__class__.__name__ == "ObjectId":
        object_id = str(value)
        if _is_object_id_hex(object_id):
            return f"o.{object_id.lower()}"
    return ""


def _decode_metadata_record_id(token: str) -> Any:
    """Decode only a Portal-issued typed identifier; never parse a Mongo query."""

    normalized = str(token or "").strip()
    if not normalized or len(normalized) > _METADATA_RECORD_TOKEN_MAX_LENGTH:
        raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.")

    prefix, separator, encoded_value = normalized.partition(".")
    if not separator or not encoded_value:
        raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.")

    if prefix == "s":
        if not all(character.isalnum() or character in {"-", "_"} for character in encoded_value):
            raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.")
        try:
            padding = "=" * (-len(encoded_value) % 4)
            value = base64.urlsafe_b64decode(f"{encoded_value}{padding}".encode("ascii")).decode("utf-8")
        except (UnicodeDecodeError, ValueError, binascii.Error):
            raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.") from None
        if not _is_safe_metadata_document_id(value):
            raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.")
        return value

    if prefix == "o":
        if not _is_object_id_hex(encoded_value):
            raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.")
        try:
            from bson import ObjectId
        except ImportError as exc:
            raise MetadataLiveMutationError(
                "MongoDB 메타데이터 상태 변경을 위해 pymongo 패키지가 필요합니다."
            ) from exc
        return ObjectId(encoded_value)

    raise InvalidMetadataRecordIdError("메타데이터 항목 식별자 형식이 올바르지 않습니다.")


def _with_live_metadata_record_id(
    item: dict[str, Any],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach the opaque status-update token without returning MongoDB's raw ``_id``."""

    record_token = _encode_metadata_record_id(document.get("_id"))
    if record_token:
        item["_record_id"] = record_token
    return item


def _metadata_payload(document: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = document.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _metadata_detail_key_is_sensitive(value: Any) -> bool:
    """Return whether a JSON key could reveal connection or credential data.

    Metadata writers already reject these fields, but detail output must remain
    safe for older collections and hand-edited documents as well.  The check
    deliberately avoids treating ordinary fields such as ``dataset_key`` as
    sensitive while blocking compound names such as ``api_key`` or
    ``token_source``.
    """

    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    if not normalized:
        return True
    compact = normalized.replace("_", "")
    if normalized in _METADATA_DETAIL_SENSITIVE_KEY_NAMES or compact in {
        "apikey",
        "connectionstring",
        "connectionuri",
        "connectionurl",
        "mongouri",
        "rawtext",
        "registrationtrace",
        "tokensource",
        "tokenkey",
    }:
        return True

    parts = {part for part in normalized.split("_") if part}
    if parts & {
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "credential",
        "cookie",
    }:
        return True
    if {"api", "key"} <= parts or {"access", "key"} <= parts or {"private", "key"} <= parts:
        return True
    if "header" in parts or "headers" in parts:
        return True
    if any(
        marker in compact
        for marker in (
            "password",
            "passwd",
            "secret",
            "token",
            "authorization",
            "credential",
            "cookie",
            "apikey",
            "accesskey",
            "privatekey",
            "header",
        )
    ):
        return True
    if compact in {"uri", "url", "endpoint", "connection", "connectionuri", "connectionurl"}:
        return True
    return False


def _redact_metadata_detail_text(value: Any) -> str:
    """Keep human-readable metadata text while hiding embedded credentials."""

    text = str(value or "").replace("\x00", "").strip()
    if _METADATA_DETAIL_CONNECTION_URI_PATTERN.search(text):
        return "[연결 주소는 표시하지 않습니다.]"
    text = _METADATA_DETAIL_SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[비공개]",
        text,
    )
    if len(text) > _METADATA_DETAIL_MAX_STRING_LENGTH:
        return f"{text[:_METADATA_DETAIL_MAX_STRING_LENGTH]}\n… (길이 제한으로 일부 생략)"
    return text


def _safe_metadata_detail_value(value: Any, *, depth: int = 0) -> Any:
    """Recursively serialize only bounded, credential-free detail values."""

    if depth >= _METADATA_DETAIL_MAX_DEPTH:
        return "[중첩 깊이 제한으로 생략]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_metadata_detail_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, nested_value) in enumerate(value.items()):
            if index >= _METADATA_DETAIL_MAX_ITEMS:
                result["_truncated"] = "항목 수 제한으로 일부 생략"
                break
            key = _safe_metadata_text(raw_key, max_length=120)
            if not key or _metadata_detail_key_is_sensitive(key):
                continue
            result[key] = _safe_metadata_detail_value(nested_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for index, item in enumerate(value):
            if index >= _METADATA_DETAIL_MAX_ITEMS:
                result.append("[항목 수 제한으로 일부 생략]")
                break
            result.append(_safe_metadata_detail_value(item, depth=depth + 1))
        return result
    return _redact_metadata_detail_text(value)


def _safe_table_catalog_source_config(value: Any) -> dict[str, Any]:
    """Select the non-secret source contract fields shown in a catalog detail."""

    source_config = value if isinstance(value, Mapping) else {}
    safe_config: dict[str, Any] = {}
    for key in _TABLE_CATALOG_DETAIL_SOURCE_CONFIG_KEYS:
        if key not in source_config or _metadata_detail_key_is_sensitive(key):
            continue
        safe_config[key] = _safe_metadata_detail_value(source_config[key])
    return safe_config


def _safe_metadata_detail_payload(
    metadata_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only Flow-backed display fields for one metadata type."""

    detail: dict[str, Any] = {}
    for key in _METADATA_DETAIL_PAYLOAD_FIELDS[metadata_type]:
        if key not in payload or _metadata_detail_key_is_sensitive(key):
            continue
        if metadata_type == "table_catalog" and key == "source_config":
            safe_config = _safe_table_catalog_source_config(payload[key])
            if safe_config:
                detail[key] = safe_config
            continue
        detail[key] = _safe_metadata_detail_value(payload[key])
    return detail


def _live_metadata_detail_item(
    metadata_type: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a structured, Flow-contract detail object without raw Mongo data."""

    payload = _metadata_payload(document)
    if metadata_type == "table_catalog":
        identifier = "dataset_key"
        item = {
            identifier: _first_safe_metadata_text(
                document.get("dataset_key"), document.get("key"), max_length=120
            ),
            "display_name": _first_safe_metadata_text(
                payload.get("display_name"), document.get("display_name"), max_length=160
            ),
            "dataset_family": _first_safe_metadata_text(
                payload.get("dataset_family"), document.get("dataset_family"), max_length=96
            ),
            "source_type": _first_safe_metadata_text(
                payload.get("source_type"), document.get("source_type"), max_length=64
            ),
        }
    elif metadata_type == "main_flow_filters":
        identifier = "filter_key"
        item = {
            identifier: _first_safe_metadata_text(
                document.get("filter_key"), document.get("key"), max_length=120
            ),
            "display_name": _first_safe_metadata_text(
                payload.get("display_name"), document.get("display_name"), max_length=160
            ),
        }
    else:
        item = {
            "section": _safe_metadata_text(document.get("section"), max_length=96),
            "key": _safe_metadata_text(document.get("key"), max_length=120),
            "display_name": _first_safe_metadata_text(
                payload.get("display_name"), document.get("display_name"), max_length=160
            ),
        }

    item["status"] = _safe_metadata_status(document.get("status"))
    safe_payload = _safe_metadata_detail_payload(metadata_type, payload)
    if safe_payload:
        item["payload"] = safe_payload
    return item


def _first_safe_metadata_text(*values: Any, max_length: int = 240) -> str:
    for value in values:
        text = _safe_metadata_text(value, max_length=max_length)
        if text:
            return text
    return ""


def _project_live_table_catalog(document: Mapping[str, Any]) -> dict[str, Any]:
    payload = _metadata_payload(document)
    filter_mappings = payload.get("filter_mappings")
    required_filters = _safe_metadata_list(
        payload.get("required_filters") or payload.get("required_params")
    )
    if not required_filters and isinstance(filter_mappings, Mapping):
        required_filters = _safe_metadata_list(filter_mappings.keys())
    dataset_key = _first_safe_metadata_text(document.get("dataset_key"), document.get("key"), max_length=120)
    source_type = _first_safe_metadata_text(
        payload.get("source_type"),
        payload.get("source_config", {}).get("source_type")
        if isinstance(payload.get("source_config"), Mapping)
        else "",
        document.get("source_type"),
        max_length=64,
    )
    return _with_live_metadata_record_id({
        "dataset_key": dataset_key,
        "display_name": _first_safe_metadata_text(
            payload.get("display_name"), document.get("display_name"), dataset_key, max_length=160
        ),
        "dataset_family": _first_safe_metadata_text(
            payload.get("dataset_family"), document.get("dataset_family"), max_length=96
        ),
        "source_type": source_type or "미지정",
        "required_params": required_filters,
        "status": _safe_metadata_status(document.get("status")),
    }, document)


def _project_live_main_flow_filter(document: Mapping[str, Any]) -> dict[str, Any]:
    payload = _metadata_payload(document)
    filter_key = _first_safe_metadata_text(document.get("filter_key"), document.get("key"), max_length=120)
    return _with_live_metadata_record_id({
        "filter_key": filter_key,
        "display_name": _first_safe_metadata_text(
            payload.get("display_name"), document.get("display_name"), filter_key, max_length=160
        ),
        "operator": _safe_metadata_text(payload.get("operator"), max_length=48),
        "value_type": _safe_metadata_text(payload.get("value_type"), max_length=48),
        "value_shape": _safe_metadata_text(payload.get("value_shape"), max_length=48),
        "status": _safe_metadata_status(document.get("status")),
    }, document)


def _project_live_domain(document: Mapping[str, Any]) -> dict[str, Any]:
    payload = _metadata_payload(document)
    section = _safe_metadata_text(document.get("section"), max_length=96)
    key = _safe_metadata_text(document.get("key"), max_length=120)
    return _with_live_metadata_record_id({
        "section": section,
        "key": key,
        "display_name": _first_safe_metadata_text(
            payload.get("display_name"), document.get("display_name"), key, max_length=160
        ),
        "status": _safe_metadata_status(document.get("status")),
    }, document)


_LIVE_METADATA_PROJECTORS: Mapping[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "domain": _project_live_domain,
    "table_catalog": _project_live_table_catalog,
    "main_flow_filters": _project_live_main_flow_filter,
}


def _empty_live_metadata_type_status(
    settings: LiveMetadataReadSettings,
    metadata_type: str,
    *,
    read_status: str,
    message: str,
) -> dict[str, Any]:
    return {
        "label": _METADATA_TYPE_LABELS[metadata_type],
        "collection": settings.collection_for(metadata_type) or None,
        "collection_source": settings.collection_source_for(metadata_type) or None,
        "read_status": read_status,
        "live": False,
        "count": 0,
        "returned_count": 0,
        "truncated": False,
        "message": message,
    }


def _live_metadata_disabled_response(settings: LiveMetadataReadSettings) -> dict[str, Any]:
    message = "실제 MongoDB 메타데이터 읽기가 비활성화되어 있습니다."
    return {
        "enabled": False,
        "read_only": True,
        "status_update": {
            "enabled": False,
            "requires_admin": True,
            "record_id_field": "_record_id",
            "allowed_values": ["active", "inactive"],
        },
        "source": {
            "database": settings.mongo_database or None,
            "item_limit": settings.item_limit,
            "mode": "disabled",
        },
        "metadata": {metadata_type: [] for metadata_type in _METADATA_TYPES},
        "metadata_types": {
            metadata_type: _empty_live_metadata_type_status(
                settings,
                metadata_type,
                read_status="disabled",
                message=message,
            )
            for metadata_type in _METADATA_TYPES
        },
    }


def _read_live_metadata(settings: LiveMetadataReadSettings) -> dict[str, Any]:
    """Read and project MongoDB metadata without modifying any collection."""

    if not settings.enabled:
        return _live_metadata_disabled_response(settings)
    if not settings.ready:
        raise MetadataLiveReadError("실제 MongoDB 메타데이터 읽기 설정이 완료되지 않았습니다.")

    reader = _get_metadata_live_reader(settings)
    metadata: dict[str, list[dict[str, Any]]] = {}
    metadata_types: dict[str, dict[str, Any]] = {}
    success_count = 0
    try:
        for metadata_type in _METADATA_TYPES:
            try:
                total_count, documents, truncated = reader.read_collection(
                    metadata_type=metadata_type,
                    collection_name=settings.collection_for(metadata_type),
                    item_limit=settings.item_limit,
                )
                projector = _LIVE_METADATA_PROJECTORS[metadata_type]
                items = [projector(document) for document in documents]
                metadata[metadata_type] = items
                metadata_types[metadata_type] = {
                    "label": _METADATA_TYPE_LABELS[metadata_type],
                    "collection": settings.collection_for(metadata_type),
                    "collection_source": settings.collection_source_for(metadata_type),
                    "read_status": "success",
                    "live": True,
                    "count": max(0, int(total_count)),
                    "returned_count": len(items),
                    "truncated": bool(truncated),
                    "message": "MongoDB 실제 등록 정보를 읽었습니다.",
                }
                success_count += 1
            except MetadataLiveReadError:
                metadata[metadata_type] = []
                metadata_types[metadata_type] = _empty_live_metadata_type_status(
                    settings,
                    metadata_type,
                    read_status="error",
                    message="이 메타데이터 컬렉션을 읽을 수 없습니다.",
                )
    finally:
        try:
            reader.close()
        except Exception:  # pragma: no cover - close must not change API output
            logger.warning("MongoDB metadata reader did not close cleanly.")

    if success_count == 0:
        raise MetadataLiveReadError("MongoDB 실제 메타데이터를 읽을 수 없습니다.")

    return {
        "enabled": True,
        "read_only": True,
        # The GET response itself remains read-only. Administrators can change
        # only the exact record's active/inactive state through the separate
        # status endpoint when this live source is explicitly configured.
        "status_update": {
            "enabled": True,
            "requires_admin": True,
            "record_id_field": "_record_id",
            "allowed_values": ["active", "inactive"],
        },
        "source": {
            "database": settings.mongo_database,
            "item_limit": settings.item_limit,
            "mode": "configured",
        },
        "metadata": metadata,
        "metadata_types": metadata_types,
    }


def _read_live_metadata_detail(
    settings: LiveMetadataReadSettings,
    *,
    metadata_type: str,
    record_token: str,
) -> dict[str, Any] | None:
    """Read one exact live metadata document and expose its safe Flow fields.

    The function intentionally validates the browser token before creating a
    MongoDB client.  It never accepts a filter or projection from the caller,
    and it returns ``None`` only when the exact configured ``_id`` was absent.
    """

    if metadata_type not in _METADATA_TYPES:
        raise ValueError("unknown metadata type")
    if not settings.enabled or not settings.ready:
        raise MetadataLiveReadError("실제 MongoDB 메타데이터 상세 조회 설정이 완료되지 않았습니다.")

    collection_name = settings.collection_for(metadata_type)
    portal_collections = _portal_mongodb_collection_settings_from_env()
    if not _valid_live_metadata_collection_name(
        collection_name,
        reserved_collections=portal_collections.all_collections,
    ):
        raise MetadataLiveReadError("메타데이터 컬렉션 이름 설정을 확인해 주세요.")

    record_id = _decode_metadata_record_id(record_token)
    reader = _get_metadata_live_detail_reader(settings)
    try:
        document = reader.read_document(
            metadata_type=metadata_type,
            collection_name=collection_name,
            record_id=record_id,
        )
    finally:
        try:
            reader.close()
        except Exception:  # pragma: no cover - close must not change API output
            logger.warning("MongoDB metadata detail reader did not close cleanly.")

    if document is None:
        return None

    return {
        "metadata_type": metadata_type,
        "metadata_label": _METADATA_TYPE_LABELS[metadata_type],
        "record_id": record_token,
        "source": {
            "mode": "configured",
            "collection": collection_name,
        },
        "item": _live_metadata_detail_item(metadata_type, document),
    }


def _update_live_metadata_record_status(
    settings: LiveMetadataReadSettings,
    *,
    metadata_type: str,
    record_token: str,
    status_value: Literal["active", "inactive"],
) -> bool:
    """Set one already-rendered live metadata document's active state.

    This function deliberately has no preview persistence path. It can only
    use a configured live source, maps the caller's metadata type to the one
    environment-selected collection, and delegates an exact ``_id`` match to
    the narrow status-updater boundary. It never sends a browser-supplied
    MongoDB update expression to the database.
    """

    if metadata_type not in _METADATA_TYPES:
        raise ValueError("unknown metadata type")
    if status_value not in {"active", "inactive"}:
        raise ValueError("invalid metadata status")
    if not settings.enabled or not settings.ready:
        raise MetadataLiveMutationError("실제 MongoDB 메타데이터 상태 변경 설정이 완료되지 않았습니다.")

    collection_name = settings.collection_for(metadata_type)
    portal_collections = _portal_mongodb_collection_settings_from_env()
    if not _valid_live_metadata_collection_name(
        collection_name,
        reserved_collections=portal_collections.all_collections,
    ):
        raise MetadataLiveMutationError("메타데이터 컬렉션 이름 설정을 확인해 주세요.")

    record_id = _decode_metadata_record_id(record_token)
    status_updater = _get_metadata_live_status_updater(settings)
    try:
        return bool(
            status_updater.update_document_status(
                metadata_type=metadata_type,
                collection_name=collection_name,
                record_id=record_id,
                status_value=status_value,
            )
        )
    finally:
        try:
            status_updater.close()
        except Exception:  # pragma: no cover - close must not change API output
            logger.warning("MongoDB metadata status updater did not close cleanly.")


def _metadata_api_headers(
    settings: MetadataAuthoringSettings,
    *,
    gaia_api_caller_employee_id: str = "",
) -> dict[str, str]:
    """Build external API headers without exposing or sourcing secrets from the UI.

    The GAIA caller employee ID comes from the administrator-managed portal
    settings document.  It is intentionally not read from `.env`.
    """

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        **settings.extra_headers,
    }
    if settings.auth_key and settings.auth_header:
        headers.setdefault(settings.auth_header, settings.auth_key)
    if gaia_api_caller_employee_id:
        headers.setdefault(_GAIA_CALLER_ID_HEADER, gaia_api_caller_employee_id)
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


def _metadata_api_status(
    settings: MetadataAuthoringSettings,
    *,
    gaia_api_caller_employee_id: str = "",
    live_read_settings: LiveMetadataReadSettings | None = None,
) -> dict[str, Any]:
    """Describe metadata API and optional live-read configuration safely.

    The portal has two deliberately separate MongoDB roles:

    * Portal settings/audit collections are used by this FastAPI app.
    * declared schedule collections are reserved for future scheduler storage.
    * the three metadata collections belong to the external Langflow Flows.

    This function does not open a Flow metadata collection or claim that a
    listed collection exists.  It only reports whether the separate,
    administrator-configured read-only endpoint is available.
    """

    live_read_settings = live_read_settings or _live_metadata_read_settings_from_env(settings)
    portal_reads_metadata_collections = bool(
        live_read_settings.enabled and live_read_settings.ready
    )

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

    metadata_types: dict[str, dict[str, Any]] = {}
    for metadata_type in _METADATA_TYPES:
        endpoint_ready = bool(
            settings.mode == "api"
            and base_api_configuration_ready
            and endpoint_configured[metadata_type]
        )
        preview_ready = bool(
            settings.mode == "preview" and not settings.configuration_errors
        )
        writer_tweak_configured = bool(
            settings.payload_mode == "langflow"
            and settings.send_mongodb_tweaks
            and settings.mongo_uri
            and settings.mongo_database
            and settings.component_for(metadata_type, "writer")
        )
        snapshot_tweak_configured = bool(
            settings.payload_mode == "langflow"
            and settings.send_mongodb_tweaks
            and settings.mongo_uri
            and settings.mongo_database
            and settings.component_for(metadata_type, "snapshot_loader")
        )
        portal_will_send_writer_tweak = bool(endpoint_ready and writer_tweak_configured)
        portal_will_send_snapshot_tweak = bool(endpoint_ready and snapshot_tweak_configured)

        metadata_types[metadata_type] = {
            "label": _METADATA_TYPE_LABELS[metadata_type],
            "endpoint_configured": endpoint_configured[metadata_type],
            "endpoint_source": settings.endpoint_source_for(metadata_type),
            # ``endpoint_ready`` only means the portal has enough information
            # to invoke this Flow. It is not a successful upstream health check.
            "endpoint_ready": endpoint_ready,
            "current_mode_ready": preview_ready if settings.mode == "preview" else endpoint_ready,
            "portal_configured_collection_name": settings.collection_for(metadata_type),
            "collection_name_source": settings.collection_source_for(metadata_type),
            # This name is known only when the portal will pass it to the
            # Langflow writer. When tweaks are disabled, the Flow may use an
            # entirely separate deployment-time MongoDB setting.
            "expected_flow_collection_name": (
                settings.collection_for(metadata_type)
                if writer_tweak_configured
                else None
            ),
            "expected_flow_collection_basis": (
                "portal_writer_tweak"
                if writer_tweak_configured
                else "external_flow_runtime_not_observed"
            ),
            "writer_tweak_configured": writer_tweak_configured,
            "writer_tweak_will_be_sent": portal_will_send_writer_tweak,
            "snapshot_tweak_configured": snapshot_tweak_configured,
            "snapshot_tweak_will_be_sent": portal_will_send_snapshot_tweak,
            # A configured endpoint and a calculated collection name are not
            # evidence that the collection exists or that its contents match
            # the static preview table.
            "live_contents_checked": False,
        }

    flow_metadata_mongodb = {
        "role": "external_flow_metadata_configuration",
        # Compatibility fields retained for older Portal UI clients.
        "uri_configured": bool(settings.mongo_uri),
        "portal_mongodb_configuration_present": bool(
            settings.mongo_uri and settings.mongo_database
        ),
        "database": settings.mongo_database or None,
        "collection_prefix": settings.mongo_collection_prefix or None,
        "tweaks_enabled": settings.send_mongodb_tweaks,
        "payload_mode": settings.payload_mode,
        "portal_reads_metadata_collections": portal_reads_metadata_collections,
        "live_metadata_contents_checked": False,
        "contents_status": (
            "available_via_metadata_live_api"
            if portal_reads_metadata_collections
            else "not_checked_by_portal"
        ),
        "message": (
            "실제 등록 정보는 관리자 전용 /api/metadata/live에서 읽을 수 있습니다."
            if portal_reads_metadata_collections
            else "현재 Portal은 메타데이터 컬렉션 내용을 직접 읽지 않습니다. "
            "표시 중인 기본 메타데이터 목록은 미리보기 예시입니다."
        ),
        "collections": {
            metadata_type: settings.collection_for(metadata_type)
            for metadata_type in _METADATA_TYPES
        },
        "writer_tweaks_configured": bool(
            settings.payload_mode == "langflow"
            and settings.send_mongodb_tweaks
            and settings.mongo_uri
            and settings.mongo_database
        ),
    }

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
            "gaia_api_caller_employee_id_configured": bool(gaia_api_caller_employee_id),
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
        "metadata_types": metadata_types,
        "live_metadata_read": {
            "enabled": live_read_settings.enabled,
            "ready": live_read_settings.ready,
            "mode": live_read_settings.mode,
            "database": live_read_settings.mongo_database or None,
            "item_limit": live_read_settings.item_limit,
            "configuration_errors": list(live_read_settings.configuration_errors),
            "collections": {
                metadata_type: {
                    "name": live_read_settings.collection_for(metadata_type) or None,
                    "source": live_read_settings.collection_source_for(metadata_type) or None,
                }
                for metadata_type in _METADATA_TYPES
            },
        },
        # Keep the existing key for callers already using it, but make the
        # semantic role explicit. ``flow_metadata_mongodb`` is the preferred
        # new name for the UI.
        "mongodb": copy.deepcopy(flow_metadata_mongodb),
        "flow_metadata_mongodb": flow_metadata_mongodb,
    }


def _portal_settings_mongodb_status(
    settings: MetadataAuthoringSettings,
    *,
    persistent: bool,
) -> dict[str, Any]:
    """Return the verified storage role of the portal's own MongoDB store.

    ``persistent`` is true only after the request has successfully read the
    configured settings document through ``MongoPortalSettingsStore``.
    This is intentionally independent of Langflow metadata collection usage.
    """

    collections = _portal_mongodb_collection_settings_from_env()
    mongo_configured = bool(settings.mongo_uri and settings.mongo_database)
    mongo_read_verified = bool(persistent and mongo_configured and collections.ready)
    return {
        "role": "portal_settings_and_audit",
        "configured": bool(mongo_configured and collections.ready),
        "backend": "mongodb" if mongo_read_verified else "preview_defaults",
        "connection_read_verified": mongo_read_verified,
        "database": settings.mongo_database or None,
        "settings_collection": collections.settings_collection if mongo_read_verified else None,
        "audit_collection": collections.audit_collection if mongo_read_verified else None,
        "collection_configuration_errors": list(collections.configuration_errors),
        "reads_flow_metadata_collections": False,
        "message": (
            "Portal 설정과 변경 이력 저장소입니다. "
            "도메인·테이블 카탈로그·메인 필터 메타데이터를 읽는 연결과는 별개입니다."
            if mongo_read_verified
            else "Portal MongoDB 컬렉션 이름 설정을 확인해 주세요."
            if mongo_configured and not collections.ready
            else "MongoDB 설정이 없어 Portal 설정은 미리보기 기본값으로만 동작합니다."
        ),
    }


def _portal_schedule_mongodb_status(
    settings: MetadataAuthoringSettings,
) -> dict[str, Any]:
    """Describe declared schedule storage without claiming CRUD is available.

    The collection names are visible to administrators so they can configure
    MongoDB before the separate scheduler service is introduced.  This Portal
    does not open, read, write, or create either collection at this stage.
    """

    collections = _portal_mongodb_collection_settings_from_env()
    mongo_connection_configured = bool(settings.mongo_uri and settings.mongo_database)
    return {
        "role": "schedule_authoring_and_run_history",
        "configured": bool(mongo_connection_configured and collections.ready),
        "storage_enabled": False,
        "storage_status": "not_implemented",
        "database": settings.mongo_database or None,
        "schedule_collection": (
            collections.schedule_collection if collections.ready else None
        ),
        "schedule_run_collection": (
            collections.schedule_run_collection if collections.ready else None
        ),
        "collection_configuration_errors": list(collections.configuration_errors),
        "message": (
            "스케줄 컬렉션 이름은 준비되어 있지만, 현재 Portal의 스케줄 등록·수정·삭제는 "
            "더미 화면 상태입니다. MongoDB 저장과 Scheduler Worker 실행은 아직 활성화되지 않았습니다."
        ),
    }


class PortalMongoConnectionError(RuntimeError):
    """A safe error raised when the Portal MongoDB connectivity check fails."""


class PortalMongoConnectionProbe(Protocol):
    """Minimal, read-only MongoDB probe used by the administrator status view."""

    def ping(self, *, uri: str, database: str) -> None:
        ...


class PyMongoPortalConnectionProbe:
    """Perform one bounded MongoDB ``ping`` without reading or writing documents."""

    def ping(self, *, uri: str, database: str) -> None:
        # ``database`` is intentionally accepted at this boundary even though
        # the MongoDB ``ping`` command runs against ``admin``.  It keeps the
        # probe contract aligned with the configured Portal storage target and
        # makes the dependency easy to replace in tests.
        del database
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise PortalMongoConnectionError(
                "MongoDB 연결 상태를 확인하려면 pymongo 패키지가 필요합니다."
            ) from exc

        client: Any | None = None
        try:
            client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
                socketTimeoutMS=3_000,
            )
            # A command is needed because MongoClient construction is lazy.
            # This is a server-side health check only; it does not alter data.
            client.admin.command("ping")
        except PyMongoError as exc:
            raise PortalMongoConnectionError(
                "MongoDB 연결을 확인할 수 없습니다."
            ) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # pragma: no cover - cleanup must not mask status
                    pass


# Tests replace this one small boundary, so no real MongoDB connection is
# needed to verify each configuration and error state.
_portal_mongodb_connection_probe_factory: Callable[[], PortalMongoConnectionProbe] | None = None


def _portal_mongodb_connection_status(
    settings: MetadataAuthoringSettings,
) -> dict[str, Any]:
    """Return a safe, bounded read-only connection state for Portal MongoDB.

    This intentionally runs independently from Flow API readiness and catches
    all probe failures.  The administrator status endpoint can therefore show
    a connection problem rather than failing because an optional health check
    could not reach MongoDB.  URI and credentials never enter this response or
    the log message.
    """

    uri = settings.mongo_uri
    database = settings.mongo_database
    if not uri and not database:
        return {
            "configured": False,
            "connected": False,
            "database": None,
            "status": "disabled",
            "message": "MongoDB 연결 정보가 없어 연결 상태 확인이 비활성화되어 있습니다.",
        }
    if not uri or not database:
        return {
            "configured": False,
            "connected": False,
            "database": database or None,
            "status": "not_configured",
            "message": "MongoDB 연결 정보가 완전하지 않아 연결할 수 없습니다.",
        }

    try:
        probe = (
            _portal_mongodb_connection_probe_factory()
            if _portal_mongodb_connection_probe_factory is not None
            else PyMongoPortalConnectionProbe()
        )
        probe.ping(uri=uri, database=database)
    except PortalMongoConnectionError:
        return {
            "configured": True,
            "connected": False,
            "database": database,
            "status": "connection_error",
            "message": "MongoDB 연결을 확인할 수 없습니다. 연결 정보와 네트워크를 확인해 주세요.",
        }
    except Exception:  # pragma: no cover - defensive adapter isolation
        # Do not log the exception detail here: driver exceptions can include
        # a server address or a URI fragment supplied in the configuration.
        logger.warning("Portal MongoDB connection status probe failed unexpectedly.")
        return {
            "configured": True,
            "connected": False,
            "database": database,
            "status": "connection_error",
            "message": "MongoDB 연결을 확인할 수 없습니다. 연결 정보와 네트워크를 확인해 주세요.",
        }

    return {
        "configured": True,
        "connected": True,
        "database": database,
        "status": "connected",
        "message": "MongoDB 연결을 확인했습니다.",
    }


_DEFAULT_PORTAL_ADMINISTRATORS = [
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
]


def _default_portal_settings() -> dict[str, Any]:
    """Return a new settings document suitable for preview or MongoDB seeding."""

    return {
        "gaia_api_caller_employee_id": "",
        "usage_policy": {
            "history_window_days": 21,
            "active_user_min_distinct_days": 3,
            "active_user_min_chat_count": 10,
        },
        "admins": copy.deepcopy(_DEFAULT_PORTAL_ADMINISTRATORS),
        "updated_at": None,
        "updated_by": None,
    }


def _normalise_portal_settings(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only the safe, known shape when reading MongoDB settings."""

    settings = _default_portal_settings()
    if not isinstance(document, Mapping):
        return settings

    settings["gaia_api_caller_employee_id"] = str(
        document.get("gaia_api_caller_employee_id") or ""
    ).strip()

    stored_policy = document.get("usage_policy")
    if isinstance(stored_policy, Mapping):
        for key in (
            "history_window_days",
            "active_user_min_distinct_days",
            "active_user_min_chat_count",
        ):
            value = stored_policy.get(key)
            if isinstance(value, int) and value > 0:
                settings["usage_policy"][key] = value

    stored_admins = document.get("admins")
    if isinstance(stored_admins, list):
        admins: list[dict[str, str]] = []
        for item in stored_admins:
            if not isinstance(item, Mapping):
                continue
            employee_id = str(item.get("employee_id") or "").strip()
            if not employee_id:
                continue
            admins.append(
                {
                    "employee_id": employee_id,
                    "name": str(item.get("name") or "").strip() or employee_id,
                    "role": str(item.get("role") or "관리자").strip(),
                    "scope": str(item.get("scope") or "관리자 권한").strip(),
                    "status": str(item.get("status") or "활성").strip(),
                }
            )
        if admins:
            settings["admins"] = admins

    for key in ("updated_at", "updated_by"):
        value = document.get(key)
        if value is not None:
            settings[key] = copy.deepcopy(value)
    return settings


@dataclass(frozen=True)
class PortalViewer:
    """A verified portal identity plus its server-side administrator result."""

    employee_id: str
    name: str
    is_admin: bool

    def as_audit_actor(self) -> dict[str, str]:
        return {"employee_id": self.employee_id, "name": self.name}


@dataclass(frozen=True)
class PortalAccess:
    viewer: PortalViewer
    store: "PortalSettingsStore"
    settings: dict[str, Any]


class PortalSettingsStoreError(RuntimeError):
    """Raised when the portal cannot safely read or write its settings store."""


class PortalSettingsStore(Protocol):
    """Small MongoDB boundary so tests do not need a live database."""

    persistent: bool

    def read(self) -> dict[str, Any]:
        ...

    def update(self, update: Mapping[str, Any], actor: PortalViewer) -> dict[str, Any]:
        ...

    def record_audit(
        self,
        action: str,
        actor: PortalViewer,
        details: Mapping[str, Any],
    ) -> None:
        ...


class PreviewPortalSettingsStore:
    """Read-only defaults used only when MongoDB is intentionally not configured."""

    persistent = False

    def read(self) -> dict[str, Any]:
        return _default_portal_settings()

    def update(self, update: Mapping[str, Any], actor: PortalViewer) -> dict[str, Any]:
        raise PortalSettingsStoreError(
            "MongoDB 설정이 없어 관리자 변경 값을 저장할 수 없습니다."
        )

    def record_audit(
        self,
        action: str,
        actor: PortalViewer,
        details: Mapping[str, Any],
    ) -> None:
        # Preview intentionally does not pretend to persist audit records.
        return None


class MongoPortalSettingsStore:
    """Persist Portal settings and audit records in configured MongoDB collections."""

    persistent = True

    def __init__(
        self,
        *,
        uri: str,
        database: str,
        collections: PortalMongoCollectionSettings,
    ) -> None:
        if not collections.ready:
            raise PortalSettingsStoreError(
                "Portal MongoDB 컬렉션 이름 설정을 확인해 주세요."
            )
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise PortalSettingsStoreError(
                "MongoDB 설정 저장을 위해 pymongo 패키지가 필요합니다."
            ) from exc

        self._mongo_error = PyMongoError
        try:
            self._client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
            )
            database_handle = self._client[database]
            self._settings = database_handle[collections.settings_collection]
            self._audit = database_handle[collections.audit_collection]
        except PyMongoError as exc:
            raise PortalSettingsStoreError(
                "MongoDB 관리자 설정 저장소를 초기화할 수 없습니다."
            ) from exc

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._mongo_error as exc:
            raise PortalSettingsStoreError(
                "MongoDB 관리자 설정 저장소에 연결할 수 없습니다."
            ) from exc

    def read(self) -> dict[str, Any]:
        document = self._run(
            lambda: self._settings.find_one({"_id": _PORTAL_SETTINGS_DOCUMENT_ID})
        )
        return _normalise_portal_settings(document)

    def update(self, update: Mapping[str, Any], actor: PortalViewer) -> dict[str, Any]:
        before = self.read()
        after = copy.deepcopy(before)
        if "gaia_api_caller_employee_id" in update:
            after["gaia_api_caller_employee_id"] = str(
                update.get("gaia_api_caller_employee_id") or ""
            ).strip()

        policy_update = update.get("usage_policy")
        if isinstance(policy_update, Mapping):
            for key in ("active_user_min_distinct_days", "active_user_min_chat_count"):
                value = policy_update.get(key)
                if isinstance(value, int) and value > 0:
                    after["usage_policy"][key] = value

        after["updated_at"] = datetime.now(timezone.utc).isoformat()
        after["updated_by"] = actor.as_audit_actor()
        document = {"_id": _PORTAL_SETTINGS_DOCUMENT_ID, **after}
        self._run(
            lambda: self._settings.replace_one(
                {"_id": _PORTAL_SETTINGS_DOCUMENT_ID}, document, upsert=True
            )
        )
        self.record_audit(
            "admin_settings_updated",
            actor,
            {
                "before": {
                    "gaia_api_caller_employee_id": before["gaia_api_caller_employee_id"],
                    "usage_policy": before["usage_policy"],
                },
                "after": {
                    "gaia_api_caller_employee_id": after["gaia_api_caller_employee_id"],
                    "usage_policy": after["usage_policy"],
                },
            },
        )
        return after

    def record_audit(
        self,
        action: str,
        actor: PortalViewer,
        details: Mapping[str, Any],
    ) -> None:
        record = {
            "action": action,
            "actor": actor.as_audit_actor(),
            "details": copy.deepcopy(dict(details)),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._run(lambda: self._audit.insert_one(record))


_portal_settings_store_factory: Callable[[], PortalSettingsStore] | None = None


def _get_portal_settings_store() -> PortalSettingsStore:
    """Return a Mongo-backed store only when both MongoDB connection values exist."""

    if _portal_settings_store_factory is not None:
        return _portal_settings_store_factory()

    settings = _metadata_settings_from_env()
    if settings.mongo_uri and settings.mongo_database:
        return MongoPortalSettingsStore(
            uri=settings.mongo_uri,
            database=settings.mongo_database,
            collections=_portal_mongodb_collection_settings_from_env(),
        )
    return PreviewPortalSettingsStore()


def _active_admin(settings: Mapping[str, Any], employee_id: str) -> dict[str, str] | None:
    admins = settings.get("admins")
    if not isinstance(admins, list):
        return None
    for admin in admins:
        if not isinstance(admin, Mapping):
            continue
        if str(admin.get("employee_id") or "").strip() != employee_id:
            continue
        if str(admin.get("status") or "").strip() != "활성":
            continue
        return {
            "employee_id": employee_id,
            "name": str(admin.get("name") or employee_id),
            "role": str(admin.get("role") or "관리자"),
            "scope": str(admin.get("scope") or "관리자 권한"),
            "status": "활성",
        }
    return None


def _portal_access(request: Request) -> PortalAccess:
    """Resolve a request identity and server-side administrator permission.

    The temporary header adapter exists for the current preview.  In production
    the same headers must be injected by a trusted SSO/proxy layer, which must
    strip user-supplied copies before the request reaches this application.
    """

    employee_id = str(request.headers.get(_PORTAL_EMPLOYEE_ID_HEADER) or "").strip()
    if not employee_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "portal_identity_required",
                "message": "로그인 사용자 사번 정보를 확인할 수 없습니다.",
            },
        )

    try:
        store = _get_portal_settings_store()
        settings = store.read()
    except PortalSettingsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "portal_settings_unavailable", "message": str(exc)},
        ) from exc

    admin = _active_admin(settings, employee_id)
    supplied_name = str(request.headers.get(_PORTAL_EMPLOYEE_NAME_HEADER) or "").strip()
    viewer = PortalViewer(
        employee_id=employee_id,
        name=str((admin or {}).get("name") or supplied_name or employee_id),
        is_admin=admin is not None,
    )
    return PortalAccess(viewer=viewer, store=store, settings=settings)


def _require_active_admin(request: Request) -> PortalAccess:
    access = _portal_access(request)
    if not access.viewer.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "admin_required",
                "message": "관리자만 등록하거나 설정을 변경할 수 있습니다.",
            },
        )
    return access


def _require_active_admin_for_status(request: Request) -> PortalAccess:
    """Authorize the read-only status view even when Portal MongoDB is down.

    The normal administrator path always reads the persisted admin list.  The
    status view is the one place where that dependency would hide the very
    MongoDB outage an operator needs to see.  For this read-only endpoint only,
    a trusted proxy identity may fall back to the fixed bootstrap admins.  This
    never grants access to settings changes, metadata authoring, or live
    metadata documents.
    """

    try:
        return _require_active_admin(request)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        if (
            exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE
            or detail.get("code") != "portal_settings_unavailable"
        ):
            raise

        employee_id = str(request.headers.get(_PORTAL_EMPLOYEE_ID_HEADER) or "").strip()
        bootstrap_settings = _default_portal_settings()
        bootstrap_admin = _active_admin(bootstrap_settings, employee_id)
        if bootstrap_admin is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "admin_required",
                    "message": "관리자만 등록하거나 설정을 변경할 수 있습니다.",
                },
            ) from exc

        supplied_name = str(request.headers.get(_PORTAL_EMPLOYEE_NAME_HEADER) or "").strip()
        return PortalAccess(
            viewer=PortalViewer(
                employee_id=employee_id,
                name=str(bootstrap_admin.get("name") or supplied_name or employee_id),
                is_admin=True,
            ),
            store=PreviewPortalSettingsStore(),
            settings=bootstrap_settings,
        )


def _admin_settings_response(
    settings: Mapping[str, Any],
    *,
    persistent: bool,
) -> dict[str, Any]:
    """Return administrator-managed values without API keys or MongoDB secrets."""

    normalized = _normalise_portal_settings(settings)
    collections = _portal_mongodb_collection_settings_from_env()
    return {
        "gaia_api_caller_employee_id": normalized["gaia_api_caller_employee_id"],
        "usage_policy": normalized["usage_policy"],
        "admins": normalized["admins"],
        "updated_at": normalized["updated_at"],
        "updated_by": normalized["updated_by"],
        "storage": {
            "persistent": persistent,
            "collection": (
                collections.settings_collection if persistent and collections.ready else None
            ),
        },
    }


_metadata_http_client: MetadataApiClient = UrlLibMetadataApiClient()

def create_app() -> FastAPI:
    """Create the portal application for both production and local Uvicorn runs."""
    portal = FastAPI(
        title="PTMORE PKG Agent Portal",
        description="Portal preview with an optional external metadata authoring API adapter.",
        version="0.2.0-preview",
    )
    portal.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
    return portal


# Both names are intentional: "application" matches the fixed production
# command, while "app" supports normal Uvicorn import syntax for local tests.
application = create_app()
app = application


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
    usage_history: list[dict[str, str]],
    usage_policy: Mapping[str, Any],
    *,
    start_day: date | None = None,
    end_day: date | None = None,
) -> dict[str, Any]:
    """Aggregate user/question/date history for both preview and Phoenix.

    Supplying an inclusive ``start_day`` / ``end_day`` zero-fills the graph.
    That is used by the live Phoenix path so a quiet day remains visible in
    the required recent-three-week dashboard rather than disappearing.
    """

    if (start_day is None) != (end_day is None):
        raise ValueError("start_day and end_day must be supplied together")
    if start_day is not None and end_day is not None and start_day > end_day:
        raise ValueError("start_day must not be later than end_day")

    day_records: dict[str, list[dict[str, str]]] = {}
    if start_day is not None and end_day is not None:
        current_day = start_day
        while current_day <= end_day:
            day_records[current_day.isoformat()] = []
            current_day += timedelta(days=1)

    user_activity: dict[str, dict[str, Any]] = {}
    channel_counts: dict[str, int] = {}

    for record in usage_history:
        record_date = str(record.get("date") or "").strip()
        if start_day is not None and end_day is not None:
            try:
                parsed_record_day = date.fromisoformat(record_date)
            except ValueError:
                continue
            if parsed_record_day < start_day or parsed_record_day > end_day:
                continue

        day_records.setdefault(record_date, []).append(record)
        activity = user_activity.setdefault(
            record["employee_id"],
            {
                "employee_id": record["employee_id"],
                "user_name": record["user_name"],
                "distinct_dates": set(),
                "chat_count": 0,
            },
        )
        activity["distinct_dates"].add(record_date)
        activity["chat_count"] += 1
        channel = str(record.get("channel") or record.get("platform") or "미분류").strip() or "미분류"
        channel_counts[channel] = channel_counts.get(channel, 0) + 1

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

    preferred_channels = ("CUBE", "CUBE_SCHEDULING", "ADMIN_TEST")
    mix_order = [
        channel for channel in preferred_channels if channel in channel_counts
    ]
    mix_order.extend(
        sorted(channel for channel in channel_counts if channel not in mix_order)
    )
    mix_colors = ("#2563eb", "#14b8a6", "#f59e0b", "#8b5cf6", "#ec4899")
    channel_mix = []
    remaining = 100
    for index, channel in enumerate(mix_order):
        color = mix_colors[index % len(mix_colors)]
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
                "target": _SCHEDULE_DELIVERY_TARGET,
            },
            {
                "time": "09:15",
                "name": "WIP 이상 LOT 알림",
                "owner": "2071044",
                "status": "성공",
                "target": _SCHEDULE_DELIVERY_TARGET,
            },
            {
                "time": "09:00",
                "name": "설비 DOWN 현황",
                "owner": "2093012",
                "status": "재시도 예정",
                "target": _SCHEDULE_DELIVERY_TARGET,
            },
            {
                "time": "08:30",
                "name": "일일 수율 요약",
                "owner": "2069026",
                "status": "성공",
                "target": _SCHEDULE_DELIVERY_TARGET,
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
        "dataset_key: production_today\n"
        "표시명: Production Today\n"
        "분류: production\n"
        "source_type: oracle\n"
        "db_key: PNT_RPT\n\n"
        "query_template:\n"
        "SELECT\n"
        "  WORK_DATE,\n"
        "  OPER_NAME,\n"
        "  PRODUCTION\n"
        "FROM PROD_TABLE\n"
        "WHERE WORK_DATE = {DATE}\n"
        "  AND OPER_NAME = {PROCESS_GROUP}\n\n"
        "columns:\n"
        "- WORK_DATE\n"
        "- OPER_NAME\n"
        "- PRODUCTION\n\n"
        "required_params:\n"
        "- DATE\n"
        "- PROCESS_GROUP\n\n"
        "required_param_mappings:\n"
        "- DATE -> WORK_DATE\n"
        "- PROCESS_GROUP -> OPER_NAME\n\n"
        "filter_mappings:\n"
        "- DATE -> WORK_DATE\n"
        "- PROCESS_GROUP -> OPER_NAME\n\n"
        "metric_semantics:\n"
        "- PRODUCTION: sum"
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
            "dataset_key=production_today인 생산 데이터셋을 등록한다.\n"
            "source_config:\n"
            "  source_type=oracle\n"
            "  db_key=PNT_RPT\n"
            "  query_template:\n"
            "    SELECT\n"
            "      WORK_DATE,\n"
            "      OPER_NAME,\n"
            "      PRODUCTION\n"
            "    FROM PROD_TABLE\n"
            "    WHERE WORK_DATE = {DATE}\n"
            "      AND OPER_NAME = {PROCESS_GROUP}\n"
            "columns=[WORK_DATE, OPER_NAME, PRODUCTION]\n"
            "required_param_mappings={DATE:[WORK_DATE], PROCESS_GROUP:[OPER_NAME]}\n"
            "filter_mappings={DATE:[WORK_DATE], PROCESS_GROUP:[OPER_NAME]}"
        ),
        columns=["데이터셋 키", "데이터셋", "분류", "연결 방식", "필수 조건", "상태"],
        rows=[
            {
                "데이터셋 키": "production_today",
                "데이터셋": "Production Today",
                "분류": "생산",
                "연결 방식": "Oracle",
                "필수 조건": "DATE, PROCESS_GROUP",
                "상태": "저장 예정",
            }
        ],
        keys=["production_today"],
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
                "target": _SCHEDULE_DELIVERY_TARGET,
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
                "target": _SCHEDULE_DELIVERY_TARGET,
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
                "target": _SCHEDULE_DELIVERY_TARGET,
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
                "target": _SCHEDULE_DELIVERY_TARGET,
                "owner": "2069026",
                "status": "활성",
                "last_run": "오늘 08:00 · 성공",
            },
            {
                # Interval schedules are preview-only data.  The separate
                # scheduler service will later interpret these optional
                # fields; this portal does not schedule or persist anything.
                "id": "SCH-2026-097",
                "title": "DA 공정 실시간 생산 분석",
                "question": "DA 공정 실시간 생산 분석을 진행해줘.",
                "repeat": "10분마다",
                "time": "08:00",
                "interval_minutes": 10,
                "start_time": "08:00",
                "end_time": "18:00",
                "rule_label": "10분마다 · 08:00 ~ 18:00",
                "next_run": "오늘 10:10",
                "target": _SCHEDULE_DELIVERY_TARGET,
                "owner": "2069026",
                "status": "활성",
                "last_run": "오늘 10:00 · 성공",
            },
        ],
        "metadata": {
            "table_catalog": [
                {
                    "dataset_key": "production_today",
                    "display_name": "Production Today",
                    "dataset_family": "생산",
                    "source_type": "oracle",
                    "required_params": ["DATE", "PROCESS_GROUP"],
                    "status": "활성",
                    "payload": {
                        "display_name": "Production Today",
                        "dataset_family": "생산",
                        "source_type": "oracle",
                        "source_config": {
                            "source_type": "oracle",
                            "db_key": "PNT_RPT",
                            "query_template": (
                                "SELECT WORK_DATE, OPER_NAME, PRODUCTION "
                                "FROM PROD_TABLE WHERE WORK_DATE = {DATE}"
                            ),
                        },
                        "required_params": ["DATE", "PROCESS_GROUP"],
                        "required_param_mappings": {
                            "DATE": ["WORK_DATE"],
                            "PROCESS_GROUP": ["OPER_NAME"],
                        },
                        "filter_mappings": {
                            "DATE": ["WORK_DATE"],
                            "PROCESS_GROUP": ["OPER_NAME"],
                        },
                        "columns": [
                            {"name": "WORK_DATE", "data_type": "date"},
                            {"name": "OPER_NAME", "data_type": "string"},
                            {"name": "PRODUCTION", "data_type": "number"},
                        ],
                        "default_detail_columns": [
                            "WORK_DATE",
                            "OPER_NAME",
                            "PRODUCTION",
                        ],
                        "metric_semantics": {
                            "PRODUCTION": {"aggregation": "sum", "label": "생산 수량"}
                        },
                    },
                },
                {
                    "dataset_key": "wip_today",
                    "display_name": "WIP Today",
                    "dataset_family": "재공",
                    "source_type": "oracle",
                    "required_params": ["DATE"],
                    "status": "활성",
                    "payload": {
                        "display_name": "WIP Today",
                        "dataset_family": "재공",
                        "source_type": "oracle",
                        "source_config": {
                            "source_type": "oracle",
                            "db_key": "PNT_RPT",
                            "query_template": "SELECT WORK_DATE, OPER_NAME, WIP FROM WIP_TABLE WHERE WORK_DATE = {DATE}",
                        },
                        "required_params": ["DATE"],
                        "required_param_mappings": {"DATE": ["WORK_DATE"]},
                        "filter_mappings": {"DATE": ["WORK_DATE"], "PROCESS_GROUP": ["OPER_NAME"]},
                        "columns": [
                            {"name": "WORK_DATE", "data_type": "date"},
                            {"name": "OPER_NAME", "data_type": "string"},
                            {"name": "WIP", "data_type": "number"},
                        ],
                        "metric_semantics": {"WIP": {"aggregation": "sum", "label": "재공 수량"}},
                    },
                },
                {
                    "dataset_key": "eqp_down_list",
                    "display_name": "Equipment Down List",
                    "dataset_family": "설비",
                    "source_type": "oracle",
                    "required_params": [],
                    "status": "검토 필요",
                    "payload": {
                        "display_name": "Equipment Down List",
                        "dataset_family": "설비",
                        "source_type": "oracle",
                        "source_config": {
                            "source_type": "oracle",
                            "db_key": "GMS_DB",
                            "query_template": "SELECT EQP_ID, DOWN_REASON, DOWN_START_TIME FROM EQP_DOWN_LIST",
                        },
                        "required_params": [],
                        "filter_mappings": {"EQP_ID": ["EQP_ID"]},
                        "columns": [
                            {"name": "EQP_ID", "data_type": "string"},
                            {"name": "DOWN_REASON", "data_type": "string"},
                            {"name": "DOWN_START_TIME", "data_type": "datetime"},
                        ],
                    },
                },
                {
                    "dataset_key": "target",
                    "display_name": "PKG Target Goodocs Plan",
                    "dataset_family": "계획",
                    "source_type": "goodocs",
                    "required_params": [],
                    "status": "활성",
                    "payload": {
                        "display_name": "PKG Target Goodocs Plan",
                        "dataset_family": "계획",
                        "source_type": "goodocs",
                        "source_config": {
                            "source_type": "goodocs",
                            "doc_id": "PKG_TARGET_PLAN",
                            "sheet_name": "Target Plan",
                        },
                        "required_params": [],
                        "columns": [
                            {"name": "MONTH", "data_type": "string"},
                            {"name": "TARGET", "data_type": "number"},
                            {"name": "PLAN", "data_type": "string"},
                        ],
                    },
                },
            ],
            "main_flow_filters": [
                {
                    "filter_key": "DATE",
                    "display_name": "기준 일자",
                    "operator": "eq",
                    "value_type": "date",
                    "value_shape": "scalar",
                    "status": "활성",
                    "payload": {
                        "display_name": "기준 일자",
                        "aliases": ["오늘", "금일", "작업일"],
                        "operator": "eq",
                        "value_type": "date",
                        "value_shape": "scalar",
                        "column_candidates": ["WORK_DATE", "WORK_DT"],
                    },
                },
                {
                    "filter_key": "PROCESS_GROUP",
                    "display_name": "공정 그룹",
                    "operator": "eq",
                    "value_type": "string",
                    "value_shape": "scalar",
                    "status": "활성",
                    "payload": {
                        "display_name": "공정 그룹",
                        "aliases": ["DA", "WB", "SG"],
                        "operator": "eq",
                        "value_type": "string",
                        "value_shape": "scalar",
                        "column_candidates": ["OPER_NAME", "OPER_NM"],
                    },
                },
                {
                    "filter_key": "LOT_ID",
                    "display_name": "LOT 식별자",
                    "operator": "eq",
                    "value_type": "string",
                    "value_shape": "scalar",
                    "status": "검토 필요",
                    "payload": {
                        "display_name": "LOT 식별자",
                        "aliases": ["LOT", "로트"],
                        "operator": "eq",
                        "value_type": "string",
                        "value_shape": "scalar",
                        "column_candidates": ["LOT_ID", "LOT_NO"],
                    },
                },
            ],
            "domain": [
                {
                    "section": "process_groups",
                    "key": "DA",
                    "display_name": "DA 공정 그룹",
                    "status": "활성",
                    "payload": {
                        "display_name": "DA 공정 그룹",
                        "aliases": ["DA", "Die Attach"],
                        "field": "OPER_NAME",
                        "processes": ["DA1", "DA2", "DA3"],
                    },
                },
                {
                    "section": "quantity_terms",
                    "key": "production_qty",
                    "display_name": "생산량",
                    "status": "활성",
                    "payload": {
                        "display_name": "생산량",
                        "aliases": ["생산 수량", "투입량"],
                        "metric_semantics": {
                            "PRODUCTION": {"aggregation": "sum", "label": "생산 수량"}
                        },
                    },
                },
                {
                    "section": "analysis_recipes",
                    "key": "wip_long_stay",
                    "display_name": "장기 체류 WIP 분석",
                    "status": "활성",
                    "payload": {
                        "display_name": "장기 체류 WIP 분석",
                        "aliases": ["장기 재공", "체류 LOT"],
                        "analysis_steps": ["WIP 조회", "기준 초과 필터", "공정별 집계"],
                    },
                },
                {
                    "section": "product_key_columns",
                    "key": "product_join_key",
                    "display_name": "제품 조인 키",
                    "status": "초안",
                    "payload": {
                        "display_name": "제품 조인 키",
                        "aliases": ["제품 코드", "제품명"],
                        "columns": ["PRODUCT_ID", "PRODUCT_NAME"],
                    },
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
                "schedule_delete": "owner_or_admin",
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
            "admins": copy.deepcopy(_DEFAULT_PORTAL_ADMINISTRATORS),
        },
    }


@application.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@application.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "dummy-preview"}


@application.get("/api/admin/settings")
async def admin_settings(request: Request) -> dict[str, Any]:
    """Return non-secret portal settings to an active administrator only."""

    access = _require_active_admin(request)
    return _admin_settings_response(access.settings, persistent=access.store.persistent)


@application.put("/api/admin/settings")
def update_admin_settings(
    request_body: PortalSettingsUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    """Persist administrator-managed settings and write an audit record."""

    access = _require_active_admin(request)
    update = request_body.model_dump(exclude_unset=True)
    if not update:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_settings_update", "message": "변경할 설정을 입력해 주세요."},
        )
    try:
        updated = access.store.update(update, access.viewer)
    except PortalSettingsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "portal_settings_unavailable", "message": str(exc)},
        ) from exc
    return _admin_settings_response(updated, persistent=access.store.persistent)


@application.get("/api/metadata-authoring/status")
async def metadata_authoring_status(request: Request) -> dict[str, Any]:
    """Report safe configuration readiness without exposing API or Mongo secrets."""

    access = _require_active_admin_for_status(request)
    metadata_settings = _metadata_settings_from_env()
    live_read_settings = _live_metadata_read_settings_from_env(metadata_settings)
    response = _metadata_api_status(
        metadata_settings,
        gaia_api_caller_employee_id=str(
            access.settings.get("gaia_api_caller_employee_id") or ""
        ),
        live_read_settings=live_read_settings,
    )
    # The access check above has already performed a safe read of the portal
    # settings store. Report that storage role separately from the three
    # external Flow metadata collections.
    response["portal_settings_mongodb"] = _portal_settings_mongodb_status(
        metadata_settings,
        persistent=access.store.persistent,
    )
    # Schedule collection names are configured here for the future scheduler,
    # but this Portal intentionally does not perform schedule CRUD yet.
    response["portal_schedule_mongodb"] = _portal_schedule_mongodb_status(
        metadata_settings
    )
    # This is deliberately independent from Flow API readiness and from the
    # Portal settings-store read above.  A failed health probe is represented
    # in the response rather than turning the whole status page into an error.
    response["portal_mongodb_connection"] = _portal_mongodb_connection_status(
        metadata_settings
    )
    return response


@application.get("/api/metadata/live")
async def live_metadata(request: Request) -> dict[str, Any]:
    """Return a bounded, projected, read-only view of live metadata collections.

    This endpoint is intentionally administrator-only because even a safe
    projection reveals internal data-model names and metadata coverage.  It
    does not accept a database, collection, filter, or projection from the
    caller; all source selection remains server-side environment configuration.
    """

    _require_active_admin(request)
    metadata_settings = _metadata_settings_from_env()
    live_settings = _live_metadata_read_settings_from_env(metadata_settings)
    if live_settings.mode == "invalid" or (
        live_settings.enabled and not live_settings.ready
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_live_read_not_ready",
                "message": "실제 메타데이터 조회 설정을 확인해 주세요.",
                "missing": list(live_settings.configuration_errors),
            },
        )
    try:
        return _read_live_metadata(live_settings)
    except MetadataLiveReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_live_read_unavailable",
                "message": str(exc),
            },
        ) from exc


@application.get("/api/metadata/live/{metadata_type}/{record_id}")
def live_metadata_detail(
    metadata_type: str,
    record_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return safe JSON detail for one exact, real Flow metadata document.

    This is deliberately separate from the compact list endpoint.  It only
    accepts the opaque ``_record_id`` already issued in a live list response,
    uses the collection configured for that metadata type, and returns a
    field allowlist rather than a raw MongoDB document.
    """

    _require_active_admin(request)
    if metadata_type not in _METADATA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "unknown_metadata_type",
                "message": "지원하지 않는 메타데이터 유형입니다.",
            },
        )

    metadata_settings = _metadata_settings_from_env()
    live_settings = _live_metadata_read_settings_from_env(metadata_settings)
    if live_settings.mode != "configured" or not live_settings.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_live_detail_not_ready",
                "message": "실제 MongoDB 메타데이터 상세 조회 설정을 확인해 주세요.",
                "missing": list(live_settings.configuration_errors),
            },
        )

    try:
        detail = _read_live_metadata_detail(
            live_settings,
            metadata_type=metadata_type,
            record_token=record_id,
        )
    except InvalidMetadataRecordIdError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_metadata_record_id",
                "message": str(exc),
            },
        ) from exc
    except MetadataLiveReadError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_live_detail_unavailable",
                "message": str(exc),
            },
        ) from exc

    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "metadata_record_not_found",
                "message": "조회할 실제 메타데이터 항목을 찾지 못했습니다.",
            },
        )
    return detail


@application.patch("/api/metadata-authoring/{metadata_type}/{record_id}/status")
def update_live_metadata_record_status(
    metadata_type: str,
    record_id: str,
    request_body: MetadataStatusUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    """Change one real Flow-backed metadata record between active/inactive.

    The Portal never deletes Flow metadata. Browser preview data is not
    persistent, so this route can only operate on an explicitly configured
    live MongoDB source. The browser must pass the opaque ``_record_id``
    returned by ``/api/metadata/live`` and an exact ``active`` or ``inactive``
    status value.
    """

    _require_active_admin(request)
    if metadata_type not in _METADATA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "unknown_metadata_type",
                "message": "지원하지 않는 메타데이터 유형입니다.",
            },
        )

    metadata_settings = _metadata_settings_from_env()
    live_settings = _live_metadata_read_settings_from_env(metadata_settings)
    if live_settings.mode != "configured" or not live_settings.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_live_status_update_not_ready",
                "message": "실제 MongoDB 메타데이터 상태 변경 설정을 확인해 주세요.",
                "missing": list(live_settings.configuration_errors),
            },
        )

    try:
        updated = _update_live_metadata_record_status(
            live_settings,
            metadata_type=metadata_type,
            record_token=record_id,
            status_value=request_body.status,
        )
    except InvalidMetadataRecordIdError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_metadata_record_id",
                "message": str(exc),
            },
        ) from exc
    except MetadataLiveMutationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "metadata_live_status_update_unavailable",
                "message": str(exc),
            },
        ) from exc

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "metadata_record_not_found",
                "message": "상태를 변경할 실제 메타데이터 항목을 찾지 못했습니다.",
            },
        )

    return {
        "metadata_type": metadata_type,
        "record_id": record_id,
        "status": request_body.status,
        "status_label": _safe_metadata_status(request_body.status),
        "updated": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@application.post("/api/metadata-authoring")
def submit_metadata_authoring(
    request_body: MetadataAuthoringRequest,
    request: Request,
) -> dict[str, Any]:
    """Submit a natural-language authoring request through the configured Flow API.

    API mode never silently turns into a mock response: missing configuration,
    call failures, and an unrecognised upstream response are returned as errors.
    Preview/mock mode is the only branch that uses the current sample response.
    """

    access = _require_active_admin(request)
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
    try:
        access.store.record_audit(
            "metadata_authoring_requested",
            access.viewer,
            {
                "run_id": run_id,
                "metadata_type": request_body.metadata_type,
                "requested_dry_run": request_body.dry_run,
                "mode": settings.mode,
            },
        )
    except PortalSettingsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "portal_settings_unavailable", "message": str(exc)},
        ) from exc

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
                headers=_metadata_api_headers(
                    settings,
                    gaia_api_caller_employee_id=str(
                        access.settings.get("gaia_api_caller_employee_id") or ""
                    ),
                ),
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
        "requested_by": access.viewer.as_audit_actor(),
        "metadata_type": request_body.metadata_type,
        "preview_only": preview_only,
        "requested_dry_run": request_body.dry_run,
        "response": response,
    }


def _dashboard_source_period(dashboard: Mapping[str, Any]) -> dict[str, str]:
    """Read the inclusive graph period without depending on a UI label."""

    usage_by_day = dashboard.get("usage_by_day")
    if not isinstance(usage_by_day, list) or not usage_by_day:
        return {"start": "", "end": ""}
    first = usage_by_day[0] if isinstance(usage_by_day[0], Mapping) else {}
    last = usage_by_day[-1] if isinstance(usage_by_day[-1], Mapping) else {}
    return {
        "start": str(first.get("date") or ""),
        "end": str(last.get("date") or ""),
    }


@application.get("/api/dashboard/usage")
def dashboard_usage_data(request: Request) -> dict[str, Any]:
    """Return the 3-week dashboard plus its explicit data-source status.

    ``PTMORE_USAGE_HISTORY_MODE=phoenix`` queries Phoenix at request time.
    A missing configuration or a Phoenix failure returns a safe 503 response;
    it never substitutes preview records for a failed production request.
    """

    access = _portal_access(request)
    usage_policy = _normalise_portal_settings(access.settings)["usage_policy"]
    mode = _usage_history_mode_from_env()
    fetched_at = datetime.now(_KST).isoformat()

    if mode == "preview":
        usage_history = _build_dummy_usage_history()
        dashboard = _build_usage_dashboard(usage_history, usage_policy)
        return {
            "source": {
                "mode": "preview",
                "status": "preview",
                "label": "예시 사용 이력",
                "detail": "미리보기 모드의 예시 이력입니다. 실제 Phoenix 조회는 수행하지 않았습니다.",
                "fetched_at": fetched_at,
                "period": _dashboard_source_period(dashboard),
                "project_count": 0,
            },
            "dashboard": dashboard,
            "usage_history": usage_history,
        }

    if mode not in _USAGE_HISTORY_MODES:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_mode_invalid",
                "message": "사용 이력 조회 모드를 확인해 주세요.",
                "missing": ["PTMORE_USAGE_HISTORY_MODE"],
            },
        )

    start_day, end_day = _recent_kst_period(days=_USAGE_HISTORY_WINDOW_DAYS)
    try:
        phoenix_configuration = _phoenix_usage_config_from_env()
    except PhoenixUsageUnavailableError as exc:
        logger.warning("Phoenix usage configuration is unavailable.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "phoenix_usage_not_ready",
                "message": "Phoenix 사용 이력 조회 설정이 완료되지 않았습니다.",
            },
        ) from exc

    configuration_errors = _phoenix_configuration_errors(phoenix_configuration)
    if not bool(getattr(phoenix_configuration, "is_configured", False)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "phoenix_usage_not_ready",
                "message": "Phoenix 사용 이력 조회 설정이 완료되지 않았습니다.",
                "missing": configuration_errors,
            },
        )

    try:
        phoenix_rows = _fetch_phoenix_usage_history(
            phoenix_configuration,
            days=_USAGE_HISTORY_WINDOW_DAYS,
            today=end_day,
        )
    except PhoenixUsageUnavailableError as exc:
        logger.warning("Phoenix usage history request failed.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "phoenix_usage_unavailable",
                "message": "Phoenix 사용 이력을 조회할 수 없습니다. 연결 정보와 API 권한을 확인해 주세요.",
            },
        ) from exc

    usage_history = _normalise_phoenix_usage_history(
        phoenix_rows,
        start_day=start_day,
        end_day=end_day,
    )
    dashboard = _build_usage_dashboard(
        usage_history,
        usage_policy,
        start_day=start_day,
        end_day=end_day,
    )
    return {
        "source": {
            "mode": "phoenix",
            "status": "connected",
            "label": "Phoenix 사용 이력",
            "detail": "최근 3주 GaiA Input 기록을 요청 시점에 조회했습니다.",
            "fetched_at": fetched_at,
            "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
            "project_count": _configured_phoenix_project_count(phoenix_configuration),
        },
        "dashboard": dashboard,
        "usage_history": usage_history,
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
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
