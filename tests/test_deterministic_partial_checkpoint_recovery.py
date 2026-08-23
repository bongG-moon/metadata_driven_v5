from __future__ import annotations

from copy import deepcopy

import pytest

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


@pytest.fixture(scope="module")
def executor():
    return load_module(V2_ROOT / "17_hybrid_analysis_executor.py")


def _payload(
    *,
    rows: list[dict],
    deterministic_contract: dict,
    output_contract: dict,
) -> dict:
    columns = list(dict.fromkeys(column for row in rows for column in row))
    return {
        "question": "generic deterministic analysis request",
        "execution_gate": {"status": "continue"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "generic_source",
                    "source_alias": "source",
                    "filters": {},
                    "filter_mappings": {
                        column: [column] for column in columns
                    },
                    "standard_column_aliases": {},
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": deepcopy(output_contract),
        },
        "runtime_sources": {"source": deepcopy(rows)},
        "source_results": [
            {
                "dataset_key": "generic_source",
                "source_alias": "source",
                "status": "ok",
                "row_count": len(rows),
                "columns": columns,
            }
        ],
        "simple_analysis_contract": deepcopy(deterministic_contract),
        "trace": {"inspection": {}},
    }


def test_fast_aggregate_contract_failure_recovers_computed_checkpoint(executor):
    """A post-aggregate contract defect must not discard the valid aggregate."""

    payload = _payload(
        rows=[
            {"GROUP": "A", "QTY": 2},
            {"GROUP": "A", "QTY": 3},
            {"GROUP": "B", "QTY": 7},
        ],
        deterministic_contract={
            "strict": True,
            "route": "fast",
            "operation": "execute_fast_path_recipe",
            "recipe": "ranked_summary",
            "source_alias": "source",
            "dataset_key": "generic_source",
            "filters": [],
            "group_by": ["GROUP"],
            "metrics": [
                {
                    "source_column": "QTY",
                    "output_column": "QTY_SUM",
                    "aggregation": "sum",
                }
            ],
            # The aggregate is valid. Only the later presentation/order
            # contract refers to a stale column.
            "ordering": [{"column": "MISSING_ORDER", "direction": "desc"}],
            "result_columns": ["GROUP", "QTY_SUM", "MISSING_ORDER"],
            "limit": 0,
            "tie_policy": "first_n",
            "null_policy": {
                "dimensions": "preserve_as_blank",
                "metrics": "display_zero",
            },
            "calculation": {"max_pivot_columns": 50},
        },
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["GROUP", "QTY_SUM"],
            "strict_result_columns": True,
        },
    )

    executed = executor.execute_pandas_code(payload, "")

    assert executed["analysis"]["status"] == "partial"
    assert executed["analysis"]["error"]["type"] == "output_contract_violation"
    assert executed["analysis"]["recovered_result"]["checkpoint_role"] in {
        "computed_result",
        "step_output",
    }
    assert executed["data"]["partial"] is True
    assert executed["data"]["columns"] == ["GROUP", "QTY_SUM"]
    assert executed["data"]["rows"] == [
        {"GROUP": "A", "QTY_SUM": 5},
        {"GROUP": "B", "QTY_SUM": 7},
    ]


def test_typed_late_sort_failure_recovers_completed_aggregate_step(executor):
    """A later Typed step failure must publish the last completed Typed frame."""

    payload = _payload(
        rows=[
            {"GROUP": "A", "QTY": 2},
            {"GROUP": "A", "QTY": 3},
            {"GROUP": "B", "QTY": 7},
        ],
        deterministic_contract={
            "strict": True,
            "route": "complex",
            "operation": "execute_typed_pandas_plan",
            "analysis_execution_mode": "deterministic_typed_plan",
            "steps": [
                {
                    "node_id": "aggregate_qty",
                    "output_alias": "aggregate_qty",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "external_source", "ref": "source"}],
                    "group_by": ["GROUP"],
                    "aggregations": [
                        {
                            "column": "QTY",
                            "method": "sum",
                            "output_column": "QTY_SUM",
                        }
                    ],
                },
                {
                    "node_id": "bad_sort",
                    "output_alias": "bad_sort",
                    "operation": "sort_and_top_n",
                    "inputs": [
                        {"kind": "node_output", "ref": "aggregate_qty"}
                    ],
                    "sort_by": "MISSING_ORDER",
                    "order": "desc",
                    "limit": 10,
                },
            ],
        },
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["GROUP", "QTY_SUM"],
            "strict_result_columns": True,
        },
    )

    executed = executor.execute_pandas_code(payload, "")

    assert executed["analysis"]["status"] == "partial"
    assert executed["analysis"]["error"]["type"] == "output_contract_violation"
    assert executed["analysis"]["recovered_result"]["checkpoint_role"] == "step_output"
    assert executed["data"]["partial"] is True
    assert executed["data"]["columns"] == ["GROUP", "QTY_SUM"]
    assert executed["data"]["rows"] == [
        {"GROUP": "A", "QTY_SUM": 5},
        {"GROUP": "B", "QTY_SUM": 7},
    ]


def test_failure_before_safe_checkpoint_does_not_expose_raw_source_as_partial(executor):
    """A raw retrieval frame alone is audit evidence, not a computed result."""

    payload = _payload(
        rows=[{"GROUP": "A", "QTY": 2}],
        deterministic_contract={
            "strict": True,
            "route": "fast",
            "operation": "execute_fast_path_recipe",
            "recipe": "group_summary",
            "source_alias": "source",
            "dataset_key": "generic_source",
            "filters": [
                {
                    "canonical_field": "MISSING_FILTER_COLUMN",
                    "operator": "eq",
                    "typed_values": ["A"],
                    "value_type": "string",
                    "execution_stage": "post_retrieval",
                }
            ],
            "group_by": ["GROUP"],
            "metrics": [
                {
                    "source_column": "QTY",
                    "output_column": "QTY_SUM",
                    "aggregation": "sum",
                }
            ],
            "result_columns": ["GROUP", "QTY_SUM"],
            "limit": 0,
        },
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["GROUP", "QTY_SUM"],
            "strict_result_columns": True,
        },
    )

    executed = executor.execute_pandas_code(payload, "")

    assert executed["analysis"]["status"] == "error"
    assert not executed["analysis"].get("recovered_result")
    assert not executed.get("data", {}).get("partial")
    assert not executed.get("data", {}).get("rows")
    assert all(
        checkpoint.get("role") == "source_input"
        for checkpoint in executed.get("intermediate_results", [])
    )
