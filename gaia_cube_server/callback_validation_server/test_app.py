from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app as validation_app
from app import (
    CallbackValidationError,
    CUBE_CALLBACK_PATH,
    FIXED_CALLBACK_REPLY,
    Settings,
    SettingsError,
    application,
    create_application,
    parse_cube_callback,
)


def _settings() -> Settings:
    return Settings(
        cube_send_url="http://cube.test/legacy/richnotification",
        cube_bot_id="bot-id",
        cube_bot_token="bot-token",
        cube_bot_fromusername=("봇 이름", "Bot JP", "Bot EN", "Bot CN", "Bot Other"),
        cube_timeout_seconds=10,
    )


def _callback(
    *,
    user_id: str = "employee-1",
    channel_id: str = "channel-A",
    message: str = "콜백 검증 질문",
) -> dict[str, Any]:
    return {
        "richnotificationmessage": {
            "header": {
                "from": {"uniquename": user_id},
                "to": {"channelid": [channel_id]},
            },
            "process": {
                "processdata": message,
                "userId": user_id,
                "channelId": channel_id,
            },
        }
    }


def _sent_message(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["richnotification"]


def test_receiver_sends_one_fixed_reply_without_a_gaia_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == "http://cube.test/legacy/richnotification"
        return httpx.Response(200, json={"ok": True})

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    assert response.status_code == 200
    assert response.json() is None
    assert len(requests) == 1

    sent = _sent_message(json.loads(requests[0].content))
    assert sent["header"]["to"] == {
        "uniquename": ["employee-1"],
        "channelid": ["channel-A"],
    }
    assert sent["header"]["fromusername"] == [
        "봇 이름",
        "Bot JP",
        "Bot EN",
        "Bot CN",
        "Bot Other",
    ]
    assert sent["content"][0]["body"]["row"][0]["column"][0]["control"][
        "text"
    ] == [FIXED_CALLBACK_REPLY]
    assert sent["content"][0]["process"] == {
        "callbacktype": "url",
        "callbackaddress": "",
        "processdata": "",
        "processtype": "",
        "summary": ["", "", "", "", ""],
        "session": {"sessionid": "", "sequence": "1"},
        "mandatory": [],
        "requestid": ["request_cond_change_main"],
    }


def test_process_only_selection_callback_sends_the_same_reply() -> None:
    sent_to: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_to.append(_sent_message(json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    process_only_callback = {
        "richnotificationmessage": {
            "process": {
                "userId": "employee-2",
                "channelId": "channel-B",
                "UserSelection": "선택 값",
            }
        }
    }
    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=process_only_callback)

    assert response.status_code == 200
    assert sent_to[0]["header"]["to"] == {
        "uniquename": ["employee-2"],
        "channelid": ["channel-B"],
    }


def test_from_channel_and_resultdata_callback_sends_the_same_reply() -> None:
    sent_to: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_to.append(_sent_message(json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    actual_callback_shape = {
        "richnotificationmessage": {
            "header": {
                "from": {
                    "uniquename": "employee-from",
                    "channelid": "channel-from",
                }
            },
            "process": {"processdata": ""},
            "result": {
                "resultdata": [
                    {
                        "requestid": "request_cond_change_main",
                        "value": ["엑셀 Export"],
                    }
                ]
            },
        }
    }

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=actual_callback_shape)

    assert response.status_code == 200
    assert sent_to[0]["header"]["to"] == {
        "uniquename": ["employee-from"],
        "channelid": ["channel-from"],
    }


def test_callback_parser_requires_one_unambiguous_channel_value() -> None:
    duplicate_channel = {
        "richnotificationmessage": {
            "header": {
                "from": {
                    "uniquename": "employee-from",
                    "channelid": ["channel-from", "channel-from"],
                }
            },
            "process": {"processdata": "검증 질문"},
        }
    }
    event = parse_cube_callback(duplicate_channel)
    assert event is not None
    assert event.channel_id == "channel-from"

    duplicate_channel["richnotificationmessage"]["header"]["from"]["channelid"] = [
        "channel-from",
        "other-channel",
    ]
    with pytest.raises(CallbackValidationError, match="exactly one unique value"):
        parse_cube_callback(duplicate_channel)


def test_receiver_rejects_mismatched_identity_without_sending() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    mismatched = _callback()
    mismatched["richnotificationmessage"]["process"]["userId"] = "employee-2"
    mismatched_channel = _callback()
    mismatched_channel["richnotificationmessage"]["header"]["from"]["channelid"] = (
        "channel-B"
    )
    malformed_resultdata = {
        "richnotificationmessage": {
            "header": {"from": {"uniquename": "employee-1", "channelid": "channel-A"}},
            "process": {"processdata": ""},
            "result": {"resultdata": [{"value": [{"not": "text"}]}]},
        }
    }

    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=mismatched)
        channel_response = client.post(CUBE_CALLBACK_PATH, json=mismatched_channel)
        malformed_resultdata_response = client.post(
            CUBE_CALLBACK_PATH, json=malformed_resultdata
        )

    assert response.status_code == 400
    assert channel_response.status_code == 400
    assert malformed_resultdata_response.status_code == 400
    assert requests == []


def test_hello_control_callback_is_ignored_without_sending() -> None:
    requests: list[httpx.Request] = []
    app = create_application(
        _settings(),
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200)
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            CUBE_CALLBACK_PATH,
            json={
                "richnotificationmessage": {
                    "process": {"processdata": "!@#HelloChatBot#@!"}
                }
            },
        )

    assert response.json() == {"status": "ignored"}
    assert requests == []


def test_receiver_keeps_the_ack_when_cube_cannot_accept_the_fixed_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    assert response.status_code == 200
    assert response.json() is None


def test_health_route_callback_route_and_fixed_entrypoint() -> None:
    app = create_application(_settings(), transport=httpx.MockTransport(lambda request: None))
    source = Path(validation_app.__file__).read_text(encoding="utf-8")

    with TestClient(app) as client:
        health_response = client.get("/health")
        legacy_response = client.post("/api/qna", json={})
        index_response = client.get("/", follow_redirects=False)
        hello_response = client.get("/hello")

    assert application is not None
    assert health_response.json() == {
        "status": "ok",
        "mode": "fixed_reply_validation",
        "callback_path": CUBE_CALLBACK_PATH,
    }
    assert legacy_response.status_code == 404
    assert index_response.status_code in {302, 307}
    assert index_response.headers["location"] == "/docs"
    assert hello_response.text == "hello world!"
    assert "GAIA_API_URL" not in source
    assert "BackgroundTasks" in source
    assert "return JSONResponse(content=None, status_code=200)" in source
    assert (
        'uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)'
        in source
    )


def test_settings_need_only_cube_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation_app, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("CUBE_SEND_URL", "http://cube.test/legacy/richnotification")
    monkeypatch.setenv("CUBE_BOT_ID", "bot-id")
    monkeypatch.setenv("CUBE_BOT_TOKEN", "bot-token")
    monkeypatch.setenv(
        "CUBE_BOT_FROMUSERNAME_JSON",
        '["Korean", "Japanese", "English", "Chinese", "Other"]',
    )

    settings = Settings.from_env()

    assert settings.cube_send_url == "http://cube.test/legacy/richnotification"
    assert settings.cube_timeout_seconds == 20


def test_settings_reject_placeholder_bot_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation_app, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("CUBE_SEND_URL", "http://cube.test/legacy/richnotification")
    monkeypatch.setenv("CUBE_BOT_ID", "bot-id")
    monkeypatch.setenv("CUBE_BOT_TOKEN", "bot-token")
    monkeypatch.setenv(
        "CUBE_BOT_FROMUSERNAME_JSON",
        '["PASTE_BOT_NAME_KOREAN", "JP", "EN", "CN", "Other"]',
    )

    with pytest.raises(SettingsError, match="real bot display names"):
        Settings.from_env()
