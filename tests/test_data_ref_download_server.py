from __future__ import annotations

from http import HTTPStatus
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen

from tools import data_ref_download_server as server


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
