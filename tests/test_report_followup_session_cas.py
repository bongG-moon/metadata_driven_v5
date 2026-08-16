from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
LOADER_PATH = ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py"
WRITER_PATH = ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py"


def _install_lfx_stubs() -> None:
    if importlib.util.find_spec("lfx") is not None:
        return

    class Component:
        pass

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    for name in (
        "lfx",
        "lfx.custom",
        "lfx.custom.custom_component",
        "lfx.custom.custom_component.component",
        "lfx.io",
        "lfx.schema",
        "lfx.schema.data",
    ):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["lfx.custom.custom_component.component"].Component = Component
    for name in ("DataInput", "DropdownInput", "MessageTextInput", "Output"):
        setattr(sys.modules["lfx.io"], name, InputBase)
    sys.modules["lfx.schema.data"].Data = Data


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_lfx_stubs()
loader = _load("report_followup_session_loader_cas_test", LOADER_PATH)
writer = _load("report_followup_session_writer_cas_test", WRITER_PATH)


def _state(context_ref: str) -> dict:
    return {
        "session_id": "session-report-cas",
        "last_question": "질문",
        "current_data": {
            "row_count": 10,
            "columns": ["VALUE"],
            "source_aliases": ["report_snapshot"],
            "source_dataset_keys": ["report_snapshot"],
            "report_context": {
                "report_type": "realtime_production",
                "context_ref": context_ref,
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        },
    }


class _Client:
    def close(self):
        return None


class _WriteResult:
    def __init__(self, matched_count: int):
        self.matched_count = matched_count


class _Collection:
    def __init__(self, document: dict, *, force_race: bool = False):
        self.document = deepcopy(document)
        self.force_race = force_race

    def find_one(self, _query):
        return deepcopy(self.document)

    def replace_one(self, query, document, upsert=False):
        if self.force_race and not upsert:
            return _WriteResult(0)
        if not upsert:
            context_ref = (
                self.document.get("state", {})
                .get("current_data", {})
                .get("report_context", {})
                .get("context_ref", "")
            )
            matched = (
                query.get("_id") == self.document.get("_id")
                and query.get("turn_count") == self.document.get("turn_count")
                and query.get("state.current_data.report_context.context_ref") == context_ref
            )
            if not matched:
                return _WriteResult(0)
        self.document = deepcopy(document)
        return _WriteResult(1)


def _document(context_ref: str, turn_count: int) -> dict:
    return {
        "_id": "session_state:session-report-cas",
        "session_id": "session-report-cas",
        "state": _state(context_ref),
        "turn_count": turn_count,
        "updated_at": "2026-08-16T00:00:00+00:00",
    }


def test_session_loader_exposes_internal_revision_for_report_followup(monkeypatch):
    collection = _Collection(_document("context-A", 7))
    monkeypatch.setattr(loader, "_connect_collection", lambda *_args: (_Client(), collection))

    result = loader.load_session_state(
        question={"session_id": "session-report-cas"},
        mongo_uri="mongodb://example.invalid",
        runtime_session_id="session-report-cas",
    )

    assert result["state"]["_session_state_revision"] == 7
    assert result["session_state_load"]["turn_count"] == 7


def test_guarded_session_write_succeeds_when_revision_and_context_match(monkeypatch):
    collection = _Collection(_document("context-A", 1))
    monkeypatch.setattr(writer, "_connect_collection", lambda *_args: (_Client(), collection))
    payload = {
        "request": {"question": "그중 5개", "session_id": "session-report-cas"},
        "state": _state("context-A"),
        "session_state_guard": {
            "expected_turn_count": 1,
            "expected_report_context_ref": "context-A",
        },
    }

    result = writer.write_session_state(payload, mongo_uri="mongodb://example.invalid")

    assert result["session_state_write"]["saved"] is True
    assert result["session_state_write"]["turn_count"] == 2
    assert collection.document["turn_count"] == 2
    assert collection.document["state"]["current_data"]["report_context"]["context_ref"] == "context-A"


def test_guarded_session_write_rejects_a_newer_active_report(monkeypatch):
    collection = _Collection(_document("context-B", 2))
    before = deepcopy(collection.document)
    monkeypatch.setattr(writer, "_connect_collection", lambda *_args: (_Client(), collection))
    payload = {
        "request": {"question": "그중 5개", "session_id": "session-report-cas"},
        "state": _state("context-A"),
        "session_state_guard": {
            "expected_turn_count": 1,
            "expected_report_context_ref": "context-A",
        },
    }

    result = writer.write_session_state(payload, mongo_uri="mongodb://example.invalid")

    status = result["session_state_write"]
    assert status["saved"] is False
    assert status["reason"] == "stale_session_state"
    assert collection.document == before


def test_guarded_session_write_rejects_a_race_at_replace_time(monkeypatch):
    collection = _Collection(_document("context-A", 1), force_race=True)
    monkeypatch.setattr(writer, "_connect_collection", lambda *_args: (_Client(), collection))
    payload = {
        "request": {"question": "그중 5개", "session_id": "session-report-cas"},
        "state": _state("context-A"),
        "session_state_guard": {
            "expected_turn_count": 1,
            "expected_report_context_ref": "context-A",
        },
    }

    result = writer.write_session_state(payload, mongo_uri="mongodb://example.invalid")

    assert result["session_state_write"]["saved"] is False
    assert result["session_state_write"]["reason"] == "stale_session_state"
