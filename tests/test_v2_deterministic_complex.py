from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


def _metric_merge_payload() -> dict:
    return {
        "request": {"question": "compare two standardized metrics"},
        "intent_plan": {
            "intent_ir": {
                "route_source_aliases": ["left_source", "right_source"],
                "operations": ["join"],
            },
            "retrieval_jobs": [
                {"dataset_key": "left_dataset", "source_alias": "left_source"},
                {"dataset_key": "right_dataset", "source_alias": "right_source"},
            ],
            "pandas_execution_plan": [
                {
                    "operation": "join",
                    "left_source_alias": "left_source",
                    "right_source_alias": "right_source",
                }
            ],
            "resolved_metric_merge_plan": {
                "strict": True,
                "operation": "merge_metric_sources",
                "join_type": "outer",
                "fill_zero_on_success": True,
                "grain_mappings": [
                    {
                        "canonical_column": "GROUP",
                        "output_column": "GROUP",
                        "source_candidates": {
                            "left_source": ["GROUP"],
                            "right_source": ["GROUP"],
                        },
                    }
                ],
                "metrics": [
                    {
                        "source_alias": "left_source",
                        "source_candidates": ["LEFT_QTY"],
                        "output_column": "LEFT_QTY",
                        "aggregation": "sum",
                    },
                    {
                        "source_alias": "right_source",
                        "source_candidates": ["RIGHT_QTY"],
                        "output_column": "RIGHT_QTY",
                        "aggregation": "sum",
                    },
                ],
            },
            "output_contract": {
                "result_mode": "aggregate",
                "required_columns": ["GROUP", "LEFT_QTY", "RIGHT_QTY"],
                "result_columns": ["GROUP", "LEFT_QTY", "RIGHT_QTY"],
                "grain_columns": ["GROUP"],
                "metric_columns": ["LEFT_QTY", "RIGHT_QTY"],
                "strict_result_columns": True,
            },
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": "left_source",
                        "dataset_key": "left_dataset",
                        "provider": "retrieval_job",
                        "required": True,
                    },
                    {
                        "source_alias": "right_source",
                        "dataset_key": "right_dataset",
                        "provider": "retrieval_job",
                        "required": True,
                    },
                ]
            },
        },
        "runtime_sources": {
            "left_source": [
                {"GROUP": "A", "LEFT_QTY": 10},
                {"GROUP": "B", "LEFT_QTY": 20},
            ],
            "right_source": [
                {"GROUP": "A", "RIGHT_QTY": 3},
                {"GROUP": "C", "RIGHT_QTY": 7},
            ],
        },
        "source_results": [
            {
                "source_alias": "left_source",
                "dataset_key": "left_dataset",
                "status": "ok",
                "columns": ["GROUP", "LEFT_QTY"],
                "row_count": 2,
            },
            {
                "source_alias": "right_source",
                "dataset_key": "right_dataset",
                "status": "ok",
                "columns": ["GROUP", "RIGHT_QTY"],
                "row_count": 2,
            },
        ],
        "trace": {"inspection": {}},
    }


def test_complex_metric_merge_uses_deterministic_contract_without_pandas_llm():
    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    prompt_builder = load_module(V2_ROOT / "16_route_aware_pandas_prompt_builder.py")
    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    payload = _metric_merge_payload()

    resolved = resolver.resolve_simple_analysis_contract(deepcopy(payload))
    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "complex"
    assert contract["analysis_execution_mode"] == "deterministic_contract"
    assert contract["requires_pandas_llm"] is False
    assert contract["deterministic_operation"] == "merge_metric_sources"
    assert prompt_builder.build_route_aware_pandas_prompt(
        resolved,
        "{intent_plan_json}",
    ) == ""

    model_calls: list[str] = []

    def invoke(prompt: str):
        model_calls.append(prompt)
        raise AssertionError("deterministic Complex must not invoke the pandas model")

    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=invoke,
        repair_prompt_template="repair",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "complex"
    assert executed["analysis"]["analysis_execution_mode"] == "deterministic_contract"
    assert executed["analysis"]["code_generation_type"] == "deterministic_function"
    assert executed["data"]["rows"] == [
        {"GROUP": "A", "LEFT_QTY": 10, "RIGHT_QTY": 3},
        {"GROUP": "B", "LEFT_QTY": 20, "RIGHT_QTY": 0},
        {"GROUP": "C", "LEFT_QTY": 0, "RIGHT_QTY": 7},
    ]
    assert model_calls == []
    trace = executed["trace"]["inspection"]["fast_path"]
    assert trace["llm_calls"]["pandas_generation"] == 0
    assert trace["requires_pandas_llm"] is False
    assert trace["prompt_chars"]["pandas_generation"] == 0


def test_metric_merge_reconciles_unique_dataset_witnessed_model_aliases():
    """A model alias is safe to repair only when one runtime job owns its dataset."""

    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    payload = _metric_merge_payload()
    merge = payload["intent_plan"]["resolved_metric_merge_plan"]
    merge["metrics"][0].update(
        {"source_alias": "prod_df", "dataset_key": "left_dataset"}
    )
    merge["metrics"][1].update(
        {"source_alias": "wip_df", "dataset_key": "right_dataset"}
    )
    merge["grain_mappings"][0]["source_candidates"] = {
        "prod_df": ["GROUP"],
        "wip_df": ["GROUP"],
    }

    resolved = resolver.resolve_simple_analysis_contract(deepcopy(payload))

    trace = resolved["trace"]["inspection"][
        "metric_merge_runtime_alias_reconciliation"
    ]
    assert trace["status"] == "reconciled"
    assert resolved["simple_analysis_contract"]["deterministic_operation"] == "merge_metric_sources"
    assert [
        item["source_alias"]
        for item in resolved["intent_plan"]["resolved_metric_merge_plan"]["metrics"]
    ] == ["left_source", "right_source"]

    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=lambda prompt: (_ for _ in ()).throw(
            AssertionError("reconciled deterministic merge must not invoke the pandas model")
        ),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"][-1] == {
        "GROUP": "C",
        "LEFT_QTY": 0,
        "RIGHT_QTY": 7,
    }


def test_invalid_metric_merge_prefers_a_valid_typed_dag_from_runtime_aliases():
    """An unresolved merge source cannot override a complete Typed DAG."""

    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    payload = _metric_merge_payload()
    plan = payload["intent_plan"]
    plan["intent_ir"]["route_source_aliases"] = ["left_source"]
    plan["pandas_execution_plan"] = [
        {
            "node_id": "aggregate_left",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "left_source"}],
            "output_alias": "left_aggregate",
            "source_alias": "left_source",
            "group_by": ["GROUP"],
            "aggregations": [
                {"column": "LEFT_QTY", "method": "sum", "output_column": "LEFT_QTY"}
            ],
        },
        {
            "node_id": "aggregate_right",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "right_source"}],
            "output_alias": "right_aggregate",
            "source_alias": "right_source",
            "group_by": ["GROUP"],
            "aggregations": [
                {"column": "RIGHT_QTY", "method": "sum", "output_column": "RIGHT_QTY"}
            ],
        },
        {
            "node_id": "join_metrics",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "aggregate_left"},
                {"kind": "node_output", "ref": "aggregate_right"},
            ],
            "output_alias": "joined_metrics",
            "left_source_alias": "left_aggregate",
            "right_source_alias": "right_aggregate",
            "on": ["GROUP"],
            "join_type": "outer",
        },
    ]
    plan["resolved_metric_merge_plan"]["metrics"][0].update(
        {"source_alias": "unregistered_model_alias", "dataset_key": ""}
    )
    plan["resolved_metric_merge_plan"]["grain_mappings"][0]["source_candidates"] = {
        "unregistered_model_alias": ["GROUP"],
        "right_source": ["GROUP"],
    }

    resolved = resolver.resolve_simple_analysis_contract(deepcopy(payload))

    contract = resolved["simple_analysis_contract"]
    assert contract["operation"] == "execute_typed_pandas_plan"
    assert contract["fallback_from"] == "resolved_metric_merge_plan"
    assert contract["analysis_execution_mode"] == "deterministic_typed_plan"
    trace = resolved["trace"]["inspection"][
        "metric_merge_runtime_alias_reconciliation"
    ]
    assert trace["status"] == "invalid"
    assert trace["fallback"] == "execute_typed_pandas_plan"

    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=lambda prompt: (_ for _ in ()).throw(
            AssertionError("Typed fallback must not invoke the pandas model")
        ),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"GROUP": "A", "LEFT_QTY": 10, "RIGHT_QTY": 3},
        {"GROUP": "B", "LEFT_QTY": 20, "RIGHT_QTY": 0},
        {"GROUP": "C", "LEFT_QTY": 0, "RIGHT_QTY": 7},
    ]


def test_invalid_metric_merge_without_a_valid_typed_dag_does_not_execute_it():
    """Unsafe merge contracts fall back to the ordinary Complex route only."""

    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    payload = _metric_merge_payload()
    payload["intent_plan"]["resolved_metric_merge_plan"]["metrics"][0][
        "source_alias"
    ] = "unregistered_model_alias"
    payload["intent_plan"]["resolved_metric_merge_plan"]["grain_mappings"][0][
        "source_candidates"
    ] = {
        "unregistered_model_alias": ["GROUP"],
        "right_source": ["GROUP"],
    }

    resolved = resolver.resolve_simple_analysis_contract(deepcopy(payload))

    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "complex"
    assert contract["analysis_execution_mode"] == "llm_pandas"
    assert "resolved_metric_merge_plan" not in resolved["intent_plan"]
    trace = resolved["trace"]["inspection"][
        "metric_merge_runtime_alias_reconciliation"
    ]
    assert trace["fallback"] == "ordinary_complex_path"


def test_pandas_model_preview_is_bounded_without_mutating_runtime_sources():
    prompt_builder = load_module(V2_ROOT / "16_route_aware_pandas_prompt_builder.py")
    rows = [
        {**{f"COL_{index}": f"value-{row_index}-{index}" for index in range(30)}, "QTY": row_index}
        for row_index in range(5)
    ]
    payload = {
        "intent_plan": {
            "retrieval_jobs": [{"dataset_key": "sample", "source_alias": "sample"}],
            "pandas_execution_plan": [
                {
                    "operation": "custom_complex_operation",
                    "source_alias": "sample",
                    "aggregations": [
                        {"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}
                    ],
                }
            ],
            "output_contract": {
                "result_columns": ["COL_29", "QTY_SUM"],
                "metric_columns": ["QTY_SUM"],
            },
        },
        "runtime_sources": {"sample": deepcopy(rows)},
        "source_results": [
            {
                "dataset_key": "sample",
                "source_alias": "sample",
                "columns": list(rows[0]),
            }
        ],
        "simple_analysis_contract": {
            "route": "complex",
            "analysis_execution_mode": "llm_pandas",
            "requires_pandas_llm": True,
        },
    }

    variables = prompt_builder.build_variables(payload)
    import json

    preview = json.loads(variables["source_preview_json"])["sample"]
    assert len(preview) == 2
    assert all(len(row) <= 16 for row in preview)
    assert "QTY" in preview[0]
    assert payload["runtime_sources"]["sample"] == rows
