from __future__ import annotations

from copy import deepcopy

import pytest

from component_test_support import ROOT, load_module


EXECUTOR_PATH = (
    ROOT / "langflow_components" / "data_analysis_flow_v2" / "17_hybrid_analysis_executor.py"
)


@pytest.fixture(scope="module")
def executor():
    return load_module(EXECUTOR_PATH)


def _typed_metric_payload(*, method: str, with_semantics: bool = True) -> dict:
    step = {
        "node_id": "aggregate_rate",
        "operation": "groupby_and_aggregate",
        "inputs": [{"kind": "external_source", "ref": "rate_source"}],
        "output_alias": "rate_result",
        "group_by": ["OPER_NAME"],
        "aggregations": [
            {
                "column": "TOTAL_INTERVAL_RATE",
                "method": method,
                "output_column": "TOTAL_INTERVAL_RATE",
            }
        ],
    }
    job = {
        "dataset_key": "generic_rate_source",
        "source_alias": "rate_source",
    }
    if with_semantics:
        job["metric_semantics"] = {
            "TOTAL_INTERVAL_RATE": {
                "semantic_type": "rate",
                "additive": False,
                "default_rollup": "mean",
                "allowed_rollups": ["mean"],
            }
        }
    return {
        "intent_plan": {
            "retrieval_jobs": [job],
            "pandas_execution_plan": [deepcopy(step)],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["OPER_NAME"],
                "metric_columns": ["TOTAL_INTERVAL_RATE"],
                "required_columns": ["OPER_NAME", "TOTAL_INTERVAL_RATE"],
                "result_columns": ["OPER_NAME", "TOTAL_INTERVAL_RATE"],
                "strict_result_columns": True,
            },
        },
        "simple_analysis_contract": {
            "strict": True,
            "route": "complex",
            "operation": "execute_typed_pandas_plan",
            "steps": [deepcopy(step)],
            "result_columns": ["OPER_NAME", "TOTAL_INTERVAL_RATE"],
        },
        "runtime_sources": {
            "rate_source": [
                {"OPER_NAME": "P1", "TOTAL_INTERVAL_RATE": 60.0},
                {"OPER_NAME": "P1", "TOTAL_INTERVAL_RATE": 90.0},
            ]
        },
        "trace": {"inspection": {}},
    }


def test_deterministic_typed_sum_rejects_explicit_non_additive_metric_contract(executor):
    executed = executor.execute_pandas_code(
        _typed_metric_payload(method="sum"),
        "",
    )

    assert executed["analysis"]["status"] == "error"
    assert executed["analysis"]["error"]["type"] == "output_contract_violation"
    assert "TOTAL_INTERVAL_RATE" in executed["analysis"]["error"]["message"]
    assert "sum" in executed["analysis"]["error"]["message"]


def test_deterministic_guard_validates_the_executed_contract_not_a_stale_plan(executor):
    unsafe = _typed_metric_payload(method="mean")
    unsafe["simple_analysis_contract"]["steps"][0]["aggregations"][0][
        "method"
    ] = "sum"
    blocked = executor.execute_pandas_code(unsafe, "")

    assert blocked["analysis"]["status"] == "error"
    assert blocked["analysis"]["error"]["type"] == "output_contract_violation"

    safe = _typed_metric_payload(method="sum")
    safe["simple_analysis_contract"]["steps"][0]["aggregations"][0][
        "method"
    ] = "mean"
    executed = executor.execute_pandas_code(safe, "")

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "P1", "TOTAL_INTERVAL_RATE": 75.0}
    ]


def test_deterministic_typed_mean_accepts_explicit_non_additive_metric_contract(executor):
    executed = executor.execute_pandas_code(
        _typed_metric_payload(method="mean"),
        "",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "P1", "TOTAL_INTERVAL_RATE": 75.0}
    ]


def test_deterministic_metric_without_semantics_preserves_existing_execution(executor):
    executed = executor.execute_pandas_code(
        _typed_metric_payload(method="sum", with_semantics=False),
        "",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "P1", "TOTAL_INTERVAL_RATE": 150.0}
    ]
