from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _metadata(*, right_value_columns: list[str] | None = None) -> dict:
    recipe_payload = {
        "source_datasets": ["assignment_registry", "throughput_reference"],
        "join_type": "left",
        "join_keys": ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"],
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
    assert resolved[0]["left_dataset_key"] == "assignment_registry"
    assert resolved[0]["right_dataset_key"] == "throughput_reference"
    assert materialized[0]["left_on"] == ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"]
    assert materialized[0]["right_on"] == ["MODEL_KEY", "ROUTE_KEY", "STEP_NAME"]
    assert materialized[0]["right_value_columns"] == ["RATE_PER_HOUR"]
    assert trace["status"] == "applied"
    assert trace["shadow_recommendations"] == []


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
