from __future__ import annotations

from copy import deepcopy

from test_langflow_components import ROOT, load_module


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
    prompt_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "15_pandas_variables_builder.py"
    )
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


def test_pandas_model_preview_is_bounded_without_mutating_runtime_sources():
    prompt_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "15_pandas_variables_builder.py"
    )
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

