from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path):
    name = f"_semantic_test_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def base_payload() -> dict:
    return {
        "intent_plan": {
            "analysis_kind": "model_generated_label",
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_source",
                    "required_params": {"DATE": "20260803"},
                    "filters": {
                        "OPER_NAME": {"operator": "eq", "value": "INPUT"}
                    },
                    "metric_semantics": {
                        "PRODUCTION": {
                            "additive": True,
                            "default_rollup": "sum",
                            "allowed_rollups": ["sum", "mean"],
                        }
                    },
                }
            ],
            "pandas_execution_plan": [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "production_source",
                    "group_by": ["PRODUCT"],
                    "aggregations": [
                        {
                            "column": "PRODUCTION",
                            "method": "sum",
                            "output_column": "TOTAL_PRODUCTION",
                        }
                    ],
                }
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "required_columns": ["PRODUCT", "TOTAL_PRODUCTION"],
                "grain_columns": ["PRODUCT"],
                "metric_columns": ["TOTAL_PRODUCTION"],
                "metric_bindings": [
                    {
                        "dataset_key": "production_today",
                        "source_alias": "production_source",
                        "source_column": "PRODUCTION",
                        "aggregation": "sum",
                        "output_column": "TOTAL_PRODUCTION",
                    }
                ],
                "strict_result_columns": True,
            },
        },
        "analysis": {
            "status": "ok",
            "row_count": 2,
            "columns": ["PRODUCT", "TOTAL_PRODUCTION"],
        },
        "data": {
            "rows": [
                {"PRODUCT": "B", "TOTAL_PRODUCTION": 20},
                {"PRODUCT": "A", "TOTAL_PRODUCTION": 10},
            ]
        },
    }


def test_validation_profile_auto_uses_semantic_live_for_llm():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")

    assert validator.resolve_validation_profile("auto", use_llm=True) == "semantic_live"
    assert validator.resolve_validation_profile("auto", use_llm=False) == "fixture_exact"


def test_semantic_contract_accepts_output_alias_and_ignores_analysis_kind():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()

    result = validator.validate_semantic_payload(payload)

    assert result["status"] == "ok"
    assert result["errors"] == []


def test_unordered_fixture_rows_are_diagnostics_in_semantic_live_profile():
    representative = load_module(ROOT / "tools" / "validate_representative_questions.py")
    payload = base_payload()
    case = {
        "id": 1,
        "question": "input production",
        "min_rows": 1,
        "required_columns": ["PRODUCT", "PRODUCTION_SUM"],
        "expected_row_count": 2,
        "expected_first_row": {"PRODUCT": "A", "PRODUCTION_SUM": 10},
        "intent_response": {
            "intent_plan": {
                "analysis_kind": "fixture_label",
                "retrieval_jobs": [
                    {
                        "dataset_key": "production_today",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "INPUT"}
                        },
                    }
                ],
            }
        },
    }

    result = representative.summarize_validation_result(
        case,
        payload,
        {},
        validation_profile="semantic_live",
        api_response={"data_mode": "dummy"},
    )

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert any("analysis_kind" in item for item in result["fixture_differences"])
    assert any("first row" in item for item in result["fixture_differences"])


def test_fixture_exact_profile_keeps_exact_regression_comparison():
    representative = load_module(ROOT / "tools" / "validate_representative_questions.py")
    payload = base_payload()
    case = {
        "id": 1,
        "question": "input production",
        "min_rows": 1,
        "required_columns": ["PRODUCT", "PRODUCTION_SUM"],
        "expected_first_row": {"PRODUCT": "A", "PRODUCTION_SUM": 10},
        "intent_response": {
            "intent_plan": {
                "analysis_kind": "fixture_label",
                "retrieval_jobs": [{"dataset_key": "production_today"}],
            }
        },
    }

    result = representative.summarize_validation_result(
        case,
        payload,
        {},
        validation_profile="fixture_exact",
        api_response={"data_mode": "dummy"},
    )

    assert result["status"] == "error"
    assert any("analysis_kind" in item for item in result["errors"])
    assert any("first row" in item for item in result["errors"])


def test_missing_expected_dataset_or_filter_is_a_semantic_error():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    case = {
        "intent_response": {
            "intent_plan": {
                "retrieval_jobs": [
                    {
                        "dataset_key": "wip_today",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "D/A1"}
                        },
                    }
                ]
            }
        }
    }

    errors = validator.validate_case_expectation(case, payload)

    assert [item["type"] for item in errors] == ["missing_expected_dataset"]


def test_single_value_in_filter_is_equivalent_to_eq_filter():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    payload["intent_plan"]["retrieval_jobs"][0]["filters"]["OPER_NAME"] = {
        "operator": "in",
        "value": ["INPUT"],
    }
    case = {
        "intent_response": {
            "intent_plan": {
                "retrieval_jobs": [
                    {
                        "dataset_key": "production_today",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "INPUT"}
                        },
                    }
                ]
            }
        }
    }

    assert validator.validate_case_expectation(case, payload) == []


def test_temporal_query_date_mismatch_is_a_semantic_error():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    payload["intent_plan"]["temporal_semantics"] = [
        {
            "dataset_key": "production_today",
            "source_alias": "production_source",
            "date_param": "DATE",
            "query_date": "20260802",
        }
    ]

    result = validator.validate_semantic_payload(payload)

    assert [item["type"] for item in result["errors"]] == [
        "temporal_query_date_mismatch"
    ]


def test_metric_rollup_uses_generic_trusted_metadata_contract():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    job = payload["intent_plan"]["retrieval_jobs"][0]
    job["metric_semantics"]["PRODUCTION"] = {
        "additive": False,
        "allowed_rollups": ["mean", "min", "max"],
    }

    result = validator.validate_semantic_payload(payload)

    issue = next(item for item in result["errors"] if item["type"] == "non_additive_metric_sum")
    assert issue["source_column"] == "PRODUCTION"


def test_unregistered_non_sum_rollup_is_advisory_for_lower_capability_models():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    job = payload["intent_plan"]["retrieval_jobs"][0]
    job["metric_semantics"]["PRODUCTION"] = {
        "additive": False,
        "allowed_rollups": ["nunique"],
    }
    binding = payload["intent_plan"]["output_contract"]["metric_bindings"][0]
    binding["aggregation"] = "collect_unique"

    result = validator.validate_semantic_payload(payload)

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert [item["type"] for item in result["warnings"]] == [
        "metric_aggregation_not_allowed"
    ]


def test_metric_binding_accepts_unambiguous_intermediate_lineage():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    payload["intent_plan"]["pandas_execution_plan"] = [
        {
            "node_id": "aggregate_production",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "production_source"}],
            "output_alias": "production_by_product",
        }
    ]
    binding = payload["intent_plan"]["output_contract"]["metric_bindings"][0]
    binding["source_alias"] = "production_by_product"

    result = validator.validate_semantic_payload(payload)

    assert result["status"] == "ok"
    assert result["errors"] == []


def test_plan_style_differences_are_advisory_not_failures():
    validator = load_module(ROOT / "tools" / "data_analysis_semantic_validator.py")
    payload = base_payload()
    payload["intent_plan"]["pandas_function_cases"] = [
        {
            "key": "generic_matcher",
            "function_name": "match_tokens",
            "source_alias": "production_source",
        }
    ]
    payload["intent_plan"]["output_contract"]["ordering"] = {
        "sort_by": "TOTAL_PRODUCTION",
        "order": "asc",
    }

    result = validator.validate_semantic_payload(payload, pandas_variables={})

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert {item["type"] for item in result["warnings"]} == {
        "function_case_not_executed",
        "result_order_violation",
    }
