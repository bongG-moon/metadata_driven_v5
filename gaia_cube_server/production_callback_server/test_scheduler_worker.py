"""Offline tests for the separate MongoDB scheduler worker.

No test uses a real MongoDB, GAIA key, CUBE bot, or HTTP endpoint.  The small
recording repositories below verify the worker contract at its boundaries.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest
import scheduler_worker as worker_module
from app import GaiaRequestError, Settings, SettingsError
from scheduler_worker import (
    ClaimedSchedule,
    MongoScheduleRepository,
    SchedulerSettings,
    SchedulerWorker,
    build_scheduled_result_message,
    calculate_next_run_at,
)


UTC = timezone.utc


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _schedule(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "_id": "mongo-1",
        "id": "SCH-001",
        "owner": "2011111",
        "question": "DA 공정 생산 현황을 알려줘.",
        "status": "active",
        "repeat": "매일",
        "time": "09:30",
        "timezone": "Asia/Seoul",
        "next_run_at": "2026-08-31T00:00:00+00:00",
    }
    document.update(overrides)
    return document


def test_next_run_rules_are_interpreted_in_kst() -> None:
    # 2026-08-31 is Monday; UTC midnight is KST 09:00.
    daily = calculate_next_run_at(_schedule(), now=_utc(2026, 8, 31, 0, 0))
    assert daily == _utc(2026, 8, 31, 0, 30)

    weekdays = calculate_next_run_at(
        _schedule(repeat="평일", time="09:30"),
        now=_utc(2026, 9, 4, 1, 0),  # Friday KST 10:00
    )
    assert weekdays == _utc(2026, 9, 7, 0, 30)  # Monday KST 09:30

    monthly = calculate_next_run_at(
        _schedule(repeat="매월", day_of_month=31, time="09:30"),
        now=_utc(2026, 9, 1, 0, 0),
    )
    assert monthly == _utc(2026, 9, 30, 0, 30)

    assert calculate_next_run_at(
        _schedule(repeat="한 번만"), now=_utc(2026, 8, 31, 0, 0)
    ) is None


def test_interval_uses_kst_start_and_end_window() -> None:
    schedule = _schedule(
        repeat="interval",
        interval_minutes=10,
        start_time="09:00",
        end_time="09:30",
    )
    assert calculate_next_run_at(schedule, now=_utc(2026, 8, 31, 0, 4)) == _utc(
        2026, 8, 31, 0, 10
    )
    # Once today's final 09:30 slot has passed, the next day begins at 09:00.
    assert calculate_next_run_at(schedule, now=_utc(2026, 8, 31, 0, 31)) == _utc(
        2026, 9, 1, 0, 0
    )


class _ClaimCollection:
    """Enough PyMongo surface to prove ``find_one_and_update`` is used."""

    def __init__(self, claimed_document: dict[str, Any]) -> None:
        self.claimed_document = claimed_document
        self.calls: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
        self.claimed = False

    def find_one_and_update(self, query: dict[str, Any], update: dict[str, Any], *, return_document: Any):
        self.calls.append((query, update, return_document))
        if self.claimed:
            return None
        self.claimed = True
        result = dict(self.claimed_document)
        result.update(update["$set"])
        return result


class _NoopCollection:
    pass


def test_due_claim_uses_one_atomic_mongo_operation() -> None:
    document = _schedule()
    schedules = _ClaimCollection(document)
    repository = MongoScheduleRepository(schedules, _NoopCollection())  # type: ignore[arg-type]
    now = _utc(2026, 8, 31, 0, 1)

    first = repository.claim(document, now=now, lease_seconds=90)
    second = repository.claim(document, now=now, lease_seconds=90)

    assert first is not None
    assert second is None
    assert len(schedules.calls) == 2
    claim_filter, update, _ = schedules.calls[0]
    assert claim_filter["$and"][0] == {"_id": "mongo-1"}
    assert "scheduler_claim_token" in update["$set"]
    assert "scheduler_claim_until" in update["$set"]


class _LeaseCheckCollection:
    """Evaluate the worker's Date conversion condition without MongoDB."""

    def __init__(self, lease_until: str) -> None:
        self.lease_until = lease_until
        self.last_query: dict[str, Any] | None = None

    def find_one(self, query: dict[str, Any], projection: dict[str, int]) -> dict[str, str] | None:
        self.last_query = query
        expression = query["$and"][-1]["$expr"]["$gt"]
        checked_at = expression[1]
        normalized = self.lease_until.replace("Z", "+00:00")
        lease_until = datetime.fromisoformat(normalized)
        return {"_id": "mongo-1"} if lease_until > checked_at else None


def test_delivery_requires_unexpired_lease_and_handles_legacy_z() -> None:
    now = _utc(2026, 8, 31, 0, 0)
    claimed = ClaimedSchedule(_schedule(), "lease-token", now, now)

    future_collection = _LeaseCheckCollection("2026-08-31T00:00:01Z")
    future_repository = MongoScheduleRepository(future_collection, _NoopCollection())  # type: ignore[arg-type]
    assert future_repository.claim_is_current(claimed, now=now)
    assert "$convert" in future_collection.last_query["$and"][-1]["$expr"]["$gt"][0]

    # Equality is expired: a legacy Z suffix must not compare as newer than
    # the same +00:00 instant through accidental lexical ordering.
    expired_collection = _LeaseCheckCollection("2026-08-31T00:00:00Z")
    expired_repository = MongoScheduleRepository(expired_collection, _NoopCollection())  # type: ignore[arg-type]
    assert not expired_repository.claim_is_current(claimed, now=now)


class _RecordingRepository:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.finished: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []

    def start_run(self, claimed: ClaimedSchedule, **kwargs: Any) -> None:
        self.started.append(dict(kwargs))

    def finish_run(self, run_id: str, **kwargs: Any) -> None:
        self.finished.append({"run_id": run_id, **kwargs})

    def complete_claim(self, claimed: ClaimedSchedule, **kwargs: Any) -> None:
        self.completed.append(dict(kwargs))

    def claim_is_current(self, claimed: ClaimedSchedule, *, now: datetime | None = None) -> bool:
        return True


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


def test_scheduled_execution_uses_fresh_session_and_no_processing_notice(monkeypatch) -> None:
    repository = _RecordingRepository()
    now = _utc(2026, 8, 31, 0, 0)
    document = _schedule()
    claimed = ClaimedSchedule(
        document=document,
        claim_token="lease-token",
        claimed_at=now,
        scheduled_for=_utc(2026, 8, 31, 0, 0),
    )
    worker = SchedulerWorker(
        gaia_settings=_gaia_settings(),
        scheduler_settings=_scheduler_settings(),
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    observed: dict[str, Any] = {}

    async def fake_call_gaia(*args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["session_id"] = args[3]
        observed["metadata"] = json.loads(kwargs["metadata"])
        return {"result": "unused"}

    async def fake_send_cube_message(*args: Any, **kwargs: Any) -> None:
        observed["receiver_id"] = args[2]
        observed["channel_id"] = args[3]
        observed["message"] = args[4]

    monkeypatch.setattr(worker_module, "call_gaia", fake_call_gaia)
    monkeypatch.setattr(worker_module, "extract_final_answer", lambda response: "GAIA 최종 답변")
    monkeypatch.setattr(worker_module, "send_cube_message", fake_send_cube_message)

    asyncio.run(worker.execute_claimed_schedule(claimed, httpx_placeholder()))

    assert observed["metadata"]["platform"] == "CUBE_SCHEDULING"
    assert observed["metadata"]["user_id"] == "2011111"
    assert observed["session_id"].startswith("cube_scheduling_2011111_")
    assert observed["channel_id"] == ""
    assert observed["message"] == build_scheduled_result_message(
        document["question"], "GAIA 최종 답변"
    )
    assert "처리중" not in observed["message"]
    assert repository.finished[-1]["status"] == "success"
    assert repository.finished[-1]["error_category"] is None


def test_gaia_failure_still_sends_one_prefixed_safe_fallback(monkeypatch) -> None:
    repository = _RecordingRepository()
    now = _utc(2026, 8, 31, 0, 0)
    document = _schedule()
    claimed = ClaimedSchedule(document, "lease-token", now, now)
    worker = SchedulerWorker(
        gaia_settings=_gaia_settings(),
        scheduler_settings=_scheduler_settings(),
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    sent: list[str] = []

    async def fail_gaia(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise GaiaRequestError("forbidden")

    async def record_send(*args: Any, **kwargs: Any) -> None:
        sent.append(args[4])

    monkeypatch.setattr(worker_module, "call_gaia", fail_gaia)
    monkeypatch.setattr(worker_module, "send_cube_message", record_send)

    asyncio.run(worker.execute_claimed_schedule(claimed, httpx_placeholder()))

    assert len(sent) == 1
    assert sent[0].startswith("안녕하세요! PTMORE PKG Agent 스케쥴링 실행 결과입니다 😀.\n실행 질문 :")
    assert repository.finished[-1]["status"] == "failed"
    assert repository.finished[-1]["error_category"] == "gaia_forbidden"


def test_portal_pause_or_edit_before_delivery_skips_old_answer(monkeypatch) -> None:
    class _CancelledRepository(_RecordingRepository):
        def claim_is_current(
            self, claimed: ClaimedSchedule, *, now: datetime | None = None
        ) -> bool:
            return False

    repository = _CancelledRepository()
    now = _utc(2026, 8, 31, 0, 0)
    document = _schedule()
    claimed = ClaimedSchedule(document, "lease-token", now, now)
    worker = SchedulerWorker(
        gaia_settings=_gaia_settings(),
        scheduler_settings=_scheduler_settings(),
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    sent: list[str] = []

    async def answer_gaia(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"result": "unused"}

    async def record_send(*args: Any, **kwargs: Any) -> None:
        sent.append(args[4])

    monkeypatch.setattr(worker_module, "call_gaia", answer_gaia)
    monkeypatch.setattr(worker_module, "extract_final_answer", lambda response: "오래된 답변")
    monkeypatch.setattr(worker_module, "send_cube_message", record_send)

    asyncio.run(worker.execute_claimed_schedule(claimed, httpx_placeholder()))

    assert sent == []
    assert repository.finished[-1]["status"] == "cancelled"
    assert repository.finished[-1]["error_category"] == "schedule_cancelled"
    # The Portal invalidated the lease, so Worker never overwrites the user's
    # newer schedule fields while finalizing the historical run record.
    assert repository.completed == []


def test_malformed_next_run_after_claim_is_recorded_deactivated_and_released(monkeypatch) -> None:
    class _InvalidClaimRepository(_RecordingRepository):
        def __init__(self, document: dict[str, Any], now: datetime) -> None:
            super().__init__()
            self.document = document
            self.now = now

        def due_candidates(self, now: datetime, limit: int) -> list[dict[str, Any]]:
            return [self.document]

        def claim(
            self, document: dict[str, Any], *, now: datetime, lease_seconds: float
        ) -> ClaimedSchedule:
            # Model the exact post-find_one_and_update state: the lease exists
            # but its source timestamp cannot be parsed.
            return ClaimedSchedule(
                document=document,
                claim_token="lease-token",
                claimed_at=now,
                scheduled_for=None,
                scheduled_for_value="not-an-iso-timestamp",
            )

    now = _utc(2026, 8, 31, 0, 0)
    document = _schedule(owner_id="2011111", next_run_at="not-an-iso-timestamp")
    repository = _InvalidClaimRepository(document, now)
    worker = SchedulerWorker(
        gaia_settings=_gaia_settings(),
        scheduler_settings=_scheduler_settings(),
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    sent: list[str] = []

    async def fail_if_called(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("GAIA must not run for malformed next_run_at")

    async def record_send(*args: Any, **kwargs: Any) -> None:
        sent.append(args[4])

    monkeypatch.setattr(worker_module, "call_gaia", fail_if_called)
    monkeypatch.setattr(worker_module, "send_cube_message", record_send)

    assert asyncio.run(worker.run_once(httpx_placeholder())) == 1
    assert len(repository.started) == 1
    assert repository.finished[-1]["status"] == "failed"
    assert repository.finished[-1]["error_category"] == "invalid_schedule"
    assert repository.completed[-1]["deactivate"] is True
    assert repository.completed[-1]["next_run_at"] is None
    assert sent[0].startswith("안녕하세요! PTMORE PKG Agent 스케쥴링 실행 결과입니다 😀.")
    assert "스케줄 설정을 확인할 수 없습니다." in sent[0]


def test_worker_rejects_lease_shorter_than_gaia_cube_timeout_budget() -> None:
    too_short = replace(_scheduler_settings(), lease_seconds=89)
    with pytest.raises(SettingsError, match="PTMORE_SCHEDULER_LEASE_SECONDS"):
        too_short.validate_lease_duration(_gaia_settings())


class httpx_placeholder:
    """The monkeypatched GAIA/CUBE functions do not inspect this object."""
