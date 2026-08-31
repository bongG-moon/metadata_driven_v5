"""Offline HCP lifecycle checks for the independent scheduler ASGI app."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


PACKAGE_DIR = Path(__file__).resolve().parent
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "ptmore_scheduler_hcp_app_test",
    PACKAGE_DIR / "app.py",
)
assert _SPEC is not None and _SPEC.loader is not None
app_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = app_module
_SPEC.loader.exec_module(app_module)

from cube_runtime import Settings, SettingsError
from scheduler_worker import SchedulerSettings


def _gaia_settings() -> Settings:
    return Settings(
        gaia_api_url="https://gaia.example.test/external",
        gaia_auth_key="test-key",
        cube_send_url="https://cube.example.test/legacy/richnotification",
        cube_bot_id="C0000000",
        cube_bot_token="bot-token",
        cube_bot_fromusername=("PTMORE",) * 5,
        gaia_timeout_seconds=10,
        cube_timeout_seconds=20,
        user_error_message="잠시 후 다시 시도해 주세요.",
    )


def _scheduler_settings() -> SchedulerSettings:
    return SchedulerSettings(
        mongodb_uri="mongodb://example.test",
        mongodb_database="ptmore",
        schedule_collection="portal_schedules",
        schedule_run_collection="portal_schedule_runs",
        poll_seconds=30,
        lease_seconds=900,
        batch_size=20,
        worker_id="test-worker",
        mongodb_server_selection_timeout_ms=5000,
    )


class _FakeMongoClient:
    def __init__(self) -> None:
        self.closed = False
        self.admin = self

    def command(self, name: str) -> dict[str, int]:
        assert name == "ping"
        return {"ok": 1}

    def __getitem__(self, _name: str) -> "_FakeMongoClient":
        return self

    def close(self) -> None:
        self.closed = True


class _FakeWorker:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.stop_event: asyncio.Event | None = None

    async def run_forever(self, *, stop_event: asyncio.Event | None = None) -> None:
        assert stop_event is not None
        self.stop_event = stop_event
        self.started.set()
        await stop_event.wait()
        self.stopped.set()


def test_hcp_app_starts_background_worker_and_stops_it() -> None:
    mongo = _FakeMongoClient()
    worker = _FakeWorker()
    runtime = app_module.SchedulerServiceRuntime(
        gaia_settings_loader=_gaia_settings,
        scheduler_settings_loader=_scheduler_settings,
        mongo_client_factory=lambda *args, **kwargs: mongo,
        repository_factory=lambda schedules, runs: object(),
        worker_factory=lambda **kwargs: worker,
    )
    application = app_module.create_application(runtime_factory=lambda: runtime)

    with TestClient(application) as client:
        assert worker.started.wait(timeout=1)
        health = client.get("/health")
        ready = client.get("/ready")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "ready": True, "worker": "running"}
        assert ready.status_code == 200
        assert ready.json()["ready"] is True

    assert worker.stopped.wait(timeout=1)
    assert mongo.closed is True


def test_hcp_readiness_is_false_for_configuration_failure_without_secret_echo() -> None:
    secret_marker = "do-not-expose-this-secret"

    def invalid_settings() -> Settings:
        raise SettingsError(secret_marker)

    runtime = app_module.SchedulerServiceRuntime(
        gaia_settings_loader=invalid_settings,
        scheduler_settings_loader=_scheduler_settings,
    )
    application = app_module.create_application(runtime_factory=lambda: runtime)

    with TestClient(application) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        assert health.status_code == 200
        assert health.json() == {
            "status": "not_ready",
            "ready": False,
            "worker": "not_running",
            "reason": "configuration_error",
        }
        assert ready.status_code == 503
        assert secret_marker not in health.text
        assert secret_marker not in ready.text


def test_hcp_entrypoint_contract_is_fixed_and_local() -> None:
    source = (PACKAGE_DIR / "app.py").read_text(encoding="utf-8")
    assert "application = create_application()" in source
    assert "app = application" in source
    assert (
        'uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)'
        in source
    )
    assert "production_callback_server" not in source
    assert "from app import" not in source
