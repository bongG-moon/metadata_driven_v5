from __future__ import annotations

import copy
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The Portal accepts synthetic identities only through its explicit test-mode
# adapter.  This keeps the dashboard route test independent from HCP SSO.
os.environ.setdefault("PTMORE_PORTAL_AUTH_MODE", "test")
import app as portal_app


CLIENT = TestClient(portal_app.application)
ADMIN_HEADERS = {
    "X-PTMORE-Employee-Id": "2069026",
    "X-PTMORE-Employee-Name": "Portal Admin",
}


class _Cursor:
    def __init__(self, documents: list[Mapping[str, Any]]) -> None:
        self._documents = [copy.deepcopy(dict(document)) for document in documents]
        self.sort_spec: list[tuple[str, int]] | None = None
        self.limit_value: int | None = None

    def sort(self, spec: list[tuple[str, int]]) -> _Cursor:
        self.sort_spec = list(spec)
        return self

    def limit(self, limit: int) -> _Cursor:
        self.limit_value = limit
        return self

    def __iter__(self):
        documents = self._documents
        if self.limit_value is not None:
            documents = documents[: self.limit_value]
        return iter(documents)


class _RunCollection:
    def __init__(self, documents: list[Mapping[str, Any]]) -> None:
        self.documents = documents
        self.find_calls: list[tuple[dict[str, Any], dict[str, int]]] = []
        self.cursor: _Cursor | None = None

    def find(self, query: dict[str, Any], projection: dict[str, int]) -> _Cursor:
        self.find_calls.append((copy.deepcopy(query), copy.deepcopy(projection)))
        self.cursor = _Cursor(self.documents)
        return self.cursor


class _ScheduleCollection:
    def __init__(self, documents: list[Mapping[str, Any]]) -> None:
        self.documents = documents
        self.find_calls: list[tuple[dict[str, Any], dict[str, int]]] = []

    def find(
        self, query: dict[str, Any], projection: dict[str, int]
    ) -> list[dict[str, Any]]:
        self.find_calls.append((copy.deepcopy(query), copy.deepcopy(projection)))
        expected_ids = set(query["_id"]["$in"])
        return [
            copy.deepcopy(dict(document))
            for document in self.documents
            if document.get("_id") in expected_ids
        ]


class _FakePortalSettingsStore:
    persistent = True

    def __init__(self) -> None:
        self.settings = portal_app._default_portal_settings()

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self.settings)


class _FakeRunReader:
    persistent = True

    def __init__(self, records: list[Mapping[str, Any]] | Exception) -> None:
        self.records = records
        self.limits: list[int] = []
        self.closed = 0

    def list_recent_runs(self, limit: int) -> list[dict[str, Any]]:
        self.limits.append(limit)
        if isinstance(self.records, Exception):
            raise self.records
        return [copy.deepcopy(dict(record)) for record in self.records]

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def portal_runtime(monkeypatch):
    """Isolate Portal identity/settings while injecting only a run-history reader."""

    monkeypatch.setenv("PTMORE_PORTAL_AUTH_MODE", "test")
    monkeypatch.setenv(
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        json.dumps([{"employee_id": "2069026", "name": "Portal Admin"}]),
    )
    monkeypatch.setattr(portal_app, "_portal_auth_mode_override", None)
    settings_store = _FakePortalSettingsStore()
    monkeypatch.setattr(
        portal_app, "_portal_settings_store_factory", lambda: settings_store
    )
    return settings_store


def test_mongo_run_reader_uses_bounded_safe_projection_and_joins_schedule_title() -> None:
    """Worker-only fields must not survive the read boundary or title join."""

    run_documents = [
        {
            "_id": "raw-run-1",
            "schedule_id": "SCH-MISSING",
            "owner_id": "2071044",
            "status": "failed",
            "scheduled_for": "2026-09-01T00:00:00+00:00",
            "started_at": "2026-09-01T00:01:00+00:00",
            "completed_at": "2026-09-01T00:02:00+00:00",
            "question": "브라우저에 노출되면 안 되는 질문",
            "session_id": "private-session",
            "error_category": "gaia_timeout",
            "delivery_status": "fallback_sent",
            "worker_id": "worker-private",
        },
        {
            "_id": "raw-run-2",
            "schedule_id": "SCH-LIVE",
            "owner_id": "2071044",
            "status": "success",
            "scheduled_for": "2026-09-01T00:10:00+00:00",
            "started_at": "2026-09-01T00:11:00+00:00",
            "completed_at": "2026-09-01T00:12:00+00:00",
            "question": "이 값도 전달되면 안 됩니다",
            "session_id": "another-private-session",
        },
    ]
    runs = _RunCollection(run_documents)
    schedules = _ScheduleCollection(
        [
            {
                "_id": "SCH-LIVE",
                "title": "실제 Mongo 실행 스케줄",
                "owner_id": "2071044",
                "owner_name": "Portal User",
                "question": "스케줄 원문도 여기서는 필요 없습니다",
            }
        ]
    )
    reader = object.__new__(portal_app.MongoPortalScheduleRunReader)
    reader._runs = runs
    reader._schedules = schedules
    reader._mongo_error = RuntimeError

    records = reader.list_recent_runs(8)

    assert runs.find_calls == [
        ({}, portal_app._SCHEDULE_RUN_DOCUMENT_PROJECTION),
    ]
    assert runs.cursor is not None
    assert runs.cursor.sort_spec == [("started_at", -1), ("_id", -1)]
    assert runs.cursor.limit_value == 8
    assert schedules.find_calls == [
        (
            {"_id": {"$in": ["SCH-LIVE", "SCH-MISSING"]}},
            portal_app._SCHEDULE_RUN_SCHEDULE_PROJECTION,
        )
    ]

    expected_keys = {
        "schedule_id",
        "owner_id",
        "status",
        "scheduled_for",
        "started_at",
        "completed_at",
        "schedule_title",
        "schedule_owner_id",
        "schedule_owner_name",
    }
    assert [set(record) for record in records] == [expected_keys, expected_keys]
    # A deleted/missing source schedule has no joined title or owner.  The
    # dashboard transformer turns these empty values into public fallbacks.
    assert records[0]["schedule_title"] == ""
    assert records[0]["schedule_owner_name"] == ""
    assert records[1] == {
        "schedule_id": "SCH-LIVE",
        "owner_id": "2071044",
        "status": "success",
        "scheduled_for": "2026-09-01T00:10:00+00:00",
        "started_at": "2026-09-01T00:11:00+00:00",
        "completed_at": "2026-09-01T00:12:00+00:00",
        "schedule_title": "실제 Mongo 실행 스케줄",
        "schedule_owner_id": "2071044",
        "schedule_owner_name": "Portal User",
    }
    assert "private-session" not in json.dumps(records, ensure_ascii=False)
    assert "브라우저에 노출되면 안 되는 질문" not in json.dumps(records, ensure_ascii=False)


def test_dashboard_recent_runs_maps_worker_statuses_and_completed_kst_time() -> None:
    """Dashboard cards keep only public fields and favour completion time."""

    now = datetime(2026, 9, 1, 12, 0, tzinfo=portal_app._KST)
    cards = portal_app._dashboard_recent_runs(
        [
            {
                "schedule_title": "완료 시각 우선",
                "schedule_owner_id": "2069026",
                "schedule_owner_name": "문봉건",
                "owner_id": "ignored-owner",
                "status": "success",
                # This is later than the completion but must not decide the card time.
                "scheduled_for": "2026-09-01T00:50:00+00:00",
                "started_at": "2026-09-01T00:25:00+00:00",
                "completed_at": "2026-09-01T00:30:00+00:00",
                "question": "private question",
                "session_id": "private-session",
            },
            {
                "schedule_title": "아직 실행 중",
                "owner_id": "2071044",
                "status": "running",
                "started_at": "2026-09-01T00:28:00+00:00",
                "completed_at": None,
            },
            {
                "schedule_title": "취소된 실행",
                "owner_id": "2093012",
                "status": "cancelled",
                "completed_at": "2026-09-01T00:20:00+00:00",
            },
            {
                "schedule_title": "건너뛴 실행",
                "owner_id": "2011111",
                "status": "skipped",
                "completed_at": "2026-09-01T00:10:00+00:00",
            },
            {
                "schedule_title": "",
                "owner_id": "",
                "status": "unrecognised",
                "scheduled_for": "not-an-iso-time",
                "question": "also-private",
            },
        ],
        now=now,
    )

    assert cards == [
        {
            "time": "오늘 09:30",
            "name": "완료 시각 우선",
            "owner": "문봉건 (2069026)",
            "status": "성공",
            "target": "개인 DM",
        },
        {
            "time": "오늘 09:28",
            "name": "아직 실행 중",
            "owner": "2071044",
            "status": "실행 중",
            "target": "개인 DM",
        },
        {
            "time": "오늘 09:20",
            "name": "취소된 실행",
            "owner": "2093012",
            "status": "취소됨",
            "target": "개인 DM",
        },
        {
            "time": "오늘 09:10",
            "name": "건너뛴 실행",
            "owner": "2011111",
            "status": "건너뜀",
            "target": "개인 DM",
        },
        {
            "time": "시간 정보 없음",
            "name": "삭제된 스케줄",
            "owner": "등록자 정보 없음",
            "status": "완료",
            "target": "개인 DM",
        },
    ]
    assert all(set(card) == {"time", "name", "owner", "status", "target"} for card in cards)
    assert "private question" not in json.dumps(cards, ensure_ascii=False)
    assert "private-session" not in json.dumps(cards, ensure_ascii=False)


def test_portal_dashboard_uses_real_reader_cards_instead_of_preview_runs(
    monkeypatch, portal_runtime
) -> None:
    reader = _FakeRunReader(
        [
            {
                "schedule_id": "SCH-LIVE",
                "owner_id": "2071044",
                "status": "success",
                "scheduled_for": "2026-09-01T00:00:00+00:00",
                "started_at": "2026-09-01T00:01:00+00:00",
                "completed_at": "2026-09-01T00:02:00+00:00",
                "schedule_title": "MongoDB에서 읽은 최근 실행",
                "schedule_owner_id": "2071044",
                "schedule_owner_name": "Portal User",
            }
        ]
    )
    monkeypatch.setattr(
        portal_app, "_portal_schedule_run_reader_factory", lambda: reader
    )

    response = CLIENT.get("/api/portal", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    dashboard = response.json()["dashboard"]
    assert len(dashboard["recent_runs"]) == 1
    card = dashboard["recent_runs"][0]
    # This route intentionally uses live current time for relative labels.
    # The exact KST/completed-at precedence is fixed above with an injected
    # clock; here the stable suffix confirms the UTC value was converted to
    # the expected KST minute while exercising the real API integration.
    assert card["time"].endswith("09:02")
    assert {key: value for key, value in card.items() if key != "time"} == {
        "name": "MongoDB에서 읽은 최근 실행",
        "owner": "Portal User (2071044)",
        "status": "성공",
        "target": "개인 DM",
    }
    assert dashboard["recent_runs_message"] == ""
    assert reader.limits == [portal_app._SCHEDULE_RUN_DASHBOARD_LIMIT]
    assert reader.closed == 1


def test_portal_dashboard_does_not_restore_preview_runs_when_history_read_fails(
    monkeypatch, portal_runtime
) -> None:
    reader = _FakeRunReader(portal_app.PortalScheduleStoreError("MongoDB offline"))
    monkeypatch.setattr(
        portal_app, "_portal_schedule_run_reader_factory", lambda: reader
    )

    response = CLIENT.get("/api/portal", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    dashboard = response.json()["dashboard"]
    assert dashboard["recent_runs"] == []
    assert dashboard["recent_runs_message"] == (
        "스케줄 실행 이력을 현재 불러오지 못했습니다. MongoDB 연결 상태를 확인해 주세요."
    )
    assert reader.limits == [portal_app._SCHEDULE_RUN_DASHBOARD_LIMIT]
    assert reader.closed == 1


def test_dashboard_usage_endpoint_uses_real_schedule_run_reader_cards(
    monkeypatch, portal_runtime
) -> None:
    """The post-load dashboard endpoint must use the same non-preview run path."""

    reader = _FakeRunReader(
        [
            {
                "schedule_id": "SCH-USAGE",
                "owner_id": "2071044",
                "status": "failed",
                "scheduled_for": "2026-09-01T00:00:00+00:00",
                "started_at": "2026-09-01T00:01:00+00:00",
                "completed_at": "2026-09-01T00:03:00+00:00",
                "schedule_title": "사용 이력 API의 실제 실행",
                "schedule_owner_id": "2071044",
                "schedule_owner_name": "Portal User",
            }
        ]
    )
    snapshot = {
        "start_day": date(2026, 9, 1),
        "end_day": date(2026, 9, 1),
        "usage_history": [
            {
                "date": "2026-09-01",
                "occurred_at": "2026-09-01T00:00:00+00:00",
                "employee_id": "2071044",
                "user_name": "Portal User",
                "question": "대시보드 사용 이력",
                "channel": "CUBE",
            }
        ],
        "source": {"mode": "test", "status": "test"},
    }
    monkeypatch.setattr(
        portal_app, "_portal_schedule_run_reader_factory", lambda: reader
    )
    monkeypatch.setattr(
        portal_app, "_load_recent_usage_snapshot", lambda: copy.deepcopy(snapshot)
    )

    response = CLIENT.get("/api/dashboard/usage", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    dashboard = response.json()["dashboard"]
    assert len(dashboard["recent_runs"]) == 1
    card = dashboard["recent_runs"][0]
    assert card["time"].endswith("09:03")
    assert {key: value for key, value in card.items() if key != "time"} == {
        "name": "사용 이력 API의 실제 실행",
        "owner": "Portal User (2071044)",
        "status": "실패",
        "target": "개인 DM",
    }
    assert dashboard["recent_runs_message"] == ""
    assert reader.limits == [portal_app._SCHEDULE_RUN_DASHBOARD_LIMIT]
    assert reader.closed == 1
