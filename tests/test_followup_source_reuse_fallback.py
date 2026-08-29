"""Regression tests for safe fallback from automatic previous-source reuse.

These tests cover the non-happy path that is deliberately outside the normal
catalog comparison: a source can be approved for reuse and then disappear
from MongoDB before node 05 restores it.  That must become an ordinary fresh
retrieval, never a new execution gate.
"""

from __future__ import annotations

from copy import deepcopy
import json

from component_test_support import ROOT, load_module


LOADER_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py"
STORE_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py"
ANSWER_BUILDER_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "20_hybrid_answer_builder.py"
NORMALIZER_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "04_intent_plan_normalizer.py"
HYDRATOR_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "04a_trusted_retrieval_job_hydrator.py"
HINT_BUILDER_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"


def _approved_reuse_payload() -> dict:
    return {
        "request": {"question": "266 LEAD는?", "session_id": "reuse-session"},
        "state": {"current_data": {"source_aliases": ["prod_src"]}},
        "intent_plan": {
            "request_scope": "followup_transform",
            "reference_mode": "previous_source",
            "reuse_strategy": "previous_source",
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "prod_src",
                    "execution_provider": "previous_source",
                    "required_params": {"DATE": "20260828"},
                    "filters": {"LEAD": {"operator": "eq", "value": 266}},
                }
            ],
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": "prod_src",
                        "provider": "previous_source",
                        "required": True,
                    }
                ]
            },
            "followup_source_reuse": {
                "managed_by": "04a_trusted_retrieval_job_hydrator",
                "decision": "reuse_previous_source",
                "fresh_fallback": True,
                "source_aliases": ["prod_src"],
            },
        },
        "trace": {"inspection": {}, "warnings": [], "errors": []},
    }


def test_missing_approved_source_ref_falls_back_to_fresh_retrieval_without_error():
    loader = load_module(LOADER_PATH)

    result = loader.load_previous_result(_approved_reuse_payload())

    plan = result["intent_plan"]
    assert plan["request_scope"] == "followup_requery"
    assert plan["reference_mode"] == "previous_filters"
    assert plan["reuse_strategy"] == "previous_intent_with_new_retrieval"
    assert "execution_provider" not in plan["retrieval_jobs"][0]
    assert plan["resolved_execution_graph"]["external_source_requirements"][0]["provider"] == "retrieval_job"
    assert result["trace"]["inspection"]["result_loader"]["status"] == "fallback"
    assert result["trace"]["warnings"][-1]["type"] == "previous_source_reuse_fallback"
    assert not result["trace"]["errors"]


def test_complete_source_reference_round_trips_into_next_turn_state():
    store = load_module(STORE_PATH)
    answer_builder = load_module(ANSWER_BUILDER_PATH)
    payload = {
        "request": {"retrieval_mode": "dummy"},
        "source_results": [
            {
                "source_alias": "prod_src",
                "dataset_key": "production_today",
                "source_type": "oracle",
                "source_execution": {"used_dummy_data": True},
            }
        ],
        "runtime_sources": {"prod_src": [{"DATE": "20260828", "PRODUCTION": 10}]},
    }
    manifest = {"runtime_sources": {"prod_src": {"complete": True}}}

    refs = store._build_data_refs(
        payload,
        "result:reuse-session:123",
        "datagov",
        "agent_v4_result_store",
        manifest,
    )
    source_ref = next(ref for ref in refs if ref.get("role") == "source_rows")
    assert source_ref["complete"] is True
    assert source_ref["retrieval_mode"] == "dummy"
    assert source_ref["used_dummy_data"] is True

    state_refs = answer_builder._runtime_source_refs({"data_refs": refs})
    assert state_refs["prod_src"]["complete"] is True


def test_source_reuse_with_different_retrieval_mode_falls_back_to_fresh_query():
    hydrator = load_module(HYDRATOR_PATH)
    job = {
        "dataset_key": "production_today",
        "source_alias": "prod_src",
        "required_params": {"DATE": "20260828"},
        "required_param_names": ["DATE"],
        "filters": {"OPER_NAME": {"operator": "eq", "value": "D/A1"}},
    }
    payload = {
        "request": {"retrieval_mode": "dummy"},
        "followup_hint": {"source_reuse_candidate": True},
        "state": {
            "last_intent_plan": {"retrieval_jobs": [job]},
            "runtime_source_refs": {
                "prod_src": {
                    "ref_id": "result:prod",
                    "role": "source_rows",
                    "complete": True,
                    "retrieval_mode": "live",
                }
            },
            "last_applied_criteria": {
                "required_params": {"prod_src": {"DATE": "20260828"}},
                "analysis_filters": {"prod_src": job["filters"]},
            },
        },
    }

    plan, jobs, decision = hydrator._resolve_followup_source_reuse(
        payload,
        {"retrieval_jobs": [deepcopy(job)]},
        [deepcopy(job)],
    )

    assert decision["decision"] == "fresh_retrieval"
    assert any(item.get("reason") == "previous_source_retrieval_mode_changed" for item in decision["sources"])
    assert plan["request_scope"] == "followup_requery"
    assert "execution_provider" not in jobs[0]


def test_partial_prior_join_never_reuses_only_one_source():
    hydrator = load_module(HYDRATOR_PATH)
    production = {
        "dataset_key": "production_today",
        "source_alias": "prod_src",
        "required_params": {"DATE": "20260828"},
        "required_param_names": ["DATE"],
        "filters": {"OPER_NAME": {"operator": "eq", "value": "D/A1"}},
    }
    wip = {
        "dataset_key": "wip_today",
        "source_alias": "wip_src",
        "required_params": {"DATE": "20260828"},
        "required_param_names": ["DATE"],
        "filters": {"OPER_NAME": {"operator": "eq", "value": "D/A1"}},
    }
    payload = {
        "followup_hint": {"source_reuse_candidate": True},
        "state": {
            "last_intent_plan": {"retrieval_jobs": [production, wip]},
            "runtime_source_refs": {
                "prod_src": {"ref_id": "result:prod", "role": "source_rows", "complete": True},
                "wip_src": {"ref_id": "result:wip", "role": "source_rows", "complete": True},
            },
            "last_applied_criteria": {
                "required_params": {"prod_src": {"DATE": "20260828"}, "wip_src": {"DATE": "20260828"}},
                "analysis_filters": {
                    "prod_src": production["filters"],
                    "wip_src": wip["filters"],
                },
            },
        },
    }
    current_plan = {"retrieval_jobs": [deepcopy(production)]}

    plan, jobs, decision = hydrator._resolve_followup_source_reuse(
        payload,
        current_plan,
        [deepcopy(production)],
    )

    assert decision["decision"] == "fresh_retrieval"
    assert any(item.get("reason") == "current_and_previous_source_sets_do_not_match" for item in decision["sources"])
    assert plan["reference_mode"] == "previous_filters"
    assert "execution_provider" not in jobs[0]


def test_condition_only_followup_can_replace_an_invented_model_dataset_with_previous_blueprint():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = {
        "domain_items": [],
        "main_flow_filters": [],
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "production_today",
                "dataset_key": "production_today",
                "source_type": "oracle",
                "payload": {
                    "columns": ["DATE", "OPER_NAME", "PRODUCTION"],
                    "required_params": ["DATE"],
                    "filter_mappings": {"DATE": ["DATE"], "OPER_NAME": ["OPER_NAME"]},
                },
            }
        ],
    }
    previous_plan = {
        "analysis_kind": "process_production_quantity",
        "retrieval_jobs": [
            {
                "dataset_key": "production_today",
                "source_alias": "prod_src",
                "required_params": {"DATE": "20260828"},
                "filters": {"OPER_NAME": {"operator": "eq", "value": "D/A1"}},
            }
        ],
        "pandas_execution_plan": [],
        "output_contract": {"result_mode": "aggregate", "metric_columns": ["PRODUCTION"]},
    }
    payload = {
        "request": {"question": "WB공정은?", "reference_date": "20260828"},
        "followup_hint": {
            "followup_candidate": True,
            "condition_only_followup_candidate": True,
            "source_reuse_candidate": True,
            "matched_cues": {"entity_switch_detected": ["condition_only"]},
        },
        "state": {"last_intent_plan": previous_plan},
        "trace": {"inspection": {}, "warnings": [], "errors": []},
    }
    weak_response = {
        "intent_plan": {
            "analysis_kind": "weak_model_plan",
            "retrieval_jobs": [
                {"dataset_key": "invented_unregistered_table", "source_alias": "weak_src"}
            ],
            "pandas_execution_plan": [],
            "output_contract": {"result_mode": "detail"},
        }
    }

    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(weak_response),
        candidates,
    )

    assert normalized["intent_plan"]["analysis_kind"] == "process_production_quantity"
    assert [job["dataset_key"] for job in normalized["intent_plan"]["retrieval_jobs"]] == ["production_today"]
    guard = normalized["trace"]["inspection"]["intent"]["condition_only_followup_blueprint"]
    assert guard["status"] == "applied"


def test_condition_only_relative_date_updates_a_catalog_mapped_physical_date_param():
    normalizer = load_module(NORMALIZER_PATH)
    candidates = {
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "physical_date_source",
                "dataset_key": "physical_date_source",
                "payload": {
                    "required_params": ["WORK_DT"],
                    "required_param_mappings": {"DATE": ["WORK_DT"]},
                },
            }
        ]
    }
    payload = {
        "request": {"question": "어제는?", "reference_date": "20260828"},
        "followup_hint": {
            "followup_candidate": True,
            "condition_only_followup_candidate": True,
            "changed_conditions_hint": {
                "date": {"expression": "어제", "resolved_value": "20260827"}
            },
        },
    }
    jobs = [
        {
            "dataset_key": "physical_date_source",
            "source_alias": "physical_src",
            "required_params": {"WORK_DT": "20260828"},
        }
    ]

    updated, guard = normalizer._apply_context_date_guard(payload, jobs, candidates)

    assert updated[0]["required_params"]["WORK_DT"] == "20260827"
    assert guard["corrected_source_aliases"] == ["physical_src"]


def test_short_attribute_scope_followups_are_candidates_without_hardcoded_domain_names():
    hint_builder = load_module(HINT_BUILDER_PATH)
    state = {
        "last_question": "오늘 DA공정 생산량 알려줘",
        "last_intent_plan": {"analysis_kind": "process_production_quantity"},
        "current_data": {"columns": ["OPER_NAME", "PRODUCTION"]},
    }

    for question in ("OPER에서는?", "자재는?"):
        hinted = hint_builder.build_followup_hint(
            {"request": {"question": question}, "state": state}
        )
        assert hinted["followup_hint"]["condition_only_followup_candidate"] is True
        assert hinted["followup_hint"]["source_reuse_candidate"] is True

    # A complete, independent metric request must not be mistaken for a terse
    # scope replacement merely because it also ends in a Korean topic marker.
    independent = hint_builder.build_followup_hint(
        {"request": {"question": "UPH는 조회해줘"}, "state": state}
    )
    assert independent["followup_hint"]["condition_only_followup_candidate"] is False
