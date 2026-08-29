"""Regression coverage for catalog-driven follow-up source reuse.

These tests deliberately exercise the public Flow 01 normalizer contract rather
than an implementation helper.  A short follow-up can inherit the previous
analysis shape, but previous raw source rows are reusable only when every
source has unchanged catalog-required parameters and the new filter is proven
to be inside the previously retrieved range.  Any uncertainty must select a
fresh retrieval, never a validation block.
"""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


@pytest.fixture(scope="module")
def normalizer():
    return load_module(V2_ROOT / "04_intent_plan_normalizer.py")


@pytest.fixture(scope="module")
def followup_hint_builder():
    return load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "01e_followup_hint_builder.py"
    )


@pytest.fixture(scope="module")
def hydrator():
    return load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "04a_trusted_retrieval_job_hydrator.py"
    )


@pytest.fixture(scope="module")
def retrieval_router():
    return load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "07_retrieval_job_router.py"
    )


def _candidates() -> dict:
    """Small, fully catalog-declared source set for reuse-policy decisions."""

    return {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "DA",
                "payload": {
                    "aliases": ["DA", "DA공정", "D/A", "D/A공정"],
                    "field": "OPER_NAME",
                    "processes": ["D/A1", "D/A2"],
                },
            },
            {
                "section": "process_groups",
                "key": "WB",
                "payload": {
                    "aliases": ["WB", "WB공정", "W/B", "W/B공정"],
                    "field": "OPER_NAME",
                    "processes": ["W/B1", "W/B2"],
                },
            },
        ],
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "production_today",
                "dataset_key": "production_today",
                "source_type": "oracle",
                "payload": {
                    "columns": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"],
                    "required_params": ["DATE"],
                    "filter_mappings": {
                        "DATE": ["DATE"],
                        "OPER_NAME": ["OPER_NAME"],
                        "LEAD": ["LEAD"],
                    },
                },
            },
            {
                "section": "table_catalog",
                "key": "lot_history",
                "dataset_key": "lot_history",
                "source_type": "oracle",
                "payload": {
                    "columns": ["LOT_ID", "OPER_NAME", "HOLD_TM"],
                    "required_params": ["LOT_ID"],
                    "filter_mappings": {"LOT_ID": ["LOT_ID"], "OPER_NAME": ["OPER_NAME"]},
                },
            },
            {
                "section": "table_catalog",
                "key": "equipment_detail",
                "dataset_key": "equipment_detail",
                "source_type": "oracle",
                "payload": {
                    "columns": ["EQP_ID", "OPER_NAME", "UPH"],
                    "required_params": ["EQP_ID"],
                    "filter_mappings": {"EQP_ID": ["EQP_ID"], "OPER_NAME": ["OPER_NAME"]},
                },
            },
            {
                "section": "table_catalog",
                "key": "wip_today",
                "dataset_key": "wip_today",
                "source_type": "oracle",
                "payload": {
                    "columns": ["DATE", "OPER_NAME", "LEAD", "WIP"],
                    "required_params": ["DATE"],
                    "filter_mappings": {
                        "DATE": ["DATE"],
                        "OPER_NAME": ["OPER_NAME"],
                        "LEAD": ["LEAD"],
                    },
                },
            },
        ],
        "main_flow_filters": [],
    }


def _detail_step(source_alias: str, columns: list[str]) -> list[dict]:
    return [
        {
            "node_id": f"select_{source_alias}",
            "operation": "select_columns",
            "inputs": [{"kind": "external_source", "ref": source_alias}],
            "source_alias": source_alias,
            "output_alias": f"{source_alias}_result",
            "columns": columns,
        }
    ]


def _production_plan(
    *,
    processes: list[str],
    date: str = "20260828",
    lead_condition: dict | None = None,
) -> dict:
    filters: dict = {"OPER_NAME": {"operator": "in", "value": processes}}
    if lead_condition is not None:
        filters["LEAD"] = deepcopy(lead_condition)
    return {
        "analysis_kind": "process_production_quantity",
        "request_scope": "new_analysis",
        "reference_mode": "none",
        "metadata_refs": [
            {"section": "process_groups", "key": "DA"},
            {"section": "table_catalog", "key": "production_today"},
        ],
        "retrieval_jobs": [
            {
                "dataset_key": "production_today",
                "source_alias": "prod_src",
                "source_type": "oracle",
                "required_params": {"DATE": date},
                "filters": filters,
            }
        ],
        "pandas_execution_plan": [
            {
                "node_id": "aggregate_production",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "external_source", "ref": "prod_src"}],
                "source_alias": "prod_src",
                "output_alias": "production_result",
                "group_by": ["OPER_NAME"],
                "aggregations": [
                    {
                        "column": "PRODUCTION",
                        "method": "sum",
                        "output_column": "PRODUCTION",
                    }
                ],
            }
        ],
        "output_contract": {
            "result_mode": "aggregate",
            "grain_columns": ["OPER_NAME"],
            "metric_columns": ["PRODUCTION"],
            "result_columns": ["OPER_NAME", "PRODUCTION"],
            "required_columns": ["OPER_NAME", "PRODUCTION"],
            "strict_result_columns": True,
        },
    }


def _simple_plan(
    *,
    dataset_key: str,
    source_alias: str,
    required_params: dict,
    columns: list[str],
    metric: str,
) -> dict:
    return {
        "analysis_kind": f"{dataset_key}_lookup",
        "request_scope": "new_analysis",
        "reference_mode": "none",
        "metadata_refs": [{"section": "table_catalog", "key": dataset_key}],
        "retrieval_jobs": [
            {
                "dataset_key": dataset_key,
                "source_alias": source_alias,
                "source_type": "oracle",
                "required_params": deepcopy(required_params),
                "filters": {},
            }
        ],
        "pandas_execution_plan": _detail_step(source_alias, columns),
        "output_contract": {
            "result_mode": "detail",
            "result_columns": columns,
            "required_columns": columns,
            "metric_columns": [metric],
            "strict_result_columns": True,
        },
    }


def _followup_payload(
    *,
    question: str,
    previous_plan: dict,
    reusable_aliases: list[str],
    source_columns: dict[str, list[str]],
    reference_date: str = "20260828",
    date_hint: dict | None = None,
    source_ref_complete: bool = True,
    entity_switch_detected: bool = False,
) -> dict:
    current_data = {
        "columns": list(previous_plan["output_contract"]["result_columns"]),
        "result_columns": list(previous_plan["output_contract"]["result_columns"]),
        "source_aliases": list(reusable_aliases),
        "source_dataset_keys": [
            job["dataset_key"] for job in previous_plan["retrieval_jobs"]
        ],
        "source_columns_by_alias": deepcopy(source_columns),
    }
    return {
        "request": {"question": question, "reference_date": reference_date},
        "followup_hint": {
            "followup_candidate": True,
            "source_reuse_candidate": True,
            "condition_only_followup_candidate": True,
            "request_scope_hint": "followup_requery",
            "reuse_strategy_hint": "previous_intent_with_new_retrieval",
            "reusable_previous_source_aliases": list(reusable_aliases),
            "matched_cues": {
                "change": ["조건 변경"],
                "entity_switch_detected": (
                    ["condition_only"] if entity_switch_detected else []
                ),
            },
            "changed_conditions_hint": (
                {"date": deepcopy(date_hint)} if date_hint else {}
            ),
        },
        "state": {
            "last_intent_plan": deepcopy(previous_plan),
            "current_data": current_data,
            "runtime_source_refs": {
                alias: {
                    "ref_id": f"result:{alias}",
                    "role": "source_rows",
                    # A raw source can only be reused when the stored manifest
                    # explicitly proves it was not clipped.  The fallback
                    # behavior is covered by the empty alias case below.
                    "complete": source_ref_complete,
                    "retrieval_mode": "dummy",
                }
                for alias in reusable_aliases
            },
            "last_applied_criteria": {
                "required_params": {
                    job["source_alias"]: deepcopy(job.get("required_params") or {})
                    for job in previous_plan["retrieval_jobs"]
                },
                "analysis_filters": {
                    job["source_alias"]: deepcopy(job.get("filters") or {})
                    for job in previous_plan["retrieval_jobs"]
                },
            },
        },
        "trace": {"inspection": {}, "warnings": [], "errors": []},
    }


def _followup_response(previous_plan: dict, *, jobs: list[dict], weak_shape: bool = False) -> dict:
    if weak_shape:
        # A weak intent model can replace a concise follow-up with an unrelated
        # LOT query.  The policy must retain the previous analytical shape and
        # only decide whether its source rows can be reused.
        return {
            "intent_plan": {
                "analysis_kind": "lot_status_detail_lookup",
                "request_scope": "followup_requery",
                "reference_mode": "previous_source",
                "metadata_refs": [{"section": "table_catalog", "key": "lot_status"}],
                "retrieval_jobs": deepcopy(jobs),
                "pandas_execution_plan": _detail_step("prod_src", ["OPER_NAME", "LEAD"]),
                "output_contract": {
                    "result_mode": "detail",
                    "result_columns": ["OPER_NAME", "LEAD"],
                    "required_columns": ["OPER_NAME", "LEAD"],
                    "strict_result_columns": True,
                },
            }
        }
    plan = deepcopy(previous_plan)
    plan["request_scope"] = "followup_requery"
    plan["reference_mode"] = "previous_source"
    plan["retrieval_jobs"] = deepcopy(jobs)
    return {"intent_plan": plan}


def _normalize_then_hydrate(
    normalizer,
    hydrator,
    payload: dict,
    response: dict,
) -> tuple[dict, dict]:
    """Run the actual Flow boundary where trusted reuse is decided.

    Node 04 intentionally keeps a candidate/blueprint while Node 04A has the
    fully hydrated Table Catalog contract needed to compare required parameters
    and executable source coverage.  The regression boundary is therefore
    normalize -> hydrate, not either node in isolation.
    """

    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response, ensure_ascii=False),
        _candidates(),
    )
    hydrated = hydrator.hydrate_retrieval_jobs(
        normalized,
        _candidates(),
        retrieval_mode="dummy",
    )
    return normalized, hydrated


def _assert_no_followup_block(hydrated: dict) -> None:
    plan = hydrated["intent_plan"]
    assert not plan.get("validation_errors"), plan.get("validation_errors")
    inspection = hydrated["trace"]["inspection"]
    assert inspection["catalog_hydration"]["status"] != "error"
    # The policy must remain diagnosable without exposing internal helper APIs.
    decision = (
        inspection.get("catalog_hydration", {}).get("followup_source_reuse_decision")
        or inspection.get("intent", {}).get("followup_source_reuse_decision")
    )
    assert decision


def _assert_fresh_followup(hydrated: dict, *, expected_aliases: list[str]) -> None:
    plan = hydrated["intent_plan"]
    assert plan["request_scope"] == "followup_requery"
    assert plan["reference_mode"] == "previous_filters"
    assert plan["reuse_strategy"] == "previous_intent_with_new_retrieval"
    assert [job["source_alias"] for job in plan["retrieval_jobs"]] == expected_aliases
    _assert_no_followup_block(hydrated)


def _assert_previous_source_reuse(hydrated: dict, retrieval_router, *, expected_aliases: list[str]) -> None:
    """Retained jobs must be non-dispatchable previous-source placeholders.

    04A deliberately keeps a hydrated job so Node 05 can fall back to a fresh
    retrieval if Mongo restoration fails.  Node 07 is the observable boundary
    proving that none of those jobs are actually dispatched as a new query.
    """

    plan = hydrated["intent_plan"]
    assert plan["reference_mode"] == "previous_source"
    assert plan["reuse_strategy"] == "previous_source"
    assert [job["source_alias"] for job in plan["retrieval_jobs"]] == expected_aliases
    assert all(
        job.get("execution_provider") == "previous_source"
        for job in plan["retrieval_jobs"]
    )
    routed = retrieval_router.route_retrieval_jobs(hydrated, "dummy")
    assert routed["retrieval_job_bundle"]["jobs"] == []
    assert routed["routing_trace"]["skipped_previous_source_aliases"] == expected_aliases
    _assert_no_followup_block(hydrated)


def test_bare_process_followup_emits_explicit_condition_only_signal(followup_hint_builder):
    """01E must carry the signal that 04 uses to retain the prior blueprint."""

    previous = _production_plan(processes=["D/A1", "D/A2"])
    payload = _followup_payload(
        question="WB공정은?",
        previous_plan=previous,
        reusable_aliases=["prod_src"],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
    )
    payload.pop("followup_hint")
    hinted = followup_hint_builder.build_followup_hint(payload)["followup_hint"]

    assert hinted["followup_candidate"] is True
    assert hinted["condition_only_followup_candidate"] is True
    assert hinted["source_reuse_candidate"] is True
    assert hinted["matched_cues"]["entity_switch_detected"] == ["condition_only"]


def test_followup_process_scope_expansion_requeries_previous_analysis_shape(normalizer, hydrator):
    """D/A -> W/B keeps production analysis but must not reuse D/A source rows."""

    previous = _production_plan(processes=["D/A1", "D/A2"])
    current_job = _production_plan(processes=["W/B1", "W/B2"])["retrieval_jobs"][0]
    payload = _followup_payload(
        question="WB공정은?",
        previous_plan=previous,
        reusable_aliases=["prod_src"],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
        entity_switch_detected=True,
    )
    _normalized, hydrated = _normalize_then_hydrate(
        normalizer,
        hydrator,
        payload,
        _followup_response(previous, jobs=[current_job], weak_shape=True),
    )

    plan = hydrated["intent_plan"]
    _assert_fresh_followup(hydrated, expected_aliases=["prod_src"])
    assert plan["analysis_kind"] == previous["analysis_kind"]
    assert [job["dataset_key"] for job in plan["retrieval_jobs"]] == ["production_today"]
    assert plan["output_contract"]["metric_columns"] == ["PRODUCTION"]
    assert plan["retrieval_jobs"][0]["filters"]["OPER_NAME"] == {
        "operator": "in",
        "value": ["W/B1", "W/B2"],
    }


def test_explicit_process_followup_does_not_reintroduce_dropped_prior_scope(
    normalizer,
    hydrator,
):
    """D/A -> W/B must not turn an audited dropped scope into an active union.

    This mirrors the production trace where the LLM correctly placed the old
    D/A scope under ``condition_resolution.dropped`` and the new W/B scope
    under ``new``.  The normalizer must still build a W/B-only fresh query.
    """

    previous = _production_plan(processes=["D/A1", "D/A2"])
    current = _production_plan(processes=["W/B1", "W/B2"])
    current["request_scope"] = "followup_requery"
    current["reference_mode"] = "previous_filters"
    current["condition_resolution"] = {
        "inherited": {"DATE": "20260828"},
        "dropped": {"OPER_NAME": ["D/A1", "D/A2"]},
        "new": {"OPER_NAME": ["W/B1", "W/B2"]},
        "effective_filters": {
            "prod_src": {
                "dataset_key": "production_today",
                "filters": {
                    "OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]}
                },
            }
        },
    }
    payload = _followup_payload(
        question="WB공정은?",
        previous_plan=previous,
        reusable_aliases=["prod_src"],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
        entity_switch_detected=True,
    )

    normalized, hydrated = _normalize_then_hydrate(
        normalizer,
        hydrator,
        payload,
        {"intent_plan": current},
    )

    expected = {"operator": "in", "value": ["W/B1", "W/B2"]}
    assert normalizer._declared_process_scope_from_plan(
        normalized["intent_plan"],
        _candidates(),
    ) == ["W/B1", "W/B2"]
    assert normalized["intent_plan"]["retrieval_jobs"][0]["filters"]["OPER_NAME"] == expected
    _assert_fresh_followup(hydrated, expected_aliases=["prod_src"])
    assert hydrated["intent_plan"]["retrieval_jobs"][0]["filters"]["OPER_NAME"] == expected


def test_followup_narrowing_filter_reuses_previous_source_when_coverage_is_proven(normalizer, hydrator, retrieval_router):
    """A new LEAD equality inside the prior D/A source coverage is reusable."""

    previous = _production_plan(processes=["D/A1", "D/A2"])
    current_job = _production_plan(
        processes=["D/A1", "D/A2"],
        lead_condition={"operator": "eq", "value": 266},
    )["retrieval_jobs"][0]
    payload = _followup_payload(
        question="266 LEAD는?",
        previous_plan=previous,
        reusable_aliases=["prod_src"],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
    )
    _normalized, hydrated = _normalize_then_hydrate(
        normalizer,
        hydrator,
        payload,
        _followup_response(previous, jobs=[current_job]),
    )

    _assert_previous_source_reuse(
        hydrated,
        retrieval_router,
        expected_aliases=["prod_src"],
    )


@pytest.mark.parametrize(
    ("question", "previous_plan", "current_params", "source_columns", "date_hint"),
    [
        (
            "어제는?",
            _production_plan(processes=["D/A1", "D/A2"], date="20260828"),
            {"DATE": "20260827"},
            {"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
            {"expression": "어제", "resolved_value": "20260827"},
        ),
        (
            "다른 LOT는?",
            _simple_plan(
                dataset_key="lot_history",
                source_alias="lot_src",
                required_params={"LOT_ID": "LOT-A"},
                columns=["LOT_ID", "OPER_NAME", "HOLD_TM"],
                metric="HOLD_TM",
            ),
            {"LOT_ID": "LOT-B"},
            {"lot_src": ["LOT_ID", "OPER_NAME", "HOLD_TM"]},
            None,
        ),
        (
            "다른 장비는?",
            _simple_plan(
                dataset_key="equipment_detail",
                source_alias="eqp_src",
                required_params={"EQP_ID": "EQP-A"},
                columns=["EQP_ID", "OPER_NAME", "UPH"],
                metric="UPH",
            ),
            {"EQP_ID": "EQP-B"},
            {"eqp_src": ["EQP_ID", "OPER_NAME", "UPH"]},
            None,
        ),
    ],
)
def test_followup_required_parameter_change_always_requeries(
    normalizer,
    hydrator,
    question,
    previous_plan,
    current_params,
    source_columns,
    date_hint,
):
    """Catalog-required identifiers never flow through a previous raw source."""

    current_job = deepcopy(previous_plan["retrieval_jobs"][0])
    current_job["required_params"] = deepcopy(current_params)
    alias = current_job["source_alias"]
    payload = _followup_payload(
        question=question,
        previous_plan=previous_plan,
        reusable_aliases=[alias],
        source_columns=source_columns,
        date_hint=date_hint,
    )
    _normalized, hydrated = _normalize_then_hydrate(
        normalizer,
        hydrator,
        payload,
        _followup_response(previous_plan, jobs=[current_job]),
    )

    _assert_fresh_followup(hydrated, expected_aliases=[alias])
    assert hydrated["intent_plan"]["retrieval_jobs"][0]["required_params"] == current_params


def test_followup_ambiguous_filter_operator_or_missing_source_requeries(normalizer, hydrator):
    """Unprovable inclusion and unavailable/incomplete source choose fresh retrieval."""

    previous = _production_plan(processes=["D/A1", "D/A2"])
    ambiguous_job = _production_plan(
        processes=["D/A1", "D/A2"],
        lead_condition={"operator": "contains", "value": "266"},
    )["retrieval_jobs"][0]
    ambiguous_payload = _followup_payload(
        question="266 LEAD는?",
        previous_plan=previous,
        reusable_aliases=["prod_src"],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
    )
    _normalized, ambiguous = _normalize_then_hydrate(
        normalizer,
        hydrator,
        ambiguous_payload,
        _followup_response(previous, jobs=[ambiguous_job]),
    )
    _assert_fresh_followup(ambiguous, expected_aliases=["prod_src"])

    exact_job = _production_plan(
        processes=["D/A1", "D/A2"],
        lead_condition={"operator": "eq", "value": 266},
    )["retrieval_jobs"][0]
    missing_source_payload = _followup_payload(
        question="266 LEAD는?",
        previous_plan=previous,
        reusable_aliases=[],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
    )
    _normalized, missing_source = _normalize_then_hydrate(
        normalizer,
        hydrator,
        missing_source_payload,
        _followup_response(previous, jobs=[exact_job]),
    )
    _assert_fresh_followup(missing_source, expected_aliases=["prod_src"])

    incomplete_source_payload = _followup_payload(
        question="266 LEAD는?",
        previous_plan=previous,
        reusable_aliases=["prod_src"],
        source_columns={"prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"]},
        source_ref_complete=False,
    )
    _normalized, incomplete_source = _normalize_then_hydrate(
        normalizer,
        hydrator,
        incomplete_source_payload,
        _followup_response(previous, jobs=[exact_job]),
    )
    _assert_fresh_followup(incomplete_source, expected_aliases=["prod_src"])


def test_followup_multi_source_requeries_all_when_any_source_is_not_reusable(normalizer, hydrator):
    """A joined analysis never mixes an old source with a fresh incompatible source."""

    production = _production_plan(processes=["D/A1", "D/A2"])
    wip = _simple_plan(
        dataset_key="wip_today",
        source_alias="wip_src",
        required_params={"DATE": "20260828"},
        columns=["DATE", "OPER_NAME", "LEAD", "WIP"],
        metric="WIP",
    )
    previous = deepcopy(production)
    previous["analysis_kind"] = "process_production_and_wip_summary"
    previous["retrieval_jobs"].append(deepcopy(wip["retrieval_jobs"][0]))
    previous["pandas_execution_plan"].append(
        {
            "node_id": "aggregate_wip",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "wip_src"}],
            "source_alias": "wip_src",
            "output_alias": "wip_result",
            "group_by": ["OPER_NAME"],
            "aggregations": [{"column": "WIP", "method": "sum", "output_column": "WIP"}],
        }
    )
    previous["output_contract"] = {
        "result_mode": "aggregate",
        "grain_columns": ["OPER_NAME"],
        "metric_columns": ["PRODUCTION", "WIP"],
        "result_columns": ["OPER_NAME", "PRODUCTION", "WIP"],
        "required_columns": ["OPER_NAME", "PRODUCTION", "WIP"],
        "strict_result_columns": True,
    }

    current_prod = _production_plan(
        processes=["D/A1", "D/A2"],
        lead_condition={"operator": "eq", "value": 266},
    )["retrieval_jobs"][0]
    current_wip = deepcopy(wip["retrieval_jobs"][0])
    current_wip["filters"] = {"OPER_NAME": {"operator": "in", "value": ["W/B1", "W/B2"]}}
    payload = _followup_payload(
        question="WB공정 266 LEAD는?",
        previous_plan=previous,
        reusable_aliases=["prod_src", "wip_src"],
        source_columns={
            "prod_src": ["DATE", "OPER_NAME", "LEAD", "PRODUCTION"],
            "wip_src": ["DATE", "OPER_NAME", "LEAD", "WIP"],
        },
    )
    _normalized, hydrated = _normalize_then_hydrate(
        normalizer,
        hydrator,
        payload,
        _followup_response(previous, jobs=[current_prod, current_wip]),
    )

    _assert_fresh_followup(hydrated, expected_aliases=["prod_src", "wip_src"])
