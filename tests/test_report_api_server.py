from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "report_api" / "server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("metadata_v5_report_api_test", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_report_api_creates_view_and_download_links(tmp_path, monkeypatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(server, "BASE_URL", "https://reports.example.internal")
    monkeypatch.setattr(server, "USE_ACCESS_TOKEN", False)

    with TestClient(server.app) as client:
        created = client.post(
            "/reports",
            json={
                "html": "<!doctype html><html><body><h1>차트</h1></body></html>",
                "title": "DA 생산량",
                "question": "오늘 DA 생산량을 그래프로 그려줘",
                "ttl_hours": 24,
                "filename_hint": "DA_생산량",
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["view_url"].startswith("https://reports.example.internal/reports/view/")
        assert payload["download_url"].startswith("https://reports.example.internal/reports/download/")
        assert payload["ttl_hours"] == 24

        viewed = client.get(f"/reports/view/{payload['report_id']}")
        assert viewed.status_code == 200
        assert "<h1>차트</h1>" in viewed.text
        assert "Content-Security-Policy" in viewed.headers

        downloaded = client.get(f"/reports/download/{payload['report_id']}")
        assert downloaded.status_code == 200
        assert "attachment" in downloaded.headers["Content-Disposition"]
        assert downloaded.content.startswith(b"<!doctype html>")

        meta_path = server._meta_path(payload["report_id"])
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        expired = client.get(f"/reports/view/{payload['report_id']}")
        assert expired.status_code == 410
        assert not server._html_path(payload["report_id"]).exists()
        assert not meta_path.exists()


def test_report_api_rejects_empty_html_and_invalid_report_id(tmp_path, monkeypatch) -> None:
    server = _load_server()
    monkeypatch.setattr(server, "STORAGE_DIR", tmp_path / "storage")
    monkeypatch.setattr(server, "BASE_URL", "http://127.0.0.1:8010")

    with TestClient(server.app) as client:
        empty = client.post("/reports", json={"html": "   "})
        assert empty.status_code == 400
        traversal = client.get("/reports/view/..%2Fsecret")
        assert traversal.status_code in {400, 404}

