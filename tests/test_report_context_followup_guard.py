from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from component_test_support import ROOT, load_module


LOADER_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py"


result_loader = load_module(LOADER_PATH)


def _stored_report_document() -> dict:
    return {
        "session_id": "report-session",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "payload": {
            "result_rows": [{"TECH": "HBM", "판정": "생산부족"}],
            "runtime_sources": {
                "report_snapshot": [{"TECH": "HBM", "판정": "생산부족"}]
            },
            "source_results": [
                {
                    "dataset_key": "production_judgement_snapshot",
                    "source_alias": "report_snapshot",
                    "columns": ["TECH", "판정"],
                    "row_count": 1,
                }
            ],
            "data": {"columns": ["TECH", "판정"], "row_count": 1},
            "storage_manifest": {
                "result_rows": {"complete": True, "original_count": 1, "stored_count": 1},
                "runtime_sources": {
                    "report_snapshot": {
                        "complete": True,
                        "original_count": 1,
                        "stored_count": 1,
                    }
                },
            },
        },
    }


def _loader_payload() -> dict:
    return {
        "request": {
            "question": "방금 Report에서 생산 부족 제품만 보여줘",
            "session_id": "report-session",
        },
        "followup_hint": {
            "report_reference": True,
            "unresolved_report_reference": False,
            "fresh_data_requested": False,
        },
        "state": {
            "current_data": {
                "data_ref": {"ref_id": "result:report-session:snapshot"},
                "source_aliases": ["report_snapshot"],
                "report_context": {
                    "context_version": "report.context.v1",
                    "context_ref": "result:report-session:snapshot",
                    "report_type": "realtime_production",
                },
            },
            "runtime_source_refs": {
                "report_snapshot": {
                    "ref_id": "result:report-session:snapshot",
                    "source_alias": "report_snapshot",
                    "dataset_key": "production_judgement_snapshot",
                    "role": "source_rows",
                }
            },
        },
        "intent_plan": {
            "reuse_strategy": "previous_source",
            "pandas_execution_plan": [
                {
                    "operation": "apply_filters",
                    "source_alias": "report_snapshot",
                    "inputs": [
                        {"kind": "external_source", "ref": "report_snapshot"}
                    ],
                }
            ],
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": "report_snapshot",
                        "provider": "previous_source",
                        "required": True,
                    }
                ]
            },
        },
    }


class _FakeCollection:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def find_one(self, query, projection):
        self.calls.append((deepcopy(query), deepcopy(projection)))
        return deepcopy(self.document)


class _FakeDatabase:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        return self.collection


class _FakeClient:
    def __init__(self, document):
        self.collection = _FakeCollection(document)
        self.closed = False

    def __getitem__(self, _name):
        return _FakeDatabase(self.collection)

    def close(self):
        self.closed = True


def _install_fake_mongo(monkeypatch: pytest.MonkeyPatch, document):
    clients = []

    def factory(*_args, **_kwargs):
        client = _FakeClient(document)
        clients.append(client)
        return client

    monkeypatch.setattr(
        result_loader,
        "import_module",
        lambda _name: SimpleNamespace(MongoClient=factory),
    )
    return clients


def test_report_context_loader_restores_the_same_session_complete_snapshot(monkeypatch):
    clients = _install_fake_mongo(monkeypatch, _stored_report_document())

    loaded = result_loader.load_previous_result(_loader_payload(), "mongodb://test")

    assert loaded["runtime_sources"] == {
        "report_snapshot": [{"TECH": "HBM", "판정": "생산부족"}]
    }
    assert loaded["trace"]["inspection"]["result_loader"]["status"] == "ok"
    assert clients[0].collection.calls[0][0] == {
        "_id": "result:report-session:snapshot"
    }
    assert clients[0].closed is True


@pytest.mark.parametrize(
    ("mutation", "expected_type"),
    [
        ("not_found", "report_context_not_found"),
        ("expired", "report_context_expired"),
        ("cross_session", "report_context_session_mismatch"),
        ("incomplete", "report_context_source_incomplete"),
    ],
)
def test_report_context_loader_fails_closed_for_untrusted_snapshot_state(
    monkeypatch,
    mutation: str,
    expected_type: str,
):
    document = _stored_report_document()
    if mutation == "not_found":
        document = None
    elif mutation == "expired":
        document["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif mutation == "cross_session":
        document["session_id"] = "another-session"
    else:
        document["payload"]["storage_manifest"]["runtime_sources"]["report_snapshot"][
            "complete"
        ] = False
    _install_fake_mongo(monkeypatch, document)

    loaded = result_loader.load_previous_result(_loader_payload(), "mongodb://test")

    inspection = loaded["trace"]["inspection"]["result_loader"]
    assert inspection["status"] == "error"
    assert inspection["errors"][0]["type"] == expected_type
    assert "runtime_sources" not in loaded


def test_non_report_missing_result_keeps_the_existing_skipped_behavior(monkeypatch):
    _install_fake_mongo(monkeypatch, None)
    payload = {
        "request": {"session_id": "ordinary-session"},
        "intent_plan": {"reuse_strategy": "previous_result"},
        "state": {"current_data": {"data_ref": "ordinary-missing-ref"}},
    }

    loaded = result_loader.load_previous_result(payload, "mongodb://test")

    assert loaded["trace"]["inspection"]["result_loader"]["status"] == "skipped"
    assert (
        loaded["trace"]["inspection"]["result_loader"]["errors"][0]["type"]
        == "result_not_found"
    )
    assert loaded.get("trace", {}).get("errors", []) == []
