from __future__ import annotations

import pytest

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


@pytest.fixture(scope="module")
def normalizer():
    return load_module(NORMALIZER_PATH)


@pytest.mark.parametrize(
    "payload",
    [
        {"join_keys": {"left": ["ASSET_ID"], "right": ["EQUIPMENT_ID"]}},
        {
            "grain_policy": {
                "join_keys": {
                    "left": ["ASSET_ID"],
                    "right": ["EQUIPMENT_ID"],
                }
            }
        },
    ],
)
def test_side_specific_join_key_dict_is_not_metadata_grain(normalizer, payload):
    assert normalizer._metadata_key_columns({"payload": payload}, {}) == []


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("grain_columns", ["PRODUCT_ID"]),
        ("columns", ["PRODUCT_ID"]),
        ("group_by", ["PRODUCT_ID"]),
        ("product_key_columns", ["PRODUCT_ID"]),
    ],
)
def test_explicit_grain_field_is_used_before_side_specific_join_dict(
    normalizer,
    field,
    expected,
):
    payload = {
        "join_keys": {"left": ["ASSET_ID"], "right": ["EQUIPMENT_ID"]},
        field: expected,
    }

    assert normalizer._metadata_key_columns({"payload": payload}, {}) == expected


def test_grain_policy_explicit_grain_is_used_before_side_specific_join_dict(normalizer):
    payload = {
        "grain_policy": {
            "join_keys": {
                "left": ["ASSET_ID"],
                "right": ["EQUIPMENT_ID"],
            },
            "grain_columns": ["PRODUCT_ID"],
        }
    }

    assert normalizer._metadata_key_columns({"payload": payload}, {}) == [
        "PRODUCT_ID"
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"join_keys": ["MODEL_KEY", "ROUTE_KEY"]},
        {"grain_policy": {"join_keys": ["MODEL_KEY", "ROUTE_KEY"]}},
    ],
)
def test_legacy_shared_join_key_list_remains_a_metadata_grain(normalizer, payload):
    assert normalizer._metadata_key_columns({"payload": payload}, {}) == [
        "MODEL_KEY",
        "ROUTE_KEY",
    ]


def test_normalizer_keeps_side_specific_keys_in_join_only(normalizer):
    recipe_ref = {
        "section": "analysis_recipes",
        "key": "assignment_rate_enrichment",
    }
    metadata = {
        "domain_items": [
            {
                **recipe_ref,
                "payload": {
                    "source_datasets": ["assignment_source", "rate_source"],
                    "join_type": "left",
                    "join_keys": {
                        "left": ["ASSET_ID", "ROUTE_KEY"],
                        "right": ["EQUIPMENT_ID", "ROUTE_KEY"],
                    },
                    "right_value_columns": ["RATE_PER_HOUR"],
                    "preserve_left_rows": True,
                },
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "assignment_source",
                "payload": {
                    "columns": ["ASSET_ID", "ROUTE_KEY", "PRODUCT_ID"]
                },
            },
            {
                "dataset_key": "rate_source",
                "payload": {
                    "columns": ["EQUIPMENT_ID", "ROUTE_KEY", "RATE_PER_HOUR"]
                },
            },
        ],
        "main_flow_filters": [],
    }
    response = {
        "metadata_refs": [recipe_ref],
        "intent_plan": {
            "analysis_kind": "generic_assignment_rate_lookup",
            "request_scope": "new_analysis",
            "retrieval_jobs": [
                {
                    "dataset_key": "assignment_source",
                    "source_alias": "assignment",
                },
                {"dataset_key": "rate_source", "source_alias": "rate"},
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "join_assignment_rate",
                    "operation": "join",
                    "inputs": [
                        {"kind": "external_source", "ref": "assignment"},
                        {"kind": "external_source", "ref": "rate"},
                    ],
                    "left_source_alias": "assignment",
                    "right_source_alias": "rate",
                    "output_alias": "result",
                    "join_type": "left",
                }
            ],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["ASSET_ID", "RATE_PER_HOUR"],
            },
        },
    }

    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "generic assignment rate lookup"}, "trace": {}},
        response,
        {"metadata_candidates": metadata},
    )

    plan = normalized["intent_plan"]
    assert "resolved_grain_plan" not in plan
    assert not any(
        "{'left':" in str(value)
        for value in plan.get("intent_ir", {}).get("grain", {}).values()
    )
    join_step = plan["pandas_execution_plan"][0]
    assert join_step["left_on"] == ["ASSET_ID", "ROUTE_KEY"]
    assert join_step["right_on"] == ["EQUIPMENT_ID", "ROUTE_KEY"]
