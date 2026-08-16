from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from component_test_support import ROOT, load_module


NORMALIZER_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "04_intent_plan_normalizer.py"
LOADER_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py"


normalizer = load_module(NORMALIZER_PATH)
result_loader = load_module(LOADER_PATH)


def _report_payload(
    *,
    fresh: bool = False,
    report_reference: bool = True,
    unresolved: bool = False,
    session_id: str = "report-session",
) -> dict:
    return {
        "request": {
            "question": "방금 Report에서 생산 부족 제품만 보여줘",
            "session_id": session_id,
        },
        "followup_hint": {
            "report_reference": report_reference,
            "unresolved_report_reference": unresolved,
            "fresh_data_requested": fresh,
        },
        "state": {
            "current_data": {
                "data_ref": {"ref_id": "result:report-session:snapshot"},
                "source_aliases": ["report_snapshot"],
                "report_context": {
                    "context_version": "report.context.v1",
                    "context_ref": "result:report-session:snapshot",
                    "report_type": "realtime_production",
                    "allowed_operations": [
                        "filter",
                        "groupby_and_aggregate",
                        "sort_and_top_n",
                    ],
                    "semantic_filters": [
                        {
                            "key": "production_shortage",
                            "aliases": ["생산부족", "생산 부족", "생산 부족 제품"],
                            "source_alias": "report_snapshot",
                            "column": "달성율*판정",
                            "operator": "eq",
                            "value": "생산부족",
                        }
                    ],
                    "value_domains": [
                        {
                            "source_alias": "report_snapshot",
                            "column": "달성율*판정",
                            "values": ["정상", "정상(초과생산)", "Abnormal", "생산부족"],
                        },
                        {
                            "source_alias": "report_snapshot",
                            "column": "CAPA판정",
                            "values": ["정상", "CAPA부족"],
                        },
                        {
                            "source_alias": "report_snapshot",
                            "column": "적정재공*판정",
                            "values": ["정상", "재공과다", "Abnormal"],
                        },
                    ],
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
    }


def _snapshot_filter_plan() -> dict:
    return {
        "request_scope": "followup_transform",
        "reference_mode": "previous_source",
        "retrieval_jobs": [],
        "condition_resolution": {
            "effective_filters": {
                "report_snapshot": {
                    "source_alias": "report_snapshot",
                    "filters": {
                        "달성율*판정": {"operator": "eq", "value": "생산부족"}
                    },
                }
            }
        },
        "pandas_execution_plan": [
            {
                "node_id": "filter_shortage",
                "operation": "apply_filters",
                "source_alias": "report_snapshot",
                "inputs": [{"kind": "external_source", "ref": "report_snapshot"}],
                "filters": {
                    "달성율*판정": {"operator": "eq", "value": "생산부족"}
                },
            }
        ],
    }


def test_report_snapshot_followup_stays_retrieval_free_and_accepts_standard_operation_alias():
    payload = _report_payload()
    plan = _snapshot_filter_plan()
    plan["pandas_execution_plan"][0]["source_alias"] = "previous_result"
    plan["pandas_execution_plan"][0]["inputs"][0]["ref"] = "previous_result"
    prepared, guard = normalizer._prepare_report_followup_contract(payload, plan)
    jobs, steps, enforced = normalizer._enforce_report_followup_execution_contract(
        payload,
        prepared,
        normalizer._retrieval_jobs(prepared),
        prepared["pandas_execution_plan"],
        guard,
    )

    assert prepared["request_scope"] == "followup_transform"
    assert prepared["reference_mode"] == "previous_source"
    assert prepared["pandas_execution_plan"][0]["source_alias"] == "report_snapshot"
    assert prepared["pandas_execution_plan"][0]["inputs"] == [
        {"kind": "external_source", "ref": "report_snapshot"}
    ]
    assert jobs == []
    assert steps[0]["operation"] == "apply_filters"
    assert enforced["status"] == "snapshot_only"
    assert enforced["validation_errors"] == []


def test_report_snapshot_followup_blocks_an_llm_silent_requery_attempt():
    plan = _snapshot_filter_plan()
    plan["retrieval_jobs"] = [
        {"dataset_key": "production_today", "source_alias": "fresh_production"}
    ]

    prepared, guard = normalizer._prepare_report_followup_contract(_report_payload(), plan)

    assert prepared["retrieval_jobs"] == []
    assert guard["status"] == "blocked"
    assert guard["blocked_retrieval_jobs"] == [
        {"dataset_key": "production_today", "source_alias": "fresh_production"}
    ]
    assert {item["type"] for item in guard["validation_errors"]} == {
        "report_snapshot_requery_blocked"
    }


def test_report_refresh_without_an_actual_retrieval_job_is_blocked():
    payload = _report_payload(fresh=True)
    prepared, guard = normalizer._prepare_report_followup_contract(payload, _snapshot_filter_plan())
    jobs, _, enforced = normalizer._enforce_report_followup_execution_contract(
        payload,
        prepared,
        [],
        prepared["pandas_execution_plan"],
        guard,
    )

    assert prepared["request_scope"] == "followup_requery"
    assert prepared["reference_mode"] == "previous_result_rows"
    assert jobs == []
    assert enforced["status"] == "blocked"
    assert {item["type"] for item in enforced["validation_errors"]} == {
        "report_refresh_retrieval_required"
    }


def test_explicit_report_reference_without_context_never_falls_back_to_retrieval():
    payload = _report_payload(report_reference=False, unresolved=True)
    payload["state"] = {}
    plan = _snapshot_filter_plan()
    plan["retrieval_jobs"] = [
        {"dataset_key": "production_today", "source_alias": "fresh_production"}
    ]

    prepared, guard = normalizer._prepare_report_followup_contract(payload, plan)

    assert prepared["request_scope"] == "clarification"
    assert prepared["retrieval_jobs"] == []
    assert prepared["pandas_execution_plan"] == []
    assert guard["validation_errors"][0]["type"] == "report_context_missing"


def test_report_snapshot_blocks_operations_outside_the_context_allowlist():
    payload = _report_payload()
    plan = _snapshot_filter_plan()
    plan["pandas_execution_plan"] = [
        {
            "node_id": "unsafe_join",
            "operation": "join",
            "left_source_alias": "report_snapshot",
            "right_source_alias": "untrusted_source",
        }
    ]
    prepared, guard = normalizer._prepare_report_followup_contract(payload, plan)

    _, _, enforced = normalizer._enforce_report_followup_execution_contract(
        payload,
        prepared,
        [],
        prepared["pandas_execution_plan"],
        guard,
    )

    error = next(
        item
        for item in enforced["validation_errors"]
        if item["type"] == "report_context_operation_not_allowed"
    )
    assert error["operations"] == ["join"]
    assert enforced["status"] == "blocked"


def _semantic_validation(plan: dict, payload: dict | None = None):
    payload = payload or _report_payload()
    contract = normalizer._report_followup_contract(payload)
    return normalizer._validate_report_semantic_filter_contract(
        contract,
        plan,
        plan.get("pandas_execution_plan", []),
    )


def test_report_semantic_filter_accepts_the_executor_canonical_filter_shape():
    errors, guard = _semantic_validation(_snapshot_filter_plan())

    assert errors == []
    assert guard["status"] == "validated"
    assert guard["matched_filter_keys"] == ["production_shortage"]
    assert guard["condition_count"] == 2


@pytest.mark.parametrize(
    ("column", "value", "expected_types"),
    [
        (
            "CAPA판정",
            "생산부족",
            {
                "report_context_value_domain_violation",
                "report_context_semantic_filter_mismatch",
                "report_context_semantic_filter_conflict",
            },
        ),
        (
            "달성율*판정",
            "CAPA부족",
            {
                "report_context_value_domain_violation",
                "report_context_semantic_filter_mismatch",
                "report_context_semantic_filter_conflict",
            },
        ),
    ],
)
def test_report_semantic_filter_blocks_wrong_column_or_value(
    column: str,
    value: str,
    expected_types: set[str],
):
    plan = _snapshot_filter_plan()
    filters = {column: {"operator": "eq", "value": value}}
    plan["condition_resolution"]["effective_filters"]["report_snapshot"]["filters"] = filters
    plan["pandas_execution_plan"][0]["filters"] = deepcopy(filters)

    errors, guard = _semantic_validation(plan)

    assert {item["type"] for item in errors} == expected_types
    assert guard["status"] == "blocked"


def test_report_semantic_filter_does_not_trust_non_executable_conditions_only():
    plan = _snapshot_filter_plan()
    plan.pop("condition_resolution")
    step = plan["pandas_execution_plan"][0]
    step.pop("filters")
    step["conditions"] = [
        {"field": "달성율*판정", "operator": "eq", "value": "생산부족"}
    ]

    errors, guard = _semantic_validation(plan)

    assert {item["type"] for item in errors} == {
        "report_context_semantic_filter_mismatch"
    }
    assert guard["condition_count"] == 0


def test_normalizer_revalidates_the_final_executor_filter_contract():
    plan = _snapshot_filter_plan()
    plan.pop("condition_resolution")
    step = plan["pandas_execution_plan"][0]
    step.pop("filters")
    step["conditions"] = [
        {"field": "달성율*판정", "operator": "eq", "value": "생산부족"}
    ]

    normalized = normalizer.normalize_intent_plan(
        _report_payload(),
        {"intent_plan": plan},
        {},
    )

    assert "report_context_semantic_filter_mismatch" in {
        item.get("type")
        for item in normalized["intent_plan"].get("validation_errors", [])
        if isinstance(item, dict)
    }
    report_guard = normalized["trace"]["inspection"]["intent"][
        "report_followup_guard"
    ]
    assert report_guard["status"] == "blocked"
    assert report_guard["semantic_filter_guard"]["condition_count"] == 0


def test_report_semantic_filter_checks_external_source_lineage_too():
    plan = _snapshot_filter_plan()
    plan["pandas_execution_plan"][0]["inputs"] = [
        {"kind": "external_source", "ref": "untrusted_source"}
    ]

    errors, _ = _semantic_validation(plan)

    source_error = next(
        item for item in errors if item["type"] == "report_context_source_not_allowed"
    )
    assert source_error["source_aliases"] == ["untrusted_source"]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("달성율*판정", "정상"),
        ("적정재공*판정", "Abnormal"),
    ],
)
def test_report_semantic_filter_blocks_a_conflicting_second_execution_channel(
    column: str,
    value: str,
):
    plan = _snapshot_filter_plan()
    plan["pandas_execution_plan"][0]["filters"] = {
        column: {"operator": "eq", "value": value}
    }

    errors, guard = _semantic_validation(plan)

    assert "report_context_semantic_filter_conflict" in {
        item["type"] for item in errors
    }
    assert "report_context_semantic_filter_mismatch" not in {
        item["type"] for item in errors
    }
    assert guard["status"] == "blocked"


def test_report_semantic_filter_requires_every_non_overlapping_question_condition():
    payload = _report_payload()
    payload["request"]["question"] = (
        "방금 Report에서 생산 부족 제품 중 장비 필요 제품만 보여줘"
    )
    context = payload["state"]["current_data"]["report_context"]
    context["semantic_filters"].append(
        {
            "key": "equipment_needed",
            "aliases": ["장비필요", "장비 필요", "장비필요 제품", "장비 필요 제품"],
            "source_alias": "report_snapshot",
            "column": "장비교체판단",
            "operator": "eq",
            "value": "장비필요",
        }
    )
    context["value_domains"].append(
        {
            "source_alias": "report_snapshot",
            "column": "장비교체판단",
            "values": ["정상", "장비필요", "교체필요"],
        }
    )
    plan = _snapshot_filter_plan()

    errors, guard = _semantic_validation(plan, payload)

    assert {item["type"] for item in errors} == {
        "report_context_semantic_filter_mismatch"
    }
    assert guard["matched_filter_keys"] == [
        "production_shortage",
        "equipment_needed",
    ]

    plan["pandas_execution_plan"][0]["filters"]["장비교체판단"] = {
        "operator": "eq",
        "value": "장비필요",
    }
    errors, guard = _semantic_validation(plan, payload)
    assert errors == []
    assert guard["status"] == "validated"


@pytest.mark.parametrize(
    ("question", "expected_keys"),
    [
        ("그중 장비필요대수 알려줘", []),
        ("그중 장비 필요 대수 알려줘", []),
        ("그중 장비 필요 제품 알려줘", ["equipment_needed"]),
        ("그중 장비필요인 제품 알려줘", ["equipment_needed"]),
        ("그중 CAPA부족제품 알려줘", ["capa_shortage"]),
        ("그중 CAPA 부족 제품 알려줘", ["capa_shortage"]),
    ],
)
def test_report_semantic_alias_distinguishes_metric_compounds_from_row_labels(
    question: str,
    expected_keys: list[str],
):
    payload = _report_payload()
    payload["state"]["current_data"]["report_context"]["semantic_filters"].append(
        {
            "key": "equipment_needed",
            "aliases": ["장비필요", "장비 필요", "장비필요 제품", "장비 필요 제품"],
            "source_alias": "report_snapshot",
            "column": "장비교체판단",
            "operator": "eq",
            "value": "장비필요",
        }
    )
    payload["state"]["current_data"]["report_context"]["semantic_filters"].append(
        {
            "key": "capa_shortage",
            "aliases": ["CAPA부족", "CAPA 부족", "캐파 부족"],
            "source_alias": "report_snapshot",
            "column": "CAPA판정",
            "operator": "eq",
            "value": "CAPA부족",
        }
    )
    contract = normalizer._report_followup_contract(payload)

    matched, ambiguity, negated = normalizer._matched_report_semantic_filters(
        question,
        contract["semantic_filters"],
    )

    assert [item["key"] for item in matched] == expected_keys
    assert ambiguity == []
    assert negated == []


@pytest.mark.parametrize(
    "question",
    [
        "방금 Report에서 생산부족을 제외한 제품만 보여줘",
        "방금 Report에서 생산 부족이 아닌 제품만 보여줘",
        "방금 Report에서 생산 부족하지 않은 제품만 보여줘",
    ],
)
def test_report_semantic_filter_blocks_undeclared_negation_instead_of_inverting_it(
    question: str,
):
    payload = _report_payload()
    payload["request"]["question"] = question

    errors, guard = _semantic_validation(_snapshot_filter_plan(), payload)

    assert "report_context_semantic_filter_negation_unsupported" in {
        item["type"] for item in errors
    }
    assert guard["negated_aliases"] == ["생산부족"]
    assert guard["status"] == "blocked"


def test_report_semantic_filter_blocks_an_alias_with_multiple_targets():
    payload = _report_payload()
    payload["state"]["current_data"]["report_context"]["semantic_filters"].append(
        {
            "key": "ambiguous_shortage",
            "aliases": ["생산 부족 제품"],
            "source_alias": "report_snapshot",
            "column": "CAPA판정",
            "operator": "eq",
            "value": "CAPA부족",
        }
    )

    errors, guard = _semantic_validation(_snapshot_filter_plan(), payload)

    assert {item["type"] for item in errors} == {
        "report_context_semantic_filter_ambiguous"
    }
    assert set(guard["ambiguity"]) == {"생산부족", "생산부족제품"}


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
    payload = _report_payload()
    payload["intent_plan"] = {
        "reuse_strategy": "previous_source",
        "pandas_execution_plan": [
            {
                "operation": "apply_filters",
                "source_alias": "report_snapshot",
                "inputs": [{"kind": "external_source", "ref": "report_snapshot"}],
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
    }
    return payload


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
    assert loaded["trace"]["inspection"]["result_loader"]["errors"][0]["type"] == "result_not_found"
    assert loaded.get("trace", {}).get("errors", []) == []
