from __future__ import annotations

import json

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT / "langflow_components" / "data_analysis_flow_v2" / "04_intent_plan_normalizer.py"
)
HYDRATOR_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "04a_trusted_retrieval_job_hydrator.py"
)
ADAPTER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "14_retrieval_payload_adapter.py"
)
EXECUTOR_PATH = (
    ROOT / "langflow_components" / "data_analysis_flow_v2" / "17_hybrid_analysis_executor.py"
)


def _candidates() -> dict:
    return {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "wip_quantity",
                "payload": {
                    "aliases": ["재공", "보유재공"],
                    "data_source": "wip",
                    "column": "WIP",
                    "aggregation_method": "sum",
                },
            }
        ],
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "lot_status",
                "dataset_key": "lot_status",
                "payload": {
                    "columns": ["LOT_ID", "OPER_NAME", "LEAD", "LOT_STAT"],
                },
            },
            {
                "section": "table_catalog",
                "key": "wip",
                "dataset_key": "wip",
                "payload": {
                    "source_type": "oracle",
                    "columns": ["DATE", "OPER_NAME", "LEAD", "WIP"],
                    "required_params": ["DATE"],
                },
            },
        ],
    }


def _lot_job() -> dict:
    return {
        "dataset_key": "lot_status",
        "source_alias": "lot_status_source",
        "source_type": "oracle",
        "filters": {
            "OPER_NAME": {"operator": "eq", "value": "SBM"},
            "LEAD": {"operator": "eq", "value": 266},
        },
    }


def test_detail_lot_list_keeps_entity_source_when_wip_alias_is_not_a_metric():
    normalizer = load_module(NORMALIZER_PATH)
    steps = [
        {
            "node_id": "filter_lots",
            "operation": "apply_filters",
            "inputs": [{"kind": "external_source", "ref": "lot_status_source"}],
            "output_alias": "filtered_lots",
            "source_alias": "lot_status_source",
        },
        {
            "node_id": "select_lots",
            "operation": "select_columns",
            "inputs": [{"kind": "node_output", "ref": "filtered_lots"}],
            "output_alias": "result",
            "columns": ["LOT_ID", "OPER_NAME", "LEAD"],
        },
    ]
    intent_plan = {
        "output_contract": {
            # Intent LLMs may call the same row-level contract either detail
            # or entity_list.  Both must protect the entity-owning source.
            "result_mode": "entity_list",
            "grain_columns": ["LOT_ID"],
            "required_columns": ["LOT_ID", "OPER_NAME", "LEAD"],
            "result_columns": ["LOT_ID", "OPER_NAME", "LEAD"],
        }
    }

    jobs, normalized_steps, trace = normalizer._ensure_selected_metric_sources(
        {
            "request": {
                "question": "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘",
                "reference_date": "20260822",
            }
        },
        [_lot_job()],
        steps,
        _candidates(),
        [{"section": "quantity_terms", "key": "wip_quantity"}],
        intent_plan,
    )

    assert jobs == [_lot_job()]
    assert normalized_steps == steps
    assert trace["status"] == "applied"
    assert trace["additions"] == []
    assert trace["replacements"] == []
    assert trace["skipped_replacements"] == [
        {
            "metadata_ref": {"section": "quantity_terms", "key": "wip_quantity"},
            "source_alias": "lot_status_source",
            "from_dataset_key": "lot_status",
            "candidate_dataset_key": "wip",
            "metrics": ["WIP"],
            "reason": "detail_metric_not_consumed",
        }
    ]


def test_true_wip_metric_still_rebinds_to_registered_wip_source():
    normalizer = load_module(NORMALIZER_PATH)
    steps = [
        {
            "node_id": "sum_wip",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "lot_status_source"}],
            "output_alias": "result",
            "source_alias": "lot_status_source",
            "group_by": ["OPER_NAME"],
            "aggregations": [
                {"column": "WIP", "method": "sum", "output_column": "TOTAL_WIP"}
            ],
        }
    ]
    intent_plan = {
        "output_contract": {
            # Even a detail-shaped response may legitimately request WIP as a
            # displayed/aggregated metric.  That explicit consumption must
            # continue to permit the registered source rebind.
            "result_mode": "detail",
            "metric_columns": ["TOTAL_WIP"],
            "result_columns": ["OPER_NAME", "TOTAL_WIP"],
        }
    }

    jobs, normalized_steps, trace = normalizer._ensure_selected_metric_sources(
        {
            "request": {
                "question": "오늘 SBM공정 재공 수량 알려줘",
                "reference_date": "20260822",
            }
        },
        [_lot_job()],
        steps,
        _candidates(),
        [{"section": "quantity_terms", "key": "wip_quantity"}],
        intent_plan,
    )

    assert normalized_steps == steps
    assert jobs == [
        {
            "dataset_key": "wip",
            "source_alias": "lot_status_source",
            "source_type": "oracle",
            "required_params": {"DATE": "20260822"},
            "filters": {
                "OPER_NAME": {"operator": "eq", "value": "SBM"},
                "LEAD": {"operator": "eq", "value": 266},
            },
        }
    ]
    assert trace["status"] == "applied"
    assert trace["skipped_replacements"] == []
    assert trace["replacements"][0]["from_dataset_key"] == "lot_status"
    assert trace["replacements"][0]["to_dataset_key"] == "wip"
    evidence = normalizer._detail_metric_consumption_evidence(
        ["WIP"], intent_plan, steps, [_lot_job()]
    )
    assert any(item.get("column") == "WIP" for item in evidence)


def test_output_only_wip_metric_cannot_replace_a_lot_grain_owner():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = _candidates()
    candidates["table_catalog_items"][0]["payload"]["dataset_family"] = "wip"
    candidates["table_catalog_items"][0]["payload"]["selection_criteria"] = {
        "time_scope": "current_day"
    }
    candidates["table_catalog_items"][1]["payload"]["dataset_family"] = "wip"
    candidates["table_catalog_items"][1]["payload"]["selection_criteria"] = {
        "time_scope": "history"
    }
    raw = {
        "intent_plan": {
            "analysis_kind": "wip_lot_list_inquiry",
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "metadata_refs": [
                {"section": "quantity_terms", "key": "wip_quantity"},
                {"section": "table_catalog", "key": "lot_status"},
            ],
            "retrieval_jobs": [_lot_job()],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_lots",
                    "operation": "apply_filters",
                    "source_alias": "lot_status_source",
                    "inputs": [
                        {"kind": "external_source", "ref": "lot_status_source"}
                    ],
                    "output_alias": "filtered_lots",
                },
                {
                    "node_id": "select_lots",
                    "operation": "select_columns",
                    "source_alias": "lot_status_source",
                    "inputs": [
                        {"kind": "node_output", "ref": "filter_lots"}
                    ],
                    "output_alias": "result",
                },
            ],
            "output_contract": {
                "result_mode": "entity_list",
                "grain_columns": ["LOT_ID"],
                "primary_metric": "WIP",
                "metric_columns": ["WIP"],
                "required_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
                "result_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
            },
        }
    }

    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘",
                "reference_date": "20260822",
            }
        },
        json.dumps(raw, ensure_ascii=False),
        candidates,
    )
    plan = normalized["intent_plan"]

    assert plan["retrieval_jobs"][0]["dataset_key"] == "lot_status"
    assert plan["retrieval_jobs"][0].get("required_params", {}) == {}
    guard = normalized["trace"]["inspection"]["intent"][
        "domain_metric_source_guard"
    ]
    assert guard["skipped_replacements"][0]["reason"] == (
        "detail_grain_owner_preserved_output_only_metric"
    )
    assert "WIP" not in plan["output_contract"].get("metric_columns", [])
    assert "WIP" not in plan["output_contract"]["result_columns"]
    projection = normalized["trace"]["inspection"]["intent"][
        "detail_grain_metric_projection"
    ]
    assert projection["status"] == "applied"
    assert projection["trigger"] == "existing_grain_owner_preserved"


def test_detail_grain_switches_to_unique_entity_owner_and_rebinds_params():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "wip_today",
                "payload": {
                    "source_type": "oracle",
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["DATE", "OPER_NAME", "LEAD", "WIP"],
                    "required_params": ["DATE"],
                    "source_config": {
                        "query_template": "SELECT DATE, OPER_NAME, LEAD, WIP FROM WIP_TODAY"
                    },
                },
            },
            {
                "dataset_key": "lot_status",
                "payload": {
                    "source_type": "oracle",
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME", "LEAD", "LOT_STAT"],
                    "required_params": [],
                },
            },
        ]
    }
    original_filters = {
        "OPER_NAME": {"operator": "eq", "value": "SBM"},
        "LEAD": {"operator": "eq", "value": 266},
    }
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {
            "request": {
                "question": "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘"
            }
        },
        [
            {
                "dataset_key": "wip_today",
                "source_alias": "wip_today_source",
                "source_type": "oracle",
                "required_params": {"DATE": "20260822"},
                "filters": original_filters,
            }
        ],
        [
            {
                "node_id": "filter_lots",
                "operation": "apply_filters",
                "source_alias": "wip_today_source",
                "inputs": [{"kind": "external_source", "ref": "wip_today_source"}],
            },
            {
                "node_id": "select_lots",
                "operation": "select_columns",
                "inputs": [{"kind": "node_output", "ref": "filter_lots"}],
            },
        ],
        candidates,
        [],
        {
            "output_contract": {
                "result_mode": "detail",
                "grain_columns": ["LOT_ID"],
                "required_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
                "result_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
            }
        },
    )

    assert jobs == [
        {
            "dataset_key": "lot_status",
            "source_alias": "wip_today_source",
            "source_type": "oracle",
            "filters": original_filters,
        }
    ]
    assert trace["status"] == "applied"
    assert trace["corrections"][0]["selection_source"] == (
        "table_catalog.unique_detail_grain_owner"
    )
    assert trace["corrections"][0]["source_columns"] == ["LOT_ID"]


def test_detail_grain_does_not_force_an_ambiguous_entity_owner():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "wip_today",
                "payload": {
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["OPER_NAME", "WIP"],
                },
            },
            {
                "dataset_key": "lot_status_a",
                "payload": {
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME"],
                },
            },
            {
                "dataset_key": "lot_status_b",
                "payload": {
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME"],
                },
            },
        ]
    }
    original_job = {"dataset_key": "wip_today", "source_alias": "wip_source"}
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {"request": {"question": "현재 LOT 목록"}},
        [original_job],
        [],
        candidates,
        [],
        {
            "output_contract": {
                "result_mode": "entity_list",
                "grain_columns": ["LOT_ID"],
            }
        },
    )

    assert jobs == [original_job]
    assert trace["status"] == "advisory"
    assert trace["corrections"] == []
    assert trace["advisories"][0]["reason"] == (
        "schema_capable_candidate_not_unique"
    )


def test_detail_grain_keeps_a_source_that_already_owns_the_grain():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "wip_today",
                "payload": {
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["OPER_NAME", "WIP"],
                },
            },
            {
                "dataset_key": "lot_status",
                "payload": {
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME"],
                },
            },
        ]
    }
    original_job = {"dataset_key": "wip_today", "source_alias": "wip_source"}
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {"request": {"question": "공정별 현재 재공"}},
        [original_job],
        [],
        candidates,
        [],
        {
            "output_contract": {
                "result_mode": "detail",
                "grain_columns": ["OPER_NAME"],
                "metric_columns": ["WIP"],
            }
        },
    )

    assert jobs == [original_job]
    assert trace["corrections"] == []


def test_detail_grain_does_not_switch_to_an_incompatible_entity_catalog():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "wip_today",
                "payload": {
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["OPER_NAME", "LEAD", "WIP"],
                },
            },
            {
                # It owns LOT_ID but belongs to another population, so the
                # reconciler must not redirect a current-WIP list to it.
                "dataset_key": "hold_history",
                "payload": {
                    "dataset_family": "hold_history",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME", "LEAD"],
                },
            },
            {
                # Same family and grain, but it cannot execute the LEAD filter.
                "dataset_key": "wip_lot_header",
                "payload": {
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME"],
                },
            },
        ]
    }
    original_job = {
        "dataset_key": "wip_today",
        "source_alias": "wip_source",
        "filters": {
            "OPER_NAME": {"operator": "eq", "value": "SBM"},
            "LEAD": {"operator": "eq", "value": 266},
        },
    }
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {"request": {"question": "SBM 266LEAD LOT 목록"}},
        [original_job],
        [],
        candidates,
        [],
        {
            "output_contract": {
                "result_mode": "entity_list",
                "grain_columns": ["LOT_ID"],
            }
        },
    )

    assert jobs == [original_job]
    assert trace["corrections"] == []


def test_full_normalization_recovers_wip_today_lot_list_to_lot_status():
    normalizer = load_module(NORMALIZER_PATH)
    payload = {
        "request": {
            "question": "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘",
            "reference_date": "20260822",
        }
    }
    candidates = {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "wip_quantity",
                "payload": {
                    "aliases": ["재공", "보유재공"],
                    "data_source": "wip",
                    "column": "WIP",
                    "aggregation_method": "sum",
                },
            }
        ],
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "wip_today",
                "dataset_key": "wip_today",
                "payload": {
                    "source_type": "oracle",
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["DATE", "OPER_NAME", "LEAD", "WIP"],
                    "required_params": ["DATE"],
                },
            },
            {
                "section": "table_catalog",
                "key": "lot_status",
                "dataset_key": "lot_status",
                "payload": {
                    "source_type": "oracle",
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["LOT_ID", "OPER_NAME", "LEAD", "LOT_STAT"],
                    "required_params": [],
                    "filter_mappings": {
                        "LOT_ID": "LOT_ID",
                        "OPER_NAME": "OPER_NAME",
                        "LEAD": "LEAD",
                    },
                    "source_config": {
                        "query_template": "SELECT LOT_ID, OPER_NAME, LEAD, LOT_STAT FROM LOT_STATUS"
                    },
                    "default_detail_columns": [
                        "LOT_ID",
                        "OPER_NAME",
                        "LEAD",
                        "LOT_STAT",
                    ],
                },
            },
        ],
        "main_flow_filters": [],
    }
    original_filters = {
        "OPER_NAME": {"operator": "eq", "value": "SBM"},
        "LEAD": {"operator": "eq", "value": 266},
    }
    raw = {
        "intent_plan": {
            "analysis_kind": "wip_lot_list_inquiry",
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "condition_resolution": {
                "effective_filters": {
                    "wip_today_source": {
                        "dataset_key": "wip_today",
                        "filters": {
                            "DATE": {"operator": "eq", "value": "20260822"},
                            **original_filters,
                        },
                        "filter_mappings": {"DATE": ["WORK_DATE"]},
                    }
                }
            },
            "metadata_refs": [
                {"section": "quantity_terms", "key": "wip_quantity"},
                {"section": "table_catalog", "key": "wip_today"},
            ],
            "retrieval_jobs": [
                {
                    "dataset_key": "wip_today",
                    "source_alias": "wip_today_source",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260822"},
                    "filters": original_filters,
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "node_apply_filters",
                    "operation": "apply_filters",
                    "inputs": [
                        {"kind": "external_source", "ref": "wip_today_source"}
                    ],
                    "output_alias": "filtered_lots",
                    "source_alias": "wip_today_source",
                },
                {
                    "node_id": "node_select_columns",
                    "operation": "select_columns",
                    "inputs": [
                        {"kind": "node_output", "ref": "node_apply_filters"}
                    ],
                    "output_alias": "final_lot_list",
                    "source_alias": "wip_today_source",
                },
            ],
            "output_contract": {
                "result_mode": "detail",
                "grain_columns": ["LOT_ID"],
                "primary_metric": "LOT_ID",
                "metric_columns": ["WIP"],
                "required_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
                "result_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
            },
        }
    }

    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(raw, ensure_ascii=False),
        candidates,
    )
    plan = normalized["intent_plan"]
    job = plan["retrieval_jobs"][0]

    assert job["dataset_key"] == "lot_status"
    assert job["source_alias"] == "wip_today_source"
    assert job.get("required_params", {}) == {}
    assert job["filters"] == original_filters
    corrections = normalized["trace"]["inspection"]["intent"][
        "source_dataset_selection"
    ]["corrections"]
    assert corrections[0]["selection_source"] == (
        "table_catalog.unique_detail_grain_owner"
    )
    output_contract = plan["output_contract"]
    assert "WIP" not in output_contract["required_columns"]
    assert "WIP" not in output_contract["result_columns"]
    assert "WIP" not in output_contract.get("metric_columns", [])
    projection = normalized["trace"]["inspection"]["intent"][
        "detail_grain_metric_projection"
    ]
    assert projection["status"] == "applied"
    assert projection["dropped_optional_metrics"] == ["WIP"]
    assert {"section": "table_catalog", "key": "lot_status"} in plan[
        "metadata_refs"
    ]
    assert {"section": "table_catalog", "key": "wip_today"} not in plan[
        "metadata_refs"
    ]
    assert {"section": "table_catalog", "key": "lot_status"} in normalized[
        "metadata_refs"
    ]
    ref_trace = normalized["trace"]["inspection"]["intent"][
        "source_catalog_ref_reconciliation"
    ]
    assert ref_trace["removed_refs"] == [
        {"section": "table_catalog", "key": "wip_today"}
    ]
    assert ref_trace["added_refs"] == [
        {"section": "table_catalog", "key": "lot_status"}
    ]
    effective = plan["condition_resolution"]["effective_filters"][
        "wip_today_source"
    ]
    assert effective["dataset_key"] == "lot_status"
    assert "DATE" not in effective["filters"]
    assert effective["filters"] == original_filters
    assert effective["filter_mappings"] == {
        "LOT_ID": "LOT_ID",
        "OPER_NAME": "OPER_NAME",
        "LEAD": "LEAD",
    }
    date_trace = normalized["trace"]["inspection"]["intent"][
        "corrected_source_effective_filter_reconciliation"
    ]
    assert date_trace["status"] == "applied"
    assert date_trace["removed_filters"][0]["field"] == "DATE"

    hydrator = load_module(HYDRATOR_PATH)
    adapter = load_module(ADAPTER_PATH)
    hydrated = hydrator.hydrate_retrieval_jobs(
        normalized,
        {"table_catalog_items": candidates["table_catalog_items"]},
        "dummy",
    )
    rows = [
        {
            "LOT_ID": "SBM-266-LOT-01",
            "OPER_NAME": "SBM",
            "LEAD": "266",
            "LOT_STAT": "WAITING",
        },
        {
            "LOT_ID": "SBM-266-LOT-02",
            "OPER_NAME": "SBM",
            "LEAD": "266",
            "LOT_STAT": "RUNNING",
        },
    ]
    hydrated["runtime_sources"] = {"wip_today_source": rows}
    hydrated["source_results"] = [
        {
            "dataset_key": "lot_status",
            "source_alias": "wip_today_source",
            "status": "ok",
            "columns": ["LOT_ID", "OPER_NAME", "LEAD", "LOT_STAT"],
            "row_count": 2,
        }
    ]
    adapted = adapter.build_retrieval_payload(hydrated)
    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "complete"
    assert schema["sources"][0]["unresolved_required_columns"] == []
    assert adapted["intent_plan"]["retrieval_jobs"][0]["dataset_key"] == (
        "lot_status"
    )
    assert not adapted["trace"].get("errors")


def test_corrected_source_prunes_stale_filter_inside_logical_group():
    normalizer = load_module(NORMALIZER_PATH)
    condition_resolution = {
        "effective_filters": {
            "lot_status_source": {
                "dataset_key": "wip",
                "filters": {
                    "and": [
                        {
                            "field": "DATE",
                            "operator": "eq",
                            "value": "20260822",
                        },
                        {
                            "field": "LEAD",
                            "operator": "eq",
                            "value": 266,
                        },
                    ]
                },
            }
        }
    }
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_status_source",
            "filters": {"LEAD": {"operator": "eq", "value": 266}},
        }
    ]
    source_selection = {
        "corrections": [
            {
                "source_alias": "lot_status_source",
                "from_dataset_key": "wip",
                "to_dataset_key": "lot_status",
            }
        ]
    }

    normalized, trace = (
        normalizer._strip_corrected_source_unsupported_effective_filters(
            condition_resolution,
            jobs,
            _candidates(),
            source_selection,
        )
    )

    effective = normalized["effective_filters"]["lot_status_source"]
    assert effective["filters"] == {
        "and": [
            {
                "field": "LEAD",
                "operator": "eq",
                "value": 266,
            }
        ]
    }
    assert [item["field"] for item in trace["removed_filters"]] == ["DATE"]


def test_corrected_source_rekeys_old_dataset_effective_filter_entry():
    normalizer = load_module(NORMALIZER_PATH)
    condition_resolution = {
        "effective_filters": {
            "wip": {
                "dataset_key": "wip",
                "filters": {
                    "DATE": {"operator": "eq", "value": "20260822"},
                    "LEAD": {"operator": "eq", "value": 266},
                },
            }
        }
    }
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_status_source",
            "filters": {"LEAD": {"operator": "eq", "value": 266}},
        }
    ]
    source_selection = {
        "corrections": [
            {
                "source_alias": "lot_status_source",
                "from_dataset_key": "wip",
                "to_dataset_key": "lot_status",
            }
        ]
    }

    normalized, trace = (
        normalizer._strip_corrected_source_unsupported_effective_filters(
            condition_resolution,
            jobs,
            _candidates(),
            source_selection,
        )
    )

    assert "wip" not in normalized["effective_filters"]
    effective = normalized["effective_filters"]["lot_status_source"]
    assert effective["dataset_key"] == "lot_status"
    assert effective["filters"] == {
        "LEAD": {"operator": "eq", "value": 266}
    }
    assert trace["rekeyed_effective_filters"] == [
        {
            "from_key": "wip",
            "to_source_alias": "lot_status_source",
            "from_dataset_key": "wip",
        }
    ]
    assert [item["field"] for item in trace["removed_filters"]] == ["DATE"]


def test_corrected_detail_source_does_not_steal_live_wip_effective_filters():
    normalizer = load_module(NORMALIZER_PATH)
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_alias",
            "filters": {"LEAD": {"operator": "eq", "value": 266}},
        },
        {
            "dataset_key": "wip",
            "source_alias": "wip_metric",
            "required_params": {"DATE": "20260822"},
        },
    ]
    condition_resolution = {
        "effective_filters": {
            "wip": {
                "dataset_key": "wip",
                "filters": {
                    "DATE": {"operator": "eq", "value": "20260822"}
                },
            }
        }
    }
    synchronized = normalizer._synchronize_effective_filters_with_retrieval_jobs(
        condition_resolution,
        jobs,
    )
    assert "wip_metric" in synchronized["effective_filters"]
    source_selection = {
        "corrections": [
            {
                "source_alias": "lot_alias",
                "from_dataset_key": "wip",
                "to_dataset_key": "lot_status",
            }
        ]
    }

    normalized, trace = (
        normalizer._strip_corrected_source_unsupported_effective_filters(
            synchronized,
            jobs,
            _candidates(),
            source_selection,
        )
    )

    assert "lot_alias" not in normalized["effective_filters"]
    assert normalized["effective_filters"]["wip_metric"]["dataset_key"] == "wip"
    assert normalized["effective_filters"]["wip_metric"]["filters"] == {
        "DATE": {"operator": "eq", "value": "20260822"}
    }
    assert trace["status"] == "not_needed"


def test_corrected_detail_source_does_not_steal_previous_result_filters():
    normalizer = load_module(NORMALIZER_PATH)
    condition_resolution = {
        "effective_filters": {
            "previous_result": {
                "dataset_key": "wip",
                "filters": {
                    "DATE": {"operator": "eq", "value": "20260822"}
                },
            }
        }
    }
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_alias",
            "filters": {"LEAD": {"operator": "eq", "value": 266}},
        }
    ]
    source_selection = {
        "corrections": [
            {
                "source_alias": "lot_alias",
                "from_dataset_key": "wip",
                "to_dataset_key": "lot_status",
            }
        ]
    }

    normalized, trace = (
        normalizer._strip_corrected_source_unsupported_effective_filters(
            condition_resolution,
            jobs,
            _candidates(),
            source_selection,
        )
    )

    assert "lot_alias" not in normalized["effective_filters"]
    assert normalized["effective_filters"]["previous_result"] == (
        condition_resolution["effective_filters"]["previous_result"]
    )
    assert trace["status"] == "not_needed"


def test_effective_filter_sync_replaces_leaf_inside_logical_group():
    normalizer = load_module(NORMALIZER_PATH)
    condition_resolution = {
        "effective_filters": {
            "wip_source": {
                "dataset_key": "wip",
                "filters": {
                    "and": [
                        {
                            "field": "DATE",
                            "operator": "eq",
                            "value": "OLD",
                        }
                    ]
                },
            }
        }
    }
    jobs = [
        {
            "dataset_key": "wip",
            "source_alias": "wip_source",
            "filters": {
                "and": [
                    {
                        "field": "DATE",
                        "operator": "eq",
                        "value": "NEW",
                    }
                ]
            },
        }
    ]

    normalized = normalizer._synchronize_effective_filters_with_retrieval_jobs(
        condition_resolution,
        jobs,
    )

    assert normalized["effective_filters"]["wip_source"]["filters"] == {
        "and": [
            {
                "field": "DATE",
                "operator": "eq",
                "value": "NEW",
            }
        ]
    }


def test_effective_filter_sync_preserves_same_field_range_bounds():
    normalizer = load_module(NORMALIZER_PATH)
    condition_resolution = {
        "effective_filters": {
            "wip_source": {
                "dataset_key": "wip",
                "filters": {
                    "and": [
                        {
                            "field": "DATE",
                            "operator": "ge",
                            "value": "OLD_START",
                        },
                        {
                            "field": "DATE",
                            "operator": "le",
                            "value": "OLD_END",
                        },
                    ]
                },
            }
        }
    }
    jobs = [
        {
            "dataset_key": "wip",
            "source_alias": "wip_source",
            "filters": {
                "and": [
                    {
                        "field": "DATE",
                        "operator": "ge",
                        "value": "NEW_START",
                    },
                    {
                        "field": "DATE",
                        "operator": "le",
                        "value": "NEW_END",
                    },
                ]
            },
        }
    ]

    normalized = normalizer._synchronize_effective_filters_with_retrieval_jobs(
        condition_resolution,
        jobs,
    )

    assert normalized["effective_filters"]["wip_source"]["filters"] == {
        "and": [
            {"field": "DATE", "operator": "ge", "value": "NEW_START"},
            {"field": "DATE", "operator": "le", "value": "NEW_END"},
        ]
    }


def test_projection_only_detail_plan_uses_typed_filter_once():
    executor = load_module(EXECUTOR_PATH)
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "lot_status",
                    "source_alias": "lot_source",
                    "filters": {
                        "OPER_NAME": {"operator": "eq", "value": "SBM"},
                        "LEAD": {"operator": "eq", "value": 266},
                    },
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_node",
                    "operation": "apply_filters",
                    "inputs": [
                        {"kind": "external_source", "ref": "lot_source"}
                    ],
                    "output_alias": "filtered_lots",
                    "source_alias": "lot_source",
                },
                {
                    "node_id": "select_node",
                    "operation": "select_columns",
                    "inputs": [
                        {"kind": "node_output", "ref": "filter_node"}
                    ],
                    "output_alias": "result",
                    "source_alias": "lot_source",
                },
            ],
            "output_contract": {
                "result_mode": "entity_list",
                "strict_result_columns": True,
                "grain_columns": ["LOT_ID"],
                "result_columns": ["LOT_ID", "OPER_NAME", "LEAD"],
                "required_columns": ["LOT_ID", "OPER_NAME", "LEAD"],
            },
        },
        "runtime_sources": {
            "lot_source": [
                {"LOT_ID": "SBM-266-01", "OPER_NAME": "SBM", "LEAD": "266"},
                {"LOT_ID": "SBM-266-02", "OPER_NAME": "SBM", "LEAD": "266"},
            ]
        },
        "trace": {
            "inspection": {
                "source_schema_resolution": {
                    "sources": [
                        {"source_alias": "lot_source", "status": "complete"}
                    ]
                }
            }
        },
    }
    model_code = """
df = sources["lot_source"]
df = df[df["LEAD"].isin([266])]
result = df[["LOT_ID", "OPER_NAME", "LEAD"]]
"""

    executed = executor.execute_pandas_code(payload, {"code": model_code})

    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["row_count"] == 2
    assert executed["analysis"]["execution_mode"] == "project_single_source"
    pandas_trace = executed["trace"]["inspection"]["pandas_execution"]
    assert pandas_trace["llm_code_executed"] is False
    assert [row["LOT_ID"] for row in executed["data"]["rows"]] == [
        "SBM-266-01",
        "SBM-266-02",
    ]


def test_projection_fallback_rejects_step_local_filter_semantics():
    executor = load_module(EXECUTOR_PATH)
    base_step = {
        "node_id": "filter_node",
        "operation": "apply_filters",
        "inputs": [{"kind": "external_source", "ref": "lot_source"}],
        "source_alias": "lot_source",
    }
    inline_filters = {
        "field": "LOT_STAT",
        "filter_conditions": [
            {"field": "LOT_STAT", "operator": "eq", "value": "RUNNING"}
        ],
        "condition": {"field": "LOT_STAT", "operator": "eq", "value": "RUNNING"},
        "predicate": "LOT_STAT == 'RUNNING'",
        "where": {"LOT_STAT": {"operator": "eq", "value": "RUNNING"}},
    }

    for key, value in inline_filters.items():
        steps = [{**base_step, key: value}]
        assert (
            executor._projection_only_single_source_steps(steps, "lot_source")
            is False
        ), key


def test_existing_lot_grain_owner_drops_output_only_unsupported_wip():
    normalizer = load_module(NORMALIZER_PATH)
    plan = {
        "output_contract": {
            "result_mode": "entity_list",
            "grain_columns": ["LOT_ID"],
            "primary_metric": "WIP",
            "metric_columns": ["WIP"],
            "required_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
            "result_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
            "column_labels": {"LOT_ID": "Lot ID", "WIP": "재공수량"},
        }
    }
    steps = [
        {
            "node_id": "filter_lots",
            "operation": "apply_filters",
            "inputs": [{"kind": "external_source", "ref": "lot_source"}],
            "output_alias": "filtered_lots",
            "source_alias": "lot_source",
        },
        {
            "node_id": "select_lots",
            "operation": "select_columns",
            "inputs": [{"kind": "node_output", "ref": "filter_lots"}],
            "output_alias": "result",
            "source_alias": "lot_source",
        },
    ]

    reconciled, trace = normalizer._reconcile_detail_grain_optional_metrics(
        plan,
        [{"dataset_key": "lot_status", "source_alias": "lot_source"}],
        steps,
        _candidates(),
        {"corrections": []},
        {"skipped_replacements": []},
    )

    contract = reconciled["output_contract"]
    assert "WIP" not in contract.get("metric_columns", [])
    assert "WIP" not in contract["required_columns"]
    assert "WIP" not in contract["result_columns"]
    assert "primary_metric" not in contract
    assert "WIP" not in contract["column_labels"]
    assert trace["status"] == "applied"
    assert trace["dropped_optional_metrics"] == ["WIP"]
    assert trace["trigger"] == "existing_explicit_grain_owner"


def test_final_normalized_lot_contract_does_not_reintroduce_output_only_wip():
    normalizer = load_module(NORMALIZER_PATH)
    raw = {
        "intent_plan": {
            "analysis_kind": "wip_lot_list",
            "request_scope": "new_analysis",
            "retrieval_jobs": [
                {
                    "dataset_key": "lot_status",
                    "source_alias": "lot_source",
                    "filters": {
                        "OPER_NAME": {"operator": "eq", "value": "SBM"},
                        "LEAD": {"operator": "eq", "value": 266},
                    },
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_lots",
                    "operation": "apply_filters",
                    "inputs": [
                        {"kind": "external_source", "ref": "lot_source"}
                    ],
                    "output_alias": "filtered_lots",
                    "source_alias": "lot_source",
                },
                {
                    "node_id": "select_lots",
                    "operation": "select_columns",
                    "inputs": [
                        {"kind": "node_output", "ref": "filter_lots"}
                    ],
                    "output_alias": "result",
                    "source_alias": "lot_source",
                },
            ],
            "output_contract": {
                "result_mode": "entity_list",
                "grain_columns": ["LOT_ID"],
                "primary_metric": "WIP",
                "metric_columns": ["WIP"],
                "required_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
                "result_columns": ["LOT_ID", "OPER_NAME", "LEAD", "WIP"],
            },
            "metadata_refs": [
                {"section": "quantity_terms", "key": "wip_quantity"},
                {"section": "table_catalog", "key": "lot_status"},
            ],
        }
    }

    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "SBM공정 현재 보유재공중 266LEAD 제품의 LOT LIST 알려줘",
                "reference_date": "20260822",
            }
        },
        raw,
        _candidates(),
    )

    contract = normalized["intent_plan"]["output_contract"]
    assert "WIP" not in contract.get("metric_columns", [])
    assert "WIP" not in contract["required_columns"]
    assert "WIP" not in contract["result_columns"]
    projection = normalized["trace"]["inspection"]["intent"][
        "detail_grain_metric_projection"
    ]
    assert projection["status"] == "applied"


def test_detail_grain_recovery_keeps_an_unsupported_metric_used_by_execution():
    normalizer = load_module(NORMALIZER_PATH)
    plan = {
        "output_contract": {
            "result_mode": "detail",
            "grain_columns": ["LOT_ID"],
            "primary_metric": "WIP",
            "metric_columns": ["WIP"],
            "required_columns": ["LOT_ID", "WIP"],
            "result_columns": ["LOT_ID", "WIP"],
        }
    }
    reconciled, trace = normalizer._reconcile_detail_grain_optional_metrics(
        plan,
        [{"dataset_key": "lot_status", "source_alias": "lot_source"}],
        [
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "lot_source",
                "group_by": ["LOT_ID"],
                "aggregations": [
                    {"column": "WIP", "method": "sum", "output_column": "WIP"}
                ],
            }
        ],
        {
            "table_catalog_items": [
                {
                    "dataset_key": "lot_status",
                    "payload": {"columns": ["LOT_ID", "OPER_NAME", "LEAD"]},
                }
            ]
        },
        {
            "corrections": [
                {
                    "source_alias": "lot_source",
                    "from_dataset_key": "wip_today",
                    "to_dataset_key": "lot_status",
                    "selection_source": "table_catalog.unique_detail_grain_owner",
                }
            ]
        },
    )

    assert reconciled == plan
    assert trace["status"] == "not_needed"
    assert trace["dropped_optional_metrics"] == []
    assert trace["preserved_execution_metrics"]["WIP"]
