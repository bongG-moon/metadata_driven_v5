from __future__ import annotations

from copy import deepcopy

import pytest

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


@pytest.fixture(scope="module")
def normalizer():
    return load_module(V2_ROOT / "04_intent_plan_normalizer.py")


@pytest.fixture(scope="module")
def executor():
    return load_module(V2_ROOT / "17_hybrid_analysis_executor.py")


def _catalog(
    *,
    time_scope: str = "current_day",
    filter_mappings: dict | None = None,
    required_params: list[str] | None = None,
) -> dict:
    return {
        "table_catalog_items": [
            {
                "dataset_key": "status_snapshot",
                "payload": {
                    "selection_criteria": {"time_scope": time_scope},
                    "columns": ["EQP_ID", "WORK_DT", "RATE"],
                    "filter_mappings": (
                        {"DATE": ["WORK_DT"]}
                        if filter_mappings is None
                        else filter_mappings
                    ),
                    "required_params": required_params or [],
                },
            }
        ]
    }


def _complete_date(normalizer, *, payload=None, job=None, catalog=None):
    return normalizer._apply_current_day_optional_date_filter_completion(
        payload
        or {
            "request": {
                "question": "현재 장비 상태 알려줘",
                "reference_date": "20260822",
            }
        },
        [
            job
            or {
                "dataset_key": "status_snapshot",
                "source_alias": "status",
            }
        ],
        catalog or _catalog(),
    )


def test_current_day_optional_date_filter_is_completed_with_trace(normalizer):
    jobs, trace = _complete_date(normalizer)

    assert jobs[0]["filters"]["DATE"] == {
        "operator": "eq",
        "value": "20260822",
    }
    assert trace["status"] == "applied"
    assert trace["completed_filters"][0] == {
        "dataset_key": "status_snapshot",
        "source_alias": "status",
        "field": "DATE",
        "condition": {"operator": "eq", "value": "20260822"},
        "catalog_time_scope": "current_day",
        "filter_mapping_candidates": ["DATE", "WORK_DT"],
        "reason": "explicit_current_day_scope_with_reference_date",
    }


def test_normalizer_exposes_current_day_date_completion_in_intent_trace(normalizer):
    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "오늘 장비 상태 알려줘",
                "reference_date": "20260822",
            },
            "trace": {},
        },
        {
            "intent_plan": {
                "analysis_kind": "generic_current_status",
                "request_scope": "new_analysis",
                "retrieval_jobs": [
                    {
                        "dataset_key": "status_snapshot",
                        "source_alias": "status",
                    }
                ],
                "pandas_execution_plan": [],
                "output_contract": {
                    "result_mode": "detail",
                    "result_columns": ["EQP_ID", "RATE"],
                },
            }
        },
        {"metadata_candidates": _catalog()},
    )

    job = normalized["intent_plan"]["retrieval_jobs"][0]
    assert job["filters"]["DATE"] == {
        "operator": "eq",
        "value": "20260822",
    }
    trace = normalized["trace"]["inspection"]["intent"][
        "current_day_optional_date_completion"
    ]
    assert trace["status"] == "applied"
    assert trace["completed_filters"][0]["reason"] == (
        "explicit_current_day_scope_with_reference_date"
    )


@pytest.mark.parametrize(
    ("payload", "job", "catalog"),
    [
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            None,
            _catalog(time_scope="history"),
        ),
        (
            {
                "request": {
                    "question": "장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            None,
            None,
        ),
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "not-a-date",
                }
            },
            None,
            None,
        ),
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            None,
            _catalog(filter_mappings={"EQP_ID": ["EQP_ID"]}),
        ),
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            {
                "dataset_key": "status_snapshot",
                "source_alias": "status",
                "filters": {
                    "WORK_DT": {"operator": "eq", "value": "20260821"}
                },
            },
            None,
        ),
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            {
                "dataset_key": "status_snapshot",
                "source_alias": "status",
                "required_params": {"DATE": "20260821"},
            },
            None,
        ),
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            None,
            _catalog(required_params=["DATE"]),
        ),
        (
            {
                "request": {
                    "question": "현재 장비 상태 알려줘",
                    "reference_date": "20260822",
                }
            },
            None,
            _catalog(required_params=["WORK_DT"]),
        ),
    ],
)
def test_current_day_optional_date_completion_keeps_non_applicable_cases_unchanged(
    normalizer,
    payload,
    job,
    catalog,
):
    original_job = job or {
        "dataset_key": "status_snapshot",
        "source_alias": "status",
    }
    jobs, trace = _complete_date(
        normalizer,
        payload=payload,
        job=original_job,
        catalog=catalog,
    )

    assert jobs == [original_job]
    assert trace["status"] == "not_needed"
    assert trace["completed_filters"] == []


def _executor_payload(right_rows: list[dict], *, multi_match_policy: str = "") -> dict:
    step = {
        "node_id": "join_status",
        "operation": "join",
        "inputs": [
            {"kind": "external_source", "ref": "equipment"},
            {"kind": "external_source", "ref": "status"},
        ],
        "output_alias": "joined",
        "left_on": ["EQP_ID"],
        "right_on": ["EQP_ID"],
        "join_type": "left",
        "right_value_columns": ["RATE"],
    }
    if multi_match_policy:
        step["multi_match_policy"] = multi_match_policy
    return {
        "request": {"question": "현재 장비 상태", "reference_date": "20260822"},
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "equipment", "source_alias": "equipment"},
                {
                    "dataset_key": "status_snapshot",
                    "source_alias": "status",
                    "filters": {
                        "DATE": {"operator": "eq", "value": "20260822"}
                    },
                    "filter_mappings": {"DATE": ["WORK_DT"]},
                },
            ],
            "pandas_execution_plan": [step],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["EQP_ID", "RATE"],
                "strict_result_columns": False,
            },
        },
        "simple_analysis_contract": {
            "strict": True,
            "operation": "execute_typed_pandas_plan",
            "steps": [deepcopy(step)],
        },
        "runtime_sources": {
            "equipment": [{"EQP_ID": "E1"}],
            "status": deepcopy(right_rows),
        },
        "trace": {"inspection": {}},
    }


def test_reference_date_filter_prevents_cross_date_join_multiplication(executor):
    executed = executor.execute_pandas_code(
        _executor_payload(
            [
                {"EQP_ID": "E1", "WORK_DT": "20260821", "RATE": 70},
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 80},
            ]
        ),
        "",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"EQP_ID": "E1", "RATE": 80}]


def test_same_date_multiple_status_rows_are_preserved_by_default(executor):
    executed = executor.execute_pandas_code(
        _executor_payload(
            [
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 80},
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 90},
            ]
        ),
        "",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"EQP_ID": "E1", "RATE": 80},
        {"EQP_ID": "E1", "RATE": 90},
    ]


def test_explicit_first_selects_stable_first_right_row(executor):
    executed = executor.execute_pandas_code(
        _executor_payload(
            [
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 90},
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 80},
            ],
            multi_match_policy="first",
        ),
        "",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"EQP_ID": "E1", "RATE": 90}]


def test_explicit_preserve_rows_never_deduplicates_right_rows(executor):
    executed = executor.execute_pandas_code(
        _executor_payload(
            [
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 80},
                {"EQP_ID": "E1", "WORK_DT": "20260822", "RATE": 90},
            ],
            multi_match_policy="preserve_rows",
        ),
        "",
    )

    assert len(executed["data"]["rows"]) == 2
