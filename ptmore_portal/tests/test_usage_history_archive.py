from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import usage_history_archive as archive_module


class _Result:
    def __init__(
        self,
        *,
        deleted_count: int = 0,
        matched_count: int = 0,
    ) -> None:
        self.deleted_count = deleted_count
        self.matched_count = matched_count


class _Cursor(list):
    def sort(self, fields):
        for field, direction in reversed(list(fields)):
            reverse = direction < 0
            super().sort(key=lambda item: str(item.get(field) or ""), reverse=reverse)
        return self


class _Collection:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []
        self.indexes: list[tuple] = []

    def create_index(self, fields, *, name: str):
        self.indexes.append((tuple(fields), name))

    def replace_one(self, selector, document, *, upsert: bool):
        assert upsert is False
        key = str(selector["_id"])
        if key.startswith("phoenix:"):
            self.events.append("replace")
        existing = self.documents.get(key)
        if existing is None or not _matches(existing, selector):
            return _Result(matched_count=0)
        self.documents[key] = copy.deepcopy(dict(document))
        return _Result(matched_count=1)

    def insert_one(self, document):
        key = str(document["_id"])
        if key.startswith("phoenix:"):
            self.events.append("insert")
        if key in self.documents:
            # The production adapter only needs an exception compatible with
            # the error class it imports from pymongo.  ``DuplicateKeyError``
            # is available in the focused test runtime as well.
            from pymongo.errors import DuplicateKeyError

            raise DuplicateKeyError("duplicate _id")
        self.documents[key] = copy.deepcopy(dict(document))

    def update_one(self, selector, update, *, upsert: bool):
        assert upsert is False
        key = str(selector["_id"])
        document = self.documents.get(key)
        if document is None or not _matches(document, selector):
            return _Result(matched_count=0)
        for field, value in dict(update.get("$set") or {}).items():
            document[field] = copy.deepcopy(value)
        return _Result(matched_count=1)

    def find_one(self, selector):
        for document in self.documents.values():
            if _matches(document, selector):
                return copy.deepcopy(document)
        return None

    def delete_many(self, selector):
        self.events.append("delete")
        removed = 0
        for key, document in list(self.documents.items()):
            if _matches(document, selector):
                del self.documents[key]
                removed += 1
        return _Result(deleted_count=removed)

    def find(self, selector, projection):
        rows = []
        for document in self.documents.values():
            if not _matches(document, selector):
                continue
            rows.append(
                {
                    field: copy.deepcopy(document[field])
                    for field, included in projection.items()
                    if included and field in document
                }
            )
        return _Cursor(rows)


class _Client:
    def __init__(self) -> None:
        self.collection = _Collection()
        self.closed = False

    def __getitem__(self, _database: str):
        return _Database(self.collection)

    def close(self) -> None:
        self.closed = True


class _Database:
    def __init__(self, collection: _Collection) -> None:
        self.collection = collection

    def __getitem__(self, _collection: str):
        return self.collection


def _matches(document: dict[str, Any], selector: dict[str, Any]) -> bool:
    for field, expected in selector.items():
        if field == "$or":
            if not any(_matches(document, candidate) for candidate in expected):
                return False
            continue
        actual = document.get(field)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$gte" in expected and not (actual is not None and actual >= expected["$gte"]):
                return False
            if "$lte" in expected and not (actual is not None and actual <= expected["$lte"]):
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$lt" in expected and not (actual is not None and actual < expected["$lt"]):
                return False
            if "$exists" in expected and bool(actual is not None) != bool(expected["$exists"]):
                return False
        elif actual != expected:
            return False
    return True


def _record(
    trace_id: str,
    *,
    project: str = "project-a",
    query_time: str = "2026-08-10T09:00:00+09:00",
    question: str = "DA 생산량 알려줘",
) -> dict[str, str]:
    return {
        "trace_id": trace_id,
        "project": project,
        "query_time": query_time,
        "platform": "CUBE",
        "user_id": "2069026",
        "question": question,
    }


def _archive() -> tuple[archive_module.MongoUsageHistoryArchive, _Client]:
    client = _Client()
    config = archive_module.UsageHistoryArchiveConfig(
        uri="mongodb://unit-test",
        database="ptmore",
        collection="portal_usage_history",
    )
    return (
        archive_module.MongoUsageHistoryArchive(
            config,
            mongo_client_factory=lambda *_args, **_kwargs: client,
        ),
        client,
    )


def test_config_uses_portal_mongo_values_and_safe_collection_name() -> None:
    config = archive_module.UsageHistoryArchiveConfig.from_env(
        {
            "MONGODB_URI": "mongodb://example",
            "MONGODB_DATABASE": "datagov_test",
            "PTMORE_USAGE_HISTORY_COLLECTION": "agent_v4_usage_history",
        }
    )

    assert config.is_configured is True
    assert config.collection == "agent_v4_usage_history"
    assert config.configuration_errors == ()


def test_archive_identity_keeps_same_trace_id_in_different_projects_separate() -> None:
    first = archive_module.usage_record_identity(_record("shared", project="project-a"))
    second = archive_module.usage_record_identity(_record("shared", project="project-b"))

    assert first != second


def test_refresh_upserts_then_removes_stale_rows_for_successfully_queried_dates() -> None:
    archive, client = _archive()
    start_day = date(2026, 8, 10)
    end_day = date(2026, 8, 11)
    first = archive.refresh(
        [
            _record("trace-1", query_time="2026-08-10T09:00:00+09:00"),
            _record("trace-2", query_time="2026-08-11T09:00:00+09:00"),
            _record("trace-other", project="project-b", query_time="2026-08-11T10:00:00+09:00"),
        ],
        start_day=start_day,
        end_day=end_day,
        source_projects=("project-a", "project-b"),
    )

    assert first.upserted_count == 3
    assert first.removed_count == 0
    assert client.collection.events[-1] == "delete"
    assert client.collection.events[:6] == [
        "replace",
        "insert",
        "replace",
        "insert",
        "replace",
        "insert",
    ]

    # A later, fully successful Phoenix query returns no project-a record for
    # 8/11.  The stale project-a row is removed, while project-b is untouched
    # because it was not part of this successfully queried project scope.
    client.collection.events.clear()
    second = archive.refresh(
        [_record("trace-1", question="정정된 DA 생산량", query_time="2026-08-10T09:00:00+09:00")],
        start_day=start_day,
        end_day=end_day,
        source_projects=("project-a",),
    )

    rows = archive.read_records(start_day=start_day, end_day=end_day)
    assert second.upserted_count == 1
    assert second.removed_count == 1
    assert client.collection.events == ["replace", "delete"]
    assert [(row["project"], row["trace_id"], row["question"]) for row in rows] == [
        ("project-a", "trace-1", "정정된 DA 생산량"),
        ("project-b", "trace-other", "DA 생산량 알려줘"),
    ]
    assert all("archive_refresh_id" not in row for row in rows)


def test_covered_scopes_uses_completed_markers_for_zero_record_days_and_filters_projects() -> None:
    archive, _client = _archive()
    start_day = date(2026, 8, 10)
    end_day = date(2026, 8, 11)
    archive.refresh(
        [_record("trace-a", project="project-a", query_time="2026-08-10T09:00:00+09:00")],
        start_day=start_day,
        end_day=end_day,
        source_projects=("project-a", "project-b"),
        refresh_started_at="2026-08-10T10:00:00+00:00",
    )

    # Every project/day queried by the completed Phoenix refresh is covered,
    # including project-a on 8/11 and both project-b dates, which had no rows.
    assert archive.covered_scopes(
        start_day=start_day,
        end_day=end_day,
        source_projects=None,
    ) == {
        ("project-a", date(2026, 8, 10)),
        ("project-a", date(2026, 8, 11)),
        ("project-b", date(2026, 8, 10)),
        ("project-b", date(2026, 8, 11)),
    }
    assert archive.covered_scopes(
        start_day=start_day,
        end_day=end_day,
        source_projects=("project-a",),
    ) == {
        ("project-a", date(2026, 8, 10)),
        ("project-a", date(2026, 8, 11)),
    }
    assert archive.covered_scopes(
        start_day=start_day,
        end_day=end_day,
        source_projects=(),
    ) == set()

    # Scope markers are an internal cache-completeness mechanism, never rows
    # shown through the public dashboard-record adapter.
    assert archive.read_records(start_day=start_day, end_day=end_day) == [
        {
            "query_time": "2026-08-10T09:00:00+09:00",
            "platform": "CUBE",
            "user_id": "2069026",
            "question": "DA 생산량 알려줘",
            "project": "project-a",
            "trace_id": "trace-a",
            "date": "2026-08-10",
        }
    ]


def test_covered_scopes_does_not_treat_data_rows_as_completed_scope_markers() -> None:
    archive, client = _archive()
    day = date(2026, 8, 10)
    # A pre-marker/partial data row can be readable for legacy compatibility,
    # but it is not safe evidence that Phoenix completed this project/day.
    client.collection.documents["phoenix:data-without-marker"] = {
        "_id": "phoenix:data-without-marker",
        "source_project": "project-a",
        "trace_id": "data-without-marker",
        "usage_date": day.isoformat(),
        "query_time": "2026-08-10T09:00:00+09:00",
        "platform": "CUBE",
        "user_id": "2069026",
        "question": "마커 없는 레거시 행",
        "refresh_started_at": "2026-08-10T10:00:00+00:00",
    }

    assert archive.covered_scopes(
        start_day=day,
        end_day=day,
        source_projects=("project-a",),
    ) == set()
    assert [row["trace_id"] for row in archive.read_records(start_day=day, end_day=day)] == [
        "data-without-marker"
    ]


def test_archive_fallback_identity_is_deterministic_without_trace_id() -> None:
    first = _record("")
    second = _record("")
    third = _record("", question="다른 질문")

    assert archive_module.usage_record_identity(first) == archive_module.usage_record_identity(second)
    assert archive_module.usage_record_identity(first) != archive_module.usage_record_identity(third)


def test_older_refresh_does_not_delete_a_newer_concurrent_refresh_row() -> None:
    archive, client = _archive()
    collection = client.collection
    original_delete_many = collection.delete_many

    def interleaved_delete(selector):
        # Emulate a newer dashboard request that completed its upsert after the
        # current request began but before its stale-row cleanup executes.
        collection.documents["phoenix:newer-refresh"] = {
            "_id": "phoenix:newer-refresh",
            "source_project": "project-a",
            "trace_id": "newer-trace",
            "usage_date": "2026-08-10",
            "query_time": "2026-08-10T09:30:00+09:00",
            "platform": "CUBE",
            "user_id": "2071044",
            "question": "더 최신 Phoenix 결과",
            "archive_refresh_id": "newer-request",
            "last_refreshed_at": "9999-12-31T23:59:59+00:00",
            "refresh_started_at": "9999-12-31T23:59:59+00:00",
        }
        return original_delete_many(selector)

    collection.delete_many = interleaved_delete
    archive.refresh(
        [_record("older-trace")],
        start_day=date(2026, 8, 10),
        end_day=date(2026, 8, 10),
        source_projects=("project-a",),
    )

    assert "phoenix:newer-refresh" in collection.documents


def test_older_refresh_cannot_overwrite_newer_same_trace_document() -> None:
    """The document-level fence must work across independent Portal workers."""

    archive, client = _archive()
    newer_record = _record("same-trace", question="더 최신 Phoenix 결과")
    document_id = f"phoenix:{archive_module.usage_record_identity(newer_record)}"
    client.collection.documents[document_id] = {
        "_id": document_id,
        "source_project": "project-a",
        "trace_id": "same-trace",
        "usage_date": "2026-08-10",
        "query_time": "2026-08-10T09:00:00+09:00",
        "platform": "CUBE",
        "user_id": "2069026",
        "question": "더 최신 Phoenix 결과",
        "archive_refresh_id": "newer-request",
        "refresh_started_at": "9999-12-31T23:59:59+00:00",
    }

    with pytest.raises(archive_module.UsageHistoryArchiveError):
        archive.refresh(
            [_record("same-trace", question="더 오래된 Phoenix 결과")],
            start_day=date(2026, 8, 10),
            end_day=date(2026, 8, 10),
            source_projects=("project-a",),
        )

    assert client.collection.documents[document_id]["question"] == "더 최신 Phoenix 결과"
    # Conditional replacement fails, the follow-up insert sees the existing
    # ``_id``, and the old refresh leaves the newer row untouched.
    assert client.collection.events[:2] == ["replace", "insert"]


def test_older_refresh_cannot_resurrect_trace_removed_by_newer_empty_result() -> None:
    """A completed scope marker blocks old inserts after a newer stale delete."""

    archive, client = _archive()
    record = _record("removed-by-newer", question="오래된 결과에만 있던 질문")
    document_id = f"phoenix:{archive_module.usage_record_identity(record)}"
    # Emulate a newer refresh that contained no trace X and therefore already
    # deleted it.  The durable sentinel still records that newer generation.
    client.collection.documents["__portal_usage_history_sync_lock__"] = {
        "_id": "__portal_usage_history_sync_lock__",
        "record_type": "usage_history_sync_lock",
        "lock_owner": "",
        "lease_until": "1970-01-01T00:00:00+00:00",
        "last_completed_refresh_started_at": "9999-12-31T23:59:59+00:00",
    }

    result = archive.refresh(
        [record],
        start_day=date(2026, 8, 10),
        end_day=date(2026, 8, 10),
        source_projects=("project-a",),
    )

    assert result.upserted_count == 0
    assert result.removed_count == 0
    assert document_id not in client.collection.documents
    assert "replace" not in client.collection.events


def test_late_phoenix_completion_uses_fetch_start_generation_not_mongo_arrival() -> None:
    """An earlier request that reaches Mongo later cannot replace newer data."""

    archive, client = _archive()
    day = date(2026, 8, 10)
    # B began later but completed its Phoenix fetch first with no trace X.
    newer = archive.refresh(
        [],
        start_day=day,
        end_day=day,
        source_projects=("project-a",),
        refresh_started_at="2026-08-10T10:00:00+00:00",
    )
    # A began earlier, then its slow Phoenix request completed after B.  It
    # must be skipped even though this ``refresh`` call arrives second.
    older = archive.refresh(
        [_record("late-old-trace", question="오래된 Phoenix 응답")],
        start_day=day,
        end_day=day,
        source_projects=("project-a",),
        refresh_started_at="2026-08-10T09:00:00+00:00",
    )

    document_id = f"phoenix:{archive_module.usage_record_identity(_record('late-old-trace'))}"
    assert newer.upserted_count == 0
    assert older.upserted_count == 0
    assert document_id not in client.collection.documents


def test_expired_old_lease_late_insert_is_hidden_by_newer_empty_scope_marker() -> None:
    """A late insert cannot resurrect a record after a newer empty scope wins."""

    archive_a, client = _archive()
    config = archive_module.UsageHistoryArchiveConfig(
        uri="mongodb://unit-test",
        database="ptmore",
        collection="portal_usage_history",
    )
    archive_b = archive_module.MongoUsageHistoryArchive(
        config,
        mongo_client_factory=lambda *_args, **_kwargs: client,
    )
    collection = client.collection
    original_insert_one = collection.insert_one
    record = _record("late-after-expiry", question="이전 조회에만 있던 질문")
    document_id = f"phoenix:{archive_module.usage_record_identity(record)}"
    day = date(2026, 8, 10)
    newer_refreshes = []
    triggered = False

    def interleaved_insert(document):
        nonlocal triggered
        if str(document.get("_id", "")).startswith("phoenix:") and not triggered:
            triggered = True
            # A has already passed its conditional replace, but its lease now
            # expires.  B takes the lock, sees no X, and completes an empty
            # project/day scope before A's direct insert resumes.
            collection.documents[archive_module._SYNC_LOCK_DOCUMENT_ID][
                "lease_until"
            ] = "1970-01-01T00:00:00+00:00"
            newer_refreshes.append(
                archive_b.refresh(
                    [],
                    start_day=day,
                    end_day=day,
                    source_projects=("project-a",),
                    refresh_started_at="2026-08-10T10:00:00+00:00",
                )
            )
        return original_insert_one(document)

    collection.insert_one = interleaved_insert
    with pytest.raises(archive_module.UsageHistoryArchiveError, match="잠금을 잃었습니다"):
        archive_a.refresh(
            [record],
            start_day=day,
            end_day=day,
            source_projects=("project-a",),
            refresh_started_at="2026-08-10T09:00:00+00:00",
        )

    assert triggered is True
    assert newer_refreshes[0].upserted_count == 0
    # The old process did physically finish its direct insert after B removed
    # stale rows.  The durable project/day generation marker nevertheless
    # keeps that old row out of Portal reads and CSV exports.
    assert document_id in collection.documents
    assert archive_a.read_records(start_day=day, end_day=day) == []


def test_scope_marker_rejects_an_older_generation() -> None:
    """A project/day completion marker itself cannot be downgraded."""

    archive, client = _archive()
    day = date(2026, 8, 10)
    archive.refresh(
        [],
        start_day=day,
        end_day=day,
        source_projects=("project-a",),
        refresh_started_at="2026-08-10T10:00:00+00:00",
    )
    old_marker = archive_module._scope_marker_documents(
        ("project-a",),
        start_day=day,
        end_day=day,
        refresh_id="old-refresh",
        refresh_started_at="2026-08-10T09:00:00+00:00",
    )[0]

    assert archive._write_scope_marker_if_not_newer(
        old_marker,
        refresh_started_at="2026-08-10T09:00:00+00:00",
    ) is False
    stored_marker = client.collection.documents[old_marker["_id"]]
    assert stored_marker["refresh_started_at"] == "2026-08-10T10:00:00+00:00"
