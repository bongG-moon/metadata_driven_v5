from __future__ import annotations

from fastapi.testclient import TestClient

from artifact_server.app import ReportCreateRequest, _csv_size_within_limit, _iter_csv_bytes, create_app
from tools.data_ref_download_server import ServerConfig


def test_fastapi_artifact_server_report_lifecycle_and_health(tmp_path) -> None:
    config = ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        host="127.0.0.1",
        port=8765,
        report_storage_dir=tmp_path,
        report_base_url="https://hbmexample.com",
        use_report_access_token=True,
    )
    with TestClient(create_app(config)) as client:
        live = client.get("/live")
        assert live.status_code == 200
        assert live.json()["service"] == "metadata-driven-v5-artifact-server"
        health = client.get("/health").json()
        assert health["report_base_url"] == "https://hbmexample.com"

        created = client.post(
            "/reports",
            json={"html": "<html><body>정상</body></html>", "title": "생산 Report"},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["view_url"].startswith("https://hbmexample.com/reports/view/")
        assert "?token=" in body["view_url"]

        view = client.get(body["view_url"].removeprefix("https://hbmexample.com"))
        assert view.status_code == 200
        assert "정상" in view.text
        assert "Content-Security-Policy" in view.headers

        download = client.get(body["download_url"].removeprefix("https://hbmexample.com"))
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment;")

        report_id = body["report_id"]
        token = body["view_url"].split("?token=", 1)[1]
        deleted = client.delete(f"/reports/{report_id}?token={token}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True


def test_fastapi_artifact_server_readiness_distinguishes_missing_mongo(tmp_path) -> None:
    config = ServerConfig(
        mongo_uri="",
        mongo_database="datagov",
        result_collection="agent_v4_result_store",
        preview_limit=10,
        report_storage_dir=tmp_path,
        report_base_url="http://127.0.0.1:8765",
    )
    with TestClient(create_app(config)) as client:
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"] == {"mongo": False, "storage": True}


def test_csv_stream_is_bom_prefixed_and_enforces_size_without_shared_defaults() -> None:
    rows = [{"name": "A", "count": 2}, {"name": "B", "count": 3}]
    chunks = list(_iter_csv_bytes(rows, ["name", "count"]))
    payload = b"".join(chunks)
    assert payload.startswith(b"\xef\xbb\xbfname,count\r\n")
    assert _csv_size_within_limit(rows, ["name", "count"], len(payload)) == len(payload)
    assert _csv_size_within_limit(rows, ["name", "count"], len(payload) - 1) is None

    first = ReportCreateRequest(html="<p>first</p>")
    second = ReportCreateRequest(html="<p>second</p>")
    first.available_datasets.append({"dataset": "one"})
    assert second.available_datasets == []
