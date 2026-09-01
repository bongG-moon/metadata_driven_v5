from collections import defaultdict
import copy
import csv
import io
import json
import os
from pathlib import Path
import re
import sys
import types
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The production app accepts only a server-side SSO identity.  Existing Portal
# contract tests intentionally exercise multiple synthetic employees, so they
# use the explicit test-only adapter instead of browser headers in production.
os.environ.setdefault("PTMORE_PORTAL_AUTH_MODE", "test")
import app as portal_app


application = portal_app.application


client = TestClient(application)


ADMIN_HEADERS = {
    "X-PTMORE-Employee-Id": "2069026",
    "X-PTMORE-Employee-Name": "Portal Admin",
}
STANDARD_USER_HEADERS = {
    "X-PTMORE-Employee-Id": "2071044",
    "X-PTMORE-Employee-Name": "Portal User",
}


class FakePortalSettingsStore:
    """Persistent test double for the server-owned portal settings store."""

    persistent = True

    def __init__(self) -> None:
        self.settings = portal_app._default_portal_settings()
        self.audit_records: list[dict[str, object]] = []

    def read(self) -> dict[str, object]:
        return copy.deepcopy(self.settings)

    def update(self, update, actor) -> dict[str, object]:
        changed = dict(update)
        if "gaia_api_caller_employee_id" in changed:
            self.settings["gaia_api_caller_employee_id"] = str(
                changed["gaia_api_caller_employee_id"] or ""
            ).strip()

        policy = changed.get("usage_policy")
        if isinstance(policy, dict):
            self.settings["usage_policy"].update(policy)

        admins = changed.get("admins")
        if isinstance(admins, list):
            self.settings["admins"] = copy.deepcopy(admins)

        self.settings["updated_by"] = actor.as_audit_actor()
        self.record_audit(
            "portal_administrators_updated" if "admins" in changed else "admin_settings_updated",
            actor,
            {"update": changed},
        )
        return self.read()

    def record_audit(self, action, actor, details) -> None:
        self.audit_records.append(
            {
                "action": action,
                "actor": actor.as_audit_actor(),
                "details": copy.deepcopy(dict(details)),
            }
        )


_METADATA_RUNTIME_ENVIRONMENT_NAMES = (
    "PTMORE_METADATA_API_MODE",
    "PTMORE_METADATA_API_URL",
    "PTMORE_METADATA_TABLE_CATALOG_API_URL",
    "PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL",
    "PTMORE_METADATA_DOMAIN_API_URL",
    "PTMORE_METADATA_API_AUTH_HEADER",
    "PTMORE_METADATA_API_AUTH_KEY",
    "PTMORE_METADATA_API_BEARER_TOKEN",
    "PTMORE_METADATA_API_EXTRA_HEADERS_JSON",
    "PTMORE_METADATA_API_TIMEOUT_SECONDS",
    "PTMORE_METADATA_API_VERIFY_TLS",
    "PTMORE_METADATA_API_PAYLOAD_MODE",
    "PTMORE_METADATA_API_INPUT_TYPE",
    "PTMORE_METADATA_API_OUTPUT_TYPE",
    "PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON",
    "PTMORE_METADATA_MONGODB_COLLECTION_MAP_JSON",
    "PTMORE_METADATA_SEND_MONGODB_TWEAKS",
    "PTMORE_METADATA_LIVE_READ_MODE",
    "PTMORE_METADATA_LIVE_MONGODB_URI",
    "PTMORE_METADATA_LIVE_MONGODB_DATABASE",
    "PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON",
    "PTMORE_METADATA_LIVE_READ_LIMIT",
    "MONGODB_URI",
    "MONGODB_DATABASE",
    "MONGODB_COLLECTION_PREFIX",
    "PTMORE_PORTAL_SETTINGS_COLLECTION",
    "PTMORE_PORTAL_AUDIT_COLLECTION",
    "PTMORE_SCHEDULE_COLLECTION",
    "PTMORE_SCHEDULE_RUN_COLLECTION",
    "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
    "PTMORE_USAGE_HISTORY_MODE",
    "PTMORE_USAGE_HISTORY_ARCHIVE_MODE",
    "PTMORE_USAGE_HISTORY_COLLECTION",
    "PTMORE_PHOENIX_ENDPOINT",
    "PTMORE_PHOENIX_API_KEY",
    "PTMORE_PHOENIX_PROJECTS_JSON",
    "PTMORE_PHOENIX_PROJECT_IDS_JSON",
    "PTMORE_PHOENIX_PROJECTS",
    "PTMORE_PHOENIX_PROJECT_ID",
    "PTMORE_PHOENIX_TIMEOUT_SECONDS",
    "PTMORE_PHOENIX_PAGE_SIZE",
    "PTMORE_PHOENIX_FILTER_CONDITION",
    "PTMORE_PHOENIX_SPAN_NAME_PREFIX",
)


@pytest.fixture(autouse=True)
def isolated_portal_runtime(monkeypatch):
    """Keep unit tests independent from a developer's real `.env` values."""

    for name in _METADATA_RUNTIME_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)

    # Test mode has no production SSO/MongoDB bootstrap document.  Keep the
    # synthetic test administrator explicit instead of relying on a sample
    # administrator baked into application defaults.
    monkeypatch.setenv(
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        json.dumps([{"employee_id": "2069026", "name": "문봉건"}]),
    )

    class SuccessfulMongoProbe:
        def ping(self, *, uri: str, database: str) -> None:
            return None

    store = FakePortalSettingsStore()
    monkeypatch.setattr(portal_app, "_portal_settings_store_factory", lambda: store)
    monkeypatch.setattr(portal_app, "_metadata_live_reader_factory", None)
    monkeypatch.setattr(portal_app, "_metadata_live_status_updater_factory", None)
    monkeypatch.setattr(portal_app, "_metadata_live_detail_reader_factory", None)
    monkeypatch.setattr(portal_app, "_phoenix_usage_config_factory", None)
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", None)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", None)
    # Status tests must never attempt to dial a developer's local or example
    # URI. Individual tests replace this with a recording/failing probe.
    monkeypatch.setattr(
        portal_app,
        "_portal_mongodb_connection_probe_factory",
        lambda: SuccessfulMongoProbe(),
    )
    return store


@pytest.fixture
def fake_portal_settings_store(isolated_portal_runtime):
    return isolated_portal_runtime


def _active_user_count(
    usage_history: list[dict[str, str]],
    *,
    min_distinct_days: int,
    min_chat_count: int,
) -> int:
    chat_counts: dict[str, int] = defaultdict(int)
    active_dates: dict[str, set[str]] = defaultdict(set)
    for record in usage_history:
        employee_id = record["employee_id"]
        chat_counts[employee_id] += 1
        active_dates[employee_id].add(record["date"])

    return sum(
        chat_counts[employee_id] >= min_chat_count
        and len(active_dates[employee_id]) >= min_distinct_days
        for employee_id in chat_counts
    )


def _kpi_number(value: object) -> int:
    return int(str(value).replace(",", "").replace("명", "").strip())


def test_design_preview_routes_are_available() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "PTMORE PKG Agent Portal" in response.text
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_chrome_devtools_probe_is_acknowledged_without_a_404() -> None:
    """Chrome probes this optional well-known path when DevTools is open."""

    response = client.get("/.well-known/appspecific/com.chrome.devtools.json")
    assert response.status_code == 204
    assert response.content == b""


def test_dummy_portal_contract_has_all_design_sections() -> None:
    response = client.get("/api/mock/portal")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "viewer",
        "dashboard",
        "schedules",
        "metadata",
        "metadata_authoring",
        "settings",
        "usage_history",
    }
    assert payload["viewer"]["is_admin"] is True
    assert len(payload["schedules"]) >= 1
    assert set(payload["metadata"]) == {"table_catalog", "main_flow_filters", "domain"}
    assert payload["metadata"]["table_catalog"]
    assert payload["metadata"]["main_flow_filters"]
    assert payload["metadata"]["domain"]
    assert len(payload["settings"]["admins"]) >= 1
    assert payload["settings"]["access_policy"] == {
        "metadata_page": "admin_only",
        "metadata_registration": "admin_only",
        "schedule_update": "owner_or_admin",
        "schedule_delete": "owner_or_admin",
        "all_schedule_view": "all_users",
    }

    first_catalog = payload["metadata"]["table_catalog"][0]
    assert {
        "dataset_key",
        "display_name",
        "source_type",
        "required_params",
        "payload",
    } <= first_catalog.keys()
    assert first_catalog["payload"]["source_config"]["query_template"] == (
        "SELECT WORK_DATE, OPER_NAME, PRODUCTION "
        "FROM PROD_TABLE WHERE WORK_DATE = {DATE}"
    )
    assert first_catalog["payload"]["required_param_mappings"] == {
        "DATE": ["WORK_DATE"],
        "PROCESS_GROUP": ["OPER_NAME"],
    }
    assert first_catalog["payload"]["filter_mappings"] == {
        "DATE": ["WORK_DATE"],
        "PROCESS_GROUP": ["OPER_NAME"],
    }
    first_filter = payload["metadata"]["main_flow_filters"][0]
    assert {"filter_key", "operator", "value_type", "value_shape", "payload"} <= first_filter.keys()
    first_domain = payload["metadata"]["domain"][0]
    assert {"section", "key", "payload"} <= first_domain.keys()
    assert "question_cues" not in first_domain
    assert all(
        {"repeat", "time", "owner", "target"} <= schedule.keys()
        for schedule in payload["schedules"]
    )
    assert {schedule["target"] for schedule in payload["schedules"]} == {"개인 DM"}
    assert {run["target"] for run in payload["dashboard"]["recent_runs"]} == {"개인 DM"}

    # One preview record demonstrates interval scheduling.  These fields are
    # optional so existing daily/weekly schedule records remain valid.
    interval_schedules = [
        schedule
        for schedule in payload["schedules"]
        if "interval_minutes" in schedule
    ]
    assert interval_schedules
    for schedule in interval_schedules:
        assert isinstance(schedule["interval_minutes"], int)
        assert schedule["interval_minutes"] > 0
        assert {"start_time", "end_time"} <= schedule.keys()
        assert re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule["start_time"])
        assert re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule["end_time"])
        assert schedule["start_time"] < schedule["end_time"]

    usage_history = payload["usage_history"]
    assert isinstance(usage_history, list)
    assert usage_history
    assert all(
        {"employee_id", "question", "date"} <= record.keys()
        for record in usage_history
    )


def test_metadata_authoring_preview_matches_rev2_flow_contract() -> None:
    portal_payload = client.get("/api/mock/portal").json()
    response = client.get("/api/mock/metadata-authoring")

    assert response.status_code == 200
    authoring = response.json()
    assert authoring == portal_payload["metadata_authoring"]
    assert authoring["contract"] == {
        "version": "metadata_authoring.rev_2.v1",
        "request": {
            "chat_input": "input_value",
            "request_loader": ["raw_text", "duplicate_action", "dry_run"],
            "defaults": {"duplicate_action": "skip", "dry_run": True},
        },
        "response": [
            "status",
            "data.columns/data.rows",
            "metadata_authoring",
            "write_result",
            "trace",
        ],
    }

    assert set(authoring["examples"]) == {"table_catalog", "main_flow_filters", "domain"}
    for example in authoring["examples"].values():
        assert example["raw_text"]
        assert example["duplicate_action"] in {"skip", "merge", "replace", "create_new"}
        assert example["dry_run"] is True
        result = example["result"]
        assert result["response_type"] == "metadata_authoring"
        assert result["status"] == "dry_run"
        assert result["data"]["row_count"] == len(result["data"]["rows"])
        assert result["metadata_authoring"]["contract_validation"]["status"] == "validated"
        assert result["write_result"]["dry_run"] is True

    table_example = authoring["examples"]["table_catalog"]
    assert "query_template:\nSELECT\n  WORK_DATE," in table_example["raw_text"]
    assert "FROM PROD_TABLE\nWHERE WORK_DATE = {DATE}" in table_example["raw_text"]
    assert "filter_mappings:\n- DATE -> WORK_DATE" in table_example["raw_text"]
    assert "required_param_mappings" in table_example["result"]["metadata_authoring"]["refined_text"]


def test_dashboard_usage_history_uses_active_user_policy() -> None:
    payload = client.get("/api/mock/portal").json()
    dashboard = payload["dashboard"]
    usage_history = payload["usage_history"]
    usage_policy = payload["settings"]["usage_policy"]

    assert len(dashboard["usage_by_day"]) == 21
    assert len(dashboard["kpis"]) == 5
    kpis_by_label = {kpi["label"]: kpi for kpi in dashboard["kpis"]}
    assert set(kpis_by_label) == {
        "일 평균 사용자",
        "일 평균 채팅",
        "누적 사용자",
        "누적 채팅",
        "활성 사용자",
    }

    min_distinct_days = usage_policy["active_user_min_distinct_days"]
    min_chat_count = usage_policy["active_user_min_chat_count"]
    assert min_distinct_days >= 1
    assert min_chat_count >= 1
    assert dashboard["active_user_rule"] == {
        "min_distinct_days": min_distinct_days,
        "min_chat_count": min_chat_count,
    }

    expected_active_users = _active_user_count(
        usage_history,
        min_distinct_days=min_distinct_days,
        min_chat_count=min_chat_count,
    )
    assert dashboard["active_user_count"] == expected_active_users
    assert _kpi_number(kpis_by_label["활성 사용자"]["value"]) == expected_active_users


def test_usage_history_endpoint_matches_portal_contract() -> None:
    portal_payload = client.get("/api/mock/portal").json()
    response = client.get("/api/mock/usage-history")

    assert response.status_code == 200
    assert response.json() == portal_payload["usage_history"]


def test_dashboard_usage_route_returns_explicit_preview_source() -> None:
    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"]["mode"] == "preview"
    assert payload["source"]["status"] == "preview"
    assert payload["source"]["project_count"] == 0
    assert len(payload["dashboard"]["usage_by_day"]) == 21
    assert payload["usage_history"]


def test_dashboard_usage_route_uses_phoenix_and_zero_fills_recent_three_weeks(
    monkeypatch,
) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime", "analysis-runtime")
        configuration_errors = ()

    calls: dict[str, object] = {}

    def fake_fetcher(configuration, *, days, today):
        calls["configuration"] = configuration
        calls["days"] = days
        calls["today"] = today
        first_day = today - timedelta(days=20)
        return [
            {
                "query_time": f"{first_day.isoformat()}T09:00:00+09:00",
                "platform": "CUBE",
                "user_id": "2069026",
                "question": "첫 번째 Phoenix 질문",
                "project": "router-runtime",
            },
            {
                "query_time": f"{today.isoformat()}T10:00:00+09:00",
                "platform": "CUBE_SCHEDULING",
                "user_id": "2071044",
                "question": "마지막 Phoenix 질문",
                "project": "analysis-runtime",
            },
        ]

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    fake_configuration = FakePhoenixConfiguration()
    monkeypatch.setattr(
        portal_app,
        "_phoenix_usage_config_factory",
        lambda: fake_configuration,
    )
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", fake_fetcher)

    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    source = payload["source"]
    assert source["mode"] == "phoenix"
    assert source["status"] == "connected"
    assert source["label"] == "Phoenix 사용 이력"
    assert source["detail"] == "최근 3주 GaiA Input 기록을 요청 시점에 조회했습니다."
    assert source["fetched_at"]
    assert source["period"] == {
        "start": (calls["today"] - timedelta(days=20)).isoformat(),
        "end": calls["today"].isoformat(),
    }
    assert source["project_count"] == 2
    assert calls["configuration"] is fake_configuration
    assert calls["days"] == 21
    assert len(payload["dashboard"]["usage_by_day"]) == 21
    assert payload["dashboard"]["usage_by_day"][0] == {
        "date": payload["source"]["period"]["start"],
        "label": (
            f"{int(payload['source']['period']['start'][5:7])}/"
            f"{int(payload['source']['period']['start'][8:])}"
        ),
        "unique_users": 1,
        "chat_count": 1,
        "user_height": 100.0,
        "chat_height": 100.0,
    }
    assert any(
        item["unique_users"] == 0 and item["chat_count"] == 0
        for item in payload["dashboard"]["usage_by_day"][1:-1]
    )
    assert payload["usage_history"][-1]["employee_id"] == "2071044"
    assert payload["usage_history"][-1]["user_name"] == "2071044"
    assert payload["usage_history"][-1]["channel"] == "CUBE_SCHEDULING"
    assert "project" not in payload["usage_history"][-1]


def test_dashboard_usage_route_never_falls_back_to_preview_when_phoenix_fails(
    monkeypatch,
) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime",)
        configuration_errors = ()

    def failing_fetcher(*_args, **_kwargs):
        raise portal_app.PhoenixUsageUnavailableError("not exposed to browser")

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setattr(
        portal_app,
        "_phoenix_usage_config_factory",
        lambda: FakePhoenixConfiguration(),
    )
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", failing_fetcher)

    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "phoenix_usage_unavailable",
        "message": "Phoenix 사용 이력을 조회할 수 없습니다. 연결 정보와 API 권한을 확인해 주세요.",
    }


def test_dashboard_usage_archives_full_phoenix_result_after_fetch_and_exports_csv(
    monkeypatch,
) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime",)
        configuration_errors = ()

    calls: list[str] = []
    covered_scopes: set[tuple[str, date]] = set()
    stored_records: list[dict[str, str]] = [
        {
            "query_time": "2026-07-01T09:10:00+09:00",
            "date": "2026-07-01",
            "platform": "GAIA",
            "user_id": "2040001",
            "question": "과거 보관 질문",
            "project": "router-runtime",
            "trace_id": "old-trace",
        }
    ]

    class FakeArchive:
        def covered_scopes(self, *, start_day, end_day, source_projects=None):
            calls.append("coverage")
            projects = tuple(source_projects or ())
            return {
                (project, scope_day)
                for project, scope_day in covered_scopes
                if start_day <= scope_day <= end_day and (not projects or project in projects)
            }

        def refresh(
            self,
            records,
            *,
            start_day,
            end_day,
            source_projects,
            refresh_started_at=None,
        ):
            calls.append("refresh")
            assert tuple(source_projects) == ("router-runtime",)
            assert all(record["trace_id"] == "live-trace" for record in records)
            assert refresh_started_at
            stored_records[:] = [
                *[
                    dict(record, date=str(record["query_time"])[:10])
                    for record in records
                ],
                *[
                    record
                    for record in stored_records
                    if record["date"] < start_day.isoformat()
                ],
            ]
            current = start_day
            while current <= end_day:
                for project in source_projects:
                    covered_scopes.add((str(project), current))
                current += timedelta(days=1)
            return types.SimpleNamespace(upserted_count=len(records), removed_count=0)

        def read_records(self, *, start_day=None, end_day=None):
            calls.append("read")
            if start_day is None:
                return copy.deepcopy(stored_records)
            return [
                copy.deepcopy(record)
                for record in stored_records
                if start_day.isoformat() <= record["date"] <= end_day.isoformat()
            ]

        def close(self):
            calls.append("close")

    def fake_fetcher(_configuration, *, days, today):
        calls.append("fetch")
        assert days == 21
        return [
            {
                "query_time": f"{today.isoformat()}T10:20:30+09:00",
                "platform": "CUBE",
                "user_id": "2069026",
                "question": "실시간 Phoenix 질문",
                "project": "router-runtime",
                "trace_id": "live-trace",
            }
        ]

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")
    monkeypatch.setattr(
        portal_app,
        "_phoenix_usage_config_factory",
        lambda: FakePhoenixConfiguration(),
    )
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", fake_fetcher)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", FakeArchive)

    dashboard_response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert dashboard_response.status_code == 200
    payload = dashboard_response.json()
    assert calls[:4] == ["read", "coverage", "close", "fetch"]
    assert payload["source"]["archive"] == {
        "mode": "configured",
        "status": "synchronized",
        "collection": "portal_usage_history",
        "full_refresh": False,
        "updated_day_count": 21,
        "updated_range_count": 1,
        "upserted_count": 1,
        "removed_count": 0,
        "message": "MongoDB 보관 이력을 표시하고 당일·누락 날짜만 Phoenix에서 갱신했습니다.",
    }
    assert payload["usage_history"][-1]["question"] == "실시간 Phoenix 질문"

    export_response = client.get(
        "/api/dashboard/usage/export.csv?scope=all",
        headers=ADMIN_HEADERS,
    )

    assert export_response.status_code == 200
    assert export_response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"ptmore_usage_history_all_all.csv\"" == export_response.headers[
        "content-disposition"
    ]
    csv_text = export_response.content.decode("utf-8-sig")
    csv_lines = csv_text.splitlines()
    assert csv_lines[0] == "PROJECT,일자,시간,플랫폼,사용자(사번),질문내용"
    assert "router-runtime,2026-07-01,09:10:00,GAIA,2040001,과거 보관 질문" in csv_lines
    assert any("실시간 Phoenix 질문" in line for line in csv_lines)


def test_recent_usage_csv_remains_available_to_an_ordinary_portal_user() -> None:
    response = client.get(
        "/api/dashboard/usage/export.csv",
        headers=STANDARD_USER_HEADERS,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")


def test_historical_usage_csv_reads_archive_without_live_phoenix(monkeypatch) -> None:
    historical_day = portal_app._recent_kst_period()[0] - timedelta(days=1)
    calls: list[object] = []

    class FakeArchive:
        def read_records(self, *, start_day=None, end_day=None):
            calls.append(("read", start_day, end_day))
            return [
                {
                    "query_time": f"{historical_day.isoformat()}T09:10:00+09:00",
                    "date": historical_day.isoformat(),
                    "platform": "CUBE",
                    "user_id": "2040001",
                    "question": "Phoenix 없이 보관 이력 조회",
                    "project": "router-runtime",
                    "trace_id": "archived-trace",
                }
            ]

        def close(self):
            calls.append("close")

    def unexpected_phoenix(*_args, **_kwargs):
        raise AssertionError("fully historical export must not call Phoenix")

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")
    monkeypatch.setattr(portal_app, "_phoenix_usage_config_factory", unexpected_phoenix)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", FakeArchive)

    response = client.get(
        "/api/dashboard/usage/export.csv"
        f"?start_date={historical_day.isoformat()}&end_date={historical_day.isoformat()}",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert "Phoenix 없이 보관 이력 조회" in response.content.decode("utf-8-sig")
    assert calls == [("read", historical_day, historical_day), "close"]


def test_long_term_usage_csv_requires_an_administrator_before_archive_access(monkeypatch) -> None:
    historical_day = portal_app._recent_kst_period()[0] - timedelta(days=1)
    opened = False

    def archive_factory():
        nonlocal opened
        opened = True
        raise AssertionError("ordinary user must be blocked before archive access")

    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", archive_factory)

    all_response = client.get(
        "/api/dashboard/usage/export.csv?scope=all",
        headers=STANDARD_USER_HEADERS,
    )
    historical_response = client.get(
        "/api/dashboard/usage/export.csv"
        f"?start_date={historical_day.isoformat()}&end_date={historical_day.isoformat()}",
        headers=STANDARD_USER_HEADERS,
    )

    for response in (all_response, historical_response):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "usage_history_export_admin_required"
    assert opened is False


def test_usage_csv_escapes_spreadsheet_formula_cells() -> None:
    response = portal_app._usage_history_csv_response(
        [
            {
                "project": "=PROJECT()",
                "date": "2026-08-10",
                "query_time": "2026-08-10T09:10:00+09:00",
                "platform": "+CUBE",
                "user_id": "-2040001",
                "question": " @HYPERLINK(\"https://example.test\")",
            }
        ],
        start_day=date(2026, 8, 10),
        end_day=date(2026, 8, 10),
    )

    rows = list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert rows[0] == ["PROJECT", "일자", "시간", "플랫폼", "사용자(사번)", "질문내용"]
    assert rows[1] == [
        "'=PROJECT()",
        "2026-08-10",
        "09:10:00",
        "'+CUBE",
        "'-2040001",
        "'@HYPERLINK(\"https://example.test\")",
    ]


@pytest.mark.parametrize(
    ("archive_collection", "metadata_map"),
    [
        ("portal_schedules", None),
        ("metadata_collection_collision", {"table_catalog": "metadata_collection_collision"}),
    ],
)
def test_usage_archive_collection_collision_is_rejected_before_fetch_or_write(
    monkeypatch,
    archive_collection,
    metadata_map,
) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime",)
        configuration_errors = ()

    calls: list[str] = []

    def unexpected_fetch(*_args, **_kwargs):
        calls.append("fetch")
        raise AssertionError("collection collision must stop before Phoenix fetch")

    def unexpected_archive():
        calls.append("archive")
        raise AssertionError("collection collision must stop before archive open")

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_COLLECTION", archive_collection)
    if metadata_map is not None:
        monkeypatch.setenv("PTMORE_METADATA_MONGODB_COLLECTION_MAP_JSON", json.dumps(metadata_map))
    monkeypatch.setattr(
        portal_app,
        "_phoenix_usage_config_factory",
        lambda: FakePhoenixConfiguration(),
    )
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", unexpected_fetch)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", unexpected_archive)

    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "usage_history_archive_not_ready",
        "message": "장기 사용 이력 보관 MongoDB 설정이 완료되지 않았습니다.",
        "missing": ["PTMORE_USAGE_HISTORY_COLLECTION"],
    }
    assert calls == []


def test_dashboard_usage_uses_archive_and_fetches_only_current_and_missing_days(
    monkeypatch,
) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime", "analysis-runtime")
        configuration_errors = ()

    start_day, end_day = portal_app._recent_kst_period()
    missing_day = start_day + timedelta(days=4)
    calls: list[object] = []
    coverage = {
        (project, scope_day)
        for project in FakePhoenixConfiguration.projects
        for scope_day in portal_app._usage_days(start_day, end_day)
        if scope_day != missing_day
    }
    stored_records = [
        {
            "query_time": f"{start_day.isoformat()}T09:00:00+09:00",
            "date": start_day.isoformat(),
            "platform": "CUBE",
            "user_id": "2069026",
            "question": "보관된 첫 질문",
            "project": "router-runtime",
            "trace_id": "archived-trace",
        }
    ]

    class FakeArchive:
        def covered_scopes(self, *, start_day, end_day, source_projects=None):
            calls.append(("coverage", start_day, end_day))
            projects = tuple(source_projects or ())
            return {
                (project, scope_day)
                for project, scope_day in coverage
                if start_day <= scope_day <= end_day and (not projects or project in projects)
            }

        def refresh(
            self,
            records,
            *,
            start_day,
            end_day,
            source_projects,
            refresh_started_at=None,
        ):
            calls.append(("refresh", start_day, end_day))
            assert tuple(source_projects) == FakePhoenixConfiguration.projects
            assert refresh_started_at
            current = start_day
            while current <= end_day:
                for project in source_projects:
                    coverage.add((str(project), current))
                current += timedelta(days=1)
            stored_records.extend(
                dict(record, date=str(record["query_time"])[:10])
                for record in records
            )
            return types.SimpleNamespace(upserted_count=len(records), removed_count=0)

        def read_records(self, *, start_day=None, end_day=None):
            calls.append(("read", start_day, end_day))
            if start_day is None:
                return copy.deepcopy(stored_records)
            return [
                copy.deepcopy(record)
                for record in stored_records
                if start_day <= date.fromisoformat(record["date"]) <= end_day
            ]

        def close(self):
            calls.append("close")

    def fake_fetcher(_configuration, *, days, today):
        calls.append(("fetch", days, today))
        return [
            {
                "query_time": f"{today.isoformat()}T10:20:30+09:00",
                "platform": "CUBE",
                "user_id": "2069026",
                "question": "선택 조회 Phoenix 질문",
                "project": "router-runtime",
                "trace_id": f"trace-{today.isoformat()}",
            }
        ]

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")
    monkeypatch.setattr(
        portal_app,
        "_phoenix_usage_config_factory",
        lambda: FakePhoenixConfiguration(),
    )
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", fake_fetcher)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", FakeArchive)

    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 200
    assert [entry[1:] for entry in calls if isinstance(entry, tuple) and entry[0] == "fetch"] == [
        (1, missing_day),
        (1, end_day),
    ]
    archive = response.json()["source"]["archive"]
    assert archive["full_refresh"] is False
    assert archive["updated_day_count"] == 2
    assert archive["updated_range_count"] == 2


def test_dashboard_usage_full_refresh_is_admin_only_and_fetches_all_three_weeks(
    monkeypatch,
) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime",)
        configuration_errors = ()

    start_day, end_day = portal_app._recent_kst_period()
    calls: list[object] = []

    class FakeArchive:
        def covered_scopes(self, *, start_day, end_day, source_projects=None):
            calls.append(("coverage", start_day, end_day))
            return {
                (project, scope_day)
                for project in tuple(source_projects or ())
                for scope_day in portal_app._usage_days(start_day, end_day)
            }

        def refresh(
            self,
            records,
            *,
            start_day,
            end_day,
            source_projects,
            refresh_started_at=None,
        ):
            calls.append(("refresh", start_day, end_day, len(records)))
            return types.SimpleNamespace(upserted_count=len(records), removed_count=0)

        def read_records(self, *, start_day=None, end_day=None):
            calls.append(("read", start_day, end_day))
            return []

        def close(self):
            calls.append("close")

    def fake_fetcher(_configuration, *, days, today):
        calls.append(("fetch", days, today))
        return []

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")
    monkeypatch.setattr(portal_app, "_phoenix_usage_config_factory", lambda: FakePhoenixConfiguration())
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", fake_fetcher)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", FakeArchive)

    forbidden = client.post("/api/dashboard/usage/refresh", headers=STANDARD_USER_HEADERS)
    assert forbidden.status_code == 403
    assert calls == []

    response = client.post("/api/dashboard/usage/refresh", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert ("fetch", 21, end_day) in calls
    assert ("refresh", start_day, end_day, 0) in calls
    archive = response.json()["source"]["archive"]
    assert archive["full_refresh"] is True
    assert archive["updated_day_count"] == 21


def test_dashboard_usage_full_refresh_requires_live_phoenix_and_archive(monkeypatch) -> None:
    response = client.post("/api/dashboard/usage/refresh", headers=ADMIN_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "phoenix_usage_not_ready",
        "message": "최근 3주 전체 새로고침에는 Phoenix 조회 모드가 필요합니다.",
    }


def test_dashboard_does_not_mutate_archive_when_required_phoenix_fetch_fails(monkeypatch) -> None:
    class FakePhoenixConfiguration:
        is_configured = True
        projects = ("router-runtime",)
        configuration_errors = ()

    calls: list[str] = []

    def failing_fetcher(*_args, **_kwargs):
        raise portal_app.PhoenixUsageUnavailableError("not exposed")

    class FakeArchive:
        def covered_scopes(self, *, start_day, end_day, source_projects=None):
            calls.append("coverage")
            return set()

        def read_records(self, *, start_day=None, end_day=None):
            calls.append("read")
            return []

        def refresh(self, *_args, **_kwargs):
            calls.append("refresh")
            raise AssertionError("Phoenix failure must not begin an archive refresh")

        def close(self):
            calls.append("close")

    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setattr(
        portal_app,
        "_phoenix_usage_config_factory",
        lambda: FakePhoenixConfiguration(),
    )
    monkeypatch.setattr(portal_app, "_phoenix_usage_fetcher", failing_fetcher)
    monkeypatch.setattr(portal_app, "_usage_history_archive_factory", FakeArchive)

    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 503
    assert "refresh" not in calls


def test_dashboard_usage_route_reports_unconfigured_phoenix_without_dummy_data(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_MODE", "phoenix")

    response = client.get("/api/dashboard/usage", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "phoenix_usage_not_ready"
    assert "PTMORE_PHOENIX_ENDPOINT" in detail["missing"]
    assert "PTMORE_PHOENIX_API_KEY" in detail["missing"]
    assert "PTMORE_PHOENIX_PROJECTS_JSON" in detail["missing"]


def test_metadata_status_reports_usage_history_archive_readiness(monkeypatch) -> None:
    monkeypatch.setenv("MONGODB_URI", "mongodb://unit-test")
    monkeypatch.setenv("MONGODB_DATABASE", "ptmore")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_ARCHIVE_MODE", "configured")
    monkeypatch.setenv("PTMORE_USAGE_HISTORY_COLLECTION", "portal_usage_history")

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    archive_status = response.json()["usage_history_archive"]
    assert archive_status == {
        "mode": "configured",
        "configured": True,
        "ready": True,
        "storage_status": "ready",
        "collection": "portal_usage_history",
        "configuration_errors": [],
        "message": "Phoenix 최근 조회 결과를 MongoDB에 날짜별로 동기화해 장기 이력을 보관합니다.",
    }


def test_standard_user_preview_can_view_all_schedules_but_only_owns_some() -> None:
    response = client.get("/api/mock/portal?preview_role=user")

    assert response.status_code == 200
    payload = response.json()
    assert payload["viewer"] == {
        "employee_id": "2071044",
        "name": "김민서",
        "role": "일반 사용자",
        "is_admin": False,
    }
    viewer_employee_id = payload["viewer"]["employee_id"]
    assert any(schedule["owner"] == viewer_employee_id for schedule in payload["schedules"])
    assert any(schedule["owner"] != viewer_employee_id for schedule in payload["schedules"])
    assert payload["settings"]["access_policy"] == {
        "metadata_page": "admin_only",
        "metadata_registration": "admin_only",
        "schedule_update": "owner_or_admin",
        "schedule_delete": "owner_or_admin",
        "all_schedule_view": "all_users",
    }


def test_health_describes_dummy_preview_mode() -> None:
    assert client.get("/health").json() == {"status": "ok", "mode": "dummy-preview"}


def test_metadata_authoring_status_defaults_to_safe_preview_mode(monkeypatch) -> None:
    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "preview"
    assert payload["ready"] is True
    assert payload["preview_only"] is True
    assert payload["api"]["endpoint_configured"] == {
        "table_catalog": False,
        "main_flow_filters": False,
        "domain": False,
    }
    assert payload["mongodb"]["uri_configured"] is False
    assert set(payload["metadata_types"]) == {
        "table_catalog",
        "main_flow_filters",
        "domain",
    }
    assert all(
        item["current_mode_ready"] is True
        and item["endpoint_ready"] is False
        and item["live_contents_checked"] is False
        for item in payload["metadata_types"].values()
    )
    assert payload["flow_metadata_mongodb"]["portal_reads_metadata_collections"] is False
    assert payload["flow_metadata_mongodb"]["live_metadata_contents_checked"] is False
    assert payload["portal_settings_mongodb"]["backend"] == "preview_defaults"
    assert payload["portal_settings_mongodb"]["reads_flow_metadata_collections"] is False
    assert payload["portal_schedule_mongodb"] == {
        "role": "schedule_authoring_and_run_history",
        "configured": False,
        "storage_enabled": False,
        "storage_status": "not_configured",
        "database": None,
        "schedule_collection": "portal_schedules",
        "schedule_run_collection": "portal_schedule_runs",
        "collection_configuration_errors": [],
        "message": (
            "스케줄 저장을 사용하려면 MongoDB 연결 정보와 컬렉션 이름 설정을 확인해 주세요."
        ),
    }
    assert payload["portal_mongodb_connection"] == {
        "configured": False,
        "connected": False,
        "database": None,
        "status": "disabled",
        "message": "MongoDB 연결 정보가 없어 연결 상태 확인이 비활성화되어 있습니다.",
    }


def test_metadata_status_reports_portal_mongodb_connection_without_exposing_uri(monkeypatch) -> None:
    """A configured connection is probed once through an injectable read-only boundary."""

    uri = "mongodb://portal-user:secret-password@example.test:27017/datagov"

    class SuccessfulProbe:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def ping(self, *, uri: str, database: str) -> None:
            self.calls.append((uri, database))

    probe = SuccessfulProbe()
    monkeypatch.setenv("MONGODB_URI", uri)
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(
        portal_app,
        "_portal_mongodb_connection_probe_factory",
        lambda: probe,
    )

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert probe.calls == [(uri, "datagov")]
    payload = response.json()
    assert payload["portal_mongodb_connection"] == {
        "configured": True,
        "connected": True,
        "database": "datagov",
        "status": "connected",
        "message": "MongoDB 연결을 확인했습니다.",
    }
    assert uri not in json.dumps(payload, ensure_ascii=False)
    assert "secret-password" not in json.dumps(payload, ensure_ascii=False)


def test_metadata_status_keeps_response_when_portal_mongodb_ping_fails(monkeypatch) -> None:
    """A failing ping is a visible status, never a failure of the status API itself."""

    class FailingProbe:
        def ping(self, *, uri: str, database: str) -> None:
            raise portal_app.PortalMongoConnectionError("driver details must not be exposed")

    mongo_uri = "mongodb://portal-user:secret-password@example.test:27017/datagov"
    monkeypatch.setenv("MONGODB_URI", mongo_uri)
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(
        portal_app,
        "_portal_mongodb_connection_probe_factory",
        lambda: FailingProbe(),
    )

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["portal_mongodb_connection"] == {
        "configured": True,
        "connected": False,
        "database": "datagov",
        "status": "connection_error",
        "message": "MongoDB 연결을 확인할 수 없습니다. 연결 정보와 네트워크를 확인해 주세요.",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert mongo_uri not in serialized
    assert "secret-password" not in serialized


def test_metadata_status_reports_mongodb_failure_when_settings_store_is_unavailable(
    monkeypatch,
) -> None:
    """The read-only status endpoint must remain usable for a bootstrap admin."""

    class UnavailableSettingsStore:
        persistent = True

        def read(self):
            raise portal_app.PortalSettingsStoreError("MongoDB settings store unavailable")

        def update(self, update, actor):
            raise portal_app.PortalSettingsStoreError("MongoDB settings store unavailable")

        def record_audit(self, action, actor, details) -> None:
            raise portal_app.PortalSettingsStoreError("MongoDB settings store unavailable")

    class FailingProbe:
        def ping(self, *, uri: str, database: str) -> None:
            raise portal_app.PortalMongoConnectionError("MongoDB ping unavailable")

    monkeypatch.setenv("MONGODB_URI", "mongodb://portal-user:secret-password@example.test:27017/datagov")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(
        portal_app,
        "_portal_settings_store_factory",
        lambda: UnavailableSettingsStore(),
    )
    monkeypatch.setattr(
        portal_app,
        "_portal_mongodb_connection_probe_factory",
        lambda: FailingProbe(),
    )

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["portal_mongodb_connection"]["status"] == "connection_error"
    # The emergency authorization path is status-only; normal admin settings
    # remain unavailable rather than falling back to a write-capable store.
    assert client.get("/api/admin/settings", headers=ADMIN_HEADERS).status_code == 503
    assert client.get("/api/metadata-authoring/status", headers=STANDARD_USER_HEADERS).status_code == 403


def test_metadata_status_reports_incomplete_portal_mongodb_configuration(monkeypatch) -> None:
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json()["portal_mongodb_connection"] == {
        "configured": False,
        "connected": False,
        "database": "datagov",
        "status": "not_configured",
        "message": "MongoDB 연결 정보가 완전하지 않아 연결할 수 없습니다.",
    }


def test_metadata_status_separates_portal_settings_from_external_flow_runtime(
    monkeypatch,
) -> None:
    """A configured portal DB must not be represented as live Flow metadata."""

    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv(
        "PTMORE_METADATA_TABLE_CATALOG_API_URL",
        "https://metadata.example.test/run/table-catalog",
    )
    monkeypatch.setenv(
        "PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL",
        "https://metadata.example.test/run/main-filter",
    )
    monkeypatch.setenv(
        "PTMORE_METADATA_DOMAIN_API_URL",
        "https://metadata.example.test/run/domain",
    )
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("MONGODB_COLLECTION_PREFIX", "agent_v4_test_")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "false")

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["all_metadata_types_ready"] is True
    assert payload["portal_settings_mongodb"] == {
        "role": "portal_settings_and_audit",
        "configured": True,
        "backend": "mongodb",
        "connection_read_verified": True,
        "database": "datagov",
        "settings_collection": "portal_settings",
        "audit_collection": "portal_audit_log",
        "collection_configuration_errors": [],
        "reads_flow_metadata_collections": False,
        "message": "Portal 설정과 변경 이력 저장소입니다. 도메인·테이블 카탈로그·메인 필터 메타데이터를 읽는 연결과는 별개입니다.",
    }

    flow_mongodb = payload["flow_metadata_mongodb"]
    assert flow_mongodb["tweaks_enabled"] is False
    assert flow_mongodb["portal_reads_metadata_collections"] is False
    assert flow_mongodb["live_metadata_contents_checked"] is False
    assert flow_mongodb["contents_status"] == "not_checked_by_portal"
    assert flow_mongodb["collections"] == {
        "table_catalog": "agent_v4_test_table_catalog_items",
        "main_flow_filters": "agent_v4_test_main_flow_filters",
        "domain": "agent_v4_test_domain_items",
    }

    for metadata_type, expected_collection in flow_mongodb["collections"].items():
        item = payload["metadata_types"][metadata_type]
        assert item["endpoint_configured"] is True
        assert item["endpoint_source"] == "type_specific_url"
        assert item["endpoint_ready"] is True
        assert item["portal_configured_collection_name"] == expected_collection
        # With tweaks off, the external Flow may use its own deployment
        # setting; Portal must not call this calculated value the live target.
        assert item["expected_flow_collection_name"] is None
        assert item["expected_flow_collection_basis"] == "external_flow_runtime_not_observed"
        assert item["live_contents_checked"] is False


def test_portal_collection_names_are_configurable_and_schedule_storage_is_declared(
    monkeypatch,
) -> None:
    """Portal settings/audit and persisted schedule sources use separate names."""

    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov_test")
    monkeypatch.setenv("PTMORE_PORTAL_SETTINGS_COLLECTION", "ptmore_test_settings")
    monkeypatch.setenv("PTMORE_PORTAL_AUDIT_COLLECTION", "ptmore_test_audit")
    monkeypatch.setenv("PTMORE_SCHEDULE_COLLECTION", "ptmore_test_schedules")
    monkeypatch.setenv("PTMORE_SCHEDULE_RUN_COLLECTION", "ptmore_test_schedule_runs")

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["portal_settings_mongodb"] == {
        "role": "portal_settings_and_audit",
        "configured": True,
        "backend": "mongodb",
        "connection_read_verified": True,
        "database": "datagov_test",
        "settings_collection": "ptmore_test_settings",
        "audit_collection": "ptmore_test_audit",
        "collection_configuration_errors": [],
        "reads_flow_metadata_collections": False,
        "message": "Portal 설정과 변경 이력 저장소입니다. 도메인·테이블 카탈로그·메인 필터 메타데이터를 읽는 연결과는 별개입니다.",
    }
    assert payload["portal_schedule_mongodb"] == {
        "role": "schedule_authoring_and_run_history",
        "configured": True,
        "storage_enabled": True,
        "storage_status": "configured",
        "database": "datagov_test",
        "schedule_collection": "ptmore_test_schedules",
        "schedule_run_collection": "ptmore_test_schedule_runs",
        "collection_configuration_errors": [],
        "message": (
            "스케줄 등록 정보는 Portal MongoDB에 저장됩니다. 실제 실행과 실행 이력 기록은 "
            "별도 Scheduler Worker가 처리합니다."
        ),
    }


def test_mongo_settings_store_uses_configured_settings_and_audit_collections(
    monkeypatch,
) -> None:
    """The Mongo store must use the two configured Portal collection handles."""

    requested_collections: list[str] = []

    class FakePyMongoError(Exception):
        pass

    class FakeDatabase:
        def __getitem__(self, collection_name: str) -> object:
            requested_collections.append(collection_name)
            return object()

    class FakeMongoClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def __getitem__(self, _database_name: str) -> FakeDatabase:
            return FakeDatabase()

    fake_pymongo = types.ModuleType("pymongo")
    fake_pymongo.MongoClient = FakeMongoClient
    fake_pymongo_errors = types.ModuleType("pymongo.errors")
    fake_pymongo_errors.PyMongoError = FakePyMongoError
    monkeypatch.setitem(sys.modules, "pymongo", fake_pymongo)
    monkeypatch.setitem(sys.modules, "pymongo.errors", fake_pymongo_errors)

    collections = portal_app.PortalMongoCollectionSettings(
        settings_collection="ptmore_settings_test",
        audit_collection="ptmore_audit_test",
        schedule_collection="ptmore_schedules_test",
        schedule_run_collection="ptmore_schedule_runs_test",
        configuration_errors=(),
    )
    portal_app.MongoPortalSettingsStore(
        uri="mongodb://example.test:27017",
        database="datagov_test",
        collections=collections,
    )

    assert requested_collections == ["ptmore_settings_test", "ptmore_audit_test"]


def test_portal_collection_names_reject_unsafe_or_duplicate_values(monkeypatch) -> None:
    """Invalid configuration cannot redirect settings or schedule storage."""

    monkeypatch.setenv("PTMORE_PORTAL_SETTINGS_COLLECTION", "ptmore_settings")
    monkeypatch.setenv("PTMORE_PORTAL_AUDIT_COLLECTION", "ptmore_settings")
    monkeypatch.setenv("PTMORE_SCHEDULE_COLLECTION", "system.users")
    monkeypatch.setenv("PTMORE_SCHEDULE_RUN_COLLECTION", "ptmore_schedule_runs")

    collections = portal_app._portal_mongodb_collection_settings_from_env()

    assert collections.ready is False
    assert set(collections.configuration_errors) == {
        "PTMORE_PORTAL_AUDIT_COLLECTION",
        "PTMORE_SCHEDULE_COLLECTION",
    }
    assert collections.all_collections == (
        "portal_settings",
        "portal_audit_log",
        "portal_schedules",
        "portal_schedule_runs",
    )


def test_metadata_status_does_not_expose_connection_or_authentication_secrets(
    monkeypatch,
) -> None:
    mongo_uri = "mongodb://portal-user:portal-password@example.test:27017/datagov"
    api_key = "api-key-that-must-not-be-returned"
    bearer_token = "bearer-token-that-must-not-be-returned"
    extra_header_value = "extra-header-secret-that-must-not-be-returned"
    monkeypatch.setenv("MONGODB_URI", mongo_uri)
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("PTMORE_METADATA_API_AUTH_KEY", api_key)
    monkeypatch.setenv("PTMORE_METADATA_API_BEARER_TOKEN", bearer_token)
    monkeypatch.setenv(
        "PTMORE_METADATA_API_EXTRA_HEADERS_JSON",
        json.dumps({"X-Internal-Token": extra_header_value}),
    )

    response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    serialized = json.dumps(response.json(), ensure_ascii=False)
    for secret in (mongo_uri, api_key, bearer_token, extra_header_value):
        assert secret not in serialized


def test_live_metadata_defaults_to_disabled_without_opening_mongodb(monkeypatch) -> None:
    """A source collection is never read until an operator explicitly enables it."""

    called = False

    def unexpected_reader(_settings):
        nonlocal called
        called = True
        raise AssertionError("disabled live metadata must not create a reader")

    monkeypatch.setattr(portal_app, "_metadata_live_reader_factory", unexpected_reader)

    response = client.get("/api/metadata/live", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert called is False
    assert payload["enabled"] is False
    assert payload["read_only"] is True
    assert payload["status_update"] == {
        "enabled": False,
        "requires_admin": True,
        "record_id_field": "_record_id",
        "allowed_values": ["active", "inactive"],
    }
    assert payload["source"]["mode"] == "disabled"
    assert payload["metadata"] == {
        "table_catalog": [],
        "main_flow_filters": [],
        "domain": [],
    }
    assert all(
        item["read_status"] == "disabled"
        and item["live"] is False
        and item["count"] == 0
        for item in payload["metadata_types"].values()
    )


def test_live_metadata_reads_explicit_collections_and_returns_safe_projection(monkeypatch) -> None:
    """The live route must project only UI-safe fields from each Flow schema."""

    actual_collections = {
        "table_catalog": "agent_v4_table_catalog_items",
        "main_flow_filters": "agent_v4_main_flow_filters",
        "domain": "agent_v4_domain_items",
    }

    class FakeLiveReader:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.closed = False

        def read_collection(self, *, metadata_type, collection_name, item_limit):
            self.calls.append(
                {
                    "metadata_type": metadata_type,
                    "collection_name": collection_name,
                    "item_limit": item_limit,
                }
            )
            documents = {
                "table_catalog": [
                    {
                        "_id": "table_catalog:production_today",
                        "dataset_key": "production_today",
                        "status": "active",
                        "updated_at": "2026-08-30T09:00:00+09:00",
                        "registration_trace": {"raw_text": "do-not-return-raw-text"},
                        "payload": {
                            "display_name": "당일 생산량",
                            "source_type": "oracle",
                            "dataset_family": "production",
                            "aliases": ["오늘 생산", "생산 실적"],
                            "required_params": ["DATE"],
                            "summary": "당일 생산량을 조회합니다.",
                            "source_config": {
                                "db_key": "PNT_RPT",
                                "password": "do-not-return-password",
                                "query_template": "do-not-return-query",
                            },
                        },
                    }
                ],
                "main_flow_filters": [
                    {
                        "_id": "main_flow_filter:date",
                        "filter_key": "date",
                        "status": "active",
                        "updated_at": "2026-08-30T08:00:00+09:00",
                        "payload": {
                            "display_name": "조회 기준일",
                            "semantic_role": "date_filter",
                            "aliases": ["오늘", "기준일"],
                            "columns": [{"column_name": "DATE"}],
                            "operator": "equals",
                            "value_type": "date",
                            "value_shape": "scalar",
                            "description": "조회 기준일을 지정합니다.",
                            "secret": "do-not-return-filter-secret",
                        },
                    }
                ],
                "domain": [
                    {
                        "_id": "domain:quantity_terms:production_qty",
                        "section": "quantity_terms",
                        "key": "production_qty",
                        "status": "active",
                        "updated_at": "2026-08-30T07:00:00+09:00",
                        "payload": {
                            "display_name": "생산량",
                            "aliases": ["생산 수량", "투입량"],
                            "question_cues": ["생산량", "생산 실적"],
                            "summary": "생산 수량 집계에 사용합니다.",
                            "token": "do-not-return-domain-token",
                        },
                    }
                ],
            }
            total_counts = {
                "table_catalog": 10,
                "main_flow_filters": 17,
                "domain": 72,
            }
            return total_counts[metadata_type], documents[metadata_type], False

        def close(self) -> None:
            self.closed = True

    fake_reader = FakeLiveReader()
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv(
        "MONGODB_URI",
        "mongodb://read-user:read-password@example.test:27017/datagov",
    )
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    # The normal Portal prefix intentionally differs from the source prefix.
    monkeypatch.setenv("MONGODB_COLLECTION_PREFIX", "agent_v4_test_")
    monkeypatch.setenv(
        "PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON",
        json.dumps(actual_collections),
    )
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_LIMIT", "25")
    monkeypatch.setattr(
        portal_app,
        "_metadata_live_reader_factory",
        lambda _settings: fake_reader,
    )

    response = client.get("/api/metadata/live", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["read_only"] is True
    assert payload["status_update"] == {
        "enabled": True,
        "requires_admin": True,
        "record_id_field": "_record_id",
        "allowed_values": ["active", "inactive"],
    }
    assert payload["source"] == {
        "database": "datagov",
        "item_limit": 25,
        "mode": "configured",
    }
    assert [call["collection_name"] for call in fake_reader.calls] == [
        "agent_v4_domain_items",
        "agent_v4_table_catalog_items",
        "agent_v4_main_flow_filters",
    ]
    assert all(call["item_limit"] == 25 for call in fake_reader.calls)
    assert fake_reader.closed is True

    catalog = payload["metadata"]["table_catalog"][0]
    assert catalog == {
        "dataset_key": "production_today",
        "display_name": "당일 생산량",
        "dataset_family": "production",
        "source_type": "oracle",
        "required_params": ["DATE"],
        "status": "활성",
        "_record_id": portal_app._encode_metadata_record_id(
            "table_catalog:production_today"
        ),
    }
    assert payload["metadata"]["main_flow_filters"][0] == {
        "filter_key": "date",
        "display_name": "조회 기준일",
        "operator": "equals",
        "value_type": "date",
        "value_shape": "scalar",
        "status": "활성",
        "_record_id": portal_app._encode_metadata_record_id("main_flow_filter:date"),
    }
    assert payload["metadata"]["domain"][0] == {
        "section": "quantity_terms",
        "key": "production_qty",
        "display_name": "생산량",
        "status": "활성",
        "_record_id": portal_app._encode_metadata_record_id(
            "domain:quantity_terms:production_qty"
        ),
    }
    assert payload["metadata_types"]["domain"] == {
        "label": "도메인 정보",
        "collection": "agent_v4_domain_items",
        "collection_source": "live_collection_map",
        "read_status": "success",
        "live": True,
        "count": 72,
        "returned_count": 1,
        "truncated": False,
        "message": "MongoDB 실제 등록 정보를 읽었습니다.",
    }

    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (
        "read-password",
        "do-not-return-raw-text",
        "do-not-return-password",
        "do-not-return-query",
        "do-not-return-filter-secret",
        "do-not-return-domain-token",
    ):
        assert secret not in serialized


def test_live_metadata_requires_complete_explicit_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")

    response = client.get("/api/metadata/live", headers=ADMIN_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "metadata_live_read_not_ready",
        "message": "실제 메타데이터 조회 설정을 확인해 주세요.",
        "missing": ["MONGODB_URI", "MONGODB_DATABASE"],
    }


def test_live_metadata_detail_requires_admin_before_any_live_mongodb_access(monkeypatch) -> None:
    def unexpected_reader(_settings):
        raise AssertionError("unauthorized request must not create a MongoDB detail reader")

    monkeypatch.setattr(portal_app, "_metadata_live_detail_reader_factory", unexpected_reader)
    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")

    response = client.get(
        f"/api/metadata/live/table_catalog/{record_token}",
        headers=STANDARD_USER_HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"


def test_live_metadata_detail_rejects_disabled_preview_source(monkeypatch) -> None:
    def unexpected_reader(_settings):
        raise AssertionError("disabled metadata must not create a MongoDB detail reader")

    monkeypatch.setattr(portal_app, "_metadata_live_detail_reader_factory", unexpected_reader)
    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")

    response = client.get(
        f"/api/metadata/live/table_catalog/{record_token}",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "metadata_live_detail_not_ready",
        "message": "실제 MongoDB 메타데이터 상세 조회 설정을 확인해 주세요.",
        "missing": [],
    }


def test_live_metadata_detail_rejects_malformed_token_before_mongodb_access(monkeypatch) -> None:
    def unexpected_reader(_settings):
        raise AssertionError("malformed token must not create a MongoDB detail reader")

    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017/datagov")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(portal_app, "_metadata_live_detail_reader_factory", unexpected_reader)

    response = client.get(
        "/api/metadata/live/table_catalog/not-a-portal-record-token",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_metadata_record_id"


def test_live_metadata_detail_reads_exact_id_and_redacts_sensitive_fields(monkeypatch) -> None:
    class FakeLiveDetailReader:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.closed = False

        def read_document(self, *, metadata_type, collection_name, record_id):
            self.calls.append(
                {
                    "metadata_type": metadata_type,
                    "collection_name": collection_name,
                    "record_id": record_id,
                }
            )
            if record_id != "table_catalog:production_today":
                return None
            return {
                "_id": "table_catalog:production_today",
                "dataset_key": "production_today",
                "status": "active",
                "registration_trace": {"raw_text": "do-not-return-registration-raw-text"},
                "payload": {
                    "display_name": "Production Today",
                    "dataset_family": "production",
                    "source_type": "oracle",
                    "source_config": {
                        "source_type": "oracle",
                        "db_key": "PNT_RPT",
                        "query_template": (
                            "SELECT WORK_DATE, OPER_NAME, PRODUCTION "
                            "FROM PROD_TABLE WHERE WORK_DATE = {DATE}"
                        ),
                        "password": "do-not-return-password",
                        "api_url": "https://api-user:api-password@example.test/private",
                        "headers": {"Authorization": "do-not-return-authorization"},
                        "token_source": "do-not-return-token-source",
                    },
                    "required_params": ["DATE", "PROCESS_GROUP"],
                    "required_param_mappings": {
                        "DATE": ["WORK_DATE"],
                        "PROCESS_GROUP": ["OPER_NAME"],
                    },
                    "filter_mappings": {
                        "DATE": ["WORK_DATE"],
                        "PROCESS_GROUP": ["OPER_NAME"],
                    },
                    "columns": [
                        {"name": "WORK_DATE", "data_type": "date"},
                        {"name": "OPER_NAME", "data_type": "string"},
                        {"name": "PRODUCTION", "data_type": "number"},
                    ],
                    "selection_criteria": {"status": "active"},
                    "metric_semantics": {
                        "PRODUCTION": {
                            "aggregation": "sum",
                            "apiKey": "do-not-return-camel-api-key",
                        }
                    },
                    "raw_text": "do-not-return-payload-raw-text",
                },
            }

        def close(self) -> None:
            self.closed = True

    fake_reader = FakeLiveDetailReader()
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv(
        "MONGODB_URI",
        "mongodb://read-user:read-password@example.test:27017/datagov",
    )
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv(
        "PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON",
        json.dumps(
            {
                "domain": "agent_v4_domain_items",
                "table_catalog": "agent_v4_table_catalog_items",
                "main_flow_filters": "agent_v4_main_flow_filters",
            }
        ),
    )
    monkeypatch.setattr(
        portal_app,
        "_metadata_live_detail_reader_factory",
        lambda _settings: fake_reader,
    )
    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")

    response = client.get(
        f"/api/metadata/live/table_catalog/{record_token}",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata_type"] == "table_catalog"
    assert payload["metadata_label"] == "테이블 카탈로그"
    assert payload["record_id"] == record_token
    assert payload["source"] == {
        "mode": "configured",
        "collection": "agent_v4_table_catalog_items",
    }
    assert fake_reader.calls == [
        {
            "metadata_type": "table_catalog",
            "collection_name": "agent_v4_table_catalog_items",
            "record_id": "table_catalog:production_today",
        }
    ]
    assert fake_reader.closed is True

    detail = payload["item"]
    assert detail["dataset_key"] == "production_today"
    assert detail["payload"]["source_config"] == {
        "source_type": "oracle",
        "db_key": "PNT_RPT",
        "query_template": (
            "SELECT WORK_DATE, OPER_NAME, PRODUCTION "
            "FROM PROD_TABLE WHERE WORK_DATE = {DATE}"
        ),
    }
    assert detail["payload"]["required_param_mappings"] == {
        "DATE": ["WORK_DATE"],
        "PROCESS_GROUP": ["OPER_NAME"],
    }
    assert detail["payload"]["filter_mappings"] == {
        "DATE": ["WORK_DATE"],
        "PROCESS_GROUP": ["OPER_NAME"],
    }
    assert detail["payload"]["columns"][-1]["name"] == "PRODUCTION"

    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (
        "read-password",
        "do-not-return-registration-raw-text",
        "do-not-return-payload-raw-text",
        "do-not-return-password",
        "api-password",
        "do-not-return-authorization",
        "do-not-return-token-source",
        "do-not-return-camel-api-key",
    ):
        assert secret not in serialized


def test_legacy_metadata_delete_route_is_not_available() -> None:
    """The Portal must never expose a physical metadata-delete API."""

    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")
    response = client.delete(
        f"/api/metadata-authoring/table_catalog/{record_token}",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 404


def test_live_metadata_status_update_requires_admin_before_any_live_mongodb_access(monkeypatch) -> None:
    def unexpected_updater(_settings):
        raise AssertionError("unauthorized request must not create a MongoDB status updater")

    monkeypatch.setattr(portal_app, "_metadata_live_status_updater_factory", unexpected_updater)
    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")

    response = client.patch(
        f"/api/metadata-authoring/table_catalog/{record_token}/status",
        headers=STANDARD_USER_HEADERS,
        json={"status": "inactive"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_required"


def test_live_metadata_status_update_rejects_preview_or_disabled_metadata_source(monkeypatch) -> None:
    def unexpected_updater(_settings):
        raise AssertionError("disabled metadata must not create a MongoDB status updater")

    monkeypatch.setattr(portal_app, "_metadata_live_status_updater_factory", unexpected_updater)
    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")

    response = client.patch(
        f"/api/metadata-authoring/table_catalog/{record_token}/status",
        headers=ADMIN_HEADERS,
        json={"status": "inactive"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "metadata_live_status_update_not_ready",
        "message": "실제 MongoDB 메타데이터 상태 변경 설정을 확인해 주세요.",
        "missing": [],
    }


def test_live_metadata_status_update_sets_exact_opaque_record_id(monkeypatch) -> None:
    class FakeLiveStatusUpdater:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.closed = False

        def update_document_status(
            self,
            *,
            metadata_type,
            collection_name,
            record_id,
            status_value,
        ):
            self.calls.append(
                {
                    "metadata_type": metadata_type,
                    "collection_name": collection_name,
                    "record_id": record_id,
                    "status_value": status_value,
                }
            )
            return record_id == "table_catalog:production_today"

        def close(self) -> None:
            self.closed = True

    fake_updater = FakeLiveStatusUpdater()
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://read-user:read-password@example.test:27017/datagov")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv(
        "PTMORE_METADATA_LIVE_COLLECTION_MAP_JSON",
        json.dumps(
            {
                "domain": "agent_v4_domain_items",
                "table_catalog": "agent_v4_table_catalog_items",
                "main_flow_filters": "agent_v4_main_flow_filters",
            }
        ),
    )
    monkeypatch.setattr(
        portal_app,
        "_metadata_live_status_updater_factory",
        lambda _settings: fake_updater,
    )
    record_token = portal_app._encode_metadata_record_id("table_catalog:production_today")

    response = client.patch(
        f"/api/metadata-authoring/table_catalog/{record_token}/status",
        headers=ADMIN_HEADERS,
        json={"status": "inactive"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata_type"] == "table_catalog"
    assert payload["record_id"] == record_token
    assert payload["status"] == "inactive"
    assert payload["status_label"] == "비활성"
    assert payload["updated"] is True
    assert payload["updated_at"]
    assert fake_updater.calls == [
        {
            "metadata_type": "table_catalog",
            "collection_name": "agent_v4_table_catalog_items",
            "record_id": "table_catalog:production_today",
            "status_value": "inactive",
        }
    ]
    assert fake_updater.closed is True
    assert "read-password" not in json.dumps(payload, ensure_ascii=False)


def test_mongo_metadata_status_updater_uses_set_and_never_deletes() -> None:
    class FakeCollection:
        def __init__(self) -> None:
            self.update_calls: list[tuple[object, object]] = []

        def update_one(self, query, update):
            self.update_calls.append((query, update))
            return types.SimpleNamespace(matched_count=1)

        def delete_one(self, _query):  # pragma: no cover - must never be reached
            raise AssertionError("metadata status update must never call delete_one")

    collection = FakeCollection()
    updater = object.__new__(portal_app.MongoMetadataLiveStatusUpdater)
    updater._database = {"agent_v4_table_catalog_items": collection}
    updater._mongo_error = RuntimeError

    updated = updater.update_document_status(
        metadata_type="table_catalog",
        collection_name="agent_v4_table_catalog_items",
        record_id="table_catalog:production_today",
        status_value="inactive",
    )

    assert updated is True
    assert collection.update_calls == [
        (
            {"_id": "table_catalog:production_today"},
            {"$set": {"status": "inactive"}},
        )
    ]


def test_live_metadata_status_update_returns_not_found_for_exact_missing_id(monkeypatch) -> None:
    class MissingLiveStatusUpdater:
        def __init__(self) -> None:
            self.closed = False

        def update_document_status(self, **_kwargs):
            return False

        def close(self) -> None:
            self.closed = True

    fake_updater = MissingLiveStatusUpdater()
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017/datagov")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(
        portal_app,
        "_metadata_live_status_updater_factory",
        lambda _settings: fake_updater,
    )
    record_token = portal_app._encode_metadata_record_id("main_flow_filter:missing")

    response = client.patch(
        f"/api/metadata-authoring/main_flow_filters/{record_token}/status",
        headers=ADMIN_HEADERS,
        json={"status": "active"},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "metadata_record_not_found"
    assert fake_updater.closed is True


def test_live_metadata_status_update_rejects_invalid_status_before_mongodb_access(monkeypatch) -> None:
    def unexpected_updater(_settings):
        raise AssertionError("invalid status must not create a MongoDB status updater")

    monkeypatch.setattr(portal_app, "_metadata_live_status_updater_factory", unexpected_updater)
    record_token = portal_app._encode_metadata_record_id("domain:quantity_terms:production_qty")

    response = client.patch(
        f"/api/metadata-authoring/domain/{record_token}/status",
        headers=ADMIN_HEADERS,
        json={"status": "draft"},
    )

    assert response.status_code == 422
    assert portal_app._safe_metadata_status("inactive") == "비활성"


def test_live_metadata_status_update_rejects_unknown_type_and_malformed_token(monkeypatch) -> None:
    unknown_type_response = client.patch(
        "/api/metadata-authoring/not-a-metadata-type/s.anything/status",
        headers=ADMIN_HEADERS,
        json={"status": "inactive"},
    )
    assert unknown_type_response.status_code == 404
    assert unknown_type_response.json()["detail"]["code"] == "unknown_metadata_type"

    def unexpected_updater(_settings):
        raise AssertionError("malformed token must not create a MongoDB status updater")

    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017/datagov")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(portal_app, "_metadata_live_status_updater_factory", unexpected_updater)

    malformed_token_response = client.patch(
        "/api/metadata-authoring/domain/not-a-portal-record-token/status",
        headers=ADMIN_HEADERS,
        json={"status": "active"},
    )
    assert malformed_token_response.status_code == 422
    assert malformed_token_response.json()["detail"]["code"] == "invalid_metadata_record_id"


def test_live_metadata_keeps_successful_collections_when_one_collection_fails(monkeypatch) -> None:
    class PartiallyFailingReader:
        def __init__(self) -> None:
            self.closed = False

        def read_collection(self, *, metadata_type, collection_name, item_limit):
            if metadata_type == "main_flow_filters":
                raise portal_app.MetadataLiveReadError("not exposed")
            if metadata_type == "table_catalog":
                return 1, [{"dataset_key": "production", "payload": {}}], False
            return 1, [{"section": "metric_terms", "key": "uph", "payload": {}}], False

        def close(self) -> None:
            self.closed = True

    fake_reader = PartiallyFailingReader()
    monkeypatch.setenv("PTMORE_METADATA_LIVE_READ_MODE", "configured")
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setattr(
        portal_app,
        "_metadata_live_reader_factory",
        lambda _settings: fake_reader,
    )

    response = client.get("/api/metadata/live", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata_types"]["table_catalog"]["read_status"] == "success"
    assert payload["metadata_types"]["main_flow_filters"]["read_status"] == "error"
    assert payload["metadata_types"]["domain"]["read_status"] == "success"
    assert payload["metadata"]["main_flow_filters"] == []
    assert fake_reader.closed is True


def test_metadata_authoring_preview_requires_explicit_preview_mode(
    monkeypatch,
    fake_portal_settings_store,
) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "preview")

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "domain",
            "raw_text": "section은 process_groups이고 key는 DA입니다.",
            "duplicate_action": "merge",
            "dry_run": False,
        },
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"].startswith("META-")
    assert payload["preview_only"] is True
    assert payload["requested_dry_run"] is False
    assert payload["requested_by"]["employee_id"] == "2069026"
    assert fake_portal_settings_store.audit_records[-1]["action"] == "metadata_authoring_requested"
    assert fake_portal_settings_store.audit_records[-1]["actor"] == {
        "employee_id": "2069026",
        "name": "문봉건",
    }
    assert payload["response"]["metadata_authoring"]["original_text"].startswith("section은")
    assert payload["response"]["metadata_authoring"]["duplicate_action"] == "merge"
    assert payload["response"]["trace"]["mode"] == "preview"


def test_metadata_authoring_api_calls_external_flow_and_normalizes_result(
    monkeypatch,
    fake_portal_settings_store,
) -> None:
    class FakeMetadataApiClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def post_json(self, url, *, payload, headers, timeout_seconds, verify_tls):
            self.calls.append(
                {
                    "url": url,
                    "payload": dict(payload),
                    "headers": dict(headers),
                    "timeout_seconds": timeout_seconds,
                    "verify_tls": verify_tls,
                }
            )
            return {
                "outputs": [
                    {
                        "outputs": [
                            {
                                "component_id": "Api-table_catalog-rev-2",
                                "component_display_name": "10 테이블 카탈로그 등록 API 응답 생성기",
                                "results": {
                                    "api_response": {
                                        "data": {
                                            "response_type": "metadata_authoring",
                                            "metadata_type": "table_catalog",
                                            "status": "saved",
                                            "success": True,
                                            "data": {
                                                "columns": ["데이터셋 키"],
                                                "rows": [{"데이터셋 키": "production_weekly"}],
                                            },
                                            "metadata_authoring": {
                                                "contract_validation": {"status": "validated"}
                                            },
                                            "write_result": {
                                                "success": True,
                                                "saved_count": 1,
                                                "status": "saved",
                                            },
                                        }
                                    }
                                }
                            }
                        ]
                    }
                ]
            }

    fake_client = FakeMetadataApiClient()
    monkeypatch.setattr(portal_app, "_metadata_http_client", fake_client)
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("PTMORE_METADATA_API_URL", "https://metadata.example.test/api/v1/run/flow")
    monkeypatch.setenv("PTMORE_METADATA_API_AUTH_HEADER", "X-API-Key")
    monkeypatch.setenv("PTMORE_METADATA_API_AUTH_KEY", "test-api-key")
    monkeypatch.setenv("MONGODB_URI", "mongodb://example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("MONGODB_COLLECTION_PREFIX", "agent_v4_")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "true")
    fake_portal_settings_store.settings["gaia_api_caller_employee_id"] = "2069026"

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "table_catalog",
            "raw_text": "dataset_key는 production_weekly입니다.",
            "duplicate_action": "replace",
            "dry_run": False,
        },
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    result = response.json()
    assert result["preview_only"] is False
    assert result["requested_dry_run"] is False
    assert result["response"]["status"] == "saved"
    assert result["response"]["data"]["row_count"] == 1
    assert result["response"]["metadata_authoring"]["original_text"] == "dataset_key는 production_weekly입니다."

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["url"] == "https://metadata.example.test/api/v1/run/flow"
    assert call["headers"]["X-API-Key"] == "test-api-key"
    assert call["headers"]["X-Gaia-User-Id"] == "2069026"
    outbound_payload = call["payload"]
    assert outbound_payload["input_value"] == "dataset_key는 production_weekly입니다."
    assert outbound_payload["output_type"] == "any"
    tweaks = outbound_payload["tweaks"]
    assert tweaks["00 테이블 카탈로그 등록 요청 로더"] == {
        "duplicate_action": "replace",
        "dry_run": False,
    }
    assert tweaks["07 테이블 카탈로그 검수/저장 처리기"] == {
        "mongo_uri": "mongodb://example.test:27017",
        "mongo_database": "datagov",
        "collection_name": "agent_v4_table_catalog_items",
    }
    # Legacy and lightweight Table Catalog Flows both omit a Snapshot node.
    # Do not send a tweak to a component that does not exist in this variant.
    assert "01 메타데이터 QA 통합 Snapshot 로더" not in tweaks

    status_response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)
    assert status_response.status_code == 200
    table_status = status_response.json()["metadata_types"]["table_catalog"]
    assert table_status["writer_tweak_configured"] is True
    assert table_status["writer_tweak_will_be_sent"] is True
    assert table_status["expected_flow_collection_name"] == "agent_v4_table_catalog_items"
    assert table_status["expected_flow_collection_basis"] == "portal_writer_tweak"


def test_metadata_authoring_api_mode_does_not_fallback_when_endpoint_is_missing(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.delenv("PTMORE_METADATA_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_TABLE_CATALOG_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_DOMAIN_API_URL", raising=False)

    status_response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)
    assert status_response.status_code == 200
    assert status_response.json()["ready"] is False
    assert "endpoint:table_catalog" in status_response.json()["missing"]

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "table_catalog",
            "raw_text": "dataset_key는 production_weekly입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "metadata_api_not_ready"


def test_metadata_authoring_allows_a_type_specific_endpoint(monkeypatch) -> None:
    class FakeMetadataApiClient:
        def post_json(self, url, *, payload, headers, timeout_seconds, verify_tls):
            return {
                "api_response": {
                    "response_type": "metadata_authoring",
                    "status": "dry_run",
                    "success": True,
                    "metadata_authoring": {},
                    "write_result": {"success": True, "status": "dry_run"},
                }
            }

    monkeypatch.setattr(portal_app, "_metadata_http_client", FakeMetadataApiClient())
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.delenv("PTMORE_METADATA_API_URL", raising=False)
    monkeypatch.setenv(
        "PTMORE_METADATA_DOMAIN_API_URL",
        "https://metadata.example.test/api/v1/run/domain-flow",
    )
    monkeypatch.delenv("PTMORE_METADATA_TABLE_CATALOG_API_URL", raising=False)
    monkeypatch.delenv("PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL", raising=False)

    status_response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)
    assert status_response.json()["ready"] is True
    assert status_response.json()["all_metadata_types_ready"] is False

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "domain",
            "raw_text": "section은 process_groups이고 key는 DA입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["preview_only"] is False


def test_metadata_mongo_values_are_not_sent_without_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("MONGODB_URI", "mongodb://sensitive.example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "false")

    settings = portal_app._metadata_settings_from_env()
    payload = portal_app._metadata_api_payload(
        settings,
        metadata_type="domain",
        raw_text="section은 process_groups이고 key는 DA입니다.",
        duplicate_action="skip",
        dry_run=True,
    )

    assert payload["tweaks"] == {
        "00 도메인 등록 요청 로더": {
            "duplicate_action": "skip",
            "dry_run": True,
        }
    }


def test_table_catalog_lightweight_flow_skips_removed_snapshot_tweak(monkeypatch) -> None:
    """A configured blank component must clear the default rather than revive it."""

    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("MONGODB_URI", "mongodb://metadata.example.test:27017")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "true")
    monkeypatch.setenv(
        "PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON",
        json.dumps({"table_catalog": {"snapshot_loader": ""}}),
    )

    settings = portal_app._metadata_settings_from_env()
    payload = portal_app._metadata_api_payload(
        settings,
        metadata_type="table_catalog",
        raw_text="dataset_key는 equipment_assign입니다.",
        duplicate_action="skip",
        dry_run=True,
    )

    assert settings.component_for("table_catalog", "snapshot_loader") == ""
    assert payload["tweaks"] == {
        "00 테이블 카탈로그 등록 요청 로더": {
            "duplicate_action": "skip",
            "dry_run": True,
        },
        "07 테이블 카탈로그 검수/저장 처리기": {
            "mongo_uri": "mongodb://metadata.example.test:27017",
            "mongo_database": "datagov",
            "collection_name": "agent_v4_table_catalog_items",
        },
    }

    monkeypatch.setenv(
        "PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON",
        json.dumps({"table_catalog": {"snapshot_loader": None}}),
    )
    assert portal_app._metadata_settings_from_env().component_for("table_catalog", "snapshot_loader") == ""


def test_metadata_mongo_tweak_mode_requires_configured_mongo_values(monkeypatch) -> None:
    monkeypatch.setenv("PTMORE_METADATA_API_MODE", "api")
    monkeypatch.setenv("PTMORE_METADATA_API_URL", "https://metadata.example.test/api/v1/run/flow")
    monkeypatch.setenv("PTMORE_METADATA_SEND_MONGODB_TWEAKS", "true")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)

    status_response = client.get("/api/metadata-authoring/status", headers=ADMIN_HEADERS)
    status_payload = status_response.json()
    assert status_payload["ready"] is False
    assert "MONGODB_URI" in status_payload["missing"]
    assert "MONGODB_DATABASE" in status_payload["missing"]

    response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "table_catalog",
            "raw_text": "dataset_key는 production_weekly입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "metadata_api_not_ready"


def test_standard_user_cannot_bypass_server_admin_authorization() -> None:
    """A browser-provided is_admin flag must never grant a non-admin write access."""

    forged_browser_headers = {
        **STANDARD_USER_HEADERS,
        "X-PTMORE-Is-Admin": "true",
    }
    metadata_response = client.post(
        "/api/metadata-authoring",
        json={
            "metadata_type": "domain",
            "raw_text": "section은 process_groups이고 key는 DA입니다.",
            "duplicate_action": "skip",
            "dry_run": True,
        },
        headers=forged_browser_headers,
    )

    assert metadata_response.status_code == 403
    assert metadata_response.json()["detail"]["code"] == "admin_required"

    read_response = client.get("/api/admin/settings", headers=forged_browser_headers)
    assert read_response.status_code == 403
    assert read_response.json()["detail"]["code"] == "admin_required"

    status_response = client.get(
        "/api/metadata-authoring/status",
        headers=forged_browser_headers,
    )
    assert status_response.status_code == 403
    assert status_response.json()["detail"]["code"] == "admin_required"

    live_metadata_response = client.get(
        "/api/metadata/live",
        headers=forged_browser_headers,
    )
    assert live_metadata_response.status_code == 403
    assert live_metadata_response.json()["detail"]["code"] == "admin_required"

    write_response = client.put(
        "/api/admin/settings",
        json={
            "usage_policy": {
                "active_user_min_distinct_days": 4,
                "active_user_min_chat_count": 12,
            }
        },
        headers=forged_browser_headers,
    )
    assert write_response.status_code == 403
    assert write_response.json()["detail"]["code"] == "admin_required"


def test_admin_can_read_and_update_settings_without_returning_secrets(
    monkeypatch,
    fake_portal_settings_store,
) -> None:
    """Admin configuration is editable, but credentials remain server-side only."""

    api_key = "metadata-api-key-that-must-not-be-returned"
    bearer_token = "metadata-bearer-token-that-must-not-be-returned"
    mongo_uri = "mongodb://metadata-user:mongodb-password@example.test:27017/datagov"
    extra_header_secret = "extra-header-secret-that-must-not-be-returned"
    monkeypatch.setenv("PTMORE_METADATA_API_AUTH_KEY", api_key)
    monkeypatch.setenv("PTMORE_METADATA_API_BEARER_TOKEN", bearer_token)
    monkeypatch.setenv("PTMORE_METADATA_API_EXTRA_HEADERS_JSON", json.dumps({"X-Internal-Token": extra_header_secret}))
    monkeypatch.setenv("MONGODB_URI", mongo_uri)
    fake_portal_settings_store.settings["gaia_api_caller_employee_id"] = "2069026"

    read_response = client.get("/api/admin/settings", headers=ADMIN_HEADERS)
    assert read_response.status_code == 200
    original = read_response.json()
    assert original["gaia_api_caller_employee_id"] == "2069026"
    assert {"history_window_days", "active_user_min_distinct_days", "active_user_min_chat_count"} <= original["usage_policy"].keys()
    assert original["admins"]
    assert {"employee_id", "name", "role", "scope", "status"} <= original["admins"][0].keys()

    serialized = json.dumps(original, ensure_ascii=False)
    for secret in (api_key, bearer_token, mongo_uri, extra_header_secret):
        assert secret not in serialized

    updated_policy = {
        "active_user_min_distinct_days": 4,
        "active_user_min_chat_count": 12,
    }
    update_response = client.put(
        "/api/admin/settings",
        json={
            "gaia_api_caller_employee_id": "2093012",
            "usage_policy": updated_policy,
        },
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["updated_by"]["employee_id"] == "2069026"
    assert updated["gaia_api_caller_employee_id"] == "2093012"
    assert updated["usage_policy"]["active_user_min_distinct_days"] == 4
    assert updated["usage_policy"]["active_user_min_chat_count"] == 12
    assert fake_portal_settings_store.audit_records[-1]["action"] == "admin_settings_updated"
    assert fake_portal_settings_store.audit_records[-1]["actor"] == {
        "employee_id": "2069026",
        "name": "문봉건",
    }

    reread_response = client.get("/api/admin/settings", headers=ADMIN_HEADERS)
    assert reread_response.status_code == 200
    reread = reread_response.json()
    assert reread["gaia_api_caller_employee_id"] == "2093012"
    assert reread["usage_policy"]["active_user_min_distinct_days"] == 4
    assert reread["usage_policy"]["active_user_min_chat_count"] == 12


def test_bootstrap_admin_must_register_self_before_adding_other_administrators(
    fake_portal_settings_store,
) -> None:
    """Sample admins are not persisted; bootstrap access has a safe first step."""

    assert fake_portal_settings_store.settings["admins"] == []

    initial = client.get("/api/admin/settings", headers=ADMIN_HEADERS)
    assert initial.status_code == 200
    assert initial.json()["admins"] == [
        {
            "employee_id": "2069026",
            "name": "문봉건",
            "role": "Bootstrap Admin",
            "scope": "초기 관리자 등록",
            "status": "활성",
        }
    ]

    blocked = client.post(
        "/api/settings/admins",
        json={"employee_id": "2079411", "employee_name": "최은서"},
        headers=ADMIN_HEADERS,
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "bootstrap_self_registration_required"
    assert fake_portal_settings_store.settings["admins"] == []

    registered = client.post(
        "/api/settings/admins",
        json={"employee_id": "2069026", "employee_name": "문봉건"},
        headers=ADMIN_HEADERS,
    )
    assert registered.status_code == 201
    payload = registered.json()
    assert payload["administrator"] == {
        "employee_id": "2069026",
        "name": "문봉건",
        "role": "관리자",
        "scope": "포털 설정 · 메타데이터 · 스케줄 관리",
        "status": "활성",
    }
    assert fake_portal_settings_store.settings["admins"] == [payload["administrator"]]
    assert fake_portal_settings_store.audit_records[-1]["action"] == "portal_administrators_updated"


def test_administrator_crud_persists_employee_id_authorization(
    fake_portal_settings_store,
) -> None:
    """Only active admins can mutate a unique, server-built admin list."""

    first = client.post(
        "/api/admin/settings/admins",
        json={"employee_id": "2069026", "name": "문봉건"},
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 201

    second = client.post(
        "/api/admin/settings/admins",
        json={"employee_id": "2079411", "name": "최은서"},
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 201
    assert {admin["employee_id"] for admin in second.json()["admins"]} == {
        "2069026",
        "2079411",
    }

    duplicate = client.post(
        "/api/admin/settings/admins",
        json={"employee_id": "2079411", "name": "다른 이름"},
        headers=ADMIN_HEADERS,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "administrator_already_registered"

    malformed = client.post(
        "/api/admin/settings/admins",
        json={"employee_id": "admin", "name": "잘못된 사번"},
        headers=ADMIN_HEADERS,
    )
    assert malformed.status_code == 422

    forged = client.post(
        "/api/admin/settings/admins",
        json={"employee_id": "2093012", "name": "이도윤"},
        headers=STANDARD_USER_HEADERS,
    )
    assert forged.status_code == 403
    assert forged.json()["detail"]["code"] == "admin_required"

    inactive = client.patch(
        "/api/admin/settings/admins/2079411",
        json={"status": "비활성"},
        headers=ADMIN_HEADERS,
    )
    assert inactive.status_code == 200
    assert inactive.json()["administrator"]["status"] == "비활성"
    assert next(
        admin
        for admin in fake_portal_settings_store.settings["admins"]
        if admin["employee_id"] == "2079411"
    )["status"] == "비활성"

    self_deactivate = client.patch(
        "/api/admin/settings/admins/2069026",
        json={"status": "비활성"},
        headers=ADMIN_HEADERS,
    )
    assert self_deactivate.status_code == 422
    assert self_deactivate.json()["detail"]["code"] == "administrator_self_deactivation_forbidden"

    deleted = client.delete(
        "/api/admin/settings/admins/2079411",
        headers=ADMIN_HEADERS,
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["administrator"] is None
    assert [admin["employee_id"] for admin in fake_portal_settings_store.settings["admins"]] == [
        "2069026"
    ]


def test_local_virtual_administrator_is_never_written_to_real_admin_list(
    monkeypatch,
    fake_portal_settings_store,
) -> None:
    """The fixed local identity authorizes development but is not persisted."""

    monkeypatch.setattr(portal_app, "_portal_auth_mode_override", "local")
    response = client.post(
        "/api/admin/settings/admins",
        json={"employee_id": "2079411", "name": "최은서"},
    )
    assert response.status_code == 201
    assert [admin["employee_id"] for admin in fake_portal_settings_store.settings["admins"]] == [
        "2079411"
    ]
    assert "2011111" not in {
        admin["employee_id"] for admin in fake_portal_settings_store.settings["admins"]
    }
