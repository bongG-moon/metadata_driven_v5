from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _metadata(
    *,
    right_value_columns: list[str] | None = None,
    join_keys: object | None = None,
) -> dict:
    recipe_payload = {
        "source_datasets": ["assignment_registry", "throughput_reference"],
        "join_type": "left",
        "join_keys": (
            join_keys
            if join_keys is not None
            else ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"]
        ),
        "preserve_left_rows": True,
    }
    if right_value_columns is not None:
        recipe_payload["right_value_columns"] = right_value_columns
    return {
        "domain_items": [
            {
                "section": "analysis_recipes",
                "key": "assignment_throughput_enrichment",
                "payload": recipe_payload,
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "assignment_registry",
                "payload": {
                    "columns": [
                        "ASSET_ID",
                        "MODEL_KEY",
                        "ROUTE_KEY",
                        "STEP_NAME",
                        "PRODUCT_CODE",
                    ]
                },
            },
            {
                "dataset_key": "throughput_reference",
                "payload": {
                    "columns": [
                        "MODEL_KEY",
                        "ROUTE_KEY",
                        "STEP_NAME",
                        "RATE_PER_HOUR",
                    ]
                },
            },
        ],
        "main_flow_filters": [],
    }


def _jobs() -> list[dict]:
    return [
        {"dataset_key": "assignment_registry", "source_alias": "assignment_frame"},
        {"dataset_key": "throughput_reference", "source_alias": "throughput_frame"},
    ]


def _join_step(**overrides: object) -> dict:
    step = {
        "node_id": "join_assignment_throughput",
        "operation": "join",
        "inputs": [
            {"kind": "external_source", "ref": "assignment_frame"},
            {"kind": "external_source", "ref": "throughput_frame"},
        ],
        "left_source_alias": "assignment_frame",
        "right_source_alias": "throughput_frame",
        "output_alias": "enriched_assignment",
        "join_type": "left",
    }
    step.update(overrides)
    return step


def _recipe_ref() -> dict[str, str]:
    return {
        "section": "analysis_recipes",
        "key": "assignment_throughput_enrichment",
    }


def _protect_recipe(metadata: dict, *keys: str) -> dict:
    metadata["metadata_load"] = {
        "status": "ok",
        "loads": {"table_catalog_items": {"status": "ok"}},
        "protected_domain_candidates": [
            {
                "section": "analysis_recipes",
                "key": key,
                "matched_aliases": ["assignment rate"],
                "retained_after_byte_fit": True,
            }
            for key in keys
        ],
    }
    return metadata


def test_selected_complete_recipe_materializes_generic_row_enrichment_join():
    """A fully structured recipe takes priority without dataset-specific code."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    steps = [_join_step()]
    selection = normalizer._selected_recipe_join_contracts(
        {}, [_recipe_ref()], metadata, _jobs(), steps
    )

    assert len(selection["materializable"]) == 1
    resolved = normalizer._resolve_join_plan(
        {},
        [_recipe_ref()],
        metadata,
        _jobs(),
        steps,
        selection["materializable"],
    )
    materialized, trace = normalizer._materialize_resolved_join_steps(
        steps,
        resolved,
        selection["shadow_recommendations"],
    )

    assert resolved[0]["contract_origin"] == "selected_analysis_recipe"
    assert resolved[0]["key_source"] == "selected_analysis_recipe"
    assert resolved[0]["left_dataset_key"] == "assignment_registry"
    assert resolved[0]["right_dataset_key"] == "throughput_reference"
    assert materialized[0]["left_on"] == ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"]
    assert materialized[0]["right_on"] == ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"]
    assert materialized[0]["right_value_columns"] == ["RATE_PER_HOUR"]
    assert trace["status"] == "applied"
    assert trace["shadow_recommendations"] == []


def test_side_specific_recipe_join_keys_materialize_each_source_key():
    """A selected recipe may own different canonical key names on each source."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(
        right_value_columns=["RATE_PER_HOUR"],
        join_keys={
            "left": ["ASSET_ID", "ROUTE_KEY"],
            "right": ["EQUIPMENT_ID", "ROUTE_KEY"],
        },
    )
    metadata["table_catalog_items"][1]["payload"]["columns"].append(
        "EQUIPMENT_ID"
    )
    steps = [_join_step()]

    selection = normalizer._selected_recipe_join_contracts(
        {}, [_recipe_ref()], metadata, _jobs(), steps
    )
    assert len(selection["materializable"]) == 1
    contract = selection["materializable"][0]
    assert contract["join_key_shape"] == "side_specific"
    assert contract["left_keys"] == ["ASSET_ID", "ROUTE_KEY"]
    assert contract["right_keys"] == ["EQUIPMENT_ID", "ROUTE_KEY"]

    resolved = normalizer._resolve_join_plan(
        {},
        [_recipe_ref()],
        metadata,
        _jobs(),
        steps,
        selection["materializable"],
    )
    materialized, trace = normalizer._materialize_resolved_join_steps(
        steps,
        resolved,
        selection["shadow_recommendations"],
    )

    assert resolved[0]["left_keys"] == ["ASSET_ID", "ROUTE_KEY"]
    assert resolved[0]["right_keys"] == ["EQUIPMENT_ID", "ROUTE_KEY"]
    assert materialized[0]["left_on"] == ["ASSET_ID", "ROUTE_KEY"]
    assert materialized[0]["right_on"] == ["EQUIPMENT_ID", "ROUTE_KEY"]
    assert trace["status"] == "applied"


def test_side_specific_recipe_join_keys_require_each_source_to_own_its_side():
    """A missing side-owned key leaves the existing plan untouched and unblocked."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(
        right_value_columns=["RATE_PER_HOUR"],
        join_keys={
            "left": ["ASSET_ID"],
            "right": ["UNREGISTERED_EQUIPMENT_ID"],
        },
    )
    steps = [_join_step()]

    selection = normalizer._selected_recipe_join_contracts(
        {}, [_recipe_ref()], metadata, _jobs(), steps
    )
    materialized, trace = normalizer._materialize_resolved_join_steps(
        steps,
        [],
        selection["shadow_recommendations"],
    )

    assert selection == {"materializable": [], "shadow_recommendations": []}
    assert materialized == steps
    assert trace["status"] == "not_needed"


def test_protected_complete_join_recipe_is_locked_without_llm_metadata_ref():
    """A retained unique alias may select only a fully Catalog-proven join recipe."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _protect_recipe(
        _metadata(right_value_columns=["RATE_PER_HOUR"]),
        "assignment_throughput_enrichment",
    )

    selection = normalizer._resolve_execution_domain_selection(
        "assignment rate lookup",
        metadata,
        [],
    )

    assert selection["status"] == "locked"
    assert selection["locked_metadata_refs"] == [_recipe_ref()]
    assert selection["matched_aliases"][0]["kinds"] == ["join_recipe"]
    assert selection["matched_aliases"][0]["selection_evidence"] == (
        "protected_direct_alias"
    )

    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "assignment rate lookup"}, "trace": {}},
        {
            "intent_plan": {
                "analysis_kind": "generic_assignment_rate_lookup",
                "request_scope": "new_analysis",
                "retrieval_jobs": [_jobs()[0]],
                "pandas_execution_plan": [],
                "output_contract": {
                    "result_mode": "detail",
                    "result_columns": ["ASSET_ID", "RATE_PER_HOUR"],
                },
            }
        },
        {"metadata_candidates": metadata},
    )

    plan = normalized["intent_plan"]
    assert [item["dataset_key"] for item in plan["retrieval_jobs"]] == [
        "assignment_registry",
        "throughput_reference",
    ]
    assert plan["pandas_execution_plan"][0]["right_value_columns"] == [
        "RATE_PER_HOUR"
    ]
    assert _recipe_ref() in normalized["metadata_refs"]
    assert not plan.get("validation_errors")


def test_unprotected_incomplete_or_ambiguous_recipe_is_not_alias_locked():
    """Protected trace is evidence only; absence, incompleteness, and competition remain unchanged."""

    normalizer = load_module(NORMALIZER_PATH)
    complete = _metadata(right_value_columns=["RATE_PER_HOUR"])
    unprotected = normalizer._resolve_execution_domain_selection(
        "assignment rate lookup",
        complete,
        [],
    )
    assert unprotected["locked_metadata_refs"] == []

    incomplete = _protect_recipe(
        _metadata(),
        "assignment_throughput_enrichment",
    )
    incomplete_selection = normalizer._resolve_execution_domain_selection(
        "assignment rate lookup",
        incomplete,
        [],
    )
    assert incomplete_selection["locked_metadata_refs"] == []

    ambiguous = _metadata(right_value_columns=["RATE_PER_HOUR"])
    alternative = deepcopy(ambiguous["domain_items"][0])
    alternative["key"] = "assignment_throughput_enrichment_alternative"
    ambiguous["domain_items"].append(alternative)
    _protect_recipe(
        ambiguous,
        "assignment_throughput_enrichment",
        "assignment_throughput_enrichment_alternative",
    )
    ambiguous_selection = normalizer._resolve_execution_domain_selection(
        "assignment rate lookup",
        ambiguous,
        [],
    )
    assert ambiguous_selection["status"] == "ambiguous"
    assert ambiguous_selection["locked_metadata_refs"] == []
    assert ambiguous_selection["ambiguities"][0]["issue"] == (
        "ambiguous_protected_join_recipe"
    )


def test_post_join_metric_is_not_rebound_to_stale_left_source_alias():
    """A metric on a two-source node output is not treated as a raw left column."""

    normalizer = load_module(NORMALIZER_PATH)
    steps = [
        _join_step(
            left_on=["MODEL_KEY"],
            right_on=["MODEL_KEY"],
            right_value_columns=["RATE_PER_HOUR"],
        ),
        {
            "node_id": "sum_joined_rate",
            "operation": "groupby_and_aggregate",
            "inputs": [
                {"kind": "node_output", "ref": "join_assignment_throughput"}
            ],
            "output_alias": "rate_summary",
            # This stale alias used to push RATE_PER_HOUR onto the left Catalog.
            "source_alias": "assignment_frame",
            "group_by": ["STEP_NAME"],
            "aggregations": [
                {
                    "column": "RATE_PER_HOUR",
                    "method": "sum",
                    "output_column": "TOTAL_RATE",
                }
            ],
        },
    ]

    source_columns = normalizer._aggregation_source_columns_by_alias(
        steps,
        {"assignment_frame", "throughput_frame"},
    )

    assert "RATE_PER_HOUR" not in source_columns.get("assignment_frame", [])
    assert "STEP_NAME" not in source_columns.get("assignment_frame", [])


def test_direct_external_source_metric_keeps_raw_dataset_reconciliation():
    """The node-output guard does not alter an ordinary direct-source aggregate."""

    normalizer = load_module(NORMALIZER_PATH)
    source_columns = normalizer._aggregation_source_columns_by_alias(
        [
            {
                "node_id": "count_assets",
                "operation": "groupby_and_aggregate",
                "inputs": [
                    {"kind": "external_source", "ref": "assignment_frame"}
                ],
                "output_alias": "asset_summary",
                "source_alias": "assignment_frame",
                "group_by": ["STEP_NAME"],
                "aggregations": [
                    {
                        "column": "ASSET_ID",
                        "method": "nunique",
                        "output_column": "ASSET_COUNT",
                    }
                ],
            }
        ],
        {"assignment_frame", "throughput_frame"},
    )

    assert source_columns == {
        "assignment_frame": ["ASSET_ID", "STEP_NAME"]
    }


def test_unselected_or_incomplete_recipe_does_not_enter_new_priority_path():
    """A new contract requires both selection and declared right-side ownership."""

    normalizer = load_module(NORMALIZER_PATH)
    steps = [_join_step()]

    unselected = normalizer._selected_recipe_join_contracts(
        {}, [], _metadata(right_value_columns=["RATE_PER_HOUR"]), _jobs(), steps
    )
    incomplete = normalizer._selected_recipe_join_contracts(
        {}, [_recipe_ref()], _metadata(), _jobs(), steps
    )

    assert unselected == {"materializable": [], "shadow_recommendations": []}
    assert incomplete == {"materializable": [], "shadow_recommendations": []}


def test_recipe_key_conflict_is_shadowed_without_overwriting_existing_typed_join():
    """A wrong model key remains executable legacy input while the trace records the recommendation."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    steps = [_join_step(on=["PRODUCT_CODE"])]
    original = deepcopy(steps)
    selection = normalizer._selected_recipe_join_contracts(
        {}, [_recipe_ref()], metadata, _jobs(), steps
    )
    resolved = normalizer._resolve_join_plan(
        {},
        [_recipe_ref()],
        metadata,
        _jobs(),
        steps,
        selection["materializable"],
    )
    materialized, trace = normalizer._materialize_resolved_join_steps(
        steps,
        resolved,
        selection["shadow_recommendations"],
    )

    assert materialized == original
    assert trace["status"] == "shadow"
    shadow = trace["shadow_recommendations"]
    assert len(shadow) == 1
    assert shadow[0]["reason"] == "typed_join_contract_conflict"
    assert shadow[0]["recommended"]["left_on"] == [
        "MODEL_KEY",
        "ROUTE_KEY",
        "STEP_NAME",
    ]
    assert shadow[0]["recommended"]["right_value_columns"] == ["RATE_PER_HOUR"]
    assert shadow[0]["observed"]["conflicts"]["on"] == ["PRODUCT_CODE"]


def test_normalizer_exposes_recipe_conflict_as_trace_only_shadow():
    """The end-to-end normalizer keeps the existing join and does not add a gate error."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    response = {
        "metadata_refs": [_recipe_ref()],
        "intent_plan": {
            "analysis_kind": "generic_assignment_rate_lookup",
            "retrieval_jobs": _jobs(),
            "pandas_execution_plan": [_join_step(on=["PRODUCT_CODE"])],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["ASSET_ID", "RATE_PER_HOUR"],
            },
        },
    }

    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "assignment rate lookup"}, "trace": {}},
        response,
        {"metadata_candidates": metadata},
    )

    join_step = normalized["intent_plan"]["pandas_execution_plan"][0]
    shadow = normalized["trace"]["inspection"]["intent"][
        "typed_join_contract_materialization"
    ]["shadow_recommendations"]
    assert join_step["on"] == ["PRODUCT_CODE"]
    assert "left_on" not in join_step
    assert shadow[0]["reason"] == "typed_join_contract_conflict"
    assert not normalized["intent_plan"].get("validation_errors")


def test_multiple_selected_recipes_for_one_typed_join_stay_shadow_only():
    """Two complete registrations never cause arbitrary recipe selection."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    duplicate = deepcopy(metadata["domain_items"][0])
    duplicate["key"] = "assignment_throughput_enrichment_alternative"
    metadata["domain_items"].append(duplicate)
    refs = [
        _recipe_ref(),
        {
            "section": "analysis_recipes",
            "key": "assignment_throughput_enrichment_alternative",
        },
    ]
    steps = [_join_step()]
    selection = normalizer._selected_recipe_join_contracts(
        {}, refs, metadata, _jobs(), steps
    )
    materialized, trace = normalizer._materialize_resolved_join_steps(
        steps,
        [],
        selection["shadow_recommendations"],
    )

    assert selection["materializable"] == []
    assert materialized == steps
    assert trace["status"] == "shadow"
    assert {
        item["reason"] for item in trace["shadow_recommendations"]
    } == {"multiple_selected_recipe_contracts_match_typed_join"}


def test_unique_selected_recipe_adds_only_missing_parameter_free_source_and_join():
    """One existing recipe source can be completed without question-specific code."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    jobs, steps, trace = normalizer._complete_selected_recipe_source_join_plan(
        {"request": {"question": "assignment rate lookup"}},
        {"analysis_kind": "generic_assignment_rate_lookup"},
        [_recipe_ref()],
        metadata,
        [_jobs()[0]],
        [],
    )

    assert [item["dataset_key"] for item in jobs] == [
        "assignment_registry",
        "throughput_reference",
    ]
    assert steps == [
        {
            "node_id": "selected_recipe_join_assignment_throughput_enrichment",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "assignment_frame"},
                {"kind": "external_source", "ref": "throughput_reference"},
            ],
            "output_alias": "assignment_throughput_enrichment_result",
            "left_source_alias": "assignment_frame",
            "right_source_alias": "throughput_reference",
            "join_type": "left",
            "population_policy": "preserve_left_rows",
            "left_on": ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"],
            "right_on": ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"],
            "right_value_columns": ["RATE_PER_HOUR"],
            "multi_match_policy": "preserve_rows",
        }
    ]
    assert trace["status"] == "applied"
    assert trace["added_source"] == {
        "dataset_key": "throughput_reference",
        "source_alias": "throughput_reference",
    }


def test_recipe_plan_rescue_preserves_side_specific_join_key_ownership():
    """The missing-source rescue emits left_on/right_on from their own Catalogs."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(
        right_value_columns=["RATE_PER_HOUR"],
        join_keys={"left": ["ASSET_ID"], "right": ["EQUIPMENT_ID"]},
    )
    metadata["table_catalog_items"][1]["payload"]["columns"].append(
        "EQUIPMENT_ID"
    )

    jobs, steps, trace = normalizer._complete_selected_recipe_source_join_plan(
        {"request": {"question": "assignment rate lookup"}},
        {"analysis_kind": "generic_assignment_rate_lookup"},
        [_recipe_ref()],
        metadata,
        [_jobs()[0]],
        [],
    )

    assert [item["dataset_key"] for item in jobs] == [
        "assignment_registry",
        "throughput_reference",
    ]
    assert steps[0]["left_on"] == ["ASSET_ID"]
    assert steps[0]["right_on"] == ["EQUIPMENT_ID"]
    assert trace["left_on"] == ["ASSET_ID"]
    assert trace["right_on"] == ["EQUIPMENT_ID"]


def test_recipe_plan_rescue_never_revives_zero_source_clarification():
    """A selected recipe is evidence, not authority to override clarification."""

    normalizer = load_module(NORMALIZER_PATH)
    jobs, steps, trace = normalizer._complete_selected_recipe_source_join_plan(
        {"request": {"question": "assignment rate lookup"}},
        {"analysis_kind": "clarification", "request_scope": "clarification"},
        [_recipe_ref()],
        _metadata(right_value_columns=["RATE_PER_HOUR"]),
        [],
        [],
    )

    assert jobs == []
    assert steps == []
    assert trace == {
        "status": "not_needed",
        "reason": "explicit_clarification_preserved",
    }


def test_recipe_plan_rescue_preserves_existing_plan_and_unresolved_required_source():
    """Existing execution and a source needing an unknown parameter stay untouched."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    existing_steps = [
        {
            "node_id": "existing_projection",
            "operation": "select_columns",
            "inputs": [{"kind": "external_source", "ref": "assignment_frame"}],
            "output_alias": "existing_result",
            "source_alias": "assignment_frame",
            "columns": ["ASSET_ID"],
        }
    ]
    jobs, steps, trace = normalizer._complete_selected_recipe_source_join_plan(
        {"request": {"question": "assignment rate lookup"}},
        {"analysis_kind": "generic_assignment_rate_lookup"},
        [_recipe_ref()],
        metadata,
        [_jobs()[0]],
        existing_steps,
    )
    assert jobs == [_jobs()[0]]
    assert steps == existing_steps
    assert trace["reason"] == "existing_pandas_plan_preserved"

    required_metadata = deepcopy(metadata)
    required_metadata["table_catalog_items"][1]["payload"]["required_params"] = [
        "REFERENCE_DATE"
    ]
    jobs, steps, trace = normalizer._complete_selected_recipe_source_join_plan(
        {"request": {"question": "assignment rate lookup"}},
        {"analysis_kind": "generic_assignment_rate_lookup"},
        [_recipe_ref()],
        required_metadata,
        [_jobs()[0]],
        [],
    )
    assert jobs == [_jobs()[0]]
    assert steps == []
    assert trace["reason"] == "missing_recipe_source_required_params_unresolved"


def test_recipe_plan_rescue_does_not_choose_between_competing_complete_recipes():
    """Two complete selected recipes remain a planner ambiguity, not an arbitrary join."""

    normalizer = load_module(NORMALIZER_PATH)
    metadata = _metadata(right_value_columns=["RATE_PER_HOUR"])
    alternative = deepcopy(metadata["domain_items"][0])
    alternative["key"] = "assignment_throughput_enrichment_alternative"
    metadata["domain_items"].append(alternative)
    refs = [
        _recipe_ref(),
        {
            "section": "analysis_recipes",
            "key": "assignment_throughput_enrichment_alternative",
        },
    ]

    jobs, steps, trace = normalizer._complete_selected_recipe_source_join_plan(
        {"request": {"question": "assignment rate lookup"}},
        {"analysis_kind": "generic_assignment_rate_lookup"},
        refs,
        metadata,
        [_jobs()[0]],
        [],
    )

    assert jobs == [_jobs()[0]]
    assert steps == []
    assert trace["reason"] == "selected_complete_recipe_not_unique"
    assert trace["complete_recipe_count"] == 2


def test_normalizer_integrates_unique_selected_recipe_rescue_without_gate_error():
    """The complete recipe becomes a typed left join and remains Catalog-proven end to end."""

    normalizer = load_module(NORMALIZER_PATH)
    response = {
        "metadata_refs": [_recipe_ref()],
        "intent_plan": {
            "analysis_kind": "generic_assignment_rate_lookup",
            "request_scope": "new_analysis",
            "retrieval_jobs": [_jobs()[0]],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["ASSET_ID", "RATE_PER_HOUR"],
            },
        },
    }

    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "assignment rate lookup"}, "trace": {}},
        response,
        {"metadata_candidates": _metadata(right_value_columns=["RATE_PER_HOUR"])},
    )

    plan = normalized["intent_plan"]
    assert [item["dataset_key"] for item in plan["retrieval_jobs"]] == [
        "assignment_registry",
        "throughput_reference",
    ]
    assert len(plan["pandas_execution_plan"]) == 1
    assert plan["pandas_execution_plan"][0]["left_on"] == [
        "MODEL_KEY",
        "ROUTE_KEY",
        "STEP_NAME",
    ]
    assert plan["pandas_execution_plan"][0]["right_value_columns"] == [
        "RATE_PER_HOUR"
    ]
    assert not plan.get("validation_errors")
    rescue = normalized["trace"]["inspection"]["intent"][
        "selected_recipe_plan_rescue"
    ]
    assert rescue["status"] == "applied"
