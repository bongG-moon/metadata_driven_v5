from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi.testclient import TestClient

from cube_scheduler_server.app import create_app
from cube_scheduler_server.config import CubeSchedulerConfig
from cube_scheduler_server.models import ScheduleDefinition, ScheduleSpec, ScheduledQuery
from cube_scheduler_server.scheduling import next_run_after
from cube_scheduler_server.service import CubeSchedulerService
from cube_scheduler_server.store import RuntimeStore, ScheduleSource
from cube_scheduler_server.transport import CubeDeliveryResult, HttpCubeTransport, render_scheduled_query


def _config() -> CubeSchedulerConfig:
    return CubeSchedulerConfig(
        source_mongo_uri="mongodb://source",
        runtime_mongo_uri="mongodb://runtime",
        cube_outbound_url="https://cube.example/legacy/richnotification",
        cube_bot_id="C00001",
        cube_bot_token="secret",
        cube_callback_address="https://scheduler.example/api/cube/callback",
    )


def _query() -> ScheduledQuery:
    return ScheduledQuery(
        employee_id="2000000",
        channel_id="500000000",
        question="현재 WIP을 알려줘",
        schedule_id="schedule:2000000:wip",
        run_id="run-1",
        dedupe_key="schedule:2000000:wip:2026-08-05T00:00:00Z",
    )


def test_schedule_definition_and_next_run_support_interval_and_cron() -> None:
    definition = ScheduleDefinition.model_validate(
        {
            "schedule_id": "schedule:2000000:wip",
            "employee_id": "2000000",
            "channel_id": "500000000",
            "question": "현재 WIP을 알려줘",
            "schedule": {"type": "interval", "minutes": 5, "timezone": "Asia/Seoul"},
        }
    )
    after = datetime(2026, 8, 5, 0, 1, tzinfo=timezone.utc)
    assert next_run_after(definition.schedule, after, anchor_utc=datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)) == datetime(2026, 8, 5, 0, 5, tzinfo=timezone.utc)

    cron = ScheduleSpec(type="cron", expression="0 8 * * 1-5", timezone="Asia/Seoul")
    assert next_run_after(cron, datetime(2026, 8, 7, 23, 30, tzinfo=timezone.utc)) == datetime(2026, 8, 9, 23, 0, tzinfo=timezone.utc)


def test_cube_renderer_uses_bot_identity_user_context_and_query() -> None:
    payload = render_scheduled_query(_query(), _config())
    header = payload["richnotification"]["header"]
    assert header["from"] == "C00001"
    assert header["token"] == "secret"
    assert header["to"] == {"uniquename": ["2000000"], "channelid": ["500000000"]}
    content = payload["richnotification"]["content"][0]
    assert content["body"]["row"][0]["column"][0]["control"]["text"] == ["현재 WIP을 알려줘"]
    assert content["process"]["callbackaddress"] == "https://scheduler.example/api/cube/callback"


class _Response:
    def __init__(self, status_code: int, body: Any = None, content: bytes = b"x") -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body
        self.content = content

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_cube_transport_classifies_success_retry_and_permanent_failure() -> None:
    empty_success = HttpCubeTransport(_config(), _Session(_Response(200, None, b""))).send(_query())
    assert empty_success.success is True

    retry = HttpCubeTransport(_config(), _Session(_Response(429, {"status": "busy"}))).send(_query())
    assert retry.success is False and retry.retryable is True

    timeout = HttpCubeTransport(_config(), _Session(requests.Timeout("late"))).send(_query())
    assert timeout.success is False and timeout.retryable is True

    rejected = HttpCubeTransport(_config(), _Session(_Response(400, {"status": "bad"}))).send(_query())
    assert rejected.success is False and rejected.retryable is False


class _Collection:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def find_one(self, query: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any] | None:
        return self.document if query.get("schedule_id") == self.document.get("schedule_id") else None


class _Source:
    def __init__(self) -> None:
        self.document = {
            "schedule_id": "schedule:2000000:wip",
            "employee_id": "2000000",
            "channel_id": "500000000",
            "question": "현재 WIP을 알려줘",
            "schedule": {"type": "interval", "minutes": 5, "timezone": "Asia/Seoul"},
            "enabled": True,
        }
        self.collection = _Collection(self.document)
        self.closed = False

    def read_all(self) -> list[dict[str, Any]]:
        return [dict(self.document)]

    def ping(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Runtime:
    def __init__(self) -> None:
        self.closed = False
        self.inbound: list[dict[str, Any]] = []

    def ensure_indexes(self) -> None:
        return None

    def ping(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def list_runs(self, schedule_id: str, limit: int) -> list[dict[str, Any]]:
        return [{"schedule_id": schedule_id, "status": "accepted"}]

    def record_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.inbound.append(payload)
        process = payload["richnotificationmessage"].get("process", {})
        return {"message_id": "m1", "process_data": process.get("processdata", ""), "duplicate": False}


class _Transport:
    def send(self, query: ScheduledQuery) -> CubeDeliveryResult:
        return CubeDeliveryResult(True, status_code=200, message="accepted")


def test_fastapi_cube_server_exposes_read_only_schedule_api_and_callback() -> None:
    source = _Source()
    runtime = _Runtime()
    with TestClient(
        create_app(_config(), source=source, runtime=runtime, transport=_Transport(), start_workers=False)
    ) as client:
        assert client.get("/ready").status_code == 200
        schedules = client.get("/api/v1/schedules")
        assert schedules.status_code == 200
        assert schedules.json()[0]["schedule_id"] == "schedule:2000000:wip"
        assert client.post("/api/v1/schedules", json={}).status_code == 405
        callback = client.post(
            "/api/cube/callback",
            json={
                "richnotificationmessage": {
                    "header": {"from": {"uniquename": "2000000"}, "to": {"channelid": ["500000000"]}},
                    "process": {"processdata": "!@#HelloChatBot#@!"},
                }
            },
        )
        assert callback.status_code == 200
        assert callback.json()["status"] == "ignored"
    assert source.closed is True and runtime.closed is True


class _DispatchRuntime:
    def __init__(self) -> None:
        self.completed = False
        self.failed = False

    def claim_outbox(self, worker_id: str) -> dict[str, Any]:
        return {"_id": "o1", "run_id": "run-1", "payload": _query().model_dump(mode="json")}

    def complete_outbox(self, document: dict[str, Any], response: dict[str, Any] | None) -> None:
        self.completed = True

    def fail_outbox(self, document: dict[str, Any], message: str, retryable: bool) -> None:
        self.failed = True


def test_dispatcher_marks_accepted_cube_query_complete() -> None:
    runtime = _DispatchRuntime()
    service = CubeSchedulerService(_Source(), runtime, _Transport(), 15)  # type: ignore[arg-type]
    assert asyncio.run(service.dispatch_once()) is True
    assert runtime.completed is True and runtime.failed is False


class _Cursor:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def sort(self, *_: Any) -> "_Cursor":
        return self

    def limit(self, limit: int) -> "_Cursor":
        self.documents = self.documents[:limit]
        return self

    def __iter__(self):
        return iter(self.documents)


class _SourceCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def find(self, *_: Any) -> _Cursor:
        return _Cursor(list(self.documents))


def test_schedule_source_stops_reconciliation_when_read_limit_is_exceeded() -> None:
    source = ScheduleSource.__new__(ScheduleSource)
    source.config = CubeSchedulerConfig(
        source_mongo_uri="mongodb://source",
        runtime_mongo_uri="mongodb://runtime",
        source_limit=1,
    )
    source.collection = _SourceCollection([{"schedule_id": "one"}, {"schedule_id": "two"}])
    try:
        source.read_all()
    except RuntimeError as exc:
        assert "exceeds CUBE_SCHEDULE_SOURCE_LIMIT" in str(exc)
    else:
        raise AssertionError("source limit saturation must stop reconciliation")


class _OutboxCollection:
    def __init__(self) -> None:
        self.query: dict[str, Any] = {}

    def find_one_and_update(self, query: dict[str, Any], *_: Any, **__: Any) -> None:
        self.query = query
        return None


def test_outbox_claim_recovers_expired_sending_lease() -> None:
    runtime = RuntimeStore.__new__(RuntimeStore)
    runtime.config = _config()
    runtime.outbox = _OutboxCollection()
    runtime.claim_outbox("worker-1", datetime(2026, 8, 5, tzinfo=timezone.utc))
    assert any(branch.get("status") == "sending" for branch in runtime.outbox.query["$or"])
