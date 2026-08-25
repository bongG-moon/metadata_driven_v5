from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import CUBE_CALLBACK_PATH, Settings, build_cube_rich_notification, create_application
from markdown_legacy_rich_notification import render_legacy_markdown_to_cube_body
from markdown_rich_notification import render_markdown_to_cube_body


def _settings() -> Settings:
    return Settings(
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


def _columns(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [column for row in body["row"] for column in row["column"]]


SAMPLE_MARKDOWN = """### 오늘 생산 현황
- 총 생산량: 12,480장
주의: 수치는 잠정 집계값입니다.

| 공정 | 생산량 | 상태 |
| --- | ---: | --- |
| DA | 4,820 | 정상 |
| WB | 3,160 | 확인 필요 |

![오늘 생산 추이](https://example.test/reports/production-trend.png)

📥 [CSV 다운로드](https://example.test/reports/production.csv)"""


def test_case_1_legacy_renderer_keeps_the_former_per_line_presentation() -> None:
    body = render_legacy_markdown_to_cube_body(SAMPLE_MARKDOWN)
    columns = _columns(body)

    assert body["bodystyle"] == "Grid"
    assert [column["control"]["text"][0] for column in columns if column["type"] == "label"][:3] == [
        "오늘 생산 현황",
        "• 총 생산량: 12,480장",
        "주의: 수치는 잠정 집계값입니다.",
    ]
    assert columns[0]["control"]["color"] == "#1f4e79"
    assert columns[2]["control"]["color"] == "#9a6700"
    table_header = next(row for row in body["row"] if len(row["column"]) == 3)
    assert {column["width"] for column in table_header["column"]} == {"33.3333%"}
    assert all(column["type"] != "image" for column in columns)
    assert "오늘 생산 추이" in [
        column["control"]["text"][0] for column in columns if column["type"] == "label"
    ]


def test_case_2_production_renderer_keeps_grouped_text_dynamic_table_and_image() -> None:
    body = render_markdown_to_cube_body(SAMPLE_MARKDOWN)
    columns = _columns(body)

    assert body["bodystyle"] == "grid"
    assert [column["control"]["color"] for column in columns if column["type"] == "label"]
    assert all(
        column["control"]["color"] == ""
        for column in columns
        if column["type"] == "label"
    )
    table_header = next(row for row in body["row"] if len(row["column"]) == 3)
    assert len({column["width"] for column in table_header["column"]}) > 1
    image_column = next(column for column in columns if column["type"] == "image")
    assert image_column["control"]["sourceurl"].endswith("production-trend.png")


def test_both_cases_keep_the_same_outer_cube_send_contract() -> None:
    legacy_payload = build_cube_rich_notification(
        _settings(),
        "employee-1",
        "channel-A",
        SAMPLE_MARKDOWN,
        body_renderer=render_legacy_markdown_to_cube_body,
    )
    production_payload = build_cube_rich_notification(
        _settings(),
        "employee-1",
        "channel-A",
        SAMPLE_MARKDOWN,
        body_renderer=render_markdown_to_cube_body,
    )

    legacy = legacy_payload["richnotification"]
    production = production_payload["richnotification"]
    assert legacy["header"] == production["header"]
    assert legacy["content"][0]["process"] == production["content"][0]["process"]
    assert legacy_payload["richnotification"]["content"][0]["body"]["bodystyle"] == "Grid"
    assert production_payload["richnotification"]["content"][0]["body"]["bodystyle"] == "grid"


def _callback() -> dict[str, Any]:
    return {
        "richnotificationmessage": {
            "header": {
                "from": {"uniquename": "employee-1"},
                "to": {"channelid": ["channel-A"]},
            },
            "process": {
                "processdata": "변환 방식 테스트",
                "userId": "employee-1",
                "channelId": "channel-A",
            },
        }
    }


def _gaia_response(answer: str) -> dict[str, Any]:
    return {
        "session_id": "case-session",
        "outputs": [
            {
                "outputs": [
                    {
                        "component_display_name": "Chat Output",
                        "results": {
                            "gaia_response": {"data": {"answer": answer}},
                            "message": {"data": {"error": False, "text": answer}},
                        },
                    }
                ]
            }
        ],
    }


@pytest.mark.parametrize(
    ("body_renderer", "expected_bodystyle"),
    [
        (render_legacy_markdown_to_cube_body, "none"),
        (render_markdown_to_cube_body, "grid"),
    ],
)
def test_each_case_app_uses_its_fixed_renderer_for_the_full_callback_flow(
    body_renderer: Any, expected_bodystyle: str
) -> None:
    outgoing_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gaia.test":
            return httpx.Response(200, json=_gaia_response("### 결과\n일반 답변"))
        if request.url.host == "cube.test":
            outgoing_bodies.append(
                json.loads(request.content)["richnotification"]["content"][0]["body"]
            )
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.url}")

    application = create_application(
        _settings(),
        transport=httpx.MockTransport(handler),
        body_renderer=body_renderer,
    )
    with TestClient(application) as client:
        response = client.post(CUBE_CALLBACK_PATH, json=_callback())

    assert response.status_code == 200
    assert response.json() is None
    assert outgoing_bodies[0]["bodystyle"] == expected_bodystyle
