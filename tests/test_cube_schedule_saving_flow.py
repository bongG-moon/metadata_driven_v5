from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components" / "cube_schedule_saving_flow"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, COMPONENT_ROOT / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cube_schedule_normalizer_builds_interval_and_stable_id() -> None:
    module = _load("cube_schedule_normalizer_test", "01_cube_schedule_normalizer.py")
    result = module.normalize_schedule(
        "5분마다 WIP 질의",
        '{"employee_id":"2000000","channel_id":"500000000","question":"현재 WIP을 알려줘","schedule":{"type":"interval","minutes":5,"timezone":"Asia/Seoul"},"enabled":true}',
    )
    assert result["ready_to_save"] is True
    document = result["schedule_document"]
    assert document["schema_version"] == "cube.schedule.v1"
    assert document["schedule_id"].startswith("schedule:2000000:")
    assert "next_run_at" not in document


def test_cube_schedule_normalizer_rejects_invalid_cron_and_missing_employee() -> None:
    module = _load("cube_schedule_normalizer_invalid_test", "01_cube_schedule_normalizer.py")
    result = module.normalize_schedule(
        "잘못된 요청",
        '{"employee_id":"","question":"질의","schedule":{"type":"cron","expression":"99 * * * *"}}',
    )
    assert result["ready_to_save"] is False
    assert {item["type"] for item in result["errors"]} == {"invalid_cron", "missing_employee_id"}


class _WriteResult:
    def __init__(self) -> None:
        self.upserted_id = "x"


class _Target:
    def __init__(self) -> None:
        self.document: dict[str, Any] | None = None

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        return None

    def find_one(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return None

    def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool) -> _WriteResult:
        self.document = {**update["$setOnInsert"], **update["$set"]}
        return _WriteResult()


class _Database:
    def __init__(self, target: _Target) -> None:
        self.target = target

    def __getitem__(self, name: str) -> _Target:
        return self.target


class _MongoClient:
    target = _Target()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    def __getitem__(self, name: str) -> _Database:
        return _Database(self.target)

    def close(self) -> None:
        self.closed = True


def test_cube_schedule_writer_dry_run_and_versioned_source_upsert() -> None:
    module = _load("cube_schedule_writer_test", "02_cube_schedule_mongodb_writer.py")
    payload = {
        "ready_to_save": True,
        "schedule_document": {
            "schema_version": "cube.schedule.v1",
            "schedule_id": "schedule:2000000:wip",
            "employee_id": "2000000",
            "channel_id": "500000000",
            "question": "현재 WIP을 알려줘",
            "schedule": {"type": "interval", "minutes": 5, "timezone": "Asia/Seoul"},
            "enabled": True,
        },
        "errors": [],
    }
    dry_run = module.write_schedule(payload, dry_run=True)
    assert dry_run["status"] == "dry_run" and dry_run["would_save_count"] == 1

    saved = module.write_schedule(
        payload,
        mongo_uri="mongodb://authoring",
        dry_run=False,
        mongo_client_cls=_MongoClient,
    )
    assert saved["status"] == "saved"
    assert saved["schedule_document"]["version"] == 1
    assert "next_run_at" not in (_MongoClient.target.document or {})
