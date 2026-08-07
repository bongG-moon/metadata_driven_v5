from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pandas as pd

from test_langflow_components import ROOT, load_module
from tools.build_data_analysis_flow_v2 import build_flow
from tools.build_v2_lazy_prompt_components import render_sources


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"
V2_VALIDATION_QUESTIONS = ROOT / "validation_questions_v2.txt"


def _modules():
    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    answer = load_module(V2_ROOT / "20_hybrid_answer_builder.py")
    return resolver, executor, answer


def _prompt_modules():
    common_root = ROOT / "langflow_components" / "data_analysis_flow"
    return (
        load_module(common_root / "15_pandas_variables_builder.py"),
        load_module(common_root / "18_answer_variables_builder.py"),
    )


def test_v2_lazy_prompt_component_sources_are_generated_and_in_sync():
    for path, expected in render_sources().items():
        assert path.read_text(encoding="utf-8") == expected


def _single_source_payload(
    *,
    rows: list[dict],
    steps: list[dict],
    output_contract: dict,
    filters: dict | None = None,
    filter_mappings: dict | None = None,
    source_alias: str = "source_1",
    dataset_key: str = "dataset_1",
) -> dict:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    job = {
        "source_alias": source_alias,
        "dataset_key": dataset_key,
        "filters": deepcopy(filters or {}),
        "filter_mappings": deepcopy(filter_mappings or {}),
        "standard_column_aliases": {},
    }
    return {
        "question": "generic analysis request",
        "intent_plan": {
            "retrieval_jobs": [job],
            "pandas_execution_plan": deepcopy(steps),
            "output_contract": deepcopy(output_contract),
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": source_alias,
                        "dataset_key": dataset_key,
                        "provider": "retrieval_job",
                        "required": True,
                    }
                ]
            },
        },
        "runtime_sources": {source_alias: deepcopy(rows)},
        "source_results": [
            {
                "source_alias": source_alias,
                "dataset_key": dataset_key,
                "status": "ok",
                "row_count": len(rows),
                "columns": columns,
            }
        ],
        "trace": {"inspection": {}},
    }


def _resolve_and_execute(payload: dict):
    resolver, executor, _ = _modules()
    resolved = resolver.resolve_simple_analysis_contract(payload)
    model_calls: list[str] = []

    def invoke(prompt: str):
        model_calls.append(prompt)
        return '{"code":"result = pd.DataFrame()"}'

    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused on fast route",
        model_invoker=invoke,
        repair_prompt_template="repair",
    )
    return resolved, executed, model_calls


def test_v2_flow_is_isolated_and_removes_always_on_pandas_and_answer_models():
    donor = json.loads((ROOT / "flow_exports" / "data_analysis_flow_v5_standalone.json").read_text(encoding="utf-8"))
    flow = build_flow()

    assert donor["id"] != flow["id"]
    assert donor["endpoint_name"] != flow["endpoint_name"]
    assert flow["name"] == "13. data_analysis_flow_v2"
    assert flow["last_tested_version"] == "1.9.2"
    assert len(flow["data"]["nodes"]) == 54

    node_ids = {node["id"] for node in flow["data"]["nodes"]}
    node_index = {node["id"]: node for node in flow["data"]["nodes"]}
    assert "CustomComponent-v2FastResolver" in node_ids
    assert "LanguageModel-intent" in node_ids
    assert "LanguageModel-pandas" not in node_ids
    assert "LanguageModel-answer" not in node_ids
    assert "Prompt Template-ELVKc" not in node_ids
    assert all(node["data"]["node"].get("lf_version") == "1.9.2" for node in flow["data"]["nodes"])
    assert node_index["CustomComponent-A5y0b"]["data"]["node"]["template"]["code"]["value"] == (
        V2_ROOT / "21_v2_answer_message_adapter.py"
    ).read_text(encoding="utf-8")

    edge_keys = {
        (
            edge["source"],
            edge["data"]["sourceHandle"]["name"],
            edge["target"],
            edge["data"]["targetHandle"]["fieldName"],
        )
        for edge in flow["data"]["edges"]
    }
    assert ("CustomComponent-v5ExecutionGate", "payload_out", "CustomComponent-v2FastResolver", "payload") in edge_keys
    assert ("CustomComponent-v2FastResolver", "payload_out", "Prompt Template-xtzD5", "payload") in edge_keys
    assert ("CustomComponent-v5Helper", "selected_helper_code", "Prompt Template-xtzD5", "function_case_helper_code") in edge_keys
    assert ("Prompt Template-xtzD5", "pandas_prompt", "CustomComponent-s3mf1", "pandas_prompt") in edge_keys
    assert ("TextInput-VFbHh", "text", "CustomComponent-aKrkH", "domain_answer_guidance") in edge_keys
    assert ("CustomComponent-aKrkH", "answer_prompt", "CustomComponent-BVItv", "answer_prompt") in edge_keys
    assert node_index["Prompt Template-xtzD5"]["data"]["node"]["display_name"] == "16 V2 경로 인식 pandas Prompt 생성기"
    assert node_index["CustomComponent-aKrkH"]["data"]["node"]["display_name"] == "18 V2 경로 인식 Answer Prompt 생성기"


def test_fast_route_skips_full_pandas_prompt_serialization(monkeypatch):
    pandas_prompt, _ = _prompt_modules()
    payload = {"simple_analysis_contract": {"route": "fast"}}

    def fail_if_called(_payload):
        raise AssertionError("Fast route must not build pandas prompt variables")

    monkeypatch.setattr(pandas_prompt, "build_variables", fail_if_called)
    assert pandas_prompt.build_route_aware_pandas_prompt(payload, "{intent_plan_json}", "helper") == ""


def test_complex_route_preserves_existing_pandas_prompt_content():
    pandas_prompt, _ = _prompt_modules()
    template = (ROOT / "langflow_components" / "data_analysis_flow" / "16_pandas_prompt_template_ko.md").read_text(encoding="utf-8")
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}],
        steps=[{"operation": "custom_complex_operation", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["GROUP", "QTY"]},
    )
    payload["simple_analysis_contract"] = {"route": "complex"}
    variables = pandas_prompt.build_variables(payload)
    expected = template.format(**variables, function_case_helper_code="helper-code")
    actual = pandas_prompt.build_route_aware_pandas_prompt(payload, template, "helper-code")
    assert actual == expected


def test_fast_route_skips_answer_context_and_complex_context_has_single_owners(monkeypatch):
    _, answer_prompt = _prompt_modules()
    fast_payload = {
        "simple_analysis_contract": {"route": "fast"},
        "analysis": {"execution_route": "fast"},
    }

    original_builder = answer_prompt.build_v2_variables

    def fail_if_called(_payload):
        raise AssertionError("Fast route must not build answer prompt variables")

    monkeypatch.setattr(answer_prompt, "build_v2_variables", fail_if_called)
    assert answer_prompt.build_route_aware_answer_prompt(fast_payload, "{answer_context_json}") == ""
    monkeypatch.setattr(answer_prompt, "build_v2_variables", original_builder)

    complex_payload = {
        "request": {"question": "그룹별 수량을 알려줘"},
        "simple_analysis_contract": {"route": "complex"},
        "intent_plan": {
            "analysis_kind": "group_quantity",
            "retrieval_jobs": [
                {
                    "dataset_key": "sample",
                    "source_alias": "sample_source",
                    "required_params": {"DATE": "20260803"},
                    "filters": {"GROUP": {"operator": "eq", "value": "A"}},
                }
            ],
            "pandas_execution_plan": [
                {"operation": "groupby_and_aggregate", "group_by": ["GROUP"]}
            ],
            "output_contract": {
                "primary_metric": "QTY_SUM",
                "column_labels": {"GROUP": "그룹", "QTY_SUM": "수량"},
            },
        },
        "source_results": [
            {"dataset_key": "sample", "source_alias": "sample_source", "status": "ok", "row_count": 1}
        ],
        "analysis": {
            "status": "ok",
            "execution_route": "complex",
            "row_count": 1,
            "columns": ["GROUP", "QTY_SUM"],
        },
        "data": {
            "columns": ["GROUP", "QTY_SUM"],
            "rows": [{"GROUP": "A", "QTY_SUM": 10}],
            "row_count": 1,
        },
        "trace": {"warnings": [], "errors": [], "inspection": {"pandas_execution": {"status": "ok", "row_count": 1, "columns": ["GROUP", "QTY_SUM"]}}},
    }
    variables = answer_prompt.build_v2_variables(complex_payload)
    applied_scope = json.loads(variables["applied_scope_json"])
    answer_context = json.loads(variables["answer_context_json"])
    assert applied_scope["criteria"]["required_params"]["sample_source"] == {"DATE": "20260803"}
    assert "row_count" not in applied_scope["pandas_execution"]
    assert "columns" not in applied_scope["pandas_execution"]
    assert "result_shape" not in answer_context
    assert "applied_criteria" not in answer_context

    template = (ROOT / "langflow_components" / "data_analysis_flow" / "19_answer_prompt_template_ko.md").read_text(encoding="utf-8")
    rendered = answer_prompt.build_route_aware_answer_prompt(complex_payload, template, "guidance")
    assert "applied_scope_json.criteria" in rendered
    assert "result_summary_json.columns" in rendered
    assert "answer_context_json.applied_criteria" not in rendered
    assert "answer_context_json.result_shape.columns" not in rendered


def test_v2_canvas_documents_all_fast_recipes_without_execution_edges():
    flow = build_flow()
    assert flow["data"]["viewport"] == {"x": 330.0, "y": 250.0, "zoom": 0.12}
    notes = [node for node in flow["data"]["nodes"] if node["type"] == "noteNode"]
    assert len(notes) == 10
    note_text = "\n".join(node["data"]["node"]["description"] for node in notes)
    recipes = {
        "detail_query",
        "scalar_summary",
        "group_summary",
        "ranked_summary",
        "frequency_summary",
        "distinct_summary",
        "list_summary",
        "existence_summary",
        "quality_summary",
        "latest_earliest",
        "percent_of_total",
        "rank_within_group",
        "threshold_after_aggregate",
        "time_bucket_summary",
        "period_change",
        "running_total",
        "moving_aggregate",
        "percentile_summary",
        "pivot_summary",
    }
    assert all(recipe in note_text for recipe in recipes)
    note_ids = {node["id"] for node in notes}
    assert not any(edge["source"] in note_ids or edge["target"] in note_ids for edge in flow["data"]["edges"])

    boxes = []
    for node in flow["data"]["nodes"]:
        position = node["position"]
        boxes.append(
            (
                node["id"],
                position["x"],
                position["y"],
                node.get("width") or 360,
                node.get("height") or 360,
            )
        )
    for index, left in enumerate(boxes):
        for right in boxes[index + 1 :]:
            overlaps = (
                left[1] < right[1] + right[3]
                and left[1] + left[3] > right[1]
                and left[2] < right[2] + right[4]
                and left[2] + left[4] > right[2]
            )
            assert not overlaps, f"canvas overlap: {left[0]} / {right[0]}"


def test_v2_validation_question_set_covers_routes_recipes_and_followups():
    text = V2_VALIDATION_QUESTIONS.read_text(encoding="utf-8")
    representative_routes = re.findall(r"^\d+\. \[(FAST|COMPLEX) \|", text, flags=re.MULTILINE)
    assert len(representative_routes) == 31
    assert set(representative_routes) == {"FAST", "COMPLEX"}

    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    assert all(recipe in text for recipe in resolver.SUPPORTED_RECIPES)
    assert "후속 질문 판정과 이전 상태 복원은 Fast/Complex 분기보다 먼저 수행된다." in text
    assert "후속 질문도 매 turn의 최종 실행 계약에 따라 FAST 또는 COMPLEX가 될 수 있다." in text
    assert "BLOCKED는 정상 분류가 아니다." in text


def test_ranked_summary_uses_catalog_mapping_and_calls_no_analysis_model():
    payload = _single_source_payload(
        rows=[
            {"OPER_NAME": "A", "QTY": 10},
            {"OPER_NAME": "B", "QTY": 20},
            {"OPER_NAME": "B", "QTY": 15},
            {"OPER_NAME": "C", "QTY": 99},
        ],
        filters={"PROCESS_NAME": {"operator": "in", "value": ["A", "B"]}},
        filter_mappings={"OPER_NAME": ["PROCESS_NAME"]},
        steps=[
            {"operation": "apply_filters", "source_alias": "source_1"},
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "source_1",
                "group_by": ["OPER_NAME"],
                "aggregations": [{"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}],
            },
            {"operation": "sort_and_top_n", "sort_by": "QTY_SUM", "order": "desc", "limit": 2},
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["OPER_NAME"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["OPER_NAME", "QTY_SUM"],
            "column_labels": {"OPER_NAME": "그룹", "QTY_SUM": "수량"},
        },
    )
    resolved, executed, model_calls = _resolve_and_execute(payload)

    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "fast"
    assert contract["recipe"] == "ranked_summary"
    assert contract["filters"][0]["canonical_field"] == "OPER_NAME"
    route_resolution = resolved["intent_plan"]["route_resolution"]
    assert route_resolution["intent_candidate"] == "fast_candidate"
    assert route_resolution["final_route"] == "fast"
    assert route_resolution["final_recipe"] == "ranked_summary"
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "fast"
    assert executed["analysis"]["code_generation_type"] == "deterministic_function"
    assert "deterministic_function" not in executed["analysis"]
    assert "resolved_fast_path_plan" not in resolved["intent_plan"]
    pandas_trace = executed["trace"]["inspection"]["pandas_execution"]
    assert pandas_trace["llm_generated_code"] == ""
    assert "_apply_fast_filters" in pandas_trace["deterministic_function"]["handlers"]
    assert "_fast_aggregate" in pandas_trace["deterministic_function"]["handlers"]
    assert "result, execution_certificate = _execute_fast_path_recipe(" in pandas_trace["deterministic_logic_code"]
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "B", "QTY_SUM": 35},
        {"OPER_NAME": "A", "QTY_SUM": 10},
    ]
    assert model_calls == []
    assert executed["trace"]["inspection"]["fast_path"]["llm_calls"]["pandas_generation"] == 0


def test_target_detail_query_standardizes_physical_plan_columns_and_uses_fast_path():
    adapter = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "14_retrieval_payload_adapter.py"
    )
    payload = _single_source_payload(
        rows=[
            {
                "DATE": "2026-08-06",
                "Mode": "LPDDR5",
                "INPUT 계획": "1.5",
                "OUT 계획": "2",
            }
        ],
        filters={"DATE": {"operator": "eq", "value": "20260806"}},
        filter_mappings={
            "DATE": ["DATE"],
            "MODE": ["Mode"],
            "INPUT_PLAN_QTY": ["INPUT 계획"],
            "OUT_PLAN_QTY": ["OUT 계획"],
        },
        steps=[
            {
                "operation": "apply_filters",
                "source_alias": "source_1",
                "output_alias": "target_filtered",
            },
            {
                "operation": "select_columns",
                "source_alias": "target_filtered",
                "output_alias": "target_result",
            },
        ],
        output_contract={
            "result_mode": "detail",
            "required_columns": ["DATE", "MODE", "INPUT_PLAN_QTY", "OUT_PLAN_QTY"],
            "metric_columns": ["INPUT_PLAN_QTY", "OUT_PLAN_QTY"],
            "result_columns": ["DATE", "MODE", "INPUT_PLAN_QTY", "OUT_PLAN_QTY"],
            "strict_result_columns": True,
        },
        source_alias="source_1",
        dataset_key="target",
    )
    payload["intent_plan"]["retrieval_jobs"][0]["metric_semantics"] = {
        "INPUT_PLAN_QTY": {
            "value_transform": {"coerce_numeric": True, "multiplier": 1000}
        },
        "OUT_PLAN_QTY": {
            "value_transform": {"coerce_numeric": True, "multiplier": 1000}
        },
    }

    adapted = adapter.build_retrieval_payload(payload)
    resolved, executed, model_calls = _resolve_and_execute(adapted)

    schema = adapted["trace"]["inspection"]["source_schema_resolution"]
    assert schema["status"] == "complete"
    assert schema["sources"][0]["runtime_columns"] == [
        "DATE",
        "MODE",
        "INPUT_PLAN_QTY",
        "OUT_PLAN_QTY",
    ]
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["recipe"] == "detail_query"
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "fast"
    assert executed["data"]["rows"] == [
        {
            "DATE": "2026-08-06",
            "MODE": "LPDDR5",
            "INPUT_PLAN_QTY": 1500,
            "OUT_PLAN_QTY": 2000,
        }
    ]
    assert model_calls == []


def test_retriever_pushdown_filter_is_not_applied_twice():
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}],
        filters={"GROUP": {"operator": "eq", "value": "A"}},
        steps=[{"operation": "apply_filters", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["GROUP", "QTY"]},
    )
    payload["source_results"][0]["source_execution"] = {
        "filters_applied_in_retriever": True
    }
    resolved, executed, model_calls = _resolve_and_execute(payload)

    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["filters"][0]["execution_stage"] == "retrieval_pushdown"
    filter_trace = executed["analysis"]["semantic_execution_certificate"]["filter_execution"]
    assert filter_trace[0]["status"] == "already_applied"
    assert executed["data"]["rows"] == [{"GROUP": "A", "QTY": 10}]
    assert model_calls == []


def test_missing_filter_mapping_is_blocked_instead_of_guessed():
    resolver, _, _ = _modules()
    payload = _single_source_payload(
        rows=[{"OPER_NAME": "A", "QTY": 1}],
        filters={"UNKNOWN_PHYSICAL_NAME": {"operator": "eq", "value": "A"}},
        steps=[],
        output_contract={"result_mode": "detail", "result_columns": ["OPER_NAME", "QTY"]},
    )
    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["route"] == "blocked"
    assert "filter_contract_invalid" in resolved["simple_analysis_contract"]["eligibility"]["reason_codes"]
    assert resolved["trace"]["errors"][-1]["type"] == "fast_path_contract_invalid"


def test_count_rows_is_a_fast_scalar_without_a_fake_metric_column():
    payload = _single_source_payload(
        rows=[{"GROUP": "A"}, {"GROUP": "B"}, {"GROUP": "C"}],
        steps=[
            {
                "operation": "count_rows",
                "source_alias": "source_1",
                "calculation": {"output_column": "ROW_COUNT"},
            }
        ],
        output_contract={
            "result_mode": "scalar",
            "required_columns": ["ROW_COUNT"],
            "metric_columns": ["ROW_COUNT"],
            "result_columns": ["ROW_COUNT"],
            "primary_metric": "ROW_COUNT",
        },
    )
    resolved, executed, model_calls = _resolve_and_execute(payload)
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["calculation"]["scalar_operation"] == "count_rows"
    assert executed["data"]["rows"] == [{"ROW_COUNT": 3}]
    assert model_calls == []


def test_value_counts_is_fast_with_an_explicit_count_output():
    payload = _single_source_payload(
        rows=[{"GROUP": "A"}, {"GROUP": "B"}, {"GROUP": "A"}],
        steps=[
            {
                "operation": "value_counts",
                "source_alias": "source_1",
                "group_by": ["GROUP"],
                "calculation": {"output_column": "ROW_COUNT"},
            }
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP"],
            "required_columns": ["GROUP", "ROW_COUNT"],
            "metric_columns": ["ROW_COUNT"],
            "result_columns": ["GROUP", "ROW_COUNT"],
        },
    )
    resolved, executed, model_calls = _resolve_and_execute(payload)
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert executed["data"]["rows"] == [
        {"GROUP": "A", "ROW_COUNT": 2},
        {"GROUP": "B", "ROW_COUNT": 1},
    ]
    assert model_calls == []


def test_multiple_external_sources_route_to_complex_and_invoke_model_once():
    resolver, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"KEY": "A", "QTY": 1}],
        steps=[{"operation": "join", "left_source_alias": "left", "right_source_alias": "right"}],
        output_contract={"result_mode": "aggregate", "result_columns": ["KEY", "QTY"]},
    )
    payload["runtime_sources"]["right"] = [{"KEY": "A", "WIP": 2}]
    payload["source_results"].append(
        {"source_alias": "right", "dataset_key": "right_dataset", "status": "ok", "columns": ["KEY", "WIP"]}
    )
    payload["intent_plan"]["retrieval_jobs"].append(
        {"source_alias": "right", "dataset_key": "right_dataset", "filters": {}}
    )
    payload["intent_plan"]["resolved_execution_graph"]["external_source_requirements"].append(
        {"source_alias": "right", "dataset_key": "right_dataset", "provider": "retrieval_job", "required": True}
    )
    resolved = resolver.resolve_simple_analysis_contract(payload)
    calls: list[str] = []

    def invoke(prompt: str):
        calls.append(prompt)
        return '{"code":"result = sources[\\"source_1\\"][[\\"KEY\\", \\"QTY\\"]].copy()"}'

    executed = executor.execute_hybrid_analysis(
        resolved,
        "complex prompt",
        model_invoker=invoke,
        repair_prompt_template="repair",
        max_repair_attempts=0,
    )
    assert resolved["simple_analysis_contract"]["route"] == "complex"
    assert "multiple_external_sources" in resolved["simple_analysis_contract"]["eligibility"]["reason_codes"]
    assert resolved["intent_plan"]["route_resolution"]["intent_candidate"] == "complex_required"
    assert resolved["intent_plan"]["route_resolution"]["final_route"] == "complex"
    assert len(calls) == 1
    assert executed["analysis"]["execution_route"] == "complex"


def test_intent_ir_authoritatively_excludes_unused_retrieval_source_from_fast_route():
    resolver, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}],
        steps=[
            {
                "operation": "groupby_and_aggregate",
                "group_by": ["GROUP"],
                "aggregations": [{"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}],
            }
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["GROUP", "QTY_SUM"],
        },
    )
    payload["intent_plan"]["retrieval_jobs"].append(
        {"source_alias": "optional_source", "dataset_key": "optional_dataset", "filters": {}, "required": False}
    )
    payload["intent_plan"]["resolved_execution_graph"]["external_source_requirements"].append(
        {
            "source_alias": "optional_source",
            "dataset_key": "optional_dataset",
            "provider": "retrieval_job",
            "required": False,
        }
    )
    payload["intent_plan"]["intent_ir"] = {
        "version": 1,
        "status": "complete",
        "route_source_aliases": ["source_1"],
        "operations": ["groupby_and_aggregate"],
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["source_alias"] == "source_1"
    assert resolved["intent_plan"]["route_resolution"]["candidate_source_aliases"] == ["source_1"]
    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused",
        model_invoker=lambda prompt: (_ for _ in ()).throw(AssertionError("Fast route called an LLM")),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["execution_route"] == "fast"


def test_multiturn_scope_is_preserved_while_each_turn_resolves_its_own_route():
    resolver, executor, _ = _modules()
    turn1 = _single_source_payload(
        rows=[{"PRODUCT": "A", "QTY": 30}, {"PRODUCT": "B", "QTY": 10}],
        steps=[
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "source_1",
                "group_by": ["PRODUCT"],
                "aggregations": [{"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}],
            },
            {"operation": "sort_and_top_n", "sort_by": "QTY_SUM", "order": "desc", "limit": 1},
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["PRODUCT"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["PRODUCT", "QTY_SUM"],
        },
    )
    turn1["intent_plan"].update(
        {"request_scope": "new_analysis", "reference_mode": "none", "reuse_strategy": "none"}
    )
    resolved1 = resolver.resolve_simple_analysis_contract(turn1)
    assert resolved1["simple_analysis_contract"]["route"] == "fast"
    assert resolved1["intent_plan"]["request_scope"] == "new_analysis"

    turn2 = deepcopy(turn1)
    turn2["intent_plan"].update(
        {
            "request_scope": "followup_analysis",
            "reference_mode": "previous_result",
            "reuse_strategy": "previous_result_plus_requery",
        }
    )
    turn2["intent_plan"]["retrieval_jobs"].append(
        {"source_alias": "source_2", "dataset_key": "dataset_2", "filters": {}}
    )
    turn2["intent_plan"]["resolved_execution_graph"]["external_source_requirements"].append(
        {
            "source_alias": "source_2",
            "dataset_key": "dataset_2",
            "provider": "retrieval_job",
            "required": True,
        }
    )
    turn2["runtime_sources"]["source_2"] = [{"PRODUCT": "A", "EQP_ID": "E1"}]
    turn2["source_results"].append(
        {
            "source_alias": "source_2",
            "dataset_key": "dataset_2",
            "status": "ok",
            "row_count": 1,
            "columns": ["PRODUCT", "EQP_ID"],
        }
    )
    turn2["intent_plan"]["pandas_execution_plan"].append(
        {
            "operation": "join",
            "left_source_alias": "source_1",
            "right_source_alias": "source_2",
            "join_type": "left",
        }
    )
    resolved2 = resolver.resolve_simple_analysis_contract(turn2)
    assert resolved2["simple_analysis_contract"]["route"] == "complex"
    assert resolved2["intent_plan"]["request_scope"] == "followup_analysis"
    assert resolved2["intent_plan"]["reference_mode"] == "previous_result"
    assert resolved2["intent_plan"]["reuse_strategy"] == "previous_result_plus_requery"

    turn3 = _single_source_payload(
        rows=[{"PRODUCT": "A", "EQP_COUNT": 4}, {"PRODUCT": "B", "EQP_COUNT": 2}],
        steps=[
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "previous_result",
                "group_by": ["PRODUCT"],
                "aggregations": [
                    {"column": "EQP_COUNT", "method": "max", "output_column": "EQP_COUNT"}
                ],
            },
            {"operation": "sort_and_top_n", "sort_by": "EQP_COUNT", "order": "desc", "limit": 1}
        ],
        output_contract={
            "result_mode": "detail",
            "fast_path_recipe": "ranked_summary",
            "grain_columns": ["PRODUCT"],
            "metric_columns": ["EQP_COUNT"],
            "result_columns": ["PRODUCT", "EQP_COUNT"],
        },
        source_alias="previous_result",
        dataset_key="previous_result",
    )
    turn3["intent_plan"].update(
        {
            "request_scope": "followup_transform",
            "reference_mode": "previous_result",
            "reuse_strategy": "reuse_previous_result",
        }
    )
    resolved3 = resolver.resolve_simple_analysis_contract(turn3)
    calls: list[str] = []
    executed3 = executor.execute_hybrid_analysis(
        resolved3,
        "unused",
        model_invoker=lambda prompt: calls.append(prompt),
        repair_prompt_template="repair",
    )
    assert resolved3["simple_analysis_contract"]["route"] == "fast"
    assert executed3["analysis"]["execution_route"] == "fast"
    assert executed3["intent_plan"]["request_scope"] == "followup_transform"
    assert calls == []


def test_fast_answer_builder_does_not_invoke_answer_model():
    _, _, answer = _modules()
    payload = {
        "simple_analysis_contract": {
            "route": "fast",
            "recipe": "ranked_summary",
            "ordering": [{"column": "QTY_SUM", "direction": "desc"}],
            "limit": 2,
        },
        "intent_plan": {
            "output_contract": {
                "column_labels": {"QTY_SUM": "수량"},
                "primary_metric": "QTY_SUM",
            }
        },
        "analysis": {"status": "ok", "execution_route": "fast", "row_count": 2},
        "data": {
            "columns": ["GROUP", "QTY_SUM"],
            "rows": [{"GROUP": "B", "QTY_SUM": 3}, {"GROUP": "A", "QTY_SUM": 2}],
            "row_count": 2,
        },
        "trace": {"inspection": {"fast_path": {"llm_calls": {"intent": 1, "pandas_generation": 0, "repair": 0, "answer": 0}}}},
    }
    calls: list[str] = []

    def invoke(prompt: str):
        calls.append(prompt)
        return "must not run"

    result = answer.build_hybrid_answer_response(payload, "unused", model_invoker=invoke)
    assert calls == []
    assert "상위 2개" in result["answer_message"]
    assert result["trace"]["inspection"]["fast_path"]["llm_calls"]["answer"] == 0


def test_complex_answer_builder_bool_toggle_controls_answer_model_call():
    _, _, answer = _modules()
    payload = {
        "request": {"question": "그룹별 수량을 알려줘"},
        "simple_analysis_contract": {"route": "complex"},
        "intent_plan": {
            "output_contract": {
                "column_labels": {"GROUP": "그룹", "QTY_SUM": "수량"},
                "primary_metric": "QTY_SUM",
                "result_columns": ["GROUP", "QTY_SUM"],
            }
        },
        "analysis": {
            "status": "ok",
            "execution_route": "complex",
            "row_count": 1,
            "columns": ["GROUP", "QTY_SUM"],
        },
        "data": {
            "columns": ["GROUP", "QTY_SUM"],
            "rows": [{"GROUP": "A", "QTY_SUM": 10}],
            "row_count": 1,
        },
        "trace": {
            "inspection": {
                "fast_path": {
                    "llm_calls": {
                        "intent": 1,
                        "pandas_generation": 1,
                        "repair": 0,
                        "answer": 0,
                    }
                }
            }
        },
    }
    disabled_calls: list[str] = []

    deterministic = answer.build_hybrid_answer_response(
        deepcopy(payload),
        "complex answer prompt",
        model_invoker=lambda prompt: disabled_calls.append(prompt),
        use_llm_answer=False,
    )
    assert disabled_calls == []
    assert "수량" in deterministic["answer_message"]
    assert "10" in deterministic["answer_message"]
    disabled_trace = deterministic["trace"]["inspection"]
    assert disabled_trace["fast_path"]["llm_calls"]["answer"] == 0
    assert disabled_trace["answer_model_response"]["model_called"] is False
    assert disabled_trace["answer_model_response"]["policy"] == "deterministic_complex_answer"
    assert disabled_trace["answer_model_response"]["llm_answer_enabled"] is False

    enabled_calls: list[str] = []

    def invoke(prompt: str):
        enabled_calls.append(prompt)
        return "모델이 생성한 Complex 답변입니다."

    llm_answer = answer.build_hybrid_answer_response(
        deepcopy(payload),
        "complex answer prompt",
        model_invoker=invoke,
        use_llm_answer=True,
    )
    assert enabled_calls == ["complex answer prompt"]
    assert llm_answer["answer_message"] == "모델이 생성한 Complex 답변입니다."
    enabled_trace = llm_answer["trace"]["inspection"]
    assert enabled_trace["fast_path"]["llm_calls"]["answer"] == 1
    assert enabled_trace["answer_model_response"]["model_called"] is True
    assert enabled_trace["answer_model_response"]["policy"] == "llm_complex_answer"
    assert enabled_trace["answer_model_response"]["llm_answer_enabled"] is True


def test_v2_flow_exposes_visible_complex_answer_llm_bool_input():
    flow = build_flow()
    node = next(
        item
        for item in flow["data"]["nodes"]
        if item["id"] == "CustomComponent-BVItv"
    )
    component = node["data"]["node"]
    field = component["template"]["use_llm_answer"]

    assert component["display_name"] == "20 V2 Hybrid 답변 생성기"
    assert field["_input_type"] == "BoolInput"
    assert field["display_name"] == "Complex 답변 LLM 사용"
    assert field["value"] is True
    assert field["advanced"] is False
    assert field["show"] is True
    assert "use_llm_answer" in component["field_order"]


def test_complex_deterministic_answer_covers_comparisons_empty_and_error_results():
    _, _, answer = _modules()

    def payload(
        *,
        question: str,
        rows: list[dict],
        columns: list[str],
        contract: dict,
        certificate: dict | None = None,
        status: str = "ok",
    ) -> dict:
        analysis = {
            "status": status,
            "execution_route": "complex",
            "row_count": len(rows),
            "columns": columns,
        }
        if certificate:
            analysis["semantic_execution_certificate"] = certificate
        return {
            "request": {"question": question},
            "simple_analysis_contract": {"route": "complex"},
            "intent_plan": {"output_contract": contract},
            "analysis": analysis,
            "data": {"columns": columns, "rows": rows, "row_count": len(rows)},
            "trace": {
                "inspection": {
                    "fast_path": {
                        "llm_calls": {
                            "intent": 1,
                            "pandas_generation": 1,
                            "repair": 0,
                            "answer": 0,
                        }
                    }
                }
            },
        }

    cases = [
        (
            payload(
                question="INPUT 실적은 있으나 D/A WIP 없는 제품 알려줘",
                rows=[
                    {"PRODUCT": "A", "INPUT_QTY": 10, "DA_WIP": 0},
                    {"PRODUCT": "B", "INPUT_QTY": 5, "DA_WIP": 0},
                ],
                columns=["PRODUCT", "INPUT_QTY", "DA_WIP"],
                contract={
                    "grain_columns": ["PRODUCT"],
                    "metric_columns": ["INPUT_QTY", "DA_WIP"],
                    "column_labels": {
                        "INPUT_QTY": "INPUT 실적",
                        "DA_WIP": "D/A 재공",
                    },
                },
                certificate={
                    "operation": "compare_presence",
                    "postcondition_validation": "passed",
                },
            ),
            ("존재·부재", "2건"),
        ),
        (
            payload(
                question="투입 실적 대비 WIP 많은 제품 알려줘",
                rows=[{"PRODUCT": "A", "INPUT_QTY": 10, "WIP_QTY": 20}],
                columns=["PRODUCT", "INPUT_QTY", "WIP_QTY"],
                contract={
                    "grain_columns": ["PRODUCT"],
                    "metric_columns": ["INPUT_QTY", "WIP_QTY"],
                    "column_labels": {
                        "INPUT_QTY": "투입 실적",
                        "WIP_QTY": "재공 수량",
                    },
                },
                certificate={
                    "operation": "compare_metrics",
                    "postcondition_validation": "passed",
                    "lhs_metric_column": "WIP_QTY",
                    "rhs_metric_column": "INPUT_QTY",
                    "operator": "gt",
                },
            ),
            ("재공 수량 > 투입 실적", "1건"),
        ),
        (
            payload(
                question="생산량 상위 3개 제품 알려줘",
                rows=[
                    {"PRODUCT": "A", "PRODUCTION_SUM": 30},
                    {"PRODUCT": "B", "PRODUCTION_SUM": 20},
                ],
                columns=["PRODUCT", "PRODUCTION_SUM"],
                contract={
                    "grain_columns": ["PRODUCT"],
                    "metric_columns": ["PRODUCTION_SUM"],
                    "primary_metric": "PRODUCTION_SUM",
                    "column_labels": {"PRODUCTION_SUM": "생산량"},
                    "ordering": {
                        "sort_by": "PRODUCTION_SUM",
                        "order": "desc",
                        "limit": 3,
                    },
                },
            ),
            ("상위 3개", "2건"),
        ),
        (
            payload(
                question="조건에 맞는 제품 알려줘",
                rows=[],
                columns=["PRODUCT", "QTY"],
                contract={"primary_metric": "QTY"},
            ),
            ("조건을 만족하는 데이터가 없습니다",),
        ),
        (
            payload(
                question="분석해줘",
                rows=[],
                columns=[],
                contract={},
                status="error",
            ),
            ("분석을 완료하지 못했습니다",),
        ),
    ]

    for source_payload, expected_fragments in cases:
        calls: list[str] = []
        result = answer.build_hybrid_answer_response(
            source_payload,
            "must not be invoked",
            model_invoker=lambda prompt: calls.append(prompt),
            use_llm_answer=False,
        )
        assert calls == []
        assert all(fragment in result["answer_message"] for fragment in expected_fragments)
        inspection = result["trace"]["inspection"]
        assert inspection["fast_path"]["llm_calls"]["answer"] == 0
        assert inspection["answer_model_response"]["policy"] == "deterministic_complex_answer"


def test_v2_message_adapter_shows_route_and_fixed_fast_logic():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}, {"GROUP": "B", "QTY": 20}],
        steps=[
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "source_1",
                "group_by": ["GROUP"],
                "aggregations": [{"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}],
            }
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["GROUP", "QTY_SUM"],
        },
    )
    resolved, executed, _ = _resolve_and_execute(payload)
    message = adapter.build_message(
        executed,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
        show_next_questions=False,
        show_intent_analysis=True,
        show_data_retrieval=False,
        show_pandas_code=True,
    )
    assert "예상 실행 경로: `Fast 후보`" in message
    assert "최종 실행 경로: `Fast`" in message
    assert "실행된 Fast 고정 로직" in message
    assert "_execute_fast_path_recipe" in message
    assert "_fast_aggregate" in message


def test_v2_normalizer_aligns_aggregate_group_by_with_metadata_grain_before_fast_execution():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_ref = {"section": "product_key_columns", "key": "standard_product_keys"}
    canonical_grain = [
        "TECH",
        "DEN",
        "MODE",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "ORG",
        "LEAD",
        "MCP_NO",
    ]
    candidates = {
        "metadata_candidates": {
            "domain_items": [
                {
                    **product_ref,
                    "payload": {"columns": canonical_grain},
                }
            ],
            "table_catalog_items": [
                {
                    "dataset_key": "production",
                    "payload": {
                        "columns": [*canonical_grain, "PRODUCTION"],
                        "filter_mappings": {
                            column: [column] for column in [*canonical_grain, "PRODUCTION"]
                        },
                    },
                }
            ],
            "main_flow_filters": [],
        }
    }
    normalized = normalizer.normalize_intent_plan(
        {
            "request": {"question": "선택된 제품군의 실적을 제품별로 알려줘"},
            "trace": {"warnings": [], "errors": [], "inspection": {}},
        },
        {
            "metadata_refs": [product_ref],
            "intent_plan": {
                "analysis_kind": "production_by_product",
                "request_scope": "new_analysis",
                "reference_mode": "none",
                "grain_plan": {
                    "metadata_ref": product_ref,
                    "source_alias": "production",
                },
                "retrieval_jobs": [
                    {"dataset_key": "production", "source_alias": "production"}
                ],
                "pandas_execution_plan": [
                    {
                        "node_id": "aggregate_by_product",
                        "operation": "groupby_and_aggregate",
                        "inputs": [{"kind": "external_source", "ref": "production"}],
                        "output_alias": "production_by_product",
                        "source_alias": "production",
                        "group_by": [
                            "TECH",
                            "DEN",
                            "MODE",
                            "PKG_TYPE1",
                            "PKG_TYPE2",
                            "LEAD",
                            "MCP_NO",
                        ],
                        "aggregations": [
                            {
                                "column": "PRODUCTION",
                                "method": "sum",
                                "output_column": "PRODUCTION_SUM",
                            }
                        ],
                    }
                ],
                "output_contract": {
                    "result_mode": "aggregate",
                    "grain_columns": [
                        "TECH",
                        "DEN",
                        "MODE",
                        "PKG_TYPE1",
                        "PKG_TYPE2",
                        "LEAD",
                        "MCP_NO",
                    ],
                    "metric_columns": ["PRODUCTION_SUM"],
                },
            },
        },
        candidates,
    )

    plan = normalized["intent_plan"]
    aggregate_step = plan["pandas_execution_plan"][0]
    assert aggregate_step["group_by"] == canonical_grain
    assert plan["output_contract"]["grain_columns"] == canonical_grain
    assert plan["output_contract"]["result_columns"] == [
        *canonical_grain,
        "PRODUCTION_SUM",
    ]
    alignment = normalized["trace"]["inspection"]["intent"][
        "aggregate_grain_alignment"
    ]
    assert alignment["status"] == "applied"
    assert alignment["changes"][0]["added_columns"] == ["ORG"]

    normalized["runtime_sources"] = {
        "production": [
            {
                "TECH": "T1",
                "DEN": "D1",
                "MODE": "M1",
                "PKG_TYPE1": "P1",
                "PKG_TYPE2": "P2",
                "ORG": "O1",
                "LEAD": "L1",
                "MCP_NO": "N1",
                "PRODUCTION": 7,
            }
        ]
    }
    normalized["source_results"] = [
        {
            "source_alias": "production",
            "dataset_key": "production",
            "status": "ok",
            "row_count": 1,
            "columns": [*canonical_grain, "PRODUCTION"],
        }
    ]
    resolved, executed, model_calls = _resolve_and_execute(normalized)
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["columns"] == [*canonical_grain, "PRODUCTION_SUM"]
    assert model_calls == []


def test_v2_resolver_rejects_result_columns_not_produced_by_aggregate_contract():
    resolver, _, _ = _modules()
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "ORG": "O1", "QTY": 10}],
        steps=[
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "source_1",
                "group_by": ["GROUP"],
                "aggregations": [
                    {"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}
                ],
            }
        ],
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["GROUP", "ORG"],
            "metric_columns": ["QTY_SUM"],
            "result_columns": ["GROUP", "ORG", "QTY_SUM"],
        },
    )

    resolved = resolver.resolve_simple_analysis_contract(payload)
    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "complex"
    assert contract["eligibility"]["reason_codes"] == ["fast_contract_incomplete"]
    assert {
        "type": "unproduced_result_column",
        "column": "ORG",
    } in contract["validation_errors"]


def test_v2_message_adapter_omits_llm_parse_diagnostics_for_fast_errors():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "intent_plan": {"pandas_execution_plan": []},
        "analysis": {
            "status": "error",
            "execution_route": "fast",
            "error": {"type": "output_contract_violation", "message": "missing column"},
        },
        "trace": {
            "inspection": {
                "pandas_execution": {
                    "status": "error",
                    "execution_mode": "fast_deterministic",
                    "error": {
                        "type": "output_contract_violation",
                        "message": "missing column",
                    },
                    "llm_response_parse": {
                        "mode": "invalid",
                        "error": "empty response",
                    },
                }
            }
        },
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
        show_next_questions=False,
        show_intent_analysis=False,
        show_data_retrieval=False,
        show_pandas_code=True,
    )
    assert "LLM 응답 해석" not in message


def test_advanced_fast_recipes_execute_deterministically():
    _, executor, _ = _modules()
    rows = [
        {"GROUP": "A", "DATE": "2026-01-01", "CATEGORY": "X", "QTY": 10},
        {"GROUP": "A", "DATE": "2026-02-01", "CATEGORY": "Y", "QTY": 30},
        {"GROUP": "B", "DATE": "2026-01-01", "CATEGORY": "X", "QTY": 20},
        {"GROUP": "B", "DATE": "2026-02-01", "CATEGORY": "Y", "QTY": 40},
    ]
    sources = {"s": pd.DataFrame(rows)}
    metric = [{"source_column": "QTY", "aggregation": "sum", "output_column": "QTY_SUM"}]

    contracts = {
        "percent_of_total": {
            "group_by": ["GROUP"],
            "metrics": metric,
            "calculation": {"denominator_scope": "grand_total", "zero_division_policy": "zero", "output_column": "SHARE"},
            "result_columns": ["GROUP", "QTY_SUM", "SHARE"],
        },
        "rank_within_group": {
            "group_by": ["GROUP", "CATEGORY"],
            "metrics": metric,
            "ordering": [{"column": "QTY_SUM", "direction": "desc"}],
            "calculation": {"partition_by": ["GROUP"], "rank_method": "dense_rank", "tie_policy": "include_all", "output_column": "RANK"},
            "result_columns": ["GROUP", "CATEGORY", "QTY_SUM", "RANK"],
        },
        "threshold_after_aggregate": {
            "group_by": ["GROUP"],
            "metrics": metric,
            "post_filters": [{"column": "QTY_SUM", "operator": "gt", "value": 50}],
            "result_columns": ["GROUP", "QTY_SUM"],
        },
        "time_bucket_summary": {
            "group_by": ["GROUP"],
            "metrics": metric,
            "calculation": {"time_column": "DATE", "time_bucket_column": "MONTH", "frequency": "month", "closed": "left", "label": "left"},
            "result_columns": ["GROUP", "MONTH", "QTY_SUM"],
        },
        "period_change": {
            "group_by": ["GROUP", "DATE"],
            "metrics": metric,
            "calculation": {"time_column": "DATE", "partition_by": ["GROUP"], "periods": 1, "change_method": "difference", "zero_division_policy": "null", "output_column": "CHANGE"},
            "result_columns": ["GROUP", "DATE", "QTY_SUM", "CHANGE"],
        },
        "running_total": {
            "group_by": ["GROUP", "DATE"],
            "metrics": metric,
            "calculation": {"time_column": "DATE", "partition_by": ["GROUP"], "output_column": "RUNNING"},
            "result_columns": ["GROUP", "DATE", "QTY_SUM", "RUNNING"],
        },
        "moving_aggregate": {
            "group_by": ["GROUP", "DATE"],
            "metrics": metric,
            "calculation": {"time_column": "DATE", "partition_by": ["GROUP"], "window": 2, "min_periods": 1, "moving_method": "mean", "output_column": "MOVING"},
            "result_columns": ["GROUP", "DATE", "QTY_SUM", "MOVING"],
        },
        "percentile_summary": {
            "group_by": ["GROUP"],
            "metrics": metric,
            "calculation": {"percentile": 0.5, "percentile_method": "continuous"},
            "result_columns": ["GROUP", "QTY_SUM"],
        },
        "pivot_summary": {
            "calculation": {"pivot_index": ["GROUP"], "pivot_columns": ["CATEGORY"], "pivot_values": ["QTY"], "pivot_aggregation": "sum", "pivot_fill_value": 0, "max_pivot_columns": 10},
            "result_columns": [],
            "result_schema_mode": "derived_bounded",
        },
    }
    results = {}
    for recipe, values in contracts.items():
        contract = {
            "operation": "execute_fast_path_recipe",
            "recipe": recipe,
            "source_alias": "s",
            "filters": [],
            "projection": [],
            "group_by": [],
            "metrics": [],
            "post_filters": [],
            "ordering": [],
            "limit": 0,
            "tie_policy": "first_n",
            "calculation": {},
            "result_columns": [],
            **deepcopy(values),
        }
        result, certificate = executor._execute_fast_path_recipe(contract, sources, pd)
        assert certificate["postcondition_validation"] == "passed"
        results[recipe] = result

    assert results["percent_of_total"]["SHARE"].round(2).tolist() == [0.4, 0.6]
    assert results["threshold_after_aggregate"][["GROUP", "QTY_SUM"]].to_dict("records") == [{"GROUP": "B", "QTY_SUM": 60}]
    assert results["running_total"]["RUNNING"].tolist() == [10, 40, 20, 60]
    assert results["moving_aggregate"]["MOVING"].tolist() == [10.0, 20.0, 20.0, 30.0]
    assert set(results["pivot_summary"].columns) == {"GROUP", "X", "Y"}


def test_basic_fast_recipes_execute_deterministically():
    _, executor, _ = _modules()
    sources = {
        "s": pd.DataFrame(
            [
                {"GROUP": "A", "ITEM": "X", "QTY": 10, "TS": "2026-01-01", "VALUE": None},
                {"GROUP": "A", "ITEM": "Y", "QTY": 20, "TS": "2026-01-02", "VALUE": ""},
                {"GROUP": "B", "ITEM": "X", "QTY": 30, "TS": "2026-01-03", "VALUE": "ok"},
                {"GROUP": "B", "ITEM": "X", "QTY": 30, "TS": "2026-01-04", "VALUE": "ok"},
            ]
        )
    }
    metric = [{"source_column": "QTY", "aggregation": "sum", "output_column": "QTY_SUM"}]
    base = {
        "operation": "execute_fast_path_recipe",
        "source_alias": "s",
        "filters": [],
        "projection": [],
        "group_by": [],
        "metrics": [],
        "post_filters": [],
        "ordering": [],
        "limit": 0,
        "tie_policy": "first_n",
        "calculation": {},
        "result_columns": [],
    }
    cases = {
        "detail_query": {"projection": ["GROUP", "ITEM"], "limit": 2, "result_columns": ["GROUP", "ITEM"]},
        "scalar_summary": {"metrics": metric, "result_columns": ["QTY_SUM"]},
        "group_summary": {"group_by": ["GROUP"], "metrics": metric, "result_columns": ["GROUP", "QTY_SUM"]},
        "frequency_summary": {
            "group_by": ["ITEM"],
            "metrics": [{"source_column": "ITEM", "aggregation": "count", "output_column": "COUNT"}],
            "result_columns": ["ITEM", "COUNT"],
        },
        "distinct_summary": {"projection": ["ITEM"], "result_columns": ["ITEM"]},
        "list_summary": {
            "group_by": ["GROUP"],
            "metrics": [{"source_column": "ITEM", "aggregation": "collect_unique", "output_column": "ITEM_LIST"}],
            "result_columns": ["GROUP", "ITEM_LIST"],
        },
        "existence_summary": {"calculation": {"output_column": "EXISTS"}, "result_columns": ["EXISTS"]},
        "quality_summary": {
            "calculation": {"quality_check": "blank_count", "quality_columns": ["VALUE"], "output_column": "BLANK_COUNT"},
            "result_columns": ["BLANK_COUNT"],
        },
        "latest_earliest": {
            "projection": ["GROUP", "TS"],
            "ordering": [{"column": "TS", "direction": "desc"}],
            "limit": 1,
            "result_columns": ["GROUP", "TS"],
        },
    }
    results = {}
    for recipe, values in cases.items():
        result, certificate = executor._execute_fast_path_recipe(
            {**deepcopy(base), "recipe": recipe, **deepcopy(values)},
            sources,
            pd,
        )
        assert certificate["postcondition_validation"] == "passed"
        results[recipe] = result

    assert len(results["detail_query"]) == 2
    assert results["scalar_summary"].iloc[0]["QTY_SUM"] == 90
    assert results["group_summary"].set_index("GROUP")["QTY_SUM"].to_dict() == {"A": 30, "B": 60}
    assert results["frequency_summary"].set_index("ITEM")["COUNT"].to_dict() == {"X": 3, "Y": 1}
    assert results["distinct_summary"]["ITEM"].tolist() == ["X", "Y"]
    assert results["list_summary"].set_index("GROUP")["ITEM_LIST"].to_dict() == {"A": "X, Y", "B": "X"}
    assert bool(results["existence_summary"].iloc[0]["EXISTS"]) is True
    assert results["quality_summary"].iloc[0]["BLANK_COUNT"] == 2
    assert results["latest_earliest"].iloc[0]["TS"] == "2026-01-04"


def test_incomplete_advanced_recipe_routes_to_complex():
    resolver, _, _ = _modules()
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}],
        steps=[
            {
                "operation": "groupby_and_aggregate",
                "group_by": ["GROUP"],
                "aggregations": [{"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}],
            },
            {"operation": "percent_of_total", "calculation": {"output_column": "SHARE"}},
        ],
        output_contract={
            "fast_path_recipe": "percent_of_total",
            "result_columns": ["GROUP", "QTY_SUM", "SHARE"],
        },
    )
    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["route"] == "complex"
    assert "fast_contract_incomplete" in resolved["simple_analysis_contract"]["eligibility"]["reason_codes"]
    missing = {
        item["field"]
        for item in resolved["simple_analysis_contract"]["validation_errors"]
        if item.get("type") == "missing_calculation_contract"
    }
    assert {"denominator_scope", "zero_division_policy"}.issubset(missing)


def test_time_bucket_honors_closed_and_label_contract():
    _, executor, _ = _modules()
    sources = {
        "s": pd.DataFrame(
            [
                {"TS": "2026-02-01 00:00:00", "QTY": 1},
                {"TS": "2026-02-01 00:00:01", "QTY": 2},
            ]
        )
    }
    base = {
        "operation": "execute_fast_path_recipe",
        "recipe": "time_bucket_summary",
        "source_alias": "s",
        "filters": [],
        "projection": [],
        "group_by": [],
        "metrics": [{"source_column": "QTY", "aggregation": "sum", "output_column": "QTY_SUM"}],
        "post_filters": [],
        "ordering": [{"column": "MONTH", "direction": "asc"}],
        "limit": 0,
        "tie_policy": "first_n",
        "result_columns": ["MONTH", "QTY_SUM"],
    }
    left, _ = executor._execute_fast_path_recipe(
        {
            **deepcopy(base),
            "calculation": {
                "time_column": "TS",
                "time_bucket_column": "MONTH",
                "frequency": "month",
                "closed": "left",
                "label": "left",
            },
        },
        sources,
        pd,
    )
    right, _ = executor._execute_fast_path_recipe(
        {
            **deepcopy(base),
            "calculation": {
                "time_column": "TS",
                "time_bucket_column": "MONTH",
                "frequency": "month",
                "closed": "right",
                "label": "right",
            },
        },
        sources,
        pd,
    )
    assert left[["MONTH", "QTY_SUM"]].to_dict("records") == [
        {"MONTH": pd.Timestamp("2026-02-01"), "QTY_SUM": 3}
    ]
    assert right["QTY_SUM"].tolist() == [1, 2]
    assert right["MONTH"].iloc[0].month == 1
    assert right["MONTH"].iloc[1].month == 2


def test_v2_intent_normalizer_preserves_advanced_recipe_contract():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    response = {
        "intent_plan": {
            "analysis_kind": "generic_share",
            "request_scope": "new_analysis",
            "retrieval_jobs": [
                {"dataset_key": "dataset_1", "source_alias": "source_1", "source_type": "dummy"}
            ],
            "pandas_execution_plan": [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "source_1",
                    "group_by": ["GROUP"],
                    "aggregations": [
                        {"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}
                    ],
                    "output_alias": "grouped",
                },
                {
                    "operation": "percent_of_total",
                    "source_alias": "grouped",
                    "calculation": {
                        "denominator_scope": "grand_total",
                        "zero_division_policy": "zero",
                        "output_column": "SHARE",
                    },
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "fast_path_recipe": "percent_of_total",
                "grain_columns": ["GROUP"],
                "required_columns": ["GROUP", "QTY_SUM", "SHARE"],
                "metric_columns": ["QTY_SUM"],
                "result_columns": ["GROUP", "QTY_SUM", "SHARE"],
                "calculation": {
                    "denominator_scope": "grand_total",
                    "zero_division_policy": "zero",
                    "output_column": "SHARE",
                },
                "column_labels": {"SHARE": "구성비"},
            },
        }
    }
    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "generic share request"}},
        json.dumps(response),
        {
            "table_catalog_items": [
                {
                    "dataset_key": "dataset_1",
                    "source_type": "dummy",
                    "active": True,
                    "columns": ["GROUP", "QTY"],
                }
            ]
        },
    )
    output = normalized["intent_plan"]["output_contract"]
    assert output["fast_path_recipe"] == "percent_of_total"
    assert output["calculation"]["denominator_scope"] == "grand_total"
    assert output["result_columns"] == ["GROUP", "QTY_SUM", "SHARE"]
    assert output["column_labels"]["SHARE"] == "구성비"
    normalized["runtime_sources"] = {
        "source_1": [{"GROUP": "A", "QTY": 10}, {"GROUP": "B", "QTY": 30}]
    }
    normalized["source_results"] = [
        {
            "source_alias": "source_1",
            "dataset_key": "dataset_1",
            "status": "ok",
            "columns": ["GROUP", "QTY"],
            "row_count": 2,
        }
    ]
    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    resolved = resolver.resolve_simple_analysis_contract(normalized)
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["recipe"] == "percent_of_total"


def test_v2_metadata_filters_are_applied_for_selected_quantity_terms():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    payload = {
        "request": {
            "question": "6/24일 투입 실적 대비 D/S1, D/A1공정에서 WIP 많은 제품",
            "reference_date": "20260807",
        },
        "trace": {"inspection": {}, "warnings": [], "errors": []},
    }
    response = {
        "intent_plan": {
            "analysis_kind": "input_actual_vs_wip_by_product",
            "request_scope": "new_analysis",
            "metadata_refs": [
                {"section": "quantity_terms", "key": "input_quantity"},
                {"section": "quantity_terms", "key": "wip_quantity"},
            ],
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production", "filters": {}},
                {
                    "dataset_key": "wip",
                    "source_alias": "wip",
                    "filters": {"OPER_NAME": {"operator": "in", "value": ["D/S1", "D/A1"]}},
                },
            ],
            "pandas_execution_plan": [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "production",
                    "group_by": ["TECH"],
                    "aggregations": [{"column": "PRODUCTION", "method": "sum", "output_column": "INPUT_QTY"}],
                },
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "wip",
                    "group_by": ["TECH"],
                    "aggregations": [{"column": "WIP", "method": "sum", "output_column": "WIP_QTY"}],
                },
            ],
            "output_contract": {"required_columns": ["TECH", "INPUT_QTY", "WIP_QTY"]},
        }
    }
    candidates = {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "input_quantity",
                "payload": {
                    "aliases": ["투입 실적"],
                    "data_source": "production",
                    "column": "PRODUCTION",
                    "filters": [
                        {"column": "OPER_NAME", "operator": "eq", "value": "INPUT"}
                    ],
                },
            },
            {
                "section": "quantity_terms",
                "key": "wip_quantity",
                "payload": {
                    "aliases": ["WIP"],
                    "data_source": "wip",
                    "column": "WIP",
                },
            },
        ],
        "table_catalog_items": [
            {"dataset_key": "production", "columns": ["TECH", "OPER_NAME", "PRODUCTION"]},
            {"dataset_key": "wip", "columns": ["TECH", "OPER_NAME", "WIP"]},
        ],
        "main_flow_filters": [],
    }
    normalized = normalizer.normalize_intent_plan(payload, json.dumps(response), {"metadata_candidates": candidates})
    jobs = {item["source_alias"]: item for item in normalized["intent_plan"]["retrieval_jobs"]}
    assert jobs["production"]["filters"]["OPER_NAME"] == {
        "operator": "eq",
        "value": "INPUT",
    }


def test_v2_recipe_selection_criteria_are_metadata_driven():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    recipe = {
        "selection_criteria": {
            "required_any_aliases": ["배정 장비", "장비 대수"],
        }
    }
    accepted, _ = normalizer._recipe_selection_criteria_match("현재 배정 장비 목록", recipe)
    rejected, detail = normalizer._recipe_selection_criteria_match("D/A1 공정의 평균 UPH", recipe)
    assert accepted is True
    assert rejected is False
    assert detail["reason"] == "required_any_aliases_not_matched"


def test_v2_fast_aggregate_drops_metric_dimension_collision():
    _, executor, _ = _modules()
    frame = pd.DataFrame(
        [
            {"EQP_MODEL": "M1", "RECIPE_ID": "R1", "OPER_NAME": "D/A1", "UPH": 10},
            {"EQP_MODEL": "M1", "RECIPE_ID": "R1", "OPER_NAME": "D/A1", "UPH": 20},
        ]
    )
    result = executor._fast_aggregate(
        frame,
        ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
        [{"source_column": "UPH", "output_column": "UPH", "aggregation": "mean"}],
        pd,
    )
    assert list(result.columns) == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"]
    assert result.iloc[0]["UPH"] == 15
