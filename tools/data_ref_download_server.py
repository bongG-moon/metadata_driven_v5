from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import errno
import hmac
import html
import ipaddress
import io
import json
import os
import re
import secrets
import signal
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web_app.data_ref_store import DEFAULT_DATABASE, DEFAULT_RESULT_COLLECTION, load_data_ref_rows


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PREVIEW_LIMIT = 100
DEFAULT_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_RESTART_TIMEOUT_SECONDS = 5.0
DEFAULT_FORCE_TERMINATE_TIMEOUT_SECONDS = 3.0
SERVICE_NAME = "metadata-driven-data-ref-download-server"
CONTROL_SHUTDOWN_PATH = "/__control/shutdown"
DEFAULT_REPORT_STORAGE_DIR = ROOT / "report_api" / "storage"
DEFAULT_REPORT_TTL_HOURS = 24
DEFAULT_MAX_REPORT_TTL_HOURS = 24 * 7
DEFAULT_MAX_REPORT_HTML_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_REPORT_METADATA_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_REPORT_REQUEST_BYTES = (
    DEFAULT_MAX_REPORT_HTML_BYTES + DEFAULT_MAX_REPORT_METADATA_BYTES + (64 * 1024)
)
DEFAULT_MAX_REPORT_STORAGE_BYTES = 512 * 1024 * 1024
MAX_REPORT_DATASET_REFS = 100
REPORT_ID_PATTERN = re.compile(r"[0-9]{14}_[a-f0-9]{32}")
REPORT_TOKEN_PATTERN = re.compile(r"[a-f0-9]{32,128}")
REPORT_STORE_LOCK = threading.RLock()
REPORT_VIEW_CONTENT_SECURITY_POLICY = (
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
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def main() -> int:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument(
        "--env-file",
        default=os.getenv("DATA_REF_DOWNLOAD_ENV_FILE", str(ROOT / ".env")),
    )
    env_args, _ = env_parser.parse_known_args()
    load_dotenv(env_args.env_file)

    parser = argparse.ArgumentParser(description="Serve MongoDB data_ref rows as local CSV downloads.")
    parser.add_argument("--host", default=os.getenv("DATA_REF_DOWNLOAD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DATA_REF_DOWNLOAD_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--env-file", default=env_args.env_file)
    parser.add_argument("--preview-limit", type=int, default=int(os.getenv("DATA_REF_DOWNLOAD_PREVIEW_LIMIT", str(DEFAULT_PREVIEW_LIMIT))))
    parser.add_argument("--max-download-bytes", type=int, default=int(os.getenv("DATA_REF_DOWNLOAD_MAX_BYTES", str(DEFAULT_MAX_DOWNLOAD_BYTES))))
    parser.add_argument(
        "--public-base-url",
        default=os.getenv("DATA_REF_DOWNLOAD_BASE_URL") or os.getenv("REPORT_BASE_URL") or "",
        help="HTML Report 응답에 넣을 사용자 브라우저용 절대 주소. 기본값은 http://127.0.0.1:<port>입니다.",
    )
    parser.add_argument(
        "--report-storage-dir",
        default=os.getenv("REPORT_STORAGE_DIR") or str(DEFAULT_REPORT_STORAGE_DIR),
    )
    parser.add_argument(
        "--report-default-ttl-hours",
        type=int,
        default=int(os.getenv("REPORT_DEFAULT_TTL_HOURS", str(DEFAULT_REPORT_TTL_HOURS))),
    )
    parser.add_argument(
        "--report-max-ttl-hours",
        type=int,
        default=int(os.getenv("REPORT_MAX_TTL_HOURS", str(DEFAULT_MAX_REPORT_TTL_HOURS))),
    )
    parser.add_argument(
        "--max-report-html-bytes",
        type=int,
        default=int(os.getenv("REPORT_MAX_HTML_BYTES", str(DEFAULT_MAX_REPORT_HTML_BYTES))),
    )
    parser.add_argument(
        "--max-report-storage-bytes",
        type=int,
        default=int(os.getenv("REPORT_MAX_STORAGE_BYTES", str(DEFAULT_MAX_REPORT_STORAGE_BYTES))),
    )
    parser.add_argument(
        "--report-access-token",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("REPORT_USE_ACCESS_TOKEN", False),
        help="HTML Report 보기·다운로드 URL에 임의 access token을 포함합니다.",
    )
    parser.add_argument(
        "--replace-existing",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DATA_REF_DOWNLOAD_REPLACE_EXISTING", True),
        help="Stop a verified instance of this same service on the selected port before binding (default: true).",
    )
    parser.add_argument(
        "--restart-timeout-seconds",
        type=float,
        default=float(os.getenv("DATA_REF_DOWNLOAD_RESTART_TIMEOUT_SECONDS", str(DEFAULT_RESTART_TIMEOUT_SECONDS))),
    )
    parser.add_argument(
        "--force-replace-port",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("DATA_REF_DOWNLOAD_FORCE_REPLACE_PORT", True),
        help=(
            "If the selected port is still occupied after verified shutdown, terminate every listener "
            "on that port and bind again (default: true)."
        ),
    )
    parser.add_argument(
        "--force-terminate-timeout-seconds",
        type=float,
        default=float(
            os.getenv(
                "DATA_REF_DOWNLOAD_FORCE_TERMINATE_TIMEOUT_SECONDS",
                str(DEFAULT_FORCE_TERMINATE_TIMEOUT_SECONDS),
            )
        ),
    )
    parser.add_argument("--state-file", default=os.getenv("DATA_REF_DOWNLOAD_STATE_FILE", ""))
    args = parser.parse_args()

    state_path = Path(args.state_file).expanduser() if str(args.state_file).strip() else server_state_path(args.port)
    control_token = secrets.token_urlsafe(32)
    config = ServerConfig(
        mongo_uri=os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or "",
        mongo_database=os.getenv("MONGODB_DATABASE") or os.getenv("MONGO_DB_NAME") or DEFAULT_DATABASE,
        result_collection=os.getenv("MONGODB_RESULT_COLLECTION") or DEFAULT_RESULT_COLLECTION,
        preview_limit=max(0, args.preview_limit),
        max_download_bytes=max(1024, args.max_download_bytes),
        host=args.host,
        port=args.port,
        control_token=control_token,
        report_storage_dir=args.report_storage_dir,
        report_base_url=args.public_base_url or f"http://127.0.0.1:{args.port}",
        report_default_ttl_hours=args.report_default_ttl_hours,
        report_max_ttl_hours=args.report_max_ttl_hours,
        max_report_html_bytes=args.max_report_html_bytes,
        max_report_storage_bytes=args.max_report_storage_bytes,
        use_report_access_token=args.report_access_token,
    )
    prepare_report_storage(config)
    if args.replace_existing:
        request_existing_server_shutdown(
            args.host,
            args.port,
            state_path,
            timeout_seconds=max(0.5, float(args.restart_timeout_seconds)),
        )
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(config))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        if args.force_replace_port:
            try:
                terminated_pids = force_release_listener_port(
                    args.host,
                    args.port,
                    timeout_seconds=max(
                        0.5,
                        float(args.force_terminate_timeout_seconds),
                    ),
                )
            except RuntimeError as force_exc:
                print(
                    f"{args.host}:{args.port} 포트 강제 종료에 실패했습니다: {force_exc}",
                    file=sys.stderr,
                )
                return 2
            try:
                server = ThreadingHTTPServer((args.host, args.port), make_handler(config))
            except OSError as retry_exc:
                if retry_exc.errno != errno.EADDRINUSE:
                    raise
                print(
                    f"포트 강제 종료 후에도 {args.host}:{args.port} bind에 실패했습니다. "
                    "다른 프로세스가 즉시 포트를 다시 점유하는지 확인하세요.",
                    file=sys.stderr,
                )
                return 2
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass
            print(
                f"{args.host}:{args.port} listener를 강제 종료하고 다시 시작합니다. "
                f"terminated_pids={terminated_pids}"
            )
        else:
            print(
                f"data_ref download server를 시작하지 못했습니다: {args.host}:{args.port} 포트를 다른 프로세스가 사용 중입니다.",
                file=sys.stderr,
            )
            print(
                "동일한 data_ref 서버 자동 종료에 실패했습니다. "
                "--force-replace-port로 지정 포트 listener를 강제 종료할 수 있습니다.",
                file=sys.stderr,
            )
            return 2
    write_server_state(state_path, config)
    print(f"data_ref download server: http://{args.host}:{args.port}")
    print(f"same-service automatic restart: enabled (state: {state_path})")
    print("Langflow component setting:")
    component_base_url = f"http://{url_host(loopback_probe_host(args.host))}:{args.port}"
    print(f"  23 MongoDB 결과 저장소.download_base_url = {component_base_url}")
    print(f"  00 HTML 시각화 생성기.report_api_url = {component_base_url}")
    print(f"  01 실시간 생산 분석 Report 생성기.report_api_url = {component_base_url}")
    print(f"HTML Report public base URL: {config.report_base_url}")
    print(f"HTML Report storage: {report_reports_dir(config)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")
    finally:
        server.server_close()
        remove_server_state_if_owned(state_path, control_token)
    return 0


class ServerConfig:
    def __init__(
        self,
        mongo_uri: str,
        mongo_database: str,
        result_collection: str,
        preview_limit: int,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        control_token: str = "",
        report_storage_dir: str | Path = DEFAULT_REPORT_STORAGE_DIR,
        report_base_url: str = "",
        report_default_ttl_hours: int = DEFAULT_REPORT_TTL_HOURS,
        report_max_ttl_hours: int = DEFAULT_MAX_REPORT_TTL_HOURS,
        max_report_html_bytes: int = DEFAULT_MAX_REPORT_HTML_BYTES,
        max_report_storage_bytes: int = DEFAULT_MAX_REPORT_STORAGE_BYTES,
        use_report_access_token: bool = False,
    ) -> None:
        self.mongo_uri = mongo_uri
        self.mongo_database = mongo_database
        self.result_collection = result_collection
        self.preview_limit = preview_limit
        self.max_download_bytes = max(1024, int(max_download_bytes))
        self.service_name = SERVICE_NAME
        self.pid = os.getpid()
        self.host = str(host)
        self.port = int(port)
        self.control_token = str(control_token)
        self.report_storage_dir = Path(report_storage_dir).expanduser().resolve()
        self.report_base_url = normalize_report_base_url(
            report_base_url or f"http://127.0.0.1:{self.port}"
        )
        self.report_default_ttl_hours = max(1, int(report_default_ttl_hours))
        self.report_max_ttl_hours = max(1, int(report_max_ttl_hours))
        if self.report_default_ttl_hours > self.report_max_ttl_hours:
            raise ValueError("report_default_ttl_hours는 report_max_ttl_hours를 초과할 수 없습니다.")
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


def server_state_path(port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"metadata_driven_data_ref_server_{int(port)}.json"


def write_server_state(path: Path, config: ServerConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "service": config.service_name,
        "pid": config.pid,
        "host": config.host,
        "port": config.port,
        "control_token": config.control_token,
    }
    temporary = path.with_name(f".{path.name}.{config.pid}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def read_server_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def remove_server_state_if_owned(path: Path, control_token: str) -> None:
    state = read_server_state(path)
    if not state or not hmac.compare_digest(str(state.get("control_token") or ""), str(control_token or "")):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def request_existing_server_shutdown(
    host: str,
    port: int,
    state_path: Path,
    timeout_seconds: float = DEFAULT_RESTART_TIMEOUT_SECONDS,
) -> bool:
    state = read_server_state(state_path)
    expected_token = str(state.get("control_token") or "")
    expected_pid = state.get("pid")
    if (
        state.get("service") != SERVICE_NAME
        or int_or_zero(state.get("port")) != int(port)
        or not expected_token
        or not expected_pid
    ):
        return False

    probe_host = loopback_probe_host(host)
    health_url = f"http://{url_host(probe_host)}:{int(port)}/health"
    request_timeout = min(max(0.2, float(timeout_seconds)), 2.0)
    try:
        with urlopen(health_url, timeout=request_timeout) as response:
            health = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, TypeError):
        if port_is_bindable(probe_host, port):
            remove_server_state_if_owned(state_path, expected_token)
        return False
    if (
        not isinstance(health, dict)
        or health.get("service") != SERVICE_NAME
        or health.get("pid") != expected_pid
    ):
        return False

    shutdown_request = Request(
        f"http://{url_host(probe_host)}:{int(port)}{CONTROL_SHUTDOWN_PATH}",
        headers={"X-Data-Ref-Control-Token": expected_token},
        method="POST",
    )
    try:
        with urlopen(shutdown_request, timeout=request_timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, TypeError):
        return False
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False

    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        if port_is_bindable(probe_host, port):
            return True
        time.sleep(0.05)
    return port_is_bindable(probe_host, port)


def loopback_probe_host(host: str) -> str:
    text = str(host or "").strip()
    if text in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if text in {"::", "[::]"}:
        return "::1"
    return text


def url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def port_is_bindable(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# 주요 함수: psutil이 있는 환경에서 지정 TCP 포트를 LISTEN 중인 프로세스 ID를 조회합니다.
def listener_process_ids_from_psutil(port: int) -> set[int]:
    try:
        import psutil
    except ImportError:
        return set()

    pids: set[int] = set()
    try:
        connections = psutil.net_connections(kind="tcp")
    except (OSError, psutil.Error):
        return set()
    for connection in connections:
        address = getattr(connection, "laddr", None)
        listener_port = getattr(address, "port", None)
        if listener_port is None and isinstance(address, tuple) and len(address) >= 2:
            listener_port = address[1]
        if (
            int_or_zero(listener_port) == int(port)
            and str(getattr(connection, "status", "")).upper() == "LISTEN"
            and getattr(connection, "pid", None)
        ):
            pids.add(int(connection.pid))
    return pids


# 주요 함수: psutil이 없는 Linux 환경에서 /proc의 LISTEN socket inode를 프로세스 ID로 역추적합니다.
def listener_process_ids_from_linux_proc(port: int) -> set[int]:
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return set()

    target_port = f"{int(port):04X}"
    socket_inodes: set[str] = set()
    for table_path in (proc_root / "net" / "tcp", proc_root / "net" / "tcp6"):
        try:
            lines = table_path.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) <= 9 or fields[3] != "0A":
                continue
            local_address = fields[1].rsplit(":", 1)
            if len(local_address) == 2 and local_address[1].upper() == target_port:
                socket_inodes.add(fields[9])
    if not socket_inodes:
        return set()

    pids: set[int] = set()
    for process_dir in proc_root.iterdir():
        if not process_dir.name.isdigit():
            continue
        fd_dir = process_dir / "fd"
        try:
            file_descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in file_descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in socket_inodes:
                pids.add(int(process_dir.name))
                break
    return pids


# 주요 함수: 운영체제별 방법을 조합해 현재 지정 포트를 점유한 listener PID 집합을 반환합니다.
def listener_process_ids(port: int) -> set[int]:
    pids = listener_process_ids_from_psutil(port)
    if not pids:
        pids = listener_process_ids_from_linux_proc(port)
    pids.discard(os.getpid())
    return pids


# 주요 함수: Windows는 psutil, POSIX는 신호 0으로 PID 실행 상태를 확인합니다.
def process_is_running(pid: int) -> bool:
    if os.name == "nt":
        try:
            import psutil
        except ImportError:
            return True
        try:
            return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# 주요 함수: 전용 포트의 모든 listener에 TERM 후 KILL을 보내고 실제 bind 가능 상태까지 기다립니다.
def force_release_listener_port(
    host: str,
    port: int,
    *,
    timeout_seconds: float = DEFAULT_FORCE_TERMINATE_TIMEOUT_SECONDS,
) -> list[int]:
    target_pids = sorted(listener_process_ids(port))
    if not target_pids:
        raise RuntimeError(
            f"{host}:{port} 포트를 점유한 listener PID를 찾지 못했습니다. "
            "동일 사용자 권한으로 실행하거나 psutil을 설치하세요."
        )

    permission_errors: list[int] = []
    for pid in target_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            permission_errors.append(pid)
    if permission_errors:
        raise RuntimeError(
            f"{host}:{port} listener 종료 권한이 없습니다. pids={permission_errors}"
        )

    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        if port_is_bindable(host, port):
            return target_pids
        time.sleep(0.05)

    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in target_pids:
        if not process_is_running(pid):
            continue
        try:
            os.kill(pid, kill_signal)
        except ProcessLookupError:
            continue
        except PermissionError:
            permission_errors.append(pid)
    if permission_errors:
        raise RuntimeError(
            f"{host}:{port} listener 강제 종료 권한이 없습니다. pids={sorted(set(permission_errors))}"
        )

    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        if port_is_bindable(host, port):
            return target_pids
        time.sleep(0.05)
    raise RuntimeError(
        f"{host}:{port} listener를 강제 종료했지만 제한 시간 안에 포트가 해제되지 않았습니다. "
        f"pids={target_pids}"
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def make_handler(config: ServerConfig) -> type[BaseHTTPRequestHandler]:
    class DataRefDownloadHandler(BaseHTTPRequestHandler):
        server_version = "DataRefDownloadServer/3.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/health", "/healthz"}:
                self.send_json(
                    {
                        "ok": True,
                        "service": config.service_name,
                        "pid": config.pid,
                        "host": config.host,
                        "port": config.port,
                        "features": {
                            "data_ref_csv": True,
                            "html_reports": True,
                        },
                        "report_base_url": config.report_base_url,
                    }
                )
                return
            report_view_prefix = "/reports/view/"
            report_download_prefix = "/reports/download/"
            if parsed.path.startswith(report_view_prefix):
                self.render_report(
                    parsed.path[len(report_view_prefix) :],
                    parsed.query,
                    download=False,
                )
                return
            if parsed.path.startswith(report_download_prefix):
                self.render_report(
                    parsed.path[len(report_download_prefix) :],
                    parsed.query,
                    download=True,
                )
                return
            # 답변의 링크와 예전 `/?download_ref=...` 링크는 중간 화면 없이 바로 CSV를 내려줍니다.
            if parsed.path in {"/", "/download.csv", "/download"}:
                self.render_csv(parsed.query)
                return
            # 운영 진단이 필요할 때만 /view를 직접 입력해 제한된 미리보기를 확인합니다.
            if parsed.path == "/view":
                self.render_view(parsed.query)
                return
            if parsed.path == "/download.json":
                self.render_json(parsed.query)
                return
            self.send_error_page(HTTPStatus.NOT_FOUND, "지원하지 않는 경로입니다.")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/reports":
                self.create_report()
                return
            if parsed.path != CONTROL_SHUTDOWN_PATH:
                self.send_error_page(HTTPStatus.NOT_FOUND, "지원하지 않는 경로입니다.")
                return
            try:
                is_loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                is_loopback = False
            supplied_token = str(self.headers.get("X-Data-Ref-Control-Token") or "")
            if (
                not is_loopback
                or not config.control_token
                or not hmac.compare_digest(supplied_token, config.control_token)
            ):
                self.send_json(
                    {"ok": False, "message": "shutdown control 인증에 실패했습니다."},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            self.send_json({"ok": True, "message": "server shutdown requested", "pid": config.pid})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def do_DELETE(self) -> None:
            parsed = urlparse(self.path)
            prefix = "/reports/"
            if not parsed.path.startswith(prefix):
                self.send_error_page(HTTPStatus.NOT_FOUND, "지원하지 않는 경로입니다.")
                return
            report_id = parsed.path[len(prefix) :]
            try:
                deleted = delete_html_report(report_id, report_token_from_query(parsed.query), config)
            except ReportHttpError as exc:
                self.send_json({"detail": exc.message}, status=exc.status)
                return
            self.send_json({"status": "ok", "deleted": True, "report_id": deleted})

        def create_report(self) -> None:
            try:
                payload = self.read_json_body(config.max_report_request_bytes)
                result = create_html_report(payload, config)
            except ReportHttpError as exc:
                self.send_json({"detail": exc.message}, status=exc.status)
                return
            self.send_json(result, status=HTTPStatus.CREATED)

        def read_json_body(self, max_bytes: int) -> dict[str, Any]:
            raw_length = str(self.headers.get("Content-Length") or "").strip()
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise ReportHttpError(HTTPStatus.LENGTH_REQUIRED, "Content-Length가 필요합니다.") from exc
            if content_length < 0:
                raise ReportHttpError(HTTPStatus.BAD_REQUEST, "Content-Length가 올바르지 않습니다.")
            if content_length > max_bytes:
                raise ReportHttpError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"request body is too large. max_bytes={max_bytes}",
                )
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                raise ReportHttpError(HTTPStatus.BAD_REQUEST, "요청 body를 완전히 읽지 못했습니다.")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReportHttpError(HTTPStatus.BAD_REQUEST, "요청 body는 UTF-8 JSON object여야 합니다.") from exc
            if not isinstance(payload, dict):
                raise ReportHttpError(HTTPStatus.BAD_REQUEST, "요청 body는 JSON object여야 합니다.")
            return payload

        def render_report(self, report_id: str, query: str, *, download: bool) -> None:
            try:
                doc, payload = load_active_html_report(
                    report_id,
                    report_token_from_query(query),
                    config,
                )
            except ReportHttpError as exc:
                self.send_json({"detail": exc.message}, status=exc.status)
                return
            filename = safe_report_filename(
                doc.get("download_filename") or doc.get("title") or report_id
            )
            self.send_report_bytes(payload, filename, download=download)

        def render_view(self, query: str) -> None:
            resolved = resolve_request(query, config, limit=config.preview_limit)
            if not resolved["ok"]:
                self.send_html(error_page("다운로드 링크 오류", resolved["message"]), status=resolved_status(resolved))
                return
            ref = resolved["ref"]
            loaded = resolved["loaded"]
            rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
            columns = loaded.get("columns") if isinstance(loaded.get("columns"), list) else rows_columns(rows)
            csv_url = "/download.csv?" + urlencode({"download_ref": encode_data_ref(ref)})
            json_url = "/download.json?" + urlencode({"download_ref": encode_data_ref(ref)})
            body = render_data_page(ref, loaded, rows, columns, csv_url, json_url, config.preview_limit)
            self.send_html(body)

        def render_csv(self, query: str) -> None:
            resolved = resolve_request(query, config, limit=None)
            if not resolved["ok"]:
                self.send_plain(resolved["message"], status=resolved_status(resolved))
                return
            ref = resolved["ref"]
            loaded = resolved["loaded"]
            rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
            columns = loaded.get("columns") if isinstance(loaded.get("columns"), list) else rows_columns(rows)
            payload = rows_to_csv_bytes(rows, columns)
            if len(payload) > config.max_download_bytes:
                self.send_plain(
                    f"CSV 파일이 다운로드 상한을 초과했습니다. max_bytes={config.max_download_bytes}",
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            filename = download_filename(ref, "csv")
            self.send_bytes(payload, "text/csv; charset=utf-8", filename=filename)

        def render_json(self, query: str) -> None:
            resolved = resolve_request(query, config, limit=None)
            if not resolved["ok"]:
                self.send_json({"ok": False, "message": resolved["message"]}, status=resolved_status(resolved))
                return
            payload = {"data_ref": resolved["ref"], "loaded": resolved["loaded"]}
            self.send_json(payload, filename=download_filename(resolved["ref"], "json"))

        def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_common_headers()
            data = body.encode("utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_plain(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = str(text).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_common_headers()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK, filename: str = "") -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_common_headers()
            if filename:
                self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def send_bytes(self, payload: bytes, content_type: str, filename: str = "") -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_common_headers()
            if filename:
                self.send_header("Content-Disposition", content_disposition(filename))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_report_bytes(self, payload: bytes, filename: str, *, download: bool) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_common_headers()
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if not download:
                self.send_header("Content-Security-Policy", REPORT_VIEW_CONTENT_SECURITY_POLICY)
            disposition = "attachment" if download else "inline"
            self.send_header(
                "Content-Disposition",
                report_content_disposition(filename, disposition),
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_error_page(self, status: HTTPStatus, message: str) -> None:
            self.send_html(error_page(status.phrase, message), status=status)

        def send_common_headers(self) -> None:
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {mask_download_server_log(fmt % args)}")

    return DataRefDownloadHandler


class ReportHttpError(Exception):
    """HTML Report HTTP 처리 중 예상 가능한 상태 코드와 사용자 메시지를 함께 전달합니다."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = str(message)


def normalize_report_base_url(value: Any) -> str:
    """Report 응답에 사용할 절대 http(s) 기준 주소를 검증하고 끝의 slash를 제거합니다."""

    candidate = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise ValueError("report_base_url이 올바른 URL이 아닙니다.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "report_base_url은 계정정보·query·fragment가 없는 절대 http(s) URL이어야 합니다."
        )
    return candidate


def mask_download_server_log(value: Any) -> str:
    """access log의 HTML access token과 data_ref token 원문을 마스킹합니다."""

    return re.sub(
        r"([?&](?:token|download_ref)=)[^&\s\"]+",
        r"\1***",
        str(value or ""),
        flags=re.IGNORECASE,
    )


def prepare_report_storage(config: ServerConfig) -> None:
    """통합 서버 시작 전에 Report 저장 폴더와 기존 만료 파일을 정리합니다."""

    with REPORT_STORE_LOCK:
        report_reports_dir(config).mkdir(parents=True, exist_ok=True)
        cleanup_html_reports_unlocked(config)
        enforce_report_storage_limit_unlocked(config, required_bytes=0)


def create_html_report(payload: dict[str, Any], config: ServerConfig) -> dict[str, Any]:
    """POST /reports JSON을 검증해 HTML·metadata 쌍을 저장하고 공개 URL을 발급합니다."""

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
            f"available_datasets는 최대 {MAX_REPORT_DATASET_REFS}개 list여야 합니다.",
        )
    if any(not isinstance(item, dict) for item in available_datasets):
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "available_datasets 항목은 object여야 합니다.")
    report_plan = payload.get("report_plan", {})
    if not isinstance(report_plan, dict):
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "report_plan은 object여야 합니다.")

    ttl_hours = report_ttl_hours(payload.get("ttl_hours"), config)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)
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
        "created_at": report_iso(now),
        "expires_at": report_iso(expires_at),
        "ttl_hours": ttl_hours,
    }
    if access_token:
        metadata["access_token_sha256"] = hash_report_token(access_token)

    try:
        metadata_bytes = (
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "Report metadata를 JSON으로 변환할 수 없습니다.") from exc
    if len(metadata_bytes) > config.max_report_metadata_bytes:
        raise ReportHttpError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"report metadata is too large. max_bytes={config.max_report_metadata_bytes}",
        )

    with REPORT_STORE_LOCK:
        report_reports_dir(config).mkdir(parents=True, exist_ok=True)
        cleanup_html_reports_unlocked(config)
        enforce_report_storage_limit_unlocked(
            config,
            required_bytes=len(html_bytes) + len(metadata_bytes),
        )
        write_html_report_pair_unlocked(
            report_id,
            html_bytes,
            metadata_bytes,
            config,
        )

    suffix = f"?{urlencode({'token': access_token})}" if access_token else ""
    return {
        "report_id": report_id,
        "title": title,
        "view_url": f"{config.report_base_url}/reports/view/{report_id}{suffix}",
        "download_url": f"{config.report_base_url}/reports/download/{report_id}{suffix}",
        "expires_at": metadata["expires_at"],
        "ttl_hours": ttl_hours,
    }


def report_text_field(
    payload: dict[str, Any],
    key: str,
    default: str,
    max_length: int,
) -> str:
    """Report 문자열 입력을 타입·길이 계약에 맞게 검증합니다."""

    value = payload.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, f"{key}는 문자열이어야 합니다.")
    if len(value) > max_length:
        raise ReportHttpError(
            HTTPStatus.BAD_REQUEST,
            f"{key} 길이는 {max_length}자를 초과할 수 없습니다.",
        )
    return value.strip() or default


def report_ttl_hours(value: Any, config: ServerConfig) -> int:
    """요청 TTL을 1시간 이상 운영 상한 이하로 제한합니다."""

    if value in (None, ""):
        return config.report_default_ttl_hours
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "ttl_hours는 정수여야 합니다.") from exc
    if parsed < 1:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "ttl_hours는 1 이상이어야 합니다.")
    return min(parsed, config.report_max_ttl_hours)


def report_reports_dir(config: ServerConfig) -> Path:
    """HTML·metadata 파일이 저장되는 reports 하위 폴더를 반환합니다."""

    return (config.report_storage_dir / "reports").resolve()


def report_path(config: ServerConfig, report_id: str, suffix: str) -> Path:
    """검증된 report_id만 저장 폴더 아래의 HTML 또는 JSON 경로로 변환합니다."""

    if not REPORT_ID_PATTERN.fullmatch(str(report_id or "")) or suffix not in {".html", ".json"}:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "invalid report_id")
    base = report_reports_dir(config)
    candidate = (base / f"{report_id}{suffix}").resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ReportHttpError(HTTPStatus.BAD_REQUEST, "invalid report storage path") from exc
    return candidate


def write_html_report_pair_unlocked(
    report_id: str,
    html_bytes: bytes,
    metadata_bytes: bytes,
    config: ServerConfig,
) -> None:
    """HTML과 metadata를 임시 파일에 쓴 뒤 같은 저장 폴더 안에서 원자적으로 교체합니다."""

    html_path = report_path(config, report_id, ".html")
    metadata_path = report_path(config, report_id, ".json")
    if html_path.exists() or metadata_path.exists():
        raise ReportHttpError(HTTPStatus.CONFLICT, "report_id collision")
    nonce = secrets.token_hex(8)
    html_tmp = html_path.with_name(f".{report_id}.{nonce}.html.tmp")
    metadata_tmp = metadata_path.with_name(f".{report_id}.{nonce}.json.tmp")
    try:
        html_tmp.write_bytes(html_bytes)
        metadata_tmp.write_bytes(metadata_bytes)
        os.replace(html_tmp, html_path)
        os.replace(metadata_tmp, metadata_path)
    except OSError as exc:
        for path in (html_tmp, metadata_tmp, html_path, metadata_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise ReportHttpError(HTTPStatus.INSUFFICIENT_STORAGE, "failed to store report") from exc


def load_active_html_report(
    report_id: str,
    token: str,
    config: ServerConfig,
) -> tuple[dict[str, Any], bytes]:
    """보기·다운로드 요청의 ID, 만료, token, 파일 크기를 검증해 Report를 읽습니다."""

    with REPORT_STORE_LOCK:
        return load_active_html_report_unlocked(report_id, token, config)


def load_active_html_report_unlocked(
    report_id: str,
    token: str,
    config: ServerConfig,
) -> tuple[dict[str, Any], bytes]:
    """REPORT_STORE_LOCK 안에서 활성 Report 쌍을 읽습니다."""

    html_path = report_path(config, report_id, ".html")
    metadata_path = report_path(config, report_id, ".json")
    metadata = read_report_metadata(metadata_path)
    if metadata is None or not html_path.is_file():
        raise ReportHttpError(HTTPStatus.NOT_FOUND, "report not found")
    if str(metadata.get("report_id") or "") != report_id:
        delete_html_report_files_unlocked(report_id, config)
        raise ReportHttpError(HTTPStatus.INTERNAL_SERVER_ERROR, "report metadata is invalid")
    expires_at = parse_report_datetime(metadata.get("expires_at"))
    if expires_at is None:
        delete_html_report_files_unlocked(report_id, config)
        raise ReportHttpError(HTTPStatus.INTERNAL_SERVER_ERROR, "report metadata is invalid")
    if expires_at <= datetime.now(timezone.utc):
        delete_html_report_files_unlocked(report_id, config)
        raise ReportHttpError(HTTPStatus.GONE, "report expired")
    expected_hash = str(metadata.get("access_token_sha256") or "")
    if expected_hash:
        if not REPORT_TOKEN_PATTERN.fullmatch(str(token or "")):
            raise ReportHttpError(HTTPStatus.FORBIDDEN, "invalid access token")
        if not hmac.compare_digest(hash_report_token(token), expected_hash):
            raise ReportHttpError(HTTPStatus.FORBIDDEN, "invalid access token")
    try:
        html_bytes = html_path.read_bytes()
    except FileNotFoundError as exc:
        raise ReportHttpError(HTTPStatus.NOT_FOUND, "report html not found") from exc
    if len(html_bytes) > config.max_report_html_bytes:
        raise ReportHttpError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "stored report exceeds configured size limit",
        )
    return metadata, html_bytes


def delete_html_report(report_id: str, token: str, config: ServerConfig) -> str:
    """활성·인증 검증 후 특정 Report 파일 쌍을 삭제합니다."""

    with REPORT_STORE_LOCK:
        metadata, _ = load_active_html_report_unlocked(report_id, token, config)
        delete_html_report_files_unlocked(report_id, config)
    return str(metadata["report_id"])


def cleanup_html_reports_unlocked(config: ServerConfig) -> None:
    """만료 Report, HTML/JSON 한쪽만 남은 고아 파일, 임시 파일을 정리합니다."""

    reports_dir = report_reports_dir(config)
    reports_dir.mkdir(parents=True, exist_ok=True)
    for path in reports_dir.glob("*.tmp"):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    html_ids = {
        path.stem
        for path in reports_dir.glob("*.html")
        if REPORT_ID_PATTERN.fullmatch(path.stem)
    }
    metadata_ids = {
        path.stem
        for path in reports_dir.glob("*.json")
        if REPORT_ID_PATTERN.fullmatch(path.stem)
    }
    now = datetime.now(timezone.utc)
    for report_id in html_ids | metadata_ids:
        if report_id not in html_ids or report_id not in metadata_ids:
            delete_html_report_files_unlocked(report_id, config)
            continue
        metadata = read_report_metadata(report_path(config, report_id, ".json"))
        expires_at = parse_report_datetime(metadata.get("expires_at")) if metadata else None
        if (
            not metadata
            or str(metadata.get("report_id") or "") != report_id
            or expires_at is None
            or expires_at <= now
        ):
            delete_html_report_files_unlocked(report_id, config)


def enforce_report_storage_limit_unlocked(
    config: ServerConfig,
    required_bytes: int,
) -> None:
    """저장 상한을 넘기기 전에 오래된 Report부터 파일 쌍 단위로 제거합니다."""

    if required_bytes > config.max_report_storage_bytes:
        raise ReportHttpError(
            HTTPStatus.INSUFFICIENT_STORAGE,
            "report is larger than the total storage limit",
        )
    reports_dir = report_reports_dir(config)
    current_size = sum(
        path.stat().st_size
        for path in reports_dir.iterdir()
        if path.is_file() and not path.is_symlink()
    )
    if current_size + required_bytes <= config.max_report_storage_bytes:
        return
    report_ids = sorted(
        {
            path.stem
            for path in reports_dir.glob("*.json")
            if REPORT_ID_PATTERN.fullmatch(path.stem)
        },
        key=lambda report_id: report_path(config, report_id, ".json").stat().st_mtime,
    )
    for report_id in report_ids:
        delete_html_report_files_unlocked(report_id, config)
        current_size = sum(
            path.stat().st_size
            for path in reports_dir.iterdir()
            if path.is_file() and not path.is_symlink()
        )
        if current_size + required_bytes <= config.max_report_storage_bytes:
            return
    raise ReportHttpError(HTTPStatus.INSUFFICIENT_STORAGE, "report storage limit exceeded")


def delete_html_report_files_unlocked(report_id: str, config: ServerConfig) -> None:
    """검증된 report_id의 HTML·JSON entry만 삭제합니다."""

    for suffix in (".html", ".json"):
        try:
            report_path(config, report_id, suffix).unlink()
        except FileNotFoundError:
            pass


def read_report_metadata(path: Path) -> dict[str, Any] | None:
    """UTF-8 JSON metadata 파일을 object로만 읽습니다."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def report_token_from_query(query: str) -> str:
    """Report URL query에서 첫 token 문자열을 추출합니다."""

    return first_param(parse_qs(query, keep_blank_values=False), "token")


def safe_report_filename(value: Any) -> str:
    """브라우저 Content-Disposition에 넣을 안전한 HTML 파일명을 만듭니다."""

    text = str(value or "report").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip(" ._-") or "report"
    text = re.sub(r"\.html?$", "", text, flags=re.IGNORECASE)
    if text.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        text = f"report_{text}"
    return f"{text[:100]}.html"


def report_content_disposition(filename: str, disposition: str) -> str:
    """한글 파일명을 지원하는 inline/attachment Content-Disposition을 생성합니다."""

    if disposition not in {"inline", "attachment"}:
        raise ValueError("invalid report disposition")
    safe_filename = safe_report_filename(filename)
    fallback = safe_filename.encode("ascii", errors="ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", fallback).strip("._") or "report.html"
    return (
        f"{disposition}; filename=\"{fallback}\"; "
        f"filename*=UTF-8''{quote(safe_filename, safe='')}"
    )


def hash_report_token(token: str) -> str:
    """Report access token 원문 대신 비교용 SHA-256을 생성합니다."""

    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def report_iso(value: datetime) -> str:
    """timezone-aware datetime을 UTC ISO 문자열로 변환합니다."""

    return value.astimezone(timezone.utc).isoformat()


def parse_report_datetime(value: Any) -> datetime | None:
    """metadata의 ISO 일시를 UTC datetime으로 안전하게 해석합니다."""

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


def resolve_request(query: str, config: ServerConfig, limit: int | None) -> dict[str, Any]:
    try:
        ref = data_ref_from_query(query)
    except Exception as exc:
        return {"ok": False, "message": f"download_ref 토큰 해석 실패: {exc}", "ref": {}, "loaded": {}}
    if not isinstance(ref, dict):
        return {"ok": False, "message": "download_ref 또는 ref_id가 필요합니다.", "ref": {}, "loaded": {}}
    ref, validation_error = normalize_download_ref(ref, config)
    if validation_error:
        return {"ok": False, "message": validation_error, "ref": ref, "loaded": {}}
    ref_id = str(ref.get("ref_id") or "").strip()
    if not ref_id:
        return {"ok": False, "message": "data_ref.ref_id가 비어 있습니다.", "ref": ref, "loaded": {}}
    if not config.mongo_uri:
        return {"ok": False, "message": "MONGODB_URI 또는 MONGO_URI 환경값이 필요합니다.", "ref": ref, "loaded": {}}
    try:
        loaded = load_data_ref_rows(
            ref,
            config.mongo_uri,
            default_database=config.mongo_database,
            default_collection=config.result_collection,
            limit=limit,
        )
    except Exception as exc:
        return {"ok": False, "message": f"MongoDB data_ref 조회 실패: {exc}", "ref": ref, "loaded": {}}
    if not loaded.get("ok"):
        status = HTTPStatus.GONE if loaded.get("expired") else HTTPStatus.BAD_REQUEST
        return {"ok": False, "message": str(loaded.get("message") or "data_ref rows를 찾지 못했습니다."), "ref": ref, "loaded": loaded, "status": status}
    return {"ok": True, "message": "", "ref": ref, "loaded": loaded}


def normalize_download_ref(ref: dict[str, Any], config: ServerConfig) -> tuple[dict[str, Any], str]:
    """23번이 발급한 result-store 경로만 허용하고 서버 설정과 다른 DB/컬렉션 접근을 차단합니다."""

    normalized = dict(ref)
    ref_id = str(normalized.get("ref_id") or "").strip()
    if not re.fullmatch(r"result:.+:[0-9a-fA-F]{32}", ref_id):
        return normalized, "허용되지 않은 data_ref.ref_id 형식입니다."

    database = str(normalized.get("database") or config.mongo_database).strip()
    collection_name = str(normalized.get("collection_name") or config.result_collection).strip()
    if database != config.mongo_database or collection_name != config.result_collection:
        return normalized, "다운로드 서버 설정과 다른 MongoDB 데이터베이스 또는 컬렉션은 조회할 수 없습니다."

    path = str(normalized.get("path") or "").strip()
    if not re.fullmatch(r"payload\.(?:result_rows|runtime_sources\.[A-Za-z0-9_-]+)", path):
        return normalized, "허용되지 않은 data_ref.path입니다."

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
        "collection_name": first_param(params, "collection_name") or first_param(params, "collection"),
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
        raise ValueError("download_ref token is not an object.")
    return parsed


def encode_data_ref(ref: dict[str, Any]) -> str:
    payload = json.dumps(ref, ensure_ascii=False, default=str).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


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
    table = render_table(rows[:preview_limit], columns)
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
    summary_html = "\n".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in summary if value not in (None, "", []))
    return page_shell(
        title,
        f"""
        <section class="toolbar">
          <a class="button primary" href="{escape(csv_url)}">CSV 다운로드</a>
          <a class="button" href="{escape(json_url)}">data_ref JSON 다운로드</a>
        </section>
        <dl class="summary">{summary_html}</dl>
        <p class="note">아래 표는 최대 {preview_limit:,}행 미리보기입니다. CSV 다운로드는 전체 rows를 내려받습니다.</p>
        {table}
        """,
    )


def render_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return '<p class="empty">표시할 rows가 없습니다.</p>'
    head = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(row.get(column, ''))}</td>" for column in columns)
        body_rows.append(f"<tr>{cells}</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'


def error_page(title: str, message: str) -> str:
    return page_shell(title, f'<p class="error">{escape(message)}</p><p class="note">링크의 download_ref 또는 서버 .env 설정을 확인하세요.</p>')


def page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Malgun Gothic", sans-serif; color: #17202a; background: #f6f7f9; }}
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
<body>
  <main>
    <h1>{escape(title)}</h1>
    {body}
  </main>
</body>
</html>"""


def rows_to_csv_bytes(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


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


def ref_label(ref: dict[str, Any]) -> str:
    label = str(ref.get("label") or "").strip()
    if label:
        return label
    role = str(ref.get("role") or "").strip()
    alias = str(ref.get("source_alias") or ref.get("dataset_key") or "").strip()
    if role == "analysis_result":
        return "분석 결과 데이터"
    if role == "source_rows" and alias:
        return f"사용 원본 데이터: {alias}"
    return "MongoDB 저장 데이터"


def download_filename(ref: dict[str, Any], suffix: str) -> str:
    seed = str(ref.get("label") or ref.get("source_alias") or ref.get("role") or ref.get("ref_id") or "data_ref")
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", seed)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = "".join(character if character.isalnum() or character in "._-" else "_" for character in cleaned)
    cleaned = cleaned.strip(" ._-") or "data_ref"
    return f"{cleaned}.{suffix}"


def content_disposition(filename: str) -> str:
    safe_filename = download_filename({"label": re.sub(r"\.[A-Za-z0-9]+$", "", str(filename or "data_ref"))}, str(filename).rsplit(".", 1)[-1] or "csv")
    fallback = safe_filename.encode("ascii", errors="ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", fallback).strip("._") or "data_ref.csv"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(safe_filename, safe='')}"


def int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def load_dotenv(env_file: str | Path) -> None:
    path = Path(env_file)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
