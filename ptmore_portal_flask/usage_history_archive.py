"""MongoDB archive for Phoenix-derived PTMORE Portal usage history.

Phoenix keeps a short retention window.  This module persists only the small
``GaiA Input`` projection that the Portal dashboard needs, never raw span
attributes or credentials.  A successful Phoenix refresh replaces the
configured project's records for every queried KST date, so corrected or
late-arriving traces overwrite prior archive rows while dates with no records
are also accurately represented.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from time import monotonic, sleep
from uuid import uuid4


DEFAULT_USAGE_HISTORY_COLLECTION = "portal_usage_history"
DEFAULT_TIMEOUT_MS = 5_000
# One sentinel document in the archive collection serializes full refreshes
# across Uvicorn processes/instances.  It has no source_project/usage_date and
# is therefore naturally excluded from dashboard reads and stale-row deletes.
_SYNC_LOCK_DOCUMENT_ID = "__portal_usage_history_sync_lock__"
_SYNC_LOCK_LEASE_SECONDS = 120
_SYNC_LOCK_WAIT_SECONDS = 15.0
_SYNC_LOCK_RETRY_SECONDS = 0.05
_SCOPE_MARKER_RECORD_TYPE = "usage_history_scope_marker"
_SCOPE_MARKER_DOCUMENT_PREFIX = "__portal_usage_history_scope__"


class UsageHistoryArchiveError(RuntimeError):
    """Raised when the long-term usage archive cannot be read or synchronized."""


@dataclass(frozen=True)
class UsageHistoryArchiveConfig:
    """Non-secret connection settings for the Portal-owned usage archive."""

    uri: str = ""
    database: str = ""
    collection: str = DEFAULT_USAGE_HISTORY_COLLECTION

    @property
    def configuration_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.uri.strip():
            errors.append("MONGODB_URI")
        if not self.database.strip():
            errors.append("MONGODB_DATABASE")
        if not _valid_collection_name(self.collection):
            errors.append("PTMORE_USAGE_HISTORY_COLLECTION")
        return tuple(errors)

    @property
    def is_configured(self) -> bool:
        return not self.configuration_errors

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "UsageHistoryArchiveConfig":
        values = os.environ if environ is None else environ
        return cls(
            uri=str(values.get("MONGODB_URI", "") or "").strip(),
            database=str(values.get("MONGODB_DATABASE", "") or "").strip(),
            collection=(
                str(
                    values.get(
                        "PTMORE_USAGE_HISTORY_COLLECTION",
                        DEFAULT_USAGE_HISTORY_COLLECTION,
                    )
                    or ""
                ).strip()
                or DEFAULT_USAGE_HISTORY_COLLECTION
            ),
        )


@dataclass(frozen=True)
class UsageHistoryArchiveRefresh:
    """Safe refresh summary returned to the Portal route and status response."""

    refresh_id: str
    start_date: str
    end_date: str
    project_count: int
    upserted_count: int
    removed_count: int


class UsageHistoryArchive(Protocol):
    """Minimal archive boundary; tests can substitute an in-memory store."""

    def refresh(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        start_day: date,
        end_day: date,
        source_projects: Iterable[str],
        refresh_started_at: str | None = None,
    ) -> UsageHistoryArchiveRefresh:
        ...

    def read_records(
        self,
        *,
        start_day: date | None = None,
        end_day: date | None = None,
    ) -> list[dict[str, str]]:
        ...

    def covered_scopes(
        self,
        *,
        start_day: date,
        end_day: date,
        source_projects: Iterable[str] | None = None,
    ) -> set[tuple[str, date]]:
        """Return project/KST-day scopes with a completed archive marker."""

        ...

    def close(self) -> None:
        ...


class MongoUsageHistoryArchive:
    """Store compact usage records in one Portal-owned MongoDB collection."""

    def __init__(
        self,
        config: UsageHistoryArchiveConfig,
        *,
        mongo_client_factory: Callable[..., Any] | None = None,
        protected_collections: Iterable[str] = (),
    ) -> None:
        if not config.is_configured:
            raise UsageHistoryArchiveError("사용 이력 보관 MongoDB 설정을 확인해 주세요.")
        if collection_name_conflicts(config.collection, protected_collections):
            raise UsageHistoryArchiveError(
                "사용 이력 보관 컬렉션은 Portal 또는 메타데이터 컬렉션과 같을 수 없습니다."
            )

        try:
            if mongo_client_factory is None:
                from pymongo import MongoClient
                from pymongo.errors import DuplicateKeyError, PyMongoError

                mongo_client_factory = MongoClient
            else:
                try:
                    from pymongo.errors import DuplicateKeyError, PyMongoError
                except ImportError:  # pragma: no cover - injected test client only
                    DuplicateKeyError = Exception
                    PyMongoError = Exception
        except ImportError as exc:
            raise UsageHistoryArchiveError(
                "사용 이력 보관을 위해 pymongo 패키지가 필요합니다."
            ) from exc

        self._mongo_error = PyMongoError
        self._mongo_duplicate_error = DuplicateKeyError
        self._collection_name = config.collection
        self._client: Any | None = None
        try:
            self._client = mongo_client_factory(
                config.uri,
                serverSelectionTimeoutMS=3_000,
                connectTimeoutMS=3_000,
                socketTimeoutMS=DEFAULT_TIMEOUT_MS,
            )
            self._collection = self._client[config.database][config.collection]
        except PyMongoError as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 보관소를 초기화할 수 없습니다."
            ) from exc
        self._ensure_indexes()

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 보관소에 연결할 수 없습니다."
            ) from exc

    def _ensure_indexes(self) -> None:
        """Create only query-supporting indexes; insufficient DDL rights are safe."""

        try:
            self._collection.create_index(
                [("source_project", 1), ("usage_date", 1)],
                name="portal_usage_source_day_lookup",
            )
            self._collection.create_index(
                [("usage_date", 1), ("query_time", 1)],
                name="portal_usage_date_time_lookup",
            )
        except self._mongo_error:
            # A runtime account can still have document read/write rights even
            # when index DDL is reserved for the database administrator.
            return None

    def refresh(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        start_day: date,
        end_day: date,
        source_projects: Iterable[str],
        refresh_started_at: str | None = None,
    ) -> UsageHistoryArchiveRefresh:
        """Synchronize one fully fetched Phoenix period into the archive.

        Callers must supply records only after all configured Phoenix projects
        have completed successfully.  The method writes/upserts all current
        rows first; stale rows are removed only after those writes finish, so a
        failed write never turns a temporary error into an empty date range.
        """

        if start_day > end_day:
            raise ValueError("start_day must not be later than end_day")
        projects = _normalise_projects(source_projects)
        if not projects:
            raise UsageHistoryArchiveError("조회한 Phoenix 프로젝트 정보가 없습니다.")

        refresh_id = str(uuid4())
        # The Portal captures this generation *before* it asks Phoenix for
        # pages.  A slow older fetch must not overwrite a newer complete fetch
        # merely because it reached MongoDB later.  Standalone callers without
        # that upstream value still get a safe generation at archive entry.
        sync_started_at = _text(refresh_started_at) or _utc_now_iso()
        lock_acquired = False
        completed = False
        try:
            lock_document = self._acquire_sync_lock(
                owner=refresh_id,
                sync_started_at=sync_started_at,
            )
            lock_acquired = True
            completed_generation = _text(
                lock_document.get("last_completed_refresh_started_at")
            )
            if completed_generation and completed_generation >= sync_started_at:
                # This request began before (or at the same instant as) a
                # fully completed refresh.  Do not bring back its older view.
                return UsageHistoryArchiveRefresh(
                    refresh_id=refresh_id,
                    start_date=start_day.isoformat(),
                    end_date=end_day.isoformat(),
                    project_count=len(projects),
                    upserted_count=0,
                    removed_count=0,
                )

            documents = _archive_documents(
                records,
                start_day=start_day,
                end_day=end_day,
                refresh_id=refresh_id,
                refreshed_at=sync_started_at,
                allowed_projects=projects,
            )

            # The sentinel lease serializes the whole refresh scope.  The
            # per-document condition remains a second safety fence for rows
            # written by earlier versions of this adapter or an unusually
            # delayed operation that loses its lease between document writes.
            written_count = 0
            for document in documents:
                self._renew_sync_lock(owner=refresh_id)
                document_written = self._write_document_if_not_newer(
                    document,
                    refresh_started_at=sync_started_at,
                )
                if not document_written:
                    # A later generation owns this trace.  Do not publish a
                    # scope marker for an incomplete/older view and do not
                    # run stale deletion for it.
                    raise UsageHistoryArchiveError(
                        "더 최신 사용 이력 동기화 결과가 있어 현재 결과를 저장하지 않았습니다."
                    )
                written_count += 1

            # Mark every successfully queried (project, KST date) scope,
            # including dates where Phoenix returned zero rows.  The marker is
            # written only after every current data row has succeeded.  Reads
            # use it as a durable generation fence if an expired old lease
            # later inserts a stale row after the newer refresh deleted it.
            for marker in _scope_marker_documents(
                projects,
                start_day=start_day,
                end_day=end_day,
                refresh_id=refresh_id,
                refresh_started_at=sync_started_at,
            ):
                self._renew_sync_lock(owner=refresh_id)
                if not self._write_scope_marker_if_not_newer(
                    marker,
                    refresh_started_at=sync_started_at,
                ):
                    raise UsageHistoryArchiveError(
                        "더 최신 사용 이력 동기화 결과가 있어 현재 범위를 확정하지 않았습니다."
                    )

            self._renew_sync_lock(owner=refresh_id)
            stale_filter = {
                "source_project": {"$in": list(projects)},
                "usage_date": {
                    "$gte": start_day.isoformat(),
                    "$lte": end_day.isoformat(),
                },
                "archive_refresh_id": {"$ne": refresh_id},
                "$or": [
                    {"refresh_started_at": {"$lt": sync_started_at}},
                    {"refresh_started_at": {"$exists": False}},
                ],
                "record_type": {"$ne": _SCOPE_MARKER_RECORD_TYPE},
            }
            removal = self._run(lambda: self._collection.delete_many(stale_filter))
            self._complete_and_release_sync_lock(
                owner=refresh_id,
                completed_generation=sync_started_at,
            )
            completed = True
            return UsageHistoryArchiveRefresh(
                refresh_id=refresh_id,
                start_date=start_day.isoformat(),
                end_date=end_day.isoformat(),
                project_count=len(projects),
                upserted_count=written_count,
                removed_count=int(getattr(removal, "deleted_count", 0) or 0),
            )
        finally:
            if lock_acquired and not completed:
                self._release_sync_lock(owner=refresh_id)

    def _acquire_sync_lock(
        self,
        *,
        owner: str,
        sync_started_at: str,
    ) -> Mapping[str, Any]:
        """Acquire the archive-wide MongoDB lease or fail without stale writes.

        A single collection-wide lease is intentionally conservative: dashboard
        refreshes are small and infrequent, while serializing them avoids a
        cross-process race where an older zero/partial result can recreate a
        record the newer result removed.  The completed generation is retained
        in the sentinel after release as a durable fencing marker.
        """

        deadline = monotonic() + _SYNC_LOCK_WAIT_SECONDS
        while True:
            now = _utc_now_iso()
            lease_until = _utc_after_seconds_iso(_SYNC_LOCK_LEASE_SECONDS)
            selector = {
                "_id": _SYNC_LOCK_DOCUMENT_ID,
                "$or": [
                    {"lease_until": {"$lt": now}},
                    {"lease_until": {"$exists": False}},
                    {"lock_owner": owner},
                ],
            }
            try:
                result = self._collection.update_one(
                    selector,
                    {
                        "$set": {
                            "record_type": "usage_history_sync_lock",
                            "lock_owner": owner,
                            "lease_until": lease_until,
                            "lock_acquired_at": now,
                            "refresh_started_at": sync_started_at,
                        }
                    },
                    upsert=False,
                )
            except self._mongo_error as exc:
                raise UsageHistoryArchiveError(
                    "MongoDB 사용 이력 동기화 잠금을 확인할 수 없습니다."
                ) from exc

            if bool(getattr(result, "matched_count", 0)):
                try:
                    document = self._collection.find_one(
                        {"_id": _SYNC_LOCK_DOCUMENT_ID, "lock_owner": owner}
                    )
                except self._mongo_error as exc:
                    raise UsageHistoryArchiveError(
                        "MongoDB 사용 이력 동기화 잠금을 확인할 수 없습니다."
                    ) from exc
                if isinstance(document, Mapping):
                    return dict(document)
            else:
                try:
                    self._collection.insert_one(
                        {
                            "_id": _SYNC_LOCK_DOCUMENT_ID,
                            "record_type": "usage_history_sync_lock",
                            "lock_owner": owner,
                            "lease_until": lease_until,
                            "lock_acquired_at": now,
                            "refresh_started_at": sync_started_at,
                            "last_completed_refresh_started_at": "",
                        }
                    )
                    return {
                        "_id": _SYNC_LOCK_DOCUMENT_ID,
                        "lock_owner": owner,
                        "lease_until": lease_until,
                        "last_completed_refresh_started_at": "",
                    }
                except self._mongo_duplicate_error:
                    # Another process created the singleton between the
                    # conditional update and initial insert.  Retry safely.
                    pass
                except self._mongo_error as exc:
                    raise UsageHistoryArchiveError(
                        "MongoDB 사용 이력 동기화 잠금을 확인할 수 없습니다."
                    ) from exc

            if monotonic() >= deadline:
                raise UsageHistoryArchiveError(
                    "다른 사용 이력 동기화 작업이 진행 중입니다. 잠시 후 다시 시도해 주세요."
                )
            sleep(_SYNC_LOCK_RETRY_SECONDS)

    def _renew_sync_lock(self, *, owner: str) -> None:
        """Keep the lease alive immediately before each mutating operation."""

        now = _utc_now_iso()
        try:
            result = self._collection.update_one(
                {
                    "_id": _SYNC_LOCK_DOCUMENT_ID,
                    "lock_owner": owner,
                    "lease_until": {"$gte": now},
                },
                {"$set": {"lease_until": _utc_after_seconds_iso(_SYNC_LOCK_LEASE_SECONDS)}},
                upsert=False,
            )
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 동기화 잠금을 갱신할 수 없습니다."
            ) from exc
        if not bool(getattr(result, "matched_count", 0)):
            raise UsageHistoryArchiveError(
                "사용 이력 동기화 잠금을 잃었습니다. 다시 시도해 주세요."
            )

    def _complete_and_release_sync_lock(
        self,
        *,
        owner: str,
        completed_generation: str,
    ) -> None:
        """Publish the completed generation and let the next refresh proceed."""

        try:
            result = self._collection.update_one(
                {"_id": _SYNC_LOCK_DOCUMENT_ID, "lock_owner": owner},
                {
                    "$set": {
                        "last_completed_refresh_started_at": completed_generation,
                        "lock_owner": "",
                        "lease_until": _utc_now_iso(),
                        "lock_released_at": _utc_now_iso(),
                    }
                },
                upsert=False,
            )
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 동기화 완료 상태를 저장할 수 없습니다."
            ) from exc
        if not bool(getattr(result, "matched_count", 0)):
            raise UsageHistoryArchiveError(
                "사용 이력 동기화 잠금을 잃었습니다. 다시 시도해 주세요."
            )

    def _release_sync_lock(self, *, owner: str) -> None:
        """Best-effort cleanup after a failed/stale refresh without masking it."""

        try:
            self._collection.update_one(
                {"_id": _SYNC_LOCK_DOCUMENT_ID, "lock_owner": owner},
                {
                    "$set": {
                        "lock_owner": "",
                        "lease_until": _utc_now_iso(),
                        "lock_released_at": _utc_now_iso(),
                    }
                },
                upsert=False,
            )
        except self._mongo_error:
            return None

    def _write_document_if_not_newer(
        self,
        document: Mapping[str, Any],
        *,
        refresh_started_at: str,
    ) -> bool:
        """Atomically write one row unless another refresh started later.

        ``replace_one(..., upsert=True)`` cannot express this safely: a
        non-matching conditional filter can attempt a duplicate ``_id`` insert.
        We therefore try a conditional replacement, then a direct insert only
        when no row matched.  A duplicate insert means another process won the
        race and is an expected, non-error outcome.
        """

        selector = {
            "_id": document["_id"],
            "$or": [
                {"refresh_started_at": {"$lt": refresh_started_at}},
                {"refresh_started_at": {"$exists": False}},
                {"archive_refresh_id": document["archive_refresh_id"]},
            ],
        }
        try:
            result = self._collection.replace_one(selector, dict(document), upsert=False)
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 보관소에 연결할 수 없습니다."
            ) from exc
        if bool(getattr(result, "matched_count", 0)):
            return True

        try:
            self._collection.insert_one(dict(document))
            return True
        except self._mongo_duplicate_error:
            # A concurrent refresh inserted the same identity after our
            # conditional lookup.  Its fencing value decides the winner.
            return False
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 보관소에 연결할 수 없습니다."
            ) from exc

    def _write_scope_marker_if_not_newer(
        self,
        marker: Mapping[str, Any],
        *,
        refresh_started_at: str,
    ) -> bool:
        """Atomically publish a project/day generation without downgrading it."""

        selector = {
            "_id": marker["_id"],
            "$or": [
                {"refresh_started_at": {"$lt": refresh_started_at}},
                {"refresh_started_at": {"$exists": False}},
                {"refresh_started_at": refresh_started_at},
            ],
        }
        try:
            result = self._collection.replace_one(selector, dict(marker), upsert=False)
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 범위 상태를 저장할 수 없습니다."
            ) from exc
        if bool(getattr(result, "matched_count", 0)):
            return True

        try:
            self._collection.insert_one(dict(marker))
            return True
        except self._mongo_duplicate_error:
            # A newer generation created this marker after the conditional
            # lookup.  The caller must not delete or expose its older scope.
            return False
        except self._mongo_error as exc:
            raise UsageHistoryArchiveError(
                "MongoDB 사용 이력 범위 상태를 저장할 수 없습니다."
            ) from exc

    def read_records(
        self,
        *,
        start_day: date | None = None,
        end_day: date | None = None,
    ) -> list[dict[str, str]]:
        """Return safe dashboard fields, optionally bounded by inclusive KST dates."""

        if (start_day is None) != (end_day is None):
            raise ValueError("start_day and end_day must be supplied together")
        if start_day is not None and end_day is not None and start_day > end_day:
            raise ValueError("start_day must not be later than end_day")

        query: dict[str, Any] = {}
        if start_day is not None and end_day is not None:
            query["usage_date"] = {
                "$gte": start_day.isoformat(),
                "$lte": end_day.isoformat(),
            }
        projection = {
            "_id": 0,
            "record_type": 1,
            "source_project": 1,
            "trace_id": 1,
            "usage_date": 1,
            "query_time": 1,
            "platform": 1,
            "user_id": 1,
            "question": 1,
            "refresh_started_at": 1,
        }
        documents = self._run(
            lambda: list(
                self._collection.find(query, projection).sort(
                    [("query_time", 1), ("source_project", 1), ("trace_id", 1)]
                )
            )
        )
        # A scope marker is written only after a complete Phoenix result has
        # been stored for that project/day.  It makes the marker generation
        # authoritative for reads.  This is deliberately a read-time fence in
        # addition to the Mongo lease: if an old worker's insert resumes after
        # its lease expired and a newer empty refresh completed, the old row is
        # harmlessly invisible even though it can no longer be deleted by the
        # newer refresh.
        scope_generations: dict[tuple[str, str], str] = {}
        data_documents: list[Mapping[str, Any]] = []
        for document in documents:
            if not isinstance(document, Mapping):
                continue
            project = _text(document.get("source_project"))
            usage_date = _text(document.get("usage_date"))
            if document.get("record_type") == _SCOPE_MARKER_RECORD_TYPE:
                generation = _text(document.get("refresh_started_at"))
                if project and usage_date and generation:
                    scope = (project, usage_date)
                    if generation > scope_generations.get(scope, ""):
                        scope_generations[scope] = generation
                continue
            data_documents.append(document)

        records: list[dict[str, str]] = []
        for document in data_documents:
            record = {
                "query_time": _text(document.get("query_time")),
                "platform": _text(document.get("platform")),
                "user_id": _text(document.get("user_id")),
                "question": _text(document.get("question")),
                "project": _text(document.get("source_project")),
                "trace_id": _text(document.get("trace_id")),
                "date": _text(document.get("usage_date")),
            }
            # Pre-marker (legacy) rows remain readable until the first
            # successful synchronized result establishes a generation for the
            # scope.  Once a marker exists, only that exact generation is
            # considered part of the complete project/day snapshot.
            marker_generation = scope_generations.get(
                (record["project"], record["date"])
            )
            if marker_generation and _text(document.get("refresh_started_at")) != marker_generation:
                continue
            if record["query_time"] and record["project"]:
                records.append(record)
        return records

    def covered_scopes(
        self,
        *,
        start_day: date,
        end_day: date,
        source_projects: Iterable[str] | None = None,
    ) -> set[tuple[str, date]]:
        """Return completed project/KST-day scopes for an inclusive date range.

        A scope marker is published only after Phoenix returned successfully for
        the complete project/day scope, including a legitimate zero-record
        result.  This read deliberately ignores archive data rows: legacy rows
        or a partial write are not evidence that Phoenix has completed that
        scope.  It also neither takes nor alters the refresh lease, so it can
        safely run alongside the existing refresh/read fencing protocol.
        """

        if start_day > end_day:
            raise ValueError("start_day must not be later than end_day")

        projects = (
            _normalise_projects(source_projects)
            if source_projects is not None
            else None
        )
        # An explicitly empty project selection must not accidentally broaden
        # to every project in the shared archive collection.
        if projects == ():
            return set()

        query: dict[str, Any] = {
            "record_type": _SCOPE_MARKER_RECORD_TYPE,
            "usage_date": {
                "$gte": start_day.isoformat(),
                "$lte": end_day.isoformat(),
            },
        }
        if projects is not None:
            query["source_project"] = {"$in": list(projects)}

        # Keep marker internals inside this adapter.  In particular, callers
        # receive only project/date identities and never raw marker documents,
        # refresh IDs, or archive timestamps.
        documents = self._run(
            lambda: list(
                self._collection.find(
                    query,
                    {
                        "_id": 0,
                        "source_project": 1,
                        "usage_date": 1,
                        "refresh_started_at": 1,
                    },
                )
            )
        )

        covered: set[tuple[str, date]] = set()
        for document in documents:
            if not isinstance(document, Mapping):
                continue
            project = _text(document.get("source_project"))
            usage_date = _text(document.get("usage_date"))
            # Match the read-time fence: an incomplete/malformed marker must
            # not make a scope look safe to skip.
            if not project or not usage_date or not _text(document.get("refresh_started_at")):
                continue
            try:
                scope_day = date.fromisoformat(usage_date)
            except ValueError:
                continue
            if not start_day <= scope_day <= end_day:
                continue
            if projects is not None and project not in projects:
                continue
            covered.add((project, scope_day))
        return covered

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception:  # pragma: no cover - cleanup must never mask success
            return None


def usage_record_identity(record: Mapping[str, Any]) -> str:
    """Return a deterministic archive key without exposing it to Portal users."""

    project = _text(record.get("project") or record.get("source_project"))
    trace_id = _text(record.get("trace_id"))
    if trace_id:
        identity = {"project": project, "trace_id": trace_id}
    else:
        identity = {
            "project": project,
            "query_time": _text(record.get("query_time") or record.get("occurred_at")),
            "platform": _text(record.get("platform") or record.get("channel")),
            "user_id": _text(record.get("user_id") or record.get("employee_id")),
            "question": _text(record.get("question")),
        }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _archive_documents(
    records: Iterable[Mapping[str, Any]],
    *,
    start_day: date,
    end_day: date,
    refresh_id: str,
    refreshed_at: str,
    allowed_projects: tuple[str, ...],
) -> list[dict[str, str]]:
    documents_by_id: dict[str, dict[str, str]] = {}
    allowed = set(allowed_projects)
    for record in records:
        if not isinstance(record, Mapping):
            continue
        project = _text(record.get("project") or record.get("source_project"))
        query_time = _text(record.get("query_time") or record.get("occurred_at"))
        usage_date = _date_from_record(record, query_time)
        if not project or project not in allowed or not usage_date:
            continue
        parsed_day = date.fromisoformat(usage_date)
        if parsed_day < start_day or parsed_day > end_day:
            continue
        document = {
            "_id": f"phoenix:{usage_record_identity(record)}",
            "record_type": "usage_history_record",
            "source_project": project,
            "trace_id": _text(record.get("trace_id")),
            "usage_date": usage_date,
            "query_time": query_time,
            "platform": _text(record.get("platform") or record.get("channel")),
            "user_id": _text(record.get("user_id") or record.get("employee_id")),
            "question": _text(record.get("question")),
            "archive_refresh_id": refresh_id,
            "archived_at": refreshed_at,
            "last_refreshed_at": refreshed_at,
            "refresh_started_at": refreshed_at,
        }
        # Phoenix data has already been deduplicated by trace.  This second
        # guard also protects the archive if a caller accidentally repeats a
        # normalized record in one refresh.
        documents_by_id[document["_id"]] = document
    return list(documents_by_id.values())


def _scope_marker_documents(
    projects: Iterable[str],
    *,
    start_day: date,
    end_day: date,
    refresh_id: str,
    refresh_started_at: str,
) -> list[dict[str, str]]:
    """Build one durable completion marker per queried project/KST day.

    A marker intentionally exists for zero-result days.  That lets a newer
    empty Phoenix result fence out a late insert from an older request even if
    the older process resumes after the newer stale-row cleanup has completed.
    """

    markers: list[dict[str, str]] = []
    current_day = start_day
    while current_day <= end_day:
        usage_date = current_day.isoformat()
        for project in projects:
            marker_identity = json.dumps(
                {"project": project, "usage_date": usage_date},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            marker_digest = hashlib.sha256(marker_identity.encode("utf-8")).hexdigest()
            markers.append(
                {
                    "_id": f"{_SCOPE_MARKER_DOCUMENT_PREFIX}:{marker_digest}",
                    "record_type": _SCOPE_MARKER_RECORD_TYPE,
                    "source_project": project,
                    "usage_date": usage_date,
                    "archive_refresh_id": refresh_id,
                    "refresh_started_at": refresh_started_at,
                    "archived_at": refresh_started_at,
                }
            )
        current_day += timedelta(days=1)
    return markers


def _normalise_projects(values: Iterable[str]) -> tuple[str, ...]:
    projects: list[str] = []
    for value in values:
        project = _text(value)
        if project and project not in projects:
            projects.append(project)
    return tuple(projects)


def _date_from_record(record: Mapping[str, Any], query_time: str) -> str:
    candidate = _text(record.get("date")) or query_time[:10]
    try:
        return date.fromisoformat(candidate[:10]).isoformat()
    except ValueError:
        return ""


def _utc_now_iso() -> str:
    """Return one lexicographically sortable UTC timestamp for Mongo filters."""

    return datetime.now(timezone.utc).isoformat()


def _utc_after_seconds_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _valid_collection_name(value: str) -> bool:
    name = _text(value)
    if not name or len(name) > 120 or name.startswith("system."):
        return False
    return all(character.isalnum() or character in {"_", "-", "."} for character in name)


def collection_name_conflicts(
    collection: str,
    protected_collections: Iterable[str],
) -> bool:
    """Return whether an archive collection aliases a protected collection.

    The check intentionally lives in this standalone module as a second
    boundary.  The Portal also validates its known settings/schedule/metadata
    collection names before opening the archive, but callers outside FastAPI
    cannot accidentally bypass the collision guard.
    """

    candidate = _text(collection)
    if not candidate:
        return False
    return candidate in {
        protected
        for value in protected_collections
        if (protected := _text(value))
    }
