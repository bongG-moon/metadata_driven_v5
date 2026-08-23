from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Support both `pytest` from this folder and a full-project pytest run.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gaia_cube_server.dummy_callback_server.app import GaiaResponseError, app, extract_final_answer


client = TestClient(app)


def valid_callback(
    *,
    user_id: str = "EMPLOYEE_ID_EXAMPLE",
    channel_id: str = "CHANNEL_ID_EXAMPLE",
    process_data: str = "오늘 생산 현황을 알려줘",
    selections: dict[str, str] | None = None,
) -> dict:
    process = {
        "processdata": process_data,
        "userId": user_id,
        "channelId": channel_id,
    }
    if selections:
        process.update(selections)
    return {
        "richnotificationmessage": {
            "header": {
                "from": {"uniquename": user_id},
                "to": {"channelid": [channel_id]},
            },
            "process": process,
        }
    }


def setup_function() -> None:
    response = client.post("/api/test/reset")
    assert response.status_code == 200


def test_text_callback_runs_dummy_gaia_and_captures_cube_payload() -> None:
    response = client.post("/api/qna", json=valid_callback())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["targetUser"] == "EMPLOYEE_ID_EXAMPLE"
    assert "sessionId" not in body

    outgoing = client.get("/api/test/outgoing-messages").json()["outgoing_messages"]
    assert len(outgoing) == 1
    delivered_text = outgoing[0]["richnotification"]["content"][0]["body"]["row"][0][
        "column"
    ][0]["control"]["text"][0]
    assert "더미 GAIA" in delivered_text
    assert "오늘 생산 현황을 알려줘" in delivered_text
    assert "이전 Chat Output" not in delivered_text

    gaia_run = client.get("/api/test/gaia-runs").json()["gaia_runs"][0]
    assert gaia_run["extracted_answer"] == delivered_text
    assert len(gaia_run["raw_response"]["outputs"][0]["outputs"]) == 2


def test_same_user_and_channel_reuse_the_same_gaia_session() -> None:
    client.post("/api/qna", json=valid_callback())
    first_session_id = client.get("/api/test/sessions").json()["sessions"][0][
        "gaia_session_id"
    ]
    client.post(
        "/api/qna",
        json=valid_callback(process_data="어제 생산 현황도 알려줘"),
    )

    sessions = client.get("/api/test/sessions").json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["gaia_session_id"] == first_session_id
    assert sessions[0]["gaia_session_id"].startswith("gc_")
    assert sessions[0]["request_count"] == 2


def test_rich_message_selection_without_text_becomes_gaia_input() -> None:
    response = client.post(
        "/api/qna",
        json=valid_callback(
            process_data="",
            selections={"UserSelection": "2", "SendBtn": "submit"},
        ),
    )

    assert response.status_code == 200
    gaia_message = client.get("/api/test/gaia-runs").json()["gaia_runs"][0][
        "gaia_message"
    ]
    assert "UserSelection: 2" in gaia_message
    assert "SendBtn: submit" in gaia_message


def test_mismatched_header_and_process_identity_is_rejected() -> None:
    payload = valid_callback()
    payload["richnotificationmessage"]["process"]["userId"] = "OTHER_EMPLOYEE"

    response = client.post("/api/qna", json=payload)

    assert response.status_code == 422
    assert "do not match" in response.json()["detail"]
    assert client.get("/api/test/gaia-runs").json()["gaia_runs"] == []
    assert client.get("/api/test/outgoing-messages").json()["outgoing_messages"] == []


def test_hello_handshake_does_not_run_gaia_or_send_cube_message() -> None:
    response = client.post(
        "/api/qna",
        json=valid_callback(process_data="!@#HelloChatBot#@!"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert client.get("/api/test/gaia-runs").json()["gaia_runs"] == []
    assert client.get("/api/test/outgoing-messages").json()["outgoing_messages"] == []


def test_gaia_response_without_chat_output_is_rejected() -> None:
    response_without_chat_output = {
        "outputs": [
            {
                "outputs": [
                    {
                        "component_display_name": "Text Output",
                        "component_id": "TextOutput-Dummy",
                        "artifacts": {"message": "임의 컴포넌트 답변"},
                    }
                ]
            }
        ]
    }

    with pytest.raises(GaiaResponseError, match="no Chat Output"):
        extract_final_answer(response_without_chat_output)
