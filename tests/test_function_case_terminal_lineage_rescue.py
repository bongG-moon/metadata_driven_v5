from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _case() -> dict:
    return {
        "key": "product_token_match",
        "function_name": "match_product_tokens",
        "input_text": "L-085",
        "source_alias": "lot_status",
    }


def _jobs() -> list[dict]:
    return [{"dataset_key": "lot_status", "source_alias": "lot_status"}]


def _plan() -> list[dict]:
    return [
        {
            "node_id": "function_case_1_product_token_match",
            "operation": "apply_pandas_function_case",
            "function_case_key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "L-085",
            "source_alias": "lot_status",
            "inputs": [{"kind": "external_source", "ref": "lot_status"}],
            "output_alias": "lot_status_function_case",
        },
        {
            "node_id": "count_lots",
            "operation": "count_rows",
            "source_alias": "lot_status",
            "inputs": [{"kind": "external_source", "ref": "lot_status"}],
            "output_alias": "lot_count",
        },
    ]


def test_single_unconsumed_function_case_is_wired_to_terminal_consumer():
    normalizer = load_module(NORMALIZER_PATH)

    repaired, trace = normalizer._reconcile_unconsumed_function_case_terminal_lineage(
        [_case()], _plan(), _jobs()
    )

    assert repaired[1]["inputs"] == [
        {"kind": "node_output", "ref": "function_case_1_product_token_match"}
    ]
    assert trace["status"] == "applied"
    assert trace["repairs"][0]["consumer_node_id"] == "count_lots"


def test_existing_helper_consumer_is_left_unchanged():
    normalizer = load_module(NORMALIZER_PATH)
    plan = _plan()
    plan[1]["inputs"] = [
        {"kind": "node_output", "ref": "lot_status_function_case"}
    ]
    original = deepcopy(plan)

    repaired, trace = normalizer._reconcile_unconsumed_function_case_terminal_lineage(
        [_case()], plan, _jobs()
    )

    assert repaired == original
    assert trace["status"] == "not_needed"


def test_branch_or_second_helper_never_triggers_implicit_rewire():
    normalizer = load_module(NORMALIZER_PATH)
    branched = _plan()
    branched.append(
        {
            "node_id": "select_lots",
            "operation": "select_columns",
            "source_alias": "lot_status",
            "inputs": [{"kind": "external_source", "ref": "lot_status"}],
            "output_alias": "lot_rows",
            "columns": ["LOT_ID"],
        }
    )
    second_helper = _plan()
    second_helper.insert(
        1,
        {
            "node_id": "function_case_2",
            "operation": "apply_pandas_function_case",
            "function_case_key": "another_case",
            "function_name": "another_helper",
            "input_text": "L-085",
            "source_alias": "lot_status",
            "inputs": [{"kind": "external_source", "ref": "lot_status"}],
            "output_alias": "lot_status_function_case_2",
        },
    )

    branch_result, branch_trace = (
        normalizer._reconcile_unconsumed_function_case_terminal_lineage(
            [_case()], branched, _jobs()
        )
    )
    helper_result, helper_trace = (
        normalizer._reconcile_unconsumed_function_case_terminal_lineage(
            [_case()], second_helper, _jobs()
        )
    )

    assert branch_result == branched
    assert branch_trace["status"] == "not_needed"
    assert helper_result == second_helper
    assert helper_trace["status"] == "not_needed"
