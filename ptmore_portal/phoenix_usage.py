"""Read recent GaiA request history from one or more Phoenix projects.

This module deliberately has no FastAPI or Portal UI dependency.  It converts
Phoenix ``GaiA Input`` spans into a small, dashboard-friendly record shape:

``query_time``, ``platform``, ``user_id``, ``question``, and ``project``.

The date range is always interpreted in Korea Standard Time (UTC+09:00).  A
single trace is treated as one chat request, even when Phoenix contains more
than one matching span for that trace.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


KST = timezone(timedelta(hours=9), name="Asia/Seoul")
DEFAULT_FILTER_CONDITION = "span_kind == 'CHAIN'"
DEFAULT_SPAN_NAME_PREFIX = "GaiA Input"
DEFAULT_PAGE_SIZE = 500
DEFAULT_TIMEOUT_SECONDS = 30.0

# These queries intentionally select only what the Portal dashboard needs.
_PROJECT_BY_NAME_QUERY = """
query ProjectByName($name: String!) {
  projects(first: 50, filter: {col: name, value: $name}) {
    edges {
      node {
        id
        name
      }
    }
  }
}
"""

_RAW_CHAT_SPANS_QUERY = """
query RecentGaiAInputSpans(
  $projectId: ID!
  $first: Int!
  $after: String
  $timeRange: TimeRange!
  $filterCondition: String!
) {
  node(id: $projectId) {
    ... on Project {
      id
      name
      spans(
        first: $first
        after: $after
        timeRange: $timeRange
        filterCondition: $filterCondition
        sort: {col: startTime, dir: asc}
      ) {
        edges {
          node {
            name
            context {
              traceId
            }
            startTime
            attributes
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""


class PhoenixUsageError(RuntimeError):
    """Raised when Phoenix configuration or a GraphQL request is invalid."""


@dataclass(frozen=True)
class PhoenixUsageConfig:
    """Non-secret Phoenix connection settings supplied by the hosting environment.

    ``projects`` may contain Phoenix project names or GraphQL Relay IDs.  The
    primary environment variable is ``PTMORE_PHOENIX_PROJECTS_JSON`` and
    accepts a JSON list, for example ``[\"project-a\", \"project-b\"]``.
    A comma-separated value is also accepted for a simple local setup.
    """

    endpoint: str = ""
    api_key: str = ""
    projects: tuple[str, ...] = ()
    page_size: int = DEFAULT_PAGE_SIZE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    filter_condition: str = DEFAULT_FILTER_CONDITION
    span_name_prefix: str = DEFAULT_SPAN_NAME_PREFIX

    @property
    def is_configured(self) -> bool:
        """Whether enough configuration exists to make a Phoenix request."""

        return not self.configuration_errors

    @property
    def project_count(self) -> int:
        """Number of configured source projects, safe to expose in API status."""

        return len(self.projects)

    @property
    def missing_settings(self) -> tuple[str, ...]:
        """Return safe setting names that are missing; never return secrets."""

        missing: list[str] = []
        if not self.endpoint.strip():
            missing.append("PTMORE_PHOENIX_ENDPOINT")
        if not self.api_key.strip():
            missing.append("PTMORE_PHOENIX_API_KEY")
        if not self.projects:
            missing.append("PTMORE_PHOENIX_PROJECTS_JSON")
        return tuple(missing)

    @property
    def configuration_errors(self) -> tuple[str, ...]:
        """Safe, non-secret configuration diagnostics for a status endpoint."""

        errors = list(self.missing_settings)
        if self.endpoint.strip():
            try:
                graphql_url(self.endpoint)
            except PhoenixUsageError:
                errors.append("PTMORE_PHOENIX_ENDPOINT 형식")
        if self.page_size < 1 or self.page_size > 1000:
            errors.append("PTMORE_PHOENIX_PAGE_SIZE 범위")
        if self.timeout_seconds <= 0:
            errors.append("PTMORE_PHOENIX_TIMEOUT_SECONDS 값")
        if not self.filter_condition.strip():
            errors.append("PTMORE_PHOENIX_FILTER_CONDITION")
        if not self.span_name_prefix.strip():
            errors.append("PTMORE_PHOENIX_SPAN_NAME_PREFIX")
        return tuple(errors)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "PhoenixUsageConfig":
        """Build configuration from environment variables without contacting Phoenix.

        ``PTMORE_PHOENIX_PROJECT_IDS_JSON`` and the old singular
        ``PTMORE_PHOENIX_PROJECT_ID`` are accepted as migration aliases.  New
        Portal deployments should use ``PTMORE_PHOENIX_PROJECTS_JSON``.
        """

        values = os.environ if environ is None else environ
        projects_value = _first_nonempty(
            values,
            "PTMORE_PHOENIX_PROJECTS_JSON",
            "PTMORE_PHOENIX_PROJECT_IDS_JSON",
            "PTMORE_PHOENIX_PROJECTS",
            "PTMORE_PHOENIX_PROJECT_ID",
        )
        return cls(
            endpoint=str(values.get("PTMORE_PHOENIX_ENDPOINT", "")).strip(),
            api_key=str(values.get("PTMORE_PHOENIX_API_KEY", "")).strip(),
            projects=_parse_projects(projects_value),
            page_size=_positive_int(
                values.get("PTMORE_PHOENIX_PAGE_SIZE"),
                DEFAULT_PAGE_SIZE,
                minimum=1,
                maximum=1000,
                setting_name="PTMORE_PHOENIX_PAGE_SIZE",
            ),
            timeout_seconds=_positive_float(
                values.get("PTMORE_PHOENIX_TIMEOUT_SECONDS"),
                DEFAULT_TIMEOUT_SECONDS,
                setting_name="PTMORE_PHOENIX_TIMEOUT_SECONDS",
            ),
            filter_condition=(
                str(values.get("PTMORE_PHOENIX_FILTER_CONDITION", "")).strip()
                or DEFAULT_FILTER_CONDITION
            ),
            span_name_prefix=(
                str(values.get("PTMORE_PHOENIX_SPAN_NAME_PREFIX", "")).strip()
                or DEFAULT_SPAN_NAME_PREFIX
            ),
        )


def graphql_url(endpoint: str) -> str:
    """Normalize a Phoenix base, traces, or GraphQL URL to its GraphQL endpoint."""

    parsed = urlsplit(str(endpoint or "").strip().rstrip("/"))
    if not parsed.scheme or not parsed.netloc:
        raise PhoenixUsageError("Phoenix endpoint must include a scheme and host.")

    path = parsed.path.rstrip("/")
    for suffix in ("/v1/traces", "/graphql"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    base = urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
    return f"{base}/graphql"


def authorization_header(api_key: str) -> str:
    """Return the Phoenix Authorization value, adding ``Bearer`` when needed."""

    key = str(api_key or "").strip()
    if not key:
        raise PhoenixUsageError("Phoenix API key is empty.")
    return key if key.lower().startswith("bearer ") else f"Bearer {key}"


def recent_kst_date_range(
    days: int = 21,
    *,
    now: datetime | date | None = None,
) -> tuple[date, date]:
    """Return an inclusive KST date range ending today (21 days by default)."""

    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=KST)
        end_day = now.astimezone(KST).date()
    elif isinstance(now, date):
        end_day = now
    else:
        end_day = datetime.now(KST).date()
    return end_day - timedelta(days=days - 1), end_day


def kst_day_bounds(start_day: date, end_day: date) -> dict[str, str]:
    """Build Phoenix's exclusive-end time range for inclusive KST calendar days."""

    if start_day > end_day:
        raise ValueError("start date must not be later than end date")
    start = datetime.combine(start_day, time.min).replace(tzinfo=KST)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min).replace(
        tzinfo=KST
    )
    return {"start": start.isoformat(), "end": end_exclusive.isoformat()}


def is_project_relay_id(value: str) -> bool:
    """Return whether ``value`` looks like Phoenix's encoded ``Project:`` node ID."""

    text = str(value or "").strip()
    if not text:
        return False
    try:
        padding = "=" * (-len(text) % 4)
        decoded = base64.b64decode(text + padding, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return decoded.startswith("Project:")


def resolve_project_id(
    project: str,
    *,
    endpoint: str,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    http_session: Any | None = None,
) -> str:
    """Resolve one exact project name to a GraphQL Relay ID.

    Passing an already encoded Relay ID skips the name lookup.
    """

    requested_project = str(project or "").strip()
    if not requested_project:
        raise PhoenixUsageError("Phoenix project name or ID is empty.")
    if is_project_relay_id(requested_project):
        return requested_project

    session, owns_session = _session(http_session)
    try:
        body = _post_graphql(
            session,
            endpoint=endpoint,
            api_key=api_key,
            query=_PROJECT_BY_NAME_QUERY,
            variables={"name": requested_project},
            timeout_seconds=timeout_seconds,
        )
    finally:
        if owns_session:
            _close_session(session)

    data = body.get("data")
    connection = data.get("projects") if isinstance(data, Mapping) else None
    edges = connection.get("edges") if isinstance(connection, Mapping) else None
    if not isinstance(edges, list):
        raise PhoenixUsageError("Phoenix response does not contain projects.")

    matches: list[str] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, Mapping) else None
        if not isinstance(node, Mapping) or node.get("name") != requested_project:
            continue
        project_id = node.get("id")
        if isinstance(project_id, str) and project_id.strip():
            matches.append(project_id.strip())

    if not matches:
        raise PhoenixUsageError(f"Phoenix project was not found: {requested_project}")
    if len(matches) > 1:
        raise PhoenixUsageError(
            f"Multiple Phoenix projects have the same name: {requested_project}"
        )
    return matches[0]


def extract_input_info(attributes_value: Any) -> dict[str, str]:
    """Extract the request question, platform, and user ID from span attributes.

    Phoenix may serialise both the ``attributes`` field and the nested
    ``input.value`` / ``metadata`` values as JSON strings.  The compatibility
    fallbacks below allow the dashboard to handle the common GaiA Input forms
    without relying on one specific Flow version.
    """

    attributes = _as_mapping(attributes_value)
    raw_input = attributes.get("input.value")
    if raw_input is None:
        raw_input = _nested(attributes, "input", "value")
    payload = _as_mapping(raw_input)
    metadata = _as_mapping(payload.get("metadata"))

    question = _first_text(
        payload.get("input_value"),
        payload.get("question"),
        payload.get("query"),
        payload.get("message"),
    )
    if not question and isinstance(raw_input, str):
        # A few legacy input spans carry a plain question rather than JSON.
        question = raw_input.strip()

    platform = _first_text(
        metadata.get("platform"),
        payload.get("platform"),
        attributes.get("metadata.platform"),
        _nested(attributes, "metadata", "platform"),
    )
    user_id = _first_text(
        metadata.get("user_id"),
        payload.get("user_id"),
        attributes.get("metadata.user_id"),
        _nested(attributes, "metadata", "user_id"),
        attributes.get("a2a.user_id"),
        _nested(attributes, "a2a", "user_id"),
        attributes.get("user.id"),
        _nested(attributes, "user", "id"),
    )
    return {
        "question": question,
        "platform": platform,
        "user_id": user_id,
    }


def fetch_recent_usage(
    config: PhoenixUsageConfig,
    *,
    days: int = 21,
    today: date | datetime | None = None,
    http_session: Any | None = None,
) -> list[dict[str, str]]:
    """Fetch, normalize, and merge recent GaiA Input records from all projects.

    The returned rows are sorted by ``query_time`` and use KST ISO timestamps.
    Each project's trace IDs are deduplicated independently so that identical
    trace ID strings from two projects cannot collapse into one chat request.
    """

    _validate_config(config)
    start_day, end_day = recent_kst_date_range(days, now=today)
    session, owns_session = _session(http_session)
    try:
        records: list[dict[str, str]] = []
        for configured_project in config.projects:
            project_id = _resolve_project_id_with_session(
                configured_project,
                config=config,
                session=session,
            )
            spans = _fetch_project_spans(
                project_id=project_id,
                config=config,
                start_day=start_day,
                end_day=end_day,
                session=session,
            )
            records.extend(
                _records_from_spans(
                    spans,
                    start_day=start_day,
                    end_day=end_day,
                    span_name_prefix=config.span_name_prefix,
                    project=configured_project,
                )
            )
        return sorted(records, key=lambda item: (item["query_time"], item["project"]))
    finally:
        if owns_session:
            _close_session(session)


def as_portal_usage_history(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Adapt Phoenix rows to the Portal's existing dashboard history contract.

    Phoenix does not know a user's display name, so the employee ID is used as
    a neutral fallback until a future employee-directory integration supplies
    names.  The source platform is mapped to the Portal's ``channel`` field.
    """

    history: list[dict[str, str]] = []
    for index, record in enumerate(records, start=1):
        query_time = str(record.get("query_time") or "").strip()
        user_id = str(record.get("user_id") or "").strip()
        platform = str(record.get("platform") or "").strip()
        history.append(
            {
                "id": f"PHX-{index:06d}",
                "employee_id": user_id or "미확인",
                "user_name": user_id or "미확인 사용자",
                "question": str(record.get("question") or "").strip(),
                "date": query_time[:10] if len(query_time) >= 10 else "",
                "occurred_at": query_time,
                "channel": platform or "미확인",
            }
        )
    return history


def _resolve_project_id_with_session(
    project: str,
    *,
    config: PhoenixUsageConfig,
    session: Any,
) -> str:
    if is_project_relay_id(project):
        return project
    body = _post_graphql(
        session,
        endpoint=config.endpoint,
        api_key=config.api_key,
        query=_PROJECT_BY_NAME_QUERY,
        variables={"name": project},
        timeout_seconds=config.timeout_seconds,
    )
    data = body.get("data")
    connection = data.get("projects") if isinstance(data, Mapping) else None
    edges = connection.get("edges") if isinstance(connection, Mapping) else None
    if not isinstance(edges, list):
        raise PhoenixUsageError("Phoenix response does not contain projects.")

    project_ids = [
        str(node.get("id")).strip()
        for edge in edges
        if isinstance(edge, Mapping)
        and isinstance((node := edge.get("node")), Mapping)
        and node.get("name") == project
        and str(node.get("id") or "").strip()
    ]
    if not project_ids:
        raise PhoenixUsageError(f"Phoenix project was not found: {project}")
    if len(project_ids) > 1:
        raise PhoenixUsageError(f"Multiple Phoenix projects have the same name: {project}")
    return project_ids[0]


def _fetch_project_spans(
    *,
    project_id: str,
    config: PhoenixUsageConfig,
    start_day: date,
    end_day: date,
    session: Any,
) -> list[Mapping[str, Any]]:
    after: str | None = None
    seen_cursors: set[str] = set()
    spans: list[Mapping[str, Any]] = []

    while True:
        body = _post_graphql(
            session,
            endpoint=config.endpoint,
            api_key=config.api_key,
            query=_RAW_CHAT_SPANS_QUERY,
            variables={
                "projectId": project_id,
                "first": config.page_size,
                "after": after,
                "timeRange": kst_day_bounds(start_day, end_day),
                "filterCondition": config.filter_condition,
            },
            timeout_seconds=config.timeout_seconds,
        )
        data = body.get("data")
        project = data.get("node") if isinstance(data, Mapping) else None
        if not isinstance(project, Mapping):
            raise PhoenixUsageError(f"Phoenix project was not found: {project_id}")
        connection = project.get("spans")
        if not isinstance(connection, Mapping):
            raise PhoenixUsageError("Phoenix response does not contain spans.")
        edges = connection.get("edges")
        if not isinstance(edges, list):
            raise PhoenixUsageError("Phoenix span edges are invalid.")

        for edge in edges:
            node = edge.get("node") if isinstance(edge, Mapping) else None
            if isinstance(node, Mapping):
                spans.append(node)

        page_info = connection.get("pageInfo")
        if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
            return spans
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise PhoenixUsageError("Phoenix pagination cursor is missing.")
        if next_cursor == after or next_cursor in seen_cursors:
            raise PhoenixUsageError("Phoenix pagination cursor did not advance.")
        seen_cursors.add(next_cursor)
        after = next_cursor


def _records_from_spans(
    spans: Iterable[Mapping[str, Any]],
    *,
    start_day: date,
    end_day: date,
    span_name_prefix: str,
    project: str,
) -> list[dict[str, str]]:
    prefix = str(span_name_prefix or "").strip()
    if not prefix:
        raise PhoenixUsageError("Phoenix span name prefix is empty.")

    traces: dict[str, dict[str, str]] = {}
    for index, span in enumerate(spans):
        if not _matches_span_name(span, prefix):
            continue
        try:
            query_time = _start_time(span.get("startTime"))
        except (TypeError, ValueError):
            # One corrupt trace must not make the last three weeks disappear.
            continue
        if query_time.date() < start_day or query_time.date() > end_day:
            continue

        context = span.get("context")
        trace_id = context.get("traceId") if isinstance(context, Mapping) else None
        trace_key = str(trace_id).strip() if trace_id is not None else ""
        if not trace_key:
            trace_key = f"__span_without_trace_id_{index}"

        info = extract_input_info(span.get("attributes"))
        row = {
            "query_time": query_time.isoformat(),
            "platform": info["platform"],
            "user_id": info["user_id"],
            "question": info["question"],
            "project": str(project),
        }
        existing = traces.get(trace_key)
        if existing is None or row["query_time"] < existing["query_time"]:
            if existing:
                # Keep available fields from a later duplicate span.  This is
                # useful when tracing instrumentation is populated in stages.
                for key in ("platform", "user_id", "question"):
                    if not row[key] and existing[key]:
                        row[key] = existing[key]
            traces[trace_key] = row
        else:
            for key in ("platform", "user_id", "question"):
                if not existing[key] and row[key]:
                    existing[key] = row[key]

    return sorted(traces.values(), key=lambda item: item["query_time"])


def _post_graphql(
    session: Any,
    *,
    endpoint: str,
    api_key: str,
    query: str,
    variables: Mapping[str, Any],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    try:
        response = session.post(
            graphql_url(endpoint),
            headers={
                "Authorization": authorization_header(api_key),
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": dict(variables)},
            timeout=timeout_seconds,
        )
        _raise_for_status(response)
        body = response.json()
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" (HTTP {status_code})" if status_code else ""
        raise PhoenixUsageError(f"Phoenix GraphQL request failed{suffix}.") from exc
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PhoenixUsageError("Phoenix returned an invalid JSON response.") from exc

    if not isinstance(body, Mapping):
        raise PhoenixUsageError("Phoenix returned an invalid GraphQL response.")
    errors = body.get("errors")
    if errors:
        messages = [
            str(item.get("message") or "Unknown GraphQL error")
            for item in errors
            if isinstance(item, Mapping)
        ]
        detail = "; ".join(messages) or "Unknown GraphQL error"
        raise PhoenixUsageError(f"Phoenix GraphQL error: {detail}")
    data = body.get("data")
    if not isinstance(data, Mapping):
        raise PhoenixUsageError("Phoenix GraphQL response does not contain data.")
    return body


def _raise_for_status(response: Any) -> None:
    """Support normal requests responses and small test doubles alike."""

    method = getattr(response, "raise_for_status", None)
    if callable(method):
        method()
        return
    status_code = int(getattr(response, "status_code", 200))
    if status_code >= 400:
        error = requests.HTTPError(f"HTTP {status_code}")
        error.response = response
        raise error


def _start_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("span startTime is missing")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST)


def _matches_span_name(span: Mapping[str, Any], prefix: str) -> bool:
    name = span.get("name")
    return isinstance(name, str) and name.strip().startswith(prefix)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, list):
        # Compatibility with key/value attribute-list serialisations.
        result: dict[str, Any] = {}
        for item in value:
            if isinstance(item, Mapping) and item.get("key") is not None:
                result[str(item["key"])] = item.get("value")
        return result
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_text(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_projects(value: Any) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    parsed: Any = None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PhoenixUsageError(
                "PTMORE_PHOENIX_PROJECTS_JSON must be a JSON list."
            ) from exc
        if not isinstance(parsed, list):
            raise PhoenixUsageError("PTMORE_PHOENIX_PROJECTS_JSON must be a JSON list.")
        values = parsed
    else:
        values = text.replace("\n", ",").split(",")

    projects: list[str] = []
    for item in values:
        project = str(item or "").strip()
        if project and project not in projects:
            projects.append(project)
    return tuple(projects)


def _first_nonempty(values: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(values.get(name, "")).strip()
        if value:
            return value
    return ""


def _positive_int(
    value: Any,
    default: int,
    *,
    minimum: int,
    maximum: int,
    setting_name: str,
) -> int:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError as exc:
        raise PhoenixUsageError(f"{setting_name} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise PhoenixUsageError(
            f"{setting_name} must be between {minimum} and {maximum}."
        )
    return parsed


def _positive_float(value: Any, default: float, *, setting_name: str) -> float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        parsed = float(text)
    except ValueError as exc:
        raise PhoenixUsageError(f"{setting_name} must be a number.") from exc
    if parsed <= 0:
        raise PhoenixUsageError(f"{setting_name} must be greater than zero.")
    return parsed


def _validate_config(config: PhoenixUsageConfig) -> None:
    if not config.is_configured:
        detail = ", ".join(config.configuration_errors)
        raise PhoenixUsageError(f"Phoenix usage configuration is incomplete: {detail}")
    # Validate early so a bad endpoint is not reported as an opaque request error.
    graphql_url(config.endpoint)
    authorization_header(config.api_key)
    if not str(config.span_name_prefix or "").strip():
        raise PhoenixUsageError("Phoenix span name prefix is empty.")
    if not 1 <= config.page_size <= 1000:
        raise PhoenixUsageError("Phoenix page size must be between 1 and 1000.")
    if config.timeout_seconds <= 0:
        raise PhoenixUsageError("Phoenix timeout must be greater than zero.")
    if not str(config.filter_condition or "").strip():
        raise PhoenixUsageError("Phoenix filter condition is empty.")


def _session(http_session: Any | None) -> tuple[Any, bool]:
    if http_session is not None:
        return http_session, False
    return requests.Session(), True


def _close_session(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        close()
