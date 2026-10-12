"""오프라인 검증 전용: 실제 서버 호출 없음."""
import copy
import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location("cleanup", Path(__file__).with_name("cleanup_phoenix_history.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
CUTOFF = datetime(2026, 10, 10, tzinfo=m.KST)

def trace(end="2026-09-01T12:01:00+09:00"):
    return {"id": "relay1", "traceId": "a" * 32,
            "startTime": "2026-09-01T11:59:00+09:00", "endTime": end}

class Phoenix:
    def __init__(self):
        self.row, self.deleted, self.fail = trace(), [], False
    def check_delete_support(self): pass
    def project(self, name): return name
    def list_traces(self, project, cutoff): yield copy.deepcopy(self.row)
    def trace(self, project, trace_id): return None if self.deleted else copy.deepcopy(self.row)
    def delete(self, relay):
        if self.fail: raise TimeoutError("ambiguous")
        self.deleted.append(relay)

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "__file__", str(tmp_path / "cleanup.py"))
    monkeypatch.setattr(m, "PROJECTS", ["p1"])
    for key in ("ARCHIVE_BACKUP_AND_REFRESH_PROTECTION_CONFIRMED", "FULL_TRACE_LOSS_ACCEPTED", "INGESTION_FOR_TARGET_TRACES_STOPPED"):
        monkeypatch.setattr(m, key, True)
    return monkeypatch, tmp_path

def test_kst_calendar_cutoff():
    assert m.retention_cutoff(datetime(2026, 10, 12, 15, tzinfo=m.KST)) == CUTOFF
    assert m.retention_cutoff(m.timestamp("2026-10-11T15:00:00Z")) == CUTOFF

def test_boundary_and_crossing_trace():
    assert m.trace_is_safe(trace(), CUTOFF)
    assert not m.trace_is_safe(trace("2026-10-10T00:00:00+09:00"), CUTOFF)
    assert not m.trace_is_safe(trace("2026-10-12T00:00:00+09:00"), CUTOFF)
    with pytest.raises(ValueError): m.trace_is_safe(trace("bad"), CUTOFF)

def test_dryrun_no_event_or_archive_filter(setup):
    phoenix = Phoenix()
    report = m.run(phoenix, CUTOFF, confirm=lambda _: pytest.fail("dry run"))
    assert len(report["traces"]) == 1
    assert not phoenix.deleted

def test_cancel_then_verified_delete(setup):
    setup[0].setattr(m, "DRY_RUN", False)
    phoenix = Phoenix()
    m.run(phoenix, CUTOFF, confirm=lambda _: "no")
    assert not phoenix.deleted
    report = m.run(phoenix, CUTOFF, confirm=lambda _: "DELETE PHOENIX 1")
    assert phoenix.deleted == ["relay1"]
    assert report["traces"][0]["status"] == "deleted_verified"

def test_changed_trace_stops(setup):
    setup[0].setattr(m, "DRY_RUN", False)
    phoenix = Phoenix()
    def confirm(_):
        phoenix.row["endTime"] = "2026-10-12T00:00:00+09:00"
        return "DELETE PHOENIX 1"
    with pytest.raises(RuntimeError): m.run(phoenix, CUTOFF, confirm=confirm)
    assert not phoenix.deleted

def test_guard(setup):
    setup[0].setattr(m, "DRY_RUN", False)
    setup[0].setattr(m, "FULL_TRACE_LOSS_ACCEPTED", False)
    with pytest.raises(ValueError): m.run(Phoenix(), CUTOFF)

def test_timeout_audited(setup):
    import json
    setup[0].setattr(m, "DRY_RUN", False)
    phoenix = Phoenix()
    phoenix.fail = True
    with pytest.raises(TimeoutError):
        m.run(phoenix, CUTOFF, confirm=lambda _: "DELETE PHOENIX 1")
    report = json.loads(next((setup[1] / "reports").glob("*.json")).read_text(encoding="utf-8"))
    assert report["traces"][0]["status"] == "delete_requested"
    assert report["traces"][0]["needs_review"]

def test_pagination_no_filters():
    client = object.__new__(m.Phoenix)
    client.url = "https://example.test/graphql"
    calls = []
    def get(url, params, timeout):
        calls.append(dict(params))
        row = {"id": str(len(calls)), "trace_id": "a" * 32, "project_id": "p1",
               "start_time": "2026-09-01T00:00:00Z", "end_time": "2026-09-01T00:01:00Z"}
        body = {"data": [row], "next_cursor": "next" if len(calls) == 1 else None}
        return SimpleNamespace(status_code=200, json=lambda: body)
    client.session = SimpleNamespace(get=get)
    assert len(list(client.list_traces("p1", CUTOFF))) == 2
    assert calls == [{"end_time": CUTOFF.isoformat(), "limit": 500},
                     {"end_time": CUTOFF.isoformat(), "limit": 500, "cursor": "next"}]

def test_wrong_project_and_unsupported_api():
    client = object.__new__(m.Phoenix)
    client.url = "https://example.test/graphql"
    client.session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=200,
        json=lambda: {"data": [{"project_id": "wrong"}], "next_cursor": None}))
    with pytest.raises(RuntimeError): list(client.list_traces("p1", CUTOFF))
    client.session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=404))
    with pytest.raises(RuntimeError): list(client.list_traces("p1", CUTOFF))
