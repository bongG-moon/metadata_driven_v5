from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app import Settings, create_app


def _settings() -> Settings:
    return Settings(
        gaia_base_url="http://gaia.test",
        gaia_auth_key="test-key",
        default_gaia_service_id="service-default",
        channel_service_map={"channel-A": "service-A"},
        cube_send_url="http://cube.test/legacy/richnotification",
        cube_bot_id="bot-id",
        cube_bot_token="bot-token",
        gaia_timeout_seconds=10,
        cube_timeout_seconds=10,
        callback_success_status="success",
        callback_ignored_status="ignored",
        user_error_message="temporary failure",
    )


def _gaia_response(
    answer: str,
    *,
    session_id: str | None = None,
    message_text: str = "fallback message text",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "outputs": [
            {
                "inputs": {"input_value": "생산량을 알려줘"},
                "outputs": [
                    {
                        "component_display_name": "Chat Output",
                        "component_id": "ChatOutput-test",
                        "results": {
                            "gaia_response": {"data": {"answer": answer}},
                            "message": {
                                "data": {"error": False, "text": message_text}
                            },
                        },
                    }
                ]
            }
        ]
    }
    if session_id is not None:
        response["session_id"] = session_id
    return response


def test_callback_calls_gaia_then_sends_cube_answer() -> None:
    sent_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        if request.url.host == "gaia.test":
            assert request.headers["X-Gaia-Auth-Key"] == "test-key"
            assert request.url.path == "/v2/agents/service-A/external"
            payload = json.loads(request.content)
            assert payload == {
                "input_value": "생산량을 알려줘",
                "user_id": "employee-1",
                "session_id": payload["session_id"],
            }
            assert payload["session_id"].startswith("gc_")
            return httpx.Response(
                200,
                json=_gaia_response(
                    "GAIA 정규화 답변",
                    session_id="gaia-returned-session",
                    message_text="이 fallback 텍스트를 CUBE로 보내면 안 됩니다.",
                ),
            )
        if request.url.host == "cube.test":
            payload = json.loads(request.content)
            assert payload["richnotification"]["header"]["to"] == {
                "uniquename": ["employee-1"],
                "channelid": ["channel-A"],
            }
            assert (
                payload["richnotification"]["content"][0]["body"]["row"][0]["column"][0]
                ["control"]["text"]
                == ["GAIA 정규화 답변"]
            )
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    payload = {
        "richnotificationmessage": {
            "header": {
                "from": {"uniquename": "employee-1"},
                "to": {"channelid": ["channel-A"]},
            },
            "process": {
                "processdata": "생산량을 알려줘",
                "userId": "employee-1",
                "channelId": "channel-A",
            },
        }
    }

    with TestClient(app) as client:
        response = client.post("/api/qna", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert len(sent_requests) == 2


def test_hello_callback_does_not_call_external_apis() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"external request must not be made: {request.url}")

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    hello_payload = {
        "richnotificationmessage": {
            "process": {"processdata": "!@#HelloChatBot#@!"}
        }
    }

    with TestClient(app) as client:
        response = client.post("/api/qna", json=hello_payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


def test_same_user_and_channel_reuses_the_gaia_returned_session() -> None:
    gaia_session_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            gaia_session_ids.append(json.loads(request.content)["session_id"])
            return httpx.Response(
                200,
                json=_gaia_response("answer", session_id="GAIA_SESSION_FROM_RESPONSE"),
            )
        if request.url.host == "cube.test":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_app(_settings(), transport=httpx.MockTransport(handler))
    payload = {
        "richnotificationmessage": {
            "process": {
                "processdata": "질문",
                "userId": "employee-1",
                "channelId": "channel-A",
            }
        }
    }

    with TestClient(app) as client:
        assert client.post("/api/qna", json=payload).status_code == 200
        assert client.post("/api/qna", json=payload).status_code == 200

    assert len(gaia_session_ids) == 2
    assert gaia_session_ids[0].startswith("gc_")
    assert gaia_session_ids[1] == "GAIA_SESSION_FROM_RESPONSE"


def test_mismatched_callback_identity_is_rejected() -> None:
    app = create_app(_settings(), transport=httpx.MockTransport(lambda request: None))
    payload = {
        "richnotificationmessage": {
            "header": {
                "from": {"uniquename": "employee-1"},
                "to": {"channelid": ["channel-A"]},
            },
            "process": {
                "processdata": "질문",
                "userId": "employee-2",
                "channelId": "channel-A",
            },
        }
    }

    with TestClient(app) as client:
        response = client.post("/api/qna", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "CUBE user ID values do not match."
