from __future__ import annotations

from copy import deepcopy
import json

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"
INTENT_NORMALIZER_PATH = V2_ROOT / "04_intent_plan_normalizer.py"
DOMAIN_NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "domain_saving_flow"
    / "04_domain_saving_result_normalizer.py"
)


def _modules():
    return (
        load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py"),
        load_module(V2_ROOT / "17_hybrid_analysis_executor.py"),
    )


def _formula_payload() -> dict:
    rows = [
        {"GROUP": "A", "EQP_ID": "EQP-01", "UPH": 10},
        {"GROUP": "A", "EQP_ID": "EQP-01", "UPH": 14},
        {"GROUP": "A", "EQP_ID": "EQP-02", "UPH": 20},
        {"GROUP": "B", "EQP_ID": "EQP-03", "UPH": 5},
    ]
    return {
        "request": {"question": "generic grouped derived metric"},
        "intent_plan": {
            "intent_ir": {
                "route_source_aliases": ["source"],
                "operations": ["groupby_and_aggregate", "derive_formula"],
            },
            "retrieval_jobs": [
                {
                    "dataset_key": "generic_equipment",
                    "source_alias": "source",
                    "filters": {},
                    "filter_mappings": {
                        "GROUP": ["GROUP"],
                        "EQP_ID": ["EQP_ID"],
                        "UPH": ["UPH"],
                    },
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "aggregate_by_group",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "external_source", "ref": "source"}],
                    "output_alias": "grouped",
                    "source_alias": "source",
                    "group_by": ["GROUP"],
                    "aggregations": [
                        {
                            "column": "EQP_ID",
                            "method": "nunique",
                            "output_column": "EQP_COUNT",
                        },
                        {
                            "column": "UPH",
                            "method": "mean",
                            "output_column": "AVG_UPH",
                        },
                    ],
                },
                {
                    "node_id": "derive_capacity",
                    "operation": "derive_formula",
                    "inputs": [{"kind": "node_output", "ref": "grouped"}],
                    "output_alias": "with_capacity",
                    "formula": {
                        "output_column": "AVAILABLE_CAPA",
                        "operator": "multiply",
                        "operands": [
                            {"column": "EQP_COUNT"},
                            {"column": "AVG_UPH"},
                            {"constant": 24},
                        ],
                        "null_policy": "propagate",
                        "round_digits": 1,
                    },
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["GROUP"],
                "metric_columns": ["EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
                "required_columns": [
                    "GROUP",
                    "EQP_COUNT",
                    "AVG_UPH",
                    "AVAILABLE_CAPA",
                ],
                "result_columns": [
                    "GROUP",
                    "EQP_COUNT",
                    "AVG_UPH",
                    "AVAILABLE_CAPA",
                ],
                "strict_result_columns": True,
                "metric_null_policy": "display_zero",
            },
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "provider": "retrieval_job",
                        "source_alias": "source",
                        "dataset_key": "generic_equipment",
                        "required": True,
                    }
                ]
            },
        },
        "runtime_sources": {"source": deepcopy(rows)},
        "source_results": [
            {
                "source_alias": "source",
                "dataset_key": "generic_equipment",
                "status": "ok",
                "row_count": len(rows),
                "columns": ["GROUP", "EQP_ID", "UPH"],
            }
        ],
        "trace": {"inspection": {}},
    }


def test_typed_formula_executes_safe_declared_arithmetic_without_pandas_llm():
    """A formula contract uses only explicit operands and stays deterministic."""

    resolver, executor = _modules()
    resolved = resolver.resolve_simple_analysis_contract(_formula_payload())

    contract = resolved["simple_analysis_contract"]
    assert contract["operation"] == "execute_typed_pandas_plan"
    assert contract["analysis_execution_mode"] == "deterministic_typed_plan"
    assert contract["requires_pandas_llm"] is False

    model_calls: list[str] = []

    def invoke(prompt: str):
        model_calls.append(prompt)
        raise AssertionError("safe Typed formula execution must not call the pandas LLM")

    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused deterministic prompt",
        model_invoker=invoke,
        repair_prompt_template="repair",
    )

    assert model_calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_mode"] == "execute_typed_pandas_plan"
    assert executed["data"]["columns"] == [
        "GROUP",
        "EQP_COUNT",
        "AVG_UPH",
        "AVAILABLE_CAPA",
    ]
    assert executed["data"]["rows"] == [
        {
            "GROUP": "A",
            "EQP_COUNT": 2,
            "AVG_UPH": 14.666666666666666,
            "AVAILABLE_CAPA": 704.0,
        },
        {
            "GROUP": "B",
            "EQP_COUNT": 1,
            "AVG_UPH": 5.0,
            "AVAILABLE_CAPA": 120.0,
        },
    ]


def test_invalid_formula_keeps_existing_complex_fallback_instead_of_blocking():
    """Unsupported expression-like payloads never enter the Typed executor."""

    resolver, _ = _modules()
    payload = _formula_payload()
    formula = payload["intent_plan"]["pandas_execution_plan"][1]["formula"]
    formula.pop("operands")
    formula["expression"] = "EQP_COUNT * AVG_UPH * 24"

    resolved = resolver.resolve_simple_analysis_contract(payload)

    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "complex"
    assert contract["analysis_execution_mode"] == "llm_pandas"
    assert contract["requires_pandas_llm"] is True
    assert contract.get("operation") != "execute_typed_pandas_plan"
    assert resolved.get("execution_gate", {}).get("status") != "blocked"


def test_selected_recipe_formula_is_materialized_only_for_proven_terminal_aggregate():
    """A selected metadata formula can fill an omitted step without guessing."""

    normalizer = load_module(INTENT_NORMALIZER_PATH)
    aggregate = deepcopy(_formula_payload()["intent_plan"]["pandas_execution_plan"][0])
    plan = [
        aggregate,
        {
            "node_id": "sort_capacity",
            "operation": "sort_and_top_n",
            "inputs": [{"kind": "node_output", "ref": "grouped"}],
            "output_alias": "sorted",
            "sort_by": "AVAILABLE_CAPA",
            "order": "desc",
            "limit": 0,
        },
    ]
    candidates = {
        "domain_items": [
            {
                "section": "analysis_recipes",
                "key": "generic_capacity",
                "payload": {
                    "derived_metrics": [
                        {
                            "output_column": "AVAILABLE_CAPA",
                            "operator": "multiply",
                            "operands": [
                                {"column": "EQP_COUNT"},
                                {"column": "AVG_UPH"},
                                {"constant": 24},
                            ],
                            "null_policy": "propagate",
                        }
                    ]
                },
            }
        ]
    }

    materialized, trace = normalizer._materialize_selected_recipe_derived_formulas(
        plan,
        candidates,
        [{"section": "analysis_recipes", "key": "generic_capacity"}],
        {
            "result_columns": ["GROUP", "EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
            "required_columns": ["GROUP", "EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
        },
    )

    assert trace["status"] == "applied"
    assert [step["operation"] for step in materialized] == [
        "groupby_and_aggregate",
        "derive_formula",
        "sort_and_top_n",
    ]
    assert materialized[1]["formula"]["output_column"] == "AVAILABLE_CAPA"
    assert materialized[2]["inputs"] == [
        {"kind": "node_output", "ref": materialized[1]["output_alias"]}
    ]


def test_normalizer_materializes_selected_formula_recipe_without_changing_other_plans():
    """The public normalizer uses a selected alias, not a dataset-specific rule."""

    normalizer = load_module(INTENT_NORMALIZER_PATH)
    aggregate = deepcopy(_formula_payload()["intent_plan"]["pandas_execution_plan"][0])
    response = {
        "intent_plan": {
            "analysis_kind": "generic_capacity",
            "request_scope": "new_analysis",
            "metadata_refs": [
                {"section": "analysis_recipes", "key": "generic_capacity"}
            ],
            "retrieval_jobs": [
                {
                    "dataset_key": "generic_equipment",
                    "source_alias": "source",
                    "filters": {},
                }
            ],
            "pandas_execution_plan": [
                aggregate,
                {
                    "node_id": "sort_capacity",
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "node_output", "ref": "grouped"}],
                    "output_alias": "sorted",
                    "sort_by": "AVAILABLE_CAPA",
                    "order": "desc",
                    "limit": 0,
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["GROUP"],
                "metric_columns": ["EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
                "required_columns": ["GROUP", "EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
                "result_columns": ["GROUP", "EQP_COUNT", "AVG_UPH", "AVAILABLE_CAPA"],
                "strict_result_columns": True,
            },
        }
    }
    candidates = {
        "domain_items": [
            {
                "section": "analysis_recipes",
                "key": "generic_capacity",
                "payload": {
                    "aliases": ["generic capacity"],
                    "derived_metrics": [
                        {
                            "output_column": "AVAILABLE_CAPA",
                            "operator": "multiply",
                            "operands": [
                                {"column": "EQP_COUNT"},
                                {"column": "AVG_UPH"},
                                {"constant": 24},
                            ],
                            "null_policy": "propagate",
                        }
                    ],
                },
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "generic_equipment",
                "source_type": "dummy",
                "columns": ["GROUP", "EQP_ID", "UPH"],
                "filter_mappings": {
                    "GROUP": ["GROUP"],
                    "EQP_ID": ["EQP_ID"],
                    "UPH": ["UPH"],
                },
            }
        ],
        "main_flow_filters": [],
    }

    normalized = normalizer.normalize_intent_plan(
        {
            "request": {"question": "generic capacity", "reference_date": "20260820"},
            "trace": {"inspection": {}, "warnings": [], "errors": []},
        },
        json.dumps(response),
        {"metadata_candidates": candidates},
    )

    steps = normalized["intent_plan"]["pandas_execution_plan"]
    assert [step["operation"] for step in steps] == [
        "groupby_and_aggregate",
        "derive_formula",
        "sort_and_top_n",
    ]
    assert normalized["trace"]["inspection"]["intent"][
        "derived_formula_materialization"
    ]["status"] == "applied"
    normalized["runtime_sources"] = {
        "source": deepcopy(_formula_payload()["runtime_sources"]["source"])
    }
    normalized["source_results"] = deepcopy(_formula_payload()["source_results"])
    resolver, executor = _modules()
    resolved = resolver.resolve_simple_analysis_contract(normalized)
    assert resolved["simple_analysis_contract"]["operation"] == "execute_typed_pandas_plan"
    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused deterministic prompt",
        model_invoker=lambda prompt: (_ for _ in ()).throw(
            AssertionError("metadata-owned formula must not call pandas LLM")
        ),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["columns"] == [
        "GROUP",
        "EQP_COUNT",
        "AVG_UPH",
        "AVAILABLE_CAPA",
    ]


def test_domain_saving_accepts_safe_formula_contract_and_rejects_expression_payloads():
    """Authoring persists declarative operands only; executable strings are refused."""

    normalizer = load_module(DOMAIN_NORMALIZER_PATH)
    base_payload = {"request": {"raw_text": "derived metric registration"}, "refinement": {}}
    valid = normalizer.normalize_authoring(
        base_payload,
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "generic_available_capacity",
                    "payload": {
                        "derived_metrics": [
                            {
                                "output_column": "AVAILABLE_CAPA",
                                "operator": "MULTIPLY",
                                "operands": [
                                    {"column": "EQP_COUNT"},
                                    {"column": "AVG_UPH"},
                                    {"constant": 24},
                                ],
                                "null_policy": "propagate",
                                "round_digits": 1,
                            }
                        ]
                    },
                }
            ]
        },
    )

    assert valid["errors"] == []
    assert valid["items"][0]["payload"]["derived_metrics"] == [
        {
            "output_column": "AVAILABLE_CAPA",
            "operator": "multiply",
            "operands": [
                {"column": "EQP_COUNT"},
                {"column": "AVG_UPH"},
                {"constant": 24},
            ],
            "null_policy": "propagate",
            "round_digits": 1,
        }
    ]

    unsafe = normalizer.normalize_authoring(
        base_payload,
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "unsafe_expression",
                    "payload": {
                        "derived_metrics": [
                            {
                                "output_column": "AVAILABLE_CAPA",
                                "operator": "multiply",
                                "operands": [
                                    {"column": "EQP_COUNT"},
                                    {"expression": "__import__('os').system('whoami')"},
                                ],
                            }
                        ]
                    },
                }
            ]
        },
    )

    assert any(error["type"] == "invalid_derived_metric_operand" for error in unsafe["errors"])
