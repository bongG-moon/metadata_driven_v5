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
    report_id = now.strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(16)
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
        "expires_at": now + timedelta(hours=ttl_hours),
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
        "expires_at": report_iso(now + timedelta(hours=ttl_hours)),
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
    return value.astimezone(timezone.utc).isoformat()


def resolve_request(
    query: str,
    config: ServerConfig,
    limit: int | None,
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
    return page_shell(
        title,
        f"""
        <section class="toolbar">
          <a class="button primary" href="{escape(csv_url)}">Download CSV</a>
          <a class="button" href="{escape(json_url)}">Download JSON</a>
        </section>
        <dl class="summary">{summary_html}</dl>
        <p class="note">Showing up to {preview_limit:,} rows. Downloads contain the complete result.</p>
        {render_table(rows[:preview_limit], columns)}
        """,
    )


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return '<p class="empty">No rows are available for preview.</p>'
    head = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(row.get(column, ''))}</td>" for column in columns)
        body_rows.append(f"<tr>{cells}</tr>")
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; color: #17202a; background: #f6f7f9; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 18px; font-size: 22px; }}
    .toolbar {{ display: flex; gap: 8px; margin: 0 0 18px; flex-wrap: wrap; }}
    .button {{ display: inline-block; padding: 9px 13px; border-radius: 6px; border: 1px solid #b9c0ca; color: #17202a; text-decoration: none; background: white; }}
    .button.primary {{ background: #1f6feb; color: white; border-color: #1f6feb; }}
    .summary {{ display: grid; grid-template-columns: 150px 1fr; gap: 6px 12px; background: white; padding: 14px; border: 1px solid #d8dee8; border-radius: 8px; }}
    .summary dt {{ font-weight: 700; color: #56606f; }}
    .summary dd {{ margin: 0; overflow-wrap: anywhere; }}
    .note {{ color: #5d6878; font-size: 13px; }}
    .error {{ background: #fff1f0; border: 1px solid #ffccc7; color: #a8071a; padding: 14px; border-radius: 8px; }}
    .table-wrap {{ overflow: auto; border: 1px solid #d8dee8; border-radius: 8px; background: white; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #e7ebf0; text-align: left; white-space: nowrap; }}
    th {{ position: sticky; top: 0; background: #eef2f7; z-index: 1; }}
    .empty {{ background: white; border: 1px solid #d8dee8; border-radius: 8px; padding: 14px; }}
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


def escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)
