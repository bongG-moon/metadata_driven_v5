from __future__ import annotations

import pandas as pd
import pytest

from component_test_support import ROOT, load_module


EXECUTOR_PATH = (
    ROOT / "langflow_components" / "data_analysis_flow_v2" / "17_hybrid_analysis_executor.py"
)


@pytest.fixture(scope="module")
def executor():
    return load_module(EXECUTOR_PATH)


def _aggregate_step(method: str) -> dict:
    return {
        "node_id": "aggregate_metric",
        "operation": "groupby_and_aggregate",
        "inputs": [{"kind": "external_source", "ref": "source"}],
        "output_alias": "aggregated_metric",
        "group_by": ["GROUP"],
        "aggregations": [
            {
                "column": "METRIC",
                "method": method,
                "output_column": "METRIC",
            }
        ],
    }


def _display_payload(
    *,
    method: str | None = None,
    additive: bool | None = None,
) -> dict:
    job: dict = {"dataset_key": "generic_source", "source_alias": "source"}
    if additive is not None:
        job["metric_semantics"] = {
            "METRIC": {
                "additive": additive,
                "default_rollup": "sum" if additive else "mean",
                "allowed_rollups": ["sum"] if additive else ["mean", "min", "max", "median"],
            }
        }
    steps = [_aggregate_step(method)] if method else []
    return {
        "intent_plan": {
            "retrieval_jobs": [job],
            "pandas_execution_plan": steps,
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["GROUP"],
                "metric_columns": ["METRIC"],
                "required_columns": ["GROUP", "METRIC"],
                "result_columns": ["GROUP", "METRIC"],
                "strict_result_columns": True,
            },
        },
        "trace": {"inspection": {}},
    }


@pytest.mark.parametrize("method", ["mean", "median", "min", "max"])
def test_typed_non_additive_rollups_exclude_missing_observations(executor, method: str):
    frame = pd.DataFrame(
        [
            {"GROUP": "A", "METRIC": 80.0},
            {"GROUP": "A", "METRIC": None},
        ]
    )

    result = executor._typed_groupby_and_aggregate(
        frame,
        _aggregate_step(method),
        pd,
    )

    assert result.to_dict(orient="records") == [{"GROUP": "A", "METRIC": 80.0}]


@pytest.mark.parametrize("method", ["mean", "median", "min", "max"])
def test_typed_non_additive_rollups_keep_all_missing_group_missing(executor, method: str):
    frame = pd.DataFrame(
        [
            {"GROUP": "A", "METRIC": None},
            {"GROUP": "A", "METRIC": None},
        ]
    )

    result = executor._typed_groupby_and_aggregate(
        frame,
        _aggregate_step(method),
        pd,
    )

    assert pd.isna(result.loc[0, "METRIC"])


def test_typed_sum_keeps_legacy_zero_semantics_for_all_missing_group(executor):
    frame = pd.DataFrame(
        [
            {"GROUP": "A", "METRIC": None},
            {"GROUP": "A", "METRIC": None},
        ]
    )

    result = executor._typed_groupby_and_aggregate(
        frame,
        _aggregate_step("sum"),
        pd,
    )

    assert result.to_dict(orient="records") == [{"GROUP": "A", "METRIC": 0.0}]


def test_actual_mean_preserves_missing_final_metric_even_when_catalog_is_additive(executor):
    payload = _display_payload(method="mean", additive=True)
    frame = pd.DataFrame([{"GROUP": "A", "METRIC": None}])
    rows = [{"GROUP": "A", "METRIC": None}]

    normalized_frame = executor._zero_fill_declared_metric_frame_values(frame, payload)
    normalized_rows = executor._normalize_missing_metric_values(rows, payload)

    assert pd.isna(normalized_frame.loc[0, "METRIC"])
    assert normalized_rows == [{"GROUP": "A", "METRIC": None}]


def test_non_additive_catalog_metric_preserves_missing_final_value(executor):
    payload = _display_payload(additive=False)
    frame = pd.DataFrame([{"GROUP": "A", "METRIC": None}])
    rows = [{"GROUP": "A", "METRIC": None}]

    normalized_frame = executor._zero_fill_declared_metric_frame_values(frame, payload)
    normalized_rows = executor._normalize_missing_metric_values(rows, payload)

    assert pd.isna(normalized_frame.loc[0, "METRIC"])
    assert normalized_rows == [{"GROUP": "A", "METRIC": None}]


def test_additive_sum_and_uncontracted_legacy_metric_still_zero_fill(executor):
    for payload in (
        _display_payload(method="sum", additive=True),
        _display_payload(),
    ):
        frame = pd.DataFrame([{"GROUP": "A", "METRIC": None}])
        rows = [{"GROUP": "A", "METRIC": None}]

        normalized_frame = executor._zero_fill_declared_metric_frame_values(frame, payload)
        normalized_rows = executor._normalize_missing_metric_values(rows, payload)

        assert normalized_frame.loc[0, "METRIC"] == 0
        assert normalized_rows == [{"GROUP": "A", "METRIC": 0}]


def test_same_physical_alias_does_not_leak_non_additive_policy_across_sources(executor):
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "rate_dataset",
                    "source_alias": "rate_source",
                    "standard_column_aliases": {"RATE": ["VALUE"]},
                    "metric_semantics": {
                        "RATE": {
                            "additive": False,
                            "default_rollup": "mean",
                            "allowed_rollups": ["mean"],
                        }
                    },
                },
                {
                    "dataset_key": "quantity_dataset",
                    "source_alias": "quantity_source",
                    "standard_column_aliases": {"QTY": ["VALUE"]},
                    "metric_semantics": {
                        "QTY": {
                            "additive": True,
                            "default_rollup": "sum",
                            "allowed_rollups": ["sum"],
                        }
                    },
                },
            ],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["GROUP"],
                "metric_columns": ["RATE_RESULT", "QTY_RESULT"],
                "result_columns": ["GROUP", "RATE_RESULT", "QTY_RESULT"],
                "metric_bindings": [
                    {
                        "source_alias": "rate_source",
                        "dataset_key": "rate_dataset",
                        "source_column": "VALUE",
                        "output_column": "RATE_RESULT",
                        "aggregation": "mean",
                    },
                    {
                        "source_alias": "quantity_source",
                        "dataset_key": "quantity_dataset",
                        "source_column": "VALUE",
                        "output_column": "QTY_RESULT",
                        "aggregation": "sum",
                    },
                ],
            },
        },
        "trace": {"inspection": {}},
    }
    frame = pd.DataFrame(
        [{"GROUP": "A", "RATE_RESULT": None, "QTY_RESULT": None}]
    )
    rows = [{"GROUP": "A", "RATE_RESULT": None, "QTY_RESULT": None}]

    preserved = executor._metric_columns_preserving_missing_values(payload)
    normalized_frame = executor._zero_fill_declared_metric_frame_values(frame, payload)
    normalized_rows = executor._normalize_missing_metric_values(rows, payload)

    assert "rate_result" in preserved
    assert "value" not in preserved
    assert "qty" not in preserved
    assert "qty_result" not in preserved
    assert pd.isna(normalized_frame.loc[0, "RATE_RESULT"])
    assert normalized_frame.loc[0, "QTY_RESULT"] == 0
    assert normalized_rows == [
        {"GROUP": "A", "RATE_RESULT": None, "QTY_RESULT": 0}
    ]
