from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "01d_metadata_candidates_builder.py"
)
NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("candidate_rescue_contract", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_normalizer():
    spec = importlib.util.spec_from_file_location(
        "candidate_filter_rescue_contract", NORMALIZER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _hold_recipe():
    return {
        "section": "analysis_recipes",
        "key": "current_hold_lot_selection",
        "payload": {
            "display_name": "현재 HOLD LOT 및 최신 HOLD 이력 조회 규칙",
            "aliases": ["현재 HOLD LOT", "HOLD LOT 목록", "HOLD 이력"],
            "current_selection": {
                "dataset_key": "lot_status",
                "filter": {
                    "HOLD_STAT": {"operator": "eq", "value": "OnHold"}
                },
            },
        },
    }


def _legacy_hold_recipe():
    return {
        "section": "analysis_recipes",
        "key": "current_hold_lot_list",
        "payload": {
            "display_name": "현재 HOLD LOT 목록",
            "aliases": ["현재 HOLD LOT", "HOLD LOT 목록", "HOLD 이력"],
            "selection_criteria": ["현재 HOLD LOT을 조회한다."],
        },
    }


def _catalog():
    return {
        "dataset_key": "lot_status",
        "payload": {
            "dataset_key": "lot_status",
            "display_name": "현재 LOT 현황",
            "columns": ["LOT_ID", "OPER_NAME", "MCP_NO", "HOLD_STAT"],
        },
    }


def _selected_domain_keys(question: str) -> list[str]:
    module = _load_module()
    result = module.build_metadata_candidates(
        {"request": {"question": question}},
        [_hold_recipe()],
        [_catalog()],
        [],
        min_table_items=1,
        max_table_items=1,
    )
    candidates = result.get("metadata_candidates", {})
    return [
        str(item.get("key") or "")
        for item in candidates.get("domain_items", [])
        if isinstance(item, dict)
    ]


def _selected_legacy_domain_keys(question: str) -> list[str]:
    module = _load_module()
    result = module.build_metadata_candidates(
        {"request": {"question": question}},
        [_legacy_hold_recipe()],
        [_catalog()],
        [],
        min_table_items=1,
        max_table_items=1,
    )
    candidates = result.get("metadata_candidates", {})
    return [
        str(item.get("key") or "")
        for item in candidates.get("domain_items", [])
        if isinstance(item, dict)
    ]


def test_generic_lot_noun_in_recipe_key_does_not_activate_hold_recipe():
    assert "current_hold_lot_selection" not in _selected_domain_keys(
        "WSD 공정 L-085 제품 몇 Lot 보유 하고 있는지 알려줘"
    )


def test_explicit_hold_word_still_activates_hold_recipe():
    assert "current_hold_lot_selection" in _selected_domain_keys(
        "TSSJQ07AH LOT Hold 이력 조회해줘"
    )


def test_legacy_recipe_key_generic_lot_noun_is_not_a_strong_activation_signal():
    assert "current_hold_lot_list" not in _selected_legacy_domain_keys(
        "WSD 공정 L-085 제품 몇 Lot 보유 하고 있는지 알려줘"
    )
    assert "current_hold_lot_list" in _selected_legacy_domain_keys(
        "TSSJQ07AH LOT Hold 이력 조회해줘"
    )


def test_generic_dataset_identity_keeps_existing_strong_candidate_signal():
    module = _load_module()
    score, strong_hits = module._score_details(
        {
            "section": "table_catalog",
            "dataset_key": "production",
            "payload": {"dataset_key": "production"},
        },
        ["production"],
    )

    assert score >= 12
    assert strong_hits == 1


def test_rejected_stage_recipe_removes_only_its_exact_unrequested_filter():
    normalizer = _load_normalizer()
    hold_ref = {"section": "analysis_recipes", "key": "current_hold_lot_selection"}
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_status",
            "filters": {
                "OPER_NAME": {"operator": "in", "value": ["WSD1", "WSD2"]},
                "MCP_NO": {"operator": "contains", "value": "L-085"},
                "HOLD_STAT": {"operator": "eq", "value": "OnHold"},
            },
        }
    ]

    reconciled, trace = normalizer._remove_unselected_domain_filter_conditions(
        "WSD 공정 L-085 제품 몇 Lot 보유 하고 있는지 알려줘",
        jobs,
        {"domain_items": [_hold_recipe()], "table_catalog_items": [_catalog()]},
        [hold_ref],
        [],
    )

    assert set(reconciled[0]["filters"]) == {"OPER_NAME", "MCP_NO"}
    assert trace["status"] == "applied"
    assert trace["removed_filters"] == [
        {
            "source_alias": "lot_status",
            "dataset_key": "lot_status",
            "field": "HOLD_STAT",
            "condition": {"operator": "eq", "value": "OnHold"},
            "metadata_refs": [hold_ref],
            "reason": "unselected_metadata_exact_filter",
        }
    ]


def test_explicit_or_independently_locked_domain_condition_is_never_removed():
    normalizer = _load_normalizer()
    hold_ref = {"section": "analysis_recipes", "key": "current_hold_lot_selection"}
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_status",
            "filters": {"HOLD_STAT": {"operator": "eq", "value": "OnHold"}},
        }
    ]
    candidates = {
        "domain_items": [_hold_recipe()],
        "table_catalog_items": [_catalog()],
    }

    explicit, explicit_trace = normalizer._remove_unselected_domain_filter_conditions(
        "현재 HOLD LOT 알려줘", jobs, candidates, [hold_ref], []
    )
    locked, locked_trace = normalizer._remove_unselected_domain_filter_conditions(
        "LOT 알려줘", jobs, candidates, [hold_ref], [hold_ref]
    )

    assert explicit == jobs
    assert explicit_trace["removed_filters"] == []
    assert locked == jobs
    assert locked_trace["removed_filters"] == []


def test_non_hold_lot_replay_drops_stale_hold_filter_during_full_normalization():
    normalizer = _load_normalizer()
    hold_ref = {"section": "analysis_recipes", "key": "current_hold_lot_selection"}
    function_ref = {
        "section": "pandas_function_cases",
        "key": "product_token_match",
    }
    catalog_ref = {"section": "table_catalog", "key": "lot_status"}
    function_item = {
        **function_ref,
        "payload": {
            "function_name": "match_product_tokens",
            "description": "등록 제품 token을 source row와 매칭한다.",
            "input_contract": {"input_text": "product token"},
        },
    }
    metadata = {
        "metadata_candidates": {
            "domain_items": [_hold_recipe(), function_item],
            "table_catalog_items": [_catalog()],
            "main_flow_filters": [],
        },
        "table_catalog_registry": {"items": [_catalog()]},
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }
    normalized = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "WSD 공정 L-085 제품 몇 Lot 보유 하고 있는지 알려줘"
            }
        },
        {
            "metadata_refs": [hold_ref, function_ref, catalog_ref],
            "intent_plan": {
                "analysis_kind": "lot_count_by_product",
                "request_scope": "new_analysis",
                "retrieval_jobs": [
                    {
                        "dataset_key": "lot_status",
                        "source_alias": "lot_status",
                        "filters": {
                            "MCP_NO": {"operator": "contains", "value": "L-085"},
                            "HOLD_STAT": {"operator": "eq", "value": "OnHold"},
                        },
                    }
                ],
                "pandas_function_cases": [
                    {
                        "key": "product_token_match",
                        "function_name": "match_product_tokens",
                        "input_text": "L-085",
                        "source_alias": "lot_status",
                    }
                ],
                "pandas_execution_plan": [
                    {
                        "node_id": "function_case_1_product_token_match",
                        "operation": "apply_pandas_function_case",
                        "function_case_key": "product_token_match",
                        "function_name": "match_product_tokens",
                        "input_text": "L-085",
                        "source_alias": "lot_status",
                        "inputs": [
                            {"kind": "external_source", "ref": "lot_status"}
                        ],
                        "output_alias": "lot_status_function_case",
                    },
                    {
                        "node_id": "count_lots",
                        "operation": "count_rows",
                        "inputs": [
                            {"kind": "external_source", "ref": "lot_status"}
                        ],
                        "source_alias": "lot_status",
                        "output_alias": "lot_count",
                    }
                ],
                "output_contract": {
                    "result_mode": "aggregate",
                    "metric_columns": ["ROW_COUNT"],
                    "required_columns": ["ROW_COUNT"],
                    "result_columns": ["ROW_COUNT"],
                    "strict_result_columns": True,
                },
            },
        },
        metadata,
    )

    filters = normalized["intent_plan"]["retrieval_jobs"][0]["filters"]
    assert "MCP_NO" in filters
    assert "HOLD_STAT" not in filters
    count_step = next(
        step
        for step in normalized["intent_plan"]["pandas_execution_plan"]
        if step.get("node_id") == "count_lots"
    )
    assert count_step["inputs"] == [
        {"kind": "node_output", "ref": "function_case_1_product_token_match"}
    ]
    trace = normalized["trace"]["inspection"]["intent"]
    assert trace["ungrounded_domain_filter_reconciliation"]["status"] == "applied"
    assert trace["function_case_terminal_lineage_reconciliation"]["status"] == "applied"
