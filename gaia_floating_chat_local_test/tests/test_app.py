from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
SPEC = importlib.util.spec_from_file_location("gaia_floating_chat_test_app", APP_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
create_app = MODULE.create_app


def _configure(monkeypatch) -> None:
    monkeypatch.setenv(
        "GAIA_EXTERNAL_AGENT_URL",
        "https://gateway.example.test/v2/agents/S010101/external/",
    )
    monkeypatch.setenv("GAIA_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("GAIA_TEST_USER_ID", "2069026")
    monkeypatch.delenv("GAIA_TEST_SESSION_ID", raising=False)


def test_health_reports_missing_configuration(monkeypatch) -> None:
    monkeypatch.delenv("GAIA_EXTERNAL_AGENT_URL", raising=False)
    monkeypatch.delenv("GAIA_EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("GAIA_TEST_USER_ID", raising=False)

    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json()["configured"] is False


def test_browser_config_never_returns_api_key(monkeypatch) -> None:
    _configure(monkeypatch)

    response = TestClient(create_app()).get("/api/config")

    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["user_id"] == "2069026"
    assert response.json()["api_key_configured"] is True
    assert "test-key" not in response.text


def test_stream_requires_complete_external_configuration(monkeypatch) -> None:
    monkeypatch.delenv("GAIA_EXTERNAL_AGENT_URL", raising=False)
    monkeypatch.delenv("GAIA_EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("GAIA_TEST_USER_ID", raising=False)

    response = TestClient(create_app()).post(
        "/api/chat/stream", json={"message": "테스트 질문", "session_id": "test-session"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "gaia_external_configuration_invalid"
