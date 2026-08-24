from __future__ import annotations

import asyncio
import json

import httpx

from app import Settings
from manual_gaia_cube_send import run_manual_send


def _settings() -> Settings:
    return Settings(
        gaia_api_url="http://gaia.test/v2/agents/agent-a/external",
        gaia_auth_key="test-key",
        cube_send_url="http://cube.test/legacy/richnotification",
        cube_bot_id="bot-id",
        cube_bot_token="bot-token",
        cube_bot_fromusername=("Bot KO", "Bot JP", "Bot EN", "Bot CN", "Bot Other"),
        gaia_timeout_seconds=10,
        cube_timeout_seconds=10,
        user_error_message="temporary failure",
    )


def _gaia_response() -> dict[str, object]:
    return {
        "session_id": "GAIA_MANUAL_SESSION",
        "outputs": [
            {
                "outputs": [
                    {
                        "component_display_name": "Chat Output",
                        "component_id": "ChatOutput-test",
                        "results": {
                            "gaia_response": {
                                "data": {"answer": "직접 입력한 질문의 답변"}
                            },
                            "message": {"data": {"error": False, "text": "fallback"}},
                        },
                    }
                ]
            }
        ],
    }


def test_manual_send_calls_gaia_then_sends_its_answer_to_cube() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "gaia.test":
            assert json.loads(request.content) == {
                "input_value": "직접 입력한 질문",
                "user_id": "gaia-authorized-user",
                "session_id": "manual-start",
            }
            return httpx.Response(200, json=_gaia_response())

        if request.url.host == "cube.test":
            payload = json.loads(request.content)["richnotification"]
            assert payload["header"]["to"] == {
                "uniquename": ["receiver-user"],
                "channelid": [""],
            }
            assert (
                payload["content"][0]["body"]["row"][0]["column"][0]["control"][
                    "text"
                ]
                == ["직접 입력한 질문의 답변"]
            )
            assert payload["content"][0]["process"]["requestid"] == [
                "request_cond_change_main"
            ]
            return httpx.Response(200, json={"ok": True})

        raise AssertionError(f"unexpected request: {request.url}")

    async def run() -> tuple[object, list[httpx.Request]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await run_manual_send(
                settings=_settings(),
                message="직접 입력한 질문",
                receiver_id="receiver-user",
                channel_id="",
                gaia_user_id="gaia-authorized-user",
                session_id="manual-start",
                client=client,
            )
        return result, requests

    result, sent_requests = asyncio.run(run())

    assert result.answer == "직접 입력한 질문의 답변"
    assert result.session_id == "GAIA_MANUAL_SESSION"
    assert len(sent_requests) == 2
