"""Offline verification for the deployable standalone scheduler package."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Any

import cube_runtime
import httpx
import scheduler_worker as worker_module
from cube_runtime import Settings, build_cube_rich_notification, call_gaia
from scheduler_worker import (
    ClaimedSchedule,
    SchedulerSettings,
    SchedulerWorker,
    build_scheduling_gaia_context,
)


UTC = timezone.utc


def _settings() -> Settings:
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


def _worker_settings() -> SchedulerSettings:
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


def _schedule() -> dict[str, Any]:
    return {
        "_id": "SCH-001",
        "owner_id": "2011111",
        "question": "DA 공정 생산 현황을 알려줘.",
        "status": "active",
        "repeat": "매일",
        "time": "09:30",
        "timezone": "Asia/Seoul",
        "next_run_at": "2026-08-31T00:00:00+00:00",
    }


def test_package_has_no_callback_server_import() -> None:
    worker_source = inspect.getsource(worker_module)
    runtime_source = inspect.getsource(cube_runtime)
    assert "from app import" not in worker_source
    assert "from app import" not in runtime_source
    assert "production_callback_server" not in worker_source


def test_local_runtime_keeps_cube_rich_notification_contract() -> None:
    payload = build_cube_rich_notification(
        _settings(),
        "2011111",
        "",
        "### 결과\n\n| 항목 | 값 |\n| --- | --- |\n| 생산량 | 100 |",
    )
    rich = payload["richnotification"]
    assert rich["header"]["from"] == "C0000000"
    assert rich["header"]["to"] == {"uniquename": ["2011111"], "channelid": [""]}
    process = rich["content"][0]["process"]
    assert process["callbacktype"] == "url"
    assert process["requestid"] == ["request_cond_change_main"]
    assert rich["content"][0]["body"]["row"]


def test_local_runtime_keeps_gaia_external_tweak_contract() -> None:
    observed: dict[str, Any] = {}

    async def send() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            observed["headers"] = dict(request.headers)
            observed["body"] = json.loads(request.content)
            return httpx.Response(200, json={"outputs": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await call_gaia(
                client,
                _settings(),
                "2011111",
                "cube_scheduling_2011111_example",
                "현재 생산량을 알려줘.",
                data='{"conversation_history": []}',
                metadata='{"platform": "CUBE_SCHEDULING"}',
            )
        assert result == {"outputs": []}

    asyncio.run(send())
    assert observed["headers"]["x-gaia-auth-key"] == "test-key"
    assert observed["headers"]["x-gaia-user-id"] == "2011111"
    assert observed["body"]["input_value"] == "현재 생산량을 알려줘."
    assert observed["body"]["tweaks"]["GaiA Input"] == {
        "data": '{"conversation_history": []}',
        "metadata": '{"platform": "CUBE_SCHEDULING"}',
    }


class _Repository:
    def __init__(self) -> None:
        self.finished: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []

    def start_run(self, claimed: ClaimedSchedule, **kwargs: Any) -> None:
        pass

    def claim_is_current(self, claimed: ClaimedSchedule, *, now: datetime | None = None) -> bool:
        return True

    def finish_run(self, run_id: str, **kwargs: Any) -> None:
        self.finished.append({"run_id": run_id, **kwargs})

    def complete_claim(self, claimed: ClaimedSchedule, **kwargs: Any) -> None:
        self.completed.append(dict(kwargs))


class _ClientPlaceholder:
    pass


def test_scheduled_execution_is_fresh_cube_scheduling_dm(monkeypatch) -> None:
    now = datetime(2026, 8, 31, tzinfo=UTC)
    document = _schedule()
    claimed = ClaimedSchedule(document, "lease-token", now, now)
    repository = _Repository()
    worker = SchedulerWorker(
        gaia_settings=_settings(),
        scheduler_settings=_worker_settings(),
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    observed: dict[str, Any] = {}

    async def fake_gaia(*args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["session_id"] = args[3]
        observed["metadata"] = json.loads(kwargs["metadata"])
        return {"not_used": True}

    async def fake_cube(*args: Any, **kwargs: Any) -> None:
        observed["receiver"] = args[2]
        observed["channel"] = args[3]
        observed["message"] = args[4]

    monkeypatch.setattr(worker_module, "call_gaia", fake_gaia)
    monkeypatch.setattr(worker_module, "extract_final_answer", lambda _: "GAIA 최종 답변")
    monkeypatch.setattr(worker_module, "send_cube_message", fake_cube)

    asyncio.run(worker.execute_claimed_schedule(claimed, _ClientPlaceholder()))

    assert observed["metadata"]["platform"] == "CUBE_SCHEDULING"
    assert observed["session_id"].startswith("cube_scheduling_2011111_")
    assert observed["receiver"] == "2011111"
    assert observed["channel"] == ""
    assert observed["message"].startswith(
        "안녕하세요! PTMORE PKG Agent 스케쥴링 실행 결과입니다 😀.\n실행 질문 :"
    )
    assert "처리중" not in observed["message"]
    assert repository.finished[-1]["status"] == "success"


def test_scheduling_gaia_context_is_json_and_has_current_question_only() -> None:
    data, metadata = build_scheduling_gaia_context(
        question="현재 생산량을 알려줘.",
        owner_id="2011111",
        session_id="cube_scheduling_2011111_example",
        schedule_id="SCH-001",
    )
    assert json.loads(data)["conversation_history"] == [
        {"role": "user", "content": "현재 생산량을 알려줘.", "files": []}
    ]
    assert json.loads(metadata)["platform"] == "CUBE_SCHEDULING"
