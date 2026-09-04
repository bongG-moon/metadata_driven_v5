"""Focused, offline contract tests for the Flask Portal migration.

The Flask layer must be usable before a real MongoDB, Phoenix, or HCP SSO
runtime is available. These tests deliberately replace those boundaries with
small in-memory doubles and exercise only Flask's test client.
"""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pytest


FLASK_ROOT = Path(__file__).resolve().parents[1]
if str(FLASK_ROOT) not in sys.path:
    sys.path.insert(0, str(FLASK_ROOT))


def _load_flask_portal_module():
    """Load the canonical Flask WebApp module used by ``index.py``."""

    module_name = "web_main"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    return importlib.import_module(module_name)


flask_portal = _load_flask_portal_module()
portal_core = flask_portal.portal_core
application = flask_portal.app


def _load_flask_index_module():
    """Import the HCP WebApp entry module without executing its server block."""

    return importlib.import_module("index")


class FakePortalSettingsStore:
    """Settings-only double: no real MongoDB client is ever constructed."""

    persistent = True

    def __init__(self) -> None:
        self.settings = portal_core._default_portal_settings()
        self.audit_records: list[dict[str, Any]] = []

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self.settings)

    def update(self, update: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        self.settings.update(copy.deepcopy(dict(update)))
        self.record_audit("settings_updated", actor, {"update": dict(update)})
        return self.read()

    def record_audit(self, action: str, actor: Any, details: Mapping[str, Any]) -> None:
        self.audit_records.append(
            {
                "action": action,
                "actor": actor.as_audit_actor(),
                "details": copy.deepcopy(dict(details)),
            }
        )


class FakeScheduleRunReader:
    """Prevents the dashboard bootstrap panel from opening MongoDB."""

    persistent = False

    def list_recent_runs(self, limit: int) -> list[dict[str, Any]]:
        assert limit > 0
        return []

    def close(self) -> None:
        return None


class FakeScheduleStore:
    """Minimal CRUD source-store double for the Flask schedule routes."""

    persistent = True

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.closed = 0

    def list_schedules(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(document) for document in self.documents.values()]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        document = self.documents.get(schedule_id)
        return copy.deepcopy(document) if document is not None else None

    def create_schedule(self, document: Mapping[str, Any]) -> dict[str, Any]:
        created = copy.deepcopy(dict(document))
        self.documents[str(created["_id"])] = created
        return copy.deepcopy(created)

    def update_schedule(
        self, schedule_id: str, update: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if schedule_id not in self.documents:
            return None
        self.documents[schedule_id].update(copy.deepcopy(dict(update)))
        return copy.deepcopy(self.documents[schedule_id])

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.documents.pop(schedule_id, None) is not None

    def close(self) -> None:
        self.closed += 1


def _unexpected_employee_directory_store() -> Any:
    """Flask session identity must never trigger the old MongoDB name lookup."""

    raise AssertionError("Flask 스케줄 저장에서 직원 이름 MongoDB 조회를 하면 안 됩니다.")


@pytest.fixture(autouse=True)
def isolated_flask_portal_runtime(monkeypatch):
    """Keep every migration test independent from local .env configuration."""

    for name in (
        "MONGODB_URI",
        "MONGODB_DATABASE",
        "PTMORE_PORTAL_AUTH_MODE",
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        "PTMORE_PHOENIX_ENDPOINT",
        "PTMORE_PHOENIX_API_KEY",
        "PTMORE_PHOENIX_PROJECTS_JSON",
        "PTMORE_PHOENIX_PROJECT_ID",
        "PTMORE_METADATA_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    # This is the Flask-local identity requested for the first deployment
    # check. It is intentionally established server-side in session, rather
    # than taken from any browser request header or cookie.
    monkeypatch.setenv("PTMORE_PORTAL_FLASK_AUTH_MODE", "mock")
    monkeypatch.setenv(
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        json.dumps([{"employee_id": "2069026", "name": "문봉건"}]),
    )

    store = FakePortalSettingsStore()
    monkeypatch.setattr(portal_core, "_portal_settings_store_factory", lambda: store)
    monkeypatch.setattr(
        portal_core,
        "_portal_schedule_run_reader_factory",
        lambda: FakeScheduleRunReader(),
    )
    yield store


@pytest.fixture
def client():
    application.config.update(TESTING=True)
    with application.test_client() as test_client:
        yield test_client


def test_index_renders_existing_portal_ui_and_employee_photo_marker(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "PTMORE PKG Agent Portal" in page
    assert 'id="my_face"' in page
    assert "http://skynet.skhynix.com/portalWeb/uploadfile/pictures/2069026.jpg" in page
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_index_exports_the_exact_web_main_flask_application() -> None:
    index = _load_flask_index_module()

    assert index.application is flask_portal.app
    assert index.application is application


def test_chrome_devtools_probe_is_acknowledged_without_a_404(client) -> None:
    response = client.get("/.well-known/appspecific/com.chrome.devtools.json")

    assert response.status_code == 204
    assert response.data == b""


def test_mock_flask_session_is_created_with_requested_employee_identity(client) -> None:
    assert client.get("/").status_code == 200

    with client.session_transaction() as current_session:
        assert current_session["emp_no"] == "2069026"
        assert current_session["emp_name"] == "문봉건"
        assert current_session["logFlag"] is True


def test_portal_api_uses_server_side_flask_session_identity_and_profile_url(client) -> None:
    response = client.get("/api/portal")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["viewer"] == {
        "employee_id": "2069026",
        "name": "문봉건",
        "role": "관리자",
        "is_admin": True,
        "profile_image_url": (
            "http://skynet.skhynix.com/portalWeb/uploadfile/pictures/2069026.jpg"
        ),
    }
    assert payload["schedules"] == []
    assert payload["usage_history"] == []
    assert payload["settings"]["admins"][0]["employee_id"] == "2069026"


def test_schedule_post_and_get_use_flask_session_owner_without_mongodb(
    client, monkeypatch
) -> None:
    store = FakeScheduleStore()
    monkeypatch.setattr(portal_core, "_portal_schedule_store_factory", lambda: store)
    monkeypatch.setattr(
        portal_core,
        "_employee_directory_store_factory",
        _unexpected_employee_directory_store,
    )

    created_response = client.post(
        "/api/schedules",
        json={
            "title": "DA 공정 실시간 생산 분석",
            "question": "DA 공정 실시간 생산 분석을 진행해줘.",
            "repeat": "매일",
            "time": "09:30",
        },
    )

    assert created_response.status_code == 201
    created = created_response.get_json()["schedule"]
    assert created["owner_id"] == "2069026"
    assert created["owner_name"] == "문봉건"
    assert created["target"] == "개인 DM"
    assert store.documents[created["id"]]["owner_id"] == "2069026"
    assert store.documents[created["id"]]["owner_name"] == "문봉건"

    listed_response = client.get("/api/schedules")

    assert listed_response.status_code == 200
    assert listed_response.get_json()["schedules"] == [created]
    assert store.closed == 2
