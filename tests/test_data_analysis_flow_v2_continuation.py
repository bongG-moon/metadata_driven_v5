from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from tools.validate_representative_questions import install_lfx_stubs


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2_continuation"


def _module(name: str, filename: str):
    if "lfx" not in sys.modules:
        install_lfx_stubs()
    path = COMPONENT_ROOT / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _repo_module(name: str, relative_path: str):
    if "lfx" not in sys.modules:
        install_lfx_stubs()
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def executor():
    return _module("_continuation_executor_tests", "17_continuation_hybrid_analysis_executor.py")


@pytest.fixture(scope="module")
def intent_router():
    return _module("_continuation_intent_router_tests", "03b_continuation_aware_intent_router.py")


@pytest.fixture(scope="module")
def request_loader():
    return _module(
        "_continuation_request_loader_tests",
        "00_continuation_analysis_request_loader.py",
    )


@pytest.fixture(scope="module")
def compiler():
    return _module("_continuation_compiler_tests", "04b_dependent_retrieval_plan_compiler.py")


@pytest.fixture(scope="module")
def v2_intent_normalizer():
    return _repo_module(
        "_continuation_v2_intent_normalizer_tests",
        "langflow_components/data_analysis_flow_v2/04_intent_plan_normalizer.py",
    )


@pytest.fixture(scope="module")
def upstream_binder():
    return _repo_module(
        "_continuation_upstream_binder_tests",
        "langflow_components/data_analysis_flow/05a_upstream_entity_parameter_binder.py",
    )


@pytest.fixture(scope="module")
def catalog_closure():
    return _module(
        "_continuation_catalog_closure_tests",
        "01e_dependency_catalog_candidate_closure.py",
    )


@pytest.fixture(scope="module")
def result_loader():
    return _module("_continuation_result_loader_tests", "05_continuation_mongodb_result_loader.py")


@pytest.fixture(scope="module")
def binding_alias_normalizer():
    return _module(
        "_continuation_binding_alias_normalizer_tests",
        "05a_continuation_binding_alias_normalizer.py",
    )


def test_extreme_row_chain_applies_filter_before_latest_selection(executor):
    payload = {
        "intent_plan": {
            "retrieval_jobs": [{"dataset_key": "history", "source_alias": "history"}],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_history",
                    "operation": "apply_filters",
                    "inputs": [{"kind": "external_source", "ref": "history"}],
                    "output_alias": "valid_history",
                    "filters": {"STATUS": {"operator": "eq", "value": "VALID"}},
                },
                {
                    "node_id": "latest_history",
                    "operation": "select_extreme_row_per_group",
                    "inputs": [{"kind": "node_output", "ref": "filter_history"}],
                    "output_alias": "latest",
                    "partition_by": ["ENTITY_ID"],
                    "order_by": [{"column": "EVENT_TM", "direction": "desc"}],
                    "tie_breakers": [{"column": "EVENT_SEQ", "direction": "desc"}],
                    "limit_per_group": 1,
                    "tie_policy": "first",
                    "projection": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "STATUS", "DETAIL"],
                    "strict": True,
                },
            ],
            "output_contract": {
                "result_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "STATUS", "DETAIL"]
            },
        }
    }
    contract = executor._select_extreme_row_per_group_contract(payload)
    assert contract["source_alias"] == "history"
    assert contract["pre_operations"][0]["operation"] == "apply_filters"
    history = pd.DataFrame(
        [
            {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 10:00", "EVENT_SEQ": 1, "STATUS": "VALID", "DETAIL": "keep"},
            {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 12:00", "EVENT_SEQ": 2, "STATUS": "INVALID", "DETAIL": "reject"},
        ]
    )
    result, certificate = executor._execute_select_extreme_row_per_group(
        contract,
        {"history": history},
        pd,
    )
    assert result.to_dict(orient="records") == [
        {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 10:00", "EVENT_SEQ": 1, "STATUS": "VALID", "DETAIL": "keep"}
    ]
    assert certificate["predecessor_execution"][0]["row_count_after"] == 1


def test_extreme_tie_error_uses_declared_tie_breakers(executor):
    contract = {
        "operation": "select_extreme_row_per_group",
        "source_alias": "history",
        "partition_by": ["ENTITY_ID"],
        "order_by": [{"column": "EVENT_TM", "direction": "desc"}],
        "tie_breakers": [{"column": "EVENT_SEQ", "direction": "desc"}],
        "limit_per_group": 1,
        "tie_policy": "error",
        "projection": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "DETAIL"],
        "strict": True,
    }
    unique = pd.DataFrame(
        [
            {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 12:00", "EVENT_SEQ": 1, "DETAIL": "old"},
            {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 12:00", "EVENT_SEQ": 2, "DETAIL": "winner"},
        ]
    )
    result, _ = executor._execute_select_extreme_row_per_group(contract, {"history": unique}, pd)
    assert result.iloc[0]["DETAIL"] == "winner"

    ambiguous = pd.concat([unique, unique.iloc[[1]].assign(DETAIL="ambiguous")], ignore_index=True)
    with pytest.raises(executor.OutputContractError, match="동률"):
        executor._execute_select_extreme_row_per_group(contract, {"history": ambiguous}, pd)


def test_extreme_include_all_keeps_primary_boundary_rows(executor):
    contract = {
        "operation": "select_extreme_row_per_group",
        "source_alias": "history",
        "partition_by": ["ENTITY_ID"],
        "order_by": [{"column": "SCORE", "direction": "desc"}],
        "tie_breakers": [{"column": "EVENT_SEQ", "direction": "asc"}],
        "limit_per_group": 2,
        "tie_policy": "include_all",
        "projection": ["ENTITY_ID", "SCORE", "EVENT_SEQ"],
        "strict": True,
    }
    frame = pd.DataFrame(
        [
            {"ENTITY_ID": "A", "SCORE": 10, "EVENT_SEQ": 1},
            {"ENTITY_ID": "A", "SCORE": 9, "EVENT_SEQ": 2},
            {"ENTITY_ID": "A", "SCORE": 9, "EVENT_SEQ": 3},
            {"ENTITY_ID": "A", "SCORE": 8, "EVENT_SEQ": 4},
        ]
    )
    result, _ = executor._execute_select_extreme_row_per_group(contract, {"history": frame}, pd)
    assert result["EVENT_SEQ"].tolist() == [1, 2, 3]


def test_strict_extreme_complex_path_skips_pandas_and_repair_models(executor):
    calls: list[str] = []
    payload = {
        "intent_plan": {
            "retrieval_jobs": [{"dataset_key": "history", "source_alias": "history"}],
            "pandas_execution_plan": [
                {
                    "operation": "select_extreme_row_per_group",
                    "inputs": [{"kind": "external_source", "ref": "history"}],
                    "source_alias": "history",
                    "partition_by": ["ENTITY_ID"],
                    "order_by": [{"column": "EVENT_TM", "direction": "desc"}],
                    "tie_breakers": [{"column": "EVENT_SEQ", "direction": "desc"}],
                    "limit_per_group": 1,
                    "tie_policy": "first",
                    "projection": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ"],
                    "strict": True,
                }
            ],
            "output_contract": {
                "required_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ"],
                "result_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ"],
                "grain_columns": ["ENTITY_ID"],
                "metric_columns": [],
                "strict_result_columns": True,
            },
        },
        "simple_analysis_contract": {"route": "complex", "requires_pandas_llm": True},
        "runtime_sources": {
            "history": [
                {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 10:00", "EVENT_SEQ": 1},
                {"ENTITY_ID": "A", "EVENT_TM": "2026-08-08 11:00", "EVENT_SEQ": 2},
            ]
        },
        "source_results": [
            {"dataset_key": "history", "source_alias": "history", "status": "ok", "row_count": 2}
        ],
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    result = executor.execute_hybrid_analysis(
        payload,
        "must not be sent",
        lambda prompt: calls.append(prompt) or "{}",
        "repair",
        max_repair_attempts=1,
    )
    assert calls == []
    assert result["analysis"]["status"] == "ok"
    llm_calls = result["trace"]["inspection"]["fast_path"]["llm_calls"]
    assert llm_calls["pandas_generation"] == 0
    assert llm_calls["repair"] == 0


def _dependent_plan_and_catalog():
    binding = {
        "source_stage_id": "stage_1",
        "source_column": "ENTITY_ID",
        "target_source_alias": "history",
        "target_param": "ENTITY_ID",
        "operator": "in",
        "entity_type": "entity",
    }
    plan = {
        "version": "analysis.dependent_retrieval.v1",
        "max_stages": 2,
        "final_stage_id": "stage_2",
        "stages": [
            {
                "stage_id": "stage_1",
                "depends_on": [],
                "retrieval_jobs": [{"dataset_key": "index", "source_alias": "index"}],
                "pandas_execution_plan": [],
                "output_contract": {"result_columns": ["ENTITY_ID"]},
                "handoff": {"columns": ["ENTITY_ID"]},
            },
            {
                "stage_id": "stage_2",
                "depends_on": ["stage_1"],
                "retrieval_jobs": [
                    {
                        "dataset_key": "history",
                        "source_alias": "history",
                        "required_params": {"ENTITY_ID": ""},
                    }
                ],
                "pandas_execution_plan": [
                    {
                        "operation": "select_extreme_row_per_group",
                        "source_alias": "history",
                        "partition_by": ["ENTITY_ID"],
                        "order_by": [{"column": "EVENT_TM", "direction": "desc"}],
                        "tie_breakers": [{"column": "EVENT_SEQ", "direction": "desc"}],
                        "projection": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "DETAIL"],
                        "limit_per_group": 1,
                        "tie_policy": "first",
                        "strict": True,
                    }
                ],
                "output_contract": {
                    "result_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "DETAIL"]
                },
                "input_bindings": [binding],
            },
        ],
    }
    metadata = {
        "table_catalog_items": [
            {"key": "index", "payload": {"columns": ["ENTITY_ID"]}},
            {
                "key": "history",
                "payload": {
                    "columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "DETAIL"],
                    "required_params": ["ENTITY_ID"],
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "entity_type": "entity",
                                "source_alias": "previous_result",
                                "source_column": "ENTITY_ID",
                                "target_param": "ENTITY_ID",
                                "operator": "in",
                            }
                        ]
                    },
                },
            },
        ]
    }
    return plan, metadata


def test_catalog_closure_protects_domain_dependency_tables_within_five(catalog_closure):
    selected = {
        "metadata_candidates": {
            "domain_items": [
                {
                    "section": "analysis_recipes",
                    "key": "dependent_lookup",
                    "payload": {"source_datasets": ["entity_index", "entity_history"]},
                }
            ],
            "table_catalog_items": [
                {"key": "temporal_a", "payload": {"columns": ["A"]}},
                {"key": "temporal_b", "payload": {"columns": ["B"]}},
                {"key": "temporal_c", "payload": {"columns": ["C"]}},
                {"key": "temporal_d", "payload": {"columns": ["D"]}},
                {"key": "entity_history", "payload": {"columns": ["ENTITY_ID", "EVENT_TM"]}},
            ],
            "main_flow_filters": [],
        },
        "metadata_load": {"selected_counts": {"table_catalog_items": 5}},
    }
    full_catalog = {
        "table_catalog_items": [
            {"key": "entity_index", "payload": {"columns": ["ENTITY_ID"]}},
            {"key": "entity_history", "payload": {"columns": ["ENTITY_ID", "EVENT_TM"]}},
            *selected["metadata_candidates"]["table_catalog_items"][:4],
        ]
    }
    result = catalog_closure.close_dependency_catalog_candidates(
        selected,
        full_catalog,
        max_table_items=5,
    )
    keys = [item["key"] for item in result["metadata_candidates"]["table_catalog_items"]]
    assert keys[:2] == ["entity_index", "entity_history"]
    assert len(keys) == 5
    trace = result["metadata_load"]["dependency_catalog_closure"]
    assert trace["status"] == "complete"
    assert trace["included_dataset_refs"] == ["entity_index", "entity_history"]


def test_catalog_closure_follows_only_explicit_catalog_dataset_links(catalog_closure):
    selected = {
        "metadata_candidates": {
            "domain_items": [
                {"section": "recipe", "key": "index", "payload": {"source_dataset": "entity_index"}}
            ],
            "table_catalog_items": [
                {"key": "entity_index", "payload": {"columns": ["ENTITY_ID"]}},
                {"key": "unrelated", "payload": {"columns": ["VALUE"]}},
            ],
        }
    }
    full_catalog = {
        "table_catalog_items": [
            *selected["metadata_candidates"]["table_catalog_items"],
            {
                "key": "entity_history",
                "payload": {
                    "columns": ["ENTITY_ID", "EVENT_TM"],
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "source_dataset_key": "entity_index",
                                "source_column": "ENTITY_ID",
                                "target_param": "ENTITY_ID",
                            }
                        ]
                    },
                },
            },
        ]
    }
    result = catalog_closure.close_dependency_catalog_candidates(selected, full_catalog, max_table_items=5)
    keys = [item["key"] for item in result["metadata_candidates"]["table_catalog_items"]]
    assert keys[:2] == ["entity_index", "entity_history"]
    assert "unrelated" in keys


def test_catalog_closure_preserves_same_family_temporal_companion_within_limit(catalog_closure):
    selected = {
        "metadata_candidates": {
            "domain_items": [],
            "table_catalog_items": [
                {
                    "key": "production",
                    "payload": {
                        "dataset_family": "production",
                        "selection_criteria": {"time_scope": "history"},
                        "columns": ["TECH", "MODE"],
                    },
                },
                {"key": "ranked_2", "payload": {"columns": ["A"]}},
                {"key": "ranked_3", "payload": {"columns": ["B"]}},
                {"key": "ranked_4", "payload": {"columns": ["C"]}},
                {"key": "ranked_5", "payload": {"columns": ["D"]}},
            ],
        }
    }
    full_catalog = {
        "table_catalog_items": [
            *selected["metadata_candidates"]["table_catalog_items"],
            {
                "key": "production_today",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": ["TECH", "MODE"],
                },
            },
        ]
    }
    result = catalog_closure.close_dependency_catalog_candidates(
        selected,
        full_catalog,
        max_table_items=5,
    )
    keys = [item["key"] for item in result["metadata_candidates"]["table_catalog_items"]]
    assert keys[:2] == ["production", "production_today"]
    assert len(keys) == 5
    trace = result["metadata_load"]["dependency_catalog_closure"]
    assert trace["temporal_companion_refs"][:2] == ["production", "production_today"]
    assert "production_today" in trace["included_temporal_companion_refs"]


def test_compiler_accepts_only_catalog_backed_extreme_columns_and_binding_linkage(compiler):
    plan, metadata = _dependent_plan_and_catalog()
    normalized = compiler._normalize_and_validate_plan(plan, metadata)
    assert normalized["stages"][1]["input_bindings"][0]["source_column"] == "ENTITY_ID"

    invented_tie_breaker = deepcopy(plan)
    invented_tie_breaker["stages"][1]["pandas_execution_plan"][0]["tie_breakers"] = [
        {"column": "INVENTED_SEQ", "direction": "desc"}
    ]
    with pytest.raises(ValueError, match="INVENTED_SEQ"):
        compiler._normalize_and_validate_plan(invented_tie_breaker, metadata)

    disconnected_handoff = deepcopy(plan)
    disconnected_handoff["stages"][0]["handoff"]["columns"] = ["OTHER_ID"]
    disconnected_handoff["stages"][0]["output_contract"]["result_columns"] = ["OTHER_ID"]
    metadata_with_other = deepcopy(metadata)
    metadata_with_other["table_catalog_items"][0]["payload"]["columns"].append("OTHER_ID")
    with pytest.raises(ValueError, match="stage1 handoff"):
        compiler._normalize_and_validate_plan(disconnected_handoff, metadata_with_other)

    non_strict = deepcopy(plan)
    non_strict["stages"][1]["pandas_execution_plan"][0]["strict"] = False
    with pytest.raises(ValueError, match="strict must be true"):
        compiler._normalize_and_validate_plan(non_strict, metadata)


def test_compiler_hydrates_unique_catalog_entity_type_and_strengthens_empty_tie(compiler):
    plan, metadata = _dependent_plan_and_catalog()
    binding = plan["stages"][1]["input_bindings"][0]
    binding.pop("entity_type")
    extreme = plan["stages"][1]["pandas_execution_plan"][0]
    extreme["tie_breakers"] = []
    extreme["tie_policy"] = "first"
    normalized = compiler._normalize_and_validate_plan(plan, metadata)
    normalized_binding = normalized["stages"][1]["input_bindings"][0]
    normalized_extreme = normalized["stages"][1]["pandas_execution_plan"][0]
    assert normalized_binding["entity_type"] == "entity"
    assert normalized_extreme["tie_policy"] == "error"
    assert normalized_extreme["tie_breakers"] == []


def test_compiler_repairs_recoverable_extra_stage_wrapper_from_catalog(compiler):
    dependent, metadata = _dependent_plan_and_catalog()
    malformed = deepcopy(dependent)
    malformed["stages"].append(
        {
            "stage_id": "planner_only",
            "depends_on": [],
            "retrieval_jobs": [],
            "pandas_execution_plan": [],
            "output_contract": {},
        }
    )
    root_plan = {
        "retrieval_jobs": [
            deepcopy(dependent["stages"][0]["retrieval_jobs"][0]),
            deepcopy(dependent["stages"][1]["retrieval_jobs"][0]),
        ],
        "pandas_execution_plan": [],
        "output_contract": {"result_columns": ["ENTITY_ID"]},
        "dependent_retrieval_plan": malformed,
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "entity history"}},
        json.dumps({"intent_plan": root_plan}, ensure_ascii=False),
        metadata,
    )
    compiled = json.loads(response)["intent_plan"]
    assert trace["dependent_shape_repaired"] is True
    assert len(compiled["dependent_retrieval_plan"]["stages"]) == 2
    assert compiled["dependent_retrieval_plan"]["stages"][1]["input_bindings"][0]["target_param"] == "ENTITY_ID"


def test_compiler_keeps_unrecoverable_dependent_shape_fail_closed(compiler):
    dependent, _ = _dependent_plan_and_catalog()
    malformed = deepcopy(dependent)
    malformed["stages"] = [deepcopy(dependent["stages"][0]), {"stage_id": "unknown"}]
    with pytest.raises(ValueError):
        compiler.compile_intent_response(
            {"request": {"question": "ambiguous dependent lookup"}},
            json.dumps(
                {
                    "intent_plan": {
                        "retrieval_jobs": [{"dataset_key": "unknown", "source_alias": "unknown"}],
                        "pandas_execution_plan": [],
                        "output_contract": {},
                        "dependent_retrieval_plan": malformed,
                    }
                },
                ensure_ascii=False,
            ),
            {"table_catalog_items": []},
        )


def _flat_plan_with_optional_unused_history():
    plan, _ = _dependent_plan_and_catalog()
    flat = {
        "analysis_kind": "entity_list",
        "retrieval_jobs": [
            {"dataset_key": "index", "source_alias": "index", "required_params": {}},
            {
                "dataset_key": "history",
                "source_alias": "history",
                "required_params": {"ENTITY_ID": ""},
            },
        ],
        "pandas_execution_plan": [
            {
                "node_id": "select_index",
                "operation": "select_columns",
                "source_alias": "index",
                "inputs": [{"kind": "external_source", "ref": "index"}],
                "projection": ["ENTITY_ID"],
                "output_alias": "entity_list",
            }
        ],
        "output_contract": {
            "required_columns": ["ENTITY_ID"],
            "result_columns": ["ENTITY_ID"],
        },
        "dependent_retrieval_plan": plan,
    }
    return flat


def test_compiler_ignores_incoherent_dependent_plan_and_prunes_unreachable_job(compiler):
    flat = _flat_plan_with_optional_unused_history()
    _, metadata = _dependent_plan_and_catalog()
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "entity list"}},
        json.dumps({"intent_plan": flat}),
        metadata,
    )
    compiled = json.loads(response)["intent_plan"]
    assert "dependent_retrieval_plan" not in compiled
    assert [job["source_alias"] for job in compiled["retrieval_jobs"]] == ["index"]
    assert trace["untrusted_dependent_ignored"] is True
    assert trace["dependent_ignore_reason"] == "dependent_final_contract_incoherent"


def test_compiler_keeps_fail_closed_when_referenced_job_has_missing_required_param(compiler):
    flat = _flat_plan_with_optional_unused_history()
    flat["pandas_execution_plan"] = [
        {
            "operation": "select_columns",
            "source_alias": "history",
            "inputs": [{"kind": "external_source", "ref": "history"}],
            "projection": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "DETAIL"],
        }
    ]
    flat["output_contract"] = {
        "result_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ", "DETAIL"]
    }
    flat["dependent_retrieval_plan"]["stages"][1]["input_bindings"][0]["target_param"] = "WRONG_ID"
    _, metadata = _dependent_plan_and_catalog()
    with pytest.raises(ValueError, match="input_binding"):
        compiler.compile_intent_response(
            {"request": {"question": "entity history"}},
            json.dumps({"intent_plan": flat}),
            metadata,
        )


def test_compiler_ignores_unproven_dependent_binding_when_flat_plan_is_complete(compiler):
    flat = {
        "analysis_kind": "entity_list",
        "retrieval_jobs": [
            {"dataset_key": "index", "source_alias": "index", "required_params": {}}
        ],
        "pandas_execution_plan": [
            {
                "operation": "select_columns",
                "source_alias": "index",
                "projection": ["ENTITY_ID"],
            }
        ],
        "output_contract": {"result_columns": ["ENTITY_ID"]},
        "dependent_retrieval_plan": {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 2,
            "stages": [
                {
                    "stage_id": "stage_1",
                    "depends_on": [],
                    "retrieval_jobs": [{"dataset_key": "index", "source_alias": "index"}],
                    "pandas_execution_plan": [],
                    "output_contract": {"result_columns": ["ENTITY_ID"]},
                    "handoff": {"columns": ["ENTITY_ID"]},
                },
                {
                    "stage_id": "stage_2",
                    "depends_on": ["stage_1"],
                    "retrieval_jobs": [{"dataset_key": "detail", "source_alias": "detail"}],
                    "pandas_execution_plan": [],
                    "output_contract": {"result_columns": ["ENTITY_ID"]},
                    "input_bindings": [
                        {
                            "source_stage_id": "stage_1",
                            "source_column": "ENTITY_ID",
                            "target_source_alias": "detail",
                            "target_param": "INVENTED_PARAM",
                            "operator": "in",
                        }
                    ],
                },
            ],
        },
    }
    metadata = {
        "table_catalog_items": [
            {"key": "index", "payload": {"columns": ["ENTITY_ID"]}},
            {"key": "detail", "payload": {"columns": ["ENTITY_ID"]}},
        ]
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "entity list"}},
        json.dumps({"intent_plan": flat}),
        metadata,
    )
    compiled = json.loads(response)["intent_plan"]
    assert "dependent_retrieval_plan" not in compiled
    assert trace["dependent_ignore_reason"] == "dependent_binding_not_catalog_proven"


def test_compiler_does_not_ignore_nonempty_one_stage_shape_even_with_complete_flat(compiler):
    response = {
        "intent_plan": {
            "retrieval_jobs": [{"dataset_key": "index", "source_alias": "index"}],
            "pandas_execution_plan": [
                {
                    "operation": "select_columns",
                    "source_alias": "index",
                    "projection": ["ENTITY_ID"],
                }
            ],
            "output_contract": {"result_columns": ["ENTITY_ID"]},
            "dependent_retrieval_plan": {
                "version": "analysis.dependent_retrieval.v1",
                "max_stages": 2,
                "stages": [
                    {
                        "stage_id": "only_stage",
                        "depends_on": [],
                        "retrieval_jobs": [{"dataset_key": "index", "source_alias": "index"}],
                        "pandas_execution_plan": [],
                        "output_contract": {"result_columns": ["ENTITY_ID"]},
                        "handoff": {"columns": ["ENTITY_ID"]},
                    }
                ],
            },
        }
    }
    metadata = {"table_catalog_items": [{"key": "index", "payload": {"columns": ["ENTITY_ID"]}}]}
    with pytest.raises(ValueError, match="2"):
        compiler.compile_intent_response(
            {"request": {"question": "entity list"}},
            json.dumps(response),
            metadata,
        )


def _fresh_live_shape_catalogs():
    product_columns = [
        "TECH",
        "DEN",
        "MODE",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "LEAD",
        "MCP_NO",
    ]
    return {
        "table_catalog_items": [
            {
                "key": "lot_status",
                "payload": {
                    "columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"]
                },
            },
            {
                "key": "hold_history",
                "payload": {
                    "columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
                    "required_params": ["LOT_ID"],
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "entity_type": "lot",
                                "source_alias": "previous_result",
                                "source_column": "LOT_ID",
                                "target_param": "LOT_ID",
                                "operator": "in",
                            }
                        ]
                    },
                },
            },
            {
                "key": "production_today",
                "payload": {
                    "columns": [*product_columns, "PRODUCTION"],
                    "required_params": ["DATE"],
                },
            },
            {
                "key": "equipment_assign",
                "payload": {"columns": [*product_columns, "EQP_ID"]},
            },
        ]
    }


def _live_dependent_stage(
    *,
    stage1_contract: dict,
    stage2_contract: dict,
    stage1_steps: list[dict],
    stage2_steps: list[dict],
    stage1_job: dict,
    stage2_job: dict,
    handoff_columns: list[str],
    input_bindings: list[dict],
    stage1_id: str = "stage_1",
    stage2_id: str = "stage_2",
):
    return {
        "version": "analysis.dependent_retrieval.v1",
        "max_stages": 2,
        "stages": [
            {
                "stage_id": stage1_id,
                "depends_on": [],
                "retrieval_jobs": [stage1_job],
                "pandas_execution_plan": stage1_steps,
                "output_contract": stage1_contract,
                "handoff": {"columns": handoff_columns},
                "input_bindings": [],
            },
            {
                "stage_id": stage2_id,
                "depends_on": [stage1_id],
                "retrieval_jobs": [stage2_job],
                "pandas_execution_plan": stage2_steps,
                "output_contract": stage2_contract,
                "handoff": {"columns": []},
                "input_bindings": input_bindings,
            },
        ],
    }


def test_live_r28_shape_allows_catalog_proven_dependent_final_to_own_result(compiler):
    stage1_contract = {
        "required_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
        "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
    }
    stage2_contract = {
        "required_columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
        "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
    }
    dependent = _live_dependent_stage(
        stage1_id="stage1_lot_status",
        stage2_id="stage2_hold_history",
        stage1_contract=stage1_contract,
        stage2_contract=stage2_contract,
        stage1_steps=[
            {
                "node_id": "select_lot_id",
                "operation": "select_columns",
                "source_alias": "lot_status_src",
                "projection": list(stage1_contract["result_columns"]),
            }
        ],
        stage2_steps=[
            {
                "node_id": "get_latest_hold_history",
                "operation": "select_extreme_row_per_group",
                "source_alias": "hold_history_src",
                "partition_by": ["LOT_ID"],
                "order_by": [{"column": "HOLD_TM", "direction": "desc"}],
                "tie_breakers": [],
                "limit_per_group": 1,
                "tie_policy": "error",
                "projection": list(stage2_contract["result_columns"]),
                "strict": True,
            }
        ],
        stage1_job={"dataset_key": "lot_status", "source_alias": "lot_status_src"},
        stage2_job={
            "dataset_key": "hold_history",
            "source_alias": "hold_history_src",
            "required_params": {"LOT_ID": ""},
        },
        handoff_columns=["LOT_ID"],
        input_bindings=[
            {
                "source_stage_id": "stage1_lot_status",
                "source_column": "LOT_ID",
                "target_source_alias": "hold_history_src",
                "target_param": "LOT_ID",
                "operator": "in",
            }
        ],
    )
    intent = {
        "retrieval_jobs": [],
        "pandas_execution_plan": [],
        "output_contract": stage1_contract,
        "dependent_retrieval_plan": dependent,
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "dependent detail"}},
        json.dumps({"intent_plan": intent}),
        _fresh_live_shape_catalogs(),
    )
    compiled = json.loads(response)["intent_plan"]
    assert trace["status"] == "pending"
    assert compiled["retrieval_jobs"][0]["dataset_key"] == "lot_status"
    stored = compiled["dependent_retrieval_plan"]
    assert stored["stages"][1]["output_contract"]["result_columns"] == stage2_contract["result_columns"]
    assert stored["stages"][1]["input_bindings"][0]["entity_type"] == "lot"


def test_live_c01_shape_rewrites_cross_stage_alias_to_external_upstream_result(
    compiler,
    executor,
    v2_intent_normalizer,
):
    stage1_contract = {
        "required_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT"],
        "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT"],
    }
    final_columns = ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_TM", "HOLD_CD", "HOLD_DESC"]
    stage2_contract = {"required_columns": final_columns, "result_columns": final_columns}
    dependent = _live_dependent_stage(
        stage1_contract=stage1_contract,
        stage2_contract=stage2_contract,
        stage1_steps=[
            {
                "operation": "select_columns",
                "source_alias": "lot_status_src",
                "projection": list(stage1_contract["result_columns"]),
                "output_alias": "stage1_output",
            }
        ],
        stage2_steps=[
            {
                "operation": "select_extreme_row_per_group",
                "source_alias": "hold_history_src",
                "partition_by": ["LOT_ID"],
                "order_by": [{"column": "HOLD_TM", "direction": "desc"}],
                "tie_breakers": [],
                "limit_per_group": 1,
                "tie_policy": "error",
                "projection": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
                "strict": True,
                "output_alias": "latest_hold",
            },
            {
                "operation": "join",
                "inputs": [
                    {"kind": "node_output", "ref": "stage_1.stage1_output"},
                    {"kind": "node_output", "ref": "latest_hold"},
                ],
                "left_source_alias": "stage_1.stage1_output",
                "right_source_alias": "latest_hold",
                "join_type": "left",
                "output_alias": "joined_result",
            },
        ],
        stage1_job={"dataset_key": "lot_status", "source_alias": "lot_status_src"},
        stage2_job={
            "dataset_key": "hold_history",
            "source_alias": "hold_history_src",
            "required_params": {"LOT_ID": ""},
        },
        handoff_columns=["LOT_ID"],
        input_bindings=[
            {
                "source_stage_id": "stage_1",
                "source_column": "LOT_ID",
                "target_source_alias": "hold_history_src",
                "target_param": "LOT_ID",
                "operator": "in",
            }
        ],
    )
    intent = {
        "retrieval_jobs": [
            {"dataset_key": "lot_status", "source_alias": "lot_status_src"},
            {
                "dataset_key": "hold_history",
                "source_alias": "hold_history_src",
                "required_params": {"LOT_ID": ""},
            },
        ],
        "pandas_execution_plan": [],
        "output_contract": stage2_contract,
        "dependent_retrieval_plan": dependent,
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "dependent detail"}},
        json.dumps({"intent_plan": intent}),
        _fresh_live_shape_catalogs(),
    )
    assert trace["status"] == "pending"
    stored = json.loads(response)["intent_plan"]["dependent_retrieval_plan"]
    join = stored["stages"][1]["pandas_execution_plan"][1]
    assert join["join_type"] == "left"
    assert join["left_source_alias"] == "upstream_result"
    assert join["inputs"][0] == {"kind": "node_output", "ref": "upstream_result"}
    continuation_ref = f"continuation:{stored['plan_id']}:{stored['plan_hash']}"
    continuation_contract = {
        "version": stored["version"],
        "plan_id": stored["plan_id"],
        "plan_hash": stored["plan_hash"],
        "max_stages": 2,
        "current_stage_index": 0,
        "next_stage_index": 1,
        "continuation_ref": continuation_ref,
        "session_id": "session-1",
        "input_bindings": deepcopy(stored["stages"][1]["input_bindings"]),
    }
    resume_payload = {
        "request": {
            "question": "dependent detail",
            "session_id": "session-1",
            "continuation": {
                "continuation_ref": continuation_ref,
                "continuation_contract": continuation_contract,
            },
        },
        "orchestration": {
            "status": "ok",
            "upstream_result_ref": "result-1",
            "source_alias": "upstream_result",
        },
    }
    resumed_text, resumed_trace = compiler.compile_intent_response(
        resume_payload,
        response,
        _fresh_live_shape_catalogs(),
    )
    assert resumed_trace["status"] == "complete"
    resumed_plan = json.loads(resumed_text)["intent_plan"]
    assert resumed_plan["request_scope"] == "new_analysis"
    assert resumed_plan["reference_mode"] == "none"
    normalized_resume = v2_intent_normalizer.normalize_intent_plan(
        resume_payload,
        resumed_text,
        _fresh_live_shape_catalogs(),
    )["intent_plan"]
    assert normalized_resume.get("validation_errors", []) == []
    assert {
        (item["source_alias"], item["provider"])
        for item in normalized_resume["resolved_execution_graph"]["external_source_requirements"]
    } == {
        ("hold_history_src", "retrieval_job"),
        ("upstream_result", "previous_result"),
    }
    stage2_payload = {
        "intent_plan": {
            "retrieval_jobs": stored["stages"][1]["retrieval_jobs"],
            "pandas_execution_plan": stored["stages"][1]["pandas_execution_plan"],
            "output_contract": stored["stages"][1]["output_contract"],
        }
    }
    contract = executor._select_extreme_row_per_group_contract(stage2_payload)
    assert contract["join"]["left_source_alias"] == "upstream_result"
    result, _ = executor._execute_select_extreme_row_per_group(
        contract,
        {
            "upstream_result": pd.DataFrame(
                [
                    {"LOT_ID": "L1", "OPER_NAME": "P1", "HOLD_STAT": "OnHold"},
                    {"LOT_ID": "L2", "OPER_NAME": "P2", "HOLD_STAT": "OnHold"},
                ]
            ),
            "hold_history_src": pd.DataFrame(
                [
                    {
                        "LOT_ID": "L1",
                        "OPER_NAME": "P1",
                        "HOLD_TM": "2026-08-08 09:00",
                        "HOLD_CD": "OLD",
                        "HOLD_DESC": "old",
                    },
                    {
                        "LOT_ID": "L1",
                        "OPER_NAME": "P1",
                        "HOLD_TM": "2026-08-08 10:00",
                        "HOLD_CD": "NEW",
                        "HOLD_DESC": "new",
                    },
                ]
            ),
        },
        pd,
    )
    assert result["LOT_ID"].tolist() == ["L1", "L2"]
    assert result.loc[result["LOT_ID"] == "L1", "HOLD_CD"].iloc[0] == "NEW"
    assert pd.isna(result.loc[result["LOT_ID"] == "L2", "HOLD_CD"].iloc[0])


def test_process_scope_allows_previous_result_row_matched_history_source(v2_intent_normalizer):
    """A dependent history source inherits the parent's process scope via LOT_ID rows."""

    candidates = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "WB",
                "payload": {
                    "field": "OPER_NAME",
                    "processes": ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"],
                },
            }
        ]
    }
    jobs = [
        {
            "dataset_key": "hold_history",
            "source_alias": "hold_history_src",
            "filters": {},
        }
    ]
    row_match_plan = [
        {
            "operation": "apply_row_match_groups",
            "source_alias": "hold_history_src",
            "reference_source_alias": "previous_result",
            "match_columns": ["LOT_ID"],
        }
    ]

    guard = v2_intent_normalizer._validate_process_scope_contract(
        jobs,
        candidates,
        "위 LOT의 HOLD이력 알려줘",
        pandas_plan=row_match_plan,
        declared_processes=["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"],
    )

    assert guard["status"] == "dependent_scope_allowed"
    assert guard["validation_errors"] == []
    assert guard["dependent_scope_sources"] == ["hold_history_src"]


def test_process_scope_still_blocks_unscoped_non_dependent_source(v2_intent_normalizer):
    candidates = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "WB",
                "payload": {
                    "field": "OPER_NAME",
                    "processes": ["W/B1", "W/B2"],
                },
            }
        ]
    }
    guard = v2_intent_normalizer._validate_process_scope_contract(
        [{"dataset_key": "hold_history", "source_alias": "history_src", "filters": {}}],
        candidates,
        "W/B공정 현재 HOLD 이력 알려줘",
        pandas_plan=[],
        declared_processes=["W/B1", "W/B2"],
    )
    assert guard["status"] == "error"
    assert guard["validation_errors"][0]["type"] == "process_scope_incomplete"


def test_process_scope_validation_runs_after_automatic_previous_result_row_match(
    v2_intent_normalizer,
):
    candidates = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "WB",
                "payload": {
                    "field": "OPER_NAME",
                    "processes": ["W/B1", "W/B2"],
                },
            }
        ]
    }
    jobs = [{"dataset_key": "hold_history", "source_alias": "history_src", "filters": {}}]
    payload = {
        "state": {
            "last_intent_plan": {
                "resolved_grain_plan": {"canonical_columns": ["LOT_ID"]}
            }
        }
    }
    inserted = v2_intent_normalizer._ensure_previous_result_row_match_step(
        [], jobs, "previous_result_rows", payload
    )
    normalized, row_guard = v2_intent_normalizer._normalize_row_match_steps(
        inserted, jobs, "previous_result_rows", payload
    )
    guard = v2_intent_normalizer._validate_process_scope_contract(
        jobs,
        candidates,
        "위 LOT의 HOLD이력 알려줘",
        pandas_plan=normalized,
        declared_processes=["W/B1", "W/B2"],
    )
    assert row_guard["status"] == "applied"
    assert guard["status"] == "dependent_scope_allowed"
    assert guard["validation_errors"] == []


@pytest.mark.parametrize("blank_value", ["", "   ", [], {}])
def test_continuation_alias_normalizer_removes_only_trusted_blank_binding_placeholders(
    binding_alias_normalizer,
    upstream_binder,
    blank_value,
):
    payload = {
        "request": {"continuation": {"continuation_ref": "continuation:p:h"}},
        "orchestration": {
            "status": "ok",
            "upstream_result_ref": "result-1",
        },
        "runtime_sources": {"upstream_result": [{"LOT_ID": "L1"}, {"LOT_ID": "L2"}]},
        "intent_plan": {
            "reference_mode": "none",
            "retrieval_jobs": [
                {
                    "dataset_key": "history",
                    "source_alias": "history_src",
                    "trusted_catalog": True,
                    "required_params": {"lot_id": deepcopy(blank_value), "DATE": "20260701"},
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "entity_type": "lot",
                                "source_alias": "previous_result",
                                "source_column": "LOT_ID",
                                "target_param": "LOT_ID",
                                "operator": "in",
                            }
                        ]
                    },
                }
            ],
        },
    }
    normalized = binding_alias_normalizer.normalize_continuation_binding_aliases(payload)
    job = normalized["intent_plan"]["retrieval_jobs"][0]
    assert job["required_params"] == {"DATE": "20260701"}
    assert job["source_config"]["upstream_bindings"][0]["source_alias"] == "upstream_result"
    inspection = normalized["trace"]["inspection"]["continuation_binding_aliases"]
    assert inspection["removed_blank_required_param_count"] == 1
    bound = upstream_binder.bind_upstream_entity_parameters(normalized)
    bound_job = bound["intent_plan"]["retrieval_jobs"][0]
    assert bound["orchestration"]["binding_status"] == "ok"
    assert bound_job["required_params"] == {
        "DATE": "20260701",
        "LOT_ID": ["L1", "L2"],
    }


def test_continuation_alias_normalizer_preserves_nonblank_or_untrusted_params(
    binding_alias_normalizer,
):
    base_job = {
        "dataset_key": "history",
        "source_alias": "history_src",
        "trusted_catalog": True,
        "required_params": {"LOT_ID": "EXPLICIT"},
        "source_config": {
            "upstream_bindings": [
                {
                    "entity_type": "lot",
                    "source_alias": "previous_result",
                    "source_column": "LOT_ID",
                    "target_param": "LOT_ID",
                    "operator": "in",
                }
            ]
        },
    }
    payload = {
        "request": {"continuation": {"continuation_ref": "continuation:p:h"}},
        "orchestration": {"status": "ok", "upstream_result_ref": "result-1"},
        "intent_plan": {
            "retrieval_jobs": [base_job, {**deepcopy(base_job), "trusted_catalog": False}]
        },
    }
    normalized = binding_alias_normalizer.normalize_continuation_binding_aliases(payload)
    trusted, untrusted = normalized["intent_plan"]["retrieval_jobs"]
    assert trusted["required_params"] == {"LOT_ID": "EXPLICIT"}
    assert trusted["source_config"]["upstream_bindings"][0]["source_alias"] == "upstream_result"
    assert untrusted["required_params"] == {"LOT_ID": "EXPLICIT"}
    assert untrusted["source_config"]["upstream_bindings"][0]["source_alias"] == "previous_result"


def test_binding_alias_normalizer_restores_explicit_detail_projection_contract(
    binding_alias_normalizer,
):
    explicit_columns = ["LOT_ID", "PROD_QTY", "WF_QTY", "IN_TAT", "CUM_TAT"]
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "lot_status", "source_alias": "lot_status"}
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_current",
                    "operation": "apply_filters",
                    "source_alias": "lot_status",
                    "output_alias": "filtered_current",
                },
                {
                    "node_id": "select_requested",
                    "operation": "select_columns",
                    "inputs": [{"kind": "node_output", "ref": "filter_current"}],
                    "projection": explicit_columns,
                    "output_alias": "final_result",
                },
            ],
            "output_contract": {
                "result_mode": "detail",
                "required_columns": [
                    *explicit_columns,
                    "OPER_NAME",
                    "HOLD_STAT",
                    "HOLD_REASON",
                    "LOT_STAT",
                ],
                "result_columns": explicit_columns,
                "grain_columns": ["LOT_ID", "OPER_NAME"],
                "metric_columns": ["PROD_QTY", "WF_QTY", "IN_TAT", "CUM_TAT"],
                "strict_result_columns": True,
                "column_labels": {
                    "LOT_ID": "LOT ID",
                    "OPER_NAME": "공정명",
                    "PROD_QTY": "UNIT 수량",
                },
            },
        },
        "trace": {"inspection": {}},
    }
    normalized = binding_alias_normalizer.normalize_continuation_binding_aliases(payload)
    contract = normalized["intent_plan"]["output_contract"]
    assert contract["required_columns"] == explicit_columns
    assert contract["grain_columns"] == ["LOT_ID"]
    assert set(contract["column_labels"]) == {"LOT_ID", "PROD_QTY"}
    inspection = normalized["trace"]["inspection"]["continuation_binding_aliases"]
    assert inspection["restored_explicit_projection_contract_count"] == 1

    aggregate = deepcopy(payload)
    aggregate["intent_plan"]["output_contract"]["result_mode"] = "aggregate"
    untouched = binding_alias_normalizer.normalize_continuation_binding_aliases(aggregate)
    assert "OPER_NAME" in untouched["intent_plan"]["output_contract"]["required_columns"]


def test_binding_alias_normalizer_preserves_explicit_scalar_empty_grain(
    binding_alias_normalizer,
):
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production_today", "source_alias": "production_today"}
            ],
            "pandas_execution_plan": [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "production_today",
                    "group_by": [],
                    "aggregations": [
                        {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                    ],
                }
            ],
            "output_contract": {
                "result_mode": "scalar",
                "required_columns": ["PRODUCTION"],
                "result_columns": ["PRODUCTION"],
                "grain_columns": ["TECH", "DEN", "MODE"],
                "metric_columns": ["PRODUCTION"],
                "strict_result_columns": True,
            },
            "resolved_output_grain_plan": {
                "entity_grain_columns": ["TECH", "DEN", "MODE"],
                "breakdown_columns": [],
                "grain_columns": ["TECH", "DEN", "MODE"],
            },
        },
        "trace": {"inspection": {}},
    }
    normalized = binding_alias_normalizer.normalize_continuation_binding_aliases(payload)
    plan = normalized["intent_plan"]
    assert plan["output_contract"]["grain_columns"] == []
    assert plan["resolved_output_grain_plan"]["grain_columns"] == []
    assert plan["resolved_output_grain_plan"]["entity_grain_columns"] == []
    assert normalized["trace"]["inspection"]["continuation_binding_aliases"][
        "restored_scalar_grain_contract_count"
    ] == 1


def test_binding_alias_normalizer_restores_trusted_function_case_entity_grain(
    binding_alias_normalizer,
):
    product_grain = [
        "TECH",
        "DEN",
        "MODE",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "ORG",
        "LEAD",
        "MCP_NO",
    ]
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production",
                    "source_alias": "production_src",
                    "filter_mappings": {column: column for column in product_grain},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "match_product",
                    "operation": "apply_pandas_function_case",
                    "function_name": "match_product_tokens",
                    "source_alias": "production_src",
                    "output_alias": "matched_df",
                },
                {
                    "node_id": "aggregate_product",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "match_product"}],
                    "source_alias": "matched_df",
                    "group_by": [column for column in product_grain if column != "ORG"],
                    "aggregations": [
                        {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                    ],
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "required_columns": [
                    *[column for column in product_grain if column != "ORG"],
                    "PRODUCTION",
                ],
                "result_columns": [
                    *[column for column in product_grain if column != "ORG"],
                    "PRODUCTION",
                ],
                "grain_columns": [column for column in product_grain if column != "ORG"],
                "metric_columns": ["PRODUCTION"],
                "strict_result_columns": True,
            },
            "resolved_grain_plan": {
                "grain_columns": product_grain,
                "canonical_columns": product_grain,
                "column_mappings": [
                    {"canonical_key": column, "source_candidates": [column]}
                    for column in product_grain
                ],
            },
            "resolved_output_grain_plan": {
                "grain_columns": [column for column in product_grain if column != "ORG"]
            },
        },
        "trace": {"inspection": {}},
    }
    normalized = binding_alias_normalizer.normalize_continuation_binding_aliases(payload)
    plan = normalized["intent_plan"]
    aggregate_step = plan["pandas_execution_plan"][1]
    assert aggregate_step["group_by"][-1] == "ORG"
    assert plan["output_contract"]["grain_columns"][-1] == "ORG"
    assert "ORG" in plan["output_contract"]["required_columns"]
    assert "ORG" in plan["output_contract"]["result_columns"]
    inspection = normalized["trace"]["inspection"]["continuation_binding_aliases"]
    assert inspection["restored_function_case_grain_columns"] == ["ORG"]


def test_live_c02_shape_flattens_independent_stages_into_one_complex_plan(
    compiler,
    v2_intent_normalizer,
):
    product = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    stage1_required = [*product, "PRODUCTION"]
    final_required = [*product, "PRODUCTION", "EQP_COUNT", "EQP_LIST"]
    stage1_contract = {
        "required_columns": stage1_required,
        "result_columns": [],
    }
    stage2_contract = {
        "required_columns": final_required,
        "result_columns": [],
    }
    dependent = _live_dependent_stage(
        stage1_id="stage1_top_products",
        stage2_id="stage2_equipment_assignment",
        stage1_contract=stage1_contract,
        stage2_contract=stage2_contract,
        stage1_steps=[
            {
                "node_id": "step1_groupby",
                "operation": "groupby_and_aggregate",
                "source_alias": "prod_today",
                "group_by": product,
                "aggregations": [
                    {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                ],
            },
            {
                "node_id": "step2_top3",
                "operation": "sort_and_top_n",
                "source_alias": "step1_groupby",
                "sort_by": "PRODUCTION",
                "order": "desc",
                "limit": 3,
            },
        ],
        stage2_steps=[
            {
                "node_id": "step3_join",
                "operation": "join",
                "left_source_alias": "stage1_top_products",
                "right_source_alias": "eqp_assign",
                "join_type": "left",
            },
            {
                "node_id": "step4_groupby_eqp",
                "operation": "groupby_and_aggregate",
                "source_alias": "step3_join",
                "group_by": [*product, "PRODUCTION"],
                "aggregations": [
                    {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                    {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_LIST"},
                ],
            },
        ],
        stage1_job={
            "dataset_key": "production_today",
            "source_alias": "prod_today",
            "required_params": {"DATE": "20260701"},
        },
        stage2_job={"dataset_key": "equipment_assign", "source_alias": "eqp_assign"},
        handoff_columns=product,
        input_bindings=[
            {
                "source_stage_id": "stage1_top_products",
                "source_column": column,
                "target_source_alias": "eqp_assign",
                "target_param": column,
                "operator": "in",
            }
            for column in product
        ],
    )
    intent = {
        "retrieval_jobs": [
            dependent["stages"][0]["retrieval_jobs"][0],
            dependent["stages"][1]["retrieval_jobs"][0],
        ],
        "pandas_execution_plan": [],
        "output_contract": {
            "required_columns": final_required,
            "result_columns": [],
        },
        "dependent_retrieval_plan": dependent,
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "independent multi source"}},
        json.dumps({"intent_plan": intent}),
        _fresh_live_shape_catalogs(),
    )
    compiled = json.loads(response)["intent_plan"]
    assert trace["dependent_flattened"] is True
    assert "dependent_retrieval_plan" not in compiled
    assert [job["dataset_key"] for job in compiled["retrieval_jobs"]] == [
        "production_today",
        "equipment_assign",
    ]
    assert compiled["output_contract"]["result_columns"] == final_required
    join = next(step for step in compiled["pandas_execution_plan"] if step["operation"] == "join")
    assert join["left_source_alias"] == "step2_top3"
    assert join["right_source_alias"] == "eqp_assign"
    assert join["join_type"] == "left"
    normalized = v2_intent_normalizer.normalize_intent_plan(
        {"request": {"question": "independent multi source"}},
        response,
        _fresh_live_shape_catalogs(),
    )["intent_plan"]
    assert normalized.get("validation_errors", []) == []
    graph = normalized["resolved_execution_graph"]
    assert graph["validation_errors"] == []
    nodes = {item["node_id"]: item for item in graph["nodes"]}
    assert nodes["step3_join"]["inputs"] == [
        {"kind": "node_output", "ref": "step2_top3"},
        {"kind": "external_source", "ref": "eqp_assign"},
    ]
    assert nodes["step4_groupby_eqp"]["inputs"] == [
        {"kind": "node_output", "ref": "step3_join"}
    ]
    metric_sources = {
        item["output_column"]: item["source_alias"]
        for item in normalized["output_contract"]["metric_bindings"]
    }
    assert metric_sources["PRODUCTION"] == "prod_today"
    assert metric_sources["EQP_COUNT"] == "eqp_assign"
    assert metric_sources["EQP_LIST"] == "eqp_assign"


@pytest.mark.parametrize("placeholder_max_stages", [0, 1, 2, ""])
def test_compiler_removes_only_structurally_empty_dependent_placeholder(
    compiler,
    placeholder_max_stages,
):
    flat_plan = {
        "analysis_kind": "recipe_equipment_list",
        "retrieval_jobs": [
            {
                "dataset_key": "equipment_assign",
                "source_alias": "equipment_assign",
                "filters": {
                    "RECIPE_ID": {"operator": "starts_with", "value": "R0429"}
                },
            }
        ],
        "pandas_execution_plan": [
            {
                "operation": "select_columns",
                "source_alias": "equipment_assign",
                "projection": ["RECIPE_ID", "EQP_ID"],
            }
        ],
        "output_contract": {"result_columns": ["RECIPE_ID", "EQP_ID"]},
        "dependent_retrieval_plan": {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": placeholder_max_stages,
            "stages": [],
        },
    }
    compiled_text, trace = compiler.compile_intent_response(
        {"request": {"question": "R0429 할당 장비"}},
        json.dumps({"intent_plan": flat_plan}, ensure_ascii=False),
        {"table_catalog_items": []},
    )
    compiled = json.loads(compiled_text)["intent_plan"]
    assert "dependent_retrieval_plan" not in compiled
    assert compiled["retrieval_jobs"] == flat_plan["retrieval_jobs"]
    assert compiled["pandas_execution_plan"] == flat_plan["pandas_execution_plan"]
    assert trace == {
        "status": "passthrough",
        "dependent": False,
        "active_stage_index": 0,
        "empty_placeholder_removed": True,
    }


def test_compiler_ignores_exact_redundant_single_stage_wrapper(compiler):
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_status_src",
            "filters": {"HOLD_STAT": {"operator": "eq", "value": "OnHold"}},
            "required_params": {},
        }
    ]
    steps = [
        {
            "operation": "apply_filters",
            "source_alias": "lot_status_src",
            "inputs": [{"kind": "external_source", "ref": "lot_status_src"}],
        },
        {
            "operation": "select_columns",
            "inputs": [{"kind": "node_output", "ref": "lot_status_src"}],
            "projection": ["LOT_ID", "HOLD_STAT"],
        },
    ]
    contract = {
        "result_mode": "entity_list",
        "required_columns": ["LOT_ID", "HOLD_STAT"],
        "result_columns": ["LOT_ID", "HOLD_STAT"],
    }
    intent = {
        "retrieval_jobs": deepcopy(jobs),
        "pandas_execution_plan": deepcopy(steps),
        "output_contract": deepcopy(contract),
        "dependent_retrieval_plan": {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 2,
            "stages": [
                {
                    "stage_id": "stage1_hold_list",
                    "depends_on": [],
                    "retrieval_jobs": deepcopy(jobs),
                    "pandas_execution_plan": deepcopy(steps),
                    "output_contract": deepcopy(contract),
                    "handoff": {"columns": ["LOT_ID"]},
                    "input_bindings": [],
                }
            ],
        },
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "current entity list"}},
        json.dumps({"intent_plan": intent}),
        {
            "table_catalog_items": [
                {"key": "lot_status", "payload": {"columns": ["LOT_ID", "HOLD_STAT"]}}
            ]
        },
    )
    compiled = json.loads(response)["intent_plan"]
    assert "dependent_retrieval_plan" not in compiled
    assert compiled["pandas_execution_plan"] == steps
    assert trace["dependent_ignore_reason"] == "redundant_single_stage_dependent_wrapper"


def test_compiler_uses_complete_stage1_when_stage2_lacks_selection_and_request_evidence(compiler):
    stage1_contract = {
        "result_mode": "entity_list",
        "required_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
        "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
        "column_labels": {"HOLD_REASON": "HOLD 사유"},
    }
    stage2_contract = {
        "result_mode": "detail",
        "required_columns": [
            "LOT_ID",
            "OPER_NAME",
            "HOLD_STAT",
            "HOLD_REASON",
            "HOLD_TM",
            "HOLD_CD",
            "HOLD_DESC",
        ],
        "result_columns": [
            "LOT_ID",
            "OPER_NAME",
            "HOLD_STAT",
            "HOLD_REASON",
            "HOLD_TM",
            "HOLD_CD",
            "HOLD_DESC",
        ],
        "column_labels": {
            "HOLD_TM": "HOLD 시각",
            "HOLD_CD": "HOLD 코드",
            "HOLD_DESC": "HOLD 상세 사유",
        },
    }
    dependent = _live_dependent_stage(
        stage1_contract=stage1_contract,
        stage2_contract=stage2_contract,
        stage1_steps=[
            {
                "operation": "select_columns",
                "source_alias": "lot_status_src",
                "projection": stage1_contract["result_columns"],
            }
        ],
        stage2_steps=[
            {
                "operation": "select_extreme_row_per_group",
                "source_alias": "hold_history_src",
                "partition_by": ["LOT_ID"],
                "order_by": [{"column": "HOLD_TM", "direction": "desc"}],
                "tie_breakers": [],
                "tie_policy": "error",
                "limit_per_group": 1,
                "projection": ["LOT_ID", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
                "strict": True,
            }
        ],
        stage1_job={"dataset_key": "lot_status", "source_alias": "lot_status_src"},
        stage2_job={
            "dataset_key": "hold_history",
            "source_alias": "hold_history_src",
            "required_params": {"LOT_ID": ""},
        },
        handoff_columns=["LOT_ID"],
        input_bindings=[
            {
                "source_stage_id": "stage_1",
                "source_column": "LOT_ID",
                "target_source_alias": "hold_history_src",
                "target_param": "LOT_ID",
                "operator": "in",
            }
        ],
    )
    intent = {
        "metadata_refs": [
            {"section": "analysis_recipes", "key": "current_entity_selection"}
        ],
        "retrieval_jobs": [
            dependent["stages"][0]["retrieval_jobs"][0],
            dependent["stages"][1]["retrieval_jobs"][0],
        ],
        "pandas_execution_plan": [],
        "output_contract": deepcopy(stage2_contract),
        "dependent_retrieval_plan": dependent,
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "current entity list"}},
        json.dumps({"intent_plan": intent}),
        _fresh_live_shape_catalogs(),
    )
    compiled = json.loads(response)["intent_plan"]
    assert [job["dataset_key"] for job in compiled["retrieval_jobs"]] == ["lot_status"]
    assert compiled["output_contract"]["result_columns"] == stage1_contract["result_columns"]
    assert "dependent_retrieval_plan" not in compiled
    assert trace["dependent_ignore_reason"] == "dependent_stage2_not_selected_by_metadata_refs"
    assert trace["dependent_ignore_evidence"]["unselected_stage2_datasets"] == ["hold_history"]

    selected = deepcopy(intent)
    selected["metadata_refs"].extend(
        [
            {"section": "table_catalog_items", "key": "lot_status"},
            {"section": "table_catalog_items", "key": "hold_history"},
        ]
    )
    selected_response, selected_trace = compiler.compile_intent_response(
        {"request": {"question": "current entity list and latest history"}},
        json.dumps({"intent_plan": selected}),
        _fresh_live_shape_catalogs(),
    )
    selected_plan = json.loads(selected_response)["intent_plan"]
    assert selected_trace["status"] == "pending"
    assert selected_plan["dependent_retrieval_plan"]["runtime"]["status"] == "pending"


def test_compiler_rewrites_stage_metric_binding_when_flattening_independent_sources(compiler):
    product = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    stage1_contract = {
        "required_columns": [*product, "PRODUCTION"],
        "result_columns": [*product, "PRODUCTION"],
        "metric_bindings": [
            {
                "output_column": "PRODUCTION",
                "source_alias": "prod_today",
                "dataset_key": "production_today",
                "source_column": "PRODUCTION",
                "aggregation": "sum",
            }
        ],
    }
    final_columns = [*product, "PRODUCTION", "EQUIPMENT_COUNT", "EQUIPMENT_LIST"]
    stage2_contract = {
        "required_columns": final_columns,
        "result_columns": [],
        "metric_bindings": [
            {
                "output_column": "PRODUCTION",
                "source_alias": "stage1_top_products",
                "dataset_key": "production_today",
                "source_column": "PRODUCTION",
                "aggregation": "sum",
            },
            {
                "output_column": "EQUIPMENT_COUNT",
                "source_alias": "eqp_assign",
                "dataset_key": "equipment_assign",
                "source_column": "EQP_ID",
                "aggregation": "nunique",
            },
            {
                "output_column": "EQUIPMENT_LIST",
                "source_alias": "eqp_assign",
                "dataset_key": "equipment_assign",
                "source_column": "EQP_ID",
                "aggregation": "collect_unique",
            },
        ],
    }
    dependent = _live_dependent_stage(
        stage1_id="stage1_top_products",
        stage2_id="stage2_equipment_assignment",
        stage1_contract=stage1_contract,
        stage2_contract=stage2_contract,
        stage1_steps=[
            {
                "node_id": "aggregate_production",
                "operation": "groupby_and_aggregate",
                "source_alias": "prod_today",
                "group_by": product,
                "aggregations": [
                    {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                ],
            },
            {
                "node_id": "top_products",
                "operation": "sort_and_top_n",
                "source_alias": "aggregate_production",
                "sort_by": "PRODUCTION",
                "order": "desc",
                "limit": 3,
            },
        ],
        stage2_steps=[
            {
                "node_id": "join_equipment",
                "operation": "join",
                "left_source_alias": "stage1_top_products",
                "right_source_alias": "eqp_assign",
                "join_type": "left",
            },
            {
                "node_id": "summarize_equipment",
                "operation": "groupby_and_aggregate",
                "source_alias": "join_equipment",
                "group_by": [*product, "PRODUCTION"],
                "aggregations": [
                    {"column": "EQP_ID", "method": "nunique", "output_column": "EQUIPMENT_COUNT"},
                    {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQUIPMENT_LIST"},
                ],
            },
        ],
        stage1_job={
            "dataset_key": "production_today",
            "source_alias": "prod_today",
            "required_params": {"DATE": "20260701"},
        },
        stage2_job={"dataset_key": "equipment_assign", "source_alias": "eqp_assign"},
        handoff_columns=product,
        input_bindings=[
            {
                "source_stage_id": "stage1_top_products",
                "source_column": "MCP_NO",
                "target_source_alias": "eqp_assign",
                "target_param": "MCP_NO",
                "operator": "in",
            }
        ],
    )
    intent = {
        "retrieval_jobs": [],
        "pandas_execution_plan": [],
        "output_contract": deepcopy(stage2_contract),
        "dependent_retrieval_plan": dependent,
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "top products with assigned equipment"}},
        json.dumps({"intent_plan": intent}),
        _fresh_live_shape_catalogs(),
    )
    compiled = json.loads(response)["intent_plan"]
    assert trace["dependent_flattened"] is True
    metric_sources = {
        binding["output_column"]: binding["source_alias"]
        for binding in compiled["output_contract"]["metric_bindings"]
    }
    assert metric_sources == {
        "PRODUCTION": "prod_today",
        "EQUIPMENT_COUNT": "eqp_assign",
        "EQUIPMENT_LIST": "eqp_assign",
    }


def test_compiler_prunes_dead_flat_branch_from_terminal_lineage(compiler):
    intent = {
        "retrieval_jobs": [
            {"dataset_key": "equipment_assign", "source_alias": "eqp_assign"},
            {"dataset_key": "eqp_uph", "source_alias": "eqp_uph"},
        ],
        "pandas_execution_plan": [
            {
                "operation": "apply_filters",
                "inputs": [{"kind": "external_source", "ref": "eqp_assign"}],
                "source_alias": "eqp_assign",
                "output_alias": "filtered_assign",
            },
            {
                "operation": "apply_filters",
                "inputs": [{"kind": "external_source", "ref": "eqp_uph"}],
                "source_alias": "eqp_uph",
                "output_alias": "filtered_uph",
            },
            {
                "operation": "join",
                "inputs": [
                    {"kind": "node_output", "ref": "filtered_assign"},
                    {"kind": "node_output", "ref": "filtered_uph"},
                ],
                "left_source_alias": "filtered_assign",
                "right_source_alias": "filtered_uph",
            },
            {
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "node_output", "ref": "filtered_assign"}],
                "source_alias": "filtered_assign",
                "group_by": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "EQP_ID"],
                "aggregations": [],
            },
        ],
        "output_contract": {
            "result_mode": "detail",
            "required_columns": ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "result_columns": ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "metric_bindings": [],
        },
        "dependent_retrieval_plan": {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 2,
            "stages": [],
        },
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "equipment detail"}},
        json.dumps({"intent_plan": intent}),
        {"table_catalog_items": []},
    )
    compiled = json.loads(response)["intent_plan"]
    assert [job["source_alias"] for job in compiled["retrieval_jobs"]] == ["eqp_assign"]
    assert [step["operation"] for step in compiled["pandas_execution_plan"]] == [
        "apply_filters",
        "groupby_and_aggregate",
    ]
    assert trace["flat_reachability_pruned"] is True
    assert trace["pruned_step_count"] == 2
    assert trace["pruned_job_count"] == 1


def test_entity_existence_question_adds_trusted_positive_metric_predicate(
    compiler,
    executor,
):
    intent = {
        "metadata_refs": [
            {"section": "quantity_terms", "key": "production_quantity"},
            {"section": "table_catalog_items", "key": "production"},
        ],
        "retrieval_jobs": [
            {
                "dataset_key": "production",
                "source_alias": "production_data",
                "required_params": {"DATE": "20260630"},
                "filters": {"OPER_NAME": {"operator": "eq", "value": "FCB/H"}},
            }
        ],
        "pandas_execution_plan": [
            {
                "operation": "apply_filters",
                "inputs": [{"kind": "external_source", "ref": "production_data"}],
                "output_alias": "filtered_production",
            },
            {
                "operation": "distinct_values",
                "inputs": [{"kind": "node_output", "ref": "filtered_production"}],
                "group_by": ["DEVICE"],
            },
        ],
        "output_contract": {
            "result_mode": "entity_list",
            "required_columns": ["DEVICE"],
            "result_columns": ["DEVICE"],
            "grain_columns": ["DEVICE"],
            "metric_columns": [],
            "metric_bindings": [],
        },
        "dependent_retrieval_plan": {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 2,
            "stages": [],
        },
    }
    metadata = {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "production_quantity",
                "payload": {"column": "PRODUCTION", "aggregation_method": "sum"},
            }
        ],
        "table_catalog_items": [
            {
                "key": "production",
                "payload": {
                    "columns": ["DEVICE", "OPER_NAME", "PRODUCTION"],
                    "required_params": ["DATE"],
                },
            }
        ],
    }
    response, trace = compiler.compile_intent_response(
        {"request": {"question": "실적이 있는 Device 알려줘"}},
        json.dumps({"intent_plan": intent}, ensure_ascii=False),
        metadata,
    )
    compiled = json.loads(response)["intent_plan"]
    predicate = compiled["output_contract"]["existence_predicate"]
    assert predicate["source_column"] == "PRODUCTION"
    assert predicate["operator"] == "gt"
    assert compiled["retrieval_jobs"][0]["filters"]["PRODUCTION"] == {
        "operator": "gt",
        "value": 0,
    }
    assert trace["existence_predicate_applied"]["metadata_ref"] == {
        "section": "quantity_terms",
        "key": "production_quantity",
    }
    filter_plan = executor._pandas_filter_plan({"intent_plan": compiled})
    conditions = filter_plan[0]["conditions"]
    frame = pd.DataFrame(
        [
            {"DEVICE": "POSITIVE", "OPER_NAME": "FCB/H", "PRODUCTION": 5},
            {"DEVICE": "ZERO", "OPER_NAME": "FCB/H", "PRODUCTION": 0},
            {"DEVICE": "OTHER_PROCESS", "OPER_NAME": "FCB1", "PRODUCTION": 7},
        ]
    )
    resolved_conditions = [
        {
            **condition,
            "canonical_field": condition["field"],
            "typed_values": condition["values"],
        }
        for condition in conditions
    ]
    filtered, _ = executor._apply_fast_filters(frame, resolved_conditions, pd)
    assert filtered["DEVICE"].tolist() == ["POSITIVE"]

    no_existence_response, no_existence_trace = compiler.compile_intent_response(
        {"request": {"question": "Device 목록 알려줘"}},
        json.dumps({"intent_plan": intent}, ensure_ascii=False),
        metadata,
    )
    no_existence = json.loads(no_existence_response)["intent_plan"]
    assert "PRODUCTION" not in no_existence["retrieval_jobs"][0]["filters"]
    assert "existence_predicate_applied" not in no_existence_trace


def test_detail_comparison_reconciles_to_unique_current_day_catalog(compiler):
    columns = ["TECH", "DEN", "PKG_TYPE2", "MCP_NO", "MODE", "PKG_TYPE1", "LEAD"]
    intent = {
        "metadata_refs": [
            {"section": "table_catalog_items", "key": "production"}
        ],
        "retrieval_jobs": [
            {
                "dataset_key": "production",
                "source_alias": "prod_src",
                "required_params": {"DATE": "20260701"},
                "filters": {},
            }
        ],
        "pandas_execution_plan": [
            {
                "node_id": "find_duplicates",
                "operation": "find_duplicate_groups",
                "inputs": [{"kind": "external_source", "ref": "prod_src"}],
                "group_by": ["TECH", "DEN", "PKG_TYPE2", "MCP_NO"],
                "comparison_columns": ["MODE", "PKG_TYPE1", "LEAD"],
                "comparison_rule": "any",
            }
        ],
        "output_contract": {
            "result_mode": "detail",
            "required_columns": columns,
            "result_columns": columns,
            "grain_columns": ["TECH", "DEN", "PKG_TYPE2", "MCP_NO"],
            "metric_columns": [],
        },
        "dependent_retrieval_plan": {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 2,
            "stages": [],
        },
    }
    metadata = {
        "table_catalog_items": [
            {
                "key": "production",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {"time_scope": "history"},
                    "required_params": ["DATE"],
                    "columns": columns,
                },
            },
            {
                "key": "production_today",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {"time_scope": "current_day"},
                    "required_params": ["DATE"],
                    "columns": columns,
                },
            },
            {
                "key": "unrelated_current",
                "payload": {
                    "dataset_family": "other",
                    "selection_criteria": {"time_scope": "current_day"},
                    "required_params": ["DATE"],
                    "columns": columns,
                },
            },
        ]
    }
    response, trace = compiler.compile_intent_response(
        {
            "request": {
                "question": "current duplicate product attributes",
                "reference_date": "20260701",
            }
        },
        json.dumps({"intent_plan": intent}),
        metadata,
    )
    compiled = json.loads(response)["intent_plan"]
    assert compiled["retrieval_jobs"][0]["dataset_key"] == "production_today"
    assert compiled["retrieval_jobs"][0]["source_alias"] == "prod_src"
    assert compiled["metadata_refs"] == [
        {"section": "table_catalog_items", "key": "production_today"}
    ]
    reconciliation = trace["dataset_scope_reconciliation"]
    assert reconciliation["from_dataset_key"] == "production"
    assert reconciliation["to_dataset_key"] == "production_today"
    assert reconciliation["requested_time_scope"] == "current_day"

    ambiguous = deepcopy(metadata)
    ambiguous["table_catalog_items"].append(
        {
            "key": "production_current_copy",
            "payload": {
                "dataset_family": "production",
                "selection_criteria": {"time_scope": "current_day"},
                "required_params": ["DATE"],
                "columns": columns,
            },
        }
    )
    ambiguous_response, ambiguous_trace = compiler.compile_intent_response(
        {
            "request": {
                "question": "current duplicate product attributes",
                "reference_date": "20260701",
            }
        },
        json.dumps({"intent_plan": intent}),
        ambiguous,
    )
    ambiguous_plan = json.loads(ambiguous_response)["intent_plan"]
    assert ambiguous_plan["retrieval_jobs"][0]["dataset_key"] == "production"
    assert "dataset_scope_reconciliation" not in ambiguous_trace


@pytest.mark.parametrize(
    "dependent",
    [
        {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 2,
            "stages": [{"stage_id": "only_stage"}],
        },
        {
            "version": "analysis.dependent_retrieval.v1",
            "max_stages": 0,
            "stages": [],
            "activation": {"reason": "invented_dependency"},
        },
        ["not", "an", "object"],
    ],
)
def test_compiler_rejects_nonempty_or_malformed_dependent_placeholder(compiler, dependent):
    response = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"}
            ],
            "pandas_execution_plan": [],
            "output_contract": {"result_columns": ["EQP_ID"]},
            "dependent_retrieval_plan": dependent,
        }
    }
    with pytest.raises(ValueError):
        compiler.compile_intent_response(
            {"request": {"question": "장비"}},
            json.dumps(response, ensure_ascii=False),
            {"table_catalog_items": []},
        )


def test_non_strict_extreme_contract_is_blocked_without_pandas_model(executor):
    calls: list[str] = []
    payload = {
        "intent_plan": {
            "retrieval_jobs": [{"dataset_key": "history", "source_alias": "history"}],
            "pandas_execution_plan": [
                {
                    "operation": "select_extreme_row_per_group",
                    "inputs": [{"kind": "external_source", "ref": "history"}],
                    "partition_by": ["ENTITY_ID"],
                    "order_by": [{"column": "EVENT_TM", "direction": "desc"}],
                    "tie_breakers": [{"column": "EVENT_SEQ", "direction": "desc"}],
                    "limit_per_group": 1,
                    "tie_policy": "first",
                    "projection": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ"],
                    "strict": False,
                }
            ],
            "output_contract": {"result_columns": ["ENTITY_ID", "EVENT_TM", "EVENT_SEQ"]},
        },
        "simple_analysis_contract": {"route": "complex", "requires_pandas_llm": True},
        "runtime_sources": {"history": []},
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    result = executor.execute_hybrid_analysis(
        payload,
        "must not be sent",
        lambda prompt: calls.append(prompt) or "{}",
        "repair",
    )
    assert calls == []
    assert result["analysis"]["status"] == "error"
    assert result["trace"]["errors"][-1]["type"] == "strict_extreme_contract_invalid"


def _stored_resume_fixture(intent_router):
    dependent = {
        "version": intent_router.CONTRACT_VERSION,
        "max_stages": 2,
        "plan_id": "drp-test",
        "stages": [
            {
                "stage_id": "stage_1",
                "depends_on": [],
                "retrieval_jobs": [{"dataset_key": "index", "source_alias": "index"}],
                "pandas_execution_plan": [],
                "output_contract": {"result_columns": ["ENTITY_ID"]},
                "handoff": {"columns": ["ENTITY_ID"], "require_complete": True},
            },
            {
                "stage_id": "stage_2",
                "depends_on": ["stage_1"],
                "retrieval_jobs": [{"dataset_key": "detail", "source_alias": "detail"}],
                "pandas_execution_plan": [],
                "output_contract": {"result_columns": ["ENTITY_ID", "DETAIL"]},
                "input_bindings": [
                    {
                        "source_stage_id": "stage_1",
                        "source_column": "ENTITY_ID",
                        "target_source_alias": "detail",
                        "target_param": "ENTITY_ID",
                        "operator": "in",
                    }
                ],
            },
        ],
        "runtime": {
            "status": "pending",
            "active_stage_index": 0,
            "current_stage_id": "stage_1",
            "next_stage_id": "stage_2",
        },
    }
    dependent["plan_hash"] = intent_router._plan_hash(dependent)
    intent_plan = {"analysis_kind": "generic_detail", "dependent_retrieval_plan": dependent}
    continuation_ref = f"continuation:{dependent['plan_id']}:{dependent['plan_hash']}"
    contract = {
        "version": dependent["version"],
        "plan_id": dependent["plan_id"],
        "plan_hash": dependent["plan_hash"],
        "max_stages": 2,
        "current_stage_index": 0,
        "next_stage_index": 1,
        "continuation_ref": continuation_ref,
        "session_id": "session-1",
        "input_bindings": deepcopy(dependent["stages"][1]["input_bindings"]),
    }
    payload = {
        "request": {
            "session_id": "session-1",
            "continuation": {
                "continuation_ref": continuation_ref,
                "continuation_contract": contract,
            },
        },
        "orchestration": {"upstream_result_ref": "result-1"},
    }
    document = {
        "session_id": "session-1",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "payload": {
            "intent_plan": intent_plan,
            "metadata_refs": [],
            "analysis": {"status": "ok"},
            "data": {"row_count": 1},
            "storage_manifest": {
                "result_rows": {"original_count": 1, "stored_count": 1, "complete": True}
            },
        },
    }
    return payload, document, contract


def test_resume_loads_server_plan_and_never_calls_intent_model(intent_router):
    payload, document, _ = _stored_resume_fixture(intent_router)
    calls: list[str] = []
    response, trace = intent_router.route_intent_response(
        payload,
        "must not be sent",
        lambda prompt: calls.append(prompt) or "{}",
        stored_plan_loader=lambda ref: document,
    )
    assert calls == []
    assert trace["model_called"] is False
    assert trace["intent_llm_skipped"] is True
    assert json.loads(response)["intent_plan"] == document["payload"]["intent_plan"]


@pytest.mark.parametrize("mutation", ["expired", "incomplete", "binding"])
def test_resume_fails_closed_for_untrusted_stored_state(intent_router, mutation: str):
    payload, document, contract = _stored_resume_fixture(intent_router)
    if mutation == "expired":
        document["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif mutation == "incomplete":
        document["payload"]["storage_manifest"]["result_rows"]["complete"] = False
    else:
        contract["input_bindings"][0]["target_param"] = "INVENTED"
    with pytest.raises(ValueError):
        intent_router.route_intent_response(
            payload,
            "unused",
            lambda prompt: "{}",
            stored_plan_loader=lambda ref: document,
        )


def test_result_loader_rejects_expired_or_missing_ttl(result_loader):
    assert result_loader._continuation_expiry_error({})["type"] == "continuation_expiry_missing"
    expired = {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    assert result_loader._continuation_expiry_error(expired)["type"] == "continuation_result_expired"
    future = {"expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}
    assert result_loader._continuation_expiry_error(future) is None


def test_result_loader_requires_exact_complete_manifest(result_loader, intent_router):
    payload, document, _ = _stored_resume_fixture(intent_router)
    current_plan = deepcopy(document["payload"]["intent_plan"])
    current_runtime = current_plan["dependent_retrieval_plan"]["runtime"]
    current_runtime.update(
        {
            "status": "complete",
            "active_stage_index": 1,
            "current_stage_id": "stage_2",
            "next_stage_id": "",
        }
    )
    payload["intent_plan"] = current_plan
    assert result_loader._continuation_resume_error(payload, document["payload"]) is None

    incomplete = deepcopy(document["payload"])
    incomplete["storage_manifest"]["result_rows"].pop("complete")
    error = result_loader._continuation_resume_error(payload, incomplete)
    assert error["type"] == "continuation_upstream_incomplete"


def test_flow14_ingress_enforces_public_continuation_contract_size_before_model(
    request_loader,
    intent_router,
):
    def contract_at_size(size: int) -> dict[str, str]:
        empty_bytes = len(
            json.dumps(
                {"padding": ""},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return {"padding": "x" * (size - empty_bytes)}

    at_limit = contract_at_size(4096)
    over_limit = contract_at_size(4097)
    compact_at_limit = json.dumps(
        at_limit,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    compact_over_limit = json.dumps(
        over_limit,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert len(compact_at_limit.encode("utf-8")) == 4096
    assert len(compact_over_limit.encode("utf-8")) == 4097
    assert request_loader._parse_contract(at_limit) == at_limit
    assert request_loader._parse_contract(compact_at_limit) == at_limit

    for oversized in (over_limit, compact_over_limit):
        blocked = request_loader.build_request(
            "resume",
            session_id="session-1",
            upstream_result_ref="result-1",
            continuation_ref="continuation:p:h",
            continuation_contract=oversized,
        )
        assert blocked["analysis"]["status"] == "blocked"
        error = blocked["orchestration"]["error"]
        assert error["type"] == "continuation_contract_too_large"
        assert error["contract_bytes"] == 4097
        calls: list[str] = []
        response, trace = intent_router.route_intent_response(
            blocked,
            "must not be sent",
            lambda prompt: calls.append(prompt) or "{}",
        )
        assert calls == []
        assert trace["mode"] == "request_blocked"
        assert trace["model_called"] is False
        assert json.loads(response)["intent_plan"]["retrieval_jobs"] == []


def test_flow14_ingress_blocks_invalid_contract_and_leaves_normal_question_unchanged(
    request_loader,
    intent_router,
):
    for invalid in ('{"broken":', ["not", "an", "object"]):
        blocked = request_loader.build_request(
            "resume",
            session_id="session-1",
            upstream_result_ref="result-1",
            continuation_ref="continuation:p:h",
            continuation_contract=invalid,
        )
        assert blocked["analysis"]["status"] == "blocked"
        assert blocked["trace"]["errors"][0]["type"] in {
            "continuation_contract_invalid_json",
            "continuation_contract_invalid_type",
        }

    normal = request_loader.build_request("ordinary question", session_id="session-1")
    assert normal["analysis"] == {}
    assert normal["trace"]["errors"] == []
    assert "continuation" not in normal["request"]
    calls: list[str] = []
    response, trace = intent_router.route_intent_response(
        normal,
        "ordinary prompt",
        lambda prompt: calls.append(prompt) or '{"intent_plan":{}}',
    )
    assert calls == ["ordinary prompt"]
    assert trace["model_called"] is True
    assert json.loads(response) == {"intent_plan": {}}


def test_public_continuation_contract_is_compact_and_contains_no_full_ir():
    api = _module("_continuation_api_tests", "22_continuation_api_response_builder.py")
    payload = {
        "request": {"session_id": "session-1"},
        "intent_plan": {
            "dependent_retrieval_plan": {
                "version": "analysis.dependent_retrieval.v1",
                "plan_id": "drp-test",
                "plan_hash": "abc123",
                "max_stages": 2,
                "runtime": {
                    "status": "pending",
                    "active_stage_index": 0,
                    "current_stage_id": "stage_1",
                    "next_stage_id": "stage_2",
                },
                "stages": [
                    {"stage_id": "stage_1"},
                    {
                        "stage_id": "stage_2",
                        "input_bindings": [
                            {"source_column": "ENTITY_ID", "target_param": "ENTITY_ID", "operator": "in"}
                        ],
                    },
                ],
            }
        },
        "analysis": {"status": "ok"},
        "data": {"row_count": 1, "data_ref": "result-1"},
        "trace": {"inspection": {"result_store": {"status": "ok"}}},
    }
    continuation = api._build_continuation(payload)
    contract = continuation["continuation_contract"]
    assert continuation["status"] == "pending"
    assert continuation["stage_index"] == 1
    assert continuation["current_stage_index"] == 0
    assert continuation["result_ref"] == "result-1"
    assert "intent_envelope" not in contract
    assert len(json.dumps(contract, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 4096
    response = api.build_api_response(payload, "1차 결과")
    public_dependent = response["intent_plan"]["dependent_retrieval_plan"]
    assert public_dependent == {
        "version": "analysis.dependent_retrieval.v1",
        "plan_id": "drp-test",
        "plan_hash": "abc123",
        "max_stages": 2,
        "status": "pending",
        "active_stage_id": "stage_1",
        "active_stage_index": 0,
        "next_stage_id": "stage_2",
        "intent_llm_skipped": False,
    }
    public_serialized = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    assert '"stages"' not in public_serialized
    assert '"pandas_execution_plan"' not in public_serialized
    assert '"input_bindings"' in public_serialized  # compact top-level continuation contract only
    assert len(
        json.dumps(response["intent_plan"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ) < len(
        json.dumps(payload["intent_plan"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    payload["intent_plan"]["dependent_retrieval_plan"]["runtime"] = {
        "status": "complete",
        "active_stage_index": 1,
        "current_stage_id": "stage_2",
        "next_stage_id": "",
        "intent_llm_skipped": True,
    }
    final_continuation = api._build_continuation(payload)
    assert final_continuation["status"] == "complete"
    assert final_continuation["stage_index"] == 2
    assert final_continuation["current_stage_index"] == 1


@pytest.mark.parametrize(
    ("mutation", "error_type"),
    [
        ("missing_session", "continuation_session_missing"),
        ("missing_result_ref", "continuation_result_ref_missing"),
        ("store_failed", "continuation_result_store_unavailable"),
    ],
)
def test_pending_continuation_requires_reusable_stored_result(mutation: str, error_type: str):
    api = _module(f"_continuation_api_fail_closed_{mutation}", "22_continuation_api_response_builder.py")
    payload = {
        "request": {"session_id": "session-1"},
        "intent_plan": {
            "dependent_retrieval_plan": {
                "version": "analysis.dependent_retrieval.v1",
                "plan_id": "drp-test",
                "plan_hash": "abc123",
                "max_stages": 2,
                "runtime": {
                    "status": "pending",
                    "active_stage_index": 0,
                    "current_stage_id": "stage_1",
                    "next_stage_id": "stage_2",
                },
                "stages": [
                    {"stage_id": "stage_1"},
                    {
                        "stage_id": "stage_2",
                        "input_bindings": [
                            {"source_column": "ENTITY_ID", "target_param": "ENTITY_ID", "operator": "in"}
                        ],
                    },
                ],
            }
        },
        "analysis": {"status": "ok"},
        "data": {"row_count": 1, "data_ref": "result-1"},
        "trace": {"inspection": {"result_store": {"status": "ok"}}},
    }
    if mutation == "missing_session":
        payload["request"]["session_id"] = ""
    elif mutation == "missing_result_ref":
        payload["data"].pop("data_ref")
    else:
        payload["trace"]["inspection"]["result_store"]["status"] = "error"
    continuation = api._build_continuation(payload)
    assert continuation["status"] == "followup_unavailable"
    assert continuation["error"]["type"] == error_type
    assert "continuation_ref" not in continuation
    assert "continuation_contract" not in continuation
    assert "input_bindings" not in continuation
    response = api.build_api_response(payload, "다음 조회 단계를 준비했습니다.")
    assert response["status"] == "partial"
    assert response["stage_status"]["continuation"] == "followup_unavailable"
    assert "후속 조회를 실행하지 않습니다" in response["message"]


def test_answer_builder_does_not_announce_unresumable_pending_stage():
    answer = _module(
        "_continuation_answer_unavailable",
        "20_continuation_hybrid_answer_builder.py",
    )
    calls: list[str] = []
    payload = {
        "request": {"session_id": "session-1"},
        "intent_plan": {
            "dependent_retrieval_plan": {
                "runtime": {"status": "pending", "active_stage_index": 0},
            }
        },
        "analysis": {"status": "ok", "execution_route": "complex"},
        "data": {"row_count": 1, "rows": [{"ENTITY_ID": "A"}]},
        "trace": {"inspection": {"result_store": {"status": "skipped"}}},
        "simple_analysis_contract": {"route": "complex"},
    }
    result = answer.build_hybrid_answer_response(
        payload,
        "must not be used",
        model_invoker=lambda prompt: calls.append(prompt) or "wrong",
        use_llm_answer=True,
    )
    assert calls == []
    assert result["analysis"]["continuation_status"] == "followup_unavailable"
    assert "후속 조회를 실행하지 않습니다" in result["answer_message"]
    inspection = result["trace"]["inspection"]["answer_model_response"]
    assert inspection["policy"] == "followup_unavailable_without_model"
    assert inspection["model_called"] is False


def test_export_uses_name_14_and_shared_result_store_settings():
    flow = json.loads(
        (ROOT / "flow_exports" / "08_data_analysis_flow_v2_continuation_standalone.json").read_text(
            encoding="utf-8-sig"
        )
    )
    assert flow["name"] == "08. v5_data_analysis_continuation"
    nodes = {node["id"]: node for node in flow["data"]["nodes"]}
    assert "CustomComponent-v2ContinuationCatalogClosure" in nodes
    intent_template = nodes["LanguageModel-intent"]["data"]["node"]["template"]
    loader_template = nodes["CustomComponent-O8vfz"]["data"]["node"]["template"]
    assert [intent_template[key]["value"] for key in ("mongo_uri", "mongo_database", "collection_name")] == [
        loader_template[key]["value"] for key in ("mongo_uri", "mongo_database", "collection_name")
    ]
    edge_pairs = {(edge.get("source"), edge.get("target")) for edge in flow["data"]["edges"]}
    assert (
        "CustomComponent-DXrpf",
        "CustomComponent-v2ContinuationCatalogClosure",
    ) in edge_pairs
    assert (
        "MongoDBDomainMetadataLoader-OM3Hg",
        "CustomComponent-v2ContinuationCatalogClosure",
    ) in edge_pairs
    assert {
        target
        for source, target in edge_pairs
        if source == "CustomComponent-v2ContinuationCatalogClosure"
    } == {
        "CustomComponent-B1hbh",
        "CustomComponent-5o0CN",
        "CustomComponent-v2ContinuationCompiler",
    }


def test_canonical_r09_fixture_uses_exact_question_and_excludes_decoys():
    from tools import validate_data_analysis_v2_continuation as validation

    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "tools" / "validate_data_analysis_v2_continuation.py"),
            "--reference-date",
            "20260701",
            "--ids",
            "R09",
            "--skip-export-check",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["status"] == "ok", report["results"][0].get("errors")
    result = report["results"][0]
    assert result["question"] == validation._canonical_v2_questions()[9]
    assert result["row_count"] == 1


def test_flow14_helper_matches_x_number_to_numeric_org_and_excludes_decoy():
    builder = _repo_module(
        "_continuation_flow_builder_helper_tests",
        "tools/build_data_analysis_flow_v2_continuation.py",
    )
    helper_source = builder._continuation_helper_library()
    assert "return _match(['ORG'], token[1:], 'exact')" in helper_source
    assert "return _match(['ORG'], token, 'exact')" not in helper_source
    namespace: dict[str, object] = {}
    exec(helper_source, namespace)
    frame = pd.DataFrame(
        [
            {
                "TECH": "SP",
                "DEN": "24G",
                "MODE": "GDDR7",
                "ORG": "32",
                "LEAD": "226",
                "PKG1": "FCBGA",
                "PKG2": "DDP",
                "DEVICE": "CORRECT",
            },
            {
                "TECH": "SP",
                "DEN": "24G",
                "MODE": "GDDR7",
                "ORG": "16",
                "LEAD": "226",
                "PKG1": "FCBGA",
                "PKG2": "DDP",
                "DEVICE": "ORG_DECOY",
            },
        ]
    )
    result = namespace["match_product_tokens"](
        "SP 24G GDDR7 X32 226 FCBGA DDP",
        frame,
    )
    assert result["DEVICE"].tolist() == ["CORRECT"]


def test_executor_removes_generated_shadow_of_selected_trusted_helper(executor):
    selector = _repo_module(
        "_continuation_selected_helper_shadow_tests",
        "langflow_components/data_analysis_flow/15a_selected_helper_code_builder.py",
    )
    helper_library = (
        ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"
    ).read_text(encoding="utf-8")
    selected_helper = selector.build_selected_helper_code(
        {"available_helpers": [{"function_name": "match_product_tokens"}]},
        helper_library,
    )
    assert "token[1:]" in selected_helper
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production"}
            ],
            "pandas_execution_plan": [
                {
                    "operation": "apply_pandas_function_case",
                    "function_name": "match_product_tokens",
                    "source_alias": "production",
                    "input_text": "SP 24G GDDR7 X32 226 FCBGA DDP",
                }
            ],
            "output_contract": {
                "result_mode": "entity_list",
                "required_columns": ["DEVICE"],
                "result_columns": ["DEVICE"],
                "strict_result_columns": True,
            },
        },
        "runtime_sources": {
            "production": [
                {
                    "TECH": "SP",
                    "DEN": "24G",
                    "MODE": "GDDR7",
                    "ORG": "32",
                    "LEAD": "226",
                    "PKG1": "FCBGA",
                    "PKG2": "DDP",
                    "DEVICE": "CORRECT",
                },
                {
                    "TECH": "SP",
                    "DEN": "24G",
                    "MODE": "GDDR7",
                    "ORG": "16",
                    "LEAD": "226",
                    "PKG1": "FCBGA",
                    "PKG2": "DDP",
                    "DEVICE": "ORG_DECOY",
                },
            ]
        },
    }
    generated = {
        "code": """
def match_product_tokens(input_text, frame, token_columns=None, output_order=None):
    token = 'X32'
    return frame[frame['ORG'].astype(str) == token]

df = sources['production'].copy()
matched = match_product_tokens('SP 24G GDDR7 X32 226 FCBGA DDP', df)
result = matched[['DEVICE']].copy()
"""
    }
    result = executor.execute_pandas_code(
        payload,
        generated,
        function_case_helper_code=selected_helper,
    )
    assert result["analysis"]["status"] == "ok", result.get("trace", {}).get("errors")
    assert result["data"]["rows"] == [{"DEVICE": "CORRECT"}]
    override = result["trace"]["inspection"]["pandas_execution"][
        "safe_import_normalization"
    ]["trusted_helper_override"]
    assert override["removed_generated_definitions"] == ["match_product_tokens"]


def test_executor_restores_unique_function_case_grain_from_matched_intermediate(
    executor,
    binding_alias_normalizer,
):
    selector = _repo_module(
        "_continuation_selected_helper_grain_tests",
        "langflow_components/data_analysis_flow/15a_selected_helper_code_builder.py",
    )
    helper_library = (
        ROOT / "langflow_components" / "data_analysis_flow" / "function_case_helper_code_input_example.py"
    ).read_text(encoding="utf-8")
    selected_helper = selector.build_selected_helper_code(
        {"available_helpers": [{"function_name": "match_product_tokens"}]},
        helper_library,
    )
    product_grain = [
        "TECH",
        "DEN",
        "MODE",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "ORG",
        "LEAD",
        "MCP_NO",
    ]
    llm_grain = [column for column in product_grain if column != "ORG"]
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production",
                    "source_alias": "production_src",
                    "filter_mappings": {column: column for column in product_grain},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "match_product",
                    "operation": "apply_pandas_function_case",
                    "function_name": "match_product_tokens",
                    "input_text": "SP 24G GDDR7 X32 226 FCBGA DDP",
                    "source_alias": "production_src",
                    "output_alias": "matched_df",
                },
                {
                    "node_id": "aggregate_product",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "match_product"}],
                    "source_alias": "matched_df",
                    "group_by": llm_grain,
                    "aggregations": [
                        {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                    ],
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "required_columns": [*llm_grain, "PRODUCTION"],
                "result_columns": [*llm_grain, "PRODUCTION"],
                "grain_columns": llm_grain,
                "metric_columns": ["PRODUCTION"],
                "strict_result_columns": True,
            },
            "resolved_grain_plan": {
                "grain_columns": product_grain,
                "canonical_columns": product_grain,
                "column_mappings": [
                    {"canonical_key": column, "source_candidates": [column]}
                    for column in product_grain
                ],
            },
        },
        "runtime_sources": {
            "production_src": [
                {
                    "TECH": "SP",
                    "DEN": "24G",
                    "MODE": "GDDR7",
                    "ORG": "32",
                    "LEAD": "226",
                    "PKG_TYPE1": "FCBGA",
                    "PKG_TYPE2": "DDP",
                    "MCP_NO": "",
                    "PRODUCTION": 424,
                },
                {
                    "TECH": "SP",
                    "DEN": "24G",
                    "MODE": "GDDR7",
                    "ORG": "16",
                    "LEAD": "226",
                    "PKG_TYPE1": "FCBGA",
                    "PKG_TYPE2": "DDP",
                    "MCP_NO": "",
                    "PRODUCTION": 999,
                },
            ]
        },
        "trace": {"inspection": {}},
    }
    normalized = binding_alias_normalizer.normalize_continuation_binding_aliases(payload)
    generated = {
        "code": f"""
src_df = sources['production_src']
matched_df = match_product_tokens('SP 24G GDDR7 X32 226 FCBGA DDP', src_df)
group_cols = {llm_grain!r}
result = matched_df.groupby(group_cols, dropna=False)['PRODUCTION'].sum().reset_index()
"""
    }
    executed = executor.execute_pandas_code(
        normalized,
        generated,
        function_case_helper_code=selected_helper,
    )
    assert executed["analysis"]["status"] == "ok", executed.get("trace", {}).get("errors")
    assert executed["data"]["rows"] == [
        {
            "TECH": "SP",
            "DEN": "24G",
            "MODE": "GDDR7",
            "PKG_TYPE1": "FCBGA",
            "PKG_TYPE2": "DDP",
            "LEAD": "226",
            "MCP_NO": "",
            "ORG": "32",
            "PRODUCTION": 424,
        }
    ]
    enrichment = executed["trace"]["inspection"]["pandas_execution"][
        "function_case_grain_enrichment"
    ]
    assert enrichment["status"] == "restored"
    assert enrichment["restored_columns"] == ["ORG"]


def test_live_c12_fixture_uses_equipment_assign_and_prefix_decoys():
    from tools import validate_data_analysis_v2_continuation as validation

    spec = {"id": "C12"}
    retrieved = {
        "source_results": [
            {
                "dataset_key": "equipment_assign",
                "source_alias": "equipment_assign_src",
                "rows": [],
            }
        ]
    }
    augmented = validation._augment_live_fixture_retrieval(spec, retrieved, "20260701")
    rows = augmented["source_results"][0]["rows"]
    matched = [row for row in rows if str(row.get("RECIPE_ID") or "").startswith("R0429")]
    assert {(row["RECIPE_ID"], row["EQP_ID"]) for row in matched} == {
        ("R0429-A", "EQP-01"),
        ("R0429-B", "EQP-02"),
    }
    assert any(row["RECIPE_ID"] == "R0428-DECOY" for row in rows)
    assert any(row["RECIPE_ID"] == "XR0429-DECOY" for row in rows)
    assert validation._live_expected_datasets({"id": "C12", "base_case": None})[0] == {
        "equipment_assign"
    }
