from __future__ import annotations

"""metadata_driven_v5용 로컬 HTML 리포트 저장/공유 API.

Langflow가 만든 self-contained HTML 문자열을 로컬 파일로 저장하고, 만료 시간이
있는 보기/다운로드 URL을 반환한다. 외부 DB나 .env 없이 단일 프로세스로 실행하는
지원 서버이며, 설정은 아래 상수 영역에서 명시적으로 관리한다.
"""

import hashlib
import hmac
import json
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse


# ---------------------------------------------------------------------------
# 실행 설정: 배포 환경에 맞게 이 영역만 검토한다.
# ---------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8010

# 클라이언트에게 돌려줄 절대 URL의 기준 주소다. SERVER_HOST를 0.0.0.0으로
# 바꾸더라도 BASE_URL에는 실제 사용자가 접속할 IP/DNS 주소를 적어야 한다.
BASE_URL = f"http://127.0.0.1:{SERVER_PORT}"

STORAGE_DIR = Path(__file__).resolve().parent / "storage"
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 24 * 7

MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 1 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_HTML_BYTES + MAX_METADATA_BYTES + (64 * 1024)
MAX_STORAGE_BYTES = 512 * 1024 * 1024
MAX_DATASET_REFS = 100

# True이면 새 리포트마다 토큰을 만들고 URL의 ?token=...으로 전달한다.
# 토큰 원문은 파일에 저장하지 않고 SHA-256 해시만 저장한다.
USE_ACCESS_TOKEN = False

# query token은 일반 access log에 남을 수 있으므로 기본값은 False다.
ENABLE_ACCESS_LOG = False

# 보기 페이지를 고유 origin의 sandbox로 격리하고 외부 통신을 막는다.
# v5 기본 HTML/SVG는 외부 CDN 없이 동작한다. 외부 리소스가 꼭 필요하면 보안 검토 후
# 이 정책을 좁은 도메인 allow-list 방식으로 수정한다.
VIEW_CONTENT_SECURITY_POLICY = (
    "sandbox allow-scripts allow-downloads; "
    "default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval' blob:; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-src 'none'"
)


REPORT_ID_PATTERN = re.compile(r"[0-9]{14}_[a-f0-9]{32}")
TOKEN_PATTERN = re.compile(r"[a-f0-9]{32,128}")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_STORE_LOCK = threading.RLock()


class DatasetRef(BaseModel):
    """리포트 생성에 사용한 데이터셋의 작은 참조 정보."""

    dataset_id: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=300)
    data_ref: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list, max_length=500)
    row_count: int = Field(default=0, ge=0)


class CreateReportRequest(BaseModel):
    """POST /reports 요청 계약."""

    html: str = Field(..., description="완성된 self-contained HTML 문자열")
    title: str = Field(default="HTML Report", max_length=200)
    question: str = Field(default="", max_length=4_000)
    view_request: str = Field(default="", max_length=1_000)
    available_datasets: list[DatasetRef] = Field(default_factory=list, max_length=MAX_DATASET_REFS)
    report_plan: dict[str, Any] = Field(default_factory=dict)
    ttl_hours: int | None = Field(default=None, ge=1)
    filename_hint: str = Field(default="report", max_length=200)


class CreateReportResponse(BaseModel):
    """v5 HTML 시각화 컴포넌트가 사용하는 링크 응답 계약."""

    report_id: str
    title: str
    view_url: str
    download_url: str
    expires_at: str
    ttl_hours: int


class RequestBodyLimitMiddleware:
    """POST /reports의 body를 파싱하기 전에 전체 byte 상한을 적용한다."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        is_report_post = (
            scope.get("type") == "http"
            and str(scope.get("method") or "").upper() == "POST"
            and str(scope.get("path") or "") == "/reports"
        )
        if not is_report_post:
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > MAX_REQUEST_BYTES:
            await _request_too_large_response(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            body.extend(message.get("body") or b"")
            if len(body) > MAX_REQUEST_BYTES:
                await _request_too_large_response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """시작 시 설정과 저장소를 검증하고 만료/고아 파일을 정리한다."""

    _validate_configuration()
    with _STORE_LOCK:
        _reports_dir().mkdir(parents=True, exist_ok=True)
        _cleanup_reports_unlocked()
        _enforce_existing_storage_limit_unlocked()
    yield


app = FastAPI(title="metadata_driven_v5 HTML Report API", version="1.0.0", lifespan=lifespan)
app.add_middleware(RequestBodyLimitMiddleware)


@app.get("/")
def alive() -> Response:
    """가장 단순한 서버 상태 확인 주소."""

    return Response(content="alive!", media_type="text/plain; charset=utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    """모니터링 도구가 사용할 JSON health check."""

    return {"status": "ok"}


@app.get("/api/admin/appReady")
def app_ready() -> Response:
    """일부 내부 배포 환경의 appReady 호환 주소."""

    return Response(content="alive!", media_type="text/plain; charset=utf-8")


@app.post("/reports", response_model=CreateReportResponse, status_code=201)
def create_report(request: CreateReportRequest) -> CreateReportResponse:
    """HTML과 메타데이터를 저장하고 만료되는 보기/다운로드 URL을 발급한다."""

    html = request.html
    if not html.strip():
        raise HTTPException(status_code=400, detail="html is empty")
    html_bytes = html.encode("utf-8")
    if len(html_bytes) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail=f"html is too large. max_bytes={MAX_HTML_BYTES}")

    now = datetime.now(timezone.utc)
    ttl_hours = _effective_ttl_hours(request.ttl_hours)
    expires_at = now + timedelta(hours=ttl_hours)
    report_id = _new_report_id()
    title = request.title.strip() or "HTML Report"
    token = uuid.uuid4().hex if USE_ACCESS_TOKEN else ""

    doc: dict[str, Any] = {
        "report_id": report_id,
        "title": title,
        "question": request.question,
        "view_request": request.view_request,
        "available_datasets": [item.model_dump() for item in request.available_datasets],
        "report_plan": request.report_plan,
        "html_bytes": len(html_bytes),
        "download_filename": _safe_html_filename(request.filename_hint or title or report_id),
        "created_at": _to_iso(now),
        "expires_at": _to_iso(expires_at),
        "ttl_hours": ttl_hours,
    }
    if token:
        doc["access_token_sha256"] = _hash_token(token)

    metadata_text = _json_text(doc)
    metadata_bytes = len(metadata_text.encode("utf-8"))
    if metadata_bytes > MAX_METADATA_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"report metadata is too large. max_bytes={MAX_METADATA_BYTES}",
        )

    with _STORE_LOCK:
        _reports_dir().mkdir(parents=True, exist_ok=True)
        _cleanup_reports_unlocked()
        _reserve_storage_unlocked(len(html_bytes) + metadata_bytes)
        _write_report_pair_unlocked(report_id, html_bytes, metadata_text.encode("utf-8"))

    return _response_from_doc(doc, token)


@app.get("/reports/view/{report_id}")
def view_report(report_id: str, token: str = Query(default="", max_length=128)) -> Response:
    """저장된 HTML을 브라우저 보기용으로 반환한다."""

    doc, html_bytes = _read_active_report(report_id, token)
    filename = _safe_html_filename(doc.get("download_filename") or doc.get("title") or report_id)
    headers = _common_report_headers()
    headers["Content-Security-Policy"] = VIEW_CONTENT_SECURITY_POLICY
    headers["Content-Disposition"] = _content_disposition(filename, disposition="inline")
    return Response(content=html_bytes, media_type="text/html; charset=utf-8", headers=headers)


@app.get("/reports/download/{report_id}")
def download_report(report_id: str, token: str = Query(default="", max_length=128)) -> Response:
    """저장된 HTML을 첨부 파일로 반환한다."""

    doc, html_bytes = _read_active_report(report_id, token)
    filename = _safe_html_filename(doc.get("download_filename") or doc.get("title") or report_id)
    headers = _common_report_headers()
    headers["Content-Disposition"] = _content_disposition(filename, disposition="attachment")
    return Response(content=html_bytes, media_type="text/html; charset=utf-8", headers=headers)


@app.get("/view/{report_id}", include_in_schema=False)
def view_report_short(report_id: str, token: str = Query(default="", max_length=128)) -> Response:
    """기존 짧은 보기 URL 호환 주소."""

    return view_report(report_id, token)


@app.get("/down/{report_id}", include_in_schema=False)
def download_report_short(report_id: str, token: str = Query(default="", max_length=128)) -> Response:
    """기존 짧은 다운로드 URL 호환 주소."""

    return download_report(report_id, token)


@app.delete("/reports/{report_id}")
def delete_report(report_id: str, token: str = Query(default="", max_length=128)) -> dict[str, Any]:
    """토큰/만료 검증 후 특정 리포트를 수동 삭제한다."""

    with _STORE_LOCK:
        doc, _ = _read_active_report_unlocked(report_id, token)
        _delete_report_files_unlocked(report_id)
    return {"status": "ok", "deleted": True, "report_id": str(doc["report_id"])}


def _content_length(scope: dict[str, Any]) -> int | None:
    for name, value in scope.get("headers") or []:
        if bytes(name).lower() != b"content-length":
            continue
        try:
            parsed = int(bytes(value).decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if parsed >= 0 else None
    return None


async def _request_too_large_response(scope: dict[str, Any], receive: Any, send: Any) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": f"request body is too large. max_bytes={MAX_REQUEST_BYTES}"},
    )
    await response(scope, receive, send)


def _validate_configuration() -> None:
    positive_values = {
        "SERVER_PORT": SERVER_PORT,
        "DEFAULT_TTL_HOURS": DEFAULT_TTL_HOURS,
        "MAX_TTL_HOURS": MAX_TTL_HOURS,
        "MAX_HTML_BYTES": MAX_HTML_BYTES,
        "MAX_METADATA_BYTES": MAX_METADATA_BYTES,
        "MAX_REQUEST_BYTES": MAX_REQUEST_BYTES,
        "MAX_STORAGE_BYTES": MAX_STORAGE_BYTES,
    }
    invalid = [name for name, value in positive_values.items() if int(value) <= 0]
    if invalid:
        raise RuntimeError(f"configuration must be positive: {', '.join(invalid)}")
    if DEFAULT_TTL_HOURS > MAX_TTL_HOURS:
        raise RuntimeError("DEFAULT_TTL_HOURS must not exceed MAX_TTL_HOURS")
    if MAX_REQUEST_BYTES <= MAX_HTML_BYTES:
        raise RuntimeError("MAX_REQUEST_BYTES must be greater than MAX_HTML_BYTES")
    _public_base_url()


def _public_base_url() -> str:
    candidate = str(BASE_URL or "").strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise RuntimeError("BASE_URL is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("BASE_URL must be an absolute http(s) URL without credentials, query, or fragment")
    return candidate


def _read_active_report(report_id: str, token: str) -> tuple[dict[str, Any], bytes]:
    with _STORE_LOCK:
        return _read_active_report_unlocked(report_id, token)


def _read_active_report_unlocked(report_id: str, token: str) -> tuple[dict[str, Any], bytes]:
    if not _valid_report_id(report_id):
        raise HTTPException(status_code=400, detail="invalid report_id")

    meta_path = _meta_path(report_id)
    html_path = _html_path(report_id)
    doc = _read_json(meta_path)
    if doc is None or not html_path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    if str(doc.get("report_id") or "") != report_id:
        _delete_report_files_unlocked(report_id)
        raise HTTPException(status_code=500, detail="report metadata is invalid")

    expires_at = _parse_datetime(doc.get("expires_at"))
    if expires_at is None:
        _delete_report_files_unlocked(report_id)
        raise HTTPException(status_code=500, detail="report metadata is invalid")
    if expires_at <= datetime.now(timezone.utc):
        _delete_report_files_unlocked(report_id)
        raise HTTPException(status_code=410, detail="report expired")

    expected_hash = str(doc.get("access_token_sha256") or "")
    if expected_hash:
        if not TOKEN_PATTERN.fullmatch(str(token or "")):
            raise HTTPException(status_code=403, detail="invalid access token")
        if not hmac.compare_digest(_hash_token(token), expected_hash):
            raise HTTPException(status_code=403, detail="invalid access token")

    try:
        html_bytes = html_path.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="report html not found") from exc
    if len(html_bytes) > MAX_HTML_BYTES:
        raise HTTPException(status_code=500, detail="stored report exceeds configured size limit")
    return doc, html_bytes


def _response_from_doc(doc: dict[str, Any], token: str) -> CreateReportResponse:
    report_id = str(doc["report_id"])
    suffix = f"?{urlencode({'token': token})}" if token else ""
    base_url = _public_base_url()
    return CreateReportResponse(
        report_id=report_id,
        title=str(doc.get("title") or "HTML Report"),
        view_url=f"{base_url}/reports/view/{report_id}{suffix}",
        download_url=f"{base_url}/reports/download/{report_id}{suffix}",
        expires_at=str(doc.get("expires_at") or ""),
        ttl_hours=int(doc.get("ttl_hours") or DEFAULT_TTL_HOURS),
    )


def _storage_dir() -> Path:
    return Path(STORAGE_DIR).expanduser().resolve()


def _reports_dir() -> Path:
    return (_storage_dir() / "reports").resolve()


def _safe_child_path(base: Path, filename: str) -> Path:
    """symlink를 포함해 최종 경로가 저장 폴더 밖으로 벗어나지 않는지 확인한다."""

    base_resolved = base.resolve()
    candidate = (base_resolved / filename).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid storage path") from exc
    return candidate


def _html_path(report_id: str) -> Path:
    return _safe_child_path(_reports_dir(), f"{report_id}.html")


def _meta_path(report_id: str) -> Path:
    return _safe_child_path(_reports_dir(), f"{report_id}.json")


def _lexical_report_path(report_id: str, suffix: str) -> Path:
    """검증된 ID의 디렉터리 entry를 symlink 대상 추적 없이 삭제할 때 사용한다."""

    if not _valid_report_id(report_id) or suffix not in {".html", ".json"}:
        raise ValueError("invalid report path")
    return _reports_dir() / f"{report_id}{suffix}"


def _write_report_pair_unlocked(report_id: str, html_bytes: bytes, metadata_bytes: bytes) -> None:
    html_path = _html_path(report_id)
    meta_path = _meta_path(report_id)
    if html_path.exists() or meta_path.exists():
        raise HTTPException(status_code=409, detail="report_id collision")

    nonce = uuid.uuid4().hex
    html_tmp = _safe_child_path(_reports_dir(), f".{report_id}.{nonce}.html.tmp")
    meta_tmp = _safe_child_path(_reports_dir(), f".{report_id}.{nonce}.json.tmp")
    try:
        html_tmp.write_bytes(html_bytes)
        meta_tmp.write_bytes(metadata_bytes)
        html_tmp.replace(html_path)
        meta_tmp.replace(meta_path)
    except OSError as exc:
        for path in (html_tmp, meta_tmp):
            _unlink_entry(path)
        _delete_report_files_unlocked(report_id)
        raise HTTPException(status_code=507, detail="failed to store report") from exc


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _cleanup_reports_unlocked() -> None:
    reports_dir = _reports_dir()
    reports_dir.mkdir(parents=True, exist_ok=True)

    for path in reports_dir.glob("*.tmp"):
        _unlink_entry(path)

    html_ids = {
        path.stem for path in reports_dir.glob("*.html") if _valid_report_id(path.stem)
    }
    meta_ids = {
        path.stem for path in reports_dir.glob("*.json") if _valid_report_id(path.stem)
    }
    now = datetime.now(timezone.utc)
    for report_id in html_ids | meta_ids:
        if report_id not in html_ids or report_id not in meta_ids:
            _delete_report_files_unlocked(report_id)
            continue
        doc = _read_json(_meta_path(report_id))
        expires_at = _parse_datetime(doc.get("expires_at")) if doc else None
        if not doc or str(doc.get("report_id") or "") != report_id or expires_at is None or expires_at <= now:
            _delete_report_files_unlocked(report_id)


def _reserve_storage_unlocked(required_bytes: int) -> None:
    if required_bytes > MAX_STORAGE_BYTES:
        raise HTTPException(status_code=507, detail="report is larger than the total storage limit")
    if _storage_size_bytes_unlocked() + required_bytes <= MAX_STORAGE_BYTES:
        return
    for report_id in _oldest_report_ids_unlocked():
        _delete_report_files_unlocked(report_id)
        if _storage_size_bytes_unlocked() + required_bytes <= MAX_STORAGE_BYTES:
            return
    raise HTTPException(status_code=507, detail="not enough report storage")


def _enforce_existing_storage_limit_unlocked() -> None:
    if _storage_size_bytes_unlocked() <= MAX_STORAGE_BYTES:
        return
    for report_id in _oldest_report_ids_unlocked():
        _delete_report_files_unlocked(report_id)
        if _storage_size_bytes_unlocked() <= MAX_STORAGE_BYTES:
            return


def _storage_size_bytes_unlocked() -> int:
    total = 0
    for path in _reports_dir().iterdir():
        try:
            total += path.lstat().st_size
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HTTPException(status_code=500, detail="failed to inspect report storage") from exc
    return total


def _oldest_report_ids_unlocked() -> list[str]:
    candidates: list[tuple[float, str]] = []
    for path in _reports_dir().glob("*.json"):
        report_id = path.stem
        if not _valid_report_id(report_id):
            continue
        try:
            modified_at = path.stat().st_mtime
        except (FileNotFoundError, OSError):
            continue
        candidates.append((modified_at, report_id))
    return [report_id for _, report_id in sorted(candidates)]


def _delete_report_files_unlocked(report_id: str) -> None:
    if not _valid_report_id(report_id):
        return
    for suffix in (".html", ".json"):
        _unlink_entry(_lexical_report_path(report_id, suffix))


def _unlink_entry(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        pass


def _effective_ttl_hours(value: int | None) -> int:
    ttl = DEFAULT_TTL_HOURS if value is None else int(value)
    return max(1, min(ttl, MAX_TTL_HOURS))


def _new_report_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{timestamp}_{uuid.uuid4().hex}"


def _valid_report_id(value: Any) -> bool:
    return bool(REPORT_ID_PATTERN.fullmatch(str(value or "")))


def _safe_html_filename(value: Any) -> str:
    raw = str(value or "report").strip()
    raw = re.sub(r"\.html?$", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", raw)
    raw = re.sub(r"\s+", "_", raw)
    raw = "".join(character if character.isalnum() or character in "._-" else "_" for character in raw)
    raw = raw.strip(" ._-") or "report"
    if raw.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raw = f"report_{raw}"
    return f"{raw[:100]}.html"


def _ascii_filename_fallback(filename: str) -> str:
    ascii_name = filename.encode("ascii", errors="ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name)
    ascii_name = re.sub(r"\.html?$", "", ascii_name, flags=re.IGNORECASE)
    ascii_name = ascii_name.strip(" ._-") or "report"
    if ascii_name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        ascii_name = f"report_{ascii_name}"
    return f"{ascii_name[:100]}.html"


def _content_disposition(filename: str, disposition: str) -> str:
    if disposition not in {"inline", "attachment"}:
        raise ValueError("invalid content disposition")
    safe_filename = _safe_html_filename(filename)
    fallback = _ascii_filename_fallback(safe_filename)
    encoded = quote(safe_filename, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _common_report_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def run_server() -> None:
    """python server.py로 단일 worker Uvicorn 서버를 실행한다."""

    import uvicorn

    _validate_configuration()
    print(f"HTML Report API: {_public_base_url()}")
    print(f"Listen address: http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"Storage folder: {_reports_dir()}")
    if SERVER_HOST not in {"127.0.0.1", "localhost", "::1"} and not USE_ACCESS_TOKEN:
        print("WARNING: network binding without access tokens; review README security guidance.")
    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        workers=1,
        access_log=ENABLE_ACCESS_LOG,
    )


if __name__ == "__main__":
    run_server()
