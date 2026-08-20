from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


ADAPTER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "14_retrieval_payload_adapter.py"
)
HYDRATOR_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "04a_trusted_retrieval_job_hydrator.py"
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


def _hold_history_payload(
    *,
    question: str = "TSSJQ07AH LOT Hold 이력 조회해줘",
    catalog_columns: list[str] | None = None,
    steps: list[dict] | None = None,
    trusted_catalog: bool = True,
) -> dict:
    runtime_columns = ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"]
    rows = [
        {
            "LOT_ID": "TSSJQ07AH",
            "OPER_NAME": "W/B1",
            "HOLD_TM": "2026-08-20 08:00:00",
            "HOLD_CD": "H001",
            "HOLD_DESC": "검증용 HOLD",
        }
    ]
    job = {
        "dataset_key": "hold_history",
        "source_alias": "hold_history",
        "trusted_catalog": trusted_catalog,
        "catalog_columns": deepcopy(catalog_columns or runtime_columns),
        "required_param_names": ["LOT_ID"],
        "required_params": {"LOT_ID": "TSSJQ07AH"},
        "filters": {},
        "filter_mappings": {
            "LOT_ID": ["LOT_ID"],
            "OPER_NAME": ["OPER_NAME"],
        },
        "default_detail_columns": runtime_columns,
    }
    return {
        "request": {"question": question},
        "intent_plan": {
            "retrieval_jobs": [job],
            "pandas_execution_plan": deepcopy(steps or []),
            "output_contract": {
                "result_mode": "detail",
                "required_columns": [*runtime_columns, "TSV_DIE_TYP"],
                "result_columns": [*runtime_columns, "TSV_DIE_TYP"],
                "strict_result_columns": True,
            },
        },
        "runtime_sources": {"hold_history": rows},
        "source_results": [
            {
                "dataset_key": "hold_history",
                "source_alias": "hold_history",
                "status": "ok",
                "columns": runtime_columns,
                "row_count": 1,
            }
        ],
        "trace": {"inspection": {}},
    }


def test_unrequested_uncataloged_detail_column_is_pruned_without_schema_block():
    adapter = load_module(ADAPTER_PATH)

    adapted = adapter.build_retrieval_payload(_hold_history_payload())

    contract = adapted["intent_plan"]["output_contract"]
    assert contract["result_columns"] == [
        "LOT_ID",
        "OPER_NAME",
        "HOLD_TM",
        "HOLD_CD",
        "HOLD_DESC",
    ]
    assert contract["required_columns"] == contract["result_columns"]
    assert contract["contract_reconciliation"] == {
        "status": "applied",
        "policy": "trusted_direct_detail_optional_projection",
        "dropped_optional_columns": ["TSV_DIE_TYP"],
    }
    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "complete"
    assert schema["sources"][0]["unresolved_required_columns"] == []
    assert not adapted["trace"].get("errors")


def test_user_requested_unsupported_detail_column_remains_fail_closed():
    adapter = load_module(ADAPTER_PATH)

    adapted = adapter.build_retrieval_payload(
        _hold_history_payload(question="TSSJQ07AH HOLD 이력에서 TSV_DIE_TYP도 보여줘")
    )

    contract = adapted["intent_plan"]["output_contract"]
    assert "TSV_DIE_TYP" in contract["required_columns"]
    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "error"
    assert schema["sources"][0]["unresolved_required_columns"] == ["TSV_DIE_TYP"]


def test_catalog_declared_but_runtime_missing_detail_column_remains_fail_closed():
    adapter = load_module(ADAPTER_PATH)
    declared = [
        "LOT_ID",
        "OPER_NAME",
        "HOLD_TM",
        "HOLD_CD",
        "HOLD_DESC",
        "TSV_DIE_TYP",
    ]

    adapted = adapter.build_retrieval_payload(
        _hold_history_payload(catalog_columns=declared)
    )

    contract = adapted["intent_plan"]["output_contract"]
    assert "TSV_DIE_TYP" in contract["required_columns"]
    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "error"
    assert schema["sources"][0]["unresolved_required_columns"] == ["TSV_DIE_TYP"]


def test_typed_projection_column_remains_execution_critical():
    adapter = load_module(ADAPTER_PATH)
    steps = [
        {
            "node_id": "select_history",
            "operation": "select_columns",
            "source_alias": "hold_history",
            "columns": ["LOT_ID", "TSV_DIE_TYP"],
        }
    ]

    adapted = adapter.build_retrieval_payload(
        _hold_history_payload(steps=steps)
    )

    reconciliation = adapted["trace"]["inspection"][
        "direct_output_column_reconciliation"
    ]
    assert reconciliation["status"] == "not_needed"
    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "error"
    assert schema["sources"][0]["unresolved_required_columns"] == ["TSV_DIE_TYP"]


def test_untrusted_job_does_not_relax_detail_output_contract():
    adapter = load_module(ADAPTER_PATH)

    adapted = adapter.build_retrieval_payload(
        _hold_history_payload(trusted_catalog=False)
    )

    reconciliation = adapted["trace"]["inspection"][
        "direct_output_column_reconciliation"
    ]
    assert reconciliation["status"] == "not_needed"
    assert reconciliation["reason"] == "trusted_catalog_contract_unavailable"
    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "error"
    assert schema["sources"][0]["unresolved_required_columns"] == ["TSV_DIE_TYP"]


def test_hydrator_carries_only_trusted_catalog_columns_for_runtime_schema_policy():
    hydrator = load_module(HYDRATOR_PATH)
    payload = {
        "request": {"question": "TSSJQ07AH LOT Hold 이력 조회해줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "hold_history",
                    "source_alias": "hold_history",
                    "required_params": {"LOT_ID": "TSSJQ07AH"},
                    "columns": ["TSV_DIE_TYP"],
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["LOT_ID", "HOLD_TM", "TSV_DIE_TYP"],
            },
        },
        "trace": {"inspection": {}},
    }
    catalog = {
        "table_catalog_items": [
            {
                "dataset_key": "hold_history",
                "status": "active",
                "payload": {
                    "dataset_key": "hold_history",
                    "source_type": "oracle",
                    "required_params": ["LOT_ID"],
                    "columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
                    "default_detail_columns": [
                        "LOT_ID",
                        "OPER_NAME",
                        "HOLD_TM",
                        "HOLD_CD",
                        "HOLD_DESC",
                    ],
                    "filter_mappings": {"LOT_ID": ["LOT_ID"]},
                    "source_config": {
                        "query_template": "SELECT LOT_ID, OPER_NAME, HOLD_TM, HOLD_CD, HOLD_DESC FROM HOLD_HIS WHERE LOT_ID IN ({LOT_ID})"
                    },
                },
            }
        ]
    }

    hydrated = hydrator.hydrate_retrieval_jobs(payload, catalog, "dummy")

    job = hydrated["intent_plan"]["retrieval_jobs"][0]
    assert job["trusted_catalog"] is True
    assert job["catalog_columns"] == [
        "LOT_ID",
        "OPER_NAME",
        "HOLD_TM",
        "HOLD_CD",
        "HOLD_DESC",
    ]
    assert "TSV_DIE_TYP" not in job["catalog_columns"]


def test_supported_catalog_detail_shape_is_unchanged():
    adapter = load_module(ADAPTER_PATH)
    payload = _hold_history_payload()
    expected_columns = ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"]
    payload["intent_plan"]["output_contract"]["required_columns"] = expected_columns
    payload["intent_plan"]["output_contract"]["result_columns"] = expected_columns

    adapted = adapter.build_retrieval_payload(payload)

    contract = adapted["intent_plan"]["output_contract"]
    assert contract["required_columns"] == expected_columns
    assert contract["result_columns"] == expected_columns
    reconciliation = adapted["trace"]["inspection"][
        "direct_output_column_reconciliation"
    ]
    assert reconciliation["status"] == "not_needed"
    assert reconciliation["reason"] == "no_missing_optional_display_columns"
    assert adapted["trace"]["inspection"]["source_schema_resolution"]["status"] == "complete"


def test_aggregate_derived_sort_column_is_not_promoted_to_raw_source_requirement():
    adapter = load_module(ADAPTER_PATH)
    payload = {
        "request": {"question": "공정별 생산량을 큰 순서로 알려줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production_today",
                    "source_alias": "production_today",
                    "trusted_catalog": True,
                    "catalog_columns": ["OPER_NAME", "PRODUCTION"],
                    "filters": {},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "aggregate_production",
                    "operation": "groupby_and_aggregate",
                    "source_alias": "production_today",
                    "group_by": ["OPER_NAME"],
                    "aggregations": [
                        {
                            "column": "PRODUCTION",
                            "method": "sum",
                            "output_column": "PRODUCTION_SUM",
                        }
                    ],
                    "output_alias": "grouped_production",
                },
                {
                    "node_id": "sort_production",
                    "operation": "sort_and_top_n",
                    "source_alias": "grouped_production",
                    "sort_by": "PRODUCTION_SUM",
                    "order": "desc",
                    "limit": 0,
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["OPER_NAME"],
                "metric_columns": ["PRODUCTION_SUM"],
                "required_columns": ["OPER_NAME", "PRODUCTION_SUM"],
                "result_columns": ["OPER_NAME", "PRODUCTION_SUM"],
                "strict_result_columns": True,
            },
        },
        "runtime_sources": {
            "production_today": [
                {"OPER_NAME": "D/A1", "PRODUCTION": 10},
                {"OPER_NAME": "D/A2", "PRODUCTION": 20},
            ]
        },
        "source_results": [
            {
                "dataset_key": "production_today",
                "source_alias": "production_today",
                "status": "ok",
                "columns": ["OPER_NAME", "PRODUCTION"],
                "row_count": 2,
            }
        ],
        "trace": {"inspection": {}},
    }

    adapted = adapter.build_retrieval_payload(payload)

    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "complete"
    assert schema["sources"][0]["required_runtime_columns"] == [
        "OPER_NAME",
        "PRODUCTION",
    ]
    assert "PRODUCTION_SUM" not in schema["sources"][0]["required_runtime_columns"]


def test_hydrated_hold_history_replay_runs_fast_without_model_code():
    hydrator = load_module(HYDRATOR_PATH)
    adapter = load_module(ADAPTER_PATH)
    resolver = load_module(RESOLVER_PATH)
    executor = load_module(EXECUTOR_PATH)
    payload = {
        "request": {"question": "TSSJQ07AH LOT Hold 이력 조회해줘"},
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "hold_history",
                    "source_alias": "hold_history",
                    "required_params": {"LOT_ID": "TSSJQ07AH"},
                }
            ],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": "detail",
                "required_columns": ["LOT_ID", "HOLD_TM", "HOLD_DESC", "TSV_DIE_TYP"],
                "result_columns": ["LOT_ID", "HOLD_TM", "HOLD_DESC", "TSV_DIE_TYP"],
                "strict_result_columns": True,
            },
        },
        "trace": {"inspection": {}},
    }
    catalog_columns = ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"]
    catalog = {
        "table_catalog_items": [
            {
                "dataset_key": "hold_history",
                "status": "active",
                "payload": {
                    "dataset_key": "hold_history",
                    "source_type": "oracle",
                    "required_params": ["LOT_ID"],
                    "columns": catalog_columns,
                    "default_detail_columns": catalog_columns,
                    "filter_mappings": {"LOT_ID": ["LOT_ID"]},
                    "source_config": {
                        "query_template": "SELECT LOT_ID, OPER_NAME, HOLD_TM, HOLD_CD, HOLD_DESC FROM HOLD_HIS WHERE LOT_ID IN ({LOT_ID})"
                    },
                },
            }
        ]
    }
    hydrated = hydrator.hydrate_retrieval_jobs(payload, catalog, "dummy")
    hydrated["runtime_sources"] = {
        "hold_history": [
            {
                "LOT_ID": "TSSJQ07AH",
                "OPER_NAME": "W/B1",
                "HOLD_TM": "2026-08-20 08:00:00",
                "HOLD_CD": "H001",
                "HOLD_DESC": "검증용 HOLD",
            }
        ]
    }
    hydrated["source_results"] = [
        {
            "dataset_key": "hold_history",
            "source_alias": "hold_history",
            "status": "ok",
            "columns": catalog_columns,
            "row_count": 1,
        }
    ]

    adapted = adapter.build_retrieval_payload(hydrated)
    resolved = resolver.resolve_simple_analysis_contract(adapted)
    model_calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused on fast route",
        model_invoker=lambda prompt: model_calls.append(prompt) or '{"code":"result = pd.DataFrame()"}',
        repair_prompt_template="repair",
    )

    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "fast"
    assert executed["data"]["columns"] == ["LOT_ID", "HOLD_TM", "HOLD_DESC"]
    assert executed["data"]["rows"] == [
        {
            "LOT_ID": "TSSJQ07AH",
            "HOLD_TM": "2026-08-20 08:00:00",
            "HOLD_DESC": "검증용 HOLD",
        }
    ]
    assert model_calls == []
