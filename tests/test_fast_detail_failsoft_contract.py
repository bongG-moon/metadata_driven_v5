from __future__ import annotations

from copy import deepcopy

import pytest

from component_test_support import ROOT, load_module


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"


def _modules():
    return (
        load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py"),
        load_module(V2_ROOT / "17_hybrid_analysis_executor.py"),
    )


def _single_source_payload(
    *,
    rows: list[dict],
    steps: list[dict],
    output_contract: dict,
    filters: dict | None = None,
    filter_mappings: dict | None = None,
) -> dict:
    source_alias = "source"
    dataset_key = "generic_source"
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return {
        "request": {"question": "generic contract regression"},
        "intent_plan": {
            "request_scope": "new_analysis",
            "retrieval_jobs": [
                {
                    "dataset_key": dataset_key,
                    "source_alias": source_alias,
                    "source_type": "oracle",
                    "required": True,
                    "filters": deepcopy(filters or {}),
                    "filter_mappings": deepcopy(filter_mappings or {}),
                }
            ],
            "pandas_execution_plan": deepcopy(steps),
            "output_contract": deepcopy(output_contract),
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "provider": "retrieval_job",
                        "source_alias": source_alias,
                        "dataset_key": dataset_key,
                        "required": True,
                    }
                ]
            },
        },
        "runtime_sources": {source_alias: deepcopy(rows)},
        "source_results": [
            {
                "dataset_key": dataset_key,
                "source_alias": source_alias,
                "source_type": "oracle",
                "status": "ok",
                "success": True,
                "row_count": len(rows),
                "columns": columns,
            }
        ],
        "execution_gate": {
            "status": "continue",
            "pandas_execution_allowed": True,
            "critical_failures": [],
        },
        "trace": {"inspection": {}},
    }


def _execute_without_model(payload: dict) -> tuple[dict, dict]:
    resolver, executor = _modules()
    resolved = resolver.resolve_simple_analysis_contract(payload)

    def unexpected_model_call(_: str):
        raise AssertionError("deterministic Fast regression must not call the pandas model")

    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused deterministic prompt",
        model_invoker=unexpected_model_call,
        repair_prompt_template="unused repair prompt",
    )
    return resolved, executed


def _normalize_generic_detail_plan(question: str) -> dict:
    """Run Node 04 with a schema-driven event source and no domain-specific rescue."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    dataset_key = "generic_event_history"
    source_alias = "events"
    response = {
        "intent_plan": {
            "analysis_kind": "generic_event_history_detail",
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "retrieval_jobs": [
                {
                    "dataset_key": dataset_key,
                    "source_alias": source_alias,
                    "source_type": "oracle",
                    "required_params": {},
                    "filters": {},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "sort_events",
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "external_source", "ref": source_alias}],
                    "source_alias": source_alias,
                    "sort_by": "EVENT_TM",
                    "order": "desc",
                    "limit": 1,
                }
            ],
            "output_contract": {
                "result_mode": "detail",
                # This deliberately models a weak LLM response. Node 04 must
                # remove the stale aggregate recipe together with an
                # unrequested row limit, based on request shape rather than a
                # dataset name.
                "fast_path_recipe": "ranked_summary",
                "required_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_DESC"],
                "result_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_DESC"],
                "strict_result_columns": True,
            },
        }
    }
    metadata = {
        "metadata_candidates": {
            "domain_items": [],
            "main_flow_filters": [],
            "table_catalog_items": [
                {
                    "dataset_key": dataset_key,
                    "payload": {
                        "dataset_key": dataset_key,
                        "source_type": "oracle",
                        "required_params": [],
                        "columns": [
                            "ENTITY_ID",
                            "EVENT_TM",
                            "EVENT_DESC",
                            "IN_TAT",
                        ],
                        "filter_mappings": {
                            "ENTITY_ID": ["ENTITY_ID"],
                            "EVENT_TM": ["EVENT_TM"],
                            "EVENT_DESC": ["EVENT_DESC"],
                            "IN_TAT": ["IN_TAT"],
                        },
                    },
                }
            ],
        },
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }
    return normalizer.normalize_intent_plan(
        {
            "request": {
                "question": question,
                "reference_date": "20260823",
            }
        },
        response,
        metadata,
    )


@pytest.mark.parametrize("result_mode", ["detail", "entity_list"])
def test_detail_like_sort_and_limit_stays_fast_detail_query(result_mode: str):
    """Sorting or limiting source rows must not turn a detail request into an aggregate ranking."""

    payload = _single_source_payload(
        rows=[
            {
                "LOT_ID": "LOT-01",
                "HOLD_TM": "2026-08-23 08:00:00",
                "HOLD_DESC": "first history",
            },
            {
                "LOT_ID": "LOT-01",
                "HOLD_TM": "2026-08-23 11:00:00",
                "HOLD_DESC": "latest history",
            },
        ],
        steps=[
            {
                "node_id": "sort_history",
                "operation": "sort_and_top_n",
                "inputs": [{"kind": "external_source", "ref": "source"}],
                "source_alias": "source",
                "sort_by": "HOLD_TM",
                "order": "desc",
                "limit": 1,
            }
        ],
        output_contract={
            "result_mode": result_mode,
            "required_columns": ["LOT_ID", "HOLD_TM", "HOLD_DESC"],
            "result_columns": ["LOT_ID", "HOLD_TM", "HOLD_DESC"],
            "strict_result_columns": True,
        },
    )

    resolved, executed = _execute_without_model(payload)

    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "fast"
    assert contract["recipe"] == "detail_query"
    assert contract["ordering"] == [{"column": "HOLD_TM", "direction": "desc"}]
    assert contract["limit"] == 1
    assert resolved["execution_gate"]["status"] == "continue"
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "fast"
    assert executed["data"]["rows"] == [
        {
            "LOT_ID": "LOT-01",
            "HOLD_TM": "2026-08-23 11:00:00",
            "HOLD_DESC": "latest history",
        }
    ]


def test_explicit_ranked_summary_hint_cannot_override_detail_execution_shape():
    """An explicit stale recipe label cannot manufacture aggregation in a detail plan."""

    payload = _single_source_payload(
        rows=[
            {"ENTITY_ID": "E-01", "EVENT_TM": "2026-08-23 08:00:00"},
            {"ENTITY_ID": "E-01", "EVENT_TM": "2026-08-23 11:00:00"},
        ],
        steps=[
            {
                "node_id": "sort_events",
                "operation": "sort_and_top_n",
                "inputs": [{"kind": "external_source", "ref": "source"}],
                "source_alias": "source",
                "sort_by": "EVENT_TM",
                "order": "desc",
                "limit": 1,
            }
        ],
        output_contract={
            "result_mode": "detail",
            "fast_path_recipe": "ranked_summary",
            "required_columns": ["ENTITY_ID", "EVENT_TM"],
            "result_columns": ["ENTITY_ID", "EVENT_TM"],
            "strict_result_columns": True,
        },
    )

    resolved, executed = _execute_without_model(payload)

    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["recipe"] == "detail_query"
    assert resolved["execution_gate"]["status"] == "continue"
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"ENTITY_ID": "E-01", "EVENT_TM": "2026-08-23 11:00:00"}
    ]


def test_node04_removes_implicit_single_row_limit_from_general_detail_history():
    """A general history request preserves ordering but returns the full detail population."""

    normalized = _normalize_generic_detail_plan("이력 데이터를 보여줘")

    plan = normalized["intent_plan"]
    sort_step = plan["pandas_execution_plan"][0]
    output_contract = plan["output_contract"]
    reconciliation = normalized["trace"]["inspection"]["intent"][
        "detail_row_selection_reconciliation"
    ]
    assert sort_step["sort_by"] == "EVENT_TM"
    assert sort_step["order"] == "desc"
    assert "limit" not in sort_step
    assert "fast_path_recipe" not in output_contract
    assert output_contract["ordering"]["sort_by"] == "EVENT_TM"
    assert "limit" not in output_contract["ordering"]
    assert reconciliation["status"] == "applied"
    assert reconciliation["reason"] == "implicit_detail_row_limit_removed"


def test_node04_preserves_explicit_latest_single_row_selection():
    """Explicit latest/one-row wording keeps the model's bounded selection contract."""

    normalized = _normalize_generic_detail_plan("최신 이력 한 건 보여줘")

    plan = normalized["intent_plan"]
    sort_step = plan["pandas_execution_plan"][0]
    output_contract = plan["output_contract"]
    reconciliation = normalized["trace"]["inspection"]["intent"][
        "detail_row_selection_reconciliation"
    ]
    assert sort_step["limit"] == 1
    assert output_contract["ordering"]["limit"] == 1
    assert reconciliation["status"] == "not_needed"
    assert reconciliation["reason"] == "explicit_question_row_selection"


def test_node04_does_not_treat_threshold_duration_as_requested_row_count():
    """The number in a threshold predicate must not preserve an unrelated detail-row limit."""

    normalized = _normalize_generic_detail_plan("IN_TAT 1일 이상 LOT LIST 알려줘")

    plan = normalized["intent_plan"]
    sort_step = plan["pandas_execution_plan"][0]
    output_contract = plan["output_contract"]
    reconciliation = normalized["trace"]["inspection"]["intent"][
        "detail_row_selection_reconciliation"
    ]
    assert "limit" not in sort_step
    assert "fast_path_recipe" not in output_contract
    assert "limit" not in output_contract["ordering"]
    assert reconciliation["status"] == "applied"
    assert reconciliation["reason"] == "implicit_detail_row_limit_removed"


def test_aggregate_sort_and_limit_remains_ranked_summary():
    """A real group aggregation followed by Top-N must retain ranked-summary semantics."""

    payload = _single_source_payload(
        rows=[
            {"OPER_NAME": "A", "QTY": 10},
            {"OPER_NAME": "B", "QTY": 20},
            {"OPER_NAME": "B", "QTY": 15},
        ],
        steps=[
            {
                "node_id": "aggregate_process",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "external_source", "ref": "source"}],
                "source_alias": "source",
                "group_by": ["OPER_NAME"],
                "aggregations": [
                    {
                        "column": "QTY",
                        "method": "sum",
                        "output_column": "QTY_SUM",
                    }
                ],
            },
            {
                "node_id": "rank_process",
                "operation": "sort_and_top_n",
                "inputs": [{"kind": "node_output", "ref": "aggregate_process"}],
                "sort_by": "QTY_SUM",
                "order": "desc",
                "limit": 1,
            },
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["OPER_NAME"],
            "metric_columns": ["QTY_SUM"],
            "required_columns": ["OPER_NAME", "QTY_SUM"],
            "result_columns": ["OPER_NAME", "QTY_SUM"],
            "strict_result_columns": True,
        },
    )

    resolved, executed = _execute_without_model(payload)

    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "fast"
    assert contract["recipe"] == "ranked_summary"
    assert contract["limit"] == 1
    assert resolved["execution_gate"]["status"] == "continue"
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"OPER_NAME": "B", "QTY_SUM": 35}]


def test_fast_result_or_ordering_mismatch_falls_back_without_blocking_retrieved_data():
    """A stale post-aggregate display/order contract is a route fallback, not a retrieval failure."""

    payload = _single_source_payload(
        rows=[
            {"OPER_NAME": "A", "QTY": 10},
            {"OPER_NAME": "B", "QTY": 20},
        ],
        steps=[
            {
                "node_id": "aggregate_process",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "external_source", "ref": "source"}],
                "source_alias": "source",
                "group_by": ["OPER_NAME"],
                "aggregations": [
                    {
                        "column": "QTY",
                        "method": "sum",
                        "output_column": "QTY_SUM",
                    }
                ],
            },
            {
                "node_id": "rank_process",
                "operation": "sort_and_top_n",
                "inputs": [{"kind": "node_output", "ref": "aggregate_process"}],
                # QTY exists in the retrieved source but is not produced by
                # the aggregate. This is a stale Fast output/order contract,
                # not evidence that retrieval itself failed.
                "sort_by": "QTY",
                "order": "desc",
                "limit": 1,
            },
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["OPER_NAME"],
            "metric_columns": ["QTY_SUM"],
            "required_columns": ["OPER_NAME", "QTY"],
            "result_columns": ["OPER_NAME", "QTY"],
            "strict_result_columns": True,
        },
    )

    resolver, _ = _modules()
    resolved = resolver.resolve_simple_analysis_contract(payload)

    contract = resolved["simple_analysis_contract"]
    error_types = {
        str(item.get("type") or "")
        for item in contract.get("validation_errors", [])
        if isinstance(item, dict)
    }
    assert contract["route"] == "complex"
    assert "unproduced_result_column" in error_types
    assert "unresolved_ordering_column" in error_types
    assert resolved["intent_plan"]["route_resolution"]["final_route"] == "complex"
    assert resolved["execution_gate"]["status"] == "continue"
    assert resolved["execution_gate"]["pandas_execution_allowed"] is True
    assert resolved["source_results"][0]["status"] == "ok"
    assert resolved["runtime_sources"]["source"] == payload["runtime_sources"]["source"]


def test_missing_mandatory_filter_mapping_remains_blocked():
    """Fail-soft routing must never guess or drop a mandatory source filter."""

    payload = _single_source_payload(
        rows=[{"OPER_NAME": "D/A1", "QTY": 10}],
        steps=[
            {
                "node_id": "apply_required_scope",
                "operation": "apply_filters",
                "inputs": [{"kind": "external_source", "ref": "source"}],
                "source_alias": "source",
            }
        ],
        filters={
            "REQUESTED_PROCESS": {
                "operator": "eq",
                "value": "D/A1",
            }
        },
        # Deliberately omit the mapping from REQUESTED_PROCESS to OPER_NAME.
        filter_mappings={},
        output_contract={
            "result_mode": "detail",
            "required_columns": ["OPER_NAME", "QTY"],
            "result_columns": ["OPER_NAME", "QTY"],
            "strict_result_columns": True,
        },
    )
    payload["intent_plan"]["retrieval_jobs"][0]["mandatory_filter_fields"] = [
        "REQUESTED_PROCESS"
    ]

    resolver, _ = _modules()
    resolved = resolver.resolve_simple_analysis_contract(payload)

    contract = resolved["simple_analysis_contract"]
    issue_types = {
        str(item.get("type") or "")
        for item in contract.get("validation_errors", [])
        if isinstance(item, dict)
    }
    assert contract["route"] == "blocked"
    assert "filter_contract_invalid" in contract["eligibility"]["reason_codes"]
    assert "missing_filter_mapping" in issue_types
    assert resolved["execution_gate"]["status"] == "blocked"
    assert resolved["execution_gate"]["reason"] == "fast_path_contract_invalid"


@pytest.mark.parametrize(
    ("steps", "output_contract", "expected_issue"),
    [
        (
            [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "source",
                    "group_by": ["MISSING_GROUP"],
                    "aggregations": [
                        {
                            "column": "QTY",
                            "method": "sum",
                            "output_column": "QTY_SUM",
                        }
                    ],
                }
            ],
            {
                "result_mode": "aggregate",
                "grain_columns": ["MISSING_GROUP"],
                "metric_columns": ["QTY_SUM"],
                "result_columns": ["MISSING_GROUP", "QTY_SUM"],
            },
            "missing_source_column",
        ),
        (
            [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "source",
                    "group_by": ["OPER_NAME"],
                    "aggregations": [
                        {
                            "column": "MISSING_QTY",
                            "method": "sum",
                            "output_column": "QTY_SUM",
                        }
                    ],
                }
            ],
            {
                "result_mode": "aggregate",
                "grain_columns": ["OPER_NAME"],
                "metric_columns": ["QTY_SUM"],
                "result_columns": ["OPER_NAME", "QTY_SUM"],
            },
            "missing_metric_source_column",
        ),
        (
            [
                {
                    "operation": "select_columns",
                    "source_alias": "source",
                    "columns": ["OPER_NAME", "MISSING_DETAIL"],
                }
            ],
            {
                "result_mode": "detail",
                "required_columns": ["OPER_NAME", "MISSING_DETAIL"],
                "result_columns": ["OPER_NAME", "MISSING_DETAIL"],
                "strict_result_columns": True,
            },
            "missing_source_column",
        ),
    ],
    ids=["group-by-source", "metric-source", "select-projection-source"],
)
def test_missing_execution_input_columns_remain_blocked(
    steps: list[dict],
    output_contract: dict,
    expected_issue: str,
):
    """Fail-soft output handling must not relax columns required to execute the plan."""

    payload = _single_source_payload(
        rows=[{"OPER_NAME": "A", "QTY": 10}],
        steps=steps,
        output_contract=output_contract,
    )

    resolver, _ = _modules()
    resolved = resolver.resolve_simple_analysis_contract(payload)

    contract = resolved["simple_analysis_contract"]
    issue_types = {
        str(item.get("type") or "")
        for item in contract.get("validation_errors", [])
        if isinstance(item, dict)
    }
    assert contract["route"] == "blocked"
    assert "source_schema_contract_invalid" in contract["eligibility"]["reason_codes"]
    assert expected_issue in issue_types
    assert resolved["execution_gate"]["status"] == "blocked"
