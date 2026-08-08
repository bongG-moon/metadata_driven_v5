from __future__ import annotations

from copy import deepcopy
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
    fixture_ids = {int(item["id"]) for item in base.representative_cases()}

    assert set(manifest) == fixture_ids | {31}
    assert sum(item["expected_route"] == "fast" for item in manifest.values()) == 22
    assert sum(item["expected_route"] == "complex" for item in manifest.values()) == 9
    assert {
        item["expected_analysis_execution_mode"] for item in manifest.values()
    } == {"deterministic_fast", "deterministic_contract", "llm_pandas"}


@pytest.mark.parametrize(
    ("case_id", "route", "analysis_mode", "executor_mode", "pandas_calls"),
    [
        (1, "fast", "deterministic_fast", "execute_fast_path_recipe", 0),
        (5, "complex", "deterministic_contract", "merge_metric_sources", 0),
        (8, "complex", "llm_pandas", "llm_generated_code", 1),
        (11, "complex", "deterministic_contract", "compare_presence", 0),
    ],
)
def test_manifest_fixtures_execute_expected_route_submode_and_call_contract(
    route_runtime,
    case_id: int,
    route: str,
    analysis_mode: str,
    executor_mode: str,
    pandas_calls: int,
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
    assert result["actual_route"] == route
    assert result["analysis_execution_mode"] == analysis_mode
    assert result["execution_mode"] == executor_mode
    assert result["pandas_model_calls"] == pandas_calls


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
