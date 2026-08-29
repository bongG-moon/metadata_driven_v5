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


def _recipe_starts_with_domain():
    return {
        "section": "analysis_recipes",
        "key": "recipe_id_starts_with",
        "payload": {
            "display_name": "RECIPE 번호 시작값 조회 규칙",
            "aliases": ["RECIPE", "레시피", "RECIPE 번호", "레시피 번호"],
            "selection_criteria": {
                "field": "RECIPE_ID",
                "operator": "starts_with",
            },
        },
    }


def _equipment_product_operation_rate_recipe():
    return {
        "section": "analysis_recipes",
        "key": "equipment_product_operation_rate_merge",
        "payload": {
            "display_name": "장비 작업제품 가동률 결합 규칙",
            "aliases": ["장비 작업제품 가동률"],
            "dataset_keys": ["equipment_assign", "operation_rate_today"],
            "join_keys": ["EQP_ID"],
        },
    }


def _equipment_assignment_uph_recipe():
    return {
        "section": "analysis_recipes",
        "key": "equipment_assignment_uph_join",
        "payload": {
            "display_name": "장비 ASSIGN 및 UPH 결합 규칙",
            "aliases": ["장비 ASSIGN UPH"],
            "dataset_keys": ["equipment_assign", "eqp_uph"],
            "join_keys": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
        },
    }


def _holding_capacity_recipe():
    return {
        "section": "analysis_recipes",
        "key": "holding_capacity_calculation",
        "payload": {
            "display_name": "보유 CAPA 계산 규칙",
            "aliases": ["보유 CAPA", "보유Capa"],
            "source_datasets": ["equipment_assign", "eqp_uph"],
        },
    }


def _table_catalog(dataset_key: str):
    return {
        "section": "table_catalog",
        "key": dataset_key,
        "dataset_key": dataset_key,
        "payload": {
            "dataset_key": dataset_key,
            "display_name": dataset_key,
            "columns": ["EQP_ID"],
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


def test_section_taxonomy_is_not_a_strong_recipe_identity_signal():
    module = _load_module()
    score, strong_hits = module._score_details(
        {
            "section": "analysis_recipes",
            "key": "unrelated_capacity_rule",
            "payload": {"display_name": "보유 CAPA 계산"},
        },
        ["recipe"],
    )

    assert score < module.DOMAIN_MIN_SCORE
    assert strong_hits == 0


def test_exact_recipe_alias_is_protected_through_byte_trimming():
    module = _load_module()
    noisy_domains = [
        {
            "section": "analysis_recipes",
            "key": f"noise_rule_{index:02d}",
            "payload": {
                "aliases": [f"RECIPE 보조 규칙 {index}"],
                "description": "x" * 1400,
            },
        }
        for index in range(15)
    ]
    result = module.build_metadata_candidates(
        {"request": {"question": "FCB공정 R0429 RECIPE의 UPH알려줘"}},
        [*noisy_domains, _recipe_starts_with_domain()],
        [],
        [],
        max_domain_items=20,
        min_table_items=1,
        max_table_items=1,
        max_bytes=4096,
    )

    candidates = result["metadata_candidates"]
    selected_keys = [item.get("key") for item in candidates["domain_items"]]
    protected_trace = result["metadata_load"]["protected_domain_candidates"]

    assert "recipe_id_starts_with" in selected_keys
    assert module._json_bytes(candidates) <= 4096
    assert result["metadata_load"]["truncated_by_bytes"] is True
    assert {
        "section": "analysis_recipes",
        "key": "recipe_id_starts_with",
        "matched_aliases": ["RECIPE"],
        "retained_after_byte_fit": True,
    } in protected_trace


def test_generic_single_alias_is_not_protected():
    module = _load_module()
    result = module.build_metadata_candidates(
        {"request": {"question": "WSD 공정 제품별 현황 알려줘"}},
        [
            {
                "section": "analysis_recipes",
                "key": "generic_product_rule",
                "payload": {
                    "display_name": "무관한 일반 규칙",
                    "aliases": ["제품", "제품별"],
                },
            }
        ],
        [],
        [],
        min_table_items=1,
        max_table_items=1,
    )

    assert result["metadata_load"]["protected_domain_candidates"] == []


def test_non_selectable_function_case_exact_alias_is_not_protected_or_quota_promoted():
    module = _load_module()
    non_selectable = {
        "section": "pandas_function_cases",
        "key": "sample_passthrough_helper",
        "payload": {
            "display_name": "검증 전용 helper",
            "aliases": ["FCB공정 R0429 RECIPE"],
            "function_name": "sample_passthrough_helper",
        },
    }
    result = module.build_metadata_candidates(
        {"request": {"question": "FCB공정 R0429 RECIPE의 UPH알려줘"}},
        [non_selectable, _recipe_starts_with_domain()],
        [],
        [],
        max_domain_items=1,
        min_table_items=1,
        max_table_items=1,
    )

    selected_keys = [
        item.get("key")
        for item in result["metadata_candidates"]["domain_items"]
    ]
    protected_keys = [
        item.get("key")
        for item in result["metadata_load"]["protected_domain_candidates"]
    ]

    assert selected_keys == ["recipe_id_starts_with"]
    assert protected_keys == ["recipe_id_starts_with"]


def test_direct_phrase_rescue_does_not_displace_existing_dataset_dependency():
    module = _load_module()
    result = module.build_metadata_candidates(
        {"request": {"question": "장비 RECIPE 현황 알려줘"}},
        [
            {
                "section": "quantity_terms",
                "key": "equipment_count",
                "payload": {
                    "aliases": ["장비"],
                    "data_source": "equipment_assign",
                },
            },
            _recipe_starts_with_domain(),
        ],
        [],
        [],
        max_domain_items=1,
        min_table_items=1,
        max_table_items=1,
    )

    assert [
        item.get("key")
        for item in result["metadata_candidates"]["domain_items"]
    ] == ["equipment_count"]


def test_shared_non_generic_alias_remains_ranked_but_is_not_protected():
    module = _load_module()
    result = module.build_metadata_candidates(
        {"request": {"question": "공정 UPH 알려줘"}},
        [
            {
                "section": "quantity_terms",
                "key": "uph_metric",
                "payload": {"aliases": ["UPH"]},
            },
            {
                "section": "analysis_recipes",
                "key": "uph_summary",
                "payload": {"aliases": ["UPH"]},
            },
        ],
        [],
        [],
        min_table_items=1,
        max_table_items=1,
    )

    assert result["metadata_load"]["protected_domain_candidates"] == []


def test_registered_multiword_alias_matches_plural_query_suffix_and_rate_spelling():
    module = _load_module()

    assert module._registered_phrase_matches(
        "D/A공정 장비들 현재 작업제품들과 가동율현황 조회해줘",
        "장비 작업제품 가동률",
    )


def test_relaxed_alias_match_does_not_cross_single_letter_process_codes():
    module = _load_module()
    question = "D/A공정 장비들 현재 작업제품들과 가동율현황 조회해줘"
    process_domains = [
        {
            "section": "process_groups",
            "key": key,
            "payload": {"aliases": [alias], "processes": [alias]},
        }
        for key, alias in (
            ("DA", "D/A공정"),
            ("DC", "D/C공정"),
            ("DI", "D/I공정"),
            ("DP", "D/P공정"),
            ("DS", "D/S공정"),
        )
    ]

    matches = module._question_matched_domain_phrases(question, process_domains)

    assert [match["item"]["key"] for match in matches] == ["DA"]


def test_operation_rate_recipe_alias_recalls_its_registered_source_datasets():
    module = _load_module()
    result = module.build_metadata_candidates(
        {
            "request": {
                "question": "D/A공정 장비들 현재 작업제품들과 가동율현황 조회해줘"
            }
        },
        [_recipe_starts_with_domain(), _equipment_product_operation_rate_recipe()],
        [
            _table_catalog("lot_status"),
            _table_catalog("equipment_assign"),
            _table_catalog("operation_rate_today"),
        ],
        [],
        max_domain_items=1,
        min_table_items=1,
        max_table_items=2,
    )

    selected_domain_keys = [
        item.get("key")
        for item in result["metadata_candidates"]["domain_items"]
    ]
    selected_dataset_keys = {
        item.get("dataset_key")
        for item in result["metadata_candidates"]["table_catalog_items"]
    }

    assert selected_domain_keys == ["equipment_product_operation_rate_merge"]
    assert selected_dataset_keys == {"equipment_assign", "operation_rate_today"}


def test_operation_rate_question_does_not_recall_uph_recipe_or_source_from_generic_eqp_tokens():
    module = _load_module()
    domain_items = [
        _equipment_assignment_uph_recipe(),
        _equipment_product_operation_rate_recipe(),
    ]
    table_items = [
        _table_catalog("equipment_assign"),
        _table_catalog("eqp_uph"),
        _table_catalog("operation_rate_today"),
    ]

    operation_result = module.build_metadata_candidates(
        {
            "request": {
                "question": "D/A공정 장비들 현재 작업제품들과 가동율현황 조회해줘"
            }
        },
        domain_items,
        table_items,
        [],
        max_domain_items=2,
        min_table_items=1,
        max_table_items=2,
    )
    operation_domain_keys = {
        item.get("key")
        for item in operation_result["metadata_candidates"]["domain_items"]
    }
    operation_dataset_keys = {
        item.get("dataset_key")
        for item in operation_result["metadata_candidates"]["table_catalog_items"]
    }

    assert operation_domain_keys == {"equipment_product_operation_rate_merge"}
    assert operation_dataset_keys == {"equipment_assign", "operation_rate_today"}

    uph_result = module.build_metadata_candidates(
        {"request": {"question": "M/D공정 장비 ASSIGN 댓수와 UPH 알려줘"}},
        domain_items,
        table_items,
        [],
        max_domain_items=2,
        min_table_items=1,
        max_table_items=2,
    )
    uph_domain_keys = {
        item.get("key")
        for item in uph_result["metadata_candidates"]["domain_items"]
    }
    uph_dataset_keys = {
        item.get("dataset_key")
        for item in uph_result["metadata_candidates"]["table_catalog_items"]
    }

    assert "equipment_assignment_uph_join" in uph_domain_keys
    assert "eqp_uph" in uph_dataset_keys


def test_capacity_recipe_includes_exact_pair_join_recipe_as_dependency_context():
    """A selected calculation recipe exposes its registered execution join.

    This is pair-based metadata closure, so the question need not repeat the
    lower-level "ASSIGN + UPH" wording and unrelated equipment joins remain
    absent.
    """

    module = _load_module()
    result = module.build_metadata_candidates(
        {"request": {"question": "SBM공정 제품별 보유Capa 알려줘"}},
        [_holding_capacity_recipe(), _equipment_assignment_uph_recipe()],
        [_table_catalog("equipment_assign"), _table_catalog("eqp_uph")],
        [],
        max_domain_items=2,
        min_table_items=1,
        max_table_items=2,
    )

    candidate_keys = {
        item.get("key")
        for item in result["metadata_candidates"]["domain_items"]
    }
    dependencies = result["metadata_load"]["auto_join_recipe_dependencies"]

    assert candidate_keys == {
        "holding_capacity_calculation",
        "equipment_assignment_uph_join",
    }
    assert dependencies == [
        {
            "section": "analysis_recipes",
            "key": "equipment_assignment_uph_join",
        }
    ]


def test_multiword_alias_rescue_does_not_match_only_generic_entity_overlap():
    module = _load_module()

    assert not module._registered_phrase_matches(
        "장비 목록 조회해줘",
        "장비 작업제품 가동률",
    )


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
