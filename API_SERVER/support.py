"""Local artifact-server support code.

Only modules in API_SERVER are imported here.  Keeping this implementation
local makes the folder deployable without the parent repository.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

try:  # Supports both "python app.py" and "import API_SERVER.app".
    from .data_ref_store import (
        DEFAULT_DATABASE,
        DEFAULT_RESULT_COLLECTION,
        load_data_ref_rows,
    )
    from .report_store import (
        ReportStoreError,
        cleanup_expired_reports,
        delete_report,
        ensure_report_store,
        get_active_report_metadata,
        read_report_html,
        report_store_readiness,
        save_report,
        storage_descriptor,
    )
except ImportError:  # pragma: no cover - exercised by direct script execution.
    from data_ref_store import (  # type: ignore[no-redef]
        DEFAULT_DATABASE,
        DEFAULT_RESULT_COLLECTION,
        load_data_ref_rows,
    )
    from report_store import (  # type: ignore[no-redef]
        ReportStoreError,
        cleanup_expired_reports,
        delete_report,
        ensure_report_store,
        get_active_report_metadata,
        read_report_html,
        report_store_readiness,
        save_report,
        storage_descriptor,
    )


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5000
DEFAULT_PREVIEW_LIMIT = 100
DEFAULT_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_REPORT_COLLECTION = "report_save_db"
DEFAULT_REPORT_TTL_HOURS = 24
DEFAULT_MAX_REPORT_TTL_HOURS = 24 * 7
DEFAULT_MAX_REPORT_HTML_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_REPORT_METADATA_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_REPORT_STORAGE_BYTES = 512 * 1024 * 1024
MAX_REPORT_DATASET_REFS = 100
REPORT_ID_PATTERN = re.compile(r"[0-9]{14}_[a-f0-9]{32}")
REPORT_TOKEN_PATTERN = re.compile(r"[a-f0-9]{32,128}")
REPORT_STORE_LOCK = threading.RLock()
KST = timezone(timedelta(hours=9), "KST")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ReportHttpError(Exception):
    """An expected report request failure with a safe HTTP response."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = str(message)


class ServerConfig:
    """Runtime configuration for this API_SERVER deployment only."""

    def __init__(
        self,
        mongo_uri: str,
        mongo_database: str,
        result_collection: str,
        preview_limit: int,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        report_mongo_uri: str = "",
        report_database: str = "",
        report_collection: str = DEFAULT_REPORT_COLLECTION,
        report_base_url: str = "",
        report_default_ttl_hours: int = DEFAULT_REPORT_TTL_HOURS,
        report_max_ttl_hours: int = DEFAULT_MAX_REPORT_TTL_HOURS,
        max_report_html_bytes: int = DEFAULT_MAX_REPORT_HTML_BYTES,
        max_report_storage_bytes: int = DEFAULT_MAX_REPORT_STORAGE_BYTES,
        use_report_access_token: bool = False,
    ) -> None:
        self.mongo_uri = str(mongo_uri or "").strip()
        self.mongo_database = str(mongo_database or DEFAULT_DATABASE).strip()
        self.result_collection = str(result_collection or DEFAULT_RESULT_COLLECTION).strip()
        self.preview_limit = max(0, int(preview_limit))
        self.max_download_bytes = max(1024, int(max_download_bytes))
        self.host = str(host or DEFAULT_HOST)
        self.port = int(port)

        self.report_mongo_uri = str(report_mongo_uri or self.mongo_uri).strip()
        self.report_database = str(report_database or self.mongo_database).strip()
        self.report_collection = str(
            report_collection or DEFAULT_REPORT_COLLECTION
        ).strip()
        if not all((self.report_database, self.report_collection)):
            raise ValueError("MongoDB report storage names must not be empty.")
        if any(
            "\x00" in value
            for value in (
                self.report_database,
                self.report_collection,
            )
        ):
            raise ValueError("MongoDB report storage names must not contain null bytes.")
        self.report_base_url = normalize_report_base_url(
            report_base_url or f"http://127.0.0.1:{self.port}"
        )

        self.report_default_ttl_hours = max(1, int(report_default_ttl_hours))
        self.report_max_ttl_hours = max(1, int(report_max_ttl_hours))
        if self.report_default_ttl_hours > self.report_max_ttl_hours:
            raise ValueError(
                "report_default_ttl_hours cannot exceed report_max_ttl_hours."
            )
        self.max_report_html_bytes = max(1024, int(max_report_html_bytes))
        self.max_report_metadata_bytes = DEFAULT_MAX_REPORT_METADATA_BYTES
        self.max_report_request_bytes = (
            self.max_report_html_bytes + self.max_report_metadata_bytes + (64 * 1024)
        )
        self.max_report_storage_bytes = max(
            self.max_report_html_bytes,
            int(max_report_storage_bytes),
        )
        self.use_report_access_token = bool(use_report_access_token)


def normalize_report_base_url(value: Any) -> str:
    """Validate an externally reachable HTTP(S) base URL."""
    candidate = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise ValueError("report_base_url is not a valid URL.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "report_base_url must be a public HTTP(S) base URL without credentials, query, or fragment."
        )
    if parsed.hostname in {"0.0.0.0", "::"}:
        raise ValueError("report_base_url must not use a bind address such as 0.0.0.0.")
    return candidate


def load_dotenv(env_file: str | Path) -> None:
    """Read a simple .env file without requiring an additional package."""
    path = Path(env_file)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mask_download_server_log(value: Any) -> str:
    """Prevent report and data-ref tokens from appearing in application logs."""
    return re.sub(
        r"([?&](?:token|download_ref)=)[^&\s\"]+",
        r"\1***",
        str(value or ""),
        flags=re.IGNORECASE,
    )


def prepare_report_storage(config: ServerConfig) -> None:
    """Verify the MongoDB report collection and remove expired reports."""
    with REPORT_STORE_LOCK:
        try:
            ensure_report_store(config)
            cleanup_expired_reports(config)
        except ReportStoreError as exc:
            raise ReportHttpError(exc.status, exc.message) from exc


def report_storage_readiness(config: ServerConfig) -> dict[str, Any]:
    """Return report-specific MongoDB collection readiness without secrets."""
    return report_store_readiness(config)


def create_html_report(payload: dict[str, Any], config: ServerConfig) -> dict[str, Any]:
    """Validate, persist, and return URLs for one generated HTML report."""
    html_document = payload.get("html")
    if not isinstance(html_document, str) or not html_document.strip():
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "html is empty")
    html_bytes = html_document.encode("utf-8")
    if len(html_bytes) > config.max_report_html_bytes:
        raise ReportHttpError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"html is too large. max_bytes={config.max_report_html_bytes}",
        )

    title = report_text_field(payload, "title", "HTML Report", 200)
    question = report_text_field(payload, "question", "", 4_000)
    view_request = report_text_field(payload, "view_request", "", 1_000)
    filename_hint = report_text_field(payload, "filename_hint", "report", 200)
    available_datasets = payload.get("available_datasets", [])
    if not isinstance(available_datasets, list) or len(available_datasets) > MAX_REPORT_DATASET_REFS:
        raise ReportHttpError(
            HTTPStatus.BAD_REQUEST,
            f"available_datasets must be a list of at most {MAX_REPORT_DATASET_REFS} objects.",
        )
    if any(not isinstance(item, dict) for item in available_datasets):
        raise ReportHttpError(
            HTTPStatus.BAD_REQUEST,
            "available_datasets items must be objects.",
        )
    report_plan = payload.get("report_plan", {})
    if not isinstance(report_plan, dict):
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "report_plan must be an object.")

    ttl_hours = report_ttl_hours(payload.get("ttl_hours"), config)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)
    # MongoDB BSON Date values are normalized to UTC for TTL and comparisons.
    # Keep those technical fields intact and store explicit KST mirrors for
    # operators and the browser/API response.
    report_id = now.astimezone(KST).strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(16)
    access_token = secrets.token_hex(16) if config.use_report_access_token else ""
    metadata: dict[str, Any] = {
        "report_id": report_id,
        "title": title,
        "question": question,
        "view_request": view_request,
        "available_datasets": available_datasets,
        "report_plan": report_plan,
        "html_bytes": len(html_bytes),
        "download_filename": safe_report_filename(filename_hint or title or report_id),
        "created_at": now,
        "created_at_kst": report_iso(now),
        "expires_at": expires_at,
        "expires_at_kst": report_iso(expires_at),
        "ttl_hours": ttl_hours,
    }
    if access_token:
        metadata["access_token_sha256"] = hash_report_token(access_token)

    try:
        metadata_bytes = json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReportHttpError(
            HTTPStatus.BAD_REQUEST,
            "report metadata cannot be serialized as JSON.",
        ) from exc
    if len(metadata_bytes) > config.max_report_metadata_bytes:
        raise ReportHttpError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"report metadata is too large. max_bytes={config.max_report_metadata_bytes}",
        )

    with REPORT_STORE_LOCK:
        try:
            save_report(config, metadata, html_bytes)
        except ReportStoreError as exc:
            raise ReportHttpError(exc.status, exc.message) from exc

    suffix = f"?{urlencode({'token': access_token})}" if access_token else ""
    return {
        "report_id": report_id,
        "title": title,
        "view_url": f"{config.report_base_url}/reports/view/{report_id}{suffix}",
        "download_url": f"{config.report_base_url}/reports/download/{report_id}{suffix}",
        "created_at": report_iso(now),
        "expires_at": report_iso(expires_at),
        "ttl_hours": ttl_hours,
        "storage": storage_descriptor(config),
    }


def report_text_field(
    payload: dict[str, Any],
    key: str,
    default: str,
    max_length: int,
) -> str:
    value = payload.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, f"{key} must be a string.")
    if len(value) > max_length:
        raise ReportHttpError(
            HTTPStatus.BAD_REQUEST,
            f"{key} may not exceed {max_length} characters.",
        )
    return value.strip() or default


def report_ttl_hours(value: Any, config: ServerConfig) -> int:
    if value in (None, ""):
        return config.report_default_ttl_hours
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "ttl_hours must be an integer.") from exc
    if parsed < 1:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "ttl_hours must be at least 1.")
    return min(parsed, config.report_max_ttl_hours)


def load_active_html_report(
    report_id: str,
    token: str,
    config: ServerConfig,
) -> tuple[dict[str, Any], bytes]:
    with REPORT_STORE_LOCK:
        if not REPORT_ID_PATTERN.fullmatch(str(report_id or "")):
            raise ReportHttpError(HTTPStatus.BAD_REQUEST, "invalid report_id")
        try:
            metadata = get_active_report_metadata(config, report_id)
            validate_report_access_token(metadata, token)
            return metadata, read_report_html(config, metadata)
        except ReportStoreError as exc:
            raise ReportHttpError(exc.status, exc.message) from exc


def delete_html_report(report_id: str, token: str, config: ServerConfig) -> str:
    with REPORT_STORE_LOCK:
        if not REPORT_ID_PATTERN.fullmatch(str(report_id or "")):
            raise ReportHttpError(HTTPStatus.BAD_REQUEST, "invalid report_id")
        try:
            metadata = get_active_report_metadata(config, report_id)
            validate_report_access_token(metadata, token)
            deleted = delete_report(config, report_id)
        except ReportStoreError as exc:
            raise ReportHttpError(exc.status, exc.message) from exc
    if not deleted:
        raise ReportHttpError(HTTPStatus.NOT_FOUND, "report not found")
    return str(metadata["report_id"])


def cleanup_html_reports_unlocked(config: ServerConfig) -> None:
    try:
        cleanup_expired_reports(config)
    except ReportStoreError as exc:
        raise ReportHttpError(exc.status, exc.message) from exc


def validate_report_access_token(metadata: dict[str, Any], token: str) -> None:
    """Validate the optional URL token after MongoDB metadata was loaded."""
    expected_hash = str(metadata.get("access_token_sha256") or "")
    if not expected_hash:
        return
    if not REPORT_TOKEN_PATTERN.fullmatch(str(token or "")):
        raise ReportHttpError(HTTPStatus.FORBIDDEN, "invalid access token")
    if not hmac.compare_digest(hash_report_token(token), expected_hash):
        raise ReportHttpError(HTTPStatus.FORBIDDEN, "invalid access token")


def safe_report_filename(value: Any) -> str:
    text = str(value or "report").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._-") or "report"
    text = re.sub(r"\.html?$", "", text, flags=re.IGNORECASE)
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        text = f"report_{text}"
    return f"{text[:100]}.html"


def report_content_disposition(filename: str, disposition: str) -> str:
    if disposition not in {"inline", "attachment"}:
        raise ValueError("invalid report disposition")
    safe_filename = safe_report_filename(filename)
    fallback = safe_filename.encode("ascii", errors="ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", fallback).strip("._") or "report.html"
    return (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(safe_filename, safe='')}"
    )


def hash_report_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def report_iso(value: datetime) -> str:
    return value.astimezone(KST).isoformat()


def resolve_request(
    query: str,
    config: ServerConfig,
    limit: int | None,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        ref = data_ref_from_query(query)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"download_ref could not be decoded: {exc}",
            "ref": {},
            "loaded": {},
        }
    if not isinstance(ref, dict):
        return {
            "ok": False,
            "message": "download_ref or ref_id is required.",
            "ref": {},
            "loaded": {},
        }

    ref, validation_error = normalize_download_ref(ref, config)
    if validation_error:
        return {"ok": False, "message": validation_error, "ref": ref, "loaded": {}}
    if not config.mongo_uri:
        return {
            "ok": False,
            "message": "MONGODB_URI (or API_SERVER_MONGODB_URI) is not configured.",
            "ref": ref,
            "loaded": {},
        }

    try:
        loaded = load_data_ref_rows(
            ref,
            config.mongo_uri,
            default_database=config.mongo_database,
            default_collection=config.result_collection,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"MongoDB data_ref lookup failed: {exc}",
            "ref": ref,
            "loaded": {},
        }
    if not loaded.get("ok"):
        status = HTTPStatus.GONE if loaded.get("expired") else HTTPStatus.BAD_REQUEST
        return {
            "ok": False,
            "message": str(loaded.get("message") or "data_ref rows were not found."),
            "ref": ref,
            "loaded": loaded,
            "status": status,
        }
    return {"ok": True, "message": "", "ref": ref, "loaded": loaded}


def normalize_download_ref(
    ref: dict[str, Any],
    config: ServerConfig,
) -> tuple[dict[str, Any], str]:
    normalized = dict(ref)
    ref_id = str(normalized.get("ref_id") or "").strip()
    if not re.fullmatch(r"result:.+:[0-9a-fA-F]{32}", ref_id):
        return normalized, "data_ref.ref_id has an unsupported format."

    database = str(normalized.get("database") or config.mongo_database).strip()
    collection_name = str(
        normalized.get("collection_name") or config.result_collection
    ).strip()
    if database != config.mongo_database or collection_name != config.result_collection:
        return normalized, "The requested database or collection is not allowed."

    path = str(normalized.get("path") or "").strip()
    if not re.fullmatch(
        r"payload\.(?:result_rows|runtime_sources\.[A-Za-z0-9_-]+|intermediate_rows\.[A-Za-z0-9_-]+)",
        path,
    ):
        return normalized, "data_ref.path is not allowed."

    normalized["store"] = "mongodb"
    normalized["database"] = config.mongo_database
    normalized["collection_name"] = config.result_collection
    normalized["path"] = path
    return normalized, ""


def resolved_status(resolved: dict[str, Any]) -> HTTPStatus:
    status = resolved.get("status")
    return status if isinstance(status, HTTPStatus) else HTTPStatus.BAD_REQUEST


def data_ref_from_query(query: str) -> dict[str, Any] | None:
    params = parse_qs(query, keep_blank_values=False)
    token = first_param(params, "download_ref")
    if token:
        return decode_data_ref(token)
    ref_id = first_param(params, "ref_id") or first_param(params, "data_ref")
    if not ref_id:
        return None
    ref = {
        "store": "mongodb",
        "ref_id": ref_id,
        "database": first_param(params, "database"),
        "collection_name": first_param(params, "collection_name")
        or first_param(params, "collection"),
        "path": first_param(params, "path") or first_param(params, "row_path"),
        "role": first_param(params, "role"),
        "source_alias": first_param(params, "source_alias"),
        "label": first_param(params, "label"),
    }
    return {key: value for key, value in ref.items() if value not in (None, "")}


def first_param(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key) or []
    return str(values[0] or "").strip() if values else ""


def decode_data_ref(token: str) -> dict[str, Any]:
    padded = token + "=" * (-len(token) % 4)
    payload = base64.urlsafe_b64decode(padded.encode("ascii"))
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("download_ref must decode to an object.")
    return parsed


def encode_data_ref(ref: dict[str, Any]) -> str:
    payload = json.dumps(ref, ensure_ascii=False, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def download_filename(ref: dict[str, Any], suffix: str) -> str:
    seed = str(
        ref.get("label")
        or ref.get("source_alias")
        or ref.get("role")
        or ref.get("ref_id")
        or "data_ref"
    )
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", seed)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in cleaned
    )
    cleaned = cleaned.strip(" ._-") or "data_ref"
    return f"{cleaned}.{suffix}"


def content_disposition(filename: str) -> str:
    suffix = str(filename).rsplit(".", 1)[-1] or "csv"
    safe_filename = download_filename(
        {"label": re.sub(r"\.[A-Za-z0-9]+$", "", str(filename or "data_ref"))},
        suffix,
    )
    fallback = safe_filename.encode("ascii", errors="ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", fallback).strip("._") or "data_ref.csv"
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(safe_filename, safe='')}"
    )


def render_data_page(
    ref: dict[str, Any],
    loaded: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
    csv_url: str,
    json_url: str,
    preview_limit: int,
    offset: int = 0,
    view_url: str = "",
) -> str:
    row_count = int_or_zero(loaded.get("row_count")) or len(rows)
    title = ref_label(ref)
    summary = [
        ("ref_id", ref.get("ref_id")),
        ("role", ref.get("role")),
        ("path", ref.get("path")),
        ("database", loaded.get("database") or ref.get("database")),
        ("collection", loaded.get("collection_name") or ref.get("collection_name")),
        ("expires_at", loaded.get("expires_at")),
        ("rows", row_count),
        ("columns", len(columns)),
    ]
    summary_html = "\n".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>"
        for label, value in summary
        if value not in (None, "", [])
    )
    try:
        page_offset = max(int(offset), 0)
    except (TypeError, ValueError):
        page_offset = 0
    page_size = max(int(preview_limit), 0)
    page_start = page_offset + 1 if rows else 0
    page_end = min(page_offset + len(rows), row_count)
    pagination = _render_data_pagination(
        view_url,
        offset=page_offset,
        page_size=page_size,
        row_count=row_count,
        shown_count=len(rows),
    )
    range_note = (
        f"총 {row_count:,}건 중 {page_start:,}–{page_end:,}행을 표시합니다. "
        if rows
        else f"이 페이지에는 표시할 행이 없습니다 (전체 {row_count:,}건). "
    )
    filter_options = "".join(
        f'<option value="{escape(column, quote=True)}">{escape(column)}</option>'
        for column in columns
    )
    return page_shell(
        title,
        f"""
        <section class="page-hero">
          <p class="eyebrow">DATA EXPLORER</p>
          <h2>전체 데이터 탐색</h2>
          <p>표에서 원하는 컬럼을 필터·정렬하고 페이지 단위로 모든 저장 행을 확인할 수 있습니다.</p>
        </section>
        <section class="toolbar">
          <a class="button primary" href="{escape(csv_url, quote=True)}">전체 CSV 다운로드</a>
          <a class="button" href="{escape(json_url, quote=True)}">전체 JSON 다운로드</a>
        </section>
        <dl class="summary">{summary_html}</dl>
        <section id="data-explorer" class="data-explorer" data-json-url="{escape(json_url, quote=True)}" data-page-size="100">
          <div class="explorer-head">
            <div><h2>테이블</h2><p id="explorer-status">전체 데이터를 준비하는 중입니다.</p></div>
            <span class="explorer-badge">필터 · 정렬 · 페이지</span>
          </div>
          <div class="explorer-controls" aria-label="데이터 필터 및 정렬">
            <label>필터 컬럼<select id="filter-column"><option value="__all__">전체 컬럼</option>{filter_options}</select></label>
            <label class="filter-value">검색어<input id="filter-value" type="search" placeholder="포함할 값 입력"></label>
            <label>정렬 컬럼<select id="sort-column"><option value="">정렬 안 함</option>{filter_options}</select></label>
            <label>정렬 방향<select id="sort-direction"><option value="asc">오름차순</option><option value="desc">내림차순</option></select></label>
            <button id="reset-data-explorer" type="button" class="button">초기화</button>
          </div>
          <div id="explorer-table" class="table-wrap explorer-table" aria-live="polite"></div>
          <nav id="explorer-pagination" class="toolbar pagination" aria-label="데이터 페이지"></nav>
        </section>
        <section id="server-preview" class="server-preview">
          <p class="note">{range_note}브라우저에서 전체 데이터를 불러오지 못하면 아래 서버 미리보기가 표시됩니다. CSV 다운로드에는 전체 결과가 포함됩니다.</p>
          {render_table(rows, columns, start=page_offset)}
          {pagination}
        </section>
        <noscript><p class="note">필터·정렬 기능은 브라우저 JavaScript가 필요합니다. 아래 미리보기와 CSV 다운로드는 계속 사용할 수 있습니다.</p></noscript>
        <script>{_data_explorer_script()}</script>
        """,
    )


def _render_data_pagination(
    view_url: str,
    *,
    offset: int,
    page_size: int,
    row_count: int,
    shown_count: int,
) -> str:
    if not view_url or row_count <= 0 or page_size <= 0:
        return ""
    previous_offset = max(offset - page_size, 0)
    has_previous = offset > 0
    has_next = offset + shown_count < row_count
    if not has_previous and not has_next:
        return ""
    links = []
    if has_previous:
        links.append(
            f'<a class="button" href="{escape(_page_url(view_url, previous_offset), quote=True)}">이전 페이지</a>'
        )
    if has_next:
        links.append(
            f'<a class="button primary" href="{escape(_page_url(view_url, offset + page_size), quote=True)}">다음 페이지</a>'
        )
    return '<nav class="toolbar pagination" aria-label="데이터 페이지">' + "".join(links) + "</nav>"


def _page_url(view_url: str, offset: int) -> str:
    separator = "&" if "?" in view_url else "?"
    return f"{view_url}{separator}offset={max(int(offset), 0)}"


def _data_explorer_script() -> str:
    """Return static client code; all row values are assigned with textContent."""

    return r"""
(() => {
  const root = document.getElementById('data-explorer');
  if (!root || !window.fetch) return;
  const controls = {
    filterColumn: document.getElementById('filter-column'),
    filterValue: document.getElementById('filter-value'),
    sortColumn: document.getElementById('sort-column'),
    sortDirection: document.getElementById('sort-direction'),
    reset: document.getElementById('reset-data-explorer'),
  };
  const tableHost = document.getElementById('explorer-table');
  const paginationHost = document.getElementById('explorer-pagination');
  const status = document.getElementById('explorer-status');
  const fallback = document.getElementById('server-preview');
  const pageSize = Math.max(Number(root.dataset.pageSize) || 100, 1);
  const state = { rows: [], columns: [], page: 0, ready: false };

  const text = (value) => value === null || value === undefined ? '' : String(value);
  const normalized = (value) => text(value).trim().toLocaleLowerCase();
  const numeric = (value) => {
    const number = Number(text(value).replace(/,/g, ''));
    return Number.isFinite(number) ? number : null;
  };
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
  const make = (tag, value, className = '') => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };
  const filteredRows = () => {
    const needle = normalized(controls.filterValue.value);
    const field = controls.filterColumn.value;
    let rows = state.rows;
    if (needle) {
      rows = rows.filter((row) => {
        if (field === '__all__') return state.columns.some((column) => normalized(row[column]).includes(needle));
        return normalized(row[field]).includes(needle);
      });
    }
    const sortColumn = controls.sortColumn.value;
    if (sortColumn) {
      const direction = controls.sortDirection.value === 'desc' ? -1 : 1;
      rows = [...rows].sort((left, right) => {
        const leftNumber = numeric(left[sortColumn]);
        const rightNumber = numeric(right[sortColumn]);
        if (leftNumber !== null && rightNumber !== null) return (leftNumber - rightNumber) * direction;
        return text(left[sortColumn]).localeCompare(text(right[sortColumn]), 'ko', { numeric: true, sensitivity: 'base' }) * direction;
      });
    }
    return rows;
  };
  const render = () => {
    const rows = filteredRows();
    const pageCount = Math.max(Math.ceil(rows.length / pageSize), 1);
    state.page = Math.min(Math.max(state.page, 0), pageCount - 1);
    const start = state.page * pageSize;
    const visible = rows.slice(start, start + pageSize);
    clear(tableHost);
    clear(paginationHost);
    status.textContent = `전체 ${state.rows.length.toLocaleString()}건 중 필터 결과 ${rows.length.toLocaleString()}건 · ${start + (visible.length ? 1 : 0)}–${start + visible.length}행 표시`;
    if (!visible.length) {
      tableHost.appendChild(make('p', '조건에 맞는 데이터가 없습니다.', 'empty'));
    } else {
      const table = document.createElement('table');
      const head = document.createElement('thead');
      const headRow = document.createElement('tr');
      headRow.appendChild(make('th', 'No.'));
      state.columns.forEach((column) => headRow.appendChild(make('th', column)));
      head.appendChild(headRow);
      table.appendChild(head);
      const body = document.createElement('tbody');
      visible.forEach((row, index) => {
        const tr = document.createElement('tr');
        tr.appendChild(make('td', String(start + index + 1), 'row-number'));
        state.columns.forEach((column) => tr.appendChild(make('td', text(row[column]))));
        body.appendChild(tr);
      });
      table.appendChild(body);
      tableHost.appendChild(table);
    }
    if (pageCount > 1) {
      const previous = make('button', '이전 페이지', 'button');
      previous.type = 'button'; previous.disabled = state.page === 0;
      previous.addEventListener('click', () => { state.page -= 1; render(); });
      const current = make('span', `${state.page + 1} / ${pageCount} 페이지`, 'page-state');
      const next = make('button', '다음 페이지', 'button primary');
      next.type = 'button'; next.disabled = state.page >= pageCount - 1;
      next.addEventListener('click', () => { state.page += 1; render(); });
      paginationHost.append(previous, current, next);
    }
  };
  const refresh = () => { state.page = 0; render(); };
  [controls.filterColumn, controls.filterValue, controls.sortColumn, controls.sortDirection].forEach((control) => {
    control.addEventListener('input', refresh);
    control.addEventListener('change', refresh);
  });
  controls.reset.addEventListener('click', () => {
    controls.filterColumn.value = '__all__'; controls.filterValue.value = '';
    controls.sortColumn.value = ''; controls.sortDirection.value = 'asc'; refresh();
  });
  fetch(root.dataset.jsonUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
    .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
    .then((payload) => {
      const loaded = payload && payload.loaded && typeof payload.loaded === 'object' ? payload.loaded : {};
      const rows = Array.isArray(loaded.rows) ? loaded.rows.filter((row) => row && typeof row === 'object') : [];
      const columns = Array.isArray(loaded.columns) ? loaded.columns.map(text).filter(Boolean) : [];
      state.rows = rows;
      state.columns = columns.length ? columns : [...new Set(rows.flatMap((row) => Object.keys(row)))];
      state.ready = true;
      if (fallback) fallback.hidden = true;
      render();
    })
    .catch(() => {
      status.textContent = '전체 데이터를 브라우저에 불러오지 못했습니다. 아래 미리보기 또는 CSV 다운로드를 이용해 주세요.';
      root.classList.add('explorer-unavailable');
    });
})();
"""


def render_table(rows: list[dict[str, Any]], columns: list[str], start: int = 0) -> str:
    if not rows:
        return '<p class="empty">No rows are available for preview.</p>'
    head = '<th scope="col">No.</th>' + "".join(f"<th scope=\"col\">{escape(column)}</th>" for column in columns)
    body_rows = []
    for index, row in enumerate(rows, start=max(int_or_zero(start), 0) + 1):
        cells = "".join(f"<td>{escape(row.get(column, ''))}</td>" for column in columns)
        body_rows.append(f'<tr><td class="row-number">{index}</td>{cells}</tr>')
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def error_page(title: str, message: str) -> str:
    return page_shell(
        title,
        f'<p class="error">{escape(message)}</p><p class="note">Check the download_ref and API_SERVER .env settings.</p>',
    )


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ --ink:#182230; --muted:#5f6f85; --line:#dbe5f0; --blue:#1e64d0; --blue-soft:#eaf2ff; --surface:#fff; --background:#f4f7fb; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Malgun Gothic", "Apple SD Gothic Neo", Arial, sans-serif; color: var(--ink); background: var(--background); line-height: 1.5; }}
    main {{ max-width: 1320px; margin: 0 auto; padding: 30px; }}
    h1 {{ margin: 0 0 18px; font-size: 22px; }}
    h2 {{ margin: 0; letter-spacing: -.02em; }}
    .page-hero {{ margin: 0 0 18px; padding: 24px 26px; border-radius: 17px; color: #fff; background: linear-gradient(128deg, #14375f 0%, #2164c7 66%, #3c8cef 100%); box-shadow: 0 10px 24px rgba(30,49,76,.12); }}
    .page-hero h2 {{ font-size: 25px; }}
    .page-hero p {{ margin: 7px 0 0; color: #e7f1ff; font-size: 14px; }}
    .eyebrow {{ margin: 0 !important; color: #cfe3ff !important; font-size: 11px !important; font-weight: 800; letter-spacing: .08em; }}
    .toolbar {{ display: flex; align-items: center; gap: 8px; margin: 0 0 18px; flex-wrap: wrap; }}
    .button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 36px; padding: 8px 13px; border: 1px solid #b9c8d9; border-radius: 8px; color: #24466f; font: inherit; font-size: 13px; font-weight: 800; text-decoration: none; background: white; cursor: pointer; }}
    .button:hover {{ border-color: #7da7df; background: #f7fbff; }}
    .button:disabled {{ cursor: not-allowed; opacity: .48; }}
    .button.primary {{ background: var(--blue); color: white; border-color: var(--blue); }}
    .summary {{ display: grid; grid-template-columns: 150px 1fr; gap: 6px 12px; margin: 0 0 18px; background: white; padding: 15px; border: 1px solid var(--line); border-radius: 11px; }}
    .summary dt {{ font-weight: 800; color: #566f8c; }}
    .summary dd {{ margin: 0; overflow-wrap: anywhere; }}
    .note {{ color: var(--muted); font-size: 13px; }}
    .error {{ background: #fff1f0; border: 1px solid #ffccc7; color: #a8071a; padding: 14px; border-radius: 8px; }}
    .data-explorer {{ margin: 0 0 20px; padding: 18px; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); box-shadow: 0 5px 16px rgba(21,47,83,.04); }}
    .explorer-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }}
    .explorer-head h2 {{ font-size: 19px; }}
    .explorer-head p {{ margin: 4px 0 0; color: var(--muted); font-size: 13px; }}
    .explorer-badge {{ flex: 0 0 auto; padding: 4px 9px; border-radius: 999px; color: #28518a; background: var(--blue-soft); font-size: 12px; font-weight: 800; }}
    .explorer-controls {{ display: grid; grid-template-columns: minmax(130px,.8fr) minmax(180px,1.3fr) minmax(130px,.8fr) minmax(120px,.65fr) auto; gap: 10px; align-items: end; margin-bottom: 14px; }}
    .explorer-controls label {{ display: grid; gap: 5px; color: #52637b; font-size: 12px; font-weight: 800; }}
    .explorer-controls select, .explorer-controls input {{ width: 100%; min-height: 36px; padding: 7px 9px; border: 1px solid #cbd8e7; border-radius: 8px; color: var(--ink); background: #fff; font: inherit; font-size: 13px; }}
    .table-wrap {{ overflow: auto; max-height: 660px; border: 1px solid var(--line); border-radius: 10px; background: white; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ padding: 9px 11px; border-bottom: 1px solid #e9eef4; text-align: left; vertical-align: top; white-space: nowrap; }}
    th {{ position: sticky; top: 0; background: #f1f6fc; color: #435c77; z-index: 1; font-weight: 800; }}
    tr:last-child td {{ border-bottom: 0; }}
    .row-number {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
    .pagination {{ justify-content: center; margin: 14px 0 0; }}
    .page-state {{ padding: 7px 9px; color: var(--muted); font-size: 13px; font-weight: 800; }}
    .server-preview {{ margin: 0 0 20px; }}
    .server-preview .note {{ margin: 0 0 10px; }}
    .empty {{ margin: 0; background: white; border: 1px dashed #c7d4e3; border-radius: 8px; padding: 14px; color: var(--muted); }}
    @media (max-width: 960px) {{ main {{ padding: 16px; }} .explorer-controls {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .explorer-controls .filter-value {{ grid-column: span 2; }} }}
    @media (max-width: 620px) {{ .page-hero {{ padding: 20px; }} .explorer-head {{ display: block; }} .explorer-badge {{ display: inline-flex; margin-top: 9px; }} .explorer-controls {{ grid-template-columns: 1fr; }} .explorer-controls .filter-value {{ grid-column: auto; }} .summary {{ grid-template-columns: 1fr; gap: 2px; }} .summary dd {{ margin-bottom: 8px; }} }}
  </style>
</head>
<body><main><h1>{escape(title)}</h1>{body}</main></body>
</html>"""


def ref_label(ref: dict[str, Any]) -> str:
    label = str(ref.get("label") or "").strip()
    if label:
        return label
    role = str(ref.get("role") or "").strip()
    alias = str(ref.get("source_alias") or ref.get("dataset_key") or "").strip()
    if role == "analysis_result":
        return "Analysis result data"
    if role == "source_rows" and alias:
        return f"Source data: {alias}"
    return "MongoDB data"


def int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def escape(value: Any, quote: bool = True) -> str:
    return html.escape(str(value if value is not None else ""), quote=quote)
