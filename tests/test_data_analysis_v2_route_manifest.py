from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from tools import validate_data_analysis_v2_routes as validator
from tools import validate_representative_questions as base


@pytest.fixture(scope="module")
def route_runtime():
    if "lfx" not in sys.modules:
        base.install_lfx_stubs()
    return base.load_flow_modules(), validator._v2_modules()


def test_route_manifest_has_independent_expectations_for_every_representative_case():
    manifest = validator.load_route_manifest()
    fixture_cases = {int(item["id"]): item for item in base.representative_cases()}
    manifest_document = json.loads(Path(validator.MANIFEST_PATH).read_text(encoding="utf-8"))
    excluded_cases = {
        int(item["id"]): item
        for item in manifest_document.get("excluded_cases", [])
    }
    active_fixture_cases = {
        case_id: item
        for case_id, item in fixture_cases.items()
        if case_id not in excluded_cases
    }

    assert set(excluded_cases) == {5, 24, 25}
    assert set(manifest).isdisjoint(excluded_cases)
    assert set(manifest) == set(active_fixture_cases)
    assert set(manifest) | set(excluded_cases) == set(range(1, 32))
    assert len(manifest) == 28
    assert {item["expected_route"] for item in manifest.values()} == {"fast", "complex"}
    assert sum(item["expected_route"] == "fast" for item in manifest.values()) == 10
    assert sum(item["expected_route"] == "complex" for item in manifest.values()) == 18

    for case_id, fixture_case in active_fixture_cases.items():
        expectation = manifest[case_id]
        assert expectation["question"] == fixture_case["question"]
        assert expectation["expected_dataset_keys"]
        if expectation["expected_route"] == "fast":
            assert expectation["expected_recipe"]
            assert isinstance(expectation["fixture_plan"], dict)

    assert manifest[22]["expected_route"] == "complex"
    assert manifest[22]["expected_dataset_keys"] == [
        "equipment_assign",
        "operation_rate_today",
    ]
    assert manifest[23]["expected_route"] == "complex"
    assert manifest[23]["expected_dataset_keys"] == [
        "equipment_assign",
        "operation_rate_today",
    ]


def test_live_representative_validator_uses_active_v2_intent_prompt():
    source = Path(base.__file__).read_text(encoding="utf-8")

    assert 'render_prompt(V2_FLOW / "03_intent_prompt_template_ko.md", intent_vars)' in source
    assert 'render_prompt(FLOW / "03_intent_prompt_template_ko.md", intent_vars)' not in source


def test_dataset_selection_assertion_is_validation_only_and_reports_wrong_source():
    dataset_keys, errors = validator._dataset_selection_contract(
        {"intent_plan": {"retrieval_jobs": [{"dataset_key": "production"}]}},
        {"expected_dataset_keys": ["target"], "forbidden_dataset_keys": ["production"]},
    )

    assert dataset_keys == ["production"]
    assert errors == [
        "expected dataset keys ['target'], got ['production']",
        "forbidden dataset keys selected: ['production']",
    ]


def test_live_manifest_can_declare_a_narrow_equivalent_deterministic_mode():
    expectation = {
        "expected_execution_mode": "merge_metric_sources",
        "expected_execution_modes": [
            "merge_metric_sources",
            "execute_typed_pandas_plan",
        ],
    }

    assert validator._mode_matches(
        "execute_typed_pandas_plan",
        expectation,
        primary_key="expected_execution_mode",
        alternatives_key="expected_execution_modes",
    )
    assert not validator._mode_matches(
        "llm_generated_code",
        expectation,
        primary_key="expected_execution_mode",
        alternatives_key="expected_execution_modes",
    )


@pytest.mark.parametrize(
    "case_id",
    [
        1,
        2,
        3,
        28,
    ],
)
def test_manifest_fixtures_execute_expected_route_submode_and_call_contract(
    route_runtime,
    case_id: int,
):
    modules, v2 = route_runtime
    cases = {int(item["id"]): item for item in base.representative_cases()}
    expectation = validator.load_route_manifest()[case_id]

    result = validator.validate_case(
        cases[case_id],
        expectation,
        modules,
        v2,
        "20260701",
    )

    assert result["status"] == "ok", result["errors"]
    assert result["actual_route"] == expectation["expected_route"]
    assert validator._mode_matches(
        result["analysis_execution_mode"],
        expectation,
        primary_key="expected_analysis_execution_mode",
        alternatives_key="expected_analysis_execution_modes",
    )
    assert validator._mode_matches(
        result["execution_mode"],
        expectation,
        primary_key="expected_execution_mode",
        alternatives_key="expected_execution_modes",
    )
    assert result["pandas_model_calls"] == expectation["expected_model_calls"]["pandas_generation"]


def test_manifest_route_drift_is_reported_instead_of_becoming_the_oracle(route_runtime):
    modules, v2 = route_runtime
    case = base.representative_cases()[0]
    expectation = deepcopy(validator.load_route_manifest()[1])
    expectation["expected_route"] = "complex"

    result = validator.validate_case(
        case,
        expectation,
        modules,
        v2,
        "20260701",
    )

    assert result["actual_route"] == "fast"
    assert result["status"] == "error"
    assert "expected route complex, got fast" in result["errors"]
