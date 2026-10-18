"""Bounded, best-effort Phoenix cleanup after successful archive writes.

Never deletes MongoDB rows. Only completed, merge-mode archive scopes qualify.
Threads are started on demand, not at import/WSGI startup; a Mongo lease covers
multiple web processes. An interrupted/ambiguous delete requires review rather
than an automatic destructive retry. No credentials or questions are logged.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, time, timedelta, timezone
from time import monotonic
from uuid import uuid4

from phoenix_usage import authorization_header, graphql_url, resolve_project_id
from runtime_settings import get_setting

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
LOCK_ID = "__portal_phoenix_cleanup_lock__"
MAX_RECORDS = 30
TIME_BUDGET = 45
REQUEST_TIMEOUT = 8
LEASE_SECONDS = 300
COOLDOWN_SECONDS = 300
_gate = threading.Lock()
_next_launch = 0.0

TRACE_QUERY = """query($project: ID!, $trace: ID!) {
 node(id: $project) { ... on Project { id trace(traceId: $trace) {
 id traceId startTime endTime
 } } }
}"""
DELETE_QUERY = """mutation($ids: [ID!]!) { deleteTraces(traceIds: $ids) { __typename } }"""


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def cutoff_for(now):
    # Today + yesterday + the preceding day, not a rolling 72-hour window.
    return datetime.combine(now.astimezone(KST).date() - timedelta(days=2), time.min, KST)


def archive_confirmed(row, marker, projects, cutoff):
    try:
        return bool(
            row and row.get("record_type") == "usage_history_record"
            and row.get("source_project") in projects
            and re.fullmatch(r"[0-9a-fA-F]{32}", row.get("trace_id", ""))
            and int(row["trace_id"], 16) != 0
            and row.get("archive_refresh_id")
            and marker and marker.get("archive_mode") == "merge"
            and marker.get("record_type") == "usage_history_scope_marker"
            and marker.get("source_project") == row["source_project"]
            and marker.get("usage_date") == row["usage_date"]
            and parse_time(row["query_time"]).astimezone(KST).date().isoformat() == row["usage_date"]
            and parse_time(row["query_time"]) < cutoff
            and parse_time(row["query_time"]) <= parse_time(row["refresh_started_at"])
            <= parse_time(marker["refresh_started_at"])
        )
    except (ValueError, KeyError, TypeError):
        return False


def trace_confirmed(trace, row, cutoff):
    try:
        start, end = parse_time(trace["startTime"]), parse_time(trace["endTime"])
        return bool(trace["id"] and trace["traceId"].lower() == row["trace_id"].lower()
                    and start <= parse_time(row["query_time"]) <= end < cutoff
                    and end <= parse_time(row["refresh_started_at"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


class PhoenixClient:
    def __init__(self, config):
        import requests
        self.config = config
        self.session = requests.Session()
        self.session.headers["Authorization"] = authorization_header(config.api_key)
        self.projects = {}

    def query(self, query, variables=None):
        response = self.session.post(graphql_url(self.config.endpoint),
            json={"query": query, "variables": variables or {}}, timeout=REQUEST_TIMEOUT,
            allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Phoenix HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise RuntimeError("Phoenix GraphQL unsupported or denied")
        return body["data"]

    def check_support(self):
        data = self.query("{ __schema { mutationType { fields { name args { name } } } } }")
        fields = (data["__schema"].get("mutationType") or {}).get("fields", [])
        if not any(f["name"] == "deleteTraces" and any(a["name"] == "traceIds" for a in f["args"]) for f in fields):
            raise RuntimeError("Phoenix deletion unsupported")

    def trace(self, project, trace_id):
        if project not in self.projects:
            self.projects[project] = resolve_project_id(project, endpoint=self.config.endpoint,
                api_key=self.config.api_key, timeout_seconds=REQUEST_TIMEOUT, http_session=self.session)
        project_id = self.projects[project]
        node = self.query(TRACE_QUERY, {"project": project_id, "trace": trace_id}).get("node")
        if not node or node.get("id") != project_id or "trace" not in node:
            raise RuntimeError("Project-scoped trace lookup failed")
        return node["trace"]

    def delete(self, relay_id):
        if not isinstance(self.query(DELETE_QUERY, {"ids": [relay_id]}).get("deleteTraces"), dict):
            raise RuntimeError("Unconfirmed delete response")

    def close(self):
        self.session.close()


def process_batch(collection, phoenix, projects, *, now=utcnow, clock=monotonic):
    """May update cleanup bookkeeping, never remove/replace archive data."""
    from pymongo import ReturnDocument
    from pymongo.errors import DuplicateKeyError
    owner, started = str(uuid4()), clock()
    current = now()
    cutoff = cutoff_for(current)
    summary = {"deleted": 0, "missing": 0, "skipped": 0, "uncertain": 0}
    try:
        lease = collection.find_one_and_update(
            {"_id": LOCK_ID, "lease_until": {"$lte": current}},
            {"$set": {"owner": owner, "lease_until": current + timedelta(seconds=LEASE_SECONDS),
                       "record_type": "phoenix_cleanup_control", "started_at": current}},
            upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        return {"busy": True}
    if not lease or lease.get("owner") != owner:
        return {"busy": True}

    def owns_lease():
        # Leave enough room for the delete and verification requests before expiry.
        return collection.find_one({"_id": LOCK_ID, "owner": owner,
            "lease_until": {"$gt": now() + timedelta(seconds=2 * REQUEST_TIMEOUT + 5)}})

    def marker_for(row):
        return collection.find_one({"record_type": "usage_history_scope_marker",
            "source_project": row.get("source_project"), "usage_date": row.get("usage_date")})

    def record_status(row, status):
        # Separate durable state survives archive.refresh replacing the data row.
        collection.update_one({"_id": "__phoenix_cleanup_state__:" + str(row["_id"])},
            {"$set": {"record_type": "phoenix_cleanup_control", "status": status,
                      "checked_at": now()}}, upsert=True)
        result = collection.update_one({"_id": row["_id"], "refresh_started_at": row["refresh_started_at"],
                                        "archive_refresh_id": row["archive_refresh_id"]},
            {"$set": {"phoenix_cleanup_status": status, "phoenix_cleanup_checked_at": now()}})
        if result.matched_count != 1:
            raise RuntimeError("Archive changed during cleanup")

    try:
        phoenix.check_support()
        candidates = collection.find({"record_type": "usage_history_record",
            "source_project": {"$in": list(projects)}, "usage_date": {"$lt": cutoff.date().isoformat()},
            "phoenix_cleanup_status": {"$nin": ["deleted", "missing", "uncertain", "requested"]},
            "$or": [{"phoenix_cleanup_checked_at": {"$exists": False}},
                     {"phoenix_cleanup_checked_at": {"$lt": current - timedelta(minutes=15)}}]},
            {"_id": 1, "record_type": 1, "source_project": 1, "usage_date": 1, "trace_id": 1,
             "query_time": 1, "refresh_started_at": 1, "archive_refresh_id": 1}).limit(MAX_RECORDS)
        for candidate in candidates:
            if clock() - started >= TIME_BUDGET or not owns_lease():
                break
            row = collection.find_one({"_id": candidate["_id"]})
            previous = collection.find_one({"_id": "__phoenix_cleanup_state__:" + str(candidate["_id"])})
            if previous and previous.get("status") in ("deleted", "missing", "uncertain", "requested"):
                collection.update_one({"_id": candidate["_id"]}, {"$set": {
                    "phoenix_cleanup_status": previous["status"], "phoenix_cleanup_checked_at": now()}})
                continue
            if not archive_confirmed(row, marker_for(candidate), projects, cutoff):
                summary["skipped"] += 1
                # Incomplete rows are never deleted; avoid blocking later candidates.
                collection.update_one({"_id": candidate["_id"]},
                    {"$set": {"phoenix_cleanup_checked_at": now()}})
                continue
            trace = phoenix.trace(row["source_project"], row["trace_id"])
            if trace is None:
                record_status(row, "missing")
                summary["missing"] += 1
                continue
            if not trace_confirmed(trace, row, cutoff):
                record_status(row, "skipped")
                summary["skipped"] += 1
                continue
            # Check both durable archive and Phoenix again immediately before deletion.
            if collection.find_one({"_id": row["_id"]}) != row or not archive_confirmed(row, marker_for(row), projects, cutoff):
                raise RuntimeError("Archive changed before deletion")
            latest = phoenix.trace(row["source_project"], row["trace_id"])
            if latest != trace or not trace_confirmed(latest, row, cutoff):
                record_status(row, "skipped")
                summary["skipped"] += 1
                continue
            if clock() - started >= TIME_BUDGET or not owns_lease():
                break
            record_status(row, "requested")  # crash/timeout: never blindly retry
            try:
                phoenix.delete(trace["id"])
                if phoenix.trace(row["source_project"], row["trace_id"]) is not None:
                    raise RuntimeError("Trace still present after deletion")
                record_status(row, "deleted")
                summary["deleted"] += 1
            except Exception:
                record_status(row, "uncertain")
                summary["uncertain"] += 1
                raise
        return summary
    finally:
        collection.update_one({"_id": LOCK_ID, "owner": owner}, {"$set": {
            "lease_until": now() + timedelta(seconds=COOLDOWN_SECONDS),
            "finished_at": now(), "summary": summary}})


def _background(phoenix_config, archive_config):
    global _next_launch
    client = phoenix = None
    try:
        from pymongo import MongoClient
        client = MongoClient(archive_config.uri, serverSelectionTimeoutMS=5000,
                             connectTimeoutMS=5000, socketTimeoutMS=10000, tz_aware=True)
        phoenix = PhoenixClient(phoenix_config)
        result = process_batch(client[archive_config.database][archive_config.collection],
                               phoenix, phoenix_config.projects)
        logger.info("Phoenix archived-trace cleanup: %s", result)
    except Exception as exc:
        logger.warning("Phoenix archived-trace cleanup stopped (%s); archive retained", type(exc).__name__)
    finally:
        try:
            if phoenix is not None:
                phoenix.close()
            if client is not None:
                client.close()
        finally:
            _next_launch = monotonic() + COOLDOWN_SECONDS
            _gate.release()


def schedule_cleanup(phoenix_config, archive_config):
    """No network or DB work on the request thread. No queue can accumulate."""
    if get_setting("PTMORE_PHOENIX_ARCHIVE_CLEANUP_ENABLED", "true").lower() != "true":
        return False
    if not phoenix_config.is_configured or not archive_config.is_configured:
        return False
    if monotonic() < _next_launch or not _gate.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_background, args=(phoenix_config, archive_config),
                         name="phoenix-archive-cleanup", daemon=True).start()
    except Exception:
        _gate.release()
        raise
    return True
