from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


NORMALIZER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "04_intent_plan_normalizer.py"
)
RESOLVER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "14b_simple_analysis_contract_resolver.py"
)
EXECUTOR_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "17_hybrid_analysis_executor.py"
)
HELPER_LIBRARY_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "function_case_helper_code_input_example.py"
)


def _product_helper_metadata() -> dict:
    return {
        "domain_items": [
            {
                "section": "pandas_function_cases",
                "key": "product_token_match",
                "payload": {
                    "function_name": "match_product_tokens",
                    "description": "제품 속성 token을 등록된 역할별 규칙으로 매칭한다.",
                },
            }
        ],
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "out_target",
                "dataset_key": "out_target",
                "source_type": "oracle",
                "columns": ["MCP_NO", "OUT_PLAN"],
            },
            {
                "section": "table_catalog",
                "key": "production",
                "dataset_key": "production",
                "source_type": "oracle",
                "columns": ["MCP_NO", "PRODUCTION"],
            },
        ],
        "main_flow_filters": [],
    }


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


def _two_source_cases() -> list[dict]:
    return [
        {
            "key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "L-267",
            "source_alias": "target",
        },
        {
            "key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "L-267",
            "source_alias": "production_raw",
        },
    ]


def _two_source_jobs() -> list[dict]:
    return [
        {"dataset_key": "out_target", "source_alias": "target"},
        {"dataset_key": "production", "source_alias": "production_raw"},
    ]


def _two_source_plan() -> list[dict]:
    return [
        {
            "node_id": "match_target",
            "operation": "apply_pandas_function_case",
            "function_case_key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "L-267",
            "source_alias": "target",
            "inputs": [{"kind": "external_source", "ref": "target"}],
            "output_alias": "target_matched",
        },
        {
            "node_id": "match_production",
            "operation": "apply_pandas_function_case",
            "function_case_key": "product_token_match",
            "function_name": "match_product_tokens",
            "input_text": "L-267",
            "source_alias": "production_raw",
            "inputs": [
                {"kind": "external_source", "ref": "production_raw"}
            ],
            "output_alias": "production_matched",
        },
        {
            "node_id": "aggregate_target",
            "operation": "groupby_and_aggregate",
            "source_alias": "target",
            "inputs": [{"kind": "external_source", "ref": "target"}],
            "output_alias": "target_by_product",
            "group_by": ["MCP_NO"],
            "aggregations": [
                {
                    "column": "OUT_PLAN",
                    "method": "sum",
                    "output_column": "OUT_PLAN",
                }
            ],
        },
        {
            "node_id": "aggregate_production",
            "operation": "groupby_and_aggregate",
            "source_alias": "production_raw",
            # A retrieval alias is not a node provider.  This legacy spelling
            # is safe to correct only because the source-local helper is unique.
            "inputs": [{"kind": "node_output", "ref": "production_raw"}],
            "output_alias": "production_by_product",
            "group_by": ["MCP_NO"],
            "aggregations": [
                {
                    "column": "PRODUCTION",
                    "method": "sum",
                    "output_column": "PRODUCTION",
                }
            ],
        },
        {
            "node_id": "merge_plan_actual",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "target_by_product"},
                {"kind": "node_output", "ref": "production_by_product"},
            ],
            "output_alias": "plan_actual",
            "left_source_alias": "target_by_product",
            "right_source_alias": "production_by_product",
            "join_type": "outer",
            "population_policy": "preserve_all_metric_source_keys",
            "on": ["MCP_NO"],
            "right_value_columns": ["PRODUCTION"],
        },
    ]


def test_same_helper_is_wired_independently_for_two_source_local_consumers():
    normalizer = load_module(NORMALIZER_PATH)

    repaired, trace = normalizer._reconcile_unconsumed_function_case_terminal_lineage(
        _two_source_cases(),
        _two_source_plan(),
        _two_source_jobs(),
    )

    assert repaired[2]["inputs"] == [
        {"kind": "node_output", "ref": "match_target"}
    ]
    assert repaired[3]["inputs"] == [
        {"kind": "node_output", "ref": "match_production"}
    ]
    assert trace["status"] == "applied"
    assert {item["source_alias"] for item in trace["repairs"]} == {
        "target",
        "production_raw",
    }
    assert {
        item["original_input"]["kind"] for item in trace["repairs"]
    } == {"external_source", "node_output"}
    graph = normalizer._compile_execution_graph(
        repaired,
        _two_source_jobs(),
        {},
        "none",
    )
    assert graph["validation_errors"] == []


def test_two_source_local_helper_lineage_is_eligible_for_typed_deterministic_contract():
    normalizer = load_module(NORMALIZER_PATH)
    resolver = load_module(RESOLVER_PATH)
    repaired, trace = normalizer._reconcile_unconsumed_function_case_terminal_lineage(
        _two_source_cases(),
        _two_source_plan(),
        _two_source_jobs(),
    )
    graph = normalizer._compile_execution_graph(
        repaired,
        _two_source_jobs(),
        {},
        "none",
    )
    payload = {
        "question": "어제 L-267제품 OUT계획과 PKG OUT 실적 알려줘",
        "intent_plan": {
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "retrieval_jobs": _two_source_jobs(),
            "pandas_function_cases": _two_source_cases(),
            "pandas_execution_plan": repaired,
            "resolved_execution_graph": graph,
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["MCP_NO"],
                "metric_columns": ["OUT_PLAN", "PRODUCTION"],
                "required_columns": ["MCP_NO", "OUT_PLAN", "PRODUCTION"],
                "result_columns": ["MCP_NO", "OUT_PLAN", "PRODUCTION"],
                "strict_result_columns": True,
            },
        },
        "runtime_sources": {
            "target": [
                {"MCP_NO": "L-267A", "OUT_PLAN": 120.0},
                {"MCP_NO": "OTHER", "OUT_PLAN": 90.0},
            ],
            "production_raw": [
                {"MCP_NO": "L-267A", "PRODUCTION": 84.0},
                {"MCP_NO": "OTHER", "PRODUCTION": 41.0},
            ],
        },
        "source_results": [
            {
                "source_alias": "target",
                "dataset_key": "out_target",
                "status": "ok",
                "row_count": 2,
                "columns": ["MCP_NO", "OUT_PLAN"],
            },
            {
                "source_alias": "production_raw",
                "dataset_key": "production",
                "status": "ok",
                "row_count": 2,
                "columns": ["MCP_NO", "PRODUCTION"],
            },
        ],
        "trace": {"inspection": {}},
    }

    resolved = resolver.resolve_simple_analysis_contract(payload)
    contract = resolved["simple_analysis_contract"]

    assert trace["status"] == "applied"
    assert contract["route"] == "complex"
    assert contract["operation"] == "execute_typed_pandas_plan"
    assert contract["requires_pandas_llm"] is False
    assert {item["source_alias"] for item in contract["source_transforms"]} == {
        "target",
        "production_raw",
    }


def test_lossy_exact_product_prefix_filter_recovers_each_source_local_helper():
    normalizer = load_module(NORMALIZER_PATH)
    jobs = [
        {
            "dataset_key": "out_target",
            "source_alias": "target",
            "filters": {"MCP_NO": {"operator": "eq", "value": "L-267"}},
        },
        {
            "dataset_key": "production",
            "source_alias": "production_raw",
            "filters": {"MCP_NO": {"operator": "in", "value": ["L-267"]}},
        },
    ]

    selected_plan, selection = normalizer._auto_select_metadata_function_case(
        {},
        jobs,
        _product_helper_metadata(),
        "어제 L-267제품 OUT계획과 PKG OUT 실적 알려줘",
    )
    cases = normalizer._function_case_items(
        selected_plan,
        jobs,
        _product_helper_metadata(),
    )
    trusted_elision_cases = deepcopy(cases)
    for case in trusted_elision_cases:
        case["execution_contract"] = {
            "elision_policy": "when_equivalent_source_filter"
        }
    retained_cases, _, sufficiency = (
        normalizer._remove_source_filter_sufficient_function_cases(
            trusted_elision_cases,
            [],
            jobs,
            _product_helper_metadata(),
        )
    )
    normalized_jobs, removal = normalizer._remove_function_owned_retrieval_filters(
        jobs,
        cases,
        _product_helper_metadata(),
    )

    assert selection["reason"] == "lossy_exact_product_prefix_filter"
    assert {case["source_alias"] for case in cases} == {
        "target",
        "production_raw",
    }
    assert len(retained_cases) == 2
    assert sufficiency["removed"] == []
    assert {case["input_text"] for case in cases} == {"L-267"}
    assert [job["filters"] for job in normalized_jobs] == [{}, {}]
    assert removal["status"] == "applied"
    assert {item["source_alias"] for item in removal["removed"]} == {
        "target",
        "production_raw",
    }

    aliased_jobs = [
        {
            "dataset_key": "production",
            "source_alias": "production_raw",
            "filter_mappings": {"MCP_NO": ["MCP SALES NO"]},
            "filters": {
                "MCP SALES NO": {"operator": "eq", "value": "A-663"}
            },
        }
    ]
    aliased_plan, _ = normalizer._auto_select_metadata_function_case(
        {},
        aliased_jobs,
        _product_helper_metadata(),
        "A-663 제품",
    )
    aliased_cases = normalizer._function_case_items(
        aliased_plan,
        aliased_jobs,
        _product_helper_metadata(),
    )
    aliased_normalized, _ = normalizer._remove_function_owned_retrieval_filters(
        aliased_jobs,
        aliased_cases,
        _product_helper_metadata(),
    )
    assert len(aliased_cases) == 1
    assert aliased_normalized[0]["filters"] == {}


def test_prefix_capable_full_code_and_plain_numeric_filters_keep_existing_paths():
    normalizer = load_module(NORMALIZER_PATH)
    metadata = _product_helper_metadata()
    cases = [
        (
            "L-267 제품",
            {"MCP_NO": {"operator": "starts_with", "value": "L-267"}},
        ),
        (
            "L-267 제품",
            {"MCP_NO": {"operator": "contains", "value": "L-267"}},
        ),
        (
            "L-267A1 제품",
            {"MCP_NO": {"operator": "eq", "value": "L-267A1"}},
        ),
        (
            "267 LEAD 제품",
            {"LEAD": {"operator": "eq", "value": 267}},
        ),
        (
            "L-267, A-663 제품",
            {
                "MCP_NO": {
                    "operator": "in",
                    "value": ["L-267", "A-663"],
                }
            },
        ),
        (
            "L-267 LOT",
            {"LOT_ID": {"operator": "eq", "value": "L-267"}},
        ),
        (
            "L-267 RECIPE",
            {"RECIPE_ID": {"operator": "eq", "value": "L-267"}},
        ),
        (
            "L-267 장비",
            {"EQP_ID": {"operator": "eq", "value": "L-267"}},
        ),
    ]

    for question, filters in cases:
        jobs = [
            {
                "dataset_key": "production",
                "source_alias": "production_raw",
                "filters": deepcopy(filters),
            }
        ]
        selected_plan, _ = normalizer._auto_select_metadata_function_case(
            {}, jobs, metadata, question
        )
        assert "pandas_function_cases" not in selected_plan
        normalized_jobs, removal = (
            normalizer._remove_function_owned_retrieval_filters(
                jobs,
                [],
                metadata,
            )
        )
        assert normalized_jobs == jobs
        assert removal["removed"] == []
        manually_selected_jobs, manual_removal = (
            normalizer._remove_function_owned_retrieval_filters(
                jobs,
                [
                    {
                        "key": "product_token_match",
                        "function_name": "match_product_tokens",
                        "input_text": question,
                        "source_alias": "production_raw",
                    }
                ],
                metadata,
            )
        )
        assert manually_selected_jobs == jobs
        assert manual_removal["removed"] == []


def test_two_source_lossy_prefix_plan_normalizes_and_executes_deterministically():
    normalizer = load_module(NORMALIZER_PATH)
    resolver = load_module(RESOLVER_PATH)
    executor = load_module(EXECUTOR_PATH)
    raw_steps = [deepcopy(step) for step in _two_source_plan()[2:]]
    response = {
        "metadata_refs": [
            {"section": "pandas_function_cases", "key": "product_token_match"}
        ],
        "intent_plan": {
            "analysis_kind": "out_plan_and_production_by_product",
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "condition_resolution": {
                "effective_filters": {
                    "target": {
                        "dataset_key": "out_target",
                        "filters": {
                            "MCP_NO": {"operator": "eq", "value": "L-267"}
                        },
                    },
                    "production_raw": {
                        "dataset_key": "production",
                        "filters": {
                            "MCP_NO": {"operator": "eq", "value": "L-267"}
                        },
                    },
                }
            },
            "retrieval_jobs": [
                {
                    "dataset_key": "out_target",
                    "source_alias": "target",
                    "source_type": "oracle",
                    "filters": {
                        "MCP_NO": {"operator": "eq", "value": "L-267"}
                    },
                },
                {
                    "dataset_key": "production",
                    "source_alias": "production_raw",
                    "source_type": "oracle",
                    "filters": {
                        "MCP_NO": {"operator": "eq", "value": "L-267"}
                    },
                },
            ],
            "pandas_execution_plan": raw_steps,
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["MCP_NO"],
                "metric_columns": ["OUT_PLAN", "PRODUCTION"],
                "required_columns": ["MCP_NO", "OUT_PLAN", "PRODUCTION"],
                "result_columns": ["MCP_NO", "OUT_PLAN", "PRODUCTION"],
                "strict_result_columns": True,
            },
        },
    }
    payload = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "어제 L-267제품 OUT계획과 PKG OUT 실적 알려줘"
            },
            "trace": {},
        },
        response,
        {"metadata_candidates": _product_helper_metadata()},
    )
    plan = payload["intent_plan"]
    helper_steps = [
        step
        for step in plan["pandas_execution_plan"]
        if step.get("operation") == "apply_pandas_function_case"
    ]
    aggregate_steps = [
        step
        for step in plan["pandas_execution_plan"]
        if step.get("operation") == "groupby_and_aggregate"
    ]

    assert not plan.get("validation_errors")
    assert [job.get("filters") for job in plan["retrieval_jobs"]] == [{}, {}]
    assert {case["source_alias"] for case in plan["pandas_function_cases"]} == {
        "target",
        "production_raw",
    }
    assert len(helper_steps) == 2
    assert {
        step["inputs"][0]["ref"] for step in aggregate_steps
    } == {step["node_id"] for step in helper_steps}
    assert plan["resolved_execution_graph"]["validation_errors"] == []

    payload["question"] = payload["request"]["question"]
    payload["runtime_sources"] = {
        "target": [
            {"MCP_NO": "L-267A1", "OUT_PLAN": 120.0},
            {"MCP_NO": "OTHER", "OUT_PLAN": 90.0},
        ],
        "production_raw": [
            {"MCP_NO": "L-267A1", "PRODUCTION": 84.0},
            {"MCP_NO": "OTHER", "PRODUCTION": 41.0},
        ],
    }
    payload["source_results"] = [
        {
            "source_alias": "target",
            "dataset_key": "out_target",
            "status": "ok",
            "row_count": 2,
            "columns": ["MCP_NO", "OUT_PLAN"],
        },
        {
            "source_alias": "production_raw",
            "dataset_key": "production",
            "status": "ok",
            "row_count": 2,
            "columns": ["MCP_NO", "PRODUCTION"],
        },
    ]
    assert executor._pandas_filter_plan(payload) == []
    resolved = resolver.resolve_simple_analysis_contract(payload)
    contract = resolved["simple_analysis_contract"]
    model_calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=lambda prompt: model_calls.append(prompt),
        repair_prompt_template="repair",
        function_case_helper_code=HELPER_LIBRARY_PATH.read_text(encoding="utf-8"),
    )

    assert contract["route"] == "complex"
    assert contract["analysis_execution_mode"] == "deterministic_contract"
    assert contract["deterministic_operation"] == "merge_metric_sources"
    assert contract["requires_pandas_llm"] is False
    assert model_calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"MCP_NO": "L-267A1", "OUT_PLAN": 120.0, "PRODUCTION": 84.0}
    ]
    transforms = executed["trace"]["inspection"]["pandas_execution"][
        "deterministic_source_transforms"
    ]
    assert {item["source_alias"] for item in transforms} == {
        "target",
        "production_raw",
    }


def test_effective_filter_cleanup_removes_only_the_proven_condition_tuple():
    normalizer = load_module(NORMALIZER_PATH)
    condition_resolution = {
        "inherited": {"DATE": "20260820"},
        "new": {"OPER_NAME": "SHIP PKT"},
        "effective_filters": {
            "target": {
                "dataset_key": "out_target",
                "filters": {
                    "MCP_NO": {"operator": "eq", "value": "L-267"},
                    "DATE": {"operator": "eq", "value": "20260820"},
                    "OPER_NAME": {"operator": "eq", "value": "SHIP PKT"},
                },
            },
            "production_raw": {
                "dataset_key": "production",
                "filters": {
                    # Same field/value but a prefix-capable operator is not the
                    # explicitly removed retrieval condition.
                    "MCP_NO": {
                        "operator": "starts_with",
                        "value": "L-267",
                    }
                },
            },
        },
    }
    filter_normalization = {
        "removed": [
            {
                "source_alias": "target",
                "filter_field": "MCP_NO",
                "operator": "eq",
                "value": "L-267",
            }
        ]
    }

    normalized, removed = normalizer._strip_removed_function_owned_effective_filters(
        condition_resolution,
        filter_normalization,
    )

    assert normalized["effective_filters"]["target"]["filters"] == {
        "DATE": {"operator": "eq", "value": "20260820"},
        "OPER_NAME": {"operator": "eq", "value": "SHIP PKT"},
    }
    assert normalized["effective_filters"]["production_raw"]["filters"] == {
        "MCP_NO": {"operator": "starts_with", "value": "L-267"}
    }
    assert normalized["inherited"] == {"DATE": "20260820"}
    assert normalized["new"] == {"OPER_NAME": "SHIP PKT"}
    assert removed == [
        {
            "source_alias": "target",
            "filter_field": "MCP_NO",
            "operator": "eq",
            "value": "L-267",
            "reason": "explicit_function_owned_retrieval_filter_removal",
        }
    ]
