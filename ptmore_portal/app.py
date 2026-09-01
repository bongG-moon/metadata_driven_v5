"""PTMORE PKG Agent management portal.

The dashboard can explicitly read recent Phoenix usage history when configured.
Schedule source documents are persisted to a dedicated Portal MongoDB
collection; their actual execution is handled by a separate worker.
Metadata authoring can call a separately configured external Flow API; this
portal never connects to CUBE or GAIA directly.
"""

from __future__ import annotations

import base64
import binascii
import copy
import csv
import io
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
from urllib.parse import quote, urlparse
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware


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
# production-specific name without changing application code.  The Portal
# owns source-schedule CRUD here; the separate Scheduler Worker owns execution
# and the schedule run-history collection.
_DEFAULT_PORTAL_SETTINGS_COLLECTION = "portal_settings"
_DEFAULT_PORTAL_AUDIT_COLLECTION = "portal_audit_log"
_DEFAULT_SCHEDULE_COLLECTION = "portal_schedules"
_DEFAULT_SCHEDULE_RUN_COLLECTION = "portal_schedule_runs"
_DEFAULT_USAGE_HISTORY_COLLECTION = "portal_usage_history"
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
    _DEFAULT_USAGE_HISTORY_COLLECTION,
}

# The caller ID is a non-secret, administrator-managed value.  It is kept in
# MongoDB settings rather than `.env` so an administrator can change it
# without a server restart.  The API key remains an environment/secret value.
_GAIA_CALLER_ID_HEADER = "X-Gaia-User-Id"
_PORTAL_EMPLOYEE_ID_HEADER = "X-PTMORE-Employee-Id"
_PORTAL_EMPLOYEE_NAME_HEADER = "X-PTMORE-Employee-Name"
_PORTAL_SETTINGS_DOCUMENT_ID = "global"

# ``app.py`` is the production entry point.  ``app_local.py`` selects the
# local adapter before importing this module.  ``test`` is intentionally only
# for the automated test suite, where request headers remain useful fixtures.
_PORTAL_AUTH_MODES = {"production", "local", "test"}
_PORTAL_SESSION_IDENTITY_KEY = "ptmore_portal_identity"
_PORTAL_SESSION_COOKIE_NAME = "ptmore_portal_session"
_PORTAL_UNCONFIGURED_SESSION_SECRET = "ptmore-portal-session-not-configured"
_PORTAL_BOOTSTRAP_ADMINS_ENVIRONMENT = "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON"
_PORTAL_ADMIN_EMPLOYEE_ID_PATTERN = re.compile(r"^\d{7}$")
_PORTAL_ADMIN_DEFAULT_ROLE = "관리자"
_PORTAL_ADMIN_DEFAULT_SCOPE = "포털 설정 · 메타데이터 · 스케줄 관리"
_PORTAL_LOCAL_EMPLOYEE_ID = "2011111"
_PORTAL_LOCAL_EMPLOYEE_NAME = "문봉건"
# This administrator exists only in the local identity adapter.  It is never
# written into the production MongoDB administrator list and therefore cannot
# grant a production SSO user additional authority.
_PORTAL_LOCAL_ADMINISTRATOR = {
    "employee_id": _PORTAL_LOCAL_EMPLOYEE_ID,
    "name": _PORTAL_LOCAL_EMPLOYEE_NAME,
    "role": "Local Admin",
    "scope": "로컬 개발 전용 전체 권한",
    "status": "활성",
}

# Narrow test seam: production code always resolves the mode from its
# environment, while focused unit tests can verify all three adapters without
# importing HCP-only modules.
_portal_auth_mode_override: str | None = None

# 스케줄 실행 결과는 현재 스케줄을 등록한 사용자에게만 개인 DM으로
# 전달한다. 채널 발송은 이 Portal의 스케줄 계약에 포함하지 않는다.
_SCHEDULE_DELIVERY_TARGET = "개인 DM"
_SCHEDULE_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SCHEDULE_REPEAT_VALUES = {"평일", "매일", "매주", "매월", "한 번만", "interval"}
_SCHEDULE_STATUS_ALIASES = {
    "active": "active",
    "활성": "active",
    "inactive": "inactive",
    "비활성": "inactive",
    "일시중지": "inactive",
}
_SCHEDULE_STATUS_LABELS = {"active": "활성", "inactive": "일시중지"}
_SCHEDULE_ID_PATTERN = re.compile(r"^SCH-[0-9a-f]{8}-[0-9a-f-]{27}$", re.IGNORECASE)

# Dashboard usage data is intentionally independent of the metadata-authoring
# Flow adapter.  ``phoenix`` must be explicitly selected in the environment;
# otherwise the Portal remains in its clearly labelled local preview mode.
_USAGE_HISTORY_WINDOW_DAYS = 21
_USAGE_HISTORY_MODES = {"preview", "phoenix"}
_USAGE_HISTORY_ARCHIVE_MODES = {"disabled", "configured"}
_KST = timezone(timedelta(hours=9), name="Asia/Seoul")

logger = logging.getLogger(__name__)

# Langflow applies tweaks by either node ID or display name.  We use stable,
# human-readable display names by default so a Flow re-import does not require
# configuration updates. The environment can override a key with an ID only
# when an operator intentionally renamed a node.
_DEFAULT_METADATA_COMPONENT_MAP = {
    "table_catalog": {
        "request_loader": "00 테이블 카탈로그 등록 요청 로더",
        # Legacy 03 and the lightweight rev_2 03 have no Snapshot node.  Keep
        # this blank by default so Mongo tweaks never target a nonexistent
        # component when the Portal calls a Table Catalog Flow.
        "snapshot_loader": "",
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


class PortalAdministratorCreateRequest(BaseModel):
    """Minimal, server-owned administrator registration input.

    An active administrator decides who may be added. Role, scope, and status
    are deliberately assigned by the server so a browser cannot grant itself
    a hidden privilege level or arbitrary settings value.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    employee_id: str = Field(..., min_length=7, max_length=7)
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("employee_name", "name"),
    )

    @field_validator("employee_id")
    @classmethod
    def validate_employee_id(cls, value: str) -> str:
        if not _PORTAL_ADMIN_EMPLOYEE_ID_PATTERN.fullmatch(value):
            raise ValueError("사번은 숫자 7자리로 입력해 주세요.")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("관리자 이름을 올바르게 입력해 주세요.")
        return value


class PortalAdministratorUpdateRequest(BaseModel):
    """The safe, limited changes allowed for a registered administrator."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("employee_name", "name"),
    )
    status: Literal["활성", "비활성"] | None = None

    @field_validator("name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("관리자 이름을 올바르게 입력해 주세요.")
        return value


class ScheduleCreateRequest(BaseModel):
    """The safe, user-editable source fields for one Portal schedule.

    Owner, delivery target, execution lease, and run-history fields are all
    assigned by the server or Scheduler Worker.  They are deliberately not
    accepted from a browser request.
    """

    title: str = Field(..., min_length=1, max_length=200)
    question: str = Field(..., min_length=1, max_length=20_000)
    repeat: str = Field(..., min_length=1, max_length=32)
    time: str | None = Field(default=None, max_length=5)
    interval_minutes: int | None = Field(default=None, ge=1, le=1_440)
    start_time: str | None = Field(default=None, max_length=5)
    end_time: str | None = Field(default=None, max_length=5)
    # The current UI does not expose this yet, but keeping it optional makes a
    # one-time schedule deterministic instead of requiring a hidden date.
    run_date: str | None = Field(default=None, max_length=10)
    status: str | None = Field(default=None, max_length=16)


class ScheduleUpdateRequest(BaseModel):
    """Partial editable fields for an existing schedule.

    The owner identity and CUBE delivery target are intentionally absent.  A
    client cannot transfer or redirect someone else's schedule by editing it.
    """

    title: str | None = Field(default=None, max_length=200)
    question: str | None = Field(default=None, max_length=20_000)
    repeat: str | None = Field(default=None, max_length=32)
    time: str | None = Field(default=None, max_length=5)
    interval_minutes: int | None = Field(default=None, ge=1, le=1_440)
    start_time: str | None = Field(default=None, max_length=5)
    end_time: str | None = Field(default=None, max_length=5)
    run_date: str | None = Field(default=None, max_length=10)
    status: str | None = Field(default=None, max_length=16)


class ScheduleStatusUpdateRequest(BaseModel):
    """Small dedicated status request used by the pause/resume UI."""

    status: str = Field(..., min_length=1, max_length=16)


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
# The archive adapter is intentionally separate from the Phoenix fetch seam:
# unit tests can prove that no MongoDB write starts before all Phoenix projects
# have returned successfully.
_usage_history_archive_factory: Callable[[], Any] | None = None


def _usage_history_mode_from_env() -> str:
    """Return the explicit dashboard source mode without guessing a fallback."""

    configured = _environment_value("PTMORE_USAGE_HISTORY_MODE", "preview").lower()
    # ``mock`` was used in a few early Portal notes; accepting it only as an
    # explicit preview alias keeps deployed preview environments compatible.
    if configured == "mock":
        return "preview"
    return configured


def _usage_history_archive_mode_from_env() -> str:
    """Return the explicit long-term archive mode without an implicit fallback."""

    return _environment_value("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "disabled").lower()


def _usage_history_full_refresh_requires_live_archive() -> None:
    """Require the configured Phoenix/MongoDB path for an admin full refresh."""

    if _usage_history_mode_from_env() != "phoenix":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "phoenix_usage_not_ready",
                "message": "최근 3주 전체 새로고침에는 Phoenix 조회 모드가 필요합니다.",
            },
        )
    if _usage_history_archive_mode_from_env() != "configured":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_not_ready",
                "message": "최근 3주 전체 새로고침에는 MongoDB 장기 보관 설정이 필요합니다.",
            },
        )


def _usage_history_archive_config_from_env() -> Any:
    """Load non-secret MongoDB archive settings only; do not connect yet."""

    try:
        from usage_history_archive import UsageHistoryArchiveConfig, UsageHistoryArchiveError
    except ImportError as exc:
        raise PhoenixUsageUnavailableError(
            "사용 이력 보관 모듈을 불러올 수 없습니다."
        ) from exc

    try:
        return UsageHistoryArchiveConfig.from_env()
    except (UsageHistoryArchiveError, TypeError, ValueError) as exc:
        raise PhoenixUsageUnavailableError(
            "사용 이력 보관 설정을 해석할 수 없습니다."
        ) from exc


def _usage_history_archive_protected_collection_names() -> tuple[str, ...]:
    """Collect every Portal/metadata MongoDB collection the archive must avoid."""

    portal_collections = _portal_mongodb_collection_settings_from_env()
    protected: set[str] = {
        str(name).strip()
        for name in portal_collections.all_collections
        if str(name).strip()
    }
    metadata_settings = _metadata_settings_from_env()
    for metadata_type in _METADATA_TYPES:
        collection_name = metadata_settings.collection_for(metadata_type)
        if collection_name:
            protected.add(collection_name)
    live_settings = _live_metadata_read_settings_from_env(metadata_settings)
    for metadata_type in _METADATA_TYPES:
        collection_name = live_settings.collection_for(metadata_type)
        if collection_name:
            protected.add(collection_name)
    return tuple(sorted(protected))


def _usage_history_archive_configuration_errors(configuration: Any) -> list[str]:
    """Return safe archive setup errors, including cross-collection collisions."""

    errors = [
        str(item)
        for item in getattr(configuration, "configuration_errors", ())
        if str(item).strip()
    ]
    collection = str(getattr(configuration, "collection", "") or "").strip()
    try:
        from usage_history_archive import collection_name_conflicts
    except ImportError as exc:
        raise PhoenixUsageUnavailableError(
            "사용 이력 보관 모듈을 불러올 수 없습니다."
        ) from exc
    if collection_name_conflicts(
        collection,
        _usage_history_archive_protected_collection_names(),
    ):
        errors.append("PTMORE_USAGE_HISTORY_COLLECTION")
    return list(dict.fromkeys(errors))


def _get_usage_history_archive() -> Any:
    """Open the request-scoped MongoDB usage-history archive adapter."""

    if _usage_history_archive_factory is not None:
        return _usage_history_archive_factory()

    try:
        from usage_history_archive import MongoUsageHistoryArchive, UsageHistoryArchiveError
    except ImportError as exc:
        raise PhoenixUsageUnavailableError(
            "사용 이력 보관 모듈을 불러올 수 없습니다."
        ) from exc

    try:
        return MongoUsageHistoryArchive(
            _usage_history_archive_config_from_env(),
            protected_collections=_usage_history_archive_protected_collection_names(),
        )
    except (UsageHistoryArchiveError, TypeError, ValueError) as exc:
        raise PhoenixUsageUnavailableError(
            "MongoDB 사용 이력 보관소를 열 수 없습니다."
        ) from exc


def _close_usage_history_archive(archive: Any) -> None:
    """Close request-scoped MongoDB clients without hiding a route result."""

    closer = getattr(archive, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - cleanup must not mask success
            logger.warning("Usage history archive did not close cleanly.")


def _usage_history_archive_configuration_status(
    *,
    connection_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose archive readiness without leaking URI, credentials, or records."""

    mode = _usage_history_archive_mode_from_env()
    if mode == "disabled":
        return {
            "mode": "disabled",
            "configured": False,
            "ready": False,
            "storage_status": "disabled",
            "collection": None,
            "message": "장기 사용 이력 보관이 비활성화되어 있습니다.",
        }
    if mode not in _USAGE_HISTORY_ARCHIVE_MODES:
        return {
            "mode": mode,
            "configured": False,
            "ready": False,
            "storage_status": "invalid_mode",
            "collection": None,
            "message": "사용 이력 보관 모드를 확인해 주세요.",
        }

    try:
        configuration = _usage_history_archive_config_from_env()
    except PhoenixUsageUnavailableError:
        return {
            "mode": "configured",
            "configured": False,
            "ready": False,
            "storage_status": "not_configured",
            "collection": None,
            "message": "사용 이력 보관 MongoDB 설정을 확인해 주세요.",
        }

    try:
        missing = _usage_history_archive_configuration_errors(configuration)
    except PhoenixUsageUnavailableError:
        missing = ["PTMORE_USAGE_HISTORY_COLLECTION"]
    configured = not missing
    connected = bool((connection_status or {}).get("connected", False))
    return {
        "mode": "configured",
        "configured": configured,
        "ready": bool(configured and connected),
        "storage_status": (
            "ready" if configured and connected else "connection_error" if configured else "not_configured"
        ),
        "collection": (
            str(getattr(configuration, "collection", "") or "").strip() or None
        ),
        "configuration_errors": missing,
        "message": (
            "Phoenix 최근 조회 결과를 MongoDB에 날짜별로 동기화해 장기 이력을 보관합니다."
            if configured and connected
            else "사용 이력 보관 MongoDB 연결 또는 설정을 확인해 주세요."
        ),
    }


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


def _usage_record_identity(record: Mapping[str, Any]) -> str:
    """Use the archive's project-plus-trace identity for safe live/archive merging."""

    try:
        from usage_history_archive import usage_record_identity
    except ImportError as exc:
        raise PhoenixUsageUnavailableError(
            "사용 이력 보관 모듈을 불러올 수 없습니다."
        ) from exc
    return usage_record_identity(record)


def _merge_usage_records(
    archived_records: list[Mapping[str, Any]],
    live_records: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge archive/history rows, letting a fresh Phoenix read win by identity."""

    merged: dict[str, dict[str, Any]] = {}
    for record in [*archived_records, *live_records]:
        if not isinstance(record, Mapping):
            continue
        project = str(record.get("project") or record.get("source_project") or "").strip()
        query_time = str(record.get("query_time") or record.get("occurred_at") or "").strip()
        if not project or not query_time:
            continue
        value = dict(record)
        value["project"] = project
        value["query_time"] = query_time
        merged[_usage_record_identity(value)] = value
    return sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("query_time") or ""),
            str(item.get("project") or ""),
        ),
    )


def _preview_usage_records() -> list[dict[str, str]]:
    """Adapt preview rows for CSV only; preview data is never archived."""

    return [
        {
            "query_time": str(item.get("occurred_at") or ""),
            "date": str(item.get("date") or ""),
            "platform": str(item.get("channel") or ""),
            "user_id": str(item.get("employee_id") or ""),
            "question": str(item.get("question") or ""),
            "project": "PREVIEW",
            "trace_id": "",
        }
        for item in _build_dummy_usage_history()
    ]


def _usage_archive_not_ready_error(configuration: Any) -> HTTPException:
    try:
        missing = _usage_history_archive_configuration_errors(configuration)
    except PhoenixUsageUnavailableError:
        missing = ["PTMORE_USAGE_HISTORY_COLLECTION"]
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "usage_history_archive_not_ready",
            "message": "장기 사용 이력 보관 MongoDB 설정이 완료되지 않았습니다.",
            "missing": missing,
        },
    )


def _usage_days(start_day: date, end_day: date) -> list[date]:
    """Return every inclusive KST calendar day in a validated period."""

    if start_day > end_day:
        raise ValueError("start_day must not be later than end_day")
    result: list[date] = []
    current = start_day
    while current <= end_day:
        result.append(current)
        current += timedelta(days=1)
    return result


def _coalesce_usage_days(days: set[date]) -> list[tuple[date, date]]:
    """Group date-only Phoenix backfills into the fewest contiguous queries."""

    ordered = sorted(days)
    if not ordered:
        return []

    ranges: list[tuple[date, date]] = []
    range_start = ordered[0]
    previous = ordered[0]
    for current in ordered[1:]:
        if current == previous + timedelta(days=1):
            previous = current
            continue
        ranges.append((range_start, previous))
        range_start = current
        previous = current
    ranges.append((range_start, previous))
    return ranges


def _fetch_phoenix_usage_range(
    configuration: Any,
    *,
    start_day: date,
    end_day: date,
) -> list[Mapping[str, Any]]:
    """Fetch one inclusive KST range through the existing Phoenix adapter."""

    return _fetch_phoenix_usage_history(
        configuration,
        days=(end_day - start_day).days + 1,
        today=end_day,
    )


def _normalise_archive_covered_scopes(
    value: Any,
) -> set[tuple[str, date]]:
    """Validate a marker-only archive coverage result at the Portal boundary."""

    if not isinstance(value, (set, list, tuple)):
        raise PhoenixUsageUnavailableError("사용 이력 보관 범위 정보를 읽을 수 없습니다.")

    scopes: set[tuple[str, date]] = set()
    for item in value:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        project = str(item[0] or "").strip()
        raw_day = item[1]
        try:
            scope_day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
        except (TypeError, ValueError):
            continue
        if project:
            scopes.add((project, scope_day))
    return scopes


def _read_recent_usage_archive_snapshot(
    *,
    start_day: date,
    end_day: date,
    source_projects: tuple[str, ...],
) -> tuple[list[Mapping[str, Any]], set[tuple[str, date]]]:
    """Read dashboard rows plus complete project/day markers from MongoDB."""

    archive: Any | None = None
    try:
        archive = _get_usage_history_archive()
        records = list(archive.read_records(start_day=start_day, end_day=end_day))
        coverage_reader = getattr(archive, "covered_scopes", None)
        if not callable(coverage_reader):
            raise PhoenixUsageUnavailableError("사용 이력 보관 범위 정보를 읽을 수 없습니다.")
        covered_scopes = _normalise_archive_covered_scopes(
            coverage_reader(
                start_day=start_day,
                end_day=end_day,
                source_projects=source_projects,
            )
        )
        return records, covered_scopes
    except PhoenixUsageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_unavailable",
                "message": "MongoDB 사용 이력 보관소를 조회할 수 없습니다. 연결 정보와 권한을 확인해 주세요.",
            },
        ) from exc
    except Exception as exc:
        logger.warning("Usage history archive read failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_unavailable",
                "message": "MongoDB 사용 이력 보관소를 조회할 수 없습니다. 연결 정보와 권한을 확인해 주세요.",
            },
        ) from exc
    finally:
        if archive is not None:
            _close_usage_history_archive(archive)


def _refresh_usage_archive_ranges(
    configuration: Any,
    *,
    ranges: list[tuple[date, date]],
    source_projects: tuple[str, ...],
) -> tuple[int, int, int]:
    """Fetch all requested Phoenix ranges, then persist their complete snapshots.

    Fetching finishes for every range before MongoDB writes start.  A transient
    Phoenix failure therefore never turns a partially refreshed dashboard into
    a seemingly complete recent-history period.
    """

    staged: list[tuple[date, date, list[Mapping[str, Any]], str]] = []
    for range_start, range_end in ranges:
        refresh_started_at = datetime.now(timezone.utc).isoformat()
        try:
            records = _fetch_phoenix_usage_range(
                configuration,
                start_day=range_start,
                end_day=range_end,
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
        staged.append((range_start, range_end, records, refresh_started_at))

    if not staged:
        return 0, 0, 0

    archive: Any | None = None
    try:
        archive = _get_usage_history_archive()
        upserted_count = 0
        removed_count = 0
        for range_start, range_end, records, refresh_started_at in staged:
            refresh = archive.refresh(
                records,
                start_day=range_start,
                end_day=range_end,
                source_projects=source_projects,
                refresh_started_at=refresh_started_at,
            )
            upserted_count += int(getattr(refresh, "upserted_count", 0) or 0)
            removed_count += int(getattr(refresh, "removed_count", 0) or 0)
        return upserted_count, removed_count, sum(
            (range_end - range_start).days + 1
            for range_start, range_end, _, _ in staged
        )
    except PhoenixUsageUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_unavailable",
                "message": "MongoDB 사용 이력 보관소에 저장할 수 없습니다. 연결 정보와 권한을 확인해 주세요.",
            },
        ) from exc
    except Exception as exc:
        logger.warning("Usage history archive synchronization failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_unavailable",
                "message": "MongoDB 사용 이력 보관소에 저장할 수 없습니다. 연결 정보와 권한을 확인해 주세요.",
            },
        ) from exc
    finally:
        if archive is not None:
            _close_usage_history_archive(archive)


def _load_recent_usage_snapshot(*, full_refresh: bool = False) -> dict[str, Any]:
    """Load recent history from MongoDB and refresh only required Phoenix days.

    In the configured archive mode the normal dashboard path always refreshes
    the current KST day and only backfills historical project/day scopes that
    have no successful snapshot marker.  An administrator can explicitly set
    ``full_refresh`` to re-query the entire rolling 21-day range.
    """

    mode = _usage_history_mode_from_env()
    fetched_at = datetime.now(_KST).isoformat()
    if mode == "preview":
        raw_records = _preview_usage_records()
        preview_days = [
            record_day
            for record in raw_records
            if (record_day := _usage_record_date(record)) is not None
        ]
        if preview_days:
            start_day, end_day = min(preview_days), max(preview_days)
        else:  # pragma: no cover - the deterministic preview fixture has rows
            start_day, end_day = _recent_kst_period(days=_USAGE_HISTORY_WINDOW_DAYS)
        history = _normalise_phoenix_usage_history(
            raw_records,
            start_day=start_day,
            end_day=end_day,
        )
        return {
            "start_day": start_day,
            "end_day": end_day,
            "raw_records": raw_records,
            "usage_history": history,
            "source": {
                "mode": "preview",
                "status": "preview",
                "label": "예시 사용 이력",
                "detail": "미리보기 모드의 예시 이력입니다. 실제 Phoenix 조회는 수행하지 않았습니다.",
                "fetched_at": fetched_at,
                "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
                "project_count": 0,
                "archive": {
                    "mode": "disabled",
                    "status": "not_used",
                    "message": "미리보기 데이터는 MongoDB에 저장하지 않습니다.",
                },
            },
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

    archive_mode = _usage_history_archive_mode_from_env()
    archive_configuration: Any | None = None
    if archive_mode not in _USAGE_HISTORY_ARCHIVE_MODES:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_mode_invalid",
                "message": "사용 이력 보관 모드를 확인해 주세요.",
                "missing": ["PTMORE_USAGE_HISTORY_ARCHIVE_MODE"],
            },
        )
    if archive_mode == "configured":
        try:
            archive_configuration = _usage_history_archive_config_from_env()
        except PhoenixUsageUnavailableError as exc:
            raise _usage_archive_not_ready_error({}) from exc
        if _usage_history_archive_configuration_errors(archive_configuration):
            raise _usage_archive_not_ready_error(archive_configuration)

    source_projects = tuple(
        project
        for project in (
            str(value or "").strip()
            for value in getattr(phoenix_configuration, "projects", ())
        )
        if project
    )

    # Without the optional MongoDB archive the established live-only behavior
    # remains intact: Phoenix returns the whole dashboard window every time.
    # The normal production configuration is ``phoenix + configured`` below.
    if archive_mode != "configured":
        try:
            raw_records = _fetch_phoenix_usage_history(
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
        archive_source: dict[str, Any] = {
            "mode": "disabled",
            "status": "not_used",
            "full_refresh": bool(full_refresh),
            "updated_day_count": _USAGE_HISTORY_WINDOW_DAYS,
            "updated_range_count": 1,
            "message": "장기 보관이 비활성화되어 Phoenix 최근 3주 이력을 매번 조회했습니다.",
        }
    else:
        # First read the compact archive and its marker-only coverage. A
        # record-less day is still covered when its marker exists, so it is not
        # needlessly queried on every Portal access.
        _archived_records, covered_scopes = _read_recent_usage_archive_snapshot(
            start_day=start_day,
            end_day=end_day,
            source_projects=source_projects,
        )
        all_days = set(_usage_days(start_day, end_day))
        if full_refresh:
            refresh_days = set(all_days)
        else:
            # The user specifically requested that the current KST day remain
            # live. Historical days are fetched only while at least one
            # configured project lacks a completed archive scope marker.
            refresh_days = {end_day}
            for candidate_day in all_days - {end_day}:
                if any(
                    (project, candidate_day) not in covered_scopes
                    for project in source_projects
                ):
                    refresh_days.add(candidate_day)

        refresh_ranges = _coalesce_usage_days(refresh_days)
        upserted_count, removed_count, updated_day_count = _refresh_usage_archive_ranges(
            phoenix_configuration,
            ranges=refresh_ranges,
            source_projects=source_projects,
        )
        # Re-read after every successful mutation. It lets the dashboard show
        # the MongoDB-owned snapshot, including zero-result markers, rather
        # than relying on a partial live response from one selected range.
        raw_records, _ = _read_recent_usage_archive_snapshot(
            start_day=start_day,
            end_day=end_day,
            source_projects=source_projects,
        )
        archive_source = {
            "mode": "configured",
            "status": "synchronized" if refresh_ranges else "cached",
            "collection": str(getattr(archive_configuration, "collection", "") or "") or None,
            "full_refresh": bool(full_refresh),
            "updated_day_count": updated_day_count,
            "updated_range_count": len(refresh_ranges),
            "upserted_count": upserted_count,
            "removed_count": removed_count,
            "message": (
                "관리자 요청으로 최근 3주 사용 이력을 Phoenix에서 전체 갱신했습니다."
                if full_refresh
                else "MongoDB 보관 이력을 표시하고 당일·누락 날짜만 Phoenix에서 갱신했습니다."
            ),
        }

    history = _normalise_phoenix_usage_history(
        list(raw_records),
        start_day=start_day,
        end_day=end_day,
    )
    return {
        "start_day": start_day,
        "end_day": end_day,
        "raw_records": list(raw_records),
        "usage_history": history,
        "source": {
            "mode": "phoenix",
            "status": "connected",
            "label": "Phoenix·MongoDB 사용 이력" if archive_mode == "configured" else "Phoenix 사용 이력",
            "detail": (
                "MongoDB 보관 이력을 우선 표시하고, 당일과 아직 보관되지 않은 날짜만 Phoenix에서 조회했습니다."
                if archive_mode == "configured"
                else "최근 3주 GaiA Input 기록을 요청 시점에 조회했습니다."
            ),
            "fetched_at": fetched_at,
            "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
            "project_count": _configured_phoenix_project_count(phoenix_configuration),
            "archive": archive_source,
        },
    }


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
            # An omitted key means "use the default".  An explicitly supplied
            # null/empty value means "this Flow has no such node".  This lets
            # the Portal switch between Flow variants without hard-coding an
            # endpoint or accidentally sending a tweak to a removed node.
            if component_name not in configured_value:
                continue
            value = configured_value.get(component_name)
            component_map[metadata_type][component_name] = "" if value is None else str(value).strip()

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
    * Portal source schedules and Scheduler run history use their own
      dedicated collections, separate from Flow metadata.
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
    """Describe Portal schedule source storage without exposing credentials.

    The Portal writes only source schedules to ``schedule_collection``.  A
    separate Scheduler Worker claims due work and writes execution history to
    ``schedule_run_collection``; this status call does not claim that worker
    is currently running.
    """

    collections = _portal_mongodb_collection_settings_from_env()
    mongo_connection_configured = bool(settings.mongo_uri and settings.mongo_database)
    return {
        "role": "schedule_authoring_and_run_history",
        "configured": bool(mongo_connection_configured and collections.ready),
        "storage_enabled": bool(mongo_connection_configured and collections.ready),
        "storage_status": (
            "configured" if mongo_connection_configured and collections.ready else "not_configured"
        ),
        "database": settings.mongo_database or None,
        "schedule_collection": (
            collections.schedule_collection if collections.ready else None
        ),
        "schedule_run_collection": (
            collections.schedule_run_collection if collections.ready else None
        ),
        "collection_configuration_errors": list(collections.configuration_errors),
        "message": (
            "스케줄 등록 정보는 Portal MongoDB에 저장됩니다. 실제 실행과 실행 이력 기록은 "
            "별도 Scheduler Worker가 처리합니다."
            if mongo_connection_configured and collections.ready
            else "스케줄 저장을 사용하려면 MongoDB 연결 정보와 컬렉션 이름 설정을 확인해 주세요."
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


_PREVIEW_PORTAL_ADMINISTRATORS = [
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
    """Return a new, non-authoritative settings document.

    Production administrator authority must come from a persisted
    ``portal_settings.admins`` list or the explicit one-time bootstrap
    environment setting.  Never seed the sample preview administrators into a
    real Portal MongoDB document.
    """

    return {
        "gaia_api_caller_employee_id": "",
        "usage_policy": {
            "history_window_days": 21,
            "active_user_min_distinct_days": 3,
            "active_user_min_chat_count": 10,
        },
        "admins": [],
        "updated_at": None,
        "updated_by": None,
    }


def _normalise_portal_admin_list(value: Any) -> list[dict[str, str]]:
    """Return de-duplicated, safe administrator records from stored data.

    Existing records are treated as data, not as a browser request: malformed
    records are skipped and an unknown status fails closed as ``비활성``.
    """

    if not isinstance(value, list):
        return []

    admins: list[dict[str, str]] = []
    seen_employee_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        employee_id = str(item.get("employee_id") or "").strip()
        if not employee_id or employee_id in seen_employee_ids:
            continue
        name = str(item.get("name") or "").strip() or employee_id
        status_value = str(item.get("status") or "").strip()
        status_value = (
            "활성" if status_value in {"활성", "active"} else "비활성"
        )
        admins.append(
            {
                "employee_id": employee_id,
                "name": name,
                "role": str(item.get("role") or _PORTAL_ADMIN_DEFAULT_ROLE).strip()
                or _PORTAL_ADMIN_DEFAULT_ROLE,
                "scope": str(item.get("scope") or "관리자 권한").strip()
                or "관리자 권한",
                "status": status_value,
            }
        )
        seen_employee_ids.add(employee_id)
    return admins


def _administrator_audit_summary(admins: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Keep administrator-change audit records useful without storing secrets."""

    return [
        {
            "employee_id": str(admin.get("employee_id") or ""),
            "status": str(admin.get("status") or ""),
        }
        for admin in admins
        if isinstance(admin, Mapping)
    ]


def _bootstrap_portal_administrators() -> list[dict[str, str]]:
    """Read the explicit initial-admin list without ever persisting it.

    It is intentionally available only while the MongoDB settings document has
    no real administrators.  Once an administrator is saved through the API,
    the persisted list replaces these entries entirely.
    """

    raw = _environment_value(_PORTAL_BOOTSTRAP_ADMINS_ENVIRONMENT)
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Portal bootstrap administrator configuration is invalid.")
        return []
    if not isinstance(decoded, list):
        logger.warning("Portal bootstrap administrator configuration must be a list.")
        return []

    admins: list[dict[str, str]] = []
    seen_employee_ids: set[str] = set()
    for item in decoded:
        if not isinstance(item, Mapping):
            continue
        employee_id = str(item.get("employee_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if (
            not _PORTAL_ADMIN_EMPLOYEE_ID_PATTERN.fullmatch(employee_id)
            or not name
            or any(ord(character) < 32 for character in name)
            or employee_id in seen_employee_ids
        ):
            continue
        admins.append(
            {
                "employee_id": employee_id,
                "name": name,
                "role": "Bootstrap Admin",
                "scope": "초기 관리자 등록",
                "status": "활성",
            }
        )
        seen_employee_ids.add(employee_id)
    return admins


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
        settings["admins"] = _normalise_portal_admin_list(stored_admins)

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


@dataclass(frozen=True)
class PortalIdentity:
    """Minimal server-verified identity used by Portal authorization.

    Only the employee number and Korean display name are retained in the
    Portal session.  Department, email, and the raw SSO cookie remain outside
    this application because the current Portal authorization rules do not
    need them.
    """

    employee_id: str
    name: str

    def as_session_value(self) -> dict[str, str]:
        return {"employee_id": self.employee_id, "name": self.name}


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


class PortalScheduleStoreError(RuntimeError):
    """Raised when the Portal cannot safely use its schedule collection."""


class ScheduleValidationError(ValueError):
    """A user-correctable schedule field or timing validation error."""


class PortalScheduleStore(Protocol):
    """Small, dedicated schedule source boundary.

    Scheduler leases and run-history writes belong to the separate worker. The
    Portal owns only the source schedule document and never receives arbitrary
    MongoDB queries from a browser.
    """

    persistent: bool

    def list_schedules(self) -> list[dict[str, Any]]:
        ...

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        ...

    def create_schedule(self, document: Mapping[str, Any]) -> dict[str, Any]:
        ...

    def update_schedule(
        self,
        schedule_id: str,
        update: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        ...

    def delete_schedule(self, schedule_id: str) -> bool:
        ...

    def close(self) -> None:
        ...


_SCHEDULE_DOCUMENT_PROJECTION = {
    "_id": 1,
    "title": 1,
    "question": 1,
    "repeat": 1,
    "time": 1,
    "interval_minutes": 1,
    "start_time": 1,
    "end_time": 1,
    "run_date": 1,
    "target": 1,
    "status": 1,
    "owner_id": 1,
    "owner_name": 1,
    "created_at": 1,
    "updated_at": 1,
    "updated_by": 1,
    "next_run_at": 1,
    "timezone": 1,
    # The worker may update these safe, compact summary fields.  It must keep
    # its execution leases, request payloads, and diagnostics out of this
    # projection and therefore out of the Portal response.
    "last_run_at": 1,
    "last_run_status": 1,
}

# The Scheduler Worker owns this collection.  The Portal reads a deliberately
# small projection to populate the dashboard; it must never return a stored
# question, GAIA session ID, worker identifier, delivery diagnostic, or error
# detail to a browser.
_SCHEDULE_RUN_DOCUMENT_PROJECTION = {
    "_id": 0,
    "schedule_id": 1,
    "owner_id": 1,
    "status": 1,
    "scheduled_for": 1,
    "started_at": 1,
    "completed_at": 1,
}
_SCHEDULE_RUN_SCHEDULE_PROJECTION = {
    "_id": 1,
    "title": 1,
    "owner_id": 1,
    "owner_name": 1,
}
_SCHEDULE_RUN_DASHBOARD_LIMIT = 8


class PortalScheduleRunReader(Protocol):
    """Read-only boundary for compact Scheduler Worker execution history."""

    persistent: bool

    def list_recent_runs(self, limit: int) -> list[dict[str, Any]]:
        ...

    def close(self) -> None:
        ...


class MongoPortalScheduleRunReader:
    """Read recent Worker runs and attach only public schedule display fields."""

    persistent = True

    def __init__(
        self,
        *,
        uri: str,
        database: str,
        collections: PortalMongoCollectionSettings,
    ) -> None:
        if not collections.ready:
            raise PortalScheduleStoreError(
                "Portal MongoDB 컬렉션 이름 설정을 확인해 주세요."
            )
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise PortalScheduleStoreError(
                "스케줄 실행 이력 조회를 위해 pymongo 패키지가 필요합니다."
            ) from exc

        self._mongo_error = PyMongoError
        try:
            self._client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
                socketTimeoutMS=5_000,
            )
            database_handle = self._client[database]
            self._runs = database_handle[collections.schedule_run_collection]
            self._schedules = database_handle[collections.schedule_collection]
        except PyMongoError as exc:
            raise PortalScheduleStoreError(
                "MongoDB 스케줄 실행 이력 저장소를 초기화할 수 없습니다."
            ) from exc

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._mongo_error as exc:
            raise PortalScheduleStoreError(
                "MongoDB 스케줄 실행 이력을 읽을 수 없습니다."
            ) from exc

    def list_recent_runs(self, limit: int) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 50:
            raise ValueError("recent schedule run limit must be between 1 and 50")

        documents = self._run(
            lambda: list(
                self._runs.find({}, _SCHEDULE_RUN_DOCUMENT_PROJECTION)
                .sort([("started_at", -1), ("_id", -1)])
                .limit(int(limit))
            )
        )
        runs = [dict(document) for document in documents if isinstance(document, Mapping)]
        schedule_ids = sorted(
            {
                str(document.get("schedule_id") or "").strip()
                for document in runs
                if str(document.get("schedule_id") or "").strip()
            }
        )
        schedules_by_id: dict[str, Mapping[str, Any]] = {}
        if schedule_ids:
            schedule_documents = self._run(
                lambda: list(
                    self._schedules.find(
                        {"_id": {"$in": schedule_ids}},
                        _SCHEDULE_RUN_SCHEDULE_PROJECTION,
                    )
                )
            )
            schedules_by_id = {
                str(document.get("_id") or "").strip(): document
                for document in schedule_documents
                if isinstance(document, Mapping) and str(document.get("_id") or "").strip()
            }

        # Keep the reader's public result compact too, so callers cannot
        # accidentally forward Worker-only source fields to an API response.
        projected_runs: list[dict[str, Any]] = []
        for document in runs:
            schedule = schedules_by_id.get(str(document.get("schedule_id") or "").strip(), {})
            projected_runs.append(
                {
                    "schedule_id": str(document.get("schedule_id") or "").strip(),
                    "owner_id": str(document.get("owner_id") or "").strip(),
                    "status": str(document.get("status") or "").strip(),
                    "scheduled_for": document.get("scheduled_for"),
                    "started_at": document.get("started_at"),
                    "completed_at": document.get("completed_at"),
                    "schedule_title": (
                        (schedule.get("title") or "")
                        if isinstance(schedule, Mapping)
                        else ""
                    ),
                    "schedule_owner_id": (
                        (schedule.get("owner_id") or "")
                        if isinstance(schedule, Mapping)
                        else ""
                    ),
                    "schedule_owner_name": (
                        (schedule.get("owner_name") or "")
                        if isinstance(schedule, Mapping)
                        else ""
                    ),
                }
            )
        return projected_runs

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - cleanup must not hide a result
            pass


class MongoPortalScheduleStore:
    """Persist source schedules in the dedicated Portal MongoDB collection."""

    persistent = True

    def __init__(
        self,
        *,
        uri: str,
        database: str,
        collections: PortalMongoCollectionSettings,
    ) -> None:
        if not collections.ready:
            raise PortalScheduleStoreError(
                "Portal MongoDB 컬렉션 이름 설정을 확인해 주세요."
            )
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise PortalScheduleStoreError(
                "스케줄 저장을 위해 pymongo 패키지가 필요합니다."
            ) from exc

        self._mongo_error = PyMongoError
        try:
            self._client = MongoClient(
                uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
                socketTimeoutMS=5_000,
            )
            self._schedules = self._client[database][collections.schedule_collection]
        except PyMongoError as exc:
            raise PortalScheduleStoreError(
                "MongoDB 스케줄 저장소를 초기화할 수 없습니다."
            ) from exc
        self._ensure_indexes()

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._mongo_error as exc:
            raise PortalScheduleStoreError(
                "MongoDB 스케줄 저장소에 연결할 수 없습니다."
            ) from exc

    def _ensure_indexes(self) -> None:
        """Create only query-supporting indexes; lack of DDL permission is nonfatal.

        A restricted runtime account may be permitted to read/write documents
        but not create indexes.  CRUD remains available in that deployment and
        the operator can create the documented indexes separately.
        """

        try:
            self._schedules.create_index(
                [("status", 1), ("next_run_at", 1)],
                name="portal_schedule_due_lookup",
            )
            self._schedules.create_index(
                [("owner_id", 1), ("updated_at", -1)],
                name="portal_schedule_owner_lookup",
            )
        except self._mongo_error:
            logger.warning(
                "Portal schedule indexes could not be ensured; schedule CRUD continues."
            )

    def list_schedules(self) -> list[dict[str, Any]]:
        documents = self._run(
            lambda: list(
                self._schedules.find({}, _SCHEDULE_DOCUMENT_PROJECTION).sort(
                    [("updated_at", -1), ("_id", 1)]
                )
            )
        )
        return [dict(document) for document in documents if isinstance(document, Mapping)]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        document = self._run(
            lambda: self._schedules.find_one(
                {"_id": schedule_id}, _SCHEDULE_DOCUMENT_PROJECTION
            )
        )
        return dict(document) if isinstance(document, Mapping) else None

    def create_schedule(self, document: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(document))
        self._run(lambda: self._schedules.insert_one(value))
        return value

    def update_schedule(
        self,
        schedule_id: str,
        update: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        value = copy.deepcopy(dict(update))
        # A worker finalizes with its claim token in the MongoDB filter.  When
        # a Portal user edits/pause-resumes a schedule, invalidate that token
        # atomically with the new source fields so an older worker completion
        # cannot overwrite this newer ``next_run_at`` or ``status`` value.
        mutation = {
            "$set": value,
            "$unset": {
                "scheduler_claim_token": "",
                "scheduler_claimed_at": "",
                "scheduler_claim_until": "",
            },
        }
        result = self._run(
            lambda: self._schedules.update_one({"_id": schedule_id}, mutation)
        )
        if not bool(getattr(result, "matched_count", 0)):
            return None
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> bool:
        result = self._run(lambda: self._schedules.delete_one({"_id": schedule_id}))
        return bool(getattr(result, "deleted_count", 0))

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - cleanup must not hide a result
            pass


_portal_schedule_store_factory: Callable[[], PortalScheduleStore] | None = None
_portal_schedule_run_reader_factory: Callable[[], PortalScheduleRunReader] | None = None


def _get_portal_schedule_store() -> PortalScheduleStore:
    """Return only a real MongoDB schedule store; never a preview fallback."""

    if _portal_schedule_store_factory is not None:
        return _portal_schedule_store_factory()

    settings = _metadata_settings_from_env()
    collections = _portal_mongodb_collection_settings_from_env()
    if not settings.mongo_uri or not settings.mongo_database:
        raise PortalScheduleStoreError(
            "스케줄 저장을 위한 MongoDB 연결 정보가 설정되지 않았습니다."
        )
    if not collections.ready:
        raise PortalScheduleStoreError(
            "Portal MongoDB 컬렉션 이름 설정을 확인해 주세요."
        )
    return MongoPortalScheduleStore(
        uri=settings.mongo_uri,
        database=settings.mongo_database,
        collections=collections,
    )


def _close_portal_schedule_store(store: PortalScheduleStore) -> None:
    """Close request-scoped Mongo clients without constraining test doubles."""

    closer = getattr(store, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - cleanup must not hide a response
            logger.warning("Portal schedule store did not close cleanly.")


def _get_portal_schedule_run_reader() -> PortalScheduleRunReader:
    """Return the configured read-only Worker run-history reader.

    The Portal never substitutes preview records here.  A missing or
    unavailable MongoDB connection is represented by an empty dashboard state
    with a clear message instead.
    """

    if _portal_schedule_run_reader_factory is not None:
        return _portal_schedule_run_reader_factory()

    settings = _metadata_settings_from_env()
    collections = _portal_mongodb_collection_settings_from_env()
    if not settings.mongo_uri or not settings.mongo_database:
        raise PortalScheduleStoreError(
            "스케줄 실행 이력을 위한 MongoDB 연결 정보가 설정되지 않았습니다."
        )
    if not collections.ready:
        raise PortalScheduleStoreError(
            "Portal MongoDB 컬렉션 이름 설정을 확인해 주세요."
        )
    return MongoPortalScheduleRunReader(
        uri=settings.mongo_uri,
        database=settings.mongo_database,
        collections=collections,
    )


def _close_portal_schedule_run_reader(reader: PortalScheduleRunReader) -> None:
    """Close the request-scoped reader without masking dashboard output."""

    closer = getattr(reader, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - cleanup must not hide a response
            logger.warning("Portal schedule-run reader did not close cleanly.")


def _schedule_status_code(value: Any, *, default: str = "active") -> str:
    normalized = str(value or default).strip().lower()
    status_code = _SCHEDULE_STATUS_ALIASES.get(normalized)
    if status_code is None:
        raise ScheduleValidationError("스케줄 상태는 활성 또는 일시중지로 입력해 주세요.")
    return status_code


def _schedule_repeat(value: Any) -> str:
    repeat = str(value or "").strip()
    aliases = {"매주 월요일": "매주", "매월 1일": "매월"}
    repeat = aliases.get(repeat, repeat)
    if repeat not in _SCHEDULE_REPEAT_VALUES:
        raise ScheduleValidationError(
            "반복 방식은 평일, 매일, 매주, 매월, 한 번만, interval 중 하나여야 합니다."
        )
    return repeat


def _schedule_text(value: Any, *, field_label: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ScheduleValidationError(f"{field_label}을 입력해 주세요.")
    if len(text) > maximum:
        raise ScheduleValidationError(f"{field_label}은 {maximum:,}자 이내로 입력해 주세요.")
    return text


def _schedule_optional_time(value: Any, *, field_label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _SCHEDULE_TIME_PATTERN.fullmatch(text):
        raise ScheduleValidationError(f"{field_label}은 HH:MM 형식으로 입력해 주세요.")
    return text


def _schedule_time_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _schedule_run_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ScheduleValidationError("한 번만 실행 날짜는 YYYY-MM-DD 형식으로 입력해 주세요.") from exc


def _schedule_at_kst(day: date, clock: str) -> datetime:
    hour, minute = (int(part) for part in clock.split(":", 1))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_KST)


def _next_month_start(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _as_kst_datetime(value: datetime | None = None) -> datetime:
    now = value or datetime.now(_KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_KST)
    return now.astimezone(_KST)


def _next_schedule_run_kst(
    *,
    repeat: str,
    time_value: str,
    interval_minutes: int | None,
    start_time: str,
    end_time: str,
    run_date: str,
    now: datetime | None = None,
) -> datetime:
    """Calculate the next future execution in KST for one validated schedule."""

    current = _as_kst_datetime(now)
    today = current.date()

    if repeat == "interval":
        window_start = _schedule_at_kst(today, start_time)
        window_end = _schedule_at_kst(today, end_time)
        interval = timedelta(minutes=int(interval_minutes or 0))
        if current < window_start:
            return window_start
        if current >= window_end:
            return _schedule_at_kst(today + timedelta(days=1), start_time)
        elapsed_seconds = (current - window_start).total_seconds()
        step = int(elapsed_seconds // interval.total_seconds()) + 1
        candidate = window_start + step * interval
        if candidate <= window_end:
            return candidate
        return _schedule_at_kst(today + timedelta(days=1), start_time)

    if repeat == "평일":
        candidate_day = today
        candidate = _schedule_at_kst(candidate_day, time_value)
        if candidate <= current or candidate_day.weekday() >= 5:
            candidate_day += timedelta(days=1)
        while candidate_day.weekday() >= 5:
            candidate_day += timedelta(days=1)
        return _schedule_at_kst(candidate_day, time_value)

    if repeat == "매일":
        candidate = _schedule_at_kst(today, time_value)
        if candidate <= current:
            candidate = _schedule_at_kst(today + timedelta(days=1), time_value)
        return candidate

    if repeat == "매주":
        candidate_day = today + timedelta(days=(0 - today.weekday()) % 7)
        candidate = _schedule_at_kst(candidate_day, time_value)
        if candidate <= current:
            candidate = _schedule_at_kst(candidate_day + timedelta(days=7), time_value)
        return candidate

    if repeat == "매월":
        candidate = _schedule_at_kst(date(today.year, today.month, 1), time_value)
        if candidate <= current:
            next_month = _next_month_start(today)
            candidate = _schedule_at_kst(next_month, time_value)
        return candidate

    # ``한 번만`` defaults to the next available day only when the user has
    # not provided a date.  An explicitly supplied past moment is rejected so
    # the user does not believe an already missed execution will occur.
    if run_date:
        candidate_day = date.fromisoformat(run_date)
        candidate = _schedule_at_kst(candidate_day, time_value)
        if candidate <= current:
            raise ScheduleValidationError("한 번만 실행 시간은 현재 시각 이후로 지정해 주세요.")
        return candidate
    candidate = _schedule_at_kst(today, time_value)
    return candidate if candidate > current else _schedule_at_kst(today + timedelta(days=1), time_value)


def _schedule_rule_label(values: Mapping[str, Any]) -> str:
    repeat = str(values.get("repeat") or "")
    if repeat == "interval":
        minutes = int(values.get("interval_minutes") or 0)
        interval_label = "1시간마다" if minutes == 60 else f"{minutes}분마다"
        return f"{interval_label} · {values.get('start_time')} ~ {values.get('end_time')}"
    if repeat == "매주":
        return f"매주 월요일 · {values.get('time')}"
    if repeat == "매월":
        return f"매월 1일 · {values.get('time')}"
    if repeat == "한 번만" and values.get("run_date"):
        return f"한 번만 · {values.get('run_date')} {values.get('time')}"
    return f"{repeat} · {values.get('time')}"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _schedule_storage_fields(
    values: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate editable fields and return the exact source fields to persist."""

    title = _schedule_text(values.get("title"), field_label="스케줄 이름", maximum=200)
    question = _schedule_text(values.get("question"), field_label="실행 질문", maximum=20_000)
    repeat = _schedule_repeat(values.get("repeat"))
    status_code = _schedule_status_code(values.get("status"), default="active")
    time_value = _schedule_optional_time(values.get("time"), field_label="실행 시간")
    start_time = _schedule_optional_time(values.get("start_time"), field_label="시작 시간")
    end_time = _schedule_optional_time(values.get("end_time"), field_label="종료 시간")
    run_date = _schedule_run_date(values.get("run_date"))

    if repeat == "interval":
        raw_interval = values.get("interval_minutes")
        try:
            interval_minutes = int(raw_interval)
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError("간격 반복 시간(분)을 입력해 주세요.") from exc
        if not 1 <= interval_minutes <= 1_440:
            raise ScheduleValidationError("간격 반복 시간은 1~1,440분 사이여야 합니다.")
        if not start_time or not end_time:
            raise ScheduleValidationError("간격 반복은 시작 시간과 종료 시간을 모두 입력해 주세요.")
        if _schedule_time_minutes(start_time) >= _schedule_time_minutes(end_time):
            raise ScheduleValidationError("종료 시간은 시작 시간보다 늦어야 합니다.")
        time_value = ""
        run_date = ""
    else:
        if not time_value:
            raise ScheduleValidationError("반복 실행 시간은 HH:MM 형식으로 입력해 주세요.")
        interval_minutes = None
        start_time = ""
        end_time = ""
        if repeat != "한 번만":
            run_date = ""

    next_run_at: str | None = None
    if status_code == "active":
        next_run = _next_schedule_run_kst(
            repeat=repeat,
            time_value=time_value,
            interval_minutes=interval_minutes,
            start_time=start_time,
            end_time=end_time,
            run_date=run_date,
            now=now,
        )
        next_run_at = _utc_iso(next_run)

    return {
        "title": title,
        "question": question,
        "repeat": repeat,
        "time": time_value,
        "interval_minutes": interval_minutes,
        "start_time": start_time,
        "end_time": end_time,
        "run_date": run_date,
        "target": _SCHEDULE_DELIVERY_TARGET,
        "status": status_code,
        "next_run_at": next_run_at,
        "timezone": "Asia/Seoul",
    }


def _schedule_editable_values(document: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only fields that a Portal user is allowed to edit."""

    return {
        "title": document.get("title"),
        "question": document.get("question"),
        "repeat": document.get("repeat"),
        "time": document.get("time"),
        "interval_minutes": document.get("interval_minutes"),
        "start_time": document.get("start_time"),
        "end_time": document.get("end_time"),
        "run_date": document.get("run_date"),
        "status": document.get("status"),
    }


def _schedule_id(value: Any) -> str:
    schedule_id = str(value or "").strip()
    if not _SCHEDULE_ID_PATTERN.fullmatch(schedule_id):
        raise ScheduleValidationError("유효하지 않은 스케줄 식별자입니다.")
    return schedule_id


def _new_schedule_id() -> str:
    return f"SCH-{uuid4()}"


def _parse_schedule_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _display_schedule_timestamp(value: Any, *, now: datetime | None = None) -> str:
    parsed = _parse_schedule_timestamp(value)
    if parsed is None:
        return ""
    local = parsed.astimezone(_KST)
    current = _as_kst_datetime(now)
    if local.date() == current.date():
        return f"오늘 {local:%H:%M}"
    if local.date() == current.date() + timedelta(days=1):
        return f"내일 {local:%H:%M}"
    if local.year == current.year:
        return f"{local.month}월 {local.day}일 {local:%H:%M}"
    return f"{local.year}년 {local.month}월 {local.day}일 {local:%H:%M}"


def _schedule_run_display_text(value: Any, *, fallback: str, maximum: int = 200) -> str:
    """Return a short, single-line display value from a trusted DB projection."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return fallback
    return text[:maximum]


def _schedule_run_status_label(value: Any) -> str:
    """Map Worker lifecycle values to the concise dashboard vocabulary."""

    normalized = str(value or "").strip().lower()
    return {
        "running": "실행 중",
        "success": "성공",
        "failed": "실패",
        "failure": "실패",
        "cancelled": "취소됨",
        "canceled": "취소됨",
        "skipped": "건너뜀",
    }.get(normalized, "완료")


def _dashboard_recent_runs(
    run_documents: list[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Convert compact Worker records into the browser-safe run table shape."""

    def event_timestamp(document: Mapping[str, Any]) -> float:
        for key in ("completed_at", "started_at", "scheduled_for"):
            parsed = _parse_schedule_timestamp(document.get(key))
            if parsed is not None:
                return parsed.timestamp()
        return float("-inf")

    cards: list[dict[str, str]] = []
    for document in sorted(run_documents, key=event_timestamp, reverse=True):
        if not isinstance(document, Mapping):
            continue
        owner_id = _schedule_run_display_text(
            document.get("schedule_owner_id") or document.get("owner_id"),
            fallback="등록자 정보 없음",
            maximum=64,
        )
        owner_name = _schedule_run_display_text(
            document.get("schedule_owner_name"),
            fallback="",
            maximum=100,
        )
        owner = f"{owner_name} ({owner_id})" if owner_name else owner_id
        event_value = (
            document.get("completed_at")
            or document.get("started_at")
            or document.get("scheduled_for")
        )
        cards.append(
            {
                "time": _display_schedule_timestamp(event_value, now=now)
                or "시간 정보 없음",
                "name": _schedule_run_display_text(
                    document.get("schedule_title"),
                    fallback="삭제된 스케줄",
                    maximum=200,
                ),
                "owner": owner,
                "status": _schedule_run_status_label(document.get("status")),
                "target": _SCHEDULE_DELIVERY_TARGET,
            }
        )
    return cards


def _load_dashboard_recent_schedule_runs() -> tuple[list[dict[str, str]], str]:
    """Load a non-fatal, real execution-history slice for the dashboard."""

    reader: PortalScheduleRunReader | None = None
    try:
        reader = _get_portal_schedule_run_reader()
        records = reader.list_recent_runs(_SCHEDULE_RUN_DASHBOARD_LIMIT)
        if not isinstance(records, list):
            raise PortalScheduleStoreError("스케줄 실행 이력 형식이 올바르지 않습니다.")
        cards = _dashboard_recent_runs(
            [record for record in records if isinstance(record, Mapping)]
        )
        if cards:
            return cards, ""
        return [], "최근 스케줄 실행 이력이 없습니다."
    except PortalScheduleStoreError as exc:
        logger.info("Dashboard schedule run history is unavailable: %s", exc)
    except Exception:  # pragma: no cover - defensive boundary for a read-only panel
        logger.exception("Dashboard schedule run history could not be loaded.")
    finally:
        if reader is not None:
            _close_portal_schedule_run_reader(reader)
    return [], "스케줄 실행 이력을 현재 불러오지 못했습니다. MongoDB 연결 상태를 확인해 주세요."


def _schedule_last_run_label(document: Mapping[str, Any]) -> str:
    rendered = _display_schedule_timestamp(document.get("last_run_at"))
    if not rendered:
        return "실행 이력 없음"
    raw_status = str(document.get("last_run_status") or "success").strip().lower()
    status_label = {
        "success": "성공",
        "성공": "성공",
        "failed": "실패",
        "failure": "실패",
        "실패": "실패",
        "skipped": "건너뜀",
        "건너뜀": "건너뜀",
    }.get(raw_status, "완료")
    return f"{rendered} · {status_label}"


def _schedule_response(document: Mapping[str, Any]) -> dict[str, Any]:
    """Project a schedule into the public Portal contract with no worker fields."""

    schedule_id = _schedule_id(document.get("_id") or document.get("id"))
    values = _schedule_editable_values(document)
    # Existing documents should already be validated.  This normalisation is
    # still defensive, so a malformed legacy record cannot leak raw MongoDB
    # content or crash the complete schedule list.
    normalized = _schedule_storage_fields(values)
    status_code = normalized["status"]
    next_run_at = normalized["next_run_at"]
    stored_next = _parse_schedule_timestamp(document.get("next_run_at"))
    if status_code == "active" and stored_next is not None:
        next_run_at = _utc_iso(stored_next)
    if status_code == "inactive":
        next_run_at = None

    owner_id = _schedule_text(
        document.get("owner_id"), field_label="등록자 사번", maximum=64
    )
    owner_name = _schedule_text(
        document.get("owner_name"), field_label="등록자 이름", maximum=200
    )
    created_at = _parse_schedule_timestamp(document.get("created_at"))
    updated_at = _parse_schedule_timestamp(document.get("updated_at"))
    return {
        "id": schedule_id,
        "title": normalized["title"],
        "question": normalized["question"],
        "repeat": normalized["repeat"],
        "time": normalized["time"],
        "interval_minutes": normalized["interval_minutes"],
        "start_time": normalized["start_time"],
        "end_time": normalized["end_time"],
        "run_date": normalized["run_date"] or None,
        "rule_label": _schedule_rule_label(normalized),
        "target": _SCHEDULE_DELIVERY_TARGET,
        "status": _SCHEDULE_STATUS_LABELS[status_code],
        "status_code": status_code,
        "owner": owner_id,
        "owner_id": owner_id,
        "owner_name": owner_name,
        "created_at": _utc_iso(created_at) if created_at is not None else "",
        "updated_at": _utc_iso(updated_at) if updated_at is not None else "",
        "next_run_at": next_run_at,
        "timezone": "Asia/Seoul",
        "next_run": (
            "일시중지됨"
            if status_code == "inactive"
            else _display_schedule_timestamp(next_run_at) or "다음 실행 계산 필요"
        ),
        "last_run": _schedule_last_run_label(document),
    }


def _schedule_owner_or_admin(access: PortalAccess, document: Mapping[str, Any]) -> None:
    """Allow schedule mutation only to the owner or an active administrator."""

    owner_id = str(document.get("owner_id") or "").strip()
    if access.viewer.is_admin or owner_id == access.viewer.employee_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "schedule_owner_or_admin_required",
            "message": "본인이 등록한 스케줄 또는 관리자 스케줄만 변경할 수 있습니다.",
        },
    )


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

        if "admins" in update:
            admins = _normalise_portal_admin_list(update.get("admins"))
            if not admins or not any(admin["status"] == "활성" for admin in admins):
                raise PortalSettingsStoreError(
                    "활성 관리자 없이 관리자 명단을 저장할 수 없습니다."
                )
            after["admins"] = admins

        after["updated_at"] = datetime.now(timezone.utc).isoformat()
        after["updated_by"] = actor.as_audit_actor()
        document = {"_id": _PORTAL_SETTINGS_DOCUMENT_ID, **after}
        self._run(
            lambda: self._settings.replace_one(
                {"_id": _PORTAL_SETTINGS_DOCUMENT_ID}, document, upsert=True
            )
        )
        self.record_audit(
            "portal_administrators_updated" if "admins" in update else "admin_settings_updated",
            actor,
            {
                "before": {
                    "gaia_api_caller_employee_id": before["gaia_api_caller_employee_id"],
                    "usage_policy": before["usage_policy"],
                    "admins": _administrator_audit_summary(before["admins"]),
                },
                "after": {
                    "gaia_api_caller_employee_id": after["gaia_api_caller_employee_id"],
                    "usage_policy": after["usage_policy"],
                    "admins": _administrator_audit_summary(after["admins"]),
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


class PortalSsoError(RuntimeError):
    """Raised when the production-only HCP SSO adapter cannot be used."""


def _portal_auth_mode() -> str:
    """Resolve the explicit identity adapter without weakening production.

    An unknown value intentionally falls back to ``production``.  This makes
    a typo fail closed instead of accidentally enabling the local adapter.
    """

    configured = str(
        _portal_auth_mode_override
        or _environment_value("PTMORE_PORTAL_AUTH_MODE", "production")
    ).strip().lower()
    return configured if configured in _PORTAL_AUTH_MODES else "production"


def _effective_portal_settings(
    settings: Mapping[str, Any] | None,
    identity: PortalIdentity,
) -> dict[str, Any]:
    """Return safe settings with the fixed local developer administrator.

    ``app_local.py`` intentionally uses one fixed identity.  Give that
    identity administrator capability only in the explicit local adapter,
    without changing a production MongoDB settings document or its admin list.
    """

    effective = _normalise_portal_settings(settings)
    if (
        _portal_auth_mode() == "local"
        and identity.employee_id == _PORTAL_LOCAL_EMPLOYEE_ID
    ):
        existing_admins = effective.get("admins")
        admins = existing_admins if isinstance(existing_admins, list) else []
        # Replace a possibly inactive/stale local entry in the returned local
        # view. The MongoDB document remains untouched.
        effective["admins"] = [
            admin
            for admin in admins
            if not isinstance(admin, Mapping)
            or str(admin.get("employee_id") or "").strip()
            != _PORTAL_LOCAL_EMPLOYEE_ID
        ]
        effective["admins"].insert(0, copy.deepcopy(_PORTAL_LOCAL_ADMINISTRATOR))
        return effective

    # Production/test bootstrap authority exists only until the real MongoDB
    # administrator list is saved. It is never written implicitly, so an
    # ordinary user still cannot self-register as an administrator.
    if not effective.get("admins"):
        effective["admins"] = _bootstrap_portal_administrators()
    return effective


def _portal_session_secret() -> str:
    """Read the production session secret without exposing it to a response."""

    return _environment_value("PTMORE_SSO_SESSION_SECRET")


def _production_sso_ready() -> bool:
    return _portal_auth_mode() != "production" or bool(_portal_session_secret())


def _identity_from_mapping(value: Any) -> PortalIdentity | None:
    if not isinstance(value, Mapping):
        return None
    employee_id = str(value.get("employee_id") or "").strip()
    name = str(value.get("name") or "").strip()
    if not employee_id:
        return None
    return PortalIdentity(employee_id=employee_id, name=name or employee_id)


def _local_portal_identity() -> PortalIdentity:
    """Return the deliberately fixed identity for ``app_local.py`` only."""

    return PortalIdentity(
        employee_id=_PORTAL_LOCAL_EMPLOYEE_ID,
        name=_PORTAL_LOCAL_EMPLOYEE_NAME,
    )


def _test_header_identity(request: Request) -> PortalIdentity | None:
    """Read test-only identity fixtures; never call this in production/local."""

    employee_id = str(request.headers.get(_PORTAL_EMPLOYEE_ID_HEADER) or "").strip()
    name = str(request.headers.get(_PORTAL_EMPLOYEE_NAME_HEADER) or "").strip()
    if not employee_id:
        return None
    return PortalIdentity(employee_id=employee_id, name=name or employee_id)


def _request_portal_identity(request: Request) -> PortalIdentity | None:
    """Return the verified current identity for one request.

    Production identity is set only by the signed Portal session after HCP
    SSO login.  Browser headers are intentionally ignored in that mode.
    """

    identity = getattr(request.state, "portal_identity", None)
    if isinstance(identity, PortalIdentity):
        return identity

    mode = _portal_auth_mode()
    if mode == "local":
        return _local_portal_identity()
    if mode == "test":
        return _test_header_identity(request)
    return None


def _portal_session_identity(request: Request) -> PortalIdentity | None:
    """Read one signed SSO session without recording its cookie or contents."""

    if not _production_sso_ready():
        return None
    try:
        stored = request.session.get(_PORTAL_SESSION_IDENTITY_KEY)
    except AssertionError:
        # This can only happen if a deployment removes SessionMiddleware.  Do
        # not fall back to request headers when the production session fails.
        return None
    return _identity_from_mapping(stored)


def _safe_return_path(request: Request, sub_path: str = "") -> str:
    """Keep post-login navigation on this Portal host only."""

    raw = str(
        request.query_params.get("next")
        or request.query_params.get("ORIGIN")
        or sub_path
        or "/"
    ).strip()
    parsed = urlparse(raw)

    if parsed.scheme or parsed.netloc:
        if parsed.netloc != request.url.netloc:
            return "/"
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    if "\\" in path:
        return "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _new_hcp_sso(request: Request) -> Any:
    """Create the HCP SSO helper lazily so local Python never imports it."""

    if not _production_sso_ready():
        raise PortalSsoError("PTMORE_SSO_SESSION_SECRET 설정이 필요합니다.")
    try:
        from hcputil.auth.sso import SSO
    except ImportError as exc:
        raise PortalSsoError(
            "운영 환경에 hcputil.auth.sso SSO 모듈이 설치되어 있지 않습니다."
        ) from exc
    try:
        return SSO(request)
    except Exception as exc:
        raise PortalSsoError("HCP SSO 초기화에 실패했습니다.") from exc


def _sso_identity_from_cookie(sso: Any, cookie: str | None) -> PortalIdentity | None:
    """Extract only the employee number/name returned by the HCP helper."""

    if not cookie:
        return None
    try:
        if sso.check_day_cookie(cookie) is not True:
            return None
        values = sso.get_sso_info(cookie)
    except Exception as exc:
        raise PortalSsoError("HCP SSO 사용자 정보를 확인할 수 없습니다.") from exc

    if not isinstance(values, (list, tuple)) or len(values) < 2:
        raise PortalSsoError("HCP SSO 사용자 정보 형식이 올바르지 않습니다.")
    employee_id = str(values[0] or "").strip()
    name = str(values[1] or "").strip()
    if not employee_id:
        raise PortalSsoError("HCP SSO에서 사용자 사번을 받지 못했습니다.")
    return PortalIdentity(employee_id=employee_id, name=name or employee_id)


def _portal_access(request: Request) -> PortalAccess:
    """Resolve verified identity and server-side administrator permission."""

    identity = _request_portal_identity(request)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "portal_identity_required",
                "message": "로그인 사용자 사번 정보를 확인할 수 없습니다.",
            },
        )

    try:
        store = _get_portal_settings_store()
        settings = _effective_portal_settings(store.read(), identity)
    except PortalSettingsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "portal_settings_unavailable", "message": str(exc)},
        ) from exc

    admin = _active_admin(settings, identity.employee_id)
    viewer_name = identity.name
    # The legacy Portal contract suite uses synthetic header identities.  Keep
    # its historical administrator display name only in the explicit test
    # adapter; production/local always use their verified identity name.
    if _portal_auth_mode() == "test" and admin is not None:
        viewer_name = str(admin.get("name") or viewer_name)
    viewer = PortalViewer(
        employee_id=identity.employee_id,
        name=viewer_name,
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

        identity = _request_portal_identity(request)
        employee_id = identity.employee_id if identity is not None else ""
        bootstrap_settings = (
            _effective_portal_settings(_default_portal_settings(), identity)
            if identity is not None
            else _default_portal_settings()
        )
        bootstrap_admin = _active_admin(bootstrap_settings, employee_id)
        if bootstrap_admin is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "admin_required",
                    "message": "관리자만 등록하거나 설정을 변경할 수 있습니다.",
                },
            ) from exc

        return PortalAccess(
            viewer=PortalViewer(
                employee_id=employee_id,
                name=(identity.name if identity is not None else employee_id),
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


def _stored_settings_for_administrator_mutation(access: PortalAccess) -> dict[str, Any]:
    """Read the persisted admin list, excluding virtual local/bootstrap data."""

    try:
        return _normalise_portal_settings(access.store.read())
    except PortalSettingsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "portal_settings_unavailable", "message": str(exc)},
        ) from exc


def _administrator_employee_id_or_422(employee_id: str) -> str:
    candidate = str(employee_id or "").strip()
    if not _PORTAL_ADMIN_EMPLOYEE_ID_PATTERN.fullmatch(candidate):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_administrator_employee_id",
                "message": "관리자 사번은 숫자 7자리로 입력해 주세요.",
            },
        )
    return candidate


def _save_administrator_list(
    access: PortalAccess,
    admins: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist only the server-built administrator list through Portal storage."""

    try:
        return access.store.update({"admins": copy.deepcopy(admins)}, access.viewer)
    except PortalSettingsStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "portal_settings_unavailable", "message": str(exc)},
        ) from exc


def _administrator_mutation_response(
    settings: Mapping[str, Any],
    *,
    employee_id: str,
    persistent: bool,
) -> dict[str, Any]:
    normalized = _normalise_portal_settings(settings)
    administrator = next(
        (
            admin
            for admin in normalized["admins"]
            if admin["employee_id"] == employee_id
        ),
        None,
    )
    return {
        "administrator": administrator,
        "admins": normalized["admins"],
        "storage": {"persistent": persistent},
    }


_metadata_http_client: MetadataApiClient = UrlLibMetadataApiClient()

def create_app() -> FastAPI:
    """Create the Portal with a production SSO or local identity adapter."""

    portal = FastAPI(
        title="PTMORE PKG Agent Portal",
        description="Portal preview with an optional external metadata authoring API adapter.",
        version="0.2.0-preview",
    )

    @portal.middleware("http")
    async def portal_identity_middleware(request: Request, call_next):
        """Attach only server-verified identity before a protected route runs."""

        path = request.url.path
        is_public = (
            path == "/health"
            or path == "/login"
            or path.startswith("/login/")
            or path.startswith("/static/")
            or path == "/docs"
            or path == "/openapi.json"
            # Chrome DevTools probes this well-known URL automatically.  It
            # is not a Portal API call, so answer quietly instead of logging a
            # misleading 404/redirect on every local browser session.
            or path == "/.well-known/appspecific/com.chrome.devtools.json"
        )
        mode = _portal_auth_mode()

        if mode == "local":
            request.state.portal_identity = _local_portal_identity()
            return await call_next(request)

        if mode == "test":
            identity = _test_header_identity(request)
            if identity is not None:
                request.state.portal_identity = identity
            return await call_next(request)

        if is_public:
            return await call_next(request)

        identity = _portal_session_identity(request)
        if identity is not None:
            request.state.portal_identity = identity
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": {
                        "code": "portal_identity_required",
                        "message": "로그인 사용자 사번 정보를 확인할 수 없습니다.",
                    }
                },
            )

        origin = str(request.url)
        return RedirectResponse(
            url=f"/login?ORIGIN={quote(origin, safe='')}",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )

    # Add SessionMiddleware after the identity middleware registration so the
    # signed session is decoded before the identity middleware reads it.
    # Importing ``app`` in test/local tooling still keeps the app shape stable.
    # When the production secret is missing, the fallback value never
    # authorizes a user because _portal_session_identity() rejects every
    # production session until the real secret is configured.
    portal.add_middleware(
        SessionMiddleware,
        secret_key=_portal_session_secret() or _PORTAL_UNCONFIGURED_SESSION_SECRET,
        session_cookie=_PORTAL_SESSION_COOKIE_NAME,
        same_site="lax",
        https_only=_bool_from_environment("PTMORE_SSO_SESSION_HTTPS_ONLY", True),
    )

    portal.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
    return portal


# Keep the existing ``create_app`` factory for compatibility with tests and
# local tools, while exposing the common deployment factory name used by every
# PTMORE server.
def create_application() -> FastAPI:
    return create_app()


# Both names are intentional: "application" matches the fixed production
# command, while "app" supports normal Uvicorn import syntax for local tests.
application = create_application()
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
    recent_runs: list[Mapping[str, Any]] | None = None,
    recent_runs_message: str = "최근 스케줄 실행 이력이 없습니다.",
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

    # A fully quiet, zero-filled 21-day period still has graph rows.  Keep the
    # denominator at one so its bar heights stay at 0 instead of raising a
    # division-by-zero error during a successful empty Phoenix refresh.
    max_users = max(1, max((item["unique_users"] for item in usage_by_day), default=0))
    max_chats = max(1, max((item["chat_count"] for item in usage_by_day), default=0))
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
    safe_recent_runs = [
        {
            "time": _schedule_run_display_text(
                run.get("time"), fallback="시간 정보 없음", maximum=80
            ),
            "name": _schedule_run_display_text(
                run.get("name"), fallback="삭제된 스케줄", maximum=200
            ),
            "owner": _schedule_run_display_text(
                run.get("owner"), fallback="등록자 정보 없음", maximum=160
            ),
            "status": _schedule_run_display_text(
                run.get("status"), fallback="완료", maximum=40
            ),
            "target": _SCHEDULE_DELIVERY_TARGET,
        }
        for run in (recent_runs or [])
        if isinstance(run, Mapping)
    ]

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
        "recent_runs": safe_recent_runs,
        "recent_runs_message": "" if safe_recent_runs else recent_runs_message,
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


def _preview_recent_schedule_runs() -> list[dict[str, str]]:
    """Static cards used only by the explicit `/api/mock/portal` preview."""

    return [
        {
            "time": "09:30",
            "name": "DA 공정 오전 생산 현황",
            "owner": "문봉건 (2069026)",
            "status": "성공",
            "target": _SCHEDULE_DELIVERY_TARGET,
        },
        {
            "time": "09:15",
            "name": "WIP 이상 LOT 알림",
            "owner": "김민서 (2071044)",
            "status": "성공",
            "target": _SCHEDULE_DELIVERY_TARGET,
        },
        {
            "time": "09:00",
            "name": "설비 DOWN 현황",
            "owner": "최은서 (2093012)",
            "status": "실패",
            "target": _SCHEDULE_DELIVERY_TARGET,
        },
        {
            "time": "08:30",
            "name": "일일 수율 요약",
            "owner": "문봉건 (2069026)",
            "status": "성공",
            "target": _SCHEDULE_DELIVERY_TARGET,
        },
    ]


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
        "dashboard": _build_usage_dashboard(
            usage_history,
            usage_policy,
            recent_runs=_preview_recent_schedule_runs(),
        ),
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
            # This endpoint is an explicit browser-only preview. Its sample
            # administrators must never be used to seed Portal MongoDB.
            "admins": copy.deepcopy(_PREVIEW_PORTAL_ADMINISTRATORS),
        },
    }


def _portal_data_for_access(access: PortalAccess) -> dict[str, Any]:
    """Return the existing Portal payload with the authenticated viewer.

    Metadata preview data remains available for its separate read-mode UI, but
    schedules are deliberately empty here.  The browser must call the real
    ``/api/schedules`` source endpoint rather than mistaking preview cards for
    persisted automations.
    """

    payload = _portal_data(preview_role="admin")
    settings = _normalise_portal_settings(access.settings)
    payload["viewer"] = {
        "employee_id": access.viewer.employee_id,
        "name": access.viewer.name,
        "role": "관리자" if access.viewer.is_admin else "일반 사용자",
        "is_admin": access.viewer.is_admin,
    }
    payload["settings"]["usage_policy"] = copy.deepcopy(settings["usage_policy"])
    payload["settings"]["admins"] = copy.deepcopy(settings["admins"])
    recent_runs, recent_runs_message = _load_dashboard_recent_schedule_runs()
    payload["dashboard"] = _build_usage_dashboard(
        payload["usage_history"],
        settings["usage_policy"],
        recent_runs=recent_runs,
        recent_runs_message=recent_runs_message,
    )
    payload["schedules"] = []
    return payload


@application.get("/login", include_in_schema=False)
@application.get("/login/{sub_path:path}", include_in_schema=False)
async def login(request: Request, sub_path: str = ""):
    """Establish a signed Portal session from the HCP SSO cookie.

    Local/test adapters do not contact HCP.  The production helper is loaded
    only here, so a developer PC can still run or test the Portal without the
    HCP-only ``hcputil`` package.
    """

    destination = _safe_return_path(request, sub_path)
    mode = _portal_auth_mode()
    if mode in {"local", "test"}:
        return RedirectResponse(destination, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    try:
        sso = _new_hcp_sso(request)
        identity = _sso_identity_from_cookie(sso, request.headers.get("cookie"))
    except PortalSsoError as exc:
        logger.warning("Portal SSO login could not be completed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    "code": "portal_sso_unavailable",
                    "message": "SSO 로그인을 시작할 수 없습니다. 운영 환경 설정을 확인해 주세요.",
                }
            },
        )

    if identity is not None:
        request.session[_PORTAL_SESSION_IDENTITY_KEY] = identity.as_session_value()
        return RedirectResponse(destination, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    redirect_url = str(getattr(sso, "redirect_url", "") or "").strip()
    if not redirect_url:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    "code": "portal_sso_redirect_missing",
                    "message": "SSO 로그인 주소를 확인할 수 없습니다.",
                }
            },
        )
    # ``SSO(request)`` receives the original ``ORIGIN`` query parameter added
    # by the middleware, matching the HCP Flask reference integration.
    return RedirectResponse(redirect_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@application.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@application.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
async def chrome_devtools_probe() -> Response:
    """Silently acknowledge Chrome DevTools' optional local capability probe."""

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@application.get("/health")
async def health() -> dict[str, str]:
    # Preserve the existing lightweight liveness contract.  Authentication
    # readiness belongs to the protected Portal API, not this public probe.
    return {"status": "ok", "mode": "dummy-preview"}


@application.get("/api/portal")
async def portal_data(request: Request) -> dict[str, Any]:
    """Return Portal UI data for the authenticated user only."""

    return _portal_data_for_access(_portal_access(request))


def _schedule_store_or_503() -> PortalScheduleStore:
    try:
        return _get_portal_schedule_store()
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "schedule_storage_unavailable",
                "message": str(exc),
            },
        ) from exc


def _schedule_id_or_422(schedule_id: str) -> str:
    try:
        return _schedule_id(schedule_id)
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_schedule_id", "message": str(exc)},
        ) from exc


def _schedule_values_or_422(values: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _schedule_storage_fields(values)
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_schedule", "message": str(exc)},
        ) from exc


def _schedule_response_or_503(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _schedule_response(document)
    except ScheduleValidationError as exc:
        # A malformed source record must not expose raw MongoDB data.  It is a
        # storage consistency problem rather than an end-user form error.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "schedule_storage_record_invalid",
                "message": "저장된 스케줄 정보를 읽을 수 없습니다. 관리자에게 문의해 주세요.",
            },
        ) from exc


def _schedule_sort_key(record: Mapping[str, Any]) -> tuple[int, str, str]:
    active = 0 if record.get("status_code") == "active" else 1
    next_run = str(record.get("next_run_at") or "9999-12-31T23:59:59+00:00")
    return active, next_run, str(record.get("id") or "")


@application.get("/api/schedules")
def list_schedules(request: Request) -> dict[str, Any]:
    """List all actual schedule sources; visibility is not an edit permission."""

    _portal_access(request)
    store = _schedule_store_or_503()
    try:
        documents = store.list_schedules()
        records = [_schedule_response_or_503(document) for document in documents]
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "schedule_storage_unavailable", "message": str(exc)},
        ) from exc
    finally:
        _close_portal_schedule_store(store)

    return {"schedules": sorted(records, key=_schedule_sort_key)}


@application.get("/api/schedules/{schedule_id}")
def get_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    """Read one actual schedule source. Any signed-in Portal user may view it."""

    _portal_access(request)
    safe_schedule_id = _schedule_id_or_422(schedule_id)
    store = _schedule_store_or_503()
    try:
        document = store.get_schedule(safe_schedule_id)
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "schedule_storage_unavailable", "message": str(exc)},
        ) from exc
    finally:
        _close_portal_schedule_store(store)

    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
        )
    return {"schedule": _schedule_response_or_503(document)}


@application.post("/api/schedules", status_code=status.HTTP_201_CREATED)
def create_schedule(
    request_body: ScheduleCreateRequest,
    request: Request,
) -> dict[str, Any]:
    """Create one schedule owned by the signed-in Portal user."""

    access = _portal_access(request)
    fields = _schedule_values_or_422(request_body.model_dump())
    now = datetime.now(timezone.utc).isoformat()
    document = {
        "_id": _new_schedule_id(),
        **fields,
        "owner_id": access.viewer.employee_id,
        "owner_name": access.viewer.name,
        "created_at": now,
        "updated_at": now,
        "updated_by": access.viewer.as_audit_actor(),
    }
    store = _schedule_store_or_503()
    try:
        created = store.create_schedule(document)
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "schedule_storage_unavailable", "message": str(exc)},
        ) from exc
    finally:
        _close_portal_schedule_store(store)
    return {"schedule": _schedule_response_or_503(created)}


@application.patch("/api/schedules/{schedule_id}")
def update_schedule(
    schedule_id: str,
    request_body: ScheduleUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    """Update editable source fields for a schedule owned by the user/admin."""

    access = _portal_access(request)
    safe_schedule_id = _schedule_id_or_422(schedule_id)
    patch = request_body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_schedule_update", "message": "변경할 스케줄 정보를 입력해 주세요."},
        )
    store = _schedule_store_or_503()
    try:
        existing = store.get_schedule(safe_schedule_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
            )
        _schedule_owner_or_admin(access, existing)
        values = _schedule_editable_values(existing)
        values.update(patch)
        fields = _schedule_values_or_422(values)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        fields["updated_by"] = access.viewer.as_audit_actor()
        updated = store.update_schedule(safe_schedule_id, fields)
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "schedule_storage_unavailable", "message": str(exc)},
        ) from exc
    finally:
        _close_portal_schedule_store(store)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
        )
    return {"schedule": _schedule_response_or_503(updated)}


@application.patch("/api/schedules/{schedule_id}/status")
def update_schedule_status(
    schedule_id: str,
    request_body: ScheduleStatusUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    """Pause or resume a source schedule without changing its owner/target."""

    access = _portal_access(request)
    safe_schedule_id = _schedule_id_or_422(schedule_id)
    try:
        requested_status = _schedule_status_code(request_body.status)
    except ScheduleValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_schedule_status", "message": str(exc)},
        ) from exc

    store = _schedule_store_or_503()
    try:
        existing = store.get_schedule(safe_schedule_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
            )
        _schedule_owner_or_admin(access, existing)
        values = _schedule_editable_values(existing)
        values["status"] = requested_status
        fields = _schedule_values_or_422(values)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        fields["updated_by"] = access.viewer.as_audit_actor()
        updated = store.update_schedule(safe_schedule_id, fields)
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "schedule_storage_unavailable", "message": str(exc)},
        ) from exc
    finally:
        _close_portal_schedule_store(store)

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
        )
    return {"schedule": _schedule_response_or_503(updated)}


@application.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    """Delete a source schedule only for its owner or an active administrator."""

    access = _portal_access(request)
    safe_schedule_id = _schedule_id_or_422(schedule_id)
    store = _schedule_store_or_503()
    try:
        existing = store.get_schedule(safe_schedule_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
            )
        _schedule_owner_or_admin(access, existing)
        deleted = store.delete_schedule(safe_schedule_id)
    except PortalScheduleStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "schedule_storage_unavailable", "message": str(exc)},
        ) from exc
    finally:
        _close_portal_schedule_store(store)

    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "schedule_not_found", "message": "스케줄을 찾지 못했습니다."},
        )
    return {"deleted": True, "schedule_id": safe_schedule_id}


@application.get("/api/admin/settings")
async def admin_settings(request: Request) -> dict[str, Any]:
    """Return non-secret portal settings to an active administrator only."""

    access = _require_active_admin(request)
    return _admin_settings_response(access.settings, persistent=access.store.persistent)


@application.post("/api/settings/admins", status_code=status.HTTP_201_CREATED)
@application.post("/api/admin/settings/admins", status_code=status.HTTP_201_CREATED)
def create_portal_administrator(
    request_body: PortalAdministratorCreateRequest,
    request: Request,
) -> dict[str, Any]:
    """Register one real Portal administrator by employee ID.

    Only a current active administrator can call this endpoint. During the
    first-production bootstrap, the bootstrap administrator must register
    their own employee ID first; this prevents a temporary bootstrap identity
    from silently granting a different person permanent administrator access.
    """

    access = _require_active_admin(request)
    stored = _stored_settings_for_administrator_mutation(access)
    admins = copy.deepcopy(stored["admins"])
    employee_id = request_body.employee_id
    if any(admin["employee_id"] == employee_id for admin in admins):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "administrator_already_registered",
                "message": "이미 등록된 관리자 사번입니다.",
            },
        )

    if (
        not admins
        and _portal_auth_mode() != "local"
        and employee_id != access.viewer.employee_id
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "bootstrap_self_registration_required",
                "message": "초기 관리자는 먼저 본인 사번을 실제 관리자 명단에 등록해 주세요.",
            },
        )

    admins.append(
        {
            "employee_id": employee_id,
            "name": request_body.name,
            "role": _PORTAL_ADMIN_DEFAULT_ROLE,
            "scope": _PORTAL_ADMIN_DEFAULT_SCOPE,
            "status": "활성",
        }
    )
    updated = _save_administrator_list(access, admins)
    return _administrator_mutation_response(
        updated,
        employee_id=employee_id,
        persistent=access.store.persistent,
    )


@application.patch("/api/admin/settings/admins/{employee_id}")
def update_portal_administrator(
    employee_id: str,
    request_body: PortalAdministratorUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    """Change a registered administrator's display name or active status."""

    access = _require_active_admin(request)
    target_employee_id = _administrator_employee_id_or_422(employee_id)
    change = request_body.model_dump(exclude_unset=True, exclude_none=True)
    if not change:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_administrator_update", "message": "변경할 값을 입력해 주세요."},
        )

    stored = _stored_settings_for_administrator_mutation(access)
    admins = copy.deepcopy(stored["admins"])
    target_index = next(
        (index for index, admin in enumerate(admins) if admin["employee_id"] == target_employee_id),
        None,
    )
    if target_index is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "administrator_not_found", "message": "관리자 정보를 찾지 못했습니다."},
        )

    target = admins[target_index]
    next_status = str(change.get("status") or target["status"])
    if next_status == "비활성" and target["status"] == "활성":
        active_admin_count = sum(admin["status"] == "활성" for admin in admins)
        if target_employee_id == access.viewer.employee_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "administrator_self_deactivation_forbidden",
                    "message": "현재 로그인한 관리자는 직접 비활성화할 수 없습니다.",
                },
            )
        if active_admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "last_active_administrator_required",
                    "message": "최소 한 명의 활성 관리자가 필요합니다.",
                },
            )

    if "name" in change:
        target["name"] = str(change["name"])
    if "status" in change:
        target["status"] = str(change["status"])
    admins[target_index] = target
    updated = _save_administrator_list(access, admins)
    return _administrator_mutation_response(
        updated,
        employee_id=target_employee_id,
        persistent=access.store.persistent,
    )


@application.delete("/api/admin/settings/admins/{employee_id}")
def delete_portal_administrator(employee_id: str, request: Request) -> dict[str, Any]:
    """Remove a registered administrator while preserving one active owner."""

    access = _require_active_admin(request)
    target_employee_id = _administrator_employee_id_or_422(employee_id)
    stored = _stored_settings_for_administrator_mutation(access)
    admins = copy.deepcopy(stored["admins"])
    target = next(
        (admin for admin in admins if admin["employee_id"] == target_employee_id),
        None,
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "administrator_not_found", "message": "관리자 정보를 찾지 못했습니다."},
        )
    if target_employee_id == access.viewer.employee_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "administrator_self_delete_forbidden",
                "message": "현재 로그인한 관리자는 직접 삭제할 수 없습니다.",
            },
        )
    if target["status"] == "활성" and sum(
        admin["status"] == "활성" for admin in admins
    ) <= 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "last_active_administrator_required",
                "message": "최소 한 명의 활성 관리자가 필요합니다.",
            },
        )

    updated = _save_administrator_list(
        access,
        [admin for admin in admins if admin["employee_id"] != target_employee_id],
    )
    response = _administrator_mutation_response(
        updated,
        employee_id=target_employee_id,
        persistent=access.store.persistent,
    )
    response.update({"deleted": True, "employee_id": target_employee_id})
    return response


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
    # Schedule source storage is independent from the external metadata Flow
    # collections.  The separate Worker owns actual execution/run-history.
    response["portal_schedule_mongodb"] = _portal_schedule_mongodb_status(
        metadata_settings
    )
    # This is deliberately independent from Flow API readiness and from the
    # Portal settings-store read above.  A failed health probe is represented
    # in the response rather than turning the whole status page into an error.
    mongo_connection = _portal_mongodb_connection_status(metadata_settings)
    response["portal_mongodb_connection"] = mongo_connection
    # Phoenix keeps only a limited retention window.  Show the separate
    # Portal-owned archive readiness here so an operator can confirm that
    # historical export will continue to work without exposing its URI/key.
    response["usage_history_archive"] = _usage_history_archive_configuration_status(
        connection_status=mongo_connection
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


def _usage_record_date(record: Mapping[str, Any]) -> date | None:
    value = str(record.get("date") or record.get("usage_date") or record.get("query_time") or "").strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _usage_export_period_or_422(
    *,
    start_date: str | None,
    end_date: str | None,
) -> tuple[date, date] | None:
    """Parse an optional inclusive history range without accepting ambiguous input."""

    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "usage_export_date_range_incomplete",
                "message": "시작일과 종료일을 함께 YYYY-MM-DD 형식으로 입력해 주세요.",
            },
        )
    if start_date is None or end_date is None:
        return None
    try:
        start_day = date.fromisoformat(start_date)
        end_day = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "usage_export_date_invalid",
                "message": "조회 일자는 YYYY-MM-DD 형식으로 입력해 주세요.",
            },
        ) from exc
    if start_day > end_day:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "usage_export_date_range_invalid",
                "message": "시작일은 종료일보다 늦을 수 없습니다.",
            },
        )
    return start_day, end_day


def _usage_export_scope_or_422(scope: str) -> str:
    selected_scope = str(scope or "recent").strip().lower()
    if selected_scope not in {"recent", "all"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "usage_export_scope_invalid",
                "message": "내보내기 범위는 recent 또는 all로 입력해 주세요.",
            },
        )
    return selected_scope


def _usage_export_requires_archive_admin(
    *,
    scope: str,
    requested_period: tuple[date, date] | None,
    recent_start: date,
    recent_end: date,
) -> bool:
    """Keep long-term history distinct from ordinary recent dashboard exports."""

    if scope == "all":
        return True
    if requested_period is None:
        return False
    start_day, end_day = requested_period
    return start_day < recent_start or end_day > recent_end


def _require_usage_history_archive_admin(access: PortalAccess) -> None:
    if access.viewer.is_admin:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "usage_history_export_admin_required",
            "message": "전체 또는 이전 사용 이력 다운로드는 관리자만 가능합니다.",
        },
    )


def _filter_usage_records_to_period(
    records: list[Mapping[str, Any]],
    *,
    start_day: date,
    end_day: date,
) -> list[dict[str, Any]]:
    return [
        dict(record)
        for record in records
        if isinstance(record, Mapping)
        and (record_day := _usage_record_date(record)) is not None
        and start_day <= record_day <= end_day
    ]


def _read_usage_archive_records_or_503(
    *,
    start_day: date | None = None,
    end_day: date | None = None,
) -> list[Mapping[str, Any]]:
    """Read previously synchronized history only when archive mode is explicit."""

    if _usage_history_archive_mode_from_env() != "configured":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_disabled",
                "message": "이전 사용 이력을 조회하려면 장기 사용 이력 보관을 활성화해 주세요.",
            },
        )
    try:
        configuration = _usage_history_archive_config_from_env()
    except PhoenixUsageUnavailableError as exc:
        raise _usage_archive_not_ready_error({}) from exc
    if _usage_history_archive_configuration_errors(configuration):
        raise _usage_archive_not_ready_error(configuration)

    archive: Any | None = None
    try:
        archive = _get_usage_history_archive()
        return list(archive.read_records(start_day=start_day, end_day=end_day))
    except PhoenixUsageUnavailableError:
        raise
    except Exception as exc:
        logger.warning("Usage history archive read failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "usage_history_archive_unavailable",
                "message": "MongoDB 사용 이력 보관소를 조회할 수 없습니다. 연결 정보와 권한을 확인해 주세요.",
            },
        ) from exc
    finally:
        if archive is not None:
            _close_usage_history_archive(archive)


def _usage_export_records(
    snapshot: Mapping[str, Any],
    *,
    requested_period: tuple[date, date] | None,
) -> tuple[list[dict[str, Any]], date | None, date | None]:
    """Overlay an archive range with the fresh Phoenix snapshot when needed.

    Callers use this only for the normal recent dashboard window or for a
    date range that overlaps it.  A fully historical range is deliberately
    read directly from MongoDB by the route, so it remains available if the
    short-retention Phoenix endpoint is temporarily unavailable.
    """

    recent_start = snapshot["start_day"]
    recent_end = snapshot["end_day"]
    current_records = list(snapshot.get("raw_records") or [])

    if requested_period is None:
        return current_records, recent_start, recent_end

    start_day, end_day = requested_period

    if start_day >= recent_start and end_day <= recent_end:
        return _filter_usage_records_to_period(
            current_records,
            start_day=start_day,
            end_day=end_day,
        ), start_day, end_day

    archived_records = _read_usage_archive_records_or_503(
        start_day=start_day,
        end_day=end_day,
    )
    # A requested historical range can overlap the newest Phoenix read.  The
    # fresh records win over archive copies of the same trace identity.
    current_records = _filter_usage_records_to_period(
        current_records,
        start_day=start_day,
        end_day=end_day,
    )
    records = _merge_usage_records(list(archived_records), current_records)
    return records, start_day, end_day


def _usage_export_time(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_KST)
        return parsed.astimezone(_KST).strftime("%H:%M:%S")
    except ValueError:
        return raw[11:19] if len(raw) >= 19 else ""


def _csv_safe_cell(value: Any) -> str:
    """Prevent spreadsheet applications from treating exported data as a formula.

    Questions and user-derived metadata can legitimately begin with formula
    prefix characters.  Prefixing a single apostrophe keeps the literal value
    visible in Excel while preserving a standards-compliant CSV file.
    """

    text = str(value or "").strip()
    formula_probe = text.lstrip(" \t\r\n\ufeff")
    if formula_probe.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def _usage_history_csv_response(
    records: list[Mapping[str, Any]],
    *,
    start_day: date | None,
    end_day: date | None,
) -> Response:
    """Build an Excel-compatible UTF-8 BOM CSV with the agreed Korean columns."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["PROJECT", "일자", "시간", "플랫폼", "사용자(사번)", "질문내용"])
    ordered_records = sorted(
        (record for record in records if isinstance(record, Mapping)),
        key=lambda record: (
            str(record.get("query_time") or record.get("occurred_at") or ""),
            str(record.get("project") or record.get("source_project") or ""),
        ),
    )
    for record in ordered_records:
        query_time = str(record.get("query_time") or record.get("occurred_at") or "").strip()
        usage_date = str(record.get("date") or record.get("usage_date") or query_time[:10]).strip()
        writer.writerow(
            [
                _csv_safe_cell(record.get("project") or record.get("source_project")),
                _csv_safe_cell(usage_date[:10]),
                _csv_safe_cell(_usage_export_time(query_time)),
                _csv_safe_cell(record.get("platform") or record.get("channel")),
                _csv_safe_cell(record.get("user_id") or record.get("employee_id")),
                _csv_safe_cell(record.get("question")),
            ]
        )
    start_label = start_day.strftime("%Y%m%d") if start_day is not None else "all"
    end_label = end_day.strftime("%Y%m%d") if end_day is not None else "all"
    filename = f"ptmore_usage_history_{start_label}_{end_label}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@application.get("/api/dashboard/usage")
def dashboard_usage_data(request: Request) -> dict[str, Any]:
    """Return the cache-first 3-week dashboard plus source status."""

    access = _portal_access(request)
    usage_policy = _normalise_portal_settings(access.settings)["usage_policy"]
    snapshot = _load_recent_usage_snapshot()
    return _dashboard_usage_response(snapshot, usage_policy)


def _dashboard_usage_response(
    snapshot: Mapping[str, Any],
    usage_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one loaded snapshot to the stable browser dashboard contract."""

    usage_history = snapshot["usage_history"]
    recent_runs, recent_runs_message = _load_dashboard_recent_schedule_runs()
    dashboard = _build_usage_dashboard(
        usage_history,
        usage_policy,
        start_day=snapshot["start_day"],
        end_day=snapshot["end_day"],
        recent_runs=recent_runs,
        recent_runs_message=recent_runs_message,
    )
    return {
        "source": snapshot["source"],
        "dashboard": dashboard,
        "usage_history": usage_history,
    }


@application.post("/api/dashboard/usage/refresh")
def dashboard_usage_full_refresh(request: Request) -> dict[str, Any]:
    """Rebuild the rolling 21-day archive snapshot on explicit admin request."""

    access = _require_active_admin(request)
    _usage_history_full_refresh_requires_live_archive()
    usage_policy = _normalise_portal_settings(access.settings)["usage_policy"]
    snapshot = _load_recent_usage_snapshot(full_refresh=True)
    return _dashboard_usage_response(snapshot, usage_policy)


@application.get("/api/dashboard/usage/export.csv")
def dashboard_usage_export_csv(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    scope: str = "recent",
) -> Response:
    """Download recent or archived usage history as Excel-compatible CSV.

    The default is the same recent three-week period shown on the dashboard.
    Administrators can use ``scope=all`` or a completed date range once the
    long-term archive is enabled; ordinary users can download the same usage
    history without receiving any secret configuration details.
    """

    access = _portal_access(request)
    selected_scope = _usage_export_scope_or_422(scope)
    requested_period = _usage_export_period_or_422(
        start_date=start_date,
        end_date=end_date,
    )
    recent_start, recent_end = _recent_kst_period(days=_USAGE_HISTORY_WINDOW_DAYS)
    if _usage_export_requires_archive_admin(
        scope=selected_scope,
        requested_period=requested_period,
        recent_start=recent_start,
        recent_end=recent_end,
    ):
        _require_usage_history_archive_admin(access)

    if selected_scope == "all":
        # Long-term export is an archive-only read.  It intentionally does
        # not call Phoenix, whose short retention must not block past reports.
        return _usage_history_csv_response(
            _read_usage_archive_records_or_503(),
            start_day=None,
            end_day=None,
        )

    if requested_period is not None:
        requested_start, requested_end = requested_period
        if requested_end < recent_start or requested_start > recent_end:
            # A completed historical (or future-empty) period can be served
            # directly from MongoDB without requiring the live Phoenix API.
            return _usage_history_csv_response(
                _read_usage_archive_records_or_503(
                    start_day=requested_start,
                    end_day=requested_end,
                ),
                start_day=requested_start,
                end_day=requested_end,
            )

    # The default recent download, and a date range that overlaps it, uses the
    # same cache-first dashboard snapshot.  It refreshes today and any missing
    # historical scope before exporting without turning every CSV download
    # into a full Phoenix re-query.
    snapshot = _load_recent_usage_snapshot()
    records, selected_start, selected_end = _usage_export_records(
        snapshot,
        requested_period=requested_period,
    )
    return _usage_history_csv_response(
        records,
        start_day=selected_start,
        end_day=selected_end,
    )


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
