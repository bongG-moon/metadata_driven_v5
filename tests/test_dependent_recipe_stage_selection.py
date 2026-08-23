from __future__ import annotations

from copy import deepcopy
import inspect

import pytest

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)


def _module():
    return load_module(NORMALIZER_PATH)


def _recipe() -> dict:
    """A business-neutral two-stage recipe used only by this test fixture."""

    return {
        "section": "analysis_recipes",
        "key": "entity_state_then_event_history",
        "payload": {
            "display_name": "현재 상태 및 HOLD 이력 조회 규칙",
            "aliases": ["현재 HOLD", "HOLD 이력"],
            "source_datasets": ["snapshot_records", "history_records"],
            "dependent_selection": {
                "current_stage": "current_selection",
                "next_stage": "history_selection",
                "when_question_includes_any": ["HOLD 이력"],
            },
            "current_selection": {
                "dataset_key": "snapshot_records",
                "time_scope": "current_day",
                "filter": {
                    "STATE": {"operator": "eq", "value": "OnHold"},
                },
                "result_columns": ["ENTITY_ID", "STATE", "CURRENT_REASON"],
            },
            "history_selection": {
                "dataset_key": "history_records",
                "time_scope": "history",
                "required_params": ["ENTITY_ID"],
                "result_columns": [
                    "ENTITY_ID",
                    "EVENT_TM",
                    "EVENT_CODE",
                    "EVENT_DESC",
                ],
            },
        },
    }


def _catalog(
    dataset_key: str,
    *,
    time_scope: str,
    columns: list[str],
    required_params: list[str] | None = None,
) -> dict:
    return {
        "section": "table_catalog",
        "key": dataset_key,
        "dataset_key": dataset_key,
        "payload": {
            "dataset_key": dataset_key,
            "source_type": "oracle",
            "time_scope": time_scope,
            "columns": columns,
            "default_detail_columns": columns,
            "required_params": list(required_params or []),
            "filter_mappings": {column: [column] for column in columns},
        },
    }


def _candidates(
    *,
    include_history_catalog: bool = True,
    history_time_scope: str = "history",
) -> dict:
    catalogs = [
        _catalog(
            "snapshot_records",
            time_scope="current_day",
            columns=["ENTITY_ID", "STATE", "CURRENT_REASON"],
        )
    ]
    if include_history_catalog:
        catalogs.append(
            _catalog(
                "history_records",
                time_scope=history_time_scope,
                columns=["ENTITY_ID", "EVENT_TM", "EVENT_CODE", "EVENT_DESC"],
                required_params=["ENTITY_ID"],
            )
        )
    return {
        "domain_items": [_recipe()],
        "table_catalog_items": catalogs,
        "main_flow_filters": [],
    }


def _plan(
    *,
    entity_value: str = "TSSJQ07AH",
    extra_filters: dict | None = None,
    include_implicit_limit: bool = False,
) -> tuple[dict, list[dict], list[dict]]:
    recipe_ref = {
        "section": "analysis_recipes",
        "key": "entity_state_then_event_history",
    }
    filters = {
        "ENTITY_ID": {"operator": "eq", "value": entity_value},
        "STATE": {"operator": "eq", "value": "OnHold"},
        **deepcopy(extra_filters or {}),
    }
    jobs = [
        {
            "dataset_key": "snapshot_records",
            "source_alias": "entity_source",
            "source_type": "oracle",
            "required": True,
            "filters": filters,
        }
    ]
    steps: list[dict] = [
        {
            "node_id": "select_current_fields",
            "operation": "select_columns",
            "inputs": [{"kind": "external_source", "ref": "entity_source"}],
            "source_alias": "entity_source",
            "columns": ["ENTITY_ID", "STATE", "CURRENT_REASON"],
        }
    ]
    output_contract = {
        "result_mode": "detail",
        "required_columns": ["ENTITY_ID", "STATE", "CURRENT_REASON"],
        "result_columns": ["ENTITY_ID", "STATE", "CURRENT_REASON"],
        "strict_result_columns": True,
        "grain_columns": ["ENTITY_ID"],
    }
    if include_implicit_limit:
        steps.append(
            {
                "node_id": "sort_history",
                "operation": "sort_and_top_n",
                "inputs": [
                    {"kind": "node_output", "ref": "select_current_fields"}
                ],
                "source_alias": "entity_source",
                "sort_by": "EVENT_TM",
                "order": "desc",
                "limit": 1,
            }
        )
        output_contract["fast_path_recipe"] = "ranked_summary"
        output_contract["ordering"] = {
            "sort_by": "EVENT_TM",
            "order": "desc",
            "limit": 1,
        }
    plan = {
        "analysis_kind": "entity_history_detail",
        "request_scope": "new_analysis",
        "metadata_refs": [recipe_ref],
        "retrieval_jobs": deepcopy(jobs),
        "pandas_execution_plan": deepcopy(steps),
        "output_contract": output_contract,
    }
    return plan, jobs, steps


def _reconcile(
    question: str,
    *,
    candidates: dict | None = None,
    entity_value: str = "TSSJQ07AH",
    extra_filters: dict | None = None,
    include_implicit_limit: bool = False,
):
    module = _module()
    plan, jobs, steps = _plan(
        entity_value=entity_value,
        extra_filters=extra_filters,
        include_implicit_limit=include_implicit_limit,
    )
    recipe_ref = deepcopy(plan["metadata_refs"])
    result = module._reconcile_selected_dependent_recipe_stage(
        question,
        plan,
        jobs,
        steps,
        candidates or _candidates(),
        recipe_ref,
    )
    return module, (plan, jobs, steps), result


def _assert_fail_soft_noop(before: tuple[dict, list[dict], list[dict]], result) -> None:
    plan, jobs, steps = before
    next_plan, next_jobs, next_steps, trace = result
    assert next_plan == plan
    assert next_jobs == jobs
    assert next_steps == steps
    assert trace["corrections"] == []
    assert trace["active_stages"] == []
    # An unresolved rescue is advisory only. It must not manufacture a gate or
    # validation error that makes an otherwise executable current-stage plan fail.
    assert "execution_gate" not in trace
    assert "validation_errors" not in trace


def test_exact_metadata_trigger_switches_to_dependent_history_stage() -> None:
    _, before, result = _reconcile("TSSJQ07AH LOT Hold 이력 조회해줘")
    next_plan, next_jobs, next_steps, trace = result

    assert trace["status"] == "applied"
    assert trace["active_stages"][0]["stage_name"] == "history_selection"
    assert trace["active_stages"][0]["matched_triggers"] == ["HOLD 이력"]
    assert trace["metadata_refs"] == [
        {"section": "table_catalog", "key": "history_records"}
    ]

    job = next_jobs[0]
    assert job["dataset_key"] == "history_records"
    assert job["source_alias"] == "entity_source"
    assert job["required_params"] == {"ENTITY_ID": "TSSJQ07AH"}
    assert job["filters"] == {
        "ENTITY_ID": {"operator": "eq", "value": "TSSJQ07AH"}
    }
    assert trace["corrections"][0]["removed_stage_filters"] == [
        {"field": "STATE", "reason": "inactive_recipe_stage_filter"}
    ]

    expected_columns = ["ENTITY_ID", "EVENT_TM", "EVENT_CODE", "EVENT_DESC"]
    assert next_plan["output_contract"]["result_columns"] == expected_columns
    assert next_plan["output_contract"]["required_columns"] == expected_columns
    assert next_plan["output_contract"]["grain_columns"] == expected_columns
    assert next_steps[0]["columns"] == expected_columns
    # The selected recipe remains part of the intent; the target Catalog ref is
    # returned separately for the caller to merge into the metadata references.
    assert next_plan["metadata_refs"] == before[0]["metadata_refs"]


def test_current_hold_request_does_not_activate_history_stage() -> None:
    _, before, result = _reconcile("TSSJQ07AH 현재 HOLD LOT 알려줘")

    _assert_fail_soft_noop(before, result)
    assert result[3]["status"] == "not_needed"


def test_trigger_without_direct_required_parameter_evidence_is_advisory_noop() -> None:
    _, before, result = _reconcile(
        "LOT Hold 이력 조회해줘",
        # This value models a stale/model-supplied identifier: it is populated
        # in the plan but is not present in the current question.
        entity_value="TSSJQ07AH",
    )

    _assert_fail_soft_noop(before, result)
    trace = result[3]
    assert trace["status"] == "advisory"
    assert trace["advisories"][0]["reason"] == (
        "dependent_stage_direct_required_params_unresolved"
    )
    assert trace["advisories"][0]["missing_required_params"] == ["ENTITY_ID"]


@pytest.mark.parametrize(
    ("candidates", "reason"),
    [
        (
            _candidates(include_history_catalog=False),
            "dependent_stage_catalog_inactive",
        ),
        (
            _candidates(history_time_scope="current_day"),
            "dependent_stage_time_scope_mismatch",
        ),
    ],
)
def test_missing_or_wrong_scope_target_catalog_is_advisory_noop(
    candidates: dict,
    reason: str,
) -> None:
    _, before, result = _reconcile(
        "TSSJQ07AH LOT Hold 이력 조회해줘",
        candidates=candidates,
    )

    _assert_fail_soft_noop(before, result)
    assert result[3]["status"] == "advisory"
    assert result[3]["advisories"][0]["reason"] == reason


@pytest.mark.parametrize(
    ("question", "extra_filters"),
    [
        (
            "TSSJQ07AH LOT Hold 이력 조회해줘",
            {"OWNER": {"operator": "eq", "value": "TEAM_A"}},
        ),
        (
            "TSSJQ07AH OnHold 상태의 LOT Hold 이력 조회해줘",
            {},
        ),
    ],
)
def test_unsupported_unowned_or_user_explicit_filter_preserves_original_plan(
    question: str,
    extra_filters: dict,
) -> None:
    _, before, result = _reconcile(question, extra_filters=extra_filters)

    _assert_fail_soft_noop(before, result)
    assert result[3]["status"] == "advisory"
    assert result[3]["advisories"][0]["reason"] == (
        "unsupported_user_or_unowned_filter_preserved"
    )


def test_general_history_activation_does_not_keep_implicit_single_row_limit() -> None:
    module, _, result = _reconcile(
        "TSSJQ07AH LOT Hold 이력 조회해줘",
        include_implicit_limit=True,
    )
    next_plan, _, next_steps, stage_trace = result
    assert stage_trace["status"] == "applied"

    reconciled_steps, output_contract, row_trace = (
        module._reconcile_unrequested_detail_row_limit(
            next_steps,
            next_plan["output_contract"],
            "TSSJQ07AH LOT Hold 이력 조회해줘",
        )
    )

    assert row_trace["status"] == "applied"
    assert row_trace["reason"] == "implicit_detail_row_limit_removed"
    assert all("limit" not in step for step in reconciled_steps)
    assert output_contract["ordering"] == {
        "sort_by": "EVENT_TM",
        "order": "desc",
    }
    assert "fast_path_recipe" not in output_contract


def test_stage_selection_runtime_contains_no_hold_dataset_or_question_hardcode() -> None:
    source = inspect.getsource(_module()._reconcile_selected_dependent_recipe_stage)

    assert "lot_status" not in source
    assert "hold_history" not in source
    assert "TSSJQ07AH" not in source
    assert "HOLD 이력" not in source
