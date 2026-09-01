from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# API contract tests intentionally use the test-only identity adapter.  It is
# never available to a production/local Portal request.
os.environ.setdefault("PTMORE_PORTAL_AUTH_MODE", "test")
import app as portal_app


client = TestClient(portal_app.application)

ADMIN_HEADERS = {
    "X-PTMORE-Employee-Id": "2069026",
    "X-PTMORE-Employee-Name": "Portal Admin",
}
STANDARD_USER_HEADERS = {
    "X-PTMORE-Employee-Id": "2071044",
    "X-PTMORE-Employee-Name": "Portal User",
}


class FakePortalSettingsStore:
    persistent = True

    def __init__(self) -> None:
        self.settings = portal_app._default_portal_settings()

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self.settings)

    def update(self, update: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        del update, actor
        return self.read()

    def record_audit(self, action: str, actor: Any, details: Mapping[str, Any]) -> None:
        del action, actor, details


class FakeScheduleStore:
    """In-memory source-store seam; no test connects to a real MongoDB."""

    persistent = True

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.closed = 0

    def list_schedules(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(item) for item in self.documents.values()]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        value = self.documents.get(schedule_id)
        return copy.deepcopy(value) if value is not None else None

    def create_schedule(self, document: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(document))
        self.documents[str(value["_id"])] = value
        return copy.deepcopy(value)

    def update_schedule(
        self,
        schedule_id: str,
        update: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if schedule_id not in self.documents:
            return None
        self.documents[schedule_id].update(copy.deepcopy(dict(update)))
        # Mirrors the real Portal mutation: a newer user edit invalidates an
        # in-flight worker claim, so its old completion token cannot overwrite
        # this source revision.
        for key in (
            "scheduler_claim_token",
            "scheduler_claimed_at",
            "scheduler_claim_until",
        ):
            self.documents[schedule_id].pop(key, None)
        return copy.deepcopy(self.documents[schedule_id])

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.documents.pop(schedule_id, None) is not None

    def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def isolated_schedule_runtime(monkeypatch):
    # Keep test authorization explicit: production no longer carries sample
    # administrators inside a default settings document.
    monkeypatch.setenv(
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        '[{"employee_id":"2069026","name":"문봉건"}]',
    )
    settings_store = FakePortalSettingsStore()
    schedule_store = FakeScheduleStore()
    monkeypatch.setattr(
        portal_app, "_portal_settings_store_factory", lambda: settings_store
    )
    monkeypatch.setattr(
        portal_app, "_portal_schedule_store_factory", lambda: schedule_store
    )
    return schedule_store


def _interval_payload() -> dict[str, Any]:
    return {
        "title": "DA 공정 실시간 생산 분석",
        "question": "DA 공정 실시간 생산 분석을 진행해줘.",
        "repeat": "interval",
        "interval_minutes": 10,
        "start_time": "08:00",
        "end_time": "18:00",
    }


def _daily_payload() -> dict[str, Any]:
    return {
        "title": "매일 생산 현황",
        "question": "오늘 생산량을 알려줘.",
        "repeat": "매일",
        "time": "09:30",
    }


def test_create_schedule_persists_server_owned_fields_and_projects_safe_response(
    isolated_schedule_runtime: FakeScheduleStore,
) -> None:
    response = client.post(
        "/api/schedules", json=_interval_payload(), headers=STANDARD_USER_HEADERS
    )

    assert response.status_code == 201
    schedule = response.json()["schedule"]
    assert schedule["id"].startswith("SCH-")
    assert schedule["owner_id"] == "2071044"
    assert schedule["owner_name"] == "Portal User"
    assert schedule["owner"] == "2071044"
    assert schedule["target"] == "개인 DM"
    assert schedule["status"] == "활성"
    assert schedule["status_code"] == "active"
    assert schedule["timezone"] == "Asia/Seoul"
    assert schedule["interval_minutes"] == 10
    assert schedule["time"] == ""
    assert schedule["start_time"] == "08:00"
    assert schedule["end_time"] == "18:00"
    assert schedule["next_run_at"].endswith("+00:00")
    assert datetime.fromisoformat(schedule["next_run_at"]).tzinfo is not None

    stored = isolated_schedule_runtime.documents[schedule["id"]]
    assert stored["status"] == "active"
    assert stored["next_run_at"] == schedule["next_run_at"]
    assert stored["target"] == "개인 DM"
    assert stored["owner_id"] == "2071044"
    assert isolated_schedule_runtime.closed == 1


def test_get_allows_any_signed_in_user_but_hides_worker_runtime_fields(
    isolated_schedule_runtime: FakeScheduleStore,
) -> None:
    created = client.post(
        "/api/schedules", json=_daily_payload(), headers=ADMIN_HEADERS
    ).json()["schedule"]
    isolated_schedule_runtime.documents[created["id"]].update(
        {
            "lease_owner": "worker-1",
            "lease_until": "2026-08-31T12:00:00+00:00",
            "last_run_at": "2026-08-31T09:30:00+00:00",
            "last_run_status": "성공",
        }
    )

    response = client.get("/api/schedules", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 200
    schedules = response.json()["schedules"]
    assert len(schedules) == 1
    record = schedules[0]
    assert record["id"] == created["id"]
    assert record["owner_id"] == "2069026"
    assert record["last_run"].endswith("성공")
    assert "lease_owner" not in record
    assert "lease_until" not in record


def test_owner_or_admin_required_for_update_status_and_delete(
    isolated_schedule_runtime: FakeScheduleStore,
) -> None:
    created = client.post(
        "/api/schedules", json=_daily_payload(), headers=ADMIN_HEADERS
    ).json()["schedule"]
    route = f"/api/schedules/{created['id']}"
    isolated_schedule_runtime.documents[created["id"]].update(
        {
            "scheduler_claim_token": "old-worker-claim",
            "scheduler_claimed_at": "2026-08-31T00:00:00+00:00",
            "scheduler_claim_until": "2026-08-31T00:05:00+00:00",
        }
    )

    assert client.patch(
        route, json={"title": "권한 없는 수정"}, headers=STANDARD_USER_HEADERS
    ).status_code == 403
    assert client.patch(
        f"{route}/status", json={"status": "일시중지"}, headers=STANDARD_USER_HEADERS
    ).status_code == 403
    assert client.delete(route, headers=STANDARD_USER_HEADERS).status_code == 403

    response = client.patch(
        route,
        json={"title": "관리자가 수정한 생산 현황", "time": "10:00"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["schedule"]["title"] == "관리자가 수정한 생산 현황"
    assert response.json()["schedule"]["owner_id"] == "2069026"
    stored_after_edit = isolated_schedule_runtime.documents[created["id"]]
    assert "scheduler_claim_token" not in stored_after_edit
    assert "scheduler_claimed_at" not in stored_after_edit
    assert "scheduler_claim_until" not in stored_after_edit

    paused = client.patch(
        f"{route}/status", json={"status": "inactive"}, headers=ADMIN_HEADERS
    )
    assert paused.status_code == 200
    assert paused.json()["schedule"]["status"] == "일시중지"
    assert paused.json()["schedule"]["status_code"] == "inactive"
    assert paused.json()["schedule"]["next_run_at"] is None

    assert client.delete(route, headers=ADMIN_HEADERS).json() == {
        "deleted": True,
        "schedule_id": created["id"],
    }
    assert not isolated_schedule_runtime.documents


def test_interval_validation_and_one_time_time_validation() -> None:
    invalid_window = client.post(
        "/api/schedules",
        json={**_interval_payload(), "start_time": "18:00", "end_time": "08:00"},
        headers=STANDARD_USER_HEADERS,
    )
    assert invalid_window.status_code == 422
    assert invalid_window.json()["detail"]["code"] == "invalid_schedule"

    invalid_repeat = client.post(
        "/api/schedules",
        json={**_daily_payload(), "repeat": "매시간"},
        headers=STANDARD_USER_HEADERS,
    )
    assert invalid_repeat.status_code == 422
    assert invalid_repeat.json()["detail"]["code"] == "invalid_schedule"


def test_missing_mongodb_never_falls_back_to_preview(monkeypatch) -> None:
    monkeypatch.setattr(portal_app, "_portal_schedule_store_factory", None)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)

    response = client.get("/api/schedules", headers=STANDARD_USER_HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "schedule_storage_unavailable"
    assert "MongoDB" in response.json()["detail"]["message"]


def test_next_run_calculation_is_persisted_as_utc_with_kst_timezone() -> None:
    now = datetime(2026, 8, 31, 8, 5, tzinfo=portal_app._KST)
    values = portal_app._schedule_storage_fields(
        _interval_payload(), now=now
    )

    assert values["timezone"] == "Asia/Seoul"
    assert values["next_run_at"] == "2026-08-30T23:10:00+00:00"
    assert values["status"] == "active"


def test_real_mongo_schedule_mutation_unsets_an_inflight_worker_claim() -> None:
    class FakeMongoError(Exception):
        pass

    class FakeResult:
        matched_count = 1

    class FakeCollection:
        def __init__(self) -> None:
            self.update_call: tuple[dict[str, Any], dict[str, Any]] | None = None

        def update_one(self, query: dict[str, Any], mutation: dict[str, Any]) -> FakeResult:
            self.update_call = (copy.deepcopy(query), copy.deepcopy(mutation))
            return FakeResult()

        def find_one(self, query: dict[str, Any], projection: dict[str, int]) -> dict[str, Any]:
            assert query == {"_id": "SCH-12345678-1234-1234-1234-123456789abc"}
            assert projection == portal_app._SCHEDULE_DOCUMENT_PROJECTION
            return {"_id": query["_id"], "title": "after edit"}

    collection = FakeCollection()
    store = object.__new__(portal_app.MongoPortalScheduleStore)
    store._mongo_error = FakeMongoError
    store._schedules = collection

    updated = store.update_schedule(
        "SCH-12345678-1234-1234-1234-123456789abc",
        {"title": "after edit", "next_run_at": "2026-08-31T01:00:00+00:00"},
    )

    assert updated == {
        "_id": "SCH-12345678-1234-1234-1234-123456789abc",
        "title": "after edit",
    }
    assert collection.update_call is not None
    query, mutation = collection.update_call
    assert query == {"_id": "SCH-12345678-1234-1234-1234-123456789abc"}
    assert mutation["$set"]["title"] == "after edit"
    assert mutation["$unset"] == {
        "scheduler_claim_token": "",
        "scheduler_claimed_at": "",
        "scheduler_claim_until": "",
    }
