from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app as callback_app
from app import (
    CallbackValidationError,
    CUBE_CALLBACK_PATH,
    Settings,
    SettingsError,
    application,
    create_application,
    parse_cube_callback,
)


def _settings() -> Settings:
    return Settings(
        # The complete path is configured once; the server must use it unchanged.
        gaia_api_url="http://gaia.test/v2/agents/agent-a/external",
        gaia_auth_key="test-key",
        cube_send_url="http://cube.test/legacy/richnotification",
        cube_bot_id="bot-id",
        cube_bot_token="bot-token",
        cube_bot_fromusername=("봇 이름", "Bot JP", "Bot EN", "Bot CN", "Bot Other"),
        gaia_timeout_seconds=10,
        cube_timeout_seconds=10,
        user_error_message="temporary failure",
    )


def _callback(
    *,
    user_id: str = "employee-1",
    channel_id: str = "channel-A",
    message: str = "생산량을 알려줘",
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


def _gaia_response(answer: str, session_id: str = "gaia-returned-session") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "outputs": [
            {
                "outputs": [
                    {
                        "component_display_name": "Chat Output",
                        "component_id": "ChatOutput-test",
                        "results": {
                            "gaia_response": {"data": {"answer": answer}},
                            "message": {"data": {"error": False, "text": "fallback"}},
                        },
                    }
                ]
            }
        ],
    }


def _all_columns(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [column for row in body["row"] for column in row["column"]]


def _label_texts(body: dict[str, Any]) -> list[str]:
    return [
        column["control"]["text"][0]
        for column in _all_columns(body)
        if column["type"] == "label"
    ]


def test_receiver_runs_full_gaia_to_cube_flow() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "gaia.test":
            assert str(request.url) == "http://gaia.test/v2/agents/agent-a/external"
            assert request.headers["X-Gaia-Auth-Key"] == "test-key"
            payload = json.loads(request.content)
            assert set(payload) == {"input_value", "user_id", "session_id", "tweaks"}
            assert {
                key: value for key, value in payload.items() if key != "tweaks"
            } == {
                "input_value": "생산량을 알려줘",
                "user_id": "employee-1",
                "session_id": payload["session_id"],
            }
            chat_input = payload["tweaks"][callback_app.GAIA_INPUT_TWEAK_NAME]
            assert json.loads(chat_input["data"]) == {
                "conversation_history": [
                    {
                        "role": "user",
                        "content": "생산량을 알려줘",
                        "files": [],
                    }
                ]
            }
            assert json.loads(chat_input["metadata"]) == {
                "platform": "CUBE",
                "user_id": "employee-1",
                "session_id": payload["session_id"],
                "cube_user_id": "employee-1",
                "cube_channel_id": "channel-A",
            }
            return httpx.Response(200, json=_gaia_response("GAIA 정규화 답변"))

        if request.url.host == "cube.test":
            payload = json.loads(request.content)["richnotification"]
            assert payload["header"]["to"] == {
                "uniquename": ["employee-1"],
                "channelid": ["channel-A"],
            }
            assert payload["header"]["fromusername"] == [
                "봇 이름",
                "Bot JP",
                "Bot EN",
                "Bot CN",
                "Bot Other",
            ]
            column = payload["content"][0]["body"]["row"][0]["column"][0]
            assert column["control"]["text"] == ["GAIA 정규화 답변"]
            assert column["control"]["active"] == "true"
            assert payload["content"][0]["process"] == {
                "callbacktype": "url",
                "callbackaddress": "",
                "processdata": "",
                "processtype": "",
                "summary": ["", "", "", "", ""],
                "session": {"sessionid": "", "sequence": "1"},
                "mandatory": [],
                "requestid": ["request_cond_change_main"],
            }
            assert payload["result"] == ""
            return httpx.Response(200, json={"ok": True})

        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    assert response.status_code == 200
    # The CUBE callback is acknowledged before the GAIA/CUBE work. TestClient
    # waits for FastAPI background tasks, so the external mock calls are also
    # complete by the time this assertion runs.
    assert response.json() is None
    assert len(requests) == 2


def test_receiver_accepts_actual_from_channel_and_resultdata_shapes() -> None:
    gaia_requests: list[dict[str, Any]] = []
    cube_targets: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            gaia_requests.append(json.loads(request.content))
            return httpx.Response(200, json=_gaia_response("실제 callback 답변"))
        if request.url.host == "cube.test":
            cube_targets.append(
                json.loads(request.content)["richnotification"]["header"]["to"]
            )
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    # This is the header shape from the supplied running CUBE callback code.
    from_channel_callback = {
        "richnotificationmessage": {
            "header": {
                "from": {
                    "uniquename": "employee-from",
                    "channelid": "channel-from",
                }
            },
            "process": {"processdata": "header.from 채널 질문"},
        }
    }
    resultdata_callback = {
        "richnotificationmessage": {
            "header": {
                "from": {
                    "uniquename": "employee-from",
                    "channelid": ["channel-from"],
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
        from_channel_response = client.post(
            CUBE_CALLBACK_PATH, json=from_channel_callback
        )
        resultdata_response = client.post(CUBE_CALLBACK_PATH, json=resultdata_callback)

    assert from_channel_response.status_code == 200
    assert resultdata_response.status_code == 200
    assert from_channel_response.json() is None
    assert resultdata_response.json() is None
    assert [request["input_value"] for request in gaia_requests] == [
        "header.from 채널 질문",
        "엑셀 Export",
    ]
    assert all(request["user_id"] == "employee-from" for request in gaia_requests)
    first_chat_input = gaia_requests[0]["tweaks"][callback_app.GAIA_INPUT_TWEAK_NAME]
    second_chat_input = gaia_requests[1]["tweaks"][callback_app.GAIA_INPUT_TWEAK_NAME]
    first_data = json.loads(first_chat_input["data"])
    second_data = json.loads(second_chat_input["data"])
    assert first_data == {
        "conversation_history": [
            {
                "role": "user",
                "content": "header.from 채널 질문",
                "files": [],
            }
        ]
    }
    assert second_data == {
        "conversation_history": [
            {
                "role": "user",
                "content": "header.from 채널 질문",
                "files": [],
            },
            {
                "role": "assistant",
                "content": "실제 callback 답변",
                "files": [],
            },
            {
                "role": "user",
                "content": "엑셀 Export",
                "files": [],
            },
        ]
    }
    assert json.loads(second_chat_input["metadata"]) == {
        "platform": "CUBE",
        "user_id": "employee-from",
        "session_id": gaia_requests[1]["session_id"],
        "cube_user_id": "employee-from",
        "cube_channel_id": "channel-from",
    }
    assert cube_targets == [
        {"uniquename": ["employee-from"], "channelid": ["channel-from"]},
        {"uniquename": ["employee-from"], "channelid": ["channel-from"]},
    ]


def test_callback_parser_preserves_resultdata_values_and_safe_channel_ids() -> None:
    resultdata_callback = {
        "richnotificationmessage": {
            "header": {
                "from": {
                    "uniquename": "employee-from",
                    "channelid": ["channel-from", "channel-from"],
                }
            },
            "process": {"processdata": ""},
            "result": {
                "resultdata": [
                    {"requestid": "first", "value": ["선택 A", "선택 B"]},
                    {"requestid": "duplicate", "value": ["선택 A"]},
                ]
            },
        }
    }
    event = parse_cube_callback(resultdata_callback)

    assert event is not None
    assert event.channel_id == "channel-from"
    assert event.message == "선택 A\n선택 B"

    # Free text remains the preferred input even if a prior Rich Message
    # result payload happens to be included in the same callback.
    resultdata_callback["richnotificationmessage"]["process"]["processdata"] = (
        "일반 질문이 우선"
    )
    assert parse_cube_callback(resultdata_callback).message == "일반 질문이 우선"

    ambiguous_channel = _callback()
    ambiguous_channel["richnotificationmessage"]["header"]["to"]["channelid"] = [
        "channel-A",
        "channel-B",
    ]
    with pytest.raises(CallbackValidationError, match="exactly one unique value"):
        parse_cube_callback(ambiguous_channel)

    invalid_resultdata = {
        "richnotificationmessage": {
            "header": {"from": {"uniquename": "employee-from", "channelid": "channel"}},
            "process": {"processdata": ""},
            "result": {"resultdata": [{"value": [{"not": "a text selection"}]}]},
        }
    }
    with pytest.raises(CallbackValidationError, match="processdata or selection value"):
        parse_cube_callback(invalid_resultdata)


def test_manual_post_to_receiver_uses_same_full_flow() -> None:
    sent_messages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            assert json.loads(request.content)["input_value"] == "수동 테스트 질문"
            return httpx.Response(200, json=_gaia_response("수동 테스트 답변"))
        if request.url.host == "cube.test":
            payload = json.loads(request.content)
            sent_messages.append(
                payload["richnotification"]["content"][0]["body"]["row"][0][
                    "column"
                ][0]["control"]["text"][0]
            )
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            CUBE_CALLBACK_PATH,
            json=_callback(message="수동 테스트 질문"),
        )

    assert response.status_code == 200
    assert response.json() is None
    assert sent_messages == ["수동 테스트 답변"]


def test_same_user_and_channel_reuses_gaia_returned_session() -> None:
    gaia_session_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            gaia_session_ids.append(json.loads(request.content)["session_id"])
            return httpx.Response(200, json=_gaia_response("answer", "GAIA_SESSION"))
        if request.url.host == "cube.test":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        assert client.post(CUBE_CALLBACK_PATH, json=_callback()).status_code == 200
        assert client.post(CUBE_CALLBACK_PATH, json=_callback(message="두 번째 질문")).status_code == 200

    assert gaia_session_ids[0].startswith("gc_")
    assert gaia_session_ids[1] == gaia_session_ids[0]


def test_same_user_and_channel_uses_the_same_session_after_app_restart() -> None:
    gaia_session_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            gaia_session_ids.append(json.loads(request.content)["session_id"])
            return httpx.Response(200, json=_gaia_response("answer"))
        if request.url.host == "cube.test":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    first_app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(first_app) as client:
        assert client.post(CUBE_CALLBACK_PATH, json=_callback()).status_code == 200

    restarted_app = create_application(
        _settings(), transport=httpx.MockTransport(handler)
    )
    with TestClient(restarted_app) as client:
        assert client.post(CUBE_CALLBACK_PATH, json=_callback()).status_code == 200

    assert gaia_session_ids[0] == gaia_session_ids[1]


def test_callback_history_keeps_only_the_last_three_completed_pairs() -> None:
    gaia_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            payload = json.loads(request.content)
            gaia_payloads.append(payload)
            return httpx.Response(
                200,
                json=_gaia_response(
                    f"답변 {payload['input_value']}", "GAIA_HISTORY_SESSION"
                ),
            )
        if request.url.host == "cube.test":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        for number in range(1, 6):
            response = client.post(
                CUBE_CALLBACK_PATH,
                json=_callback(message=f"질문 {number}"),
            )
            assert response.status_code == 200

    latest_history = json.loads(
        gaia_payloads[-1]["tweaks"][callback_app.GAIA_INPUT_TWEAK_NAME]["data"]
    )["conversation_history"]
    assert [(turn["role"], turn["content"]) for turn in latest_history] == [
        ("user", "질문 2"),
        ("assistant", "답변 질문 2"),
        ("user", "질문 3"),
        ("assistant", "답변 질문 3"),
        ("user", "질문 4"),
        ("assistant", "답변 질문 4"),
        ("user", "질문 5"),
    ]


def test_gaia_failure_sends_the_safe_fallback_once() -> None:
    cube_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            return httpx.Response(500, json={"error": "failed"})
        if request.url.host == "cube.test":
            payload = json.loads(request.content)
            cube_bodies.append(
                payload["richnotification"]["content"][0]["body"]
            )
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    assert response.status_code == 200
    assert response.json() is None
    assert len(cube_bodies) == 1
    rendered = "\n".join(_label_texts(cube_bodies[0]))
    assert "오류: GAIA API가 요청을 정상 처리하지 못했습니다." in rendered
    assert "temporary failure" in rendered
    assert cube_bodies[0]["row"][0]["column"][0]["control"]["color"] == ""


def test_gaia_forbidden_sends_the_agent_permission_link() -> None:
    cube_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            return httpx.Response(
                403,
                json={
                    "errorCode": 403,
                    "errorName": "Auth-server-Forbidden",
                    "errorMessage": "권한이 없는 사용자입니다.",
                },
            )
        if request.url.host == "cube.test":
            cube_bodies.append(
                json.loads(request.content)["richnotification"]["content"][0]["body"]
            )
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    rendered = json.dumps(cube_bodies[0], ensure_ascii=False)
    assert response.status_code == 200
    assert response.json() is None
    assert "권한이 없는 사용자입니다." in rendered
    assert "PTMORE PKG Agent 권한 신청" in rendered
    assert "http://aimarket.skhynix.com/apps/gaia-market-web/market/agent" in rendered
    assert "temporary failure" not in rendered
    assert any(
        column["type"] == "hypertext"
        and column["control"]["linkurl"]
        == "http://aimarket.skhynix.com/apps/gaia-market-web/market/agent"
        for column in _all_columns(cube_bodies[0])
    )


def test_gaia_timeout_sends_a_safe_timeout_reason_without_raw_error_details() -> None:
    cube_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            raise httpx.ReadTimeout("private GAIA host details")
        if request.url.host == "cube.test":
            payload = json.loads(request.content)
            cube_bodies.append(payload["richnotification"]["content"][0]["body"])
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    rendered = "\n".join(_label_texts(cube_bodies[0]))
    assert response.status_code == 200
    assert response.json() is None
    assert "주의: GAIA 응답 시간이 초과되었습니다." in rendered
    assert "private GAIA host details" not in rendered
    assert cube_bodies[0]["row"][0]["column"][0]["control"]["color"] == ""


def test_gaia_response_without_final_answer_describes_that_cause() -> None:
    cube_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            return httpx.Response(200, json={"outputs": []})
        if request.url.host == "cube.test":
            payload = json.loads(request.content)
            cube_bodies.append(payload["richnotification"]["content"][0]["body"])
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    assert response.status_code == 200
    assert response.json() is None
    rendered = "\n".join(_label_texts(cube_bodies[0]))
    assert "오류: GAIA/Langflow 응답에서 최종 답변을 찾지 못했습니다." in rendered
    assert "temporary failure" in rendered


def test_gaia_failure_keeps_the_ack_when_cube_fallback_delivery_fails() -> None:
    cube_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cube_attempts
        if request.url.host == "gaia.test":
            return httpx.Response(500, json={"error": "failed"})
        if request.url.host == "cube.test":
            cube_attempts += 1
            return httpx.Response(500, json={"error": "fallback failed"})
        raise AssertionError(f"unexpected request: {request.url}")

    app = create_application(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    # The callback was already acknowledged before this background delivery
    # failure, so CUBE does not receive a delayed non-2xx response.
    assert response.status_code == 200
    assert response.json() is None
    assert cube_attempts == 1


def test_receiver_rejects_mismatched_identity_and_ignores_hello() -> None:
    app = create_application(_settings(), transport=httpx.MockTransport(lambda request: None))
    mismatched = _callback()
    mismatched["richnotificationmessage"]["process"]["userId"] = "employee-2"
    mismatched_channel = _callback()
    mismatched_channel["richnotificationmessage"]["header"]["from"]["channelid"] = (
        "channel-B"
    )

    with TestClient(app) as client:
        mismatch_response = client.post(CUBE_CALLBACK_PATH, json=mismatched)
        channel_mismatch_response = client.post(
            CUBE_CALLBACK_PATH, json=mismatched_channel
        )
        hello_response = client.post(
            CUBE_CALLBACK_PATH,
            json={"richnotificationmessage": {"process": {"processdata": "!@#HelloChatBot#@!"}}},
        )

    assert mismatch_response.status_code == 400
    assert channel_mismatch_response.status_code == 400
    assert hello_response.json() == {"status": "ignored"}


def test_only_the_registered_callback_route_exists() -> None:
    app = create_application(_settings(), transport=httpx.MockTransport(lambda request: None))

    with TestClient(app) as client:
        receiver_response = client.post(CUBE_CALLBACK_PATH, json={})
        legacy_response = client.post("/api/qna", json={})
        health_response = client.get("/health")

    assert receiver_response.status_code == 400
    assert legacy_response.status_code == 404
    assert health_response.json() == {
        "status": "ok",
        "callback_path": CUBE_CALLBACK_PATH,
    }


def test_required_skeleton_routes_and_fixed_entrypoint() -> None:
    app = create_application(_settings(), transport=httpx.MockTransport(lambda request: None))
    source = Path(callback_app.__file__).read_text(encoding="utf-8")

    with TestClient(app) as client:
        index_response = client.get("/", follow_redirects=False)
        hello_response = client.get("/hello")

    assert application is not None
    assert index_response.status_code in {302, 307}
    assert index_response.headers["location"] == "/docs"
    assert hello_response.text == "hello world!"
    assert "/api/qna" not in source
    assert "BackgroundTasks" in source
    assert "return JSONResponse(content=None, status_code=200)" in source
    assert (
        'uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)'
        in source
    )


def test_settings_use_a_complete_gaia_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(callback_app, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("GAIA_API_URL", "http://gaia.test/v2/agents/agent-a/external")
    monkeypatch.setenv("GAIA_AUTH_KEY", "test-key")
    monkeypatch.setenv("CUBE_SEND_URL", "http://cube.test/legacy/richnotification")
    monkeypatch.setenv("CUBE_BOT_ID", "bot-id")
    monkeypatch.setenv("CUBE_BOT_TOKEN", "bot-token")
    monkeypatch.setenv(
        "CUBE_BOT_FROMUSERNAME_JSON",
        '["Bot KO", "Bot JP", "Bot EN", "Bot CN", "Bot Other"]',
    )

    settings = Settings.from_env()

    assert settings.gaia_api_url == "http://gaia.test/v2/agents/agent-a/external"


def test_settings_reject_gaia_url_without_an_api_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(callback_app, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("GAIA_API_URL", "http://gaia.test")
    monkeypatch.setenv("GAIA_AUTH_KEY", "test-key")
    monkeypatch.setenv("CUBE_SEND_URL", "http://cube.test/legacy/richnotification")
    monkeypatch.setenv("CUBE_BOT_ID", "bot-id")
    monkeypatch.setenv("CUBE_BOT_TOKEN", "bot-token")
    monkeypatch.setenv(
        "CUBE_BOT_FROMUSERNAME_JSON",
        '["Bot KO", "Bot JP", "Bot EN", "Bot CN", "Bot Other"]',
    )

    with pytest.raises(SettingsError, match="GAIA_API_URL must be a complete"):
        Settings.from_env()
