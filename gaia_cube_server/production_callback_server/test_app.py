from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import app as callback_app
from app import (
    CUBE_CALLBACK_PATH,
    CUBE_MAX_DISPLAY_TEXT_CHARACTERS,
    CUBE_MAX_RENDERED_ROWS,
    CUBE_MAX_SOURCE_CHARACTERS,
    CUBE_MAX_TABLE_COLUMNS,
    CUBE_TRUNCATED_TABLE_CELL,
    CUBE_TRUNCATION_MESSAGE,
    Settings,
    SettingsError,
    application,
    build_cube_rich_notification,
    create_application,
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


def _rich_body(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["richnotification"]["content"][0]["body"]


def _all_columns(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [column for row in body["row"] for column in row["column"]]


def _label_texts(body: dict[str, Any]) -> list[str]:
    return [
        column["control"]["text"][0]
        for column in _all_columns(body)
        if column["type"] == "label"
    ]


def _control_texts(body: dict[str, Any]) -> list[str]:
    return [column["control"]["text"][0] for column in _all_columns(body)]


def _build_rich(message_text: str) -> dict[str, Any]:
    return build_cube_rich_notification(
        settings=_settings(),
        receiver_id="receiver-user",
        channel_id="channel-A",
        message_text=message_text,
    )


def test_build_cube_rich_notification_keeps_plain_text_legacy_shape() -> None:
    message_text = "GAIA의 일반 텍스트 답변은 기존처럼 한 개의 label로 전달됩니다."

    body = _rich_body(_build_rich(message_text))

    assert body["bodystyle"] == "none"
    assert len(body["row"]) == 1
    column = body["row"][0]["column"][0]
    assert column["type"] == "label"
    assert column["control"] == {
        "active": "true",
        "text": [message_text],
        "color": "#000000",
    }


def test_build_cube_rich_notification_renders_markdown_heading_and_bullets() -> None:
    body = _rich_body(
        _build_rich(
            "# 조회 가능한 데이터\n\n"
            "- 총 12개 데이터셋이 등록되어 있습니다.\n"
            "- 연결 방식: Oracle 11개, Goodocs 1개"
        )
    )

    assert body["bodystyle"] == "none"
    assert _label_texts(body) == [
        "조회 가능한 데이터",
        "• 총 12개 데이터셋이 등록되어 있습니다.",
        "• 연결 방식: Oracle 11개, Goodocs 1개",
    ]
    assert all(
        not text.startswith(("#", "- ")) for text in _label_texts(body)
    )


def test_build_cube_rich_notification_renders_pipe_table_as_grid() -> None:
    body = _rich_body(
        _build_rich(
            "| 데이터셋 | 연결 방식 | 필수 조건 |\n"
            "| --- | --- | --- |\n"
            "| Equipment UPH | Oracle | 없음 |\n"
            "| HOLD History | Oracle | LOT_ID |"
        )
    )

    assert body["bodystyle"] == "Grid"
    assert [[column["type"] for column in row["column"]] for row in body["row"]] == [
        ["label", "label", "label"],
        ["label", "label", "label"],
        ["label", "label", "label"],
    ]
    assert [
        [column["control"]["text"][0] for column in row["column"]]
        for row in body["row"]
    ] == [
        ["데이터셋", "연결 방식", "필수 조건"],
        ["Equipment UPH", "Oracle", "없음"],
        ["HOLD History", "Oracle", "LOT_ID"],
    ]


def test_build_cube_rich_notification_does_not_guess_grid_without_pipe_delimiters() -> None:
    body = _rich_body(
        _build_rich(
            "데이터셋\n"
            "데이터셋 키\n"
            "분류\n"
            "Equipment UPH\n"
            "eqp_uph\n"
            "eqp_uph"
        )
    )

    assert body["bodystyle"] == "none"
    assert [column["type"] for column in _all_columns(body)] == [
        "label",
        "label",
        "label",
        "label",
        "label",
        "label",
    ]
    assert _label_texts(body) == [
        "데이터셋",
        "데이터셋 키",
        "분류",
        "Equipment UPH",
        "eqp_uph",
        "eqp_uph",
    ]


def test_build_cube_rich_notification_renders_html_and_markdown_links() -> None:
    body = _rich_body(
        _build_rich(
            "데이터 다운로드\n"
            '📥 <a href="https://example.test/download.csv?report=12" target="_blank">'
            "<strong>분석 결과 CSV 다운로드</strong></a>\n"
            "🔗 [분석 과정 보기](https://example.test/reports/12)"
        )
    )

    columns = _all_columns(body)
    hypertexts = [column for column in columns if column["type"] == "hypertext"]
    assert _label_texts(body) == ["데이터 다운로드"]
    assert [column["control"] for column in hypertexts] == [
        {
            "active": "true",
            "text": ["📥 분석 결과 CSV 다운로드"],
            "linkurl": "https://example.test/download.csv?report=12",
            "opengraph": "false",
        },
        {
            "active": "true",
            "text": ["🔗 분석 과정 보기"],
            "linkurl": "https://example.test/reports/12",
            "opengraph": "false",
        },
    ]
    assert all("<a" not in text.lower() for text in _label_texts(body))
    assert all("<a" not in column["control"]["text"][0].lower() for column in hypertexts)
    assert all(
        column["control"]["text"][0].startswith(("📥", "🔗"))
        for column in hypertexts
    )


def test_build_cube_rich_notification_preserves_html_block_line_breaks() -> None:
    body = _rich_body(
        _build_rich(
            "<p>첫 번째 문단</p>"
            "<div>둘째 문단<br>셋째 문단</div>"
            '📥 <a href="https://example.test/download.csv">CSV 다운로드</a>'
        )
    )

    assert _label_texts(body) == ["첫 번째 문단", "둘째 문단", "셋째 문단"]
    hypertexts = [
        column for column in _all_columns(body) if column["type"] == "hypertext"
    ]
    assert hypertexts[0]["control"] == {
        "active": "true",
        "text": ["📥 CSV 다운로드"],
        "linkurl": "https://example.test/download.csv",
        "opengraph": "false",
    }


def test_build_cube_rich_notification_styles_explicit_guidance_rows() -> None:
    body = _rich_body(
        _build_rich(
            "### 오류: 조회 서버에 연결할 수 없습니다.\n"
            "> 주의: 수치는 잠정 집계값입니다.\n"
            "추가 조건 필요: 조회 날짜를 입력해 주세요.\n"
            "필수 조건이 있는 데이터셋은 1개입니다."
        )
    )

    columns = [row["column"][0] for row in body["row"]]
    assert [column["control"]["text"][0] for column in columns] == [
        "오류: 조회 서버에 연결할 수 없습니다.",
        "주의: 수치는 잠정 집계값입니다.",
        "추가 조건 필요: 조회 날짜를 입력해 주세요.",
        "필수 조건이 있는 데이터셋은 1개입니다.",
    ]
    assert [column["control"]["color"] for column in columns] == [
        "#b42318",
        "#9a6700",
        "#1f4e79",
        "#000000",
    ]
    assert [column["bgcolor"] for column in columns] == [
        "#fff1f1",
        "#fff8e6",
        "#eef6ff",
        "#ffffff",
    ]
    assert [column["border"] for column in columns] == [
        "true",
        "true",
        "true",
        "false",
    ]


def test_build_cube_rich_notification_styles_natural_guidance_sentences() -> None:
    body = _rich_body(
        _build_rich("오류가 발생했습니다.\n추가 조건이 필요합니다.")
    )

    columns = [row["column"][0] for row in body["row"]]
    assert [column["control"]["color"] for column in columns] == [
        "#b42318",
        "#1f4e79",
    ]


def test_build_cube_rich_notification_keeps_quoted_gt_url_as_hypertext() -> None:
    url = "https://example.test/download.csv?comparison=before>after"

    body = _rich_body(
        _build_rich(f'<a href="{url}">조건이 포함된 CSV 다운로드</a>')
    )

    columns = _all_columns(body)
    assert [column["type"] for column in columns] == ["hypertext"]
    assert columns[0]["control"]["text"] == ["조건이 포함된 CSV 다운로드"]
    assert columns[0]["control"]["linkurl"] == url


def test_build_cube_rich_notification_removes_script_and_style_bodies() -> None:
    body = _rich_body(
        _build_rich(
            "표시 전 텍스트"
            "<script>window.location='https://malicious.test'; alert('숨김')</script>"
            "<style>.hidden { background: url(https://malicious.test); }</style>"
            "표시 후 텍스트"
        )
    )

    assert _label_texts(body) == ["표시 전 텍스트표시 후 텍스트"]
    rendered = "\n".join(_control_texts(body)).lower()
    assert "script" not in rendered
    assert "style" not in rendered
    assert "malicious" not in rendered
    assert "alert" not in rendered


@pytest.mark.parametrize(
    ("href", "label"),
    [
        ("https://example.test/has whitespace", "공백 URL"),
        ("https://user:secret@example.test/download.csv", "사용자정보 URL"),
        ("https://example.test:99999/download.csv", "잘못된 포트 URL"),
    ],
)
def test_build_cube_rich_notification_keeps_invalid_http_urls_as_labels(
    href: str, label: str
) -> None:
    body = _rich_body(_build_rich(f'<a href="{href}">{label}</a>'))

    columns = _all_columns(body)
    assert [column["type"] for column in columns] == ["label"]
    assert columns[0]["control"]["text"] == [label]
    assert "linkurl" not in columns[0]["control"]


def test_build_cube_rich_notification_truncates_oversized_source_safely() -> None:
    body = _rich_body(_build_rich("x" * (CUBE_MAX_SOURCE_CHARACTERS + 1)))

    assert body["bodystyle"] == "none"
    assert len(body["row"]) <= CUBE_MAX_RENDERED_ROWS
    assert _label_texts(body)[-1] == CUBE_TRUNCATION_MESSAGE
    assert len(_label_texts(body)[0]) == CUBE_MAX_DISPLAY_TEXT_CHARACTERS
    assert _label_texts(body)[0].endswith("…")
    assert all(
        len(text) <= CUBE_MAX_DISPLAY_TEXT_CHARACTERS
        for text in _control_texts(body)
    )


def test_build_cube_rich_notification_caps_large_row_counts_with_notice() -> None:
    body = _rich_body(
        _build_rich(
            "\n".join(
                f"결과 행 {index}" for index in range(CUBE_MAX_RENDERED_ROWS + 10)
            )
        )
    )

    assert len(body["row"]) == CUBE_MAX_RENDERED_ROWS
    assert _label_texts(body)[0] == "결과 행 0"
    assert _label_texts(body)[-1] == CUBE_TRUNCATION_MESSAGE
    assert all(column["type"] == "label" for column in _all_columns(body))


def test_build_cube_rich_notification_caps_wide_grid_with_notice() -> None:
    headers = [f"헤더 {index}" for index in range(CUBE_MAX_TABLE_COLUMNS + 1)]
    values = [f"값 {index}" for index in range(CUBE_MAX_TABLE_COLUMNS + 1)]
    markdown_table = "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
            "| " + " | ".join(values) + " |",
        ]
    )

    body = _rich_body(_build_rich(markdown_table))

    assert body["bodystyle"] == "Grid"
    assert len(body["row"]) <= CUBE_MAX_RENDERED_ROWS
    assert len(body["row"][0]["column"]) == CUBE_MAX_TABLE_COLUMNS
    assert len(body["row"][1]["column"]) == CUBE_MAX_TABLE_COLUMNS
    assert body["row"][0]["column"][-1]["control"]["text"] == [
        CUBE_TRUNCATED_TABLE_CELL
    ]
    assert body["row"][1]["column"][-1]["control"]["text"] == [
        CUBE_TRUNCATED_TABLE_CELL
    ]
    assert _label_texts(body)[-1] == CUBE_TRUNCATION_MESSAGE


def test_build_cube_rich_notification_keeps_unknown_or_unsafe_markup_as_labels() -> None:
    body = _rich_body(
        _build_rich(
            "알 수 없는 형식\n"
            "<widget data-state=\"future\">지원되지 않는 태그</widget>\n"
            '<a href="javascript:alert(1)">위험 링크</a>\n'
            '<a href="data:text/html;base64,AAAA">데이터 링크</a>\n'
            "[닫히지 않은 링크](https://example.test"
        )
    )

    columns = _all_columns(body)
    assert body["bodystyle"] == "none"
    assert columns
    assert all(column["type"] == "label" for column in columns)
    assert all("linkurl" not in column["control"] for column in columns)
    assert "지원되지 않는 태그" in "\n".join(_label_texts(body))
    assert "위험 링크" in "\n".join(_label_texts(body))
    assert "데이터 링크" in "\n".join(_label_texts(body))


def test_receiver_runs_full_gaia_to_cube_flow() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "gaia.test":
            assert str(request.url) == "http://gaia.test/v2/agents/agent-a/external"
            assert request.headers["X-Gaia-Auth-Key"] == "test-key"
            assert json.loads(request.content) == {
                "input_value": "생산량을 알려줘",
                "user_id": "employee-1",
                "session_id": json.loads(request.content)["session_id"],
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
    assert response.json()["status"] == "success"
    assert len(requests) == 2


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
    assert gaia_session_ids[1] == "GAIA_SESSION"


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
    assert response.json() == {"status": "fallback_sent"}
    assert len(cube_bodies) == 1
    assert _label_texts(cube_bodies[0]) == [
        "오류: GAIA API가 요청을 정상 처리하지 못했습니다.",
        "temporary failure",
    ]
    assert cube_bodies[0]["row"][0]["column"][0]["control"]["color"] == "#b42318"


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
    assert "주의: GAIA 응답 시간이 초과되었습니다." in rendered
    assert "private GAIA host details" not in rendered
    assert cube_bodies[0]["row"][0]["column"][0]["control"]["color"] == "#9a6700"


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
    assert _label_texts(cube_bodies[0])[0] == (
        "오류: GAIA/Langflow 응답에서 최종 답변을 찾지 못했습니다."
    )


def test_gaia_failure_returns_502_when_cube_fallback_delivery_fails() -> None:
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

    assert response.status_code == 502
    assert response.json() == {
        "status": "error",
        "message": "Unable to deliver the fallback message.",
    }
    assert cube_attempts == 1


def test_receiver_rejects_mismatched_identity_and_ignores_hello() -> None:
    app = create_application(_settings(), transport=httpx.MockTransport(lambda request: None))
    mismatched = _callback()
    mismatched["richnotificationmessage"]["process"]["userId"] = "employee-2"

    with TestClient(app) as client:
        mismatch_response = client.post(CUBE_CALLBACK_PATH, json=mismatched)
        hello_response = client.post(
            CUBE_CALLBACK_PATH,
            json={"richnotificationmessage": {"process": {"processdata": "!@#HelloChatBot#@!"}}},
        )

    assert mismatch_response.status_code == 400
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
