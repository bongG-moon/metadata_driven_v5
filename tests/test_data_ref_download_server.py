from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

from tools import data_ref_download_server as server
from web_app.data_ref_store import rows_from_data_ref_document


def test_data_ref_download_token_round_trip() -> None:
    ref = {
        "store": "mongodb",
        "ref_id": "result:s1:abc",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.result_rows",
        "role": "analysis_result",
        "label": "분석 결과 데이터",
    }

    token = server.encode_data_ref(ref)
    decoded = server.decode_data_ref(token)

    assert decoded == ref
    assert "=" not in token


def test_data_ref_download_server_masks_query_tokens_in_access_log() -> None:
    masked = server.mask_download_server_log(
        'GET /reports/view/id?token=secret-token&download_ref=secret-ref HTTP/1.1'
    )

    assert "secret-token" not in masked
    assert "secret-ref" not in masked
    assert "token=***" in masked
    assert "download_ref=***" in masked


def test_data_ref_download_query_supports_direct_ref_params() -> None:
    ref = server.data_ref_from_query(
        "ref_id=result%3As1%3Aabc&path=payload.runtime_sources.production_data&source_alias=production_data"
    )

    assert ref == {
        "store": "mongodb",
        "ref_id": "result:s1:abc",
        "path": "payload.runtime_sources.production_data",
        "source_alias": "production_data",
    }


def test_data_ref_download_resolve_reports_bad_token_without_mongo_call() -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://unused",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=100,
    )

    resolved = server.resolve_request("download_ref=not-valid-base64", config, limit=10)

    assert resolved["ok"] is False
    assert "토큰 해석 실패" in resolved["message"]


def test_data_ref_download_resolve_reports_expired_ref_as_gone(monkeypatch) -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=100,
    )

    def fake_load_data_ref_rows(*args, **kwargs):
        return {"ok": False, "expired": True, "message": "data_ref expired.", "rows": []}

    monkeypatch.setattr(server, "load_data_ref_rows", fake_load_data_ref_rows)
    token = server.encode_data_ref(
        {
            "store": "mongodb",
            "ref_id": "result:s1:0123456789abcdef0123456789abcdef",
            "database": "datagov",
            "collection_name": "agent_v4_result_store",
            "path": "payload.result_rows",
        }
    )

    resolved = server.resolve_request(f"download_ref={token}", config, limit=10)

    assert resolved["ok"] is False
    assert resolved["status"] == HTTPStatus.GONE


def test_data_ref_download_rejects_other_collection_and_unapproved_path() -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://unused",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=100,
    )
    base = {
        "store": "mongodb",
        "ref_id": "result:s1:0123456789abcdef0123456789abcdef",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.result_rows",
    }

    other_collection = server.resolve_request(
        "download_ref=" + server.encode_data_ref({**base, "collection_name": "secret_collection"}),
        config,
        limit=None,
    )
    unsafe_path = server.resolve_request(
        "download_ref=" + server.encode_data_ref({**base, "path": "payload.request"}),
        config,
        limit=None,
    )

    assert other_collection["ok"] is False
    assert "다른 MongoDB" in other_collection["message"]
    assert unsafe_path["ok"] is False
    assert "path" in unsafe_path["message"]


def test_data_ref_download_allows_selected_intermediate_checkpoint_path() -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://unused",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=100,
    )
    normalized, error = server.normalize_download_ref(
        {
            "store": "mongodb",
            "ref_id": "result:s1:0123456789abcdef0123456789abcdef",
            "database": "datagov",
            "collection_name": "agent_v4_result_store",
            "path": "payload.intermediate_rows.last_successful",
            "role": "intermediate_result",
        },
        config,
    )

    assert error == ""
    assert normalized["path"] == "payload.intermediate_rows.last_successful"


def test_data_ref_download_reads_selected_intermediate_checkpoint_rows() -> None:
    document = {
        "payload": {
            "intermediate_rows": {
                "last_successful": {
                    "rows": [
                        {"OPER_NAME": "INPUT", "MCP_NO": "L-267A1", "PRODUCTION": 300},
                        {"OPER_NAME": "INPUT", "MCP_NO": "L-267A2", "PRODUCTION": 180},
                    ],
                    "columns": ["OPER_NAME", "MCP_NO", "PRODUCTION"],
                    "row_count": 2,
                }
            }
        }
    }

    loaded = rows_from_data_ref_document(
        document,
        limit=1,
        path="payload.intermediate_rows.last_successful",
    )

    assert loaded["ok"] is True
    assert loaded["row_count"] == 2
    assert loaded["columns"] == ["OPER_NAME", "MCP_NO", "PRODUCTION"]
    assert loaded["rows"] == [{"OPER_NAME": "INPUT", "MCP_NO": "L-267A1", "PRODUCTION": 300}]


def test_data_ref_download_csv_uses_utf8_bom_and_headers() -> None:
    payload = server.rows_to_csv_bytes(
        [{"DEVICE": "DEV-A", "생산량": 123}],
        ["DEVICE", "생산량"],
    )

    assert payload.startswith("\ufeff".encode("utf-8"))
    text = payload.decode("utf-8-sig")
    assert "DEVICE,생산량" in text
    assert "DEV-A,123" in text


def test_data_ref_download_http_link_returns_attachment_without_preview(monkeypatch) -> None:
    config = server.ServerConfig(
        mongo_uri="mongodb://fake",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
    )

    def fake_load_data_ref_rows(*args, **kwargs):
        return {
            "ok": True,
            "rows": [{"DEVICE": "DEV-A", "생산량": 123}],
            "columns": ["DEVICE", "생산량"],
            "row_count": 1,
            "expires_at": "2099-01-01T00:00:00+00:00",
        }

    monkeypatch.setattr(server, "load_data_ref_rows", fake_load_data_ref_rows)
    ref = {
        "store": "mongodb",
        "ref_id": "result:s1:0123456789abcdef0123456789abcdef",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.result_rows",
        "role": "analysis_result",
        "label": "분석 결과 데이터",
    }
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config))
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/download.csv?download_ref={server.encode_data_ref(ref)}"
        with urlopen(url, timeout=5) as response:
            body = response.read()
            disposition = response.headers.get("Content-Disposition", "")
            content_type = response.headers.get("Content-Type", "")
        assert body.startswith("\ufeff".encode("utf-8"))
        assert disposition.startswith("attachment;")
        assert "filename*=UTF-8''" in disposition
        assert content_type.startswith("text/csv")
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_data_ref_download_server_creates_views_and_downloads_html_reports(tmp_path) -> None:
    config = server.ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        host="127.0.0.1",
        port=0,
        report_storage_dir=tmp_path / "report-storage",
        report_base_url="http://127.0.0.1:1",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config))
    config.port = httpd.server_port
    config.report_base_url = f"http://127.0.0.1:{httpd.server_port}"
    server.prepare_report_storage(config)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"{config.report_base_url}/reports",
            data=json.dumps(
                {
                    "html": "<!doctype html><html><body><h1>실시간 생산 분석</h1></body></html>",
                    "title": "실시간 생산 분석 Report",
                    "question": "오늘 생산 분석 Report를 만들어줘",
                    "ttl_hours": 4,
                    "filename_hint": "실시간_생산_분석",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            created = json.loads(response.read().decode("utf-8"))
            assert response.status == HTTPStatus.CREATED

        assert created["view_url"].startswith(f"{config.report_base_url}/reports/view/")
        assert created["download_url"].startswith(f"{config.report_base_url}/reports/download/")
        assert created["ttl_hours"] == 4

        with urlopen(created["view_url"], timeout=5) as response:
            viewed = response.read().decode("utf-8")
            assert response.status == HTTPStatus.OK
            assert response.headers["Content-Disposition"].startswith("inline;")
            assert "Content-Security-Policy" in response.headers
        assert "<h1>실시간 생산 분석</h1>" in viewed

        with urlopen(created["download_url"], timeout=5) as response:
            downloaded = response.read()
            assert response.headers["Content-Disposition"].startswith("attachment;")
            assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
        assert downloaded.startswith(b"<!doctype html>")

        metadata_path = server.report_path(config, created["report_id"], ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat()
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        try:
            urlopen(created["view_url"], timeout=5)
            raise AssertionError("만료된 HTML Report는 410이어야 합니다.")
        except HTTPError as exc:
            assert exc.code == HTTPStatus.GONE
        assert not server.report_path(config, created["report_id"], ".html").exists()
        assert not metadata_path.exists()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_data_ref_download_server_report_access_token_and_validation(tmp_path) -> None:
    config = server.ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        report_storage_dir=tmp_path / "report-storage",
        report_base_url="https://reports.example.internal",
        use_report_access_token=True,
        max_report_html_bytes=1_024,
    )
    server.prepare_report_storage(config)

    created = server.create_html_report(
        {
            "html": "<!doctype html><html><body>token report</body></html>",
            "ttl_hours": 999,
        },
        config,
    )

    assert "?token=" in created["view_url"]
    assert created["ttl_hours"] == config.report_max_ttl_hours
    report_id = created["report_id"]
    token = created["view_url"].split("?token=", 1)[1]
    metadata, payload = server.load_active_html_report(report_id, token, config)
    assert metadata["report_id"] == report_id
    assert payload.startswith(b"<!doctype html>")
    try:
        server.load_active_html_report(report_id, "", config)
        raise AssertionError("token이 없는 요청은 거부되어야 합니다.")
    except server.ReportHttpError as exc:
        assert exc.status == HTTPStatus.FORBIDDEN

    try:
        server.create_html_report({"html": "x" * 1_025}, config)
        raise AssertionError("HTML byte 상한 초과 요청은 거부되어야 합니다.")
    except server.ReportHttpError as exc:
        assert exc.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_data_ref_download_server_verified_instance_can_be_restarted(monkeypatch) -> None:
    config = server.ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        host="127.0.0.1",
        port=0,
        control_token="verified-control-token",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config))
    config.port = httpd.server_port
    state_path = Path("unused-state.json")
    monkeypatch.setattr(
        server,
        "read_server_state",
        lambda path: {
            "service": server.SERVICE_NAME,
            "pid": config.pid,
            "host": config.host,
            "port": config.port,
            "control_token": config.control_token,
        },
    )

    def serve_until_shutdown() -> None:
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()

    thread = Thread(target=serve_until_shutdown, daemon=True)
    thread.start()
    with urlopen(f"http://127.0.0.1:{httpd.server_port}/health", timeout=5) as response:
        health = json.loads(response.read().decode("utf-8"))

    assert health["service"] == server.SERVICE_NAME
    assert health["pid"] == config.pid
    assert server.request_existing_server_shutdown(
        "127.0.0.1",
        httpd.server_port,
        state_path,
        timeout_seconds=3,
    )
    thread.join(timeout=5)
    assert not thread.is_alive()

    rebound = ThreadingHTTPServer(("127.0.0.1", config.port), server.make_handler(config))
    rebound.server_close()


def test_data_ref_download_server_does_not_stop_instance_with_wrong_control_token(monkeypatch) -> None:
    config = server.ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        host="127.0.0.1",
        port=0,
        control_token="actual-token",
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(config))
    config.port = httpd.server_port
    state_path = Path("unused-state.json")
    monkeypatch.setattr(
        server,
        "read_server_state",
        lambda path: {
            "service": server.SERVICE_NAME,
            "pid": config.pid,
            "host": config.host,
            "port": config.port,
            "control_token": "wrong-token",
        },
    )
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert not server.request_existing_server_shutdown(
            "127.0.0.1",
            httpd.server_port,
            state_path,
            timeout_seconds=1,
        )
        assert thread.is_alive()
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_force_release_listener_port_uses_term_then_kill(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    force_killed = set()
    clock = [0.0]
    kill_signal = 9

    monkeypatch.setattr(server, "listener_process_ids", lambda port: {101, 102})
    monkeypatch.setattr(server.signal, "SIGKILL", kill_signal, raising=False)
    monkeypatch.setattr(
        server,
        "port_is_bindable",
        lambda host, port: bool(force_killed),
    )
    monkeypatch.setattr(server, "process_is_running", lambda pid: True)

    def fake_kill(pid: int, selected_signal: int) -> None:
        signals.append((pid, selected_signal))
        if selected_signal == kill_signal:
            force_killed.add(pid)

    def fake_monotonic() -> float:
        clock[0] += 0.3
        return clock[0]

    monkeypatch.setattr(server.os, "kill", fake_kill)
    monkeypatch.setattr(server.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)

    terminated = server.force_release_listener_port(
        "0.0.0.0",
        8765,
        timeout_seconds=0.5,
    )

    assert terminated == [101, 102]
    assert signals[:2] == [(101, signal.SIGTERM), (102, signal.SIGTERM)]
    assert signals[2:] == [
        (101, kill_signal),
        (102, kill_signal),
    ]


def test_force_release_listener_port_terminates_real_child_listener() -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,time;"
                "listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM);"
                "listener.bind(('127.0.0.1',0));"
                "listener.listen();"
                "print(listener.getsockname()[1],flush=True);"
                "time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout is not None
        port = int(child.stdout.readline().strip())
        assert not server.port_is_bindable("127.0.0.1", port)
        assert server.listener_process_ids(port)

        terminated = server.force_release_listener_port(
            "127.0.0.1",
            port,
            timeout_seconds=2,
        )

        assert terminated
        child.wait(timeout=5)
        rebound = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(
            server.ServerConfig(
                mongo_uri="",
                mongo_database="datagov",
                result_collection="agent_v4_result_store",
                preview_limit=10,
            )
        ))
        rebound.server_close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
