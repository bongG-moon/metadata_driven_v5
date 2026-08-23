from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


def _load_normalizer():
    path = V2_ROOT / "04_intent_plan_normalizer.py"
    spec = importlib.util.spec_from_file_location("self_referential_filter_guard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_self_referential_filter_guard_drops_only_column_name_literals():
    normalizer = _load_normalizer()
    jobs, guard = normalizer._drop_self_referential_retrieval_filters(
        [
            {
                "dataset_key": "equipment_assign",
                "source_alias": "assign",
                "filters": {
                    "OPER_NAME": {"operator": "eq", "value": "M/D"},
                    "LEAD": {"operator": "eq", "value": "LEAD"},
                    "MCP_NO": {"operator": "eq", "value": "L-266MD"},
                },
            },
            {
                "dataset_key": "eqp_uph",
                "source_alias": "uph",
                "filters": {
                    "and": [
                        {"field": "LEAD", "operator": "in", "values": ["LEAD"]},
                        {"field": "OPER_NAME", "operator": "eq", "value": "M/D"},
                    ]
                },
            },
        ]
    )

    assert jobs[0]["filters"] == {
        "OPER_NAME": {"operator": "eq", "value": "M/D"},
        "MCP_NO": {"operator": "eq", "value": "L-266MD"},
    }
    assert jobs[1]["filters"] == {
        "and": [{"field": "OPER_NAME", "operator": "eq", "value": "M/D"}]
    }
    assert guard["status"] == "applied"
    assert [(item["source_alias"], item["field"]) for item in guard["removed"]] == [
        ("assign", "LEAD"),
        ("uph", "LEAD"),
    ]


def test_self_referential_filter_guard_keeps_normal_product_conditions():
    normalizer = _load_normalizer()
    jobs, guard = normalizer._drop_self_referential_retrieval_filters(
        [
            {
                "dataset_key": "production_today",
                "source_alias": "prod",
                "filters": {
                    "LEAD": {"operator": "eq", "value": "267"},
                    "MCP_NO": {"operator": "contains", "value": "L-267"},
                    "DATE": {"operator": "eq", "value": "20260822"},
                },
            }
        ]
    )

    assert jobs[0]["filters"] == {
        "LEAD": {"operator": "eq", "value": "267"},
        "MCP_NO": {"operator": "contains", "value": "L-267"},
        "DATE": {"operator": "eq", "value": "20260822"},
    }
    assert guard == {"status": "not_needed", "removed": []}


def test_cross_field_label_filter_is_removed_when_numeric_sibling_is_explicit():
    normalizer = _load_normalizer()
    question = "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘"
    jobs, guard = normalizer._drop_self_referential_retrieval_filters(
        [
            {
                "dataset_key": "lot_status",
                "source_alias": "lot_source",
                "filters": {
                    "OPER_NAME": {"operator": "eq", "value": "SBM"},
                    "LEAD": {"operator": "eq", "value": 266},
                    "PKG_TYPE1": {"operator": "eq", "value": "LEAD"},
                },
            }
        ],
        question,
    )

    assert jobs[0]["filters"] == {
        "OPER_NAME": {"operator": "eq", "value": "SBM"},
        "LEAD": {"operator": "eq", "value": 266},
    }
    assert guard["status"] == "applied"
    assert guard["removed"] == [
        {
            "source_alias": "lot_source",
            "path": "retrieval_jobs[0].filters.PKG_TYPE1",
            "field": "PKG_TYPE1",
            "operator": "eq",
            "values": ["LEAD"],
            "sibling_field": "LEAD",
            "sibling_value": "266",
            "reason": "filter_value_repeats_concrete_sibling_field_label",
        }
    ]


def test_cross_field_label_filter_is_kept_without_adjacent_question_evidence():
    normalizer = _load_normalizer()
    filters = {
        "LEAD": {"operator": "eq", "value": 266},
        "PKG_TYPE1": {"operator": "eq", "value": "LEAD"},
    }
    jobs, guard = normalizer._drop_self_referential_retrieval_filters(
        [
            {
                "dataset_key": "lot_status",
                "source_alias": "lot_source",
                "filters": filters,
            }
        ],
        "패키지 타입은 LEAD이고 리드 수는 별도 조건입니다",
    )

    assert jobs[0]["filters"] == filters
    assert guard == {"status": "not_needed", "removed": []}


def test_cross_field_label_filter_is_kept_when_user_explicitly_names_the_field():
    normalizer = _load_normalizer()
    filters = {
        "LEAD": {"operator": "eq", "value": 266},
        "PKG_TYPE1": {"operator": "eq", "value": "LEAD"},
    }

    jobs, guard = normalizer._drop_self_referential_retrieval_filters(
        [
            {
                "dataset_key": "lot_status",
                "source_alias": "lot_source",
                "filters": filters,
            }
        ],
        "PKG_TYPE1은 LEAD이고 266LEAD 제품 보여줘",
    )

    assert jobs[0]["filters"] == filters
    assert guard == {"status": "not_needed", "removed": []}


def test_cross_field_label_filter_is_kept_for_natural_language_field_alias():
    normalizer = _load_normalizer()
    filters = {
        "LEAD": {"operator": "eq", "value": 266},
        "PKG_TYPE1": {"operator": "eq", "value": "LEAD"},
    }

    jobs, guard = normalizer._drop_self_referential_retrieval_filters(
        [
            {
                "dataset_key": "lot_status",
                "source_alias": "lot_source",
                "filters": filters,
            }
        ],
        "패키지 타입은 LEAD이고 266LEAD 제품 보여줘",
    )

    assert jobs[0]["filters"] == filters
    assert guard == {"status": "not_needed", "removed": []}


def test_self_referential_condition_resolution_filters_are_removed_from_display_contracts():
    normalizer = _load_normalizer()
    condition_resolution, guard = (
        normalizer._drop_self_referential_condition_resolution_filters(
            {
                "new": {
                    "effective_filters": {
                        "equipment_assign": {
                            "OPER_NAME": {"operator": "eq", "value": "M/D"},
                            "LEAD": {"operator": "eq", "value": "LEAD"},
                        }
                    }
                },
                "effective_filters": {
                    "assign": {
                        "dataset_key": "equipment_assign",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "M/D"},
                            "LEAD": {"operator": "eq", "value": "LEAD"},
                        },
                    }
                },
            }
        )
    )

    assert condition_resolution["new"]["effective_filters"] == {
        "equipment_assign": {"OPER_NAME": {"operator": "eq", "value": "M/D"}}
    }
    assert condition_resolution["effective_filters"]["assign"]["filters"] == {
        "OPER_NAME": {"operator": "eq", "value": "M/D"}
    }
    assert guard["status"] == "applied"
    assert len(guard["removed"]) == 2


def test_cross_field_label_filter_is_removed_from_effective_filters():
    normalizer = _load_normalizer()
    condition_resolution, guard = (
        normalizer._drop_self_referential_condition_resolution_filters(
            {
                "effective_filters": {
                    "lot_source": {
                        "dataset_key": "lot_status",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "SBM"},
                            "LEAD": {"operator": "eq", "value": 266},
                            "PKG_TYPE1": {"operator": "eq", "value": "LEAD"},
                        },
                    }
                }
            },
            "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘",
        )
    )

    assert condition_resolution["effective_filters"]["lot_source"]["filters"] == {
        "OPER_NAME": {"operator": "eq", "value": "SBM"},
        "LEAD": {"operator": "eq", "value": 266},
    }
    assert guard["status"] == "applied"
    assert guard["removed"][0]["field"] == "PKG_TYPE1"


def test_normalizer_applies_the_guard_before_the_m_d_retrieval_contract_is_executed():
    """Replay the prior live-plan shape that incorrectly emitted ``LEAD=LEAD``."""

    normalizer = _load_normalizer()
    question = "M/D공정 LEAD별 장비 ASSIGN 댓수와 UPH알려줘"
    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": question, "reference_date": "20260822"}},
        {
            "intent_plan": {
                "analysis_kind": "equipment_assignment_and_uph_analysis",
                "retrieval_jobs": [
                    {
                        "dataset_key": "equipment_assign",
                        "source_alias": "assign",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "M/D"},
                            "LEAD": {"operator": "eq", "value": "LEAD"},
                        },
                    },
                    {
                        "dataset_key": "eqp_uph",
                        "source_alias": "uph",
                        "filters": {
                            "OPER_NAME": {"operator": "eq", "value": "M/D"},
                            "LEAD": {"operator": "eq", "value": "LEAD"},
                        },
                    },
                ],
                "pandas_execution_plan": [],
                "condition_resolution": {
                    "new": {
                        "effective_filters": {
                            "equipment_assign": {
                                "LEAD": {"operator": "eq", "value": "LEAD"}
                            },
                            "eqp_uph": {
                                "LEAD": {"operator": "eq", "value": "LEAD"}
                            },
                        }
                    }
                },
            }
        },
        {
            "domain_items": [
                {
                    "section": "process_groups",
                    "key": "MD",
                    "payload": {
                        "aliases": ["M/D", "M/D공정"],
                        "field": "OPER_NAME",
                        "processes": ["M/D"],
                    },
                }
            ],
            "table_catalog_items": [
                {
                    "dataset_key": "equipment_assign",
                    "payload": {
                        "columns": ["OPER_NAME", "LEAD", "EQP_ID"],
                        "filter_mappings": {
                            "OPER_NAME": ["OPER_NAME"],
                            "LEAD": ["LEAD"],
                        },
                    },
                },
                {
                    "dataset_key": "eqp_uph",
                    "payload": {
                        "columns": ["OPER_NAME", "LEAD", "UPH"],
                        "filter_mappings": {
                            "OPER_NAME": ["OPER_NAME"],
                            "LEAD": ["LEAD"],
                        },
                    },
                },
            ],
        },
    )

    jobs = normalized["intent_plan"]["retrieval_jobs"]
    assert [job["filters"] for job in jobs] == [
        {"OPER_NAME": {"operator": "eq", "value": "M/D"}},
        {"OPER_NAME": {"operator": "eq", "value": "M/D"}},
    ]
    assert normalized["intent_plan"]["condition_resolution"]["new"][
        "effective_filters"
    ] == {"equipment_assign": {}, "eqp_uph": {}}
    trace = normalized["trace"]["inspection"]["intent"]
    assert trace["self_referential_filter_guard"]["status"] == "applied"
    assert trace["self_referential_condition_resolution_guard"]["status"] == "applied"
