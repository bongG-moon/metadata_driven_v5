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
GaiaExternalSettings = MODULE.GaiaExternalSettings
external_payload = MODULE._external_payload


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


def test_completion_requires_complete_external_configuration(monkeypatch) -> None:
    monkeypatch.delenv("GAIA_EXTERNAL_AGENT_URL", raising=False)
    monkeypatch.delenv("GAIA_EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("GAIA_TEST_USER_ID", raising=False)

    response = TestClient(create_app()).post(
        "/api/chat/completion", json={"message": "테스트 질문", "session_id": "test-session"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "gaia_external_configuration_invalid"


def test_external_payload_matches_gaia_input_tweak_example() -> None:
    settings = GaiaExternalSettings(
        agent_url="https://gateway.example.test/v2/agents/S010101/external",
        api_key="test-key",
        user_id="2069026",
        fixed_session_id="",
        input_tweak_name="GaiA Input",
        timeout_seconds=300.0,
        verify_ssl=True,
    )

    payload = external_payload("DA공정 생산량 알려줘", "test_2069026", settings)

    assert payload == {
        "input_value": "DA공정 생산량 알려줘",
        "session_id": "test_2069026",
        "tweaks": {"GaiA Input": {"metadata": '{"user_id":"2069026"}'}},
    }


def test_completion_posts_the_proven_external_api_body_and_headers(monkeypatch) -> None:
    """The browser proxy must keep the known-working External API contract."""

    _configure(monkeypatch)
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        content = b'{"outputs": []}'

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(MODULE.httpx, "AsyncClient", FakeAsyncClient)

    response = TestClient(create_app()).post(
        "/api/chat/completion",
        json={"message": "DA공정 생산량 알려줘", "session_id": "test_2069026"},
    )

    assert response.status_code == 200
    assert captured["url"] == "https://gateway.example.test/v2/agents/S010101/external/"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "X-Gaia-Auth-Key": "test-key",
        "X-Gaia-User-Id": "2069026",
    }
    assert captured["json"] == {
        "input_value": "DA공정 생산량 알려줘",
        "session_id": "test_2069026",
        "tweaks": {"GaiA Input": {"metadata": '{"user_id":"2069026"}'}},
    }
