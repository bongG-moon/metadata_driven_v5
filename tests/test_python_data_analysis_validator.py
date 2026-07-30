from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path):
    name = f"_test_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_metadata_context_passes_env_config_to_standalone_loaders(monkeypatch):
    validator = load_module(ROOT / "tools" / "validate_representative_questions.py")
    calls = {}

    class Loader:
        def __init__(self, name, result_key):
            self.name = name
            self.result_key = result_key

        def __getattr__(self, method_name):
            def load(**kwargs):
                calls[self.name] = {"method": method_name, **kwargs}
                return {
                    self.result_key: [{"key": self.name}],
                    "metadata_load": {"status": "ok", "errors": []},
                }

            return load

    monkeypatch.setenv("MONGODB_URI", "mongodb://validator.example")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov_test")
    monkeypatch.setenv("MONGODB_DOMAIN_COLLECTION", "domain_test")
    monkeypatch.setenv("MONGODB_TABLE_CATALOG_COLLECTION", "table_test")
    monkeypatch.setenv("MONGODB_MAIN_FLOW_FILTER_COLLECTION", "main_test")
    monkeypatch.setenv("VALIDATION_METADATA_LIMIT", "123")

    result = validator.load_metadata_context(
        {
            "domain_loader": Loader("domain", "domain_items"),
            "table_loader": Loader("table", "table_catalog_items"),
            "main_loader": Loader("main", "main_flow_filters"),
        }
    )

    assert result["domain"]["domain_items"] == [{"key": "domain"}]
    assert calls["domain"]["mongo_uri"] == "mongodb://validator.example"
    assert calls["domain"]["mongo_database"] == "datagov_test"
    assert calls["domain"]["collection_name"] == "domain_test"
    assert calls["table"]["collection_name"] == "table_test"
    assert calls["main"]["collection_name"] == "main_test"
    assert {item["limit"] for item in calls.values()} == {"123"}


def test_environment_summary_does_not_expose_secret_values(monkeypatch):
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    monkeypatch.setenv("MONGODB_URI", "mongodb://user:password@example")
    monkeypatch.setenv("GOOGLE_API_KEY", "secret-google-key")
    monkeypatch.setenv("MONGODB_DATABASE", "datagov")

    summary = validator.build_environment_summary(
        {
            "domain": {"domain_items": [{"key": "a"}]},
            "table": {"table_catalog_items": [{"key": "b"}, {"key": "c"}]},
            "main": {"main_flow_filters": [{"key": "d"}]},
        },
        {"model": "gemini-test", "temperature": 0, "timeout": 60},
        "20260701",
    )

    rendered = str(summary)
    assert "password" not in rendered
    assert "secret-google-key" not in rendered
    assert summary["secrets"] == {
        "mongodb_uri_configured": True,
        "google_api_key_configured": True,
    }
    assert summary["mongodb"]["loaded_counts"] == {
        "domain": 1,
        "table_catalog": 2,
        "main_flow_filter": 1,
    }


def test_semantic_check_rejects_unexpanded_process_group():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    errors = validator._semantic_plan_errors(
        "BG공정 생산량 알려줘",
        {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_data",
                    "filters": {
                        "OPER_NAME": {
                            "operator": "eq",
                            "value": "BG",
                        }
                    },
                }
            ]
        },
        {
            "domain_items": [
                {
                    "section": "process_groups",
                    "key": "BG",
                    "payload": {
                        "display_name": "BG",
                        "aliases": ["BG", "B/G"],
                        "processes": ["B/G1", "B/G2"],
                    },
                }
            ]
        },
        {
            "domain_items": [
                {
                    "section": "process_groups",
                    "key": "BG",
                    "payload": {
                        "display_name": "BG",
                        "aliases": ["BG", "B/G"],
                        "processes": ["B/G1", "B/G2"],
                    },
                }
            ]
        },
    )

    assert [item["type"] for item in errors] == ["unexpanded_process_group"]
    assert errors[0]["expected_oper_names"] == ["B/G1", "B/G2"]
    assert errors[0]["actual_oper_names"] == ["BG"]


def test_semantic_check_accepts_expanded_group_and_device_free_product_grain():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    errors = validator._semantic_plan_errors(
        "오늘 BG공정에서 생산량이 가장 많은 3개 제품 알려줘",
        {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "filters": {
                        "OPER_NAME": {
                            "operator": "in",
                            "value": ["B/G1", "B/G2"],
                        }
                    },
                }
            ],
            "resolved_grain_plan": {
                "grain_columns": [
                    "TECH",
                    "DENSITY",
                    "MODE",
                    "PKG1",
                    "PKG2",
                    "LEAD",
                    "MCP_NO",
                ]
            },
        },
        {
            "domain_items": [
                {
                    "section": "process_groups",
                    "key": "BG",
                    "payload": {
                        "aliases": ["BG", "B/G"],
                        "processes": ["B/G1", "B/G2"],
                    },
                }
            ]
        },
        {
            "domain_items": [
                {
                    "section": "process_groups",
                    "key": "BG",
                    "payload": {
                        "aliases": ["BG", "B/G"],
                        "processes": ["B/G1", "B/G2"],
                    },
                }
            ]
        },
    )

    assert errors == []


def test_semantic_check_accepts_source_scoped_input_and_da_filters():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    da_processes = ["D/A1", "D/A2", "D/A3", "D/A4", "D/A5", "D/A6"]
    domain = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "DA",
                "payload": {
                    "display_name": "D/A",
                    "aliases": ["DA", "D/A"],
                    "processes": da_processes,
                },
            }
        ]
    }

    errors = validator._semantic_plan_errors(
        "오늘 현시간 기준 INPUT실적은 있으나 D/A공정 WIP 없는 제품 확인해줘",
        {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "input_actual",
                    "filters": {
                        "OPER_NAME": {"operator": "eq", "value": "INPUT"}
                    },
                },
                {
                    "dataset_key": "wip_today",
                    "source_alias": "da_wip",
                    "filters": {
                        "OPER_NAME": {"operator": "in", "value": da_processes}
                    },
                },
            ]
        },
        domain,
        domain,
    )

    assert errors == []


def test_semantic_check_accepts_ordered_process_range_applied_after_retrieval():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    domain = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "DA",
                "payload": {
                    "aliases": ["DA", "D/A"],
                    "processes": ["D/A1", "D/A2"],
                },
            },
            {
                "section": "process_groups",
                "key": "WB",
                "payload": {
                    "aliases": ["WB", "W/B"],
                    "processes": ["W/B5", "W/B6"],
                },
            },
        ]
    }
    plan = {
        "retrieval_jobs": [
            {
                "dataset_key": "production",
                "source_alias": "production_data",
                "filters": {},
            }
        ],
        "pandas_function_cases": [
            {
                "key": "ordered_process_range",
                "function_name": "filter_ordered_range",
                "input_text": "D/A1~W/B6",
                "source_alias": "production_data",
            }
        ],
        "pandas_execution_plan": [
            {
                "operation": "apply_pandas_function_case",
                "function_case_key": "ordered_process_range",
                "function_name": "filter_ordered_range",
                "input_text": "D/A1~W/B6",
                "source_alias": "production_data",
            }
        ],
    }

    errors = validator._semantic_plan_errors(
        "7월 1일 D/A1~W/B6 공정 구간의 공정별 생산량을 OPER_SEQ 순서로 알려줘",
        plan,
        domain,
        domain,
    )

    assert errors == []


def test_semantic_check_rejects_unbound_ordered_process_range_helper():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    domain = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "DA",
                "payload": {
                    "aliases": ["DA", "D/A"],
                    "processes": ["D/A1", "D/A2"],
                },
            }
        ]
    }
    plan = {
        "retrieval_jobs": [
            {
                "dataset_key": "production",
                "source_alias": "production_data",
                "filters": {},
            }
        ],
        "pandas_function_cases": [
            {
                "key": "ordered_process_range",
                "function_name": "filter_ordered_range",
                "input_text": "D/A1~W/B6",
                "source_alias": "production_data",
            }
        ],
        "pandas_execution_plan": [],
    }

    errors = validator._semantic_plan_errors(
        "7월 1일 D/A1~W/B6 공정 구간의 공정별 생산량을 알려줘",
        plan,
        domain,
        domain,
    )

    assert [item["type"] for item in errors] == ["specific_process_overexpanded"]


def test_semantic_check_rejects_device_in_implicit_product_ranking_grain():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    errors = validator._semantic_plan_errors(
        "오늘 생산량 상위 3개 제품 알려줘",
        {
            "retrieval_jobs": [{"dataset_key": "production_today"}],
            "resolved_grain_plan": {
                "grain_columns": ["TECH", "MODE", "DEVICE"]
            },
        },
        {"domain_items": []},
    )

    assert [item["type"] for item in errors] == [
        "device_in_default_product_grain"
    ]


def test_semantic_check_rejects_process_group_missing_from_candidate_pool():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    errors = validator._semantic_plan_errors(
        "BG공정 생산량 알려줘",
        {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "filters": {
                        "OPER_NAME": {
                            "operator": "eq",
                            "value": "BG",
                        }
                    },
                }
            ]
        },
        {"domain_items": []},
        {
            "domain_items": [
                {
                    "section": "process_groups",
                    "key": "BG",
                    "payload": {
                        "aliases": ["BG", "B/G"],
                        "processes": ["B/G1", "B/G2"],
                    },
                }
            ]
        },
    )

    assert {item["type"] for item in errors} == {
        "missing_process_group_candidate",
        "unexpanded_process_group",
    }


def test_process_group_alias_does_not_match_inside_product_token():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")

    assert validator._alias_in_question("DP", "RG 32G DDR4 FBGA 96 DDP 제품") is False
    assert validator._alias_in_question("BG", "BG공정 생산량") is True


def test_preferred_process_group_items_drop_legacy_suffix_duplicate():
    validator = load_module(ROOT / "tools" / "validate_data_analysis_question.py")
    items = validator._preferred_process_group_items(
        [
            {
                "section": "process_groups",
                "key": "BG",
                "payload": {"aliases": ["BG", "B/G"], "processes": ["B/G1", "B/G2"]},
            },
            {
                "section": "process_groups",
                "key": "BG_PROCESS_GROUP",
                "payload": {
                    "aliases": ["BG", "B/G"],
                    "processes": ["B/G1", "B/G2", "B/G3"],
                },
            },
        ]
    )

    assert [item["key"] for item in items] == ["BG"]
