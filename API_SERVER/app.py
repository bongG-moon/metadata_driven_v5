"""Standalone FastAPI entry point for the metadata-driven artifact service.

Run this file directly from the API_SERVER folder with: python app.py

It deliberately uses the same Uvicorn form as the existing deployment:
uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field

try:  # Supports both "python app.py" and "import API_SERVER.app".
    from .data_ref_store import DEFAULT_DATABASE, DEFAULT_RESULT_COLLECTION
    from .support import (
        DEFAULT_MAX_DOWNLOAD_BYTES,
        DEFAULT_MAX_REPORT_HTML_BYTES,
        DEFAULT_MAX_REPORT_STORAGE_BYTES,
        DEFAULT_MAX_REPORT_TTL_HOURS,
        DEFAULT_PREVIEW_LIMIT,
        DEFAULT_REPORT_COLLECTION,
        DEFAULT_REPORT_TTL_HOURS,
        REPORT_STORE_LOCK,
        ReportHttpError,
        ServerConfig,
        cleanup_html_reports_unlocked,
        content_disposition,
        create_html_report,
        delete_html_report,
        download_filename,
        encode_data_ref,
        error_page,
        load_active_html_report,
        load_dotenv,
        mask_download_server_log,
        prepare_report_storage,
        render_data_page,
        report_content_disposition,
        report_storage_readiness,
        resolve_request,
        resolved_status,
        rows_columns,
        storage_descriptor,
    )
except ImportError:  # pragma: no cover - exercised by direct script execution.
    from data_ref_store import DEFAULT_DATABASE, DEFAULT_RESULT_COLLECTION  # type: ignore[no-redef]
    from support import (  # type: ignore[no-redef]
        DEFAULT_MAX_DOWNLOAD_BYTES,
        DEFAULT_MAX_REPORT_HTML_BYTES,
        DEFAULT_MAX_REPORT_STORAGE_BYTES,
        DEFAULT_MAX_REPORT_TTL_HOURS,
        DEFAULT_PREVIEW_LIMIT,
        DEFAULT_REPORT_COLLECTION,
        DEFAULT_REPORT_TTL_HOURS,
        REPORT_STORE_LOCK,
        ReportHttpError,
        ServerConfig,
        cleanup_html_reports_unlocked,
        content_disposition,
        create_html_report,
        delete_html_report,
        download_filename,
        encode_data_ref,
        error_page,
        load_active_html_report,
        load_dotenv,
        mask_download_server_log,
        prepare_report_storage,
        render_data_page,
        report_content_disposition,
        report_storage_readiness,
        resolve_request,
        resolved_status,
        rows_columns,
        storage_descriptor,
    )


ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("metadata_driven_v5.api_server")
COMMON_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
REPORT_HEADERS = {
    **COMMON_HEADERS,
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
REPORT_VIEW_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; img-src 'self' data: blob:; style-src 'unsafe-inline'; "
    # Report-internal data tabs fetch only same-origin /download.json data-ref
    # URLs.  Keep external network access forbidden while allowing the report
    # to load its own already-authorized stored rows in the same page.
    "script-src 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; "
    "media-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)


class ReportCreateRequest(BaseModel):
    """Request schema preserved from the existing artifact server."""

    model_config = ConfigDict(extra="forbid")

    html: str
    title: str = "HTML Report"
    question: str = ""
    view_request: str = ""
    available_datasets: list[dict[str, Any]] = Field(default_factory=list)
    report_plan: dict[str, Any] = Field(default_factory=dict)
    ttl_hours: int | None = None
    filename_hint: str = "report"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return max(minimum, int(raw_value))
    except ValueError:
        LOGGER.warning("Ignoring invalid integer environment value: %s", name)
        return default


def config_from_env() -> ServerConfig:
    """Build runtime settings from API_SERVER's own .env file and environment."""
    env_file = os.getenv("API_SERVER_ENV_FILE") or str(ROOT / ".env")
    load_dotenv(env_file)

    public_base_url = (
        os.getenv("API_SERVER_PUBLIC_BASE_URL")
        or os.getenv("ARTIFACT_PUBLIC_BASE_URL")
        or os.getenv("REPORT_BASE_URL")
        or "http://127.0.0.1:5000"
    )
    data_mongo_uri = (
        os.getenv("API_SERVER_MONGODB_URI")
        or os.getenv("MONGODB_URI")
        or os.getenv("MONGO_URI")
        or ""
    )
    data_mongo_database = (
        os.getenv("API_SERVER_MONGODB_DATABASE")
        or os.getenv("MONGODB_DATABASE")
        or os.getenv("MONGO_DB_NAME")
        or DEFAULT_DATABASE
    )
    report_mongo_uri = (
        os.getenv("API_SERVER_REPORT_MONGODB_URI")
        or os.getenv("REPORT_MONGODB_URI")
        or data_mongo_uri
    )
    report_mongo_database = (
        os.getenv("API_SERVER_REPORT_MONGODB_DATABASE")
        or os.getenv("REPORT_MONGODB_DATABASE")
        or data_mongo_database
    )
    return ServerConfig(
        mongo_uri=data_mongo_uri,
        mongo_database=data_mongo_database,
        result_collection=os.getenv("API_SERVER_RESULT_COLLECTION")
        or os.getenv("MONGODB_RESULT_COLLECTION")
        or DEFAULT_RESULT_COLLECTION,
        preview_limit=_int_env(
            "API_SERVER_PREVIEW_LIMIT",
            _int_env("DATA_REF_DOWNLOAD_PREVIEW_LIMIT", DEFAULT_PREVIEW_LIMIT),
        ),
        max_download_bytes=_int_env(
            "API_SERVER_MAX_DOWNLOAD_BYTES",
            _int_env("DATA_REF_DOWNLOAD_MAX_BYTES", DEFAULT_MAX_DOWNLOAD_BYTES, 1024),
            1024,
        ),
        host="0.0.0.0",
        port=5000,
        report_mongo_uri=report_mongo_uri,
        report_database=report_mongo_database,
        report_collection=(
            os.getenv("API_SERVER_REPORT_COLLECTION")
            or os.getenv("REPORT_COLLECTION")
            # Existing deployments can retain the previous collection setting.
            or os.getenv("API_SERVER_REPORT_METADATA_COLLECTION")
            or os.getenv("REPORT_METADATA_COLLECTION")
            or DEFAULT_REPORT_COLLECTION
        ),
        report_base_url=public_base_url,
        report_default_ttl_hours=_int_env(
            "REPORT_DEFAULT_TTL_HOURS",
            DEFAULT_REPORT_TTL_HOURS,
            1,
        ),
        report_max_ttl_hours=_int_env(
            "REPORT_MAX_TTL_HOURS",
            DEFAULT_MAX_REPORT_TTL_HOURS,
            1,
        ),
        max_report_html_bytes=_int_env(
            "REPORT_MAX_HTML_BYTES",
            DEFAULT_MAX_REPORT_HTML_BYTES,
            1024,
        ),
        max_report_storage_bytes=_int_env(
            "REPORT_MAX_STORAGE_BYTES",
            DEFAULT_MAX_REPORT_STORAGE_BYTES,
            1024,
        ),
        use_report_access_token=_bool_env("REPORT_USE_ACCESS_TOKEN", False),
    )


def _readiness(config: ServerConfig) -> dict[str, Any]:
    checks: dict[str, bool] = {"data_ref_mongo": False, "report_storage_mongo": False}
    errors: list[str] = []

    if not config.mongo_uri:
        errors.append("MONGODB_URI is not configured")
    else:
        client: Any = None
        try:
            from pymongo import MongoClient

            client = MongoClient(
                config.mongo_uri,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                socketTimeoutMS=3000,
            )
            client.admin.command("ping")
            checks["data_ref_mongo"] = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"data_ref_mongo: {type(exc).__name__}: {exc}")
        finally:
            if client is not None:
                client.close()

    report_status = report_storage_readiness(config)
    checks["report_storage_mongo"] = bool(report_status["ok"])
    if report_status["error"]:
        errors.append(f"report_storage_mongo: {report_status['error']}")
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "errors": errors,
        "report_storage": report_status["storage"],
    }


async def _cleanup_loop(config: ServerConfig, stop: asyncio.Event) -> None:
    interval = max(60, _int_env("API_SERVER_CLEANUP_INTERVAL_SECONDS", 900, 1))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                await asyncio.to_thread(_cleanup_once, config)
            except Exception:  # noqa: BLE001
                LOGGER.exception("artifact cleanup failed")


def _cleanup_once(config: ServerConfig) -> None:
    with REPORT_STORE_LOCK:
        cleanup_html_reports_unlocked(config)


def _csv_row_bytes(values: list[Any]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(values)
    return buffer.getvalue().encode("utf-8")


def _iter_csv_bytes(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> Iterator[bytes]:
    yield b"\xef\xbb\xbf"
    yield _csv_row_bytes(columns)
    for raw in rows:
        row = raw if isinstance(raw, dict) else {}
        yield _csv_row_bytes([row.get(column, "") for column in columns])


def _csv_size_within_limit(
    rows: list[dict[str, Any]],
    columns: list[str],
    max_bytes: int,
) -> int | None:
    total = 0
    for chunk in _iter_csv_bytes(rows, columns):
        total += len(chunk)
        if total > max_bytes:
            return None
    return total


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Create a self-contained Artifact API application."""
    supplied_config = config
    download_slots = max(1, _int_env("API_SERVER_MAX_CONCURRENT_DOWNLOADS", 4, 1))
    download_semaphore = threading.BoundedSemaphore(download_slots)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_config = supplied_config or config_from_env()
        try:
            prepare_report_storage(active_config)
        except ReportHttpError as exc:
            LOGGER.warning("MongoDB report storage is not ready at startup: %s", exc.message)
        application.state.config = active_config
        stop = asyncio.Event()
        cleanup_task = asyncio.create_task(_cleanup_loop(active_config, stop))
        application.state.cleanup_stop = stop
        application.state.cleanup_task = cleanup_task
        try:
            yield
        finally:
            stop.set()
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

    application = FastAPI(
        title="metadata_driven_v5 Artifact API Server",
        description=(
            "Standalone FastAPI server for data_ref CSV/JSON downloads and "
            "HTML report storage."
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    def active_config(request: Request) -> ServerConfig:
        return request.app.state.config

    @application.middleware("http")
    async def request_guard(request: Request, call_next: Any) -> Response:
        config_value = getattr(request.app.state, "config", supplied_config)
        max_body = getattr(
            config_value,
            "max_report_request_bytes",
            DEFAULT_MAX_REPORT_HTML_BYTES + 65536,
        )
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > int(max_body):
                    return JSONResponse(
                        {"detail": f"request body is too large. max_bytes={max_body}"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
        response = await call_next(request)
        LOGGER.info(
            "%s %s %s",
            request.method,
            mask_download_server_log(
                str(request.url.path)
                + (f"?{request.url.query}" if request.url.query else "")
            ),
            response.status_code,
        )
        return response

    @application.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        """Open the built-in Swagger UI from the base URL."""
        return RedirectResponse("/docs")

    @application.get("/live")
    def live() -> dict[str, Any]:
        return {"ok": True, "service": "metadata-driven-v5-artifact-api-server"}

    @application.get("/health")
    @application.get("/healthz")
    def health(request: Request) -> dict[str, Any]:
        config_value = active_config(request)
        return {
            "ok": True,
            "service": "metadata-driven-v5-artifact-api-server",
            "listen": {
                "host": config_value.host,
                "port": config_value.port,
            },
            "features": {
                "data_ref_csv": True,
                "html_reports": True,
            },
            "report_base_url": config_value.report_base_url,
            "report_storage": storage_descriptor(config_value),
        }

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        result = _readiness(active_config(request))
        return JSONResponse(result, status_code=200 if result["ok"] else 503)

    def resolve_data(
        query: str,
        config_value: ServerConfig,
        limit: int | None,
    ) -> tuple[dict[str, Any], Response | None]:
        resolved = resolve_request(query, config_value, limit=limit)
        if resolved["ok"]:
            return resolved, None
        return (
            resolved,
            Response(
                str(resolved["message"]),
                status_code=int(resolved_status(resolved)),
                media_type="text/plain; charset=utf-8",
                headers=COMMON_HEADERS,
            ),
        )

    @application.get("/download")
    @application.get("/download.csv")
    def download_csv(request: Request) -> Response:
        config_value = active_config(request)
        with download_semaphore:
            resolved, error = resolve_data(request.url.query, config_value, None)
            if error is not None:
                return error
            ref = resolved["ref"]
            loaded = resolved["loaded"]
            rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
            columns = (
                loaded.get("columns")
                if isinstance(loaded.get("columns"), list)
                else rows_columns(rows)
            )
            payload_size = _csv_size_within_limit(
                rows,
                columns,
                config_value.max_download_bytes,
            )
            if payload_size is None:
                return Response(
                    (
                        "CSV download exceeds the configured limit. "
                        f"max_bytes={config_value.max_download_bytes}"
                    ),
                    status_code=413,
                    media_type="text/plain; charset=utf-8",
                    headers=COMMON_HEADERS,
                )
            filename = download_filename(ref, "csv")
            return StreamingResponse(
                _iter_csv_bytes(rows, columns),
                media_type="text/csv; charset=utf-8",
                headers={
                    **COMMON_HEADERS,
                    "Content-Disposition": content_disposition(filename),
                    "Content-Length": str(payload_size),
                },
            )

    @application.get("/download.json")
    def download_json(request: Request) -> Response:
        config_value = active_config(request)
        with download_semaphore:
            resolved, error = resolve_data(request.url.query, config_value, None)
            if error is not None:
                return error
            payload = json.dumps(
                {"data_ref": resolved["ref"], "loaded": resolved["loaded"]},
                ensure_ascii=False,
                indent=2,
                default=str,
            ).encode("utf-8")
            if len(payload) > config_value.max_download_bytes:
                return Response(
                    (
                        "JSON download exceeds the configured limit. "
                        f"max_bytes={config_value.max_download_bytes}"
                    ),
                    status_code=413,
                    media_type="text/plain; charset=utf-8",
                    headers=COMMON_HEADERS,
                )
            filename = download_filename(resolved["ref"], "json")
            return Response(
                payload,
                media_type="application/json; charset=utf-8",
                headers={
                    **COMMON_HEADERS,
                    "Content-Disposition": content_disposition(filename),
                },
            )

    @application.get("/view", response_class=HTMLResponse)
    def view_data(request: Request, offset: int = Query(default=0, ge=0)) -> Response:
        config_value = active_config(request)
        page_size = max(int(config_value.preview_limit), 1)
        resolved = resolve_request(
            request.url.query,
            config_value,
            limit=page_size,
            offset=offset,
        )
        if not resolved["ok"]:
            return HTMLResponse(
                error_page("Download link error", str(resolved["message"])),
                status_code=int(resolved_status(resolved)),
                headers=COMMON_HEADERS,
            )
        ref = resolved["ref"]
        loaded = resolved["loaded"]
        rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
        columns = (
            loaded.get("columns")
            if isinstance(loaded.get("columns"), list)
            else rows_columns(rows)
        )
        csv_url = "/download.csv?download_ref=" + encode_data_ref(ref)
        json_url = "/download.json?download_ref=" + encode_data_ref(ref)
        view_url = "/view?download_ref=" + encode_data_ref(ref)
        return HTMLResponse(
            render_data_page(
                ref,
                loaded,
                rows,
                columns,
                csv_url,
                json_url,
                page_size,
                offset=offset,
                view_url=view_url,
            ),
            headers=COMMON_HEADERS,
        )

    @application.post("/reports", status_code=201)
    def create_report(
        request: Request,
        payload: ReportCreateRequest,
    ) -> dict[str, Any]:
        return create_html_report(payload.model_dump(), active_config(request))

    def report_response(
        request: Request,
        report_id: str,
        token: str,
        download: bool,
    ) -> Response:
        config_value = active_config(request)
        metadata, html_bytes = load_active_html_report(report_id, token, config_value)
        filename = str(
            metadata.get("download_filename") or metadata.get("title") or report_id
        )
        headers = dict(REPORT_HEADERS)
        headers["Content-Disposition"] = report_content_disposition(
            filename,
            "attachment" if download else "inline",
        )
        if not download:
            headers["Content-Security-Policy"] = REPORT_VIEW_CONTENT_SECURITY_POLICY
        return Response(
            html_bytes,
            media_type="text/html; charset=utf-8",
            headers=headers,
        )

    @application.get("/reports/view/{report_id}")
    def view_report(
        request: Request,
        report_id: str,
        token: str = Query(default="", max_length=128),
    ) -> Response:
        return report_response(request, report_id, token, False)

    @application.get("/reports/download/{report_id}")
    def download_report(
        request: Request,
        report_id: str,
        token: str = Query(default="", max_length=128),
    ) -> Response:
        return report_response(request, report_id, token, True)

    @application.delete("/reports/{report_id}")
    def remove_report(
        request: Request,
        report_id: str,
        token: str = Query(default="", max_length=128),
    ) -> dict[str, Any]:
        deleted = delete_html_report(report_id, token, active_config(request))
        return {"status": "ok", "deleted": True, "report_id": deleted}

    @application.exception_handler(ReportHttpError)
    async def report_error_handler(_: Request, exc: ReportHttpError) -> JSONResponse:
        return JSONResponse(
            {"detail": exc.message},
            status_code=int(exc.status),
            headers=COMMON_HEADERS,
        )

    return application


# Both names are intentional: "application" matches the existing production
# command, while "app" supports normal Uvicorn import syntax.
application = create_app()
app = application


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
