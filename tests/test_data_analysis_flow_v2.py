from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pandas as pd

from component_test_support import ROOT, load_module
from tools.build_data_analysis_flow_v2 import build_flow


V2_ROOT = ROOT / "langflow_components" / "data_analysis_flow_v2"
V2_VALIDATION_QUESTIONS = ROOT / "validation_questions_v2.txt"


def _modules():
    resolver = load_module(V2_ROOT / "14b_simple_analysis_contract_resolver.py")
    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    answer = load_module(V2_ROOT / "20_hybrid_answer_builder.py")
    return resolver, executor, answer


def _prompt_modules():
    return (
        load_module(V2_ROOT / "16_route_aware_pandas_prompt_builder.py"),
        load_module(V2_ROOT / "18_route_aware_answer_prompt_builder.py"),
    )


def test_v2_lazy_prompt_components_are_self_contained():
    pandas_prompt, answer_prompt = _prompt_modules()
    assert callable(pandas_prompt.build_route_aware_pandas_prompt)
    assert callable(answer_prompt.build_route_aware_answer_prompt)


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


def test_v2_flow_export_matches_current_native_graph():
    flow = build_flow()

    assert flow["endpoint_name"] == "metadata-driven-v5-data-analysis"
    assert flow["name"] == "01. v5_data_analysis"
    assert flow["last_tested_version"] == "1.9.2"
    assert len(flow["data"]["nodes"]) == 51

    node_ids = {node["id"] for node in flow["data"]["nodes"]}
    node_index = {node["id"]: node for node in flow["data"]["nodes"]}
    assert "CustomComponent-v2FastResolver" in node_ids
    assert "LanguageModel-intent" in node_ids
    assert "LanguageModel-pandas" not in node_ids
    assert "LanguageModel-answer" not in node_ids
    assert "Prompt Template-ELVKc" not in node_ids
    assert "CustomComponent-aKrkH" not in node_ids
    assert all(node["data"]["node"].get("lf_version") == "1.9.2" for node in flow["data"]["nodes"])
    assert node_index["CustomComponent-A5y0b"]["data"]["node"]["template"]["code"]["value"] == (
        V2_ROOT / "21_v2_answer_message_adapter.py"
    ).read_text(encoding="utf-8")
    assert node_index["CustomComponent-A5y0b"]["data"]["node"]["field_order"] == [
        "payload",
        "include_diagnostics",
        "show_result_table",
        "table_preview_limit",
        "show_intermediate_results",
        "intermediate_preview_limit",
        "show_download_links",
        "show_notices",
        "show_applied_criteria",
        "show_intent_analysis",
        "show_data_retrieval",
        "show_pandas_code",
    ]
    intent_template = node_index["LanguageModel-intent"]["data"]["node"]["template"]
    assert intent_template["code"]["value"] == (
        V2_ROOT / "03b_catalog_guarded_intent_router.py"
    ).read_text(encoding="utf-8")
    assert {"payload", "metadata_candidates", "intent_prompt", "model", "api_key"}.issubset(intent_template)

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
    assert ("TextInput-VFbHh", "text", "CustomComponent-BVItv", "domain_answer_guidance") in edge_keys
    assert ("CustomComponent-HFsYn", "payload_out", "LanguageModel-intent", "payload") in edge_keys
    assert ("CustomComponent-DXrpf", "metadata_candidates", "LanguageModel-intent", "metadata_candidates") in edge_keys
    assert ("Prompt Template-AUpQz", "prompt", "LanguageModel-intent", "intent_prompt") in edge_keys
    assert node_index["Prompt Template-xtzD5"]["data"]["node"]["display_name"] == "16 V2 경로 인식 pandas Prompt 생성기"
    answer_template = node_index["CustomComponent-BVItv"]["data"]["node"]["template"]
    assert answer_template["answer_prompt_template"]["value"]
    assert "answer_prompt" not in answer_template
    assert not any(target == "CustomComponent-BVItv" and field == "answer_prompt" for _, _, target, field in edge_keys)


def test_catalog_guarded_intent_router_skips_model_when_table_catalog_load_fails():
    router = load_module(V2_ROOT / "03b_catalog_guarded_intent_router.py")
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    metadata = {
        "metadata_candidates": {
            "domain_items": [],
            "table_catalog_items": [],
            "main_flow_filters": [],
        },
        "metadata_load": {
            "status": "error",
            "loads": {"table_catalog_items": {"status": "error", "errors": [{"message": "DNS failed"}]}},
        },
    }
    calls: list[str] = []
    response, trace = router.route_intent_response(
        {"request": {"question": "오늘 생산량 알려줘"}},
        "intent prompt",
        lambda prompt: calls.append(prompt),
        metadata,
    )

    assert calls == []
    assert trace["model_called"] is False
    assert json.loads(response)["intent_plan"]["validation_errors"][0]["type"] == "table_catalog_metadata_unavailable"

    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "오늘 생산량 알려줘"}},
        response,
        metadata,
    )
    assert normalized["intent_plan"]["retrieval_jobs"] == []
    assert normalized["metadata_refs"] == []
    assert normalized["execution_gate"]["status"] == "blocked"
    gated = gate.apply_retrieval_execution_gate(normalized)
    assert gated["execution_gate"]["status"] == "blocked"
    assert gated["analysis"]["error"]["type"] == "table_catalog_metadata_unavailable"
    assert "메타데이터 연결 정보를 확인해 주세요" in gated["answer_message"]


def test_catalog_loader_failure_exposes_safe_detailed_reason_and_purges_llm_plan():
    hydrator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04a_trusted_retrieval_job_hydrator.py")
    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    payload = {
        "metadata_refs": [{"section": "Domain", "key": "invented_daily_production"}],
        "intent_plan": {
            "analysis_kind": "daily_production_volume_by_process",
            "metadata_refs": [{"section": "Domain", "key": "invented_daily_production"}],
            "retrieval_jobs": [{"dataset_key": "DAILY_PRODUCTION_DATA", "source_alias": "production_data"}],
            "pandas_execution_plan": [{"operation": "groupby_and_aggregate"}],
        },
        "trace": {"errors": [], "warnings": [], "inspection": {}},
    }
    loader_failure = {
        "table_catalog_items": [],
        "metadata_load": {
            "status": "error",
            "metadata_kind": "table_catalog_items",
            "database": "datagov",
            "collection_name": "agent_v4_table_catalog_items",
            "errors": [
                {
                    "type": "mongo_load_error",
                    "message": (
                        "The DNS response does not contain an answer to the question: "
                        "_mongodb._tcp.datagov.example.mongodb.net. IN SRV "
                        "mongodb+srv://user:password@datagov.example.mongodb.net"
                    ),
                }
            ],
        },
    }

    blocked = hydrator.hydrate_retrieval_jobs(payload, loader_failure, retrieval_mode="dummy")
    final = gate.apply_retrieval_execution_gate(blocked)

    assert blocked["intent_plan"]["analysis_kind"] == "metadata_catalog_unavailable"
    assert blocked["intent_plan"]["retrieval_jobs"] == []
    assert blocked["intent_plan"]["pandas_execution_plan"] == []
    assert blocked["metadata_refs"] == []
    assert final["analysis"]["error"]["type"] == "table_catalog_metadata_unavailable"
    assert "메타데이터 연결 정보를 확인해 주세요" in final["answer_message"]
    assert "mongo_load_error" in final["answer_message"]
    assert "The DNS response does not contain an answer" in final["answer_message"]
    assert "user:password" not in final["answer_message"]


def test_metadata_candidate_loader_error_reaches_final_answer_without_model_call():
    candidates_builder = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py")
    router = load_module(V2_ROOT / "03b_catalog_guarded_intent_router.py")
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    answer = load_module(V2_ROOT / "20_hybrid_answer_builder.py")
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    loader_error = {
        "table_catalog_items": [],
        "metadata_load": {
            "status": "error",
            "errors": [{"type": "mongo_load_error", "message": "The DNS response does not contain an answer to the question: _mongodb._tcp.example.net. IN SRV"}],
        },
    }
    candidates = candidates_builder.build_metadata_candidates(
        {"request": {"question": "오늘 DA공정 생산량 알려줘"}},
        {"domain_items": [], "metadata_load": {"status": "error", "errors": []}},
        loader_error,
        {"main_flow_filters": [], "metadata_load": {"status": "error", "errors": []}},
    )
    calls: list[str] = []
    response, trace = router.route_intent_response(
        {"request": {"question": "오늘 DA공정 생산량 알려줘"}},
        "intent prompt",
        lambda prompt: calls.append(prompt),
        candidates,
    )
    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "오늘 DA공정 생산량 알려줘"}},
        response,
        candidates,
    )
    final = gate.apply_retrieval_execution_gate(normalized)

    assert calls == []
    assert trace["model_called"] is False
    assert final["intent_plan"]["retrieval_jobs"] == []
    assert "메타데이터 연결 정보를 확인해 주세요" in final["answer_message"]
    assert "mongo_load_error" in final["answer_message"]
    assert "DNS response does not contain an answer" in final["answer_message"]

    # The deterministic error must survive both final answer stages.  This is
    # the path used by the Desktop Flow, not only an internal gate assertion.
    answered = answer.build_answer_response(final, "LLM 응답은 사용하면 안 됩니다.")
    rendered = adapter.build_message(answered, show_notices=True)
    assert "메타데이터 연결 정보를 확인해 주세요" in rendered
    assert "DNS response does not contain an answer" in rendered
    assert "필수 데이터 조회에 실패하여 pandas 분석을 실행하지 않았고 모델 응답도 사용하지 않았습니다." not in rendered


def test_catalog_loader_success_with_no_active_dataset_is_not_misreported_as_connection_failure():
    hydrator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04a_trusted_retrieval_job_hydrator.py")
    payload = {"intent_plan": {"retrieval_jobs": [{"dataset_key": "unknown", "source_alias": "source"}]}, "trace": {}}
    empty_catalog = {"table_catalog_items": [], "metadata_load": {"status": "ok", "errors": []}}

    blocked = hydrator.hydrate_retrieval_jobs(payload, empty_catalog, retrieval_mode="dummy")

    failure = blocked["execution_gate"]["critical_failures"][0]
    assert failure["reason"] == "no_active_table_catalog"
    assert "연결은 성공했지만" in failure["message"]


def test_v2_normalizer_rejects_unregistered_dataset_when_catalog_load_is_available():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    metadata = {
        "metadata_candidates": {
            "domain_items": [],
            "table_catalog_items": [{"dataset_key": "production_today", "payload": {"columns": ["DATE", "PRODUCTION"]}}],
            "main_flow_filters": [],
        },
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }
    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "오늘 INPUT 수량"}},
        {
            "metadata_refs": [{"section": "domains", "key": "invented_domain"}],
            "intent_plan": {
                "retrieval_jobs": [{"dataset_key": "invented_dataset", "source_alias": "invented"}],
                "pandas_execution_plan": [],
            },
        },
        metadata,
    )

    error = normalized["intent_plan"]["validation_errors"][0]
    assert error["type"] == "unregistered_dataset_key"
    assert error["dataset_keys"] == ["invented_dataset"]
    assert normalized["intent_plan"]["retrieval_jobs"] == []
    assert normalized["metadata_refs"] == []


def test_v2_normalizer_rejects_dataset_keys_not_present_in_live_catalog_snapshot():
    """A model must never turn an unavailable schema into a dummy retrieval job."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    metadata = {
        "metadata_candidates": {
            "domain_items": [],
            "table_catalog_items": [{"dataset_key": "production_today", "payload": {"columns": ["DATE", "PRODUCTION"]}}],
            "main_flow_filters": [],
        },
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }

    # These are representative hallucinated keys observed when MongoDB metadata
    # was unavailable. The rule itself is catalog membership, not these names.
    for dataset_key in ("fab_product_input_daily", "process_production_dataset"):
        normalized = normalizer.normalize_intent_plan(
            {"request": {"question": "데이터를 알려줘"}},
            {
                "intent_plan": {
                    "retrieval_jobs": [{"dataset_key": dataset_key, "source_alias": "invented_source"}],
                    "pandas_execution_plan": [],
                }
            },
            metadata,
        )

        error = normalized["intent_plan"]["validation_errors"][0]
        assert error["type"] == "unregistered_dataset_key"
        assert error["dataset_keys"] == [dataset_key]
        assert normalized["intent_plan"]["retrieval_jobs"] == []


def test_bounded_candidates_keep_full_execution_catalog_registry():
    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )
    catalog_items = [
        {
            "dataset_key": "production_today",
            "payload": {"display_name": "Today Production", "columns": ["DATE", "PRODUCTION"]},
        },
        {
            "dataset_key": "equipment_assign",
            "payload": {"display_name": "Equipment Assignment", "columns": ["EQP_ID", "RECIPE_ID"]},
        },
    ]
    result = builder.build_metadata_candidates(
        {"request": {"question": "today production"}},
        [],
        catalog_items,
        [],
        min_table_items=1,
        max_table_items=1,
    )

    selected = result["metadata_candidates"]["table_catalog_items"]
    registry = result["table_catalog_registry"]
    assert len(selected) == 1
    assert selected[0]["dataset_key"] == "production_today"
    assert registry["dataset_keys"] == ["production_today", "equipment_assign"]
    assert {item["dataset_key"] for item in registry["items"]} == {
        "production_today",
        "equipment_assign",
    }
    assert result["metadata_load"]["registered_dataset_count"] == 2
    variables_builder = load_module(V2_ROOT / "02_intent_variables_builder.py")
    model_candidates = json.loads(
        variables_builder.build_variables({"request": {"question": "today production"}}, result)[
            "metadata_candidates"
        ]
    )
    assert [item["dataset_key"] for item in model_candidates["table_catalog_items"]] == [
        "production_today"
    ]
    assert "table_catalog_registry" not in model_candidates


def test_normalizer_hydrates_registered_dataset_outside_bounded_candidates():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    production_item = {
        "dataset_key": "production_today",
        "payload": {"columns": ["DATE", "PRODUCTION"]},
    }
    equipment_item = {
        "dataset_key": "equipment_assign",
        "payload": {
            "columns": ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NM"],
            "filter_mappings": {
                "EQP_ID": ["EQP_ID"],
                "EQP_MODEL": ["EQP_MODEL"],
                "RECIPE_ID": ["RECIPE_ID"],
                "OPER_NAME": ["OPER_NM"],
            },
        },
    }
    metadata = {
        "metadata_candidates": {
            "domain_items": [],
            "table_catalog_items": [production_item],
            "main_flow_filters": [],
        },
        "table_catalog_registry": {
            "dataset_keys": ["production_today", "equipment_assign"],
            "items": [production_item, equipment_item],
        },
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }
    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "list equipment for recipe R0429"}},
        {
            "intent_plan": {
                "analysis_kind": "recipe_equipment_list",
                "retrieval_jobs": [
                    {
                        "dataset_key": "EQUIPMENT_ASSIGN",
                        "source_alias": "equipment_source",
                        "filters": {"RECIPE_ID": {"operator": "starts_with", "value": "R0429"}},
                    }
                ],
                "pandas_execution_plan": [
                    {
                        "node_id": "select_equipment",
                        "operation": "select_columns",
                        "inputs": [{"kind": "external_source", "ref": "equipment_source"}],
                        "source_alias": "equipment_source",
                        "output_alias": "equipment_result",
                    }
                ],
                "output_contract": {
                    "result_mode": "detail",
                    "result_columns": ["EQP_ID", "RECIPE_ID"],
                },
            }
        },
        metadata,
    )

    plan = normalized["intent_plan"]
    assert plan["retrieval_jobs"][0]["dataset_key"] == "equipment_assign"
    assert not any(
        item.get("type") == "unregistered_dataset_key"
        for item in plan.get("validation_errors", [])
        if isinstance(item, dict)
    )
    resolution = normalized["trace"]["inspection"]["intent"]["execution_catalog_resolution"]
    assert resolution["hydrated_dataset_keys"] == ["equipment_assign"]
    assert {"section": "table_catalog", "key": "equipment_assign"} in normalized["metadata_refs"]


def test_catalog_guard_uses_execution_registry_when_prompt_candidates_are_empty():
    router = load_module(V2_ROOT / "03b_catalog_guarded_intent_router.py")
    metadata = {
        "metadata_candidates": {
            "domain_items": [],
            "table_catalog_items": [],
            "main_flow_filters": [],
        },
        "table_catalog_registry": {
            "dataset_keys": ["equipment_assign"],
            "items": [{"dataset_key": "equipment_assign", "payload": {"columns": ["EQP_ID"]}}],
        },
        "metadata_load": {
            "status": "ok",
            "loads": {"table_catalog_items": {"status": "ok"}},
        },
    }
    calls: list[str] = []
    response, trace = router.route_intent_response(
        {"request": {"question": "recipe equipment"}},
        "intent prompt",
        lambda prompt: calls.append(prompt) or '{"intent_plan":{"retrieval_jobs":[]}}',
        metadata,
    )

    assert calls == ["intent prompt"]
    assert trace["model_called"] is True
    assert json.loads(response)["intent_plan"]["retrieval_jobs"] == []


def test_fast_route_skips_full_pandas_prompt_serialization(monkeypatch):
    pandas_prompt, _ = _prompt_modules()
    payload = {"simple_analysis_contract": {"route": "fast"}}

    def fail_if_called(_payload):
        raise AssertionError("Fast route must not build pandas prompt variables")

    monkeypatch.setattr(pandas_prompt, "build_variables", fail_if_called)
    assert pandas_prompt.build_route_aware_pandas_prompt(payload, "{intent_plan_json}", "helper") == ""


def test_repair_prompt_contains_failed_code_only_once():
    template = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "17b_pandas_repair_prompt_template_ko.md"
    ).read_text(encoding="utf-8")

    assert template.count("{failed_code}") == 1
    assert "executed_code_with_preamble" not in template


def test_complex_route_uses_compact_function_case_selection_not_helper_source():
    pandas_prompt, _ = _prompt_modules()
    template = (ROOT / "langflow_components" / "data_analysis_flow" / "16_pandas_prompt_template_ko.md").read_text(encoding="utf-8")
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}],
        steps=[{"operation": "custom_complex_operation", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["GROUP", "QTY"]},
    )
    payload["simple_analysis_contract"] = {"route": "complex"}
    variables = pandas_prompt.build_variables(payload)
    helper_source = "def selected_helper(input_text, frame):\n    return frame\n"
    expected = template.format(**variables, function_case_helper_code="")
    actual = pandas_prompt.build_route_aware_pandas_prompt(payload, template, helper_source)
    assert actual == expected
    assert helper_source not in actual
    assert "해당 `source_alias`에 **먼저 적용**한다" in actual


def test_v2_complex_executor_injects_trusted_helper_and_removes_llm_shadow_definition():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"TOKEN": "SP24", "QTY": 10}, {"TOKEN": "OTHER", "QTY": 20}],
        steps=[{"operation": "apply_pandas_function_case", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["TOKEN", "QTY"]},
    )
    helper_source = (
        "def selected_helper(input_text, frame):\n"
        "    return frame[frame['TOKEN'].eq(input_text)].copy()\n"
    )
    llm_response = {
        "code": (
            "def selected_helper(input_text, frame):\n"
            "    return frame.iloc[0:0].copy()\n\n"
            "df = sources['source_1'].copy()\n"
            "result = selected_helper('SP24', df)[['TOKEN', 'QTY']].copy()"
        )
    }

    executed = executor.execute_pandas_code(
        payload,
        llm_response,
        function_case_helper_code=helper_source,
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"TOKEN": "SP24", "QTY": 10}]
    helper_trace = executed["trace"]["inspection"]["pandas_execution"]["safe_import_normalization"]
    assert helper_trace["trusted_helper_override"]["removed_generated_definitions"] == ["selected_helper"]


def test_v2_complex_executor_pretransforms_selected_function_case_before_llm_code():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[
            {"MCP_NO": "L-116A", "UPH": 112.0},
            {"MCP_NO": "OTHER", "UPH": 90.0},
        ],
        steps=[
            {
                "operation": "apply_pandas_function_case",
                "function_name": "match_product_tokens",
                "input_text": "L-116",
                "source_alias": "source_1",
            },
            {"operation": "custom_complex_operation", "source_alias": "source_1"},
        ],
        output_contract={"result_mode": "detail", "result_columns": ["MCP_NO", "UPH"]},
    )
    payload["intent_plan"]["pandas_function_cases"] = [
        {
            "function_name": "match_product_tokens",
            "input_text": "L-116",
            "source_alias": "source_1",
        }
    ]
    helper_source = (
        "def match_product_tokens(input_text, frame):\n"
        "    return frame[frame['MCP_NO'].astype(str).str.startswith(input_text)].copy()\n"
    )
    # A weak model supplied the helper arguments in reverse order.  The
    # executor must use the catalog-selected pre-transform instead of running
    # this call directly.
    executed = executor.execute_pandas_code(
        payload,
        {"code": "df = match_product_tokens(sources['source_1'], 'L-116')\nresult = df[['MCP_NO', 'UPH']].copy()"},
        function_case_helper_code=helper_source,
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"MCP_NO": "L-116A", "UPH": 112.0}]
    trace = executed["trace"]["inspection"]["pandas_execution"]
    assert trace["deterministic_source_transforms"][0]["function_name"] == "match_product_tokens"
    safe_trace = trace["safe_import_normalization"]
    assert safe_trace["selected_function_case_pre_transform"]["replacement_count"] == 1


def test_v2_complex_executor_rejects_ambiguous_function_case_source_rewrite():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"MCP_NO": "L-116A", "UPH": 112.0}],
        steps=[{"operation": "custom_complex_operation", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["MCP_NO", "UPH"]},
    )
    payload["intent_plan"]["pandas_function_cases"] = [
        {"function_name": "match_product_tokens", "input_text": "L-116", "source_alias": "source_1"},
        {"function_name": "match_product_tokens", "input_text": "L-116", "source_alias": "source_2"},
    ]
    helper_source = "def match_product_tokens(input_text, frame):\n    return frame.copy()\n"

    executed = executor.execute_pandas_code(
        payload,
        {"code": "result = match_product_tokens('L-116', sources['source_1'])"},
        function_case_helper_code=helper_source,
    )

    assert executed["analysis"]["status"] == "error"
    assert executed["analysis"]["error"]["type"] == "trusted_function_case_contract_invalid"


def test_v2_executor_ignores_unbound_effective_filter_alias():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"GROUP": "A", "QTY": 10}],
        steps=[{"operation": "custom_complex_operation", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["GROUP", "QTY"]},
    )
    payload["intent_plan"]["condition_resolution"] = {
        "effective_filters": {
            "stale_alias": {"dataset_key": "other", "filters": {"GROUP": {"operator": "eq", "value": "A"}}}
        }
    }

    assert executor._pandas_filter_plan(payload) == []


def test_v2_catalog_metric_ownership_blocks_cross_dataset_metric_relabeling():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "production",
                "payload": {
                    "columns": ["PRODUCTION"],
                    "metric_semantics": {"PRODUCTION": {"default_rollup": "sum"}},
                },
            },
            {
                "dataset_key": "eqp_uph",
                "payload": {
                    "columns": ["UPH"],
                    "metric_semantics": {"UPH": {"default_rollup": "mean"}},
                },
            },
        ]
    }
    errors = normalizer._metric_source_validation_errors(
        {
            "metric_bindings": [
                {
                    "source_alias": "production_source",
                    "dataset_key": "production",
                    "source_column": "PRODUCTION",
                    "output_column": "UPH",
                }
            ]
        },
        [{"source_alias": "production_source", "dataset_key": "production"}],
        {},
        {},
        candidates,
    )
    assert any(error["type"] == "catalog_metric_ownership_mismatch" for error in errors)

    safe_errors = normalizer._metric_source_validation_errors(
        {
            "metric_bindings": [
                {
                    "source_alias": "production_source",
                    "dataset_key": "production",
                    "source_column": "PRODUCTION",
                    "output_column": "TOTAL_PRODUCTION",
                }
            ]
        },
        [{"source_alias": "production_source", "dataset_key": "production"}],
        {},
        {},
        candidates,
    )
    assert not any(error["type"] == "catalog_metric_ownership_mismatch" for error in safe_errors)


def test_v2_repair_prompt_does_not_repeat_selected_helper_source():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"TOKEN": "SP24", "QTY": 10}],
        steps=[{"operation": "apply_pandas_function_case", "source_alias": "source_1"}],
        output_contract={"result_mode": "detail", "result_columns": ["TOKEN", "QTY"]},
    )
    helper_source = "def selected_helper(input_text, frame):\n    return frame\n"

    prompt = executor.build_pandas_repair_prompt(
        payload,
        "helper={function_case_helper_code}",
        helper_source,
    )

    assert helper_source not in prompt
    assert "executor가 안전성 검증 후 주입합니다" in prompt


def test_v2_retries_a_nonempty_invalid_llm_code_response_once():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"TOKEN": "SP24", "QTY": 10}],
        steps=[],
        output_contract={"result_mode": "detail", "result_columns": ["TOKEN", "QTY"]},
    )
    repair_prompts: list[str] = []

    repaired = executor.execute_pandas_with_repair(
        payload,
        '{"code": "result = sources[\\"source_1\\"].copy()}',
        repair_invoker=lambda prompt: repair_prompts.append(prompt)
        or '{"code": "result = sources[\\"source_1\\"][[\\"TOKEN\\", \\"QTY\\"]].copy()"}',
        repair_prompt_template="repair={repair_required}\n{function_case_helper_code}\n{failed_code}",
        max_repair_attempts=1,
    )

    assert repaired["analysis"]["status"] == "ok"
    assert repaired["trace"]["inspection"]["pandas_repair"]["attempted"] is True
    assert len(repair_prompts) == 1


def test_v2_fast_detail_query_uses_standardized_runtime_rows_when_schema_is_canonical():
    adapter = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "14_retrieval_payload_adapter.py"
    )
    payload = _single_source_payload(
        rows=[
            {
                "EQUIP_MODEL": "MODEL-A",
                "RECIPE_ID": "R0429A",
                "OPER_NM": "FCB1",
                "UPH": 100,
            }
        ],
        filters={
            "RECIPE_ID": {"operator": "starts_with", "value": "R0429"},
            "OPER_NAME": {"operator": "eq", "value": "FCB1"},
        },
        filter_mappings={
            "EQP_MODEL": ["EQUIP_MODEL"],
            "OPER_NAME": ["OPER_NM"],
            "RECIPE_ID": ["RECIPE_ID"],
            "UPH": ["UPH"],
        },
        steps=[
            {"operation": "apply_filters", "source_alias": "eqp_uph"},
            {"operation": "select_columns", "source_alias": "eqp_uph"},
        ],
        output_contract={
            "result_mode": "detail",
            "projection": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
            "result_columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
            "required_columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
            "strict_result_columns": True,
        },
        source_alias="eqp_uph",
        dataset_key="eqp_uph",
    )
    # The retriever schema is canonical, but its unprojected runtime rows are
    # still physical. This is the production failure shape.
    payload["source_results"][0]["columns"] = [
        "EQP_MODEL",
        "RECIPE_ID",
        "OPER_NAME",
        "UPH",
    ]

    adapted = adapter.build_retrieval_payload(payload)
    resolved, executed, model_calls = _resolve_and_execute(adapted)

    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "fast"
    assert executed["data"]["rows"] == [
        {
            "EQP_MODEL": "MODEL-A",
            "RECIPE_ID": "R0429A",
            "OPER_NAME": "FCB1",
            "UPH": 100,
        }
    ]
    assert model_calls == []


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
    checkpoints = executed["intermediate_results"]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint["role"] == "filtered_source"
    assert checkpoint["row_count"] == 3
    assert checkpoint["preview_rows"] == [
        {"OPER_NAME": "A", "QTY": 10},
        {"OPER_NAME": "B", "QTY": 20},
        {"OPER_NAME": "B", "QTY": 15},
    ]
    assert executed["_intermediate_download_rows"]["last_successful"]["rows"] == checkpoint["preview_rows"]
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


def test_complex_answer_llm_off_does_not_materialize_answer_prompt(monkeypatch):
    _, _, answer = _modules()
    payload = {
        "request": {"question": "그룹별 수량을 알려줘"},
        "simple_analysis_contract": {"route": "complex"},
        "intent_plan": {
            "output_contract": {
                "primary_metric": "QTY_SUM",
                "grain_columns": ["GROUP"],
                "metric_columns": ["QTY_SUM"],
            }
        },
        "analysis": {"status": "ok", "execution_route": "complex", "row_count": 1},
        "data": {"columns": ["GROUP", "QTY_SUM"], "rows": [{"GROUP": "A", "QTY_SUM": 10}], "row_count": 1},
        "trace": {"inspection": {"fast_path": {"llm_calls": {"intent": 1, "pandas_generation": 1, "repair": 0, "answer": 0}}}},
    }

    def fail_if_materialized(*_args, **_kwargs):
        raise AssertionError("answer LLM OFF must not serialize AnswerEvidence or render a prompt")

    monkeypatch.setattr(answer, "build_lazy_llm_answer_prompt", fail_if_materialized)
    result = answer.build_hybrid_answer_response(
        payload,
        model_invoker=lambda _prompt: (_ for _ in ()).throw(AssertionError("model must not be called")),
        use_llm_answer=False,
        answer_prompt_template="{result_summary_json}",
    )

    assert result["trace"]["inspection"]["answer_model_response"]["policy"] == "deterministic_complex_answer"
    assert result["trace"]["inspection"]["fast_path"]["llm_calls"]["answer"] == 0


def test_complex_answer_evidence_is_bounded_without_changing_runtime_payload():
    _, _, answer = _modules()
    columns = [f"COL_{index:02d}" for index in range(20)]
    rows = [
        {column: ("x" * 400 if column == "COL_00" else f"{column}-{row_index}") for column in columns}
        for row_index in range(12)
    ]
    payload = {
        "request": {"question": "상세 결과를 알려줘"},
        "simple_analysis_contract": {"route": "complex"},
        "intent_plan": {
            "analysis_kind": "generic_detail",
            "pandas_execution_plan": [{"operation": "select_columns"}],
            "output_contract": {
                "grain_columns": columns[:10],
                "metric_columns": columns[10:18],
                "primary_metric": "COL_10",
                "result_columns": columns,
            },
        },
        "source_results": [{"dataset_key": "sample", "source_alias": "sample", "status": "ok", "row_count": 12}],
        "analysis": {"status": "ok", "execution_route": "complex", "row_count": 12, "columns": columns},
        "data": {
            "columns": columns,
            "rows": deepcopy(rows),
            "row_count": 12,
            "data_ref": {"ref_id": "result:keep", "download_url": "https://example.invalid/download"},
        },
        "_full_result_rows": deepcopy(rows),
        "trace": {
            "warnings": [{"type": "demo", "message": "w" * 900, "traceback_summary": "do-not-send"}],
            "errors": [],
            "inspection": {"fast_path": {"llm_calls": {"intent": 1, "pandas_generation": 1, "repair": 0, "answer": 0}}},
        },
    }
    variables = answer.build_answer_evidence_variables(payload)
    result_view = json.loads(variables["result_summary_json"])
    diagnostics = json.loads(variables["warnings_errors_json"])

    assert len(result_view["rows"]) == 5
    assert len(result_view["columns"]) == 16
    assert len(result_view["rows"][0]["COL_00"]) == 160
    assert "data_ref" not in variables["result_summary_json"]
    assert "download_url" not in variables["result_summary_json"]
    assert "traceback_summary" not in variables["warnings_errors_json"]
    assert len(diagnostics["warnings"][0]["message"]) == 600

    prompts: list[str] = []
    result = answer.build_hybrid_answer_response(
        payload,
        model_invoker=lambda prompt: prompts.append(prompt) or '{"answer_message":"요청한 결과를 확인했습니다."}',
        use_llm_answer=True,
        answer_prompt_template=(
            "Q={question}\nR={result_summary_json}\nS={applied_scope_json}\n"
            "C={answer_context_json}\nD={warnings_errors_json}\nG={domain_answer_guidance}"
        ),
        domain_answer_guidance="공통 지침",
    )

    assert len(prompts) == 1
    assert "result:keep" not in prompts[0]
    assert len(result["data"]["rows"]) == 12
    assert result["data"]["data_ref"]["ref_id"] == "result:keep"
    assert len(result["_full_result_rows"]) == 12
    response_trace = result["trace"]["inspection"]["answer_model_response"]
    assert response_trace["answer_evidence_row_limit"] == 5
    assert response_trace["answer_evidence_column_limit"] == 16
    assert response_trace["answer_evidence_cell_limit"] == 160


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
        show_intent_analysis=True,
        show_data_retrieval=False,
        show_pandas_code=True,
    )
    assert "예상 실행 경로: `Fast 후보`" in message
    assert "최종 실행 경로: `Fast`" in message
    assert "실행된 Fast 고정 로직" in message
    assert "_execute_fast_path_recipe" in message
    assert "_fast_aggregate" in message


def test_v2_fast_contract_error_keeps_last_available_checkpoint_and_download_rows():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"LOT_ID": "L1", "QTY": 3}],
        steps=[],
        output_contract={"result_mode": "detail", "result_columns": ["LOT_ID", "MISSING_COL"]},
    )
    payload["simple_analysis_contract"] = {
        "strict": True,
        "route": "fast",
        "operation": "execute_fast_path_recipe",
        "recipe": "detail_query",
        "source_alias": "source_1",
        "projection": ["LOT_ID", "MISSING_COL"],
        "result_columns": ["LOT_ID", "MISSING_COL"],
    }
    result = executor.execute_pandas_code(payload, "")
    assert result["analysis"]["status"] == "error"
    checkpoints = result.get("intermediate_results") or result["analysis"].get("intermediate_results")
    assert checkpoints
    assert len(checkpoints) == 1
    assert checkpoints[0]["role"] == "source_input"
    assert checkpoints[0]["row_count"] == 1
    assert checkpoints[0]["preview_rows"][0]["LOT_ID"] == "L1"
    artifact = result["_intermediate_download_rows"]["last_successful"]
    assert artifact["rows"] == [{"LOT_ID": "L1", "QTY": 3}]
    assert result["data"]["partial"] is True
    assert result["data"]["stage"] == "source:source_1"


def test_v2_intermediate_success_falls_back_to_source_when_calculation_matches_final_result():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[
            {"DATE": "20260809", "OPER_NAME": "INPUT", "MCP_NO": "L-267A1", "PRODUCTION": 300},
            {"DATE": "20260809", "OPER_NAME": "INPUT", "MCP_NO": "M-001", "PRODUCTION": 100},
            {"DATE": "20260808", "OPER_NAME": "INPUT", "MCP_NO": "L-267A2", "PRODUCTION": 200},
        ],
        steps=[],
        filters={
            "OPER_NAME": {"operator": "eq", "value": "INPUT"},
            "MCP_NO": {"operator": "starts_with", "value": "L-267"},
            "DATE": {"operator": "eq", "value": "20260809"},
        },
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["OPER_NAME", "MCP_NO", "DATE"],
            "metric_columns": ["PRODUCTION"],
            "result_columns": ["OPER_NAME", "MCP_NO", "DATE", "PRODUCTION"],
        },
    )
    payload["intent_plan"]["retrieval_jobs"][0]["required_params"] = {"DATE": "20260809"}
    executed = executor.execute_pandas_code(
        payload,
        """
df = sources[\"source_1\"].copy()
result = df.groupby([\"OPER_NAME\", \"MCP_NO\", \"DATE\"], dropna=False)[\"PRODUCTION\"].sum().reset_index()
""",
    )

    assert executed["analysis"]["status"] == "ok"
    checkpoints = executed["intermediate_results"]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    # The filtered frame is identical to the final aggregate in this case, so
    # show the unfiltered source as the only informative intermediate result.
    assert checkpoint["role"] == "source_input"
    assert checkpoint["row_count"] == 3
    assert checkpoint["description"].startswith("최종 집계 전 중간 데이터")
    assert executed["_intermediate_download_rows"]["last_successful"]["rows"] == [
        {"DATE": "20260809", "OPER_NAME": "INPUT", "MCP_NO": "L-267A1", "PRODUCTION": 300},
        {"DATE": "20260809", "OPER_NAME": "INPUT", "MCP_NO": "M-001", "PRODUCTION": 100},
        {"DATE": "20260808", "OPER_NAME": "INPUT", "MCP_NO": "L-267A2", "PRODUCTION": 200},
    ]


def test_v2_intermediate_success_keeps_calculation_when_final_contract_changes_columns():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[
            {"DATE": "20260809", "OPER_NAME": "INPUT", "MCP_NO": "L-267A1", "PRODUCTION": 300},
        ],
        steps=[],
        filters={
            "OPER_NAME": {"operator": "eq", "value": "INPUT"},
            "MCP_NO": {"operator": "starts_with", "value": "L-267"},
            "DATE": {"operator": "eq", "value": "20260809"},
        },
        output_contract={
            "result_mode": "aggregate",
            "grain_columns": ["OPER_NAME", "MCP_NO", "DATE"],
            "metric_columns": ["PRODUCTION"],
            "result_columns": ["OPER_NAME", "MCP_NO", "DATE", "PRODUCTION"],
            "strict_result_columns": True,
        },
    )
    payload["intent_plan"]["retrieval_jobs"][0]["required_params"] = {"DATE": "20260809"}
    executed = executor.execute_pandas_code(
        payload,
        """
df = sources["source_1"].copy()
result = df.groupby(["OPER_NAME", "MCP_NO", "DATE"], dropna=False)["PRODUCTION"].sum().reset_index()
result["INTERNAL_NOTE"] = "계약 전 보조 컬럼"
""",
    )

    assert executed["analysis"]["status"] == "ok"
    checkpoints = executed["intermediate_results"]
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint["role"] == "computed_result"
    assert checkpoint["description"] == "최종 계약 적용 전 계산 결과 (OPER_NAME, MCP_NO, DATE 필터 적용 후)"
    assert checkpoint["preview_rows"][0]["INTERNAL_NOTE"] == "계약 전 보조 컬럼"
    assert executed["data"]["columns"] == ["OPER_NAME", "MCP_NO", "DATE", "PRODUCTION"]
    artifact = executed["_intermediate_download_rows"]["last_successful"]
    assert artifact["label"] == checkpoint["description"]
    assert artifact["rows"][0]["INTERNAL_NOTE"] == "계약 전 보조 컬럼"


def test_v2_multi_source_intermediate_results_keep_each_filtered_source_and_pre_contract_join():
    _, executor, _ = _modules()
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "production",
                    "source_alias": "production_src",
                    "required_params": {"DATE": "20260809"},
                    "filters": {"OPER_NAME": {"operator": "eq", "value": "INPUT"}},
                },
                {
                    "dataset_key": "eqp_uph",
                    "source_alias": "eqp_uph_src",
                    "required_params": {"DATE": "20260809"},
                    "filters": {"OPER_NAME": {"operator": "eq", "value": "INPUT"}},
                },
            ],
            "output_contract": {
                "result_columns": ["EQP_MODEL", "TOTAL_PRODUCTION"],
                "metric_columns": ["TOTAL_PRODUCTION"],
            },
        }
    }
    checkpoints = [
        {"key": "filtered:production_src", "role": "filtered_source"},
        {"key": "filtered:eqp_uph_src", "role": "filtered_source"},
        {"key": "computed_result", "role": "computed_result"},
    ]
    values = {
        "filtered:production_src": [
            {"EQP_ID": "EQ1", "OPER_NAME": "INPUT", "PRODUCTION": 300}
        ],
        "filtered:eqp_uph_src": [
            {"EQP_ID": "EQ1", "OPER_NAME": "INPUT", "EQP_MODEL": "M1", "UPH": 120}
        ],
        "computed_result": [
            {"EQP_MODEL": "M1", "TOTAL_PRODUCTION": 300, "INTERNAL_JOIN_KEY": "EQ1"}
        ],
    }

    visible, artifacts, metadata = executor._project_intermediate_checkpoint(
        checkpoints,
        values,
        payload,
        [],
        completed=True,
        final_rows=[{"EQP_MODEL": "M1", "TOTAL_PRODUCTION": 300}],
        final_columns=["EQP_MODEL", "TOTAL_PRODUCTION"],
    )

    assert [item["download_key"] for item in visible] == [
        "source_production_src",
        "source_eqp_uph_src",
        "pre_contract_result",
    ]
    assert [item["description"] for item in visible] == [
        "production — 최종 집계 전 중간 데이터 (OPER_NAME, DATE 필터 적용 후)",
        "eqp_uph — 최종 집계 전 중간 데이터 (OPER_NAME, DATE 필터 적용 후)",
        "결합·계산 결과 (최종 계약 적용 전)",
    ]
    assert artifacts["source_production_src"]["rows"][0]["PRODUCTION"] == 300
    assert artifacts["source_eqp_uph_src"]["rows"][0]["UPH"] == 120
    assert artifacts["pre_contract_result"]["rows"][0]["INTERNAL_JOIN_KEY"] == "EQ1"
    assert set(metadata) == set(artifacts)


def test_v2_message_adapter_renders_each_published_multi_source_checkpoint():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "analysis": {"status": "ok"},
        "intermediate_results": [
            {
                "download_key": "source_production",
                "description": "production — 최종 집계 전 중간 데이터",
                "role": "filtered_source",
                "row_count": 1,
                "columns": ["EQP_ID", "PRODUCTION"],
                "preview_rows": [{"EQP_ID": "EQ1", "PRODUCTION": 300}],
            },
            {
                "download_key": "source_eqp_uph",
                "description": "eqp_uph — 최종 집계 전 중간 데이터",
                "role": "filtered_source",
                "row_count": 1,
                "columns": ["EQP_ID", "UPH"],
                "preview_rows": [{"EQP_ID": "EQ1", "UPH": 120}],
            },
            {
                "download_key": "pre_contract_result",
                "description": "결합·계산 결과 (최종 계약 적용 전)",
                "role": "computed_result",
                "row_count": 1,
                "columns": ["EQP_MODEL", "TOTAL_PRODUCTION"],
                "preview_rows": [{"EQP_MODEL": "M1", "TOTAL_PRODUCTION": 300}],
            },
        ],
    }

    message = adapter.build_message(
        payload,
        show_intermediate_results=True,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
        show_intent_analysis=False,
        show_data_retrieval=False,
        show_pandas_code=False,
    )

    assert "#### production — 최종 집계 전 중간 데이터" in message
    assert "#### eqp_uph — 최종 집계 전 중간 데이터" in message
    assert "#### 결합·계산 결과 (최종 계약 적용 전)" in message
    assert message.count("| EQP_ID |") == 2


def test_v2_message_adapter_keeps_curated_intermediate_results_but_hides_raw_evidence_and_body_followups():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "answer_message": "분석을 완료했습니다.",
        "answer_sections": {
            "summary": {"headline": "분석을 완료했습니다."},
            "next_questions": ["제품별로 더 나눠볼까요?"],
        },
        "analysis": {
            "step_outputs": [{"key": "raw_step", "row_count": 99, "preview_rows": [{"RAW": "x"}]}],
            "function_case_results": [{"function_name": "sample_helper", "matched_count": 99}],
        },
        "intermediate_results": [
            {
                "download_key": "pre_contract_result",
                "description": "최종 집계 전 중간 데이터",
                "role": "computed_result",
                "row_count": 1,
                "columns": ["DEVICE", "QTY"],
                "preview_rows": [{"DEVICE": "DEV-A", "QTY": 10}],
            }
        ],
        "data": {"columns": ["DEVICE", "QTY"], "rows": [{"DEVICE": "DEV-A", "QTY": 10}], "row_count": 1},
    }

    message = adapter.build_message(
        payload,
        show_intermediate_results=True,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "### 중간 결과" in message
    assert "최종 집계 전 중간 데이터" in message
    assert "### 중간 분석 산출물" not in message
    assert "### helper 실행 결과" not in message
    assert "### 다음에 볼 만한 질문" not in message
    assert adapter.build_response_metadata(payload)["followup_questions"] == [
        {"type": "followup_question", "id": "followup-1", "value": "제품별로 더 나눠볼까요?"}
    ]


def test_result_store_creates_one_download_ref_per_multi_source_checkpoint():
    store = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py"
    )
    payload = {
        "data": {"columns": ["EQP_MODEL"], "row_count": 1},
        "runtime_sources": {},
        "_intermediate_download_rows": {
            "source_production_src": {
                "rows": [{"EQP_ID": "EQ1", "PRODUCTION": 300}],
                "columns": ["EQP_ID", "PRODUCTION"],
                "label": "production — 최종 집계 전 중간 데이터",
                "role": "filtered_source",
                "checkpoint_key": "filtered:production_src",
            },
            "source_eqp_uph_src": {
                "rows": [{"EQP_ID": "EQ1", "UPH": 120}],
                "columns": ["EQP_ID", "UPH"],
                "label": "eqp_uph — 최종 집계 전 중간 데이터",
                "role": "filtered_source",
                "checkpoint_key": "filtered:eqp_uph_src",
            },
            "pre_contract_result": {
                "rows": [{"EQP_MODEL": "M1", "TOTAL_PRODUCTION": 300}],
                "columns": ["EQP_MODEL", "TOTAL_PRODUCTION"],
                "label": "결합·계산 결과 (최종 계약 적용 전)",
                "role": "computed_result",
                "checkpoint_key": "computed_result",
            },
        },
    }
    refs = store._build_data_refs(
        payload,
        "result:session:0123456789abcdef0123456789abcdef",
        "datagov",
        "agent_v4_result_store",
    )

    intermediate_paths = {
        ref["path"]
        for ref in refs
        if ref.get("role") == "intermediate_result"
    }
    assert intermediate_paths == {
        "payload.intermediate_rows.source_production_src",
        "payload.intermediate_rows.source_eqp_uph_src",
        "payload.intermediate_rows.pre_contract_result",
    }


def test_v2_contract_error_after_calculation_keeps_computed_checkpoint():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"LOT_ID": "L1", "QTY": 3}],
        steps=[],
        output_contract={
            "result_mode": "detail",
            "result_columns": ["LOT_ID"],
            "strict_result_columns": True,
            "result_segments": [{"name": "left"}, {"name": "right"}],
            "segment_column": "RESULT_GROUP",
        },
    )
    result = executor.execute_pandas_code(
        payload,
        "result = sources[\"source_1\"][[\"LOT_ID\"]].copy()",
    )

    assert result["analysis"]["status"] == "error"
    assert result["intermediate_results"][0]["role"] == "computed_result"
    assert result["_intermediate_download_rows"]["last_successful"]["rows"] == [{"LOT_ID": "L1"}]


def test_v2_fast_detail_projection_overrides_stale_catalog_defaults():
    _, executor, _ = _modules()
    rows = [
        {
            "LOT_ID": "L1",
            "PROD_QTY": 10,
            "WF_QTY": 20,
            "CUM_TAT": 3,
            "OPER_NAME": "W/B1",
            "HOLD_STAT": "OnHold",
            "HOLD_REASON": "R1",
            "LOT_STAT": "HOLD",
        }
    ]
    payload = _single_source_payload(
        rows=rows,
        steps=[],
        output_contract={
            "result_mode": "detail",
            "required_columns": [
                "LOT_ID",
                "PROD_QTY",
                "WF_QTY",
                "CUM_TAT",
                "OPER_NAME",
                "HOLD_STAT",
                "HOLD_REASON",
                "LOT_STAT",
            ],
            "result_columns": [
                "LOT_ID",
                "PROD_QTY",
                "WF_QTY",
                "CUM_TAT",
                "OPER_NAME",
                "HOLD_STAT",
                "HOLD_REASON",
                "LOT_STAT",
            ],
        },
    )
    payload["simple_analysis_contract"] = {
        "strict": True,
        "route": "fast",
        "operation": "execute_fast_path_recipe",
        "recipe": "detail_query",
        "source_alias": "source_1",
        "projection": ["LOT_ID", "PROD_QTY", "WF_QTY", "CUM_TAT"],
        # Simulate an older/stale resolver carrying catalog defaults.
        "result_columns": [
            "LOT_ID",
            "PROD_QTY",
            "WF_QTY",
            "CUM_TAT",
            "OPER_NAME",
            "HOLD_STAT",
            "HOLD_REASON",
            "LOT_STAT",
        ],
    }
    result = executor.execute_pandas_code(payload, "")
    assert result["analysis"]["status"] == "ok"
    assert result["data"]["columns"] == ["LOT_ID", "PROD_QTY", "WF_QTY", "CUM_TAT"]
    assert result["intent_plan"]["output_contract"]["required_columns"] == [
        "LOT_ID",
        "PROD_QTY",
        "WF_QTY",
        "CUM_TAT",
    ]


def test_v2_fast_detail_without_projection_drops_unavailable_catalog_defaults():
    _, executor, _ = _modules()
    rows = [
        {"LOT_ID": "L1", "PROD_QTY": 10, "WF_QTY": 20, "CUM_TAT": 3},
    ]
    payload = _single_source_payload(rows=rows, steps=[], output_contract={"result_mode": "detail"})
    payload["simple_analysis_contract"] = {
        "strict": True,
        "route": "fast",
        "operation": "execute_fast_path_recipe",
        "recipe": "detail_query",
        "source_alias": "source_1",
        # Simulate a weak resolver that copied the catalog detail defaults
        # but did not produce a concrete select/projection step.
        "result_columns": [
            "LOT_ID",
            "PROD_QTY",
            "WF_QTY",
            "CUM_TAT",
            "OPER_NAME",
            "IN_TAT",
            "HOLD_STAT",
            "HOLD_REASON",
            "LOT_STAT",
        ],
    }
    result = executor.execute_pandas_code(payload, "")
    assert result["analysis"]["status"] == "ok"
    assert result["data"]["columns"] == ["LOT_ID", "PROD_QTY", "WF_QTY", "CUM_TAT"]
    assert result["intent_plan"]["output_contract"]["contract_reconciliation"]["policy"] == (
        "available_detail_columns_own_shape"
    )


def test_v2_message_adapter_can_show_intermediate_results_without_prompt_context():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "answer_message": "조회 결과를 확인했습니다.",
        "data": {"columns": ["LOT_ID"], "rows": [{"LOT_ID": "L1"}], "row_count": 1},
        "intermediate_results": [
            {
                "key": "source:lot_status",
                "role": "source_input",
                "description": "조회된 원본 데이터",
                "row_count": 8,
                "columns": ["LOT_ID", "HOLD_STAT"],
                "preview_rows": [
                    {"LOT_ID": "L1", "HOLD_STAT": "OnHold"},
                    {"LOT_ID": "L2", "HOLD_STAT": "OnHold"},
                    {"LOT_ID": "L3", "HOLD_STAT": "OnHold"},
                    {"LOT_ID": "L4", "HOLD_STAT": "OnHold"},
                    {"LOT_ID": "L5", "HOLD_STAT": "OnHold"},
                    {"LOT_ID": "L6", "HOLD_STAT": "OnHold"},
                ],
            }
        ],
    }
    hidden = adapter.build_message(payload, show_intermediate_results=False)
    visible = adapter.build_message(
        payload,
        show_intermediate_results=True,
        intermediate_preview_limit=2,
    )
    assert "중간 결과" not in hidden
    assert "중간 결과" in visible
    assert "조회된 원본 데이터" in visible
    assert "#### 조회된 원본 데이터" in visible
    assert "전체 8건 중 2건을 표시했습니다." in visible
    assert "\n\n| LOT_ID | HOLD_STAT |" in visible
    assert "L1" in visible and "L2" in visible
    assert "L3" not in visible

    # A large UI value never exposes more rows than the executor retained.
    capped = adapter.build_message(
        payload,
        show_intermediate_results=True,
        intermediate_preview_limit=99,
    )
    assert "전체 8건 중 5건을 표시했습니다." in capped
    assert "L5" in capped
    assert "L6" not in capped


def test_common_hydrator_does_not_add_catalog_defaults_to_explicit_detail_shape():
    hydrator = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "04a_trusted_retrieval_job_hydrator.py")
    contract = hydrator._output_contract_with_default_detail(
        {
            "result_mode": "detail",
            "required_columns": ["LOT_ID", "PROD_QTY", "WF_QTY", "CUM_TAT"],
            "result_columns": ["LOT_ID", "PROD_QTY", "WF_QTY", "CUM_TAT"],
            "strict_result_columns": True,
        },
        [{"default_detail_columns": ["OPER_NAME", "HOLD_REASON", "LOT_STAT"]}],
    )
    assert contract["required_columns"] == ["LOT_ID", "PROD_QTY", "WF_QTY", "CUM_TAT"]
    assert "HOLD_REASON" not in contract["required_columns"]


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


def test_v2_normalizer_reconciles_current_scope_for_comparison_without_aggregation():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    columns = ["TECH", "DEN", "PKG_TYPE2", "MCP_NO", "MODE", "PKG_TYPE1", "LEAD"]
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "production",
                "payload": {
                    "dataset_family": "production",
                    "time_scope": "history",
                    "columns": columns,
                    "required_params": ["DATE"],
                },
            },
            {
                "dataset_key": "production_today",
                "payload": {
                    "dataset_family": "production",
                    "time_scope": "current_day",
                    "columns": columns,
                    "required_params": ["DATE"],
                },
            },
            {
                "dataset_key": "lot_status",
                "payload": {
                    "dataset_family": "lot",
                    "time_scope": "current_day",
                    "columns": columns,
                    "required_params": ["DATE"],
                },
            },
        ]
    }
    jobs, trace = normalizer._reconcile_metric_dataset_selection(
        {
            "request": {
                "question": "현재 제품 중 TECH, DEN, PKG_TYPE2, MCP_NO는 같지만 MODE, PKG_TYPE1 또는 LEAD가 다른 제품들",
                "reference_date": "20260701",
            }
        },
        [
            {
                "dataset_key": "production",
                "source_alias": "production_source",
                "required_params": {"DATE": "20260701"},
            }
        ],
        [
            {
                "operation": "compare_group_attributes",
                "inputs": [{"kind": "external_source", "ref": "production_source"}],
                "group_by": ["TECH", "DEN", "PKG_TYPE2", "MCP_NO"],
                "comparison_columns": ["MODE", "PKG_TYPE1", "LEAD"],
            }
        ],
        candidates,
    )
    assert jobs[0]["dataset_key"] == "production_today"
    assert trace["status"] == "applied"
    assert trace["corrections"][0]["requested_time_scope"] == "current_day"


def test_v2_normalizer_keeps_schema_capable_llm_source_and_records_catalog_advisory():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    columns = ["TECH", "DEN", "PKG_TYPE2", "MCP_NO", "MODE", "PKG_TYPE1", "LEAD"]
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "production_today",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {
                        "time_scope": "current_day",
                        "use_when": ["current production"],
                    },
                    "columns": columns,
                },
            },
            {
                "dataset_key": "lot_status",
                "payload": {
                    "dataset_family": "wip",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": columns,
                },
            },
        ]
    }
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {
            "request": {"question": "current product comparison"},
        },
        [{"dataset_key": "lot_status", "source_alias": "product_source"}],
        [
            {
                "operation": "compare_group_attributes",
                "source_alias": "product_source",
                "group_by": ["TECH", "DEN", "PKG_TYPE2", "MCP_NO"],
                "comparison_columns": ["MODE", "PKG_TYPE1", "LEAD"],
            }
        ],
        candidates,
        [],
    )
    assert jobs[0]["dataset_key"] == "lot_status"
    assert trace["status"] == "advisory"
    assert trace["corrections"] == []
    assert trace["advisories"][0]["candidate_dataset_key"] == "production_today"
    assert trace["advisories"][0]["reason"] == "semantic_candidate_not_forced"


def test_metadata_candidates_expose_bounded_catalog_selection_hints_without_source_lock():
    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )
    result = builder.build_metadata_candidates(
        {"request": {"question": "장비별 UPH 평균을 알려줘"}},
        [],
        [
            {
                "section": "table_catalog",
                "key": "eqp_uph",
                "dataset_key": "eqp_uph",
                "payload": {
                    "selection_criteria": {
                        "use_when": ["장비별 UPH를 조회하거나 평균 UPH를 비교할 때"],
                        "exclude_when": ["생산 수량만 조회할 때"],
                    },
                    "metric_semantics": {"UPH": {"default_rollup": "mean"}},
                },
            }
        ],
        [],
        min_table_items=1,
        max_table_items=1,
    )

    candidate = result["metadata_candidates"]["table_catalog_items"][0]
    hint = candidate["intent_selection_hint"]
    assert hint["metric_columns"] == ["UPH"]
    assert hint["metric_default_rollups"] == {"UPH": "mean"}
    assert hint["use_when"] == ["장비별 UPH를 조회하거나 평균 UPH를 비교할 때"]
    assert "dataset_key" not in hint


def test_v2_normalizer_switches_to_unique_schema_capable_source():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "production",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {"time_scope": "history"},
                    "columns": ["DATE", "PRODUCTION"],
                },
            },
            {
                "dataset_key": "target",
                "payload": {
                    "dataset_family": "target",
                    "selection_criteria": {"time_scope": "history"},
                    "columns": ["DATE", "INPUT_PLAN_QTY", "OUT_PLAN_QTY"],
                },
            },
        ]
    }
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {"request": {"question": "생산 계획"}},
        [{"dataset_key": "production", "source_alias": "plan_source"}],
        [
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "plan_source",
                "group_by": ["DATE"],
                "aggregations": [
                    {"column": "INPUT_PLAN_QTY", "method": "sum", "output_column": "INPUT_PLAN_QTY"},
                    {"column": "OUT_PLAN_QTY", "method": "sum", "output_column": "OUT_PLAN_QTY"},
                ],
            }
        ],
        candidates,
        [],
    )
    assert jobs[0]["dataset_key"] == "target"
    assert trace["corrections"][0]["selection_source"] == "table_catalog.unique_schema_contract"


def test_v2_normalizer_keeps_schema_capable_identity_choice_and_records_advisory():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    columns = ["TECH", "DEN", "PKG_TYPE2", "MCP_NO", "MODE", "PKG_TYPE1", "LEAD"]
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "status_population",
                "payload": {
                    "dataset_family": "population",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": [*columns, "LOT_ID", "EQP_ID"],
                },
            },
            {
                "dataset_key": "product_population",
                "payload": {
                    "dataset_family": "population",
                    "selection_criteria": {"time_scope": "current_day"},
                    "columns": columns,
                },
            },
        ]
    }
    jobs, trace = normalizer._reconcile_source_dataset_selection(
        {"request": {"question": "현재 제품 중 그룹별 속성이 다른 제품"}},
        [{"dataset_key": "status_population", "source_alias": "product_source"}],
        [
            {
                "operation": "compare_group_attributes",
                "source_alias": "product_source",
                "group_by": ["TECH", "DEN", "PKG_TYPE2", "MCP_NO"],
                "comparison_columns": ["MODE", "PKG_TYPE1", "LEAD"],
            }
        ],
        candidates,
        [],
    )
    assert jobs[0]["dataset_key"] == "status_population"
    assert trace["status"] == "advisory"
    assert trace["advisories"][0]["candidate_dataset_key"] == "product_population"


def test_v2_normalizer_recovers_omitted_token_function_case_from_metadata():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, trace = normalizer._auto_select_metadata_function_case(
        {},
        [{"dataset_key": "production", "source_alias": "production_source"}],
        {
            "domain_items": [
                {
                    "section": "pandas_function_cases",
                    "key": "product_token_match",
                    "payload": {
                        "function_name": "match_product_tokens",
                        "description": "Select when product attributes are expressed as tokens.",
                        "input_contract": {"input_text": "token bundle"},
                    },
                }
            ]
        },
        "FCB production SP 24G GDDR7 X32 226 FCBGA DDP",
    )
    assert plan["pandas_function_cases"][0]["function_name"] == "match_product_tokens"
    assert plan["pandas_function_cases"][0]["source_alias"] == "production_source"
    assert trace["status"] == "applied"


def test_v2_normalizer_does_not_activate_token_helper_for_column_comparison():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, trace = normalizer._auto_select_metadata_function_case(
        {},
        [{"dataset_key": "production_today", "source_alias": "product_source"}],
        {
            "domain_items": [
                {
                    "section": "pandas_function_cases",
                    "key": "product_token_match",
                    "payload": {
                        "function_name": "match_product_tokens",
                        "description": "Select when product attributes are expressed as tokens.",
                        "input_contract": {"input_text": "token bundle"},
                    },
                }
            ],
            "table_catalog_items": [
                {
                    "dataset_key": "production_today",
                    "payload": {
                        "columns": [
                            "TECH",
                            "DEN",
                            "MODE",
                            "PKG_TYPE1",
                            "PKG_TYPE2",
                            "LEAD",
                            "MCP_NO",
                        ]
                    },
                }
            ],
        },
        "현재 제품 중 TECH, DEN, PKG_TYPE2, MCP_NO는 같지만 MODE, PKG_TYPE1 또는 LEAD가 다른 제품",
    )
    assert "pandas_function_cases" not in plan
    assert trace["status"] == "not_needed"


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
        show_intent_analysis=False,
        show_data_retrieval=False,
        show_pandas_code=True,
    )
    assert "LLM 응답 해석" not in message


def test_v2_message_adapter_shows_invalid_llm_attempt_as_non_executed_code_excerpt():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "intent_plan": {"pandas_execution_plan": []},
        "analysis": {
            "status": "error",
            "execution_route": "complex",
            "error": {"type": "missing_code", "message": "pandas code is unavailable"},
        },
        "trace": {
            "inspection": {
                "pandas_execution": {
                    "status": "error",
                    "execution_mode": "llm_generated_code",
                    "error": {"type": "missing_code", "message": "pandas code is unavailable"},
                    "llm_response_parse": {
                        "mode": "invalid",
                        "error": "unterminated string literal",
                        "raw_response_preview": '{"code": "def selected_helper(...',
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
        show_intent_analysis=False,
        show_data_retrieval=False,
        show_pandas_code=True,
    )

    assert "LLM 생성 시도 원문 (형식 오류로 실행하지 않음; 일부만 표시)" in message
    assert "```text" in message
    assert 'def selected_helper' in message


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


def _followup_catalog_candidates():
    return {
        "domain_items": [],
        "table_catalog_items": [
            {
                "dataset_key": "lot_status",
                "source_type": "oracle",
                "payload": {
                    "columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
                    "default_detail_columns": [
                        "LOT_ID",
                        "OPER_NAME",
                        "HOLD_STAT",
                        "HOLD_REASON",
                    ],
                },
            },
            {
                "dataset_key": "hold_history",
                "source_type": "oracle",
                "payload": {
                    "columns": ["LOT_ID", "OPER_NAME", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
                    "required_params": ["LOT_ID"],
                    "default_detail_columns": [
                        "LOT_ID",
                        "OPER_NAME",
                        "HOLD_TM",
                        "HOLD_CD",
                        "HOLD_DESC",
                    ],
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "entity_type": "lot",
                                "source_alias": "previous_result",
                                "source_column": "LOT_ID",
                                "target_param": "LOT_ID",
                                "operator": "in",
                                "max_values": 200,
                            }
                        ]
                    },
                },
            },
        ],
        "main_flow_filters": [],
    }


def test_v2_followup_contract_compiles_previous_lot_rows_to_dependent_history_catalog():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    payload = {
        "request": {"question": "지금 조회된 LOT의 HOLD이력을 알려줄래?"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_requery",
            "reuse_strategy_hint": "previous_result",
            "matched_cues": {"previous_entity_identifiers": ["LOT_ID"]},
        },
        "state": {
            "current_data": {
                "columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
                "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
                "source_aliases": ["lot_status_src"],
                "source_columns_by_alias": {
                    "lot_status_src": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"]
                },
            },
            "last_intent_plan": {
                "output_contract": {"grain_columns": ["LOT_ID"]},
            },
        },
        "trace": {"inspection": {}},
    }
    response = {
        "intent_plan": {
            "analysis_kind": "current_hold_lot_history_status",
            "request_scope": "followup_requery",
            "reference_mode": "none",
            "retrieval_jobs": [
                {
                    "dataset_key": "lot_status",
                    "source_alias": "lot_status_src",
                    "filters": {"HOLD_STAT": {"operator": "eq", "value": "OnHold"}},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_old_status",
                    "operation": "apply_filters",
                    "inputs": [{"kind": "external_source", "ref": "lot_status_src"}],
                    "source_alias": "lot_status_src",
                },
                {
                    "node_id": "select_old_status",
                    "operation": "select_columns",
                    "inputs": [{"kind": "node_output", "ref": "filter_old_status"}],
                    "source_alias": "lot_status_src",
                    "output_alias": "old_result",
                },
            ],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["LOT_ID", "OPER_NAME", "HOLD_STAT", "HOLD_REASON"],
            },
        }
    }
    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response),
        _followup_catalog_candidates(),
    )
    plan = normalized["intent_plan"]
    assert plan["reference_mode"] == "previous_result_rows"
    assert plan["request_scope"] == "followup_requery"
    assert [job["dataset_key"] for job in plan["retrieval_jobs"]] == ["hold_history"]
    assert plan["retrieval_jobs"][0]["required_params"] == {"LOT_ID": ""}
    assert any(
        step.get("operation") == "apply_row_match_groups"
        and step.get("source_alias") == "hold_history_src"
        for step in plan["pandas_execution_plan"]
    )
    assert plan["output_contract"]["result_columns"] == [
        "LOT_ID",
        "OPER_NAME",
        "HOLD_TM",
        "HOLD_CD",
        "HOLD_DESC",
    ]
    assert not plan.get("validation_errors")


def test_v2_direct_required_param_precedes_optional_previous_result_binding():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    hydrator = load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "04a_trusted_retrieval_job_hydrator.py"
    )
    payload = {
        "request": {"question": "LOT-A와 LOT-B의 이력을 알려줘"},
        # A direct question can be asked in a session that also has a prior
        # result.  The optional binding must not overwrite the direct IDs.
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_requery",
            "reuse_strategy_hint": "previous_result",
            "matched_cues": {"previous_entity_identifiers": ["LOT_ID"]},
        },
        "state": {
            "current_data": {
                "columns": ["LOT_ID"],
                "result_columns": ["LOT_ID"],
            }
        },
        "trace": {"inspection": {}},
    }
    response = {
        "intent_plan": {
            "analysis_kind": "history_lookup",
            "request_scope": "followup_requery",
            "reference_mode": "previous_result_rows",
            "retrieval_jobs": [
                {
                    "dataset_key": "hold_history",
                    "source_alias": "hold_history_src",
                    "required_params": {"LOT_ID": ["LOT-A", "LOT-B"]},
                    "filters": {},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "select_history",
                    "operation": "select_columns",
                    "inputs": [
                        {"kind": "external_source", "ref": "hold_history_src"}
                    ],
                    "source_alias": "hold_history_src",
                    "output_alias": "history_result",
                }
            ],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["LOT_ID", "HOLD_TM", "HOLD_CD", "HOLD_DESC"],
            },
        }
    }

    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response),
        _followup_catalog_candidates(),
    )
    plan = normalized["intent_plan"]
    assert plan["request_scope"] == "new_analysis"
    assert plan["reference_mode"] == "none"
    assert plan["reuse_strategy"] == "none"
    assert plan["retrieval_jobs"][0]["required_params"] == {
        "LOT_ID": ["LOT-A", "LOT-B"]
    }
    assert normalized["trace"]["inspection"]["intent"]["followup_contract_guard"]["kind"] == (
        "direct_required_parameter"
    )

    hydrated = hydrator.hydrate_retrieval_jobs(
        normalized,
        {"table_catalog_items": _followup_catalog_candidates()["table_catalog_items"]},
        retrieval_mode="dummy",
    )
    assert hydrated["intent_plan"]["retrieval_jobs"][0]["required_params"] == {
        "LOT_ID": ["LOT-A", "LOT-B"]
    }
    assert not any(
        item.get("type") == "missing_catalog_required_params"
        for item in hydrated["trace"].get("warnings", [])
        if isinstance(item, dict)
    )


def test_v2_followup_product_grain_uses_source_reuse_not_previous_aggregate_rows():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    payload = {
        "request": {"question": "위 결과를 제품별로 알려줘"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_transform",
            "reuse_strategy_hint": "previous_result",
            "matched_cues": {"reference": ["위 결과"], "transform": ["제품별"]},
            "reusable_previous_source_aliases": ["prod_df", "wip_df"],
        },
        "state": {
            "current_data": {
                "columns": ["OPER_NAME", "PRODUCTION", "WIP"],
                "result_columns": ["OPER_NAME", "PRODUCTION", "WIP"],
                "source_aliases": ["prod_df", "wip_df"],
                "source_columns_by_alias": {
                    "prod_df": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO", "PRODUCTION"],
                    "wip_df": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO", "WIP"],
                },
            },
            "runtime_source_refs": {
                "prod_df": {"ref_id": "result:prod", "role": "source_rows"},
                "wip_df": {"ref_id": "result:wip", "role": "source_rows"},
            },
        },
        "trace": {"inspection": {}},
    }
    response = {
        "intent_plan": {
            "analysis_kind": "process_production_and_wip_summary_by_product",
            "request_scope": "followup_transform",
            "reference_mode": "previous_result_transform",
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "prod_df", "filters": {}},
                {"dataset_key": "wip", "source_alias": "wip_df", "filters": {}},
            ],
            "pandas_execution_plan": [
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "prod_df",
                    "group_by": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"],
                    "aggregations": [{"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}],
                },
                {
                    "operation": "groupby_and_aggregate",
                    "source_alias": "wip_df",
                    "group_by": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"],
                    "aggregations": [{"column": "WIP", "method": "sum", "output_column": "WIP"}],
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"],
            },
        }
    }
    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response),
        {"domain_items": [], "table_catalog_items": [], "main_flow_filters": []},
    )
    plan = normalized["intent_plan"]
    assert plan["reference_mode"] == "previous_source"
    assert plan["request_scope"] == "followup_expand_source"
    assert normalized["trace"]["inspection"]["intent"]["followup_contract_guard"]["kind"] == "source_grain_expansion"
    assert not any(
        error.get("type") == "invalid_reference_mode_contract"
        for error in plan.get("validation_errors", [])
    )


def test_v2_function_case_input_reconciles_missing_product_token_without_process_or_metric_words():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    cases, guard = normalizer._reconcile_function_case_inputs(
        [
            {
                "key": "product_token_match",
                "function_name": "match_product_tokens",
                "source_alias": "production",
                "input_text": "L-116",
            }
        ],
        "F315 L-116제품 WB 공정 차수별 UPH 알려줘",
        {"domain_items": [], "table_catalog_items": [], "main_flow_filters": []},
    )
    assert cases[0]["input_text"] == "F315 L-116"
    assert guard["status"] == "applied"
    assert guard["cases"][0]["missing_tokens"] == ["F315"]

    unchanged, unchanged_guard = normalizer._reconcile_function_case_inputs(
        [
            {
                "key": "product_token_match",
                "function_name": "match_product_tokens",
                "source_alias": "production",
                "input_text": "L-116",
            }
        ],
        "L-116제품 WB 공정 UPH 알려줘",
        {"domain_items": [], "table_catalog_items": [], "main_flow_filters": []},
    )
    assert unchanged[0]["input_text"] == "L-116"
    assert unchanged_guard["status"] == "not_needed"


def test_v2_followup_requery_never_keeps_ambiguous_none_reference_mode():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    payload = {
        "request": {"question": "같은 원본으로 다시 조회해줘"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_requery",
            "reusable_previous_source_aliases": ["prod_src"],
        },
        "state": {
            "current_data": {
                "columns": ["OPER_NAME", "PRODUCTION"],
                "source_columns_by_alias": {
                    "prod_src": ["OPER_NAME", "PRODUCTION"],
                },
            },
        },
    }
    response = {
        "intent_plan": {
            "analysis_kind": "production_refresh",
            "request_scope": "followup_requery",
            "reference_mode": "none",
            "retrieval_jobs": [
                {
                    "dataset_key": "production",
                    "source_alias": "prod_src",
                    "filters": {"OPER_NAME": {"operator": "eq", "value": "INPUT"}},
                }
            ],
            "pandas_execution_plan": [
                {
                    "operation": "select_columns",
                    "source_alias": "prod_src",
                    "inputs": [{"kind": "external_source", "ref": "prod_src"}],
                    "output_alias": "production_result",
                }
            ],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["OPER_NAME", "PRODUCTION"],
            },
        }
    }
    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response),
        {"domain_items": [], "table_catalog_items": [], "main_flow_filters": []},
    )
    plan = normalized["intent_plan"]
    assert plan["reference_mode"] == "previous_source"
    assert plan["request_scope"] == "followup_expand_source"
    guard = normalized["trace"]["inspection"]["intent"]["followup_contract_guard"]
    assert guard["kind"] == "generic_followup_reference_completion"
    assert guard["reference_contract"]["source_aliases"] == ["prod_src"]
    assert not any(
        error.get("type") == "invalid_reference_mode_contract"
        for error in plan.get("validation_errors", [])
    )


def test_v2_followup_detail_without_catalog_contract_fails_closed():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    payload = {
        "request": {"question": "조회된 LOT의 이력 상세를 알려줘"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_requery",
            "matched_cues": {"previous_entity_identifiers": ["LOT_ID"]},
        },
        "state": {
            "current_data": {
                "columns": ["LOT_ID", "OPER_NAME"],
                "source_columns_by_alias": {"lot_src": ["LOT_ID", "OPER_NAME"]},
            }
        },
    }
    response = {
        "intent_plan": {
            "analysis_kind": "lot_detail_followup",
            "request_scope": "followup_requery",
            "reference_mode": "none",
            "retrieval_jobs": [{"dataset_key": "lot_status", "source_alias": "lot_src", "filters": {}}],
            "pandas_execution_plan": [],
            "output_contract": {"result_mode": "detail", "result_columns": ["LOT_ID", "OPER_NAME"]},
        }
    }
    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response),
        {
            "domain_items": [],
            "table_catalog_items": [
                {"dataset_key": "lot_status", "payload": {"columns": ["LOT_ID", "OPER_NAME"]}}
            ],
            "main_flow_filters": [],
        },
    )
    plan = normalized["intent_plan"]
    assert any(
        error.get("type") == "followup_dependent_catalog_unresolved"
        for error in plan.get("validation_errors", [])
    )
    guard = normalized["trace"]["inspection"]["intent"]["followup_contract_guard"]
    assert guard["status"] == "blocked"
    assert guard["reason"] == "dependent_catalog_unavailable"
