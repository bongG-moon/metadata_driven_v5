from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


def _modules():
    return (
        load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py"),
        load_module(V2_ROOT / "17_hybrid_analysis_executor.py"),
    )


def _join_only_payload() -> dict:
    wip_rows = [
        {"OPER_NAME": "W/B1", "SHIFT": "A", "WIP": 10},
        {"OPER_NAME": "W/B1", "SHIFT": "B", "WIP": 20},
        {"OPER_NAME": "W/B2", "SHIFT": "A", "WIP": 5},
    ]
    production_rows = [
        {"OPER_NAME": "W/B1", "SHIFT": "C", "PRODUCTION": 3},
        {"OPER_NAME": "W/B1", "SHIFT": "D", "PRODUCTION": 4},
        {"OPER_NAME": "W/B3", "SHIFT": "C", "PRODUCTION": 7},
    ]
    jobs = [
        {
            "dataset_key": "wip_today",
            "source_alias": "wip_today",
            "filters": {},
            "filter_mappings": {"OPER_NAME": ["OPER_NAME"], "WIP": ["WIP"]},
            "metric_semantics": {
                "WIP": {
                    "additive": True,
                    "default_rollup": "sum",
                    "allowed_rollups": ["sum"],
                }
            },
        },
        {
            "dataset_key": "production_today",
            "source_alias": "production_today",
            "filters": {},
            "filter_mappings": {
                "OPER_NAME": ["OPER_NAME"],
                "PRODUCTION": ["PRODUCTION"],
            },
            "metric_semantics": {
                "PRODUCTION": {
                    "additive": True,
                    "default_rollup": "sum",
                    "allowed_rollups": ["sum"],
                }
            },
        },
    ]
    step = {
        "node_id": "join_wb_stage",
        "operation": "join",
        "inputs": [
            {"kind": "external_source", "ref": "wip_today"},
            {"kind": "external_source", "ref": "production_today"},
        ],
        "output_alias": "joined_wb_stage",
        "join_type": "outer",
        "population_policy": "preserve_all_metric_source_keys",
        "left_metric_column": "WIP",
        "right_metric_column": "PRODUCTION",
        "group_by": ["OPER_NAME"],
        "blank_policy": "normalize_blank",
    }
    output_contract = {
        "result_mode": "aggregate",
        "grain_columns": ["OPER_NAME"],
        "metric_columns": ["WIP", "PRODUCTION"],
        "required_columns": ["OPER_NAME", "WIP", "PRODUCTION"],
        "result_columns": ["OPER_NAME", "WIP", "PRODUCTION"],
        "strict_result_columns": True,
        "metric_null_policy": "display_zero",
    }
    return {
        "request": {"question": "generic multi-source metric request"},
        "intent_plan": {
            "request_scope": "new_analysis",
            "retrieval_jobs": jobs,
            "pandas_execution_plan": [step],
            "output_contract": output_contract,
            "intent_ir": {
                "route_source_aliases": ["wip_today", "production_today"],
                "operations": ["join"],
            },
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "provider": "retrieval_job",
                        "source_alias": "wip_today",
                        "dataset_key": "wip_today",
                    },
                    {
                        "provider": "retrieval_job",
                        "source_alias": "production_today",
                        "dataset_key": "production_today",
                    },
                ]
            },
        },
        "runtime_sources": {
            "wip_today": deepcopy(wip_rows),
            "production_today": deepcopy(production_rows),
        },
        "source_results": [
            {
                "source_alias": "wip_today",
                "dataset_key": "wip_today",
                "status": "ok",
                "columns": ["OPER_NAME", "SHIFT", "WIP"],
            },
            {
                "source_alias": "production_today",
                "dataset_key": "production_today",
                "status": "ok",
                "columns": ["OPER_NAME", "SHIFT", "PRODUCTION"],
            },
        ],
        "trace": {"inspection": {}},
    }


def test_join_only_outer_metrics_are_aggregated_per_source_before_merge():
    """The exact weak join shape must not multiply raw rows or compare SHIFT."""

    resolver, executor = _modules()
    resolved = resolver.resolve_simple_analysis_contract(_join_only_payload())

    merge = resolved["intent_plan"]["resolved_metric_merge_plan"]
    assert merge["strict"] is True
    assert merge["operation"] == "merge_metric_sources"
    assert merge["selection_source"] == "runtime_join_only_metric_rescue"
    assert merge["grain_mappings"] == [
        {
            "canonical_column": "OPER_NAME",
            "output_column": "OPER_NAME",
            "source_candidates": {
                "wip_today": ["OPER_NAME"],
                "production_today": ["OPER_NAME"],
            },
        }
    ]
    assert resolved["simple_analysis_contract"]["deterministic_operation"] == (
        "merge_metric_sources"
    )
    rescue_trace = resolved["trace"]["inspection"]["join_only_metric_merge_rescue"]
    assert rescue_trace["policy"] == "source_local_aggregate_before_outer_metric_merge"
    assert rescue_trace["aggregation_sources"] == [
        "trusted_retrieval_metric_semantics",
        "trusted_retrieval_metric_semantics",
    ]

    model_calls: list[str] = []

    def invoke(prompt: str):
        model_calls.append(prompt)
        raise AssertionError("deterministic metric merge must not call pandas LLM")

    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused deterministic prompt",
        model_invoker=invoke,
        repair_prompt_template="repair",
    )

    assert model_calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_mode"] == "merge_metric_sources"
    assert executed["data"]["columns"] == ["OPER_NAME", "WIP", "PRODUCTION"]
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "W/B1", "WIP": 30, "PRODUCTION": 7},
        {"OPER_NAME": "W/B2", "WIP": 5, "PRODUCTION": 0},
        {"OPER_NAME": "W/B3", "WIP": 0, "PRODUCTION": 7},
    ]


def test_join_only_runtime_rescue_rejects_row_enrichment_shape():
    """A value-carrying row relationship stays on the ordinary Typed path."""

    resolver, _ = _modules()
    payload = _join_only_payload()
    step = payload["intent_plan"]["pandas_execution_plan"][0]
    step["join_type"] = "left"
    step["population_policy"] = "left_source_only"
    step["right_value_columns"] = ["SHIFT"]

    resolved = resolver.resolve_simple_analysis_contract(payload)

    assert "resolved_metric_merge_plan" not in resolved["intent_plan"]
    assert "join_only_metric_merge_rescue" not in resolved["trace"]["inspection"]
    assert resolved["simple_analysis_contract"]["operation"] == (
        "execute_typed_pandas_plan"
    )


def test_join_only_runtime_rescue_requires_unique_metric_ownership():
    """A metric present on both sources is not an independent-source witness."""

    resolver, _ = _modules()
    payload = _join_only_payload()
    for row in payload["runtime_sources"]["production_today"]:
        row["WIP"] = 999
    payload["source_results"][1]["columns"].append("WIP")

    resolved = resolver.resolve_simple_analysis_contract(payload)

    assert "resolved_metric_merge_plan" not in resolved["intent_plan"]
    assert "join_only_metric_merge_rescue" not in resolved["trace"]["inspection"]
    assert resolved["simple_analysis_contract"]["operation"] == (
        "execute_typed_pandas_plan"
    )


def test_join_only_runtime_rescue_requires_aggregation_evidence():
    """A pure metric shape without a trusted rollup stays on the Typed path."""

    resolver, _ = _modules()
    payload = _join_only_payload()
    for job in payload["intent_plan"]["retrieval_jobs"]:
        job.pop("metric_semantics", None)

    resolved = resolver.resolve_simple_analysis_contract(payload)

    assert "resolved_metric_merge_plan" not in resolved["intent_plan"]
    assert "join_only_metric_merge_rescue" not in resolved["trace"]["inspection"]
    assert resolved["simple_analysis_contract"]["operation"] == (
        "execute_typed_pandas_plan"
    )


def test_join_only_runtime_rescue_accepts_explicit_metric_bindings():
    """Explicit output ownership and rollups are sufficient without Catalog semantics."""

    resolver, _ = _modules()
    payload = _join_only_payload()
    for job in payload["intent_plan"]["retrieval_jobs"]:
        job.pop("metric_semantics", None)
    payload["intent_plan"]["output_contract"]["metric_bindings"] = [
        {
            "source_alias": "wip_today",
            "dataset_key": "wip_today",
            "source_column": "WIP",
            "aggregation": "sum",
            "output_column": "WIP",
        },
        {
            "source_alias": "production_today",
            "dataset_key": "production_today",
            "source_column": "PRODUCTION",
            "aggregation": "sum",
            "output_column": "PRODUCTION",
        },
    ]

    resolved = resolver.resolve_simple_analysis_contract(payload)

    assert resolved["intent_plan"]["resolved_metric_merge_plan"]["strict"] is True
    assert resolved["trace"]["inspection"]["join_only_metric_merge_rescue"][
        "aggregation_sources"
    ] == [
        "output_contract_metric_binding",
        "output_contract_metric_binding",
    ]


def _uph_mean_payload() -> dict:
    payload = _join_only_payload()
    left_job = payload["intent_plan"]["retrieval_jobs"][0]
    left_job["filter_mappings"] = {"OPER_NAME": ["OPER_NAME"], "UPH": ["UPH"]}
    left_job["metric_semantics"] = {
        "UPH": {
            "additive": False,
            "default_rollup": "mean",
            "allowed_rollups": ["mean"],
        }
    }
    payload["runtime_sources"]["wip_today"] = [
        {
            "OPER_NAME": row["OPER_NAME"],
            "SHIFT": row["SHIFT"],
            "UPH": row["WIP"],
        }
        for row in payload["runtime_sources"]["wip_today"]
    ]
    payload["source_results"][0]["columns"] = ["OPER_NAME", "SHIFT", "UPH"]
    step = payload["intent_plan"]["pandas_execution_plan"][0]
    step["left_metric_column"] = "UPH"
    output = payload["intent_plan"]["output_contract"]
    output["metric_columns"] = ["UPH", "PRODUCTION"]
    output["required_columns"] = ["OPER_NAME", "UPH", "PRODUCTION"]
    output["result_columns"] = ["OPER_NAME", "UPH", "PRODUCTION"]
    return payload


def test_join_only_runtime_rescue_uses_trusted_non_additive_mean():
    """Catalog mean semantics are preserved instead of defaulting UPH to sum."""

    resolver, executor = _modules()
    resolved = resolver.resolve_simple_analysis_contract(_uph_mean_payload())

    merge = resolved["intent_plan"]["resolved_metric_merge_plan"]
    assert [item["aggregation"] for item in merge["metrics"]] == ["mean", "sum"]
    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused deterministic prompt",
        model_invoker=lambda _: (_ for _ in ()).throw(
            AssertionError("proven UPH merge must not call pandas LLM")
        ),
        repair_prompt_template="repair",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "W/B1", "UPH": 15.0, "PRODUCTION": 7},
        {"OPER_NAME": "W/B2", "UPH": 5.0, "PRODUCTION": 0},
        {"OPER_NAME": "W/B3", "UPH": 0.0, "PRODUCTION": 7},
    ]


def test_join_only_runtime_rescue_rejects_rollup_conflict():
    """An explicit sum that conflicts with trusted UPH mean is not rewritten."""

    resolver, _ = _modules()
    payload = _uph_mean_payload()
    payload["intent_plan"]["pandas_execution_plan"][0]["left_aggregation"] = "sum"

    resolved = resolver.resolve_simple_analysis_contract(payload)

    assert "resolved_metric_merge_plan" not in resolved["intent_plan"]
    assert "join_only_metric_merge_rescue" not in resolved["trace"]["inspection"]
    assert resolved["simple_analysis_contract"]["operation"] == (
        "execute_typed_pandas_plan"
    )


def test_unrescued_join_keeps_existing_shared_dimension_conflict_guard():
    """Evidence failure must not weaken the raw Typed join SHIFT guard."""

    resolver, executor = _modules()
    payload = _join_only_payload()
    payload["intent_plan"]["output_contract"]["result_mode"] = "detail"

    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert "resolved_metric_merge_plan" not in resolved["intent_plan"]
    assert resolved["simple_analysis_contract"]["operation"] == (
        "execute_typed_pandas_plan"
    )

    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused deterministic prompt",
        model_invoker=lambda _: (_ for _ in ()).throw(
            AssertionError("typed deterministic execution must not call pandas LLM")
        ),
        repair_prompt_template="repair",
    )

    assert executed["analysis"]["status"] == "error"
    assert executed["analysis"]["error"]["type"] == "output_contract_violation"
    assert "SHIFT" in executed["analysis"]["error"]["message"]
