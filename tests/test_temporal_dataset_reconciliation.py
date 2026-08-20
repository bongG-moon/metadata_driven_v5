from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _catalog_item(
    dataset_key: str,
    *,
    family: str,
    time_scope: str,
    columns: list[str],
) -> dict:
    return {
        "dataset_key": dataset_key,
        "payload": {
            "dataset_key": dataset_key,
            "dataset_family": family,
            "time_scope": time_scope,
            "source_type": "oracle",
            "required_params": ["DATE"],
            "filter_mappings": {
                "DATE": ["DATE"],
                "OPER_NAME": ["OPER_NAME"],
            },
            "columns": deepcopy(columns),
        },
    }


def _metadata_envelope(
    *,
    bounded_catalog: list[dict],
    registry_catalog: list[dict],
    domain_items: list[dict] | None = None,
) -> dict:
    return {
        "metadata_candidates": {
            "domain_items": deepcopy(domain_items or []),
            "table_catalog_items": deepcopy(bounded_catalog),
            "main_flow_filters": [],
        },
        "table_catalog_registry": {
            "dataset_keys": [item["dataset_key"] for item in registry_catalog],
            "items": deepcopy(registry_catalog),
        },
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }


def _aggregate_plan(source_alias: str, metric: str, *, sort_by: str = "") -> list[dict]:
    aggregate_alias = f"{source_alias}_grouped"
    result = [
        {
            "node_id": f"aggregate_{source_alias}",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": source_alias}],
            "source_alias": source_alias,
            "output_alias": aggregate_alias,
            "group_by": ["OPER_NAME"],
            "aggregations": [
                {"column": metric, "method": "sum", "output_column": metric}
            ],
        }
    ]
    if sort_by:
        result.append(
            {
                "node_id": f"sort_{source_alias}",
                "operation": "sort_and_top_n",
                "inputs": [{"kind": "node_output", "ref": aggregate_alias}],
                "source_alias": aggregate_alias,
                "output_alias": f"{source_alias}_result",
                "sort_by": sort_by,
                "order": "desc",
                "limit": 0,
            }
        )
    return result


def _aggregate_contract(metric: str) -> dict:
    return {
        "result_mode": "aggregate",
        "result_columns": ["OPER_NAME", metric],
        "required_columns": ["OPER_NAME", metric],
        "group_columns": ["OPER_NAME"],
        "metric_columns": [metric],
        "strict_result_columns": True,
    }


def _validation_types(normalized: dict) -> set[str]:
    return {
        str(item.get("type") or "")
        for item in normalized["intent_plan"].get("validation_errors", [])
        if isinstance(item, dict)
    }


def test_today_boh_exact_domain_contract_owns_dataset_and_normalizes_display_sort_alias():
    normalizer = load_module(NORMALIZER_PATH)
    wip_today = _catalog_item(
        "wip_today",
        family="wip",
        time_scope="current_day",
        columns=["DATE", "OPER_NAME", "WIP"],
    )
    wip_history = _catalog_item(
        "wip",
        family="wip",
        time_scope="history",
        columns=["DATE", "OPER_NAME", "WIP"],
    )
    boh_domain = {
        "section": "quantity_terms",
        "key": "wip_boh_quantity",
        "payload": {
            "display_name": "BOH 재공",
            "aliases": ["아침 재공", "아침재공", "BOH"],
            "data_source": "wip",
            "column": "WIP",
            "aggregation_method": "sum",
            "temporal_semantics": {
                "business_timepoint": "BOH",
                "dataset_key": "wip",
                "date_param": "DATE",
                "requested_date_offset_days": -1,
                "disallowed_dataset_keys": ["wip_today"],
                "inherit_filters": True,
                "source_column": "WIP",
                "metric_aliases": ["아침 재공", "아침재공", "BOH"],
            },
        },
    }
    metadata = _metadata_envelope(
        bounded_catalog=[wip_today],
        registry_catalog=[wip_today, wip_history],
        domain_items=[boh_domain],
    )
    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "오늘 아침 재공 알려줘",
                "reference_date": "20260820",
            }
        },
        {
            "metadata_refs": [
                {"section": "quantity_terms", "key": "wip_boh_quantity"},
                {"section": "table_catalog", "key": "wip_today"},
            ],
            "intent_plan": {
                "analysis_kind": "today_boh_wip",
                "retrieval_jobs": [
                    {
                        "dataset_key": "wip_today",
                        "source_alias": "wip",
                        "source_type": "oracle",
                        "required_params": {"DATE": "20260820"},
                    }
                ],
                "pandas_execution_plan": _aggregate_plan("wip", "WIP", sort_by="BOH"),
                "output_contract": _aggregate_contract("WIP"),
            },
        },
        metadata,
    )

    plan = normalized["intent_plan"]
    assert len(plan["retrieval_jobs"]) == 1
    assert plan["retrieval_jobs"][0]["dataset_key"] == "wip"
    assert plan["retrieval_jobs"][0]["source_alias"] == "wip"
    assert plan["retrieval_jobs"][0]["source_type"] == "oracle"
    assert plan["retrieval_jobs"][0]["required_params"] == {"DATE": "20260819"}
    assert plan["pandas_execution_plan"][1]["sort_by"] == "WIP"
    assert "metric_dataset_selection_unresolved" not in _validation_types(normalized)

    inspection = normalized["trace"]["inspection"]["intent"]
    assert inspection["business_time_guard"]["selection_source"] == "metadata_alias_lock"
    assert inspection["metric_dataset_selection"]["domain_temporal_locks"] == [
        {
            "source_alias": "wip",
            "dataset_key": "wip",
            "date_param": "DATE",
            "query_date": "20260819",
            "domain_ref": {
                "section": "quantity_terms",
                "key": "wip_boh_quantity",
            },
            "selection_source": "metadata_alias_lock",
        }
    ]
    assert inspection["temporal_contract_catalog_resolution"]["hydrated_dataset_keys"] == [
        "wip"
    ]
    assert any(
        item.get("reason") == "temporal_output_alias_contract"
        and item.get("from_column") == "BOH"
        and item.get("to_column") == "WIP"
        for item in inspection["temporal_metric_alignment"]["corrections"]
    )


def test_yesterday_production_uses_unique_same_family_history_sibling_from_registry():
    normalizer = load_module(NORMALIZER_PATH)
    production_today = _catalog_item(
        "production_today",
        family="production",
        time_scope="current_day",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    production_history = _catalog_item(
        "production",
        family="production",
        time_scope="history",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    unrelated_history = _catalog_item(
        "wip",
        family="wip",
        time_scope="history",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    metadata = _metadata_envelope(
        bounded_catalog=[production_today],
        registry_catalog=[production_today, production_history, unrelated_history],
    )
    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "어제 생산실적 알려줘",
                "reference_date": "20260820",
            }
        },
        {
            "metadata_refs": [
                {"section": "table_catalog", "key": "production_today"}
            ],
            "intent_plan": {
                "analysis_kind": "yesterday_production",
                "retrieval_jobs": [
                    {
                        "dataset_key": "production_today",
                        "source_alias": "production_source",
                        "source_type": "oracle",
                        "required_params": {"DATE": "20260819"},
                    }
                ],
                "pandas_execution_plan": _aggregate_plan(
                    "production_source", "PRODUCTION"
                ),
                "output_contract": _aggregate_contract("PRODUCTION"),
            },
        },
        metadata,
    )

    plan = normalized["intent_plan"]
    assert plan["retrieval_jobs"][0]["dataset_key"] == "production"
    assert plan["retrieval_jobs"][0]["required_params"] == {"DATE": "20260819"}
    assert "metric_dataset_selection_unresolved" not in _validation_types(normalized)
    inspection = normalized["trace"]["inspection"]["intent"]
    assert inspection["metric_dataset_selection"]["corrections"][0][
        "to_dataset_key"
    ] == "production"
    assert inspection["temporal_sibling_catalog_resolution"]["hydrated_dataset_keys"] == [
        "production"
    ]


def test_time_scope_recovery_does_not_cross_an_unrelated_catalog_family():
    normalizer = load_module(NORMALIZER_PATH)
    production_today = _catalog_item(
        "production_today",
        family="production",
        time_scope="current_day",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    unrelated_history = _catalog_item(
        "wip_history_with_production_column",
        family="wip",
        time_scope="history",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    candidates = {
        "table_catalog_items": [production_today, unrelated_history]
    }
    original_jobs = [
        {
            "dataset_key": "production_today",
            "source_alias": "production_source",
            "required_params": {"DATE": "20260819"},
        }
    ]

    jobs, trace = normalizer._reconcile_metric_dataset_selection(
        {"request": {"reference_date": "20260820"}},
        original_jobs,
        _aggregate_plan("production_source", "PRODUCTION"),
        candidates,
    )

    assert jobs == original_jobs
    assert trace["corrections"] == []
    assert trace["status"] == "unresolved"
    assert trace["unresolved"][0]["candidate_dataset_keys"] == []


def test_time_scope_recovery_keeps_existing_state_when_same_family_sibling_is_ambiguous():
    normalizer = load_module(NORMALIZER_PATH)
    production_today = _catalog_item(
        "production_today",
        family="production",
        time_scope="current_day",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    history_a = _catalog_item(
        "production_history_a",
        family="production",
        time_scope="history",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    history_b = _catalog_item(
        "production_history_b",
        family="production",
        time_scope="history",
        columns=["DATE", "OPER_NAME", "PRODUCTION"],
    )
    original_jobs = [
        {
            "dataset_key": "production_today",
            "source_alias": "production_source",
            "required_params": {"DATE": "20260819"},
        }
    ]

    jobs, trace = normalizer._reconcile_metric_dataset_selection(
        {"request": {"reference_date": "20260820"}},
        original_jobs,
        _aggregate_plan("production_source", "PRODUCTION"),
        {"table_catalog_items": [production_today, history_a, history_b]},
    )

    assert jobs == original_jobs
    assert trace["corrections"] == []
    assert trace["status"] == "unresolved"
    assert trace["unresolved"][0]["candidate_dataset_keys"] == [
        "production_history_a",
        "production_history_b",
    ]


def test_registered_temporal_display_alias_is_not_collected_as_a_source_column():
    normalizer = load_module(NORMALIZER_PATH)
    pandas_plan = _aggregate_plan("wip", "WIP", sort_by="BOH")
    aligned, trace = normalizer._align_temporal_metric_columns(
        pandas_plan,
        {
            "status": "applied",
            "temporal_semantics": [
                {
                    "source_alias": "wip",
                    "dataset_key": "wip",
                    "source_column": "WIP",
                    "metric_aliases": ["BOH", "아침 재공"],
                }
            ],
        },
        {
            "table_catalog_items": [
                _catalog_item(
                    "wip",
                    family="wip",
                    time_scope="history",
                    columns=["DATE", "OPER_NAME", "WIP"],
                )
            ]
        },
    )

    source_columns = normalizer._aggregation_source_columns_by_alias(
        aligned,
        {"wip"},
    )
    assert aligned[1]["sort_by"] == "WIP"
    assert "BOH" not in source_columns["wip"]
    assert {"OPER_NAME", "WIP"}.issubset(source_columns["wip"])
    assert trace["status"] == "applied"
