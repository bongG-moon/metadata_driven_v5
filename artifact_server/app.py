from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import tempfile
import threading
from contextlib import asynccontextmanager, suppress
from http import HTTPStatus
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from tools.data_ref_download_server import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DEFAULT_MAX_REPORT_HTML_BYTES,
    DEFAULT_MAX_REPORT_STORAGE_BYTES,
    DEFAULT_MAX_REPORT_TTL_HOURS,
    DEFAULT_PREVIEW_LIMIT,
    DEFAULT_REPORT_STORAGE_DIR,
    DEFAULT_REPORT_TTL_HOURS,
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
    report_reports_dir,
    resolve_request,
    resolved_status,
    rows_columns,
)
from web_app.data_ref_store import DEFAULT_DATABASE, DEFAULT_RESULT_COLLECTION


LOGGER = logging.getLogger("metadata_driven_v5.artifact_server")
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
    "script-src 'unsafe-inline'; font-src 'self' data:; connect-src 'none'; "
    "media-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'none'"
)


class ReportCreateRequest(BaseModel):
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


def config_from_env() -> ServerConfig:
    env_file = os.getenv("ARTIFACT_ENV_FILE") or os.getenv("DATA_REF_DOWNLOAD_ENV_FILE") or str(
        Path(__file__).resolve().parents[1] / ".env"
    )
    load_dotenv(env_file)
    host = os.getenv("ARTIFACT_LISTEN_HOST") or os.getenv("DATA_REF_DOWNLOAD_HOST") or "127.0.0.1"
    port = int(os.getenv("ARTIFACT_LISTEN_PORT") or os.getenv("DATA_REF_DOWNLOAD_PORT") or "8765")
    public_base_url = (
        os.getenv("ARTIFACT_PUBLIC_BASE_URL")
        or os.getenv("DATA_REF_DOWNLOAD_BASE_URL")
        or os.getenv("REPORT_BASE_URL")
        or f"http://127.0.0.1:{port}"
    )
    return ServerConfig(
        mongo_uri=os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "",
        mongo_database=os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DB_NAME") or DEFAULT_DATABASE,
        result_collection=os.getenv("MONGODB_RESULT_COLLECTION") or DEFAULT_RESULT_COLLECTION,
        preview_limit=max(0, int(os.getenv("DATA_REF_DOWNLOAD_PREVIEW_LIMIT", str(DEFAULT_PREVIEW_LIMIT)))),
        max_download_bytes=max(
            1024,
            int(os.getenv("DATA_REF_DOWNLOAD_MAX_BYTES", str(DEFAULT_MAX_DOWNLOAD_BYTES))),
        ),
        host=host,
        port=port,
        report_storage_dir=os.getenv("REPORT_STORAGE_DIR") or str(DEFAULT_REPORT_STORAGE_DIR),
        report_base_url=public_base_url,
        report_default_ttl_hours=int(
            os.getenv("REPORT_DEFAULT_TTL_HOURS", str(DEFAULT_REPORT_TTL_HOURS))
        ),
        report_max_ttl_hours=int(
            os.getenv("REPORT_MAX_TTL_HOURS", str(DEFAULT_MAX_REPORT_TTL_HOURS))
        ),
        max_report_html_bytes=int(
            os.getenv("REPORT_MAX_HTML_BYTES", str(DEFAULT_MAX_REPORT_HTML_BYTES))
        ),
        max_report_storage_bytes=int(
            os.getenv("REPORT_MAX_STORAGE_BYTES", str(DEFAULT_MAX_REPORT_STORAGE_BYTES))
        ),
        use_report_access_token=_bool_env("REPORT_USE_ACCESS_TOKEN", False),
    )


def _readiness(config: ServerConfig) -> dict[str, Any]:
    checks: dict[str, Any] = {"mongo": False, "storage": False}
    errors: list[str] = []
    if not config.mongo_uri:
        errors.append("MONGODB_URI is not configured")
    else:
        client = None
        try:
            from pymongo import MongoClient

            client = MongoClient(
                config.mongo_uri,
                serverSelectionTimeoutMS=3000,
                connectTimeoutMS=3000,
                socketTimeoutMS=3000,
            )
            client.admin.command("ping")
            checks["mongo"] = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"mongo: {type(exc).__name__}: {exc}")
        finally:
            if client is not None:
                client.close()
    try:
        reports_dir = report_reports_dir(config)
        reports_dir.mkdir(parents=True, exist_ok=True)
        handle, probe_name = tempfile.mkstemp(prefix=".ready-", dir=reports_dir)
        os.close(handle)
        Path(probe_name).unlink(missing_ok=True)
        checks["storage"] = True
    except Exception as exc:  # noqa: BLE001
        errors.append(f"storage: {type(exc).__name__}: {exc}")
    return {"ok": all(checks.values()), "checks": checks, "errors": errors}


async def _cleanup_loop(config: ServerConfig, stop: asyncio.Event) -> None:
    interval = max(60, int(os.getenv("ARTIFACT_CLEANUP_INTERVAL_SECONDS", "900")))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                await asyncio.to_thread(_cleanup_once, config)
            except Exception:  # noqa: BLE001
                LOGGER.exception("artifact cleanup failed")


def _cleanup_once(config: ServerConfig) -> None:
    from tools.data_ref_download_server import REPORT_STORE_LOCK

    with REPORT_STORE_LOCK:
        cleanup_html_reports_unlocked(config)


def _csv_row_bytes(values: list[Any]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(values)
    return buffer.getvalue().encode("utf-8")


def _iter_csv_bytes(rows: list[dict[str, Any]], columns: list[str]) -> Iterator[bytes]:
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
    supplied_config = config
    download_slots = max(1, int(os.getenv("ARTIFACT_MAX_CONCURRENT_DOWNLOADS", "4")))
    download_semaphore = threading.BoundedSemaphore(download_slots)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_config = supplied_config or config_from_env()
        prepare_report_storage(active_config)
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
        title="metadata_driven_v5 Artifact Server",
        version="1.0.0",
        lifespan=lifespan,
    )

    def active_config(request: Request) -> ServerConfig:
        return request.app.state.config

    @application.middleware("http")
    async def request_guard(request: Request, call_next: Any) -> Response:
        config_value = getattr(request.app.state, "config", supplied_config)
        max_body = getattr(config_value, "max_report_request_bytes", DEFAULT_MAX_REPORT_HTML_BYTES + 65536)
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
            mask_download_server_log(str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")),
            response.status_code,
        )
        return response

    @application.get("/live")
    def live() -> dict[str, Any]:
        return {"ok": True, "service": "metadata-driven-v5-artifact-server"}

    @application.get("/health")
    @application.get("/healthz")
    def health(request: Request) -> dict[str, Any]:
        config_value = active_config(request)
        return {
            "ok": True,
            "service": "metadata-driven-v5-artifact-server",
            "features": {"data_ref_csv": True, "html_reports": True},
            "report_base_url": config_value.report_base_url,
        }

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        result = _readiness(active_config(request))
        return JSONResponse(result, status_code=200 if result["ok"] else 503)

    def resolve_data(query: str, config_value: ServerConfig, limit: int | None) -> tuple[dict[str, Any], Response | None]:
        resolved = resolve_request(query, config_value, limit=limit)
        if resolved["ok"]:
            return resolved, None
        status = int(resolved_status(resolved))
        return resolved, Response(
            str(resolved["message"]),
            status_code=status,
            media_type="text/plain; charset=utf-8",
            headers=COMMON_HEADERS,
        )

    @application.get("/")
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
            columns = loaded.get("columns") if isinstance(loaded.get("columns"), list) else rows_columns(rows)
            payload_size = _csv_size_within_limit(rows, columns, config_value.max_download_bytes)
            if payload_size is None:
                return Response(
                    f"CSV 파일이 다운로드 상한을 초과했습니다. max_bytes={config_value.max_download_bytes}",
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
                    f"JSON 파일이 다운로드 상한을 초과했습니다. max_bytes={config_value.max_download_bytes}",
                    status_code=413,
                    media_type="text/plain; charset=utf-8",
                    headers=COMMON_HEADERS,
                )
            filename = download_filename(resolved["ref"], "json")
            return Response(
                payload,
                media_type="application/json; charset=utf-8",
                headers={**COMMON_HEADERS, "Content-Disposition": content_disposition(filename)},
            )

    @application.get("/view", response_class=HTMLResponse)
    def view_data(request: Request) -> Response:
        config_value = active_config(request)
        resolved = resolve_request(request.url.query, config_value, limit=config_value.preview_limit)
        if not resolved["ok"]:
            return HTMLResponse(
                error_page("다운로드 링크 오류", resolved["message"]),
                status_code=int(resolved_status(resolved)),
                headers=COMMON_HEADERS,
            )
        ref = resolved["ref"]
        loaded = resolved["loaded"]
        rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
        columns = loaded.get("columns") if isinstance(loaded.get("columns"), list) else rows_columns(rows)
        csv_url = "/download.csv?" + "download_ref=" + encode_data_ref(ref)
        json_url = "/download.json?" + "download_ref=" + encode_data_ref(ref)
        return HTMLResponse(
            render_data_page(ref, loaded, rows, columns, csv_url, json_url, config_value.preview_limit),
            headers=COMMON_HEADERS,
        )

    @application.post("/reports", status_code=201)
    def create_report(request: Request, payload: ReportCreateRequest) -> dict[str, Any]:
        return create_html_report(payload.model_dump(), active_config(request))

    def report_response(request: Request, report_id: str, token: str, download: bool) -> Response:
        config_value = active_config(request)
        metadata, html_bytes = load_active_html_report(report_id, token, config_value)
        filename = str(metadata.get("download_filename") or metadata.get("title") or report_id)
        headers = dict(REPORT_HEADERS)
        headers["Content-Disposition"] = report_content_disposition(
            filename,
            "attachment" if download else "inline",
        )
        if not download:
            headers["Content-Security-Policy"] = REPORT_VIEW_CONTENT_SECURITY_POLICY
        return Response(html_bytes, media_type="text/html; charset=utf-8", headers=headers)

    @application.get("/reports/view/{report_id}")
    def view_report(request: Request, report_id: str, token: str = Query(default="", max_length=128)) -> Response:
        return report_response(request, report_id, token, False)

    @application.get("/reports/download/{report_id}")
    def download_report(request: Request, report_id: str, token: str = Query(default="", max_length=128)) -> Response:
        return report_response(request, report_id, token, True)

    @application.delete("/reports/{report_id}")
    def remove_report(request: Request, report_id: str, token: str = Query(default="", max_length=128)) -> dict[str, Any]:
        deleted = delete_html_report(report_id, token, active_config(request))
        return {"status": "ok", "deleted": True, "report_id": deleted}

    from tools.data_ref_download_server import ReportHttpError

    @application.exception_handler(ReportHttpError)
    async def report_error_handler(_: Request, exc: ReportHttpError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=int(exc.status), headers=COMMON_HEADERS)

    return application


app = create_app()
