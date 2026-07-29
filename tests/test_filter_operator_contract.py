from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path) -> Any:
    name = f"test_filter_operator_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_domain_saving_normalizes_blank_predicate_and_rejects_unknown_operator():
    normalizer = load_module(
        ROOT
        / "langflow_components"
        / "domain_saving_flow"
        / "04_domain_saving_result_normalizer.py"
    )
    normalized = normalizer.normalize_authoring(
        {},
        {
            "items": [
                {
                    "section": "product_terms",
                    "key": "HBM",
                    "payload": {
                        "conditions": [
                            {
                                "column": "TSV_DIE_TYP",
                                "operator": "is_not_null_or_empty",
                                "value": "",
                            }
                        ]
                    },
                }
            ]
        },
    )

    condition = normalized["items"][0]["payload"]["conditions"][0]
    assert condition == {"column": "TSV_DIE_TYP", "operator": "not_blank"}
    assert normalized["errors"] == []

    rejected = normalizer.normalize_authoring(
        {},
        {
            "items": [
                {
                    "section": "product_terms",
                    "key": "INVALID",
                    "payload": {
                        "conditions": [
                            {
                                "column": "MODE",
                                "operator": "invented_predicate",
                            }
                        ]
                    },
                }
            ]
        },
    )
    assert rejected["errors"][0]["type"] == "unsupported_filter_operator"


def test_metadata_candidates_canonicalize_legacy_blank_operator_before_prompt():
    builder = load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "01d_metadata_candidates_builder.py"
    )
    item = {
        "section": "product_terms",
        "key": "HBM_3DS_TSV_PRODUCT",
        "payload": {
            "aliases": ["HBM", "3DS", "TSV"],
            "conditions": [
                {
                    "column": "TSV_DIE_TYP",
                    "operator": "is_not_null_or_empty",
                }
            ],
        },
    }

    sanitized = builder._sanitize_metadata_item(item, "domain")

    assert sanitized["payload"]["conditions"][0] == {
        "column": "TSV_DIE_TYP",
        "operator": "not_blank",
    }


def test_intent_normalizer_canonicalizes_blank_operator_without_changing_value_filters():
    normalizer = load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "04_intent_plan_normalizer.py"
    )
    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "HBM, POP, MOBILE 제품 조건을 적용해줘",
                "reference_date": "20260729",
            },
            "trace": {"warnings": [], "errors": [], "inspection": {}},
        },
        {
            "intent_plan": {
                "analysis_kind": "product_filter_test",
                "request_scope": "new_analysis",
                "reference_mode": "none",
                "retrieval_jobs": [
                    {
                        "dataset_key": "wip_today",
                        "source_alias": "hbm",
                        "filters": {
                            "TSV_DIE_TYP": {
                                "operator": "is_not_null_and_not_empty",
                                "value": "",
                            }
                        },
                    },
                    {
                        "dataset_key": "wip_today",
                        "source_alias": "pop_mobile",
                        "filters": {
                            "PKG2": {
                                "operator": "in",
                                "value": ["POP", "MOBILE"],
                            }
                        },
                    },
                ],
                "pandas_execution_plan": [],
                "output_contract": {},
            }
        },
        {},
    )

    jobs = normalized["intent_plan"]["retrieval_jobs"]
    assert jobs[0]["filters"]["TSV_DIE_TYP"] == {"operator": "not_blank"}
    assert jobs[1]["filters"]["PKG2"] == {
        "operator": "in",
        "value": ["POP", "MOBILE"],
    }
    trace = normalized["trace"]["inspection"]["intent"]["filter_operator_normalization"]
    assert trace["status"] == "applied"
    assert trace["changes"][0]["to"] == "not_blank"


def test_numeric_comparison_operator_contract_is_canonicalized_and_saved():
    intent_normalizer = load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "04_intent_plan_normalizer.py"
    )
    normalized = intent_normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "IN TAT 10시간 이상 LOT",
                "reference_date": "20260729",
            },
            "trace": {"warnings": [], "errors": [], "inspection": {}},
        },
        {
            "intent_plan": {
                "analysis_kind": "lot_in_tat_threshold",
                "request_scope": "new_analysis",
                "reference_mode": "none",
                "retrieval_jobs": [
                    {
                        "dataset_key": "lot_status",
                        "source_alias": "lot_status",
                        "filters": {
                            "IN_TAT": {
                                "operator": ">=",
                                "value": 10,
                            }
                        },
                    }
                ],
                "pandas_execution_plan": [],
                "output_contract": {},
            }
        },
        {},
    )

    assert normalized["intent_plan"]["retrieval_jobs"][0]["filters"]["IN_TAT"] == {
        "operator": "ge",
        "value": 10,
    }

    domain_normalizer = load_module(
        ROOT
        / "langflow_components"
        / "domain_saving_flow"
        / "04_domain_saving_result_normalizer.py"
    )
    saved = domain_normalizer.normalize_authoring(
        {},
        {
            "items": [
                {
                    "section": "analysis_recipes",
                    "key": "tat_threshold",
                    "payload": {
                        "conditions": [
                            {
                                "column": "IN_TAT",
                                "operator": "gte",
                                "value": 10,
                            }
                        ]
                    },
                }
            ]
        },
    )
    assert saved["items"][0]["payload"]["conditions"][0]["operator"] == "ge"
    assert saved["errors"] == []
