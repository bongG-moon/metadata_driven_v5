"""Offline only; no real Phoenix deletion or MongoDB connection."""
import copy
import importlib.util
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import phoenix_archive_cleanup as m

NOW = datetime(2026, 10, 18, 12, tzinfo=m.KST)


def row():
    return {"_id": "phoenix:one", "record_type": "usage_history_record", "source_project": "project",
            "usage_date": "2026-10-14", "query_time": "2026-10-14T10:00:00+09:00",
            "trace_id": "a" * 32, "refresh_started_at": "2026-10-15T12:00:00+09:00",
            "archive_refresh_id": "generation", "question": "retained", "user_id": "1234567"}


def marker():
    return {"_id": "marker", "record_type": "usage_history_scope_marker", "archive_mode": "merge",
            "source_project": "project", "usage_date": "2026-10-14",
            "refresh_started_at": "2026-10-16T12:00:00+09:00"}


def matches(doc, query):
    for key, value in query.items():
        if key == "$or":
            if not any(matches(doc, q) for q in value): return False
            continue
        got = doc.get(key)
        if not isinstance(value, dict):
            if got != value: return False
            continue
        for op, expected in value.items():
            if op == "$in" and got not in expected: return False
            if op == "$nin" and got in expected: return False
            if op == "$exists" and (key in doc) != expected: return False
            if op == "$lt" and (got is None or not got < expected): return False
            if op == "$lte" and (got is None or not got <= expected): return False
            if op == "$gt" and (got is None or not got > expected): return False
    return True


class Cursor(list):
    def limit(self, count): return self[:count]


class Collection:
    def __init__(self): self.docs = {d["_id"]: d for d in (row(), marker())}
    def find_one(self, query):
        return next((copy.deepcopy(d) for d in self.docs.values() if matches(d, query)), None)
    def find(self, query, projection):
        return Cursor(copy.deepcopy(d) for d in self.docs.values() if matches(d, query))
    def update_one(self, query, update, upsert=False):
        found = self.find_one(query)
        if found is None and not upsert: return SimpleNamespace(matched_count=0)
        found = found or {"_id": query["_id"]}
        found.update(copy.deepcopy(update["$set"]))
        self.docs[found["_id"]] = found
        return SimpleNamespace(matched_count=1)
    def find_one_and_update(self, query, update, **kwargs):
        from pymongo.errors import DuplicateKeyError
        if query["_id"] in self.docs and not self.find_one(query): raise DuplicateKeyError("busy")
        self.update_one(query, update, upsert=True)
        return self.find_one({"_id": query["_id"]})


class Phoenix:
    def __init__(self):
        self.value = {"id": "relay", "traceId": "a" * 32, "startTime": "2026-10-14T09:59:00+09:00",
                      "endTime": "2026-10-14T10:01:00+09:00"}
        self.deleted, self.fail, self.reads = [], False, 0
    def check_support(self): pass
    def trace(self, project, trace):
        assert project == "project" and trace == "a" * 32
        self.reads += 1
        return None if self.deleted else copy.deepcopy(self.value)
    def delete(self, relay):
        if self.fail: raise TimeoutError("uncertain")
        self.deleted.append(relay)


def run(c, p, **kwargs): return m.process_batch(c, p, ("project",), now=lambda: NOW, **kwargs)


def test_confirmed_delete_keeps_mongo_history():
    c, p = Collection(), Phoenix()
    assert run(c, p)["deleted"] == 1
    assert p.deleted == ["relay"]
    assert c.docs["phoenix:one"]["question"] == "retained"
    assert c.docs["phoenix:one"]["phoenix_cleanup_status"] == "deleted"
    assert run(c, p) == {"busy": True}


@pytest.mark.parametrize("change", ["no_marker", "old_marker", "snapshot", "no_id", "wrong_project", "today", "cutoff"])
def test_unconfirmed_or_recent_archive_never_deleted(change):
    c, p = Collection(), Phoenix()
    if change == "no_marker": c.docs.pop("marker")
    if change == "old_marker": c.docs["marker"]["refresh_started_at"] = "2026-10-13T00:00:00Z"
    if change == "snapshot": c.docs["marker"].pop("archive_mode")
    if change == "no_id": c.docs["phoenix:one"]["trace_id"] = ""
    if change == "wrong_project": c.docs["phoenix:one"]["source_project"] = "other"
    if change in ("today", "cutoff"):
        c.docs["phoenix:one"]["usage_date"] = "2026-10-18" if change == "today" else "2026-10-16"
    run(c, p)
    assert not p.deleted


@pytest.mark.parametrize("end", ["2026-10-16T00:00:00+09:00", "2026-10-15T13:00:00+09:00"])
def test_crossing_cutoff_or_changed_since_archive_preserved(end):
    c, p = Collection(), Phoenix()
    p.value["endTime"] = end
    run(c, p)
    assert not p.deleted


def test_kst_cutoff():
    assert m.cutoff_for(NOW) == datetime(2026, 10, 16, tzinfo=m.KST)
    assert m.cutoff_for(datetime(2026, 10, 17, 15, tzinfo=timezone.utc)) == m.cutoff_for(NOW)


def test_uncertain_survives_archive_replacement():
    c, p = Collection(), Phoenix()
    p.fail = True
    with pytest.raises(TimeoutError): run(c, p)
    assert c.docs["phoenix:one"]["phoenix_cleanup_status"] == "uncertain"
    c.docs["phoenix:one"] = row()  # mimic full archive refresh replacing bookkeeping
    c.docs[m.LOCK_ID]["lease_until"] = NOW - timedelta(seconds=1)
    p.fail = False
    run(c, p)
    assert not p.deleted
    assert c.docs["phoenix:one"]["phoenix_cleanup_status"] == "uncertain"


def test_archive_changes_during_phoenix_read_stop_delete():
    c, p = Collection(), Phoenix()
    original = p.trace
    def changed(*args):
        c.docs["phoenix:one"]["archive_refresh_id"] = "changed"
        return original(*args)
    p.trace = changed
    with pytest.raises(RuntimeError): run(c, p)
    assert not p.deleted


def test_budget_stops_before_delete():
    c, p = Collection(), Phoenix()
    values = iter([0, 100])
    run(c, p, clock=lambda: next(values))
    assert not p.deleted


def test_dispatch_nonblocking_and_local_duplicate_prevention(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(m, "_gate", threading.Lock())
    monkeypatch.setattr(m, "_next_launch", 0)
    monkeypatch.setattr(m, "get_setting", lambda *a: "true")
    def work(*args):
        entered.set()
        release.wait(2)
        m._gate.release()
    monkeypatch.setattr(m, "_background", work)
    config = SimpleNamespace(is_configured=True)
    try:
        assert m.schedule_cleanup(config, config) is True
        assert entered.wait(1)
        assert m.schedule_cleanup(config, config) is False
    finally:
        release.set()
    assert m._gate.acquire(timeout=2)
    m._gate.release()


def test_disabled_never_dispatches(monkeypatch):
    monkeypatch.setattr(m, "get_setting", lambda *a: "false")
    assert m.schedule_cleanup(None, None) is False


def test_record_limit_and_missing_trace(monkeypatch):
    c, p = Collection(), Phoenix()
    extra = row()
    extra["_id"] = "phoenix:two"
    c.docs[extra["_id"]] = extra
    monkeypatch.setattr(m, "MAX_RECORDS", 1)
    p.value = None
    result = run(c, p)
    assert result["missing"] == 1
    assert "phoenix_cleanup_status" not in c.docs[extra["_id"]]
    assert not p.deleted


def test_unsupported_api_keeps_archive_and_releases_lock():
    c, p = Collection(), Phoenix()
    def unsupported(): raise RuntimeError("unsupported")
    p.check_support = unsupported
    with pytest.raises(RuntimeError): run(c, p)
    assert c.docs["phoenix:one"] == row()
    assert not p.deleted
    assert "finished_at" in c.docs[m.LOCK_ID]


def test_other_process_lease_blocks_before_phoenix():
    c, p = Collection(), Phoenix()
    c.docs[m.LOCK_ID] = {"_id": m.LOCK_ID, "owner": "another", "lease_until": NOW + timedelta(minutes=5)}
    def forbidden(): pytest.fail("Must not contact Phoenix while lease is held")
    p.check_support = forbidden
    assert run(c, p) == {"busy": True}
