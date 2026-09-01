from __future__ import annotations

import base64
import copy
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import phoenix_usage


class FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self.body = copy.deepcopy(body)
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self) -> dict[str, Any]:
        return copy.deepcopy(self.body)


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **copy.deepcopy(kwargs)})
        if not self.responses:
            raise AssertionError("Unexpected Phoenix request")
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _project_lookup(name: str, project_id: str) -> FakeResponse:
    return FakeResponse(
        {
            "data": {
                "projects": {
                    "edges": [{"node": {"id": project_id, "name": name}}]
                }
            }
        }
    )


def _spans_page(
    spans: list[dict[str, Any]], *, after: str | None = None
) -> FakeResponse:
    return FakeResponse(
        {
            "data": {
                "node": {
                    "spans": {
                        "edges": [{"node": span} for span in spans],
                        "pageInfo": {
                            "hasNextPage": after is not None,
                            "endCursor": after,
                        },
                    }
                }
            }
        }
    )


def _input_attributes(
    question: str,
    *,
    user_id: str = "2069026",
    platform: str = "CUBE",
) -> str:
    return json.dumps(
        {
            "input.value": json.dumps(
                {
                    "input_value": question,
                    "metadata": json.dumps(
                        {"user_id": user_id, "platform": platform},
                        ensure_ascii=False,
                    ),
                },
                ensure_ascii=False,
            )
        },
        ensure_ascii=False,
    )


def _span(
    trace_id: str,
    started_at: str,
    attributes: Any,
    *,
    name: str = "GaiA Input / ChatInput",
) -> dict[str, Any]:
    return {
        "name": name,
        "context": {"traceId": trace_id},
        "startTime": started_at,
        "attributes": attributes,
    }


def test_config_reads_project_list_and_exposes_safe_status() -> None:
    config = phoenix_usage.PhoenixUsageConfig.from_env(
        {
            "PTMORE_PHOENIX_ENDPOINT": "https://phoenix.example/v1/traces",
            "PTMORE_PHOENIX_API_KEY": "top-secret",
            "PTMORE_PHOENIX_PROJECTS_JSON": '["alpha", "beta", "alpha"]',
            "PTMORE_PHOENIX_PAGE_SIZE": "123",
        }
    )

    assert config.endpoint == "https://phoenix.example/v1/traces"
    assert config.projects == ("alpha", "beta")
    assert config.project_count == 2
    assert config.page_size == 123
    assert config.is_configured is True
    assert config.configuration_errors == ()
    assert "top-secret" not in str(config.configuration_errors)


def test_config_accepts_singular_project_alias_and_reports_missing_settings() -> None:
    config = phoenix_usage.PhoenixUsageConfig.from_env(
        {"PTMORE_PHOENIX_PROJECT_ID": "legacy-project"}
    )

    assert config.projects == ("legacy-project",)
    assert config.is_configured is False
    assert config.missing_settings == (
        "PTMORE_PHOENIX_ENDPOINT",
        "PTMORE_PHOENIX_API_KEY",
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://phoenix.example", "https://phoenix.example/graphql"),
        (
            "https://phoenix.example/path/v1/traces",
            "https://phoenix.example/path/graphql",
        ),
        (
            "https://phoenix.example/path/graphql",
            "https://phoenix.example/path/graphql",
        ),
    ],
)
def test_graphql_url_normalizes_supported_phoenix_endpoints(
    source: str, expected: str
) -> None:
    assert phoenix_usage.graphql_url(source) == expected


def test_authorization_header_adds_bearer_only_when_needed() -> None:
    assert phoenix_usage.authorization_header("abc") == "Bearer abc"
    assert phoenix_usage.authorization_header("bearer abc") == "bearer abc"
    with pytest.raises(phoenix_usage.PhoenixUsageError, match="API key is empty"):
        phoenix_usage.authorization_header("")


def test_relay_project_id_skips_name_lookup() -> None:
    relay_id = base64.b64encode(b"Project:phoenix-node-id").decode("ascii")
    session = FakeSession([])

    resolved = phoenix_usage.resolve_project_id(
        relay_id,
        endpoint="https://phoenix.example",
        api_key="key",
        http_session=session,
    )

    assert resolved == relay_id
    assert session.calls == []


def test_fetch_recent_usage_merges_projects_paginates_and_deduplicates_each_project() -> None:
    # Project A has the same trace on page one twice.  The later span fills in
    # the missing platform but must not create another chat.  Project B has the
    # same trace ID string, which is deliberately retained as a separate chat.
    first_a = _span(
        "shared-trace",
        "2026-08-10T00:10:00Z",
        json.dumps(
            {
                "input.value": json.dumps(
                    {
                        "input_value": "DA 공정 현황 알려줘",
                        "metadata": json.dumps({"user_id": "2069026"}),
                    },
                    ensure_ascii=False,
                )
            },
            ensure_ascii=False,
        ),
    )
    later_a = _span(
        "shared-trace",
        "2026-08-10T00:11:00Z",
        _input_attributes("", user_id="", platform="CUBE"),
    )
    second_a = _span(
        "trace-a-2",
        "2026-08-30T14:00:00+09:00",
        {
            "input": {
                "value": {
                    "question": "스케줄 실행 결과 알려줘",
                    "metadata": {
                        "user_id": "2071044",
                        "platform": "CUBE_SCHEDULING",
                    },
                }
            }
        },
    )
    ignored = _span(
        "ignore-me",
        "2026-08-30T14:01:00+09:00",
        _input_attributes("표시하면 안 되는 span"),
        name="Tool execution",
    )
    project_b_same_trace = _span(
        "shared-trace",
        "2026-08-15T01:00:00Z",
        _input_attributes("다른 프로젝트 질문", user_id="2093012", platform="GAIA"),
    )
    session = FakeSession(
        [
            _project_lookup("project-a", "project-a-id"),
            _spans_page([first_a, later_a], after="a-page-2"),
            _spans_page([second_a, ignored]),
            _project_lookup("project-b", "project-b-id"),
            _spans_page([project_b_same_trace]),
        ]
    )
    config = phoenix_usage.PhoenixUsageConfig(
        endpoint="https://phoenix.example/observability/v1/traces",
        api_key="phoenix-secret",
        projects=("project-a", "project-b"),
    )

    records = phoenix_usage.fetch_recent_usage(
        config,
        days=21,
        today=date(2026, 8, 30),
        http_session=session,
    )

    assert len(records) == 3
    assert [(item["project"], item["question"]) for item in records] == [
        ("project-a", "DA 공정 현황 알려줘"),
        ("project-b", "다른 프로젝트 질문"),
        ("project-a", "스케줄 실행 결과 알려줘"),
    ]
    assert records[0] == {
        "query_time": "2026-08-10T09:10:00+09:00",
        "platform": "CUBE",
        "user_id": "2069026",
        "question": "DA 공정 현황 알려줘",
        "project": "project-a",
        "trace_id": "shared-trace",
    }
    assert records[2]["platform"] == "CUBE_SCHEDULING"
    assert records[2]["user_id"] == "2071044"

    # Two project name lookups plus three paginated span calls.
    assert len(session.calls) == 5
    assert all(call["url"] == "https://phoenix.example/observability/graphql" for call in session.calls)
    assert all(
        call["headers"]["Authorization"] == "Bearer phoenix-secret"
        for call in session.calls
    )
    page_one_variables = session.calls[1]["json"]["variables"]
    assert page_one_variables["after"] is None
    assert page_one_variables["timeRange"] == {
        "start": "2026-08-10T00:00:00+09:00",
        "end": "2026-08-31T00:00:00+09:00",
    }
    assert session.calls[2]["json"]["variables"]["after"] == "a-page-2"


def test_extract_input_info_supports_attribute_fallbacks_and_plain_text() -> None:
    assert phoenix_usage.extract_input_info(
        {
            "input.value": "단순 문자열 질문",
            "metadata.platform": "CUBE",
            "a2a.user_id": "2069026",
        }
    ) == {
        "question": "단순 문자열 질문",
        "platform": "CUBE",
        "user_id": "2069026",
    }


def test_fetch_usage_rejects_nonadvancing_pagination_cursor() -> None:
    session = FakeSession(
        [
            _project_lookup("project-a", "project-a-id"),
            _spans_page([], after="same-cursor"),
            _spans_page([], after="same-cursor"),
        ]
    )
    config = phoenix_usage.PhoenixUsageConfig(
        endpoint="https://phoenix.example",
        api_key="key",
        projects=("project-a",),
    )

    with pytest.raises(phoenix_usage.PhoenixUsageError, match="did not advance"):
        phoenix_usage.fetch_recent_usage(
            config,
            today=date(2026, 8, 30),
            http_session=session,
        )


def test_portal_history_adapter_preserves_time_question_and_platform() -> None:
    history = phoenix_usage.as_portal_usage_history(
        [
            {
                "query_time": "2026-08-30T09:15:00+09:00",
                "platform": "CUBE",
                "user_id": "2069026",
                "question": "오늘 생산량 알려줘",
            }
        ]
    )

    assert history == [
        {
            "id": "PHX-000001",
            "employee_id": "2069026",
            "user_name": "2069026",
            "question": "오늘 생산량 알려줘",
            "date": "2026-08-30",
            "occurred_at": "2026-08-30T09:15:00+09:00",
            "channel": "CUBE",
        }
    ]
