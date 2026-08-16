from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pandas as pd
import pytest

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


def _report_followup_state(*, with_source_ref: bool = True) -> dict:
    state = {
        "last_question": "D/A 공정그룹 실시간 생산 분석을 해줘",
        "current_data": {
            "row_count": 24,
            "columns": ["TECH", "DEN", "MODE", "달성율*판정", "현재작업재공"],
            "result_columns": ["TECH", "DEN", "MODE", "달성율*판정", "현재작업재공"],
            "data_ref": {
                "ref_id": "result:report-context",
                "role": "analysis_result",
                "download_url": "https://example.invalid/private-result",
            },
            "source_aliases": ["report_snapshot"],
            "source_dataset_keys": ["production_judgement_snapshot"],
            "source_columns_by_alias": {
                "report_snapshot": ["TECH", "DEN", "MODE", "달성율*판정", "현재작업재공", "PRODUCTION"]
            },
            "preview_rows": [{"TECH": "SECRET_RAW_ROW"}],
            "report_context": {
                "context_version": "report.context.v1",
                "context_ref": "result:report-context",
                "report_type": "realtime_production",
                "snapshot_id": "snapshot-20260815-090000",
                "as_of": "2026-08-15T09:00:00+09:00",
                "expires_at": "2099-08-15T13:00:00+09:00",
                "report_scope": {"process_group": "DA", "date": "20260815"},
                "kpi_facts": {
                    "shortage_products": 12,
                    "risk_note": "X" * 500,
                },
                "rules": {
                    "rules_version": "production-risk-v3",
                    "rows": [{"SECRET_RULE_ROW": 1}],
                    "html": "<table>SECRET_HTML</table>",
                },
                "allowed_operations": [
                    "filter",
                    "groupby_and_aggregate",
                    "sort_and_top_n",
                ],
                "semantic_filters": [
                    {
                        "key": "production_shortage",
                        "aliases": ["생산부족", "생산 부족", "생산 부족 제품"],
                        "source_alias": "report_snapshot",
                        "column": "달성율*판정",
                        "operator": "eq",
                        "value": "생산부족",
                        "rows": [{"SECRET_SEMANTIC_ROW": 1}],
                    },
                    {
                        "key": "unsafe_filter",
                        "aliases": ["위험 필터"],
                        "source_alias": "report_snapshot",
                        "column": "TECH",
                        "operator": "python_eval",
                        "value": "SECRET_UNSAFE_OPERATOR",
                    },
                ],
                "value_domains": [
                    {
                        "source_alias": "report_snapshot",
                        "column": "달성율*판정",
                        "values": ["정상", "정상(초과생산)", "Abnormal", "생산부족"],
                        "aliases": {"생산 부족": "생산부족"},
                        "html": "SECRET_DOMAIN_HTML",
                    }
                ],
                "artifacts": [{"view_url": "https://example.invalid/private-report"}],
                "raw_rows": [{"SECRET_CONTEXT_ROW": 1}],
                "html": "<html>SECRET_CONTEXT_HTML</html>",
                "view_url": "https://example.invalid/private-report",
            },
        },
        "followup_source_results": [
            {
                "source_alias": "report_snapshot",
                "dataset_key": "production_judgement_snapshot",
                "row_count": 24,
                "columns": ["TECH", "DEN", "MODE", "판정", "PRODUCTION"],
                "preview_rows": [{"TECH": "SECRET_SOURCE_ROW"}],
                "data_ref": {
                    "ref_id": "source:report-snapshot",
                    "role": "source_rows",
                    "download_url": "https://example.invalid/private-source",
                },
            }
        ],
    }
    if with_source_ref:
        state["runtime_source_refs"] = {
            "report_snapshot": {
                "ref_id": "source:report-snapshot",
                "role": "source_rows",
                "source_alias": "report_snapshot",
                "download_url": "https://example.invalid/private-source",
            }
        }
    return state


def test_v2_intent_state_compacts_report_context_without_rows_html_or_urls():
    builder = load_module(V2_ROOT / "02_intent_variables_builder.py")
    variables = builder.build_variables(
        {
            "request": {"question": "그중 HBM 제품만 보여줘", "reference_date": "20260815"},
            "followup_hint": {"followup_candidate": True},
            "state": _report_followup_state(),
        }
    )

    summary = json.loads(variables["state_summary"])
    current_data = summary["state"]["current_data"]
    report_context = current_data["report_context"]
    serialized = json.dumps(summary["state"], ensure_ascii=False)

    assert report_context["context_version"] == "report.context.v1"
    assert report_context["report_type"] == "realtime_production"
    assert report_context["expires_at"] == "2099-08-15T13:00:00+09:00"
    assert report_context["report_scope"] == {"process_group": "DA", "date": "20260815"}
    assert report_context["kpi_facts"]["shortage_products"] == 12
    assert len(report_context["kpi_facts"]["risk_note"]) == 240
    assert report_context["allowed_operations"] == [
        "filter",
        "groupby_and_aggregate",
        "sort_and_top_n",
    ]
    assert report_context["semantic_filters"] == [
        {
            "key": "production_shortage",
            "aliases": ["생산부족", "생산 부족", "생산 부족 제품"],
            "source_alias": "report_snapshot",
            "column": "달성율*판정",
            "operator": "eq",
            "value": "생산부족",
        }
    ]
    assert report_context["value_domains"] == [
        {
            "source_alias": "report_snapshot",
            "column": "달성율*판정",
            "values": ["정상", "정상(초과생산)", "Abnormal", "생산부족"],
            "aliases": {"생산 부족": "생산부족"},
        }
    ]
    assert current_data["data_ref"] == {
        "ref_id": "result:report-context",
        "role": "analysis_result",
    }
    assert "preview_rows" not in current_data
    assert summary["state"]["runtime_source_refs"]["report_snapshot"] == {
        "ref_id": "source:report-snapshot",
        "role": "source_rows",
        "source_alias": "report_snapshot",
    }
    assert "preview_rows" not in summary["state"]["followup_source_results"][0]
    for secret in (
        "SECRET_RAW_ROW",
        "SECRET_RULE_ROW",
        "SECRET_HTML",
        "SECRET_CONTEXT_ROW",
        "SECRET_CONTEXT_HTML",
        "SECRET_SOURCE_ROW",
        "SECRET_SEMANTIC_ROW",
        "SECRET_UNSAFE_OPERATOR",
        "SECRET_DOMAIN_HTML",
        "example.invalid",
    ):
        assert secret not in serialized


def test_v2_report_context_aliases_are_normalized_and_bounded():
    builder = load_module(V2_ROOT / "02_intent_variables_builder.py")
    context = builder._compact_report_context(
        {
            "context_version": "report.context.v1",
            "report_type": "realtime_production",
            "scope": {"process_group": "DA"},
            "kpis": {f"metric_{index}": index for index in range(40)},
            "allowed_ops": [f"operation_{index}" for index in range(20)],
            "semantic_filters": [
                {
                    "key": f"filter_{index}",
                    "aliases": [f"alias_{alias_index}" for alias_index in range(20)],
                    "source_alias": "report_snapshot",
                    "column": f"COLUMN_{index}",
                    "operator": "eq",
                    "value": index,
                }
                for index in range(30)
            ],
            "value_domains": [
                {
                    "source_alias": "report_snapshot",
                    "column": f"COLUMN_{index}",
                    "values": [f"value_{value_index}" for value_index in range(50)],
                }
                for index in range(30)
            ],
        }
    )

    assert context["report_scope"] == {"process_group": "DA"}
    assert list(context["kpi_facts"]) == [f"metric_{index}" for index in range(24)]
    assert context["allowed_operations"] == [f"operation_{index}" for index in range(12)]
    assert len(context["semantic_filters"]) == 24
    assert context["semantic_filters"][0]["aliases"] == [f"alias_{index}" for index in range(16)]
    assert len(context["value_domains"]) == 24
    assert context["value_domains"][0]["values"] == [f"value_{index}" for index in range(40)]


def test_v2_report_followup_prefers_snapshot_source_without_freshness_cue():
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": "그중 HBM 제품만 보여줘", "reference_date": "20260815"},
            "state": _report_followup_state(),
        }
    )

    hint = result["followup_hint"]
    assert hint["followup_candidate"] is True
    assert hint["request_scope_hint"] == "followup_transform"
    assert hint["reuse_strategy_hint"] == "previous_source"
    assert hint["report_context_available"] is True
    assert hint["report_context_status"] == "valid"
    assert hint["report_reference"] is True
    assert hint["fresh_data_requested"] is False
    assert hint["reusable_previous_source_aliases"] == ["report_snapshot"]


def test_v2_expired_report_context_is_fail_closed_before_intent_planning():
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    state = _report_followup_state()
    state["current_data"]["report_context"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": "그중 생산부족 제품만 보여줘", "reference_date": "20260815"},
            "state": state,
        }
    )

    hint = result["followup_hint"]
    assert hint["request_scope_hint"] == "clarification"
    assert hint["reuse_strategy_hint"] == "none"
    assert hint["report_context_available"] is False
    assert hint["report_context_status"] == "expired"
    assert hint["unresolved_report_reference"] is True
    assert hint.get("reusable_previous_source_aliases", []) == []


def test_v2_report_column_name_containing_current_is_not_a_freshness_request():
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": "그중 현재작업재공이 0인 제품만 보여줘", "reference_date": "20260815"},
            "state": _report_followup_state(),
        }
    )

    hint = result["followup_hint"]
    assert hint["followup_candidate"] is True
    assert hint["request_scope_hint"] == "followup_transform"
    assert hint["reuse_strategy_hint"] == "previous_source"
    assert hint["fresh_data_requested"] is False
    assert hint["matched_cues"].get("fresh_data", []) == []
    assert hint.get("changed_conditions_hint", {}) == {}
    assert hint_builder._matched_fresh_cues("현재작업재공") == []


@pytest.mark.parametrize("question", ["이 Report를 현재 기준으로 다시 조회해줘", "방금 보고서를 최신 데이터로 보여줘"])
def test_v2_report_followup_freshness_cue_requires_a_new_retrieval(question: str):
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": question, "reference_date": "20260815"},
            "state": _report_followup_state(),
        }
    )

    hint = result["followup_hint"]
    assert hint["followup_candidate"] is True
    assert hint["request_scope_hint"] == "followup_requery"
    assert hint["reuse_strategy_hint"] == "previous_result"
    assert hint["report_reference"] is True
    assert hint["fresh_data_requested"] is True
    assert "previous_source" not in hint["required_previous_artifacts"]


def test_v2_report_context_does_not_turn_an_independent_current_query_into_a_followup():
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": "현재 WIP 알려줘", "reference_date": "20260815"},
            "state": _report_followup_state(),
        }
    )

    hint = result["followup_hint"]
    assert hint["followup_candidate"] is False
    assert hint["request_scope_hint"] == "new_analysis"
    assert hint["reuse_strategy_hint"] == "none"
    assert hint["report_context_available"] is True
    assert hint["report_reference"] is False
    assert hint["fresh_data_requested"] is True


def test_v2_explicit_report_reference_without_context_requires_clarification():
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": "방금 Report에서 HBM 제품만 보여줘", "reference_date": "20260815"},
            "state": {},
        }
    )

    hint = result["followup_hint"]
    assert hint["followup_candidate"] is True
    assert hint["request_scope_hint"] == "clarification"
    assert hint["reuse_strategy_hint"] == "none"
    assert hint["report_context_available"] is False
    assert hint["report_reference"] is False
    assert hint["unresolved_report_reference"] is True
    assert hint["required_previous_artifacts"] == ["report_context"]


def test_v2_normal_previous_result_followup_behavior_is_unchanged_without_report_context():
    hint_builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01e_followup_hint_builder.py"
    )
    state = _report_followup_state()
    state["current_data"].pop("report_context")
    result = hint_builder.build_followup_hint(
        {
            "request": {"question": "그중 수량이 가장 많은 항목만 보여줘", "reference_date": "20260815"},
            "state": state,
        }
    )

    hint = result["followup_hint"]
    assert hint["followup_candidate"] is True
    assert hint["request_scope_hint"] == "followup_transform"
    assert hint["reuse_strategy_hint"] == "previous_result"
    assert hint["report_context_available"] is False


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


def test_v2_strict_contract_projects_a_unique_presentation_label_back_to_canonical_key():
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"PRODUCTION": 10}],
        steps=[],
        output_contract={
            "result_mode": "scalar",
            "result_columns": ["PRODUCTION_SUM"],
            "required_columns": ["PRODUCTION_SUM"],
            "metric_columns": ["PRODUCTION_SUM"],
            "strict_result_columns": True,
            "column_labels": {"PRODUCTION_SUM": "전일 생산량"},
        },
    )

    rows, columns = executor._apply_strict_result_columns(
        [{"전일 생산량": 123}],
        ["전일 생산량"],
        payload,
    )

    assert columns == ["PRODUCTION_SUM"]
    assert rows == [{"PRODUCTION_SUM": 123}]


def test_v2_flow_export_matches_current_native_graph():
    flow = build_flow()

    assert flow["endpoint_name"] == "metadata-driven-v5-data-analysis"
    assert flow["name"] == "01. v5_data_analysis"
    assert flow["last_tested_version"] == "1.11.0"
    assert len(flow["data"]["nodes"]) == 51

    node_ids = {node["id"] for node in flow["data"]["nodes"]}
    node_index = {node["id"]: node for node in flow["data"]["nodes"]}
    assert "CustomComponent-v2FastResolver" in node_ids
    assert "LanguageModel-intent" in node_ids
    assert "LanguageModel-pandas" not in node_ids
    assert "LanguageModel-answer" not in node_ids
    assert "Prompt Template-ELVKc" not in node_ids
    assert "CustomComponent-aKrkH" not in node_ids
    assert all(node["data"]["node"].get("lf_version") == "1.11.0" for node in flow["data"]["nodes"])
    assert node_index["CustomComponent-A5y0b"]["data"]["node"]["template"]["code"]["value"] == (
        V2_ROOT / "21_v2_answer_message_adapter.py"
    ).read_text(encoding="utf-8")
    helper_library = (
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "function_case_helper_code_input_example.py"
    ).read_text(encoding="utf-8")
    assert (
        node_index["TextInput-AXG9a"]["data"]["node"]["template"]["input_value"]["value"]
        == helper_library
    )
    assert "excluded_tokens=None" in helper_library
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


def test_api_response_builder_projects_curated_intermediate_results_as_web_tables():
    api = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "answer_message": "분석 결과입니다.",
        "analysis": {
            "status": "ok",
            "step_outputs": [{"key": "raw_step", "preview_rows": [{"RAW_ONLY": "do not publish"}]}],
            "intermediate_results": [{"key": "analysis-only", "preview_rows": [{"RAW_ONLY": "analysis"}]}],
        },
        "data": {
            "columns": ["FINAL"],
            "rows": [{"FINAL": "final-value"}],
            "row_count": 1,
        },
        "answer_sections": {
            "summary": {"headline": "분석 결과입니다."},
            "evidence": {"intermediate_results": [{"key": "evidence-only", "preview_rows": [{"RAW_ONLY": "evidence"}]}]},
        },
        "intermediate_results": [
            {
                "key": "source:production",
                "description": "생산 데이터 필터 결과",
                "role": "filtered_source",
                "download_key": "source_production",
                "columns": ["OPER", "QTY"],
                "display_columns": ["OPER", "QTY"],
                "column_labels": {"OPER": "공정", "QTY": "수량"},
                "preview_rows": [
                    {"OPER": "A", "QTY": 1},
                    {"OPER": "B", "QTY": 2},
                    {"OPER": "C", "QTY": 3},
                    {"OPER": "D", "QTY": 4},
                    {"OPER": "E", "QTY": 5},
                    {"OPER": "F", "QTY": 6},
                ],
                "row_count": 12,
            },
            {
                "key": "computed:before_contract",
                "description": "최종 집계 전 중간 데이터",
                "role": "computed_result",
                "download_key": "pre_contract_result",
                "columns": ["OPER", "TOTAL_QTY"],
                "preview_rows": [{"OPER": "A", "TOTAL_QTY": 9}],
                "row_count": 3,
            },
        ],
        "data_refs": [
            {
                "role": "intermediate_result",
                "path": "payload.intermediate_rows.source_production",
                "download_url": "https://artifact.example.internal/download.csv?source",
                "download_format": "csv",
                "expires_at": "2026-08-10T12:00:00Z",
            },
            {
                "role": "intermediate_result",
                "path": "payload.intermediate_rows.pre_contract_result",
                "download_url": "https://artifact.example.internal/download.csv?computed",
                "download_format": "csv",
                "expires_at": "2026-08-10T12:00:00Z",
            },
        ],
        "trace": {
            "inspection": {
                "pandas_execution": {
                    "intermediate_results": [{"key": "trace-only", "preview_rows": [{"RAW_ONLY": "trace"}]}]
                }
            }
        },
    }

    response = api.build_api_response(payload, "웹용 요약", intermediate_preview_limit=2)

    assert response["message"] == "웹용 요약"
    assert response["data"]["rows"] == [{"FINAL": "final-value"}]
    assert len(response["intermediate_tables"]) == 2
    source_table, computed_table = response["intermediate_tables"]
    assert source_table["render_type"] == "table"
    assert source_table["title"] == "생산 데이터 필터 결과"
    assert source_table["rows"] == payload["intermediate_results"][0]["preview_rows"][:2]
    assert source_table["row_count"] == 12
    assert source_table["preview_row_count"] == 2
    assert source_table["preview_only"] is True
    assert source_table["column_labels"] == {"OPER": "공정", "QTY": "수량"}
    assert source_table["download"]["url"].endswith("?source")
    assert computed_table["download"]["url"].endswith("?computed")
    descriptors = response["answer_sections"]["intermediate_tables"]
    assert [item["row_source"] for item in descriptors] == [
        "intermediate_tables[0].rows",
        "intermediate_tables[1].rows",
    ]
    assert all("rows" not in item for item in descriptors)
    assert "RAW_ONLY" not in json.dumps(response["intermediate_tables"], ensure_ascii=False)
    assert "intermediate_results" not in response["answer_sections"].get("evidence", {})
    assert "intermediate_results" not in response["analysis"]
    assert "intermediate_results" not in response["trace"]["inspection"]["pandas_execution"]


def test_api_response_builder_uses_last_computed_checkpoint_when_legacy_results_are_not_published():
    api = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "22_api_response_builder.py")
    payload = {
        "analysis": {"status": "ok"},
        "intermediate_results": [
            {
                "key": "source:raw",
                "role": "source_input",
                "columns": ["RAW"],
                "preview_rows": [{"RAW": "source"}],
                "row_count": 10,
            },
            {
                "key": "computed:legacy",
                "role": "computed_result",
                "description": "계약 전 계산 결과",
                "columns": ["TOTAL"],
                "preview_rows": [{"TOTAL": 7}],
                "row_count": 1,
            },
        ],
    }

    response = api.build_api_response(payload)

    assert response["intermediate_tables"] == [
        {
            "render_type": "table",
            "table_id": "intermediate:computed:legacy",
            "title": "계약 전 계산 결과",
            "role": "computed_result",
            "checkpoint_key": "computed:legacy",
            "download_key": "",
            "columns": ["TOTAL"],
            "display_columns": ["TOTAL"],
            "column_labels": {},
            "rows": [{"TOTAL": 7}],
            "row_source": "intermediate_tables[0].rows",
            "row_count": 1,
            "preview_row_count": 1,
            "preview_only": False,
        }
    ]


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


def test_v2_normalizer_reconciles_unique_dataset_alias_variants():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, steps, trace = normalizer._reconcile_execution_source_aliases(
        {
            "pandas_function_cases": [
                {
                    "function_name": "match_product_tokens",
                    "source_alias": "eqp_uph_src",
                }
            ],
            "grain_plan": {"source_alias": "eqp_uph_src"},
            "output_contract": {
                "metric_bindings": [{"source_alias": "eqp_uph_src"}]
            },
        },
        [{"dataset_key": "eqp_uph", "source_alias": "eqp_uph_source"}],
        [
            {
                "operation": "apply_filters",
                "source_alias": "eqp_uph_src",
                "inputs": [{"kind": "external_source", "ref": "eqp_uph_src"}],
            }
        ],
    )

    assert trace["status"] == "applied"
    assert steps[0]["source_alias"] == "eqp_uph_source"
    assert steps[0]["inputs"][0]["ref"] == "eqp_uph_source"
    assert plan["pandas_function_cases"][0]["source_alias"] == "eqp_uph_source"
    assert plan["grain_plan"]["source_alias"] == "eqp_uph_source"
    assert plan["output_contract"]["metric_bindings"][0]["source_alias"] == "eqp_uph_source"


def test_v2_normalizer_reconciles_metric_alias_from_unique_dataset_key():
    """A unique declared dataset can ground a model-created metric alias."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, _, trace = normalizer._reconcile_execution_source_aliases(
        {
            "output_contract": {
                "metric_bindings": [
                    {
                        "source_alias": "prod_df",
                        "dataset_key": "production",
                        "source_column": "PRODUCTION",
                    }
                ]
            }
        },
        [{"dataset_key": "production", "source_alias": "production_source"}],
        [],
    )

    assert trace["status"] == "applied"
    assert plan["output_contract"]["metric_bindings"][0]["source_alias"] == "production_source"


def test_v2_normalizer_grounds_typed_inputs_from_output_contract_dataset_witness():
    """A contract dataset witness may repair only its exact display alias."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, steps, trace = normalizer._reconcile_execution_source_aliases(
        {
            "output_contract": {
                "metric_bindings": [
                    {
                        "source_alias": "actual_display",
                        "dataset_key": "production_today",
                        "source_column": "PRODUCTION",
                    },
                    {
                        "source_alias": "wip_display",
                        "dataset_key": "wip_today",
                        "source_column": "WIP",
                    },
                ]
            }
        },
        [
            {"dataset_key": "production_today", "source_alias": "prod_actual"},
            {"dataset_key": "wip_today", "source_alias": "wip_actual"},
        ],
        [
            {
                "operation": "join",
                "inputs": [
                    {"kind": "external_source", "ref": "actual_display"},
                    {"kind": "external_source", "ref": "wip_display"},
                ],
            }
        ],
    )

    assert trace["status"] == "applied"
    assert [item["ref"] for item in steps[0]["inputs"]] == [
        "prod_actual",
        "wip_actual",
    ]
    assert [
        item["source_alias"] for item in plan["output_contract"]["metric_bindings"]
    ] == ["prod_actual", "wip_actual"]


def test_v2_normalizer_does_not_ground_typed_input_when_dataset_witness_is_ambiguous():
    """Two runtime jobs for one dataset must retain the model reference."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    _, steps, trace = normalizer._reconcile_execution_source_aliases(
        {
            "output_contract": {
                "metric_bindings": [
                    {"source_alias": "actual_display", "dataset_key": "production"}
                ]
            }
        },
        [
            {"dataset_key": "production", "source_alias": "production_current"},
            {"dataset_key": "production", "source_alias": "production_previous"},
        ],
        [
            {
                "operation": "aggregate",
                "inputs": [
                    {"kind": "external_source", "ref": "actual_display"}
                ],
            }
        ],
    )

    assert trace["status"] == "not_needed"
    assert steps[0]["inputs"][0]["ref"] == "actual_display"


def test_v2_normalizer_keeps_metric_alias_when_dataset_key_is_ambiguous():
    """Dataset-key evidence must not redirect one of several same-table jobs."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, _, trace = normalizer._reconcile_execution_source_aliases(
        {
            "output_contract": {
                "metric_bindings": [
                    {"source_alias": "prod_df", "dataset_key": "production"}
                ]
            }
        },
        [
            {"dataset_key": "production", "source_alias": "production_current"},
            {"dataset_key": "production", "source_alias": "production_previous"},
        ],
        [],
    )

    assert trace["status"] == "not_needed"
    assert plan["output_contract"]["metric_bindings"][0]["source_alias"] == "prod_df"


def test_v2_normalizer_prunes_unconsumed_right_source_from_simple_preserve_left_join():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {
                    "columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                },
            },
            {
                "dataset_key": "eqp_uph",
                "payload": {
                    "columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
                },
            },
        ]
    }
    plan = {
        "output_contract": {
            "result_mode": "detail",
            "result_columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "required_columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
        },
        "join_plan": {"left_source_alias": "equipment_assign", "right_source_alias": "eqp_uph"},
    }
    jobs = [
        {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"},
        {"dataset_key": "eqp_uph", "source_alias": "eqp_uph"},
    ]
    steps = [
        {
            "node_id": "left_filter",
            "operation": "apply_filters",
            "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
            "output_alias": "left_filtered",
            "source_alias": "equipment_assign",
        },
        {
            "node_id": "right_filter",
            "operation": "apply_filters",
            "inputs": [{"kind": "external_source", "ref": "eqp_uph"}],
            "output_alias": "right_filtered",
            "source_alias": "eqp_uph",
        },
        {
            "node_id": "assignment_uph_join",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "left_filter"},
                {"kind": "node_output", "ref": "right_filter"},
            ],
            "output_alias": "assignment_uph_joined",
            "left_source_alias": "equipment_assign",
            "right_source_alias": "eqp_uph",
            "join_type": "left",
            "population_policy": "preserve_left_rows",
        },
        {
            "node_id": "select_output",
            "operation": "select_columns",
            "inputs": [{"kind": "node_output", "ref": "assignment_uph_join"}],
            "output_alias": "result",
            "source_alias": "assignment_uph_joined",
            "columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
        },
    ]

    normalized_plan, normalized_jobs, normalized_steps, trace = (
        normalizer._prune_source_sufficient_left_joins(
            plan, jobs, steps, candidates, [], "assigned equipment list"
        )
    )

    assert trace["status"] == "pruned"
    assert trace["pruned_source_alias"] == "eqp_uph"
    assert [item["dataset_key"] for item in normalized_jobs] == ["equipment_assign"]
    assert [item["node_id"] for item in normalized_steps] == ["left_filter", "select_output"]
    assert normalized_steps[-1]["inputs"] == [{"kind": "node_output", "ref": "left_filter"}]
    assert "join_plan" not in normalized_plan


def test_v2_normalizer_keeps_right_source_when_output_metric_is_owned_by_it():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {"dataset_key": "left", "payload": {"columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"]}},
            {"dataset_key": "right", "payload": {"columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"]}},
        ]
    }
    plan = {
        "output_contract": {
            "result_columns": ["EQP_MODEL", "UPH"],
            "metric_bindings": [{"source_alias": "right", "source_column": "UPH"}],
        }
    }
    jobs = [{"dataset_key": "left", "source_alias": "left"}, {"dataset_key": "right", "source_alias": "right"}]
    steps = [
        {"node_id": "left_step", "operation": "apply_filters", "inputs": [{"kind": "external_source", "ref": "left"}]},
        {"node_id": "right_step", "operation": "apply_filters", "inputs": [{"kind": "external_source", "ref": "right"}]},
        {"node_id": "join", "operation": "join", "inputs": [{"kind": "node_output", "ref": "left_step"}, {"kind": "node_output", "ref": "right_step"}], "join_type": "left", "population_policy": "preserve_left_rows"},
    ]

    _, normalized_jobs, _, trace = normalizer._prune_source_sufficient_left_joins(
        plan, jobs, steps, candidates, [], "UPH by equipment"
    )

    assert trace["status"] == "not_needed"
    assert trace["reason"] == "right_source_is_consumed_by_metric_or_transform"
    assert len(normalized_jobs) == 2


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


def test_candidate_tokens_split_mixed_ascii_korean_semantic_terms():
    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )

    tokens = builder._tokens("PKG계획 데이터 7/6일자 보여줘")

    assert {"pkg", "계획", "target", "plan"}.issubset(tokens)
    result = builder.build_metadata_candidates(
        {"request": {"question": "PKG계획 데이터 7/6일자 보여줘"}},
        [],
        [
            {
                "dataset_key": "production",
                "payload": {"display_name": "Production History", "columns": ["DATE", "PRODUCTION"]},
            },
            {
                "dataset_key": "target",
                "payload": {
                    "display_name": "PKG Target Plan",
                    "metric_semantics": {"INPUT_PLAN_QTY": {"default_rollup": "sum"}},
                },
            },
        ],
        [],
        min_table_items=1,
        max_table_items=1,
    )

    assert [item["dataset_key"] for item in result["metadata_candidates"]["table_catalog_items"]] == [
        "target"
    ]


def test_bounded_candidates_preserve_exact_domain_source_before_temporal_companions():
    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )
    result = builder.build_metadata_candidates(
        {"request": {"question": "assigned equipment count"}},
        [
            {
                "section": "quantity_terms",
                "key": "equipment_count",
                "payload": {
                    "aliases": ["assigned equipment"],
                    "data_source": "equipment_assign",
                    "columns": ["EQP_ID"],
                },
            }
        ],
        [
            {
                "dataset_key": "production_today",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {"time_scope": "current_day"},
                },
            },
            {
                "dataset_key": "production",
                "payload": {
                    "dataset_family": "production",
                    "selection_criteria": {"time_scope": "history"},
                },
            },
            {
                "dataset_key": "equipment_assign",
                "payload": {"dataset_family": "equipment", "columns": ["EQP_ID"]},
            },
        ],
        [],
        min_table_items=1,
        max_table_items=2,
    )

    selected = result["metadata_candidates"]["table_catalog_items"]
    keys = [item["dataset_key"] for item in selected]
    assert keys[0] == "equipment_assign"
    assert "equipment_assign" in keys
    assert result["metadata_load"]["temporal_family_companions"]["protected_dataset_keys"] == [
        "equipment_assign"
    ]


def test_bounded_candidates_keep_registered_dataset_family_members_as_guidance():
    """A family hint keeps choices visible; it never forces one dataset."""

    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )
    result = builder.build_metadata_candidates(
        {"request": {"question": "장비 대수 알려줘"}},
        [
            {
                "section": "quantity_terms",
                "key": "equipment_count",
                "payload": {
                    "aliases": ["장비 대수", "장비 수"],
                    "data_source": "equipment",
                    "columns": ["EQP_ID"],
                    "aggregation_method": "nunique",
                },
            }
        ],
        [
            {
                "dataset_key": "equipment_assign",
                "payload": {
                    "dataset_family": "equipment",
                    "columns": ["EQP_ID", "RECIPE_ID"],
                },
            },
            {
                "dataset_key": "eqp_uph",
                "payload": {
                    "dataset_family": "equipment",
                    "columns": ["EQP_ID", "UPH"],
                },
            },
            {
                "dataset_key": "production_today",
                "payload": {"dataset_family": "production", "columns": ["DATE", "PRODUCTION"]},
            },
        ],
        [],
        min_table_items=1,
        max_table_items=5,
    )

    keys = [item["dataset_key"] for item in result["metadata_candidates"]["table_catalog_items"]]
    assert {"equipment_assign", "eqp_uph"}.issubset(keys)
    dependencies = result["metadata_load"]["domain_dataset_dependencies"]
    assert dependencies["family_reference_values"] == ["equipment"]
    assert dependencies["family_included_dataset_keys"] == ["equipment_assign", "eqp_uph"]
    assert dependencies["unresolved_reference_values"] == []


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
    assert len(trace["deterministic_source_transforms"]) == 1
    assert trace["deterministic_source_transforms"][0]["function_name"] == "match_product_tokens"
    safe_trace = trace["safe_import_normalization"]
    assert safe_trace["selected_function_case_pre_transform"]["replacement_count"] == 1


def test_v2_complex_executor_repairs_single_source_generated_result_alias_for_selected_helper():
    """A weak model may treat a helper result name as a source alias.

    The repair is limited to one active retrieval source, so it cannot redirect
    a legitimate second source in a join.
    """

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

    executed = executor.execute_pandas_code(
        payload,
        {
            "code": (
                "candidate = sources['matched_prod'].copy()\n"
                "selected = match_product_tokens('L-116', candidate)\n"
                "result = selected[['MCP_NO', 'UPH']].copy()"
            )
        },
        function_case_helper_code=helper_source,
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"MCP_NO": "L-116A", "UPH": 112.0}]
    safe_trace = executed["trace"]["inspection"]["pandas_execution"]["safe_import_normalization"]
    rewrite = safe_trace["selected_function_case_pre_transform"]
    assert rewrite["replaced_unknown_source_refs"] == ["matched_prod"]
    assert rewrite["replaced_unbound_function_names"] == ["match_product_tokens"]


def test_v2_complex_executor_does_not_redirect_same_helper_on_another_source():
    """A source-local helper transform must not overwrite a second source's schema."""
    _, executor, _ = _modules()
    payload = _single_source_payload(
        rows=[{"MCP_NO": "L-116A", "UPH": 112.0}],
        steps=[
            {
                "operation": "apply_pandas_function_case",
                "function_name": "match_product_tokens",
                "input_text": "L-116",
                "source_alias": "source_1",
            },
            {"operation": "custom_complex_operation", "source_alias": "source_1"},
        ],
        output_contract={"result_mode": "detail", "result_columns": ["MCP_NO", "WIP"]},
    )
    payload["intent_plan"]["pandas_function_cases"] = [
        {
            "function_name": "match_product_tokens",
            "input_text": "L-116",
            "source_alias": "source_1",
        }
    ]
    payload["intent_plan"]["retrieval_jobs"].append(
        {"source_alias": "source_2", "dataset_key": "dataset_2", "filters": {}}
    )
    payload["intent_plan"]["resolved_execution_graph"]["external_source_requirements"].append(
        {
            "source_alias": "source_2",
            "dataset_key": "dataset_2",
            "provider": "retrieval_job",
            "required": True,
        }
    )
    payload["runtime_sources"]["source_2"] = [{"MCP_NO": "L-116B", "WIP": 47.0}]
    payload["source_results"].append(
        {"source_alias": "source_2", "dataset_key": "dataset_2", "status": "ok", "row_count": 1}
    )
    helper_source = (
        "def match_product_tokens(input_text, frame):\n"
        "    return frame[frame['MCP_NO'].astype(str).str.startswith(input_text)].copy()\n"
    )

    executed = executor.execute_pandas_code(
        payload,
        {
            "code": (
                "selected = match_product_tokens('L-116', sources['source_1'])\n"
                "other = match_product_tokens('L-116', sources['source_2'])\n"
                "result = other[['MCP_NO', 'WIP']].copy()"
            )
        },
        function_case_helper_code=helper_source,
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [{"MCP_NO": "L-116B", "WIP": 47.0}]
    safe_trace = executed["trace"]["inspection"]["pandas_execution"]["safe_import_normalization"]
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


def test_v2_explicit_metric_domain_dataset_is_not_discarded_for_missing_time_scope():
    """A registered metric source remains authoritative when scope only ranks alternatives."""
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "target_data",
                "payload": {
                    "aliases": ["생산계획", "계획"],
                    "data_source": "target",
                    "metric_columns": ["INPUT_PLAN_QTY", "OUT_PLAN_QTY"],
                },
            }
        ],
        "table_catalog_items": [
            {
                "section": "table_catalog",
                "key": "production",
                "dataset_key": "production",
                "payload": {
                    "columns": ["DATE", "PRODUCTION"],
                    "time_scope": "history",
                },
            },
            {
                "section": "table_catalog",
                "key": "target",
                "dataset_key": "target",
                "payload": {
                    "source_type": "goodocs",
                    "columns": ["DATE", "INPUT 계획", "OUT 계획"],
                    "filter_mappings": {
                        "DATE": ["DATE"],
                        "INPUT_PLAN_QTY": ["INPUT 계획"],
                        "OUT_PLAN_QTY": ["OUT 계획"],
                    },
                    # The catalog deliberately has no time_scope.  It is a
                    # standalone date-filtered plan dataset, not a history
                    # companion of production.
                },
            },
        ],
    }
    jobs, steps, guard = normalizer._ensure_selected_metric_sources(
        {
            "request": {
                "question": "2026-07-06 생산계획을 보여줘",
                "reference_date": "20260701",
            }
        },
        [
            {
                "dataset_key": "production",
                "source_alias": "retrieval_source",
                "source_type": "oracle",
                "required_params": {"DATE": "20260706"},
                "filters": {"DATE": {"operator": "eq", "value": "20260706"}},
            }
        ],
        [
            {
                "node_id": "select_plan",
                "operation": "select_columns",
                "inputs": [{"kind": "external_source", "ref": "retrieval_source"}],
                "output_alias": "plan_result",
                "source_alias": "retrieval_source",
            }
        ],
        candidates,
        [{"section": "quantity_terms", "key": "target_data"}],
    )

    assert steps[0]["source_alias"] == "retrieval_source"
    assert jobs == [
        {
            "dataset_key": "target",
            "source_alias": "retrieval_source",
            "source_type": "goodocs",
            "filters": {"DATE": {"operator": "eq", "value": "20260706"}},
        }
    ]
    assert guard["status"] == "applied"
    assert guard["additions"] == []
    assert guard["replacements"] == [
        {
            "metadata_ref": {"section": "quantity_terms", "key": "target_data"},
            "source_alias": "retrieval_source",
            "from_dataset_key": "production",
            "to_dataset_key": "target",
            "metrics": ["INPUT_PLAN_QTY", "OUT_PLAN_QTY"],
            "requested_time_scope": "history",
            "selection_source": "domain_explicit_metric_dataset",
        }
    ]


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


def test_v2_typed_multi_source_plan_executes_without_pandas_model():
    """A complete Typed IR joins generic sources without generating pandas code.

    This deliberately uses neutral columns instead of manufacturing names.  It
    protects the common multi-source path used by Flow 01 without teaching the
    analysis Flow a question-specific recipe.
    """

    resolver, executor, _ = _modules()
    payload = {
        "question": "top groups with assigned item count and list",
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "left_dataset", "source_alias": "left_src", "filters": {}},
                {"dataset_key": "right_dataset", "source_alias": "right_src", "filters": {}},
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "sum_left",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "external_source", "ref": "left_src"}],
                    "output_alias": "group_totals",
                    "group_by": ["GROUP"],
                    "aggregations": [
                        {"column": "RAW_VALUE", "method": "sum", "output_column": "TOTAL_VALUE"}
                    ],
                },
                {
                    "node_id": "join_assignments",
                    "operation": "join",
                    "inputs": [
                        {"kind": "node_output", "ref": "group_totals"},
                        {"kind": "external_source", "ref": "right_src"},
                    ],
                    "output_alias": "joined_groups",
                    "on": ["GROUP"],
                    "join_type": "left",
                },
                {
                    "node_id": "aggregate_assignments",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "joined_groups"}],
                    "output_alias": "group_summary",
                    "group_by": ["GROUP", "TOTAL_VALUE"],
                    "aggregations": [
                        {"column": "ITEM", "method": "nunique", "output_column": "ITEM_COUNT"},
                        {"column": "ITEM", "method": "collect_unique", "output_column": "ITEM_LIST"},
                    ],
                },
                {
                    "node_id": "rank_groups",
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "node_output", "ref": "group_summary"}],
                    "output_alias": "final_groups",
                    "sort_by": "TOTAL_VALUE",
                    "order": "desc",
                    "limit": 3,
                },
            ],
            "output_contract": {
                "strict_result_columns": True,
                "result_mode": "aggregate",
                "grain_columns": ["GROUP"],
                "metric_columns": ["TOTAL_VALUE", "ITEM_COUNT", "ITEM_LIST"],
                "required_columns": ["GROUP", "TOTAL_VALUE", "ITEM_COUNT", "ITEM_LIST"],
                "result_columns": ["GROUP", "TOTAL_VALUE", "ITEM_COUNT", "ITEM_LIST"],
            },
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {"source_alias": "left_src", "dataset_key": "left_dataset", "provider": "retrieval_job", "required": True},
                    {"source_alias": "right_src", "dataset_key": "right_dataset", "provider": "retrieval_job", "required": True},
                ],
                "validation_errors": [],
            },
            "validation_errors": [],
        },
        "runtime_sources": {
            "left_src": [
                {"GROUP": "A", "RAW_VALUE": 7},
                {"GROUP": "A", "RAW_VALUE": 3},
                {"GROUP": "B", "RAW_VALUE": 4},
            ],
            "right_src": [
                {"GROUP": "A", "ITEM": "E1"},
                {"GROUP": "A", "ITEM": "E2"},
                {"GROUP": "A", "ITEM": "E2"},
                {"GROUP": "B", "ITEM": "E3"},
            ],
        },
        "source_results": [
            {"source_alias": "left_src", "dataset_key": "left_dataset", "status": "ok", "columns": ["GROUP", "RAW_VALUE"]},
            {"source_alias": "right_src", "dataset_key": "right_dataset", "status": "ok", "columns": ["GROUP", "ITEM"]},
        ],
        "trace": {"inspection": {}},
    }

    resolved = resolver.resolve_simple_analysis_contract(payload)
    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "complex"
    assert contract["operation"] == "execute_typed_pandas_plan"
    assert contract["requires_pandas_llm"] is False

    calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "this must never be used for a validated Typed IR",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        repair_prompt_template="repair",
    )

    assert calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_route"] == "complex"
    assert executed["data"]["rows"] == [
        {"GROUP": "A", "TOTAL_VALUE": 10, "ITEM_COUNT": 2, "ITEM_LIST": "E1, E2"},
        {"GROUP": "B", "TOTAL_VALUE": 4, "ITEM_COUNT": 1, "ITEM_LIST": "E3"},
    ]


def test_v2_typed_join_aggregates_the_right_source_before_merging():
    """A typed join can own its aggregate outputs without a redundant step."""

    _, executor, _ = _modules()
    result = executor._typed_join_frames(
        pd.DataFrame(
            [
                {"GROUP": "A", "TOTAL": 10},
                {"GROUP": "B", "TOTAL": 4},
                {"GROUP": "C", "TOTAL": 1},
            ]
        ),
        pd.DataFrame(
            [
                {"GROUP": "A", "ITEM": "E1"},
                {"GROUP": "A", "ITEM": "E2"},
                {"GROUP": "A", "ITEM": "E2"},
                {"GROUP": "B", "ITEM": "E3"},
            ]
        ),
        {
            "on": ["GROUP"],
            "join_type": "left",
            "aggregations": [
                {"column": "ITEM", "method": "nunique", "output_column": "ITEM_COUNT"},
                {"column": "ITEM", "method": "collect_unique", "output_column": "ITEM_LIST"},
            ],
        },
        pd,
    )
    assert result.to_dict(orient="records") == [
        {"GROUP": "A", "TOTAL": 10, "ITEM_COUNT": 2, "ITEM_LIST": "E1, E2"},
        {"GROUP": "B", "TOTAL": 4, "ITEM_COUNT": 1, "ITEM_LIST": "E3"},
        {"GROUP": "C", "TOTAL": 1, "ITEM_COUNT": 0, "ITEM_LIST": ""},
    ]


def test_v2_typed_left_join_keeps_left_group_column_when_right_values_are_declared():
    """A left-preserving join must not suffix a shared left dimension."""

    _, executor, _ = _modules()
    result = executor._execute_typed_pandas_plan(
        {
            "steps": [
                {
                    "node_id": "join_assign_uph",
                    "operation": "join",
                    "inputs": [
                        {"kind": "external_source", "ref": "equipment_assign"},
                        {"kind": "external_source", "ref": "eqp_uph"},
                    ],
                    "output_alias": "joined_assign_uph",
                    "left_source_alias": "equipment_assign",
                    "right_source_alias": "eqp_uph",
                    "left_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                    "right_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                    "join_type": "left",
                    "population_policy": "left_source_only",
                    "right_value_columns": ["UPH"],
                },
                {
                    "node_id": "aggregate_by_lead",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "joined_assign_uph"}],
                    "output_alias": "lead_summary",
                    "group_by": ["LEAD"],
                    "aggregations": [
                        {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                        {"column": "UPH", "method": "mean", "output_column": "UPH_AVG"},
                    ],
                },
            ]
        },
        {
            "equipment_assign": pd.DataFrame(
                [
                    {"EQP_ID": "E-01", "EQP_MODEL": "M-1", "RECIPE_ID": "R-1", "OPER_NAME": "M/D", "LEAD": "200"},
                    {"EQP_ID": "E-02", "EQP_MODEL": "M-2", "RECIPE_ID": "R-2", "OPER_NAME": "M/D", "LEAD": "300"},
                ]
            ),
            "eqp_uph": pd.DataFrame(
                [
                    {"EQP_MODEL": "M-1", "RECIPE_ID": "R-1", "OPER_NAME": "M/D", "LEAD": "200", "UPH": 100},
                    {"EQP_MODEL": "M-2", "RECIPE_ID": "R-2", "OPER_NAME": "M/D", "LEAD": "300", "UPH": 200},
                ]
            ),
        },
        pd,
    )

    assert result.to_dict(orient="records") == [
        {"LEAD": "200", "EQP_COUNT": 1, "UPH_AVG": 100.0},
        {"LEAD": "300", "EQP_COUNT": 1, "UPH_AVG": 200.0},
    ]


def test_v2_typed_left_join_blocks_ambiguous_same_named_right_value():
    """A source-side output alias is required for an overlapping right value."""

    _, executor, _ = _modules()
    with pytest.raises(executor.OutputContractError, match="source-side output alias"):
        executor._typed_join_frames(
            pd.DataFrame([{"KEY": "A", "UPH": 10}]),
            pd.DataFrame([{"KEY": "A", "UPH": 20}]),
            {
                "on": ["KEY"],
                "join_type": "left",
                "right_value_columns": ["UPH"],
            },
            pd,
        )


def test_v2_typed_outer_join_coalesces_shared_dimension_for_right_only_rows():
    """Outer joins retain one canonical shared dimension across both sides."""

    _, executor, _ = _modules()
    result = executor._typed_join_frames(
        pd.DataFrame(
            [
                {
                    "EQP_ID": "E-01",
                    "EQP_MODEL": "M-1",
                    "RECIPE_ID": "R-1",
                    "OPER_NAME": "M/D",
                    "LEAD": "200",
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "EQP_MODEL": "M-1",
                    "RECIPE_ID": "R-1",
                    "OPER_NAME": "M/D",
                    "LEAD": "200",
                    "UPH": 100,
                },
                {
                    "EQP_MODEL": "M-2",
                    "RECIPE_ID": "R-2",
                    "OPER_NAME": "M/D",
                    "LEAD": "300",
                    "UPH": 200,
                },
            ]
        ),
        {
            "left_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "right_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "join_type": "outer",
        },
        pd,
    )

    assert "LEAD" in result.columns
    assert "LEAD_left" not in result.columns
    assert "LEAD_right" not in result.columns
    assert {
        (str(row["LEAD"]), int(row["UPH"]))
        for _, row in result.iterrows()
    } == {("200", 100), ("300", 200)}


def test_v2_typed_outer_join_can_group_by_a_coalesced_shared_dimension():
    """A following aggregate consumes the restored canonical dimension."""

    _, executor, _ = _modules()
    result = executor._execute_typed_pandas_plan(
        {
            "steps": [
                {
                    "node_id": "join_sources",
                    "operation": "join",
                    "inputs": [
                        {"kind": "external_source", "ref": "assigned"},
                        {"kind": "external_source", "ref": "uph"},
                    ],
                    "output_alias": "joined",
                    "left_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                    "right_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                    "join_type": "outer",
                    "right_value_columns": ["UPH"],
                },
                {
                    "node_id": "aggregate_by_lead",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "joined"}],
                    "output_alias": "lead_summary",
                    "group_by": ["LEAD"],
                    "aggregations": [
                        {"column": "UPH", "method": "mean", "output_column": "UPH_AVG"},
                    ],
                },
            ]
        },
        {
            "assigned": pd.DataFrame(
                [{"EQP_MODEL": "M-1", "RECIPE_ID": "R-1", "OPER_NAME": "M/D", "LEAD": "200"}]
            ),
            "uph": pd.DataFrame(
                [
                    {"EQP_MODEL": "M-1", "RECIPE_ID": "R-1", "OPER_NAME": "M/D", "LEAD": "200", "UPH": 100},
                    {"EQP_MODEL": "M-2", "RECIPE_ID": "R-2", "OPER_NAME": "M/D", "LEAD": "300", "UPH": 200},
                ]
            ),
        },
        pd,
    )

    assert result.to_dict(orient="records") == [
        {"LEAD": "200", "UPH_AVG": 100.0},
        {"LEAD": "300", "UPH_AVG": 200.0},
    ]


def test_v2_typed_outer_join_blocks_conflicting_shared_dimension_values():
    """A shared outer-join dimension cannot silently choose either source."""

    _, executor, _ = _modules()
    with pytest.raises(executor.OutputContractError, match="공통 차원 값이 source 간에 다릅니다"):
        executor._typed_join_frames(
            pd.DataFrame(
                [{"EQP_MODEL": "M-1", "RECIPE_ID": "R-1", "OPER_NAME": "M/D", "LEAD": "200"}]
            ),
            pd.DataFrame(
                [{"EQP_MODEL": "M-1", "RECIPE_ID": "R-1", "OPER_NAME": "M/D", "LEAD": "300", "UPH": 100}]
            ),
            {
                "left_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                "right_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                "join_type": "outer",
                "right_value_columns": ["UPH"],
            },
            pd,
        )


def test_v2_typed_outer_join_blocks_ambiguous_same_named_right_value():
    """A value needed from both sides requires an explicit output alias."""

    _, executor, _ = _modules()
    with pytest.raises(executor.OutputContractError, match="source-side output alias"):
        executor._typed_join_frames(
            pd.DataFrame([{"KEY": "A", "UPH": 10}]),
            pd.DataFrame([{"KEY": "A", "UPH": 20}]),
            {
                "on": ["KEY"],
                "join_type": "outer",
                "right_value_columns": ["UPH"],
            },
            pd,
        )


def test_v2_typed_outer_join_keeps_right_only_product_keys():
    """Outer joins must not blank a product key that exists only on the right."""

    _, executor, _ = _modules()
    result = executor._typed_join_frames(
        pd.DataFrame(
            [{"TECH": "A", "DEVICE": "DEV-A", "INPUT_QTY": 100}]
        ),
        pd.DataFrame(
            [
                {"TECH": "A", "DEVICE": "DEV-A", "WIP_QTY": 10},
                {"TECH": "B", "DEVICE": "DEV-B", "WIP_QTY": 200},
            ]
        ),
        {"on": ["TECH", "DEVICE"], "join_type": "outer"},
        pd,
    )

    right_only = result.loc[result["DEVICE"].eq("DEV-B")].iloc[0]
    assert right_only["TECH"] == "B"
    assert right_only["DEVICE"] == "DEV-B"
    assert right_only["WIP_QTY"] == 200
    assert pd.isna(right_only["INPUT_QTY"])


def test_v2_typed_outer_aggregate_join_keeps_right_only_group_key():
    """Grouped enrichment uses its normalized join key for a right-only row."""

    _, executor, _ = _modules()
    result = executor._typed_join_frames(
        pd.DataFrame([{"GROUP": "A", "TOTAL": 10}]),
        pd.DataFrame([{"GROUP": "B", "ITEM": "E-01"}]),
        {
            "on": ["GROUP"],
            "join_type": "outer",
            "aggregations": [
                {"column": "ITEM", "method": "nunique", "output_column": "ITEM_COUNT"},
            ],
        },
        pd,
    )

    right_only = result.loc[result["GROUP"].eq("B")].iloc[0]
    assert right_only["ITEM_COUNT"] == 1


def _independent_metric_catalogs() -> dict:
    return {
        "domain_items": [],
        "main_flow_filters": [],
        "table_catalog_items": [
            {
                "dataset_key": "production_today",
                "payload": {
                    "columns": ["TECH", "PRODUCTION"],
                    "filter_mappings": {"TECH": ["TECH"]},
                },
            },
            {
                "dataset_key": "wip_today",
                "payload": {
                    "columns": ["TECH", "WIP"],
                    "filter_mappings": {"TECH": ["TECH"]},
                },
            },
        ],
    }


def _raw_metric_join_plan() -> tuple[dict, list[dict], list[dict]]:
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_src", "filters": {}},
        {"dataset_key": "wip_today", "source_alias": "wip_src", "filters": {}},
    ]
    steps = [
        {
            "node_id": "join_raw_metrics",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "prod_src"},
                {"kind": "external_source", "ref": "wip_src"},
            ],
            "output_alias": "joined_metrics",
            "on": ["TECH"],
            "join_type": "outer",
            "population_policy": "preserve_all_metric_source_keys",
        },
        {
            "node_id": "aggregate_after_join",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "node_output", "ref": "joined_metrics"}],
            "output_alias": "final_metrics",
            "group_by": ["TECH"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION_SUM"},
                {"column": "WIP", "method": "sum", "output_column": "WIP_SUM"},
            ],
        },
    ]
    plan = {
        "output_contract": {
            "result_mode": "aggregate",
            "grain_columns": ["TECH"],
            "metric_columns": ["PRODUCTION_SUM", "WIP_SUM"],
            "required_columns": ["TECH", "PRODUCTION_SUM", "WIP_SUM"],
            "result_columns": ["TECH", "PRODUCTION_SUM", "WIP_SUM"],
            "strict_result_columns": True,
        }
    }
    return plan, jobs, steps


def test_source_local_metric_aggregate_join_accepts_owned_declared_value_columns():
    """A model may carry a metric through an already aggregated source join."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    _, executor, _ = _modules()
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_src", "filters": {}},
        {"dataset_key": "wip_today", "source_alias": "wip_src", "filters": {}},
    ]
    steps = [
        {
            "node_id": "aggregate_wip",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "wip_src"}],
            "output_alias": "wip_agg",
            "source_alias": "wip_src",
            "group_by": ["TECH"],
            "aggregations": [{"column": "WIP", "method": "sum", "output_column": "WIP"}],
        },
        {
            "node_id": "aggregate_production",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "prod_src"}],
            "output_alias": "production_agg",
            "source_alias": "prod_src",
            "group_by": ["TECH"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
            ],
        },
        {
            "node_id": "join_wip_production",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "wip_agg"},
                {"kind": "node_output", "ref": "production_agg"},
            ],
            "output_alias": "joined_metrics",
            "on": ["TECH"],
            "join_type": "outer",
            "population_policy": "preserve_all_metric_source_keys",
            "right_value_columns": ["PRODUCTION"],
        },
    ]
    plan = {
        "output_contract": {
            "result_mode": "aggregate",
            "grain_columns": ["TECH"],
            "metric_columns": ["WIP", "PRODUCTION"],
            "required_columns": ["TECH", "WIP", "PRODUCTION"],
            "result_columns": ["TECH", "WIP", "PRODUCTION"],
            "strict_result_columns": True,
            "primary_metric": "WIP",
            "ordering": {"sort_by": "WIP", "order": "asc"},
        }
    }

    merge = normalizer._resolve_metric_merge_plan(
        plan,
        _independent_metric_catalogs(),
        jobs,
        steps,
        {"canonical_columns": ["TECH"]},
        {},
        [],
    )
    assert merge["execution_shape"] == "aggregate_outputs_then_join"
    assert merge["join_type"] == "outer"

    comparison = normalizer._resolve_metric_comparison_plan(
        [
            {
                "operation": "compare_metrics",
                "lhs_metric_column": "WIP",
                "operator": "lt",
                "rhs_metric_column": "PRODUCTION",
            }
        ],
        plan["output_contract"],
        merge,
        "WIP is less than production",
    )
    assert comparison["null_numeric_policy"] == "fill_missing_with_zero"
    payload = {
        "intent_plan": {
            "output_contract": plan["output_contract"],
            "resolved_metric_merge_plan": merge,
            "resolved_metric_comparison_plan": comparison,
        },
        "simple_analysis_contract": {
            "strict": True,
            "route": "complex",
            "operation": "execute_typed_pandas_plan",
            "steps": [],
        },
        "runtime_sources": {
            "prod_src": [
                {"TECH": "A", "PRODUCTION": 10},
                {"TECH": "B", "PRODUCTION": 8},
            ],
            "wip_src": [{"TECH": "A", "WIP": 5}],
        },
        "trace": {"inspection": {}},
    }
    executed = executor.execute_pandas_code(payload, "")

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"TECH": "B", "WIP": 0, "PRODUCTION": 8},
        {"TECH": "A", "WIP": 5, "PRODUCTION": 10},
    ]


def test_independent_raw_metric_join_is_compiled_to_source_local_merge():
    """Raw source joins are rewritten only for a proven independent metric shape."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    resolver, executor, _ = _modules()
    plan, jobs, steps = _raw_metric_join_plan()
    merge = normalizer._resolve_metric_merge_plan(
        plan,
        _independent_metric_catalogs(),
        jobs,
        steps,
        {"canonical_columns": ["TECH"]},
        {},
        [],
    )

    assert merge["execution_shape"] == "raw_join_rewritten_as_metric_merge"
    assert merge["join_type"] == "outer"
    assert {item["source_alias"] for item in merge["metrics"]} == {"prod_src", "wip_src"}
    assert {item["output_column"] for item in merge["metrics"]} == {"PRODUCTION_SUM", "WIP_SUM"}

    payload = {
        "intent_plan": {
            **plan,
            "retrieval_jobs": jobs,
            "pandas_execution_plan": steps,
            "resolved_metric_merge_plan": merge,
            "intent_ir": {"route_source_aliases": ["prod_src", "wip_src"], "operations": ["join", "groupby_and_aggregate"]},
            "resolved_execution_graph": {"external_source_requirements": []},
        },
        "runtime_sources": {
            "prod_src": [
                {"TECH": "A", "PRODUCTION": 10},
                {"TECH": "A", "PRODUCTION": 20},
                {"TECH": "B", "PRODUCTION": 5},
            ],
            "wip_src": [
                {"TECH": "A", "WIP": 2},
                {"TECH": "A", "WIP": 3},
                {"TECH": "C", "WIP": 7},
            ],
        },
        "source_results": [
            {"source_alias": "prod_src", "dataset_key": "production_today", "status": "ok", "columns": ["TECH", "PRODUCTION"]},
            {"source_alias": "wip_src", "dataset_key": "wip_today", "status": "ok", "columns": ["TECH", "WIP"]},
        ],
        "trace": {"inspection": {}},
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["deterministic_operation"] == "merge_metric_sources"
    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=lambda _: (_ for _ in ()).throw(AssertionError("metric merge must not call pandas LLM")),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"TECH": "A", "PRODUCTION_SUM": 30, "WIP_SUM": 5},
        {"TECH": "B", "PRODUCTION_SUM": 5, "WIP_SUM": 0},
        {"TECH": "C", "PRODUCTION_SUM": 0, "WIP_SUM": 7},
    ]


def test_metric_merge_restores_metric_missing_from_strict_result_projection():
    """A weak display contract must not hide a successfully merged metric."""

    resolver, executor, _ = _modules()
    plan = {
        "output_contract": {
            "result_mode": "aggregate",
            "grain_columns": ["TECH"],
            "metric_columns": ["실적"],
            "required_columns": ["TECH", "실적"],
            "result_columns": ["TECH", "실적"],
            "strict_result_columns": True,
            "metric_bindings": [
                {
                    "source_alias": "prod_actual",
                    "dataset_key": "production_today",
                    "source_column": "PRODUCTION",
                    "aggregation": "sum",
                    "output_column": "실적",
                }
            ],
        }
    }
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_actual"},
        {"dataset_key": "target", "source_alias": "tgt_plan"},
    ]
    steps = [
        {
            "node_id": "agg_actual",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "prod_actual"}],
            "output_alias": "prod_agg",
            "group_by": ["TECH"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "실적"}
            ],
        },
        {
            "node_id": "agg_target",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "tgt_plan"}],
            "output_alias": "tgt_agg",
            "group_by": ["TECH"],
            "aggregations": [
                {"column": "OUT 계획", "method": "sum", "output_column": "목표"}
            ],
        },
        {
            "node_id": "join_compare",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "prod_agg"},
                {"kind": "node_output", "ref": "tgt_agg"},
            ],
            "output_alias": "final_result",
            "join_type": "outer",
            "on": ["TECH"],
        },
    ]
    merge = {
        "operation": "merge_metric_sources",
        "join_type": "outer",
        "population_policy": "preserve_all_metric_source_keys",
        "grain_mappings": [
            {
                "canonical_column": "TECH",
                "output_column": "TECH",
                "source_candidates": {
                    "prod_actual": ["TECH"],
                    "tgt_plan": ["TECH"],
                },
            }
        ],
        "metrics": [
            {
                "source_alias": "prod_actual",
                "dataset_key": "production_today",
                "source_column": "PRODUCTION",
                "aggregation": "sum",
                "output_column": "실적",
                "fill_value": 0,
                "fill_on_absence": True,
                "source_candidates": ["PRODUCTION"],
            },
            {
                "source_alias": "tgt_plan",
                "dataset_key": "target",
                "source_column": "OUT 계획",
                "aggregation": "sum",
                "output_column": "목표",
                "fill_value": 0,
                "fill_on_absence": True,
                "source_candidates": ["OUT 계획"],
            },
        ],
        "fill_zero_on_success": True,
        "strict": True,
    }
    payload = {
        "intent_plan": {
            **plan,
            "retrieval_jobs": jobs,
            "pandas_execution_plan": steps,
            "resolved_metric_merge_plan": merge,
            "intent_ir": {"route_source_aliases": ["prod_actual", "tgt_plan"]},
            "resolved_execution_graph": {"external_source_requirements": []},
        },
        "runtime_sources": {
            "prod_actual": [{"TECH": "A", "PRODUCTION": 10}],
            "tgt_plan": [{"TECH": "A", "OUT 계획": 3}],
        },
        "source_results": [
            {
                "source_alias": "prod_actual",
                "dataset_key": "production_today",
                "status": "ok",
                "columns": ["TECH", "PRODUCTION"],
            },
            {
                "source_alias": "tgt_plan",
                "dataset_key": "target",
                "status": "ok",
                "columns": ["TECH", "OUT 계획"],
            },
        ],
        "trace": {"inspection": {}},
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=lambda _: (_ for _ in ()).throw(
            AssertionError("metric merge must not call pandas LLM")
        ),
        repair_prompt_template="repair",
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["columns"] == [
        "TECH",
        "실적",
        "목표",
    ]
    assert executed["data"]["rows"] == [
        {"TECH": "A", "실적": 10, "목표": 3}
    ]
    assert executed["intent_plan"]["output_contract"]["metric_columns"] == [
        "실적",
        "목표",
    ]
    reconciliation = executed["trace"]["inspection"][
        "runtime_metric_merge_output_contract_reconciliation"
    ]
    assert reconciliation["added_result_columns"] == ["목표"]


def test_metric_comparison_fills_missing_metric_with_zero_and_precedes_typed_plan():
    """A left-preserved comparison must keep no-WIP products as zero-WIP rows."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    _, executor, _ = _modules()
    merge = {
        "operation": "merge_metric_sources",
        "join_type": "left",
        "population_policy": "left_source_only",
        "grain_mappings": [
            {
                "canonical_column": "PRODUCT",
                "output_column": "PRODUCT",
                "source_candidates": {
                    "input_perf": ["PRODUCT"],
                    "wip_data": ["PRODUCT"],
                },
            },
            {
                "canonical_column": "ORG",
                "output_column": "ORG",
                "source_candidates": {
                    "input_perf": ["ORG"],
                    "wip_data": ["ORG"],
                },
            },
        ],
        "metrics": [
            {
                "source_alias": "input_perf",
                "dataset_key": "production",
                "source_column": "PRODUCTION",
                "source_candidates": ["PRODUCTION"],
                "aggregation": "sum",
                "output_column": "input_quantity",
                "fill_on_absence": True,
                "fill_value": 0,
            },
            {
                "source_alias": "wip_data",
                "dataset_key": "wip",
                "source_column": "WIP",
                "source_candidates": ["WIP"],
                "aggregation": "sum",
                "output_column": "wip_quantity",
                "fill_on_absence": True,
                "fill_value": 0,
            },
        ],
        "fill_zero_on_success": True,
        "strict": True,
    }
    output_contract = {
        "result_mode": "aggregate",
        "grain_columns": ["PRODUCT", "ORG"],
        "metric_columns": ["input_quantity", "wip_quantity"],
        "required_columns": ["PRODUCT", "ORG", "input_quantity", "wip_quantity"],
        "result_columns": ["PRODUCT", "ORG", "input_quantity", "wip_quantity"],
        "strict_result_columns": True,
        "primary_metric": "wip_quantity",
        "ordering": {"sort_by": "wip_quantity", "order": "asc"},
    }
    comparison = normalizer._resolve_metric_comparison_plan(
        [
            {
                "operation": "compare_metrics",
                "lhs_metric_column": "wip_quantity",
                "operator": "lt",
                "rhs_metric_column": "input_quantity",
            }
        ],
        output_contract,
        merge,
        "WIP is less than input",
    )

    assert comparison["merge_plan"]["join_type"] == "left"
    assert comparison["merge_plan"]["fill_zero_on_success"] is True
    assert comparison["null_numeric_policy"] == "fill_missing_with_zero"
    payload = {
        "intent_plan": {
            "output_contract": output_contract,
            "resolved_metric_merge_plan": merge,
            "resolved_metric_comparison_plan": comparison,
        },
        # A generic Typed plan would only sort here.  The comparison contract
        # must take precedence and enforce the row predicate instead.
        "simple_analysis_contract": {
            "strict": True,
            "route": "complex",
            "operation": "execute_typed_pandas_plan",
            "steps": [],
        },
        "runtime_sources": {
            "input_perf": [
                {"PRODUCT": "A", "ORG": "O1", "PRODUCTION": 10},
                {"PRODUCT": "A", "ORG": "O2", "PRODUCTION": 5},
                {"PRODUCT": "B", "ORG": "O1", "PRODUCTION": 7},
            ],
            "wip_data": [
                {"PRODUCT": "A", "ORG": "O1", "WIP": 2},
                {"PRODUCT": "A", "ORG": "O2", "WIP": 10},
            ],
        },
        "trace": {"inspection": {}},
    }
    executed = executor.execute_pandas_code(payload, "")

    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["execution_mode"] == "compare_metrics"
    assert executed["data"]["rows"] == [
        {"PRODUCT": "B", "ORG": "O1", "input_quantity": 7, "wip_quantity": 0},
        {"PRODUCT": "A", "ORG": "O1", "input_quantity": 10, "wip_quantity": 2},
    ]
    certificate = executed["analysis"]["semantic_execution_certificate"]
    assert certificate["null_numeric_policy"] == "fill_missing_with_zero"
    assert certificate["postcondition_validation"] == "passed"


def test_ordering_contract_allows_missing_metric_values_only_at_sorted_tail():
    _, executor, _ = _modules()
    payload = {
        "intent_plan": {
            "output_contract": {
                "ordering": {"sort_by": "wip_quantity", "order": "asc"}
            }
        }
    }

    assert executor._ordering_contract_error(
        payload,
        [
            {"wip_quantity": 0},
            {"wip_quantity": 2},
            {"wip_quantity": float("nan")},
        ],
        ["wip_quantity"],
    ) == ""
    assert executor._ordering_contract_error(
        payload,
        [
            {"wip_quantity": float("nan")},
            {"wip_quantity": 2},
        ],
        ["wip_quantity"],
    )


def test_missing_quantity_metrics_are_zero_filled_for_none_blank_and_nan_values():
    _, executor, _ = _modules()
    payload = {
        "intent_plan": {
            "output_contract": {
                "grain_columns": ["PRODUCT"],
                "metric_columns": ["INPUT_QTY", "WIP_QTY", "TARGET_QTY", "OUTPUT_QTY"],
            }
        }
    }
    rows = [
        {
            "PRODUCT": "A",
            "INPUT_QTY": None,
            "WIP_QTY": "",
            "TARGET_QTY": float("nan"),
            "OUTPUT_QTY": "<NA>",
            "NOTE": None,
        }
    ]

    checkpoint_frame = executor._zero_fill_declared_metric_frame_values(
        pd.DataFrame(rows),
        payload,
    )
    assert checkpoint_frame.to_dict(orient="records") == [
        {
            "PRODUCT": "A",
            "INPUT_QTY": 0,
            "WIP_QTY": 0,
            "TARGET_QTY": 0,
            "OUTPUT_QTY": 0,
            "NOTE": None,
        }
    ]

    normalized = executor._normalize_missing_metric_values(rows, payload)

    assert normalized == [
        {
            "PRODUCT": "A",
            "INPUT_QTY": 0,
            "WIP_QTY": 0,
            "TARGET_QTY": 0,
            "OUTPUT_QTY": 0,
            "NOTE": None,
        }
    ]


def test_explicit_output_contract_recovers_partial_outer_metric_merge():
    """A partial raw outer join can recover only from explicit source ownership."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    resolver, executor, _ = _modules()
    plan, jobs, _ = _raw_metric_join_plan()
    plan["output_contract"]["metric_bindings"] = [
        {
            "source_alias": "prod_src",
            "dataset_key": "production_today",
            "source_column": "PRODUCTION",
            "aggregation": "sum",
            "output_column": "PRODUCTION_SUM",
        },
        {
            "source_alias": "wip_src",
            "dataset_key": "wip_today",
            "source_column": "WIP",
            "aggregation": "sum",
            "output_column": "WIP_SUM",
        },
    ]
    # The weak plan kept the comparison join, but omitted both source-local
    # aggregates.  It also leaves the right metric on the raw join.  The
    # explicit output contract is sufficient to restore aggregate-then-merge.
    steps = [
        {
            "node_id": "partial_raw_join",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "prod_src"},
                {"kind": "external_source", "ref": "wip_src"},
            ],
            "output_alias": "partial_join",
            "on": ["TECH"],
            "join_type": "outer",
            "population_policy": "preserve_all_metric_source_keys",
            "right_value_columns": ["WIP"],
        }
    ]
    merge = normalizer._resolve_metric_merge_plan(
        plan,
        _independent_metric_catalogs(),
        jobs,
        steps,
        {"canonical_columns": ["TECH"]},
        {},
        [],
    )

    assert merge["execution_shape"] == "output_contract_independent_metric_shape"
    assert merge["join_type"] == "outer"
    assert {item["source_alias"] for item in merge["metrics"]} == {
        "prod_src",
        "wip_src",
    }

    payload = {
        "intent_plan": {
            **plan,
            "retrieval_jobs": jobs,
            "pandas_execution_plan": steps,
            "resolved_metric_merge_plan": merge,
            "intent_ir": {
                "route_source_aliases": ["prod_src", "wip_src"],
                "operations": ["join"],
            },
            "resolved_execution_graph": {"external_source_requirements": []},
        },
        "runtime_sources": {
            "prod_src": [{"TECH": "A", "PRODUCTION": 10}],
            "wip_src": [{"TECH": "B", "WIP": 3}],
        },
        "source_results": [
            {
                "source_alias": "prod_src",
                "dataset_key": "production_today",
                "status": "ok",
                "columns": ["TECH", "PRODUCTION"],
            },
            {
                "source_alias": "wip_src",
                "dataset_key": "wip_today",
                "status": "ok",
                "columns": ["TECH", "WIP"],
            },
        ],
        "trace": {"inspection": {}},
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["deterministic_operation"] == "merge_metric_sources"
    executed = executor.execute_hybrid_analysis(
        resolved,
        "",
        model_invoker=lambda _: (_ for _ in ()).throw(
            AssertionError("recovered metric merge must not call pandas LLM")
        ),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"TECH": "A", "PRODUCTION_SUM": 10, "WIP_SUM": 0},
        {"TECH": "B", "PRODUCTION_SUM": 0, "WIP_SUM": 3},
    ]


def test_partial_outer_metric_merge_rejects_row_enrichment_value():
    """A right-side attribute outside metric ownership cannot be rewritten."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, jobs, _ = _raw_metric_join_plan()
    plan["output_contract"]["metric_bindings"] = [
        {
            "source_alias": "prod_src",
            "dataset_key": "production_today",
            "source_column": "PRODUCTION",
            "aggregation": "sum",
            "output_column": "PRODUCTION_SUM",
        },
        {
            "source_alias": "wip_src",
            "dataset_key": "wip_today",
            "source_column": "WIP",
            "aggregation": "sum",
            "output_column": "WIP_SUM",
        },
    ]
    steps = [
        {
            "node_id": "partial_enrichment_join",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "prod_src"},
                {"kind": "external_source", "ref": "wip_src"},
            ],
            "on": ["TECH"],
            "join_type": "outer",
            "population_policy": "preserve_all_metric_source_keys",
            "right_value_columns": ["UNRELATED_ATTRIBUTE"],
        }
    ]

    merge = normalizer._resolve_metric_merge_plan(
        plan,
        _independent_metric_catalogs(),
        jobs,
        steps,
        {"canonical_columns": ["TECH"]},
        {},
        [],
    )
    assert merge == {}


def test_partial_outer_metric_merge_uses_full_output_grain_over_raw_key_subset():
    """A proven final grain prevents a weak raw join from collapsing keys."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    resolver, executor, _ = _modules()
    candidates = _independent_metric_catalogs()
    for item in candidates["table_catalog_items"]:
        item["payload"]["columns"].append("ORG")
        item["payload"]["filter_mappings"]["ORG"] = ["ORG"]
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_src", "filters": {}},
        {"dataset_key": "wip_today", "source_alias": "wip_src", "filters": {}},
    ]
    plan = {
        "output_contract": {
            "result_mode": "aggregate",
            "grain_columns": ["TECH", "ORG"],
            "metric_columns": ["PRODUCTION_SUM", "WIP_SUM"],
            "result_columns": ["TECH", "ORG", "PRODUCTION_SUM", "WIP_SUM"],
            "metric_bindings": [
                {
                    "source_alias": "prod_src",
                    "dataset_key": "production_today",
                    "source_column": "PRODUCTION",
                    "aggregation": "sum",
                    "output_column": "PRODUCTION_SUM",
                },
                {
                    "source_alias": "wip_src",
                    "dataset_key": "wip_today",
                    "source_column": "WIP",
                    "aggregation": "sum",
                    "output_column": "WIP_SUM",
                },
            ],
        }
    }
    # ``ORG`` is absent from the model's raw join key, but it is declared in
    # the final output grain and both catalogs prove it.  The recovery must
    # aggregate and merge by the full output grain instead of duplicating it.
    steps = [
        {
            "node_id": "weak_raw_join",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "prod_src"},
                {"kind": "external_source", "ref": "wip_src"},
            ],
            "on": ["TECH"],
            "join_type": "outer",
            "population_policy": "preserve_all_metric_source_keys",
            "right_value_columns": ["WIP"],
        }
    ]
    merge = normalizer._resolve_metric_merge_plan(
        plan,
        candidates,
        jobs,
        steps,
        {"canonical_columns": ["TECH", "ORG"]},
        {},
        [],
    )
    assert [
        item["canonical_column"] for item in merge["grain_mappings"]
    ] == ["TECH", "ORG"]

    payload = {
        "intent_plan": {
            **plan,
            "retrieval_jobs": jobs,
            "pandas_execution_plan": steps,
            "resolved_metric_merge_plan": merge,
            "intent_ir": {"route_source_aliases": ["prod_src", "wip_src"]},
            "resolved_execution_graph": {"external_source_requirements": []},
        },
        "runtime_sources": {
            "prod_src": [
                {"TECH": "A", "ORG": "1", "PRODUCTION": 10},
                {"TECH": "A", "ORG": "2", "PRODUCTION": 20},
            ],
            "wip_src": [
                {"TECH": "A", "ORG": "1", "WIP": 3},
                {"TECH": "A", "ORG": "3", "WIP": 7},
            ],
        },
        "source_results": [
            {"source_alias": "prod_src", "dataset_key": "production_today", "status": "ok"},
            {"source_alias": "wip_src", "dataset_key": "wip_today", "status": "ok"},
        ],
        "trace": {"inspection": {}},
    }
    executed = executor.execute_hybrid_analysis(
        resolver.resolve_simple_analysis_contract(payload),
        "",
        model_invoker=lambda _: (_ for _ in ()).throw(
            AssertionError("full-grain recovery must not call pandas LLM")
        ),
        repair_prompt_template="repair",
    )
    assert executed["data"]["rows"] == [
        {"TECH": "A", "ORG": "1", "PRODUCTION_SUM": 10, "WIP_SUM": 3},
        {"TECH": "A", "ORG": "2", "PRODUCTION_SUM": 20, "WIP_SUM": 0},
        {"TECH": "A", "ORG": "3", "PRODUCTION_SUM": 0, "WIP_SUM": 7},
    ]


def test_row_enrichment_join_is_not_rewritten_as_independent_metric_merge():
    """Equipment/UPH-style raw joins retain their row-level relationship."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "domain_items": [],
        "main_flow_filters": [],
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NAME", "LEAD"]},
            },
            {
                "dataset_key": "eqp_uph",
                "payload": {"columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"]},
            },
        ],
    }
    jobs = [
        {"dataset_key": "equipment_assign", "source_alias": "assign_src", "filters": {}},
        {"dataset_key": "eqp_uph", "source_alias": "uph_src", "filters": {}},
    ]
    steps = [
        {
            "node_id": "join_assign_uph",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "assign_src"},
                {"kind": "external_source", "ref": "uph_src"},
            ],
            "output_alias": "joined_assign_uph",
            "left_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "right_on": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
            "join_type": "left",
            "population_policy": "left_source_only",
            "right_value_columns": ["UPH"],
        },
        {
            "node_id": "aggregate_by_lead",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "node_output", "ref": "joined_assign_uph"}],
            "output_alias": "lead_summary",
            "group_by": ["LEAD"],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                {"column": "UPH", "method": "mean", "output_column": "UPH_AVG"},
            ],
        },
    ]
    plan = {
        "output_contract": {
            "grain_columns": ["LEAD"],
            "metric_columns": ["EQP_COUNT", "UPH_AVG"],
            "required_columns": ["LEAD", "EQP_COUNT", "UPH_AVG"],
        }
    }

    merge = normalizer._resolve_metric_merge_plan(
        plan,
        candidates,
        jobs,
        steps,
        {"canonical_columns": ["LEAD"]},
        {},
        [],
    )
    assert merge == {}


def test_metric_merge_does_not_zero_fill_an_absent_average():
    """An unavailable average stays absent while additive quantities can be zero."""

    _, executor, _ = _modules()
    result = executor._execute_metric_source_merge(
        {
            "operation": "merge_metric_sources",
            "join_type": "outer",
            "fill_zero_on_success": True,
            "grain_mappings": [
                {
                    "canonical_column": "GROUP",
                    "output_column": "GROUP",
                    "source_candidates": {"quantity": ["GROUP"], "rate": ["GROUP"]},
                }
            ],
            "metrics": [
                {
                    "source_alias": "quantity",
                    "source_candidates": ["QTY"],
                    "output_column": "QTY_SUM",
                    "aggregation": "sum",
                    "fill_value": 0,
                    "fill_on_absence": True,
                },
                {
                    "source_alias": "rate",
                    "source_candidates": ["UPH"],
                    "output_column": "UPH_AVG",
                    "aggregation": "mean",
                    "fill_value": 0,
                    "fill_on_absence": False,
                },
            ],
        },
        {
            "quantity": pd.DataFrame([{"GROUP": "A", "QTY": 10}]),
            "rate": pd.DataFrame([{"GROUP": "B", "UPH": 200}]),
        },
        pd,
    )
    row_a = result.loc[result["GROUP"].eq("A")].iloc[0]
    row_b = result.loc[result["GROUP"].eq("B")].iloc[0]
    assert row_a["QTY_SUM"] == 10
    assert pd.isna(row_a["UPH_AVG"])
    assert row_b["QTY_SUM"] == 0
    assert row_b["UPH_AVG"] == 200


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


def test_v2_fast_path_uses_a_restored_previous_result_for_retrieval_free_top_n():
    """A safe prior-result transform must not spend a pandas-model call."""

    resolver, executor, _ = _modules()
    payload = {
        "intent_plan": {
            "request_scope": "followup_transform",
            "reference_mode": "previous_result_transform",
            "reuse_strategy": "previous_result",
            "retrieval_jobs": [],
            "pandas_execution_plan": [
                {
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "external_source", "ref": "previous_result"}],
                    "source_alias": "previous_result",
                    "sort_by": "EQP_COUNT",
                    "order": "desc",
                    "limit": 1,
                }
            ],
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": "previous_result",
                        "dataset_key": "",
                        "provider": "previous_result",
                        "required": True,
                    }
                ]
            },
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["PRODUCT"],
                "metric_columns": ["EQP_COUNT", "EQP_LIST", "PRODUCTION"],
                "metric_bindings": [
                    {
                        "source_alias": "previous_result",
                        "source_column": "EQP_COUNT",
                        "output_column": "EQP_COUNT",
                        "aggregation": "max",
                    },
                    {
                        "source_alias": "previous_result",
                        "source_column": "EQP_LIST",
                        "output_column": "EQP_LIST",
                        "aggregation": "collect_unique",
                    },
                    {
                        "source_alias": "previous_result",
                        "source_column": "PRODUCTION",
                        "output_column": "PRODUCTION",
                        "aggregation": "sum",
                    },
                ],
                "result_columns": ["PRODUCT", "EQP_COUNT", "EQP_LIST", "PRODUCTION"],
                "strict_result_columns": True,
                "ordering": {"sort_by": "EQP_COUNT", "order": "desc", "limit": 1},
            },
        },
        "runtime_sources": {
            "previous_result": [
                {"PRODUCT": "A", "EQP_COUNT": 1, "EQP_LIST": "E1", "PRODUCTION": 100},
                {"PRODUCT": "B", "EQP_COUNT": 3, "EQP_LIST": "E2, E3, E4", "PRODUCTION": 50},
            ]
        },
        "source_results": [],
    }

    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert resolved["simple_analysis_contract"]["source_alias"] == "previous_result"
    executed = executor.execute_hybrid_analysis(
        resolved,
        "unused",
        model_invoker=lambda prompt: (_ for _ in ()).throw(
            AssertionError("retrieval-free previous-result Fast path called pandas LLM")
        ),
        repair_prompt_template="repair",
    )
    assert executed["analysis"]["execution_route"] == "fast"
    assert executed["data"]["rows"] == [
        {"PRODUCT": "B", "EQP_COUNT": 3, "EQP_LIST": "E2, E3, E4", "PRODUCTION": 50}
    ]


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
    assert result["analysis"]["recovered_result"] == {
        "available": True,
        "checkpoint_key": "source:source_1",
        "checkpoint_role": "source_input",
        "row_count": 1,
    }


def test_v2_message_adapter_emphasizes_recovered_result_and_formats_download_link():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "answer_sections": {"summary": {"headline": "직전 결과를 확인할 수 있습니다."}},
        "analysis": {
            "status": "error",
            "error": {"type": "output_contract_violation", "message": "missing column"},
            "recovered_result": {
                "available": True,
                "checkpoint_key": "computed_result",
                "checkpoint_role": "computed_result",
                "row_count": 1,
            },
        },
        "data": {
            "columns": ["LOT_ID"],
            "rows": [{"LOT_ID": "L1"}],
            "row_count": 1,
            "partial": True,
        },
        "data_refs": [
            {
                "ref_id": "result:checkpoint",
                "role": "intermediate_result",
                "label": "최종 집계 전 중간 데이터",
                "download_url": "http://127.0.0.1:8765/download.csv?download_ref=result",
            }
        ],
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_intermediate_results=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert message.startswith("> ⚠️ **결과 계약 적용 단계에서 오류가 발생했습니다.**")
    assert "직전 정상 단계의 결과 데이터를 기반으로 답변을 생성했습니다." in message
    assert "📥" in message
    assert "<strong>최종 집계 전 중간 데이터 CSV 다운로드</strong>" in message


def test_v2_message_adapter_omits_recovery_notice_when_no_result_was_recovered():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    payload = {
        "answer_message": "데이터를 찾지 못했습니다.",
        "analysis": {
            "status": "error",
            "error": {"type": "output_contract_violation", "message": "missing column"},
        },
        "data": {"columns": [], "rows": [], "row_count": 0},
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "직전 정상 단계의 결과 데이터" not in message


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


def test_v2_message_adapter_keeps_control_characters_inside_one_table_cell_without_mutating_rows():
    adapter = load_module(V2_ROOT / "21_v2_answer_message_adapter.py")
    raw_detail = "첫 줄\r\n둘째 줄\t셋째 줄\u2028넷째|다섯째\r여섯째\x00끝"
    payload = {
        "answer_message": "결과를 확인했습니다.",
        "data": {
            "columns": ["HOLD_DESC"],
            "rows": [{"HOLD_DESC": raw_detail}],
            "row_count": 1,
        },
        "intermediate_results": [
            {
                "key": "pre_contract_result",
                "role": "computed_result",
                "description": "최종 집계 전 중간 데이터",
                "row_count": 1,
                "columns": ["HOLD_DESC"],
                "preview_rows": [{"HOLD_DESC": raw_detail}],
            }
        ],
    }

    message = adapter.build_message(
        payload,
        show_intermediate_results=True,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )
    rendered_cell = "| 첫 줄 ⏎ 둘째 줄 ⇥ 셋째 줄 ⏎ 넷째\\|다섯째 ⏎ 여섯째 끝 |"

    # 결과 표와 중간 결과 표가 같은 렌더러를 사용하며, 저장/다운로드용 원본 값은 바꾸지 않습니다.
    assert message.count(rendered_cell) == 2
    assert "\r" not in message
    assert "\t" not in message
    assert "\u2028" not in message
    assert payload["data"]["rows"][0]["HOLD_DESC"] == raw_detail
    assert payload["intermediate_results"][0]["preview_rows"][0]["HOLD_DESC"] == raw_detail


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


def test_v2_metric_scope_distinguishes_query_time_and_optional_dates_before_hydration():
    """A mixed actual-vs-target plan preserves each Catalog date role.

    The model can initially place a date phrase in ``required_params``.  For a
    registered Catalog that declares DATE only in ``filter_mappings``, the
    normalizer must leave the source selected and the Hydrator must later move
    that value to the trusted post-retrieval filter contract.  A second source
    that declares DATE as required must still use it to select its current-day
    Catalog and retain it as a query parameter.
    """
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    hydrator = load_module(
        ROOT
        / "langflow_components"
        / "data_analysis_flow"
        / "04a_trusted_retrieval_job_hydrator.py"
    )
    product_columns = [
        "DATE",
        "TECH",
        "DEN",
        "MODE",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "ORG",
        "LEAD",
        "MCP_NO",
    ]
    target_item = {
        "dataset_key": "target",
        "payload": {
            "source_type": "goodocs",
            "dataset_family": "goodocs_pkg_plan",
            "columns": [*product_columns, "OUT 계획"],
            "required_params": [],
            "filter_mappings": {
                "DATE": ["DATE"],
                "TECH": ["TECH"],
                "DEN": ["DEN"],
                "MODE": ["Mode"],
                "PKG_TYPE1": ["PKG1"],
                "PKG_TYPE2": ["PKG2"],
                "ORG": ["ORG"],
                "LEAD": ["LEAD"],
                "MCP_NO": ["MCP NO"],
                "OUT_PLAN_QTY": ["OUT 계획"],
            },
        },
    }
    candidates = {
        "table_catalog_items": [
            target_item,
            {
                "dataset_key": "production",
                "payload": {
                    "dataset_family": "production",
                    "time_scope": "history",
                    "required_params": ["DATE"],
                    "filter_mappings": {"DATE": ["WORK_DATE"]},
                    "columns": [*product_columns, "PRODUCTION"],
                },
            },
            {
                "dataset_key": "production_today",
                "payload": {
                    "dataset_family": "production",
                    "time_scope": "current_day",
                    "required_params": ["DATE"],
                    "filter_mappings": {"DATE": ["WORK_DATE"]},
                    "columns": [*product_columns, "PRODUCTION"],
                },
            },
        ]
    }
    raw_jobs = [
        {
            "dataset_key": "production",
            "source_alias": "prod_actual",
            "required_params": {"DATE": "20260811"},
        },
        {
            "dataset_key": "target",
            "source_alias": "tgt_plan",
            # This models a raw intent response.  It is not a trusted
            # query-time parameter because target has no Catalog-required DATE.
            "required_params": {"DATE": "20260811"},
        },
    ]
    pandas_plan = [
        {
            "node_id": "aggregate_actual",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "prod_actual"}],
            "output_alias": "actual_by_product",
            "source_alias": "prod_actual",
            "group_by": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "ORG", "LEAD", "MCP_NO"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "실적"}
            ],
        },
        {
            "node_id": "aggregate_target",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "tgt_plan"}],
            "output_alias": "target_by_product",
            "source_alias": "tgt_plan",
            "group_by": ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "ORG", "LEAD", "MCP_NO"],
            "aggregations": [
                {"column": "OUT 계획", "method": "sum", "output_column": "목표"}
            ],
        }
    ]

    jobs, scope_trace = normalizer._reconcile_metric_dataset_selection(
        {"request": {"reference_date": "20260811"}},
        raw_jobs,
        pandas_plan,
        candidates,
    )

    jobs_by_alias = {job["source_alias"]: job for job in jobs}
    assert jobs_by_alias["prod_actual"]["dataset_key"] == "production_today"
    assert jobs_by_alias["prod_actual"]["required_params"] == {"DATE": "20260811"}
    assert jobs_by_alias["tgt_plan"] == raw_jobs[1]
    assert scope_trace["status"] == "applied"
    assert scope_trace["unresolved"] == []
    assert scope_trace["corrections"] == [
        {
            "source_alias": "prod_actual",
            "from_dataset_key": "production",
            "to_dataset_key": "production_today",
            "metric_columns": [
                "PRODUCTION",
                "TECH",
                "DEN",
                "MODE",
                "PKG_TYPE1",
                "PKG_TYPE2",
                "ORG",
                "LEAD",
                "MCP_NO",
            ],
            "requested_time_scope": "current_day",
            "selection_source": "table_catalog.selection_criteria",
        }
    ]

    hydrated = hydrator.hydrate_retrieval_jobs(
        {
            "request": {"reference_date": "20260811"},
            "intent_plan": {
                "retrieval_jobs": jobs,
                "pandas_execution_plan": pandas_plan,
                "output_contract": {},
            },
        },
        candidates,
        "dummy",
    )
    hydrated_by_alias = {
        job["source_alias"]: job
        for job in hydrated["intent_plan"]["retrieval_jobs"]
    }
    assert hydrated_by_alias["prod_actual"]["required_params"] == {
        "DATE": "20260811"
    }
    assert hydrated_by_alias["tgt_plan"]["required_params"] == {}
    assert hydrated_by_alias["tgt_plan"]["filters"]["DATE"] == {
        "operator": "eq",
        "value": "20260811",
    }
    reconciliation = hydrated["trace"]["inspection"]["catalog_hydration"][
        "condition_reconciliation"
    ]
    assert reconciliation[-1]["moved_to_filters"] == ["DATE"]


def test_v2_source_scope_reconciliation_excludes_derived_aggregate_outputs():
    """A post-aggregate sort key must not be required from the raw Catalog."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    columns = normalizer._aggregation_source_columns_by_alias(
        [
            {
                "operation": "groupby_and_aggregate",
                "source_alias": "production_source",
                "inputs": [{"kind": "external_source", "ref": "production_source"}],
                "group_by": ["TECH"],
                "aggregations": [
                    {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION_SUM"}
                ],
            },
            {
                "operation": "sort_and_top_n",
                "source_alias": "production_source",
                "inputs": [{"kind": "node_output", "ref": "aggregate"}],
                "sort_by": "PRODUCTION_SUM",
                "limit": 3,
            },
        ],
        {"production_source"},
    )
    assert columns["production_source"] == ["PRODUCTION", "TECH"]


def test_v2_normalizer_canonicalizes_unambiguous_filter_shorthand():
    """Raw scalar/list filters become the deterministic executor contract."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs, trace = normalizer._normalize_retrieval_filter_operators(
        [
            {
                "dataset_key": "production_today",
                "filters": {
                    "DATE": "20260701",
                    "OPER_NAME": ["D/A1", "D/A2"],
                    "HOLD_STAT": {"operator": "=", "value": "OnHold"},
                },
            }
        ]
    )
    filters = jobs[0]["filters"]
    assert filters["DATE"] == {"operator": "eq", "value": "20260701"}
    assert filters["OPER_NAME"] == {"operator": "in", "value": ["D/A1", "D/A2"]}
    assert filters["HOLD_STAT"] == {"operator": "eq", "value": "OnHold"}
    assert trace["status"] == "applied"


def test_v2_normalizer_preserves_explicit_prefix_intent_for_the_same_literal_only():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs, trace = normalizer._normalize_retrieval_filter_operators(
        [
            {
                "dataset_key": "sample",
                "source_alias": "sample_source",
                "filters": {
                    "MCP_NO": {"operator": "eq", "value": "L-116"},
                    "OPER_NAME": {"operator": "eq", "value": "W/B1"},
                    "DATE": {"operator": "eq", "value": "20260701"},
                },
            }
        ],
        question="F315 L-116로 시작하는 제품 WB 공정 차수별 UPH 알려줘",
    )
    filters = jobs[0]["filters"]
    assert filters["MCP_NO"]["operator"] == "starts_with"
    assert filters["OPER_NAME"]["operator"] == "eq"
    assert filters["DATE"]["operator"] == "eq"
    assert any(change.get("reason") == "explicit_prefix_cue_for_same_literal" for change in trace["changes"])


def test_v2_normalizer_repairs_a_prefix_literal_embedded_in_a_compound_filter():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs, trace = normalizer._normalize_retrieval_filter_operators(
        [
            {
                "dataset_key": "sample",
                "source_alias": "sample_source",
                "filters": {
                    "MCP_NO": {"operator": "contains", "value": "F315 L-116"},
                    "OPER_NAME": {"operator": "eq", "value": "W/B1"},
                    "DATE": {"operator": "eq", "value": "20260701"},
                },
            }
        ],
        question="F315 L-116로 시작하는 제품 WB 공정 차수별 UPH 알려줘",
    )

    filters = jobs[0]["filters"]
    # The repair does not choose a new field or discard F315.  It only fixes
    # the already selected field's malformed compound literal to the explicit
    # prefix in the user's wording; F315 remains available to its helper.
    assert filters["MCP_NO"] == {"operator": "starts_with", "value": "L-116"}
    assert filters["OPER_NAME"]["operator"] == "eq"
    assert filters["DATE"]["operator"] == "eq"
    assert any(
        change.get("reason") == "explicit_prefix_literal_extracted_from_compound_filter"
        for change in trace["changes"]
    )


def test_v2_normalizer_removes_helper_only_when_one_typed_filter_covers_its_full_input():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    cases, steps, trace = normalizer._remove_source_filter_sufficient_function_cases(
        [
            {
                "key": "generic_match",
                "function_name": "generic_match",
                "input_text": "L-116",
                "source_alias": "source",
            }
        ],
        [
            {
                "operation": "apply_pandas_function_case",
                "source_alias": "source",
            }
        ],
        [
            {
                "dataset_key": "sample",
                "source_alias": "source",
                "filters": {"MCP_NO": {"operator": "starts_with", "value": "L-116"}},
            }
        ],
    )
    assert cases == []
    assert steps == []
    assert trace["removed"][0]["filter_field"] == "MCP_NO"

    retained, retained_steps, retained_trace = normalizer._remove_source_filter_sufficient_function_cases(
        [
            {
                "key": "generic_match",
                "function_name": "generic_match",
                "input_text": "SP 24G GDDR7",
                "source_alias": "source",
            }
        ],
        [{"operation": "apply_pandas_function_case", "source_alias": "source"}],
        [
            {
                "dataset_key": "sample",
                "source_alias": "source",
                "filters": {"MCP_NO": {"operator": "starts_with", "value": "L-116"}},
            }
        ],
    )
    assert len(retained) == 1
    assert len(retained_steps) == 1
    assert retained_trace["removed"] == []


def test_v2_normalizer_removes_model_selected_product_helper_when_typed_filters_cover_all_tokens():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    question = "7/5 FCB1, FCB2, FCB/H process production summary"
    cases, steps, trace = normalizer._remove_source_filter_sufficient_function_cases(
        [
            {
                "key": "match_product_tokens",
                "function_name": "match_product_tokens",
                "input_text": question,
                "source_alias": "production_source",
            }
        ],
        [
            {
                "operation": "apply_pandas_function_case",
                "function_name": "match_product_tokens",
                "source_alias": "production_source",
            },
            {"operation": "groupby_and_aggregate", "source_alias": "production_source"},
        ],
        [
            {
                "dataset_key": "production",
                "source_alias": "production_source",
                "filters": {
                    "OPER_NAME": {
                        "operator": "in",
                        "value": ["FCB1", "FCB2", "FCB/H"],
                    },
                    "DATE": {"operator": "eq", "value": "20260705"},
                },
            }
        ],
        {},
    )

    assert cases == []
    assert [step["operation"] for step in steps] == ["groupby_and_aggregate"]
    assert trace["status"] == "applied"
    assert trace["removed"][0]["reason"] == (
        "typed_source_filters_cover_all_structured_product_tokens"
    )
    assert trace["removed"][0]["covered_tokens"] == ["FCB1", "FCB2"]


def test_v2_normalizer_keeps_product_helper_when_a_product_token_is_not_in_typed_filters():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    cases, steps, trace = normalizer._remove_source_filter_sufficient_function_cases(
        [
            {
                "key": "match_product_tokens",
                "function_name": "match_product_tokens",
                "input_text": "F315 L-116 WB UPH",
                "source_alias": "eqp_source",
            }
        ],
        [{"operation": "apply_pandas_function_case", "source_alias": "eqp_source"}],
        [
            {
                "dataset_key": "eqp_uph",
                "source_alias": "eqp_source",
                "filters": {
                    "MCP_NO": {"operator": "starts_with", "value": "L-116"},
                    "OPER_NAME": {"operator": "eq", "value": "W/B1"},
                },
            }
        ],
        {},
    )

    assert len(cases) == 1
    assert len(steps) == 1
    assert trace["removed"] == []


def test_v2_pruned_product_helper_rewires_unique_downstream_alias_to_source():
    """Removing a source-equivalent helper must not leave a dangling DAG edge."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "equipment_assign",
            "filters": {"DEVICE": {"operator": "eq", "value": "L-256K9B"}},
        }
    ]
    cases, steps, trace = normalizer._remove_source_filter_sufficient_function_cases(
        [
            {
                "key": "product_token_match",
                "function_name": "match_product_tokens",
                "input_text": "L-256K9B",
                "source_alias": "equipment_assign",
            }
        ],
        [
            {
                "node_id": "group_by_process",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "node_output", "ref": "filtered_product"}],
                "output_alias": "process_equipment_count",
                "group_by": ["OPER_NAME"],
                "aggregations": [
                    {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"}
                ],
            }
        ],
        jobs,
        {},
    )

    assert cases == []
    assert steps[0]["inputs"] == [
        {"kind": "external_source", "ref": "equipment_assign"}
    ]
    assert trace["rewired_inputs"] == [
        {
            "node_id": "group_by_process",
            "from_ref": "filtered_product",
            "source_alias": "equipment_assign",
            "reason": "removed_function_case_unique_implicit_output",
        }
    ]
    graph = normalizer._compile_execution_graph(steps, jobs, {}, "none")
    assert graph["validation_errors"] == []


def test_v2_auto_product_helper_skips_process_tokens_already_covered_by_typed_filters():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, trace = normalizer._auto_select_metadata_function_case(
        {},
        [
            {
                "dataset_key": "production",
                "source_alias": "production_source",
                "filters": {
                    "OPER_NAME": {
                        "operator": "in",
                        "value": ["FCB1", "FCB2", "FCB/H"],
                    }
                },
            }
        ],
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
        "7/5 FCB1, FCB2, FCB/H process production summary",
    )

    assert "pandas_function_cases" not in plan
    assert trace["status"] == "not_needed"
    assert trace["reason"] == "no_uncovered_structured_product_tokens"


def test_v2_process_summary_executes_fast_after_product_helper_is_pruned():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    rows = [
        {"DATE": "20260705", "OPER_NAME": "FCB1", "PRODUCTION": 10},
        {"DATE": "20260705", "OPER_NAME": "FCB2", "PRODUCTION": 20},
        {"DATE": "20260705", "OPER_NAME": "FCB/H", "PRODUCTION": 30},
        {"DATE": "20260705", "OPER_NAME": "DA1", "PRODUCTION": 99},
    ]
    steps = [
        {
            "node_id": "aggregate_by_process",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "production_source"}],
            "output_alias": "process_summary",
            "source_alias": "production_source",
            "group_by": ["OPER_NAME"],
            "aggregations": [
                {
                    "column": "PRODUCTION",
                    "method": "sum",
                    "output_column": "PRODUCTION_SUM",
                }
            ],
        }
    ]
    filters = {
        "DATE": {"operator": "eq", "value": "20260705"},
        "OPER_NAME": {"operator": "in", "value": ["FCB1", "FCB2", "FCB/H"]},
    }
    payload = _single_source_payload(
        rows=rows,
        steps=steps,
        filters=filters,
        source_alias="production_source",
        dataset_key="production",
        output_contract={
            "result_mode": "detail",
            "grain_columns": ["OPER_NAME"],
            "metric_columns": ["PRODUCTION_SUM"],
            "result_columns": ["OPER_NAME", "PRODUCTION_SUM"],
            "required_columns": ["OPER_NAME", "PRODUCTION_SUM"],
            "strict_result_columns": True,
        },
    )
    cases, normalized_steps, trace = normalizer._remove_source_filter_sufficient_function_cases(
        [
            {
                "key": "match_product_tokens",
                "function_name": "match_product_tokens",
                "input_text": "7/5 FCB1, FCB2, FCB/H process production summary",
                "source_alias": "production_source",
            }
        ],
        [
            {"operation": "apply_pandas_function_case", "source_alias": "production_source"},
            *steps,
        ],
        payload["intent_plan"]["retrieval_jobs"],
        {},
    )
    assert cases == []
    assert trace["status"] == "applied"
    payload["intent_plan"]["pandas_execution_plan"] = normalized_steps
    resolved, executed, model_calls = _resolve_and_execute(payload)

    assert resolved["simple_analysis_contract"]["route"] == "fast"
    assert model_calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "FCB/H", "PRODUCTION_SUM": 30.0},
        {"OPER_NAME": "FCB1", "PRODUCTION_SUM": 10.0},
        {"OPER_NAME": "FCB2", "PRODUCTION_SUM": 20.0},
    ]


def test_v2_executor_rejects_stale_helper_keyword_contract_before_execution():
    _, executor, _ = _modules()
    contract = {
        "source_transforms": [
            {
                "function_name": "match_product_tokens",
                "source_alias": "source_1",
                "input_text": "L-116",
                "arguments": {"excluded_tokens": ["UPH"]},
            }
        ]
    }
    stale_helper = "def match_product_tokens(input_text, frame):\n    return frame.copy()\n"
    preamble, error = executor._deterministic_function_case_preamble(contract, stale_helper)

    assert preamble == ""
    assert "excluded_tokens" in error
    assert "최신 버전" in error


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


def test_metadata_candidates_prioritize_matching_clause_from_worker_written_catalog_usage_list():
    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )
    result = builder.build_metadata_candidates(
        {"request": {"question": "현재 D/A1 공정에 배정된 장비를 장비 모델과 Recipe 조합별로 보여줘"}},
        [],
        [
            {
                "section": "table_catalog",
                "key": "equipment_assign",
                "dataset_key": "equipment_assign",
                "payload": {
                    "selection_criteria": {
                        "use_when": [
                            "배정된 장비, 할당 장비, 장비 목록, 장비 대수, 장비 모델과 레시피 조합을 조회할 때"
                        ],
                    },
                    "columns": ["EQP_ID", "EQP_MODEL", "RECIPE_ID", "OPER_NAME"],
                },
            },
            {
                "section": "table_catalog",
                "key": "eqp_uph",
                "dataset_key": "eqp_uph",
                "payload": {
                    "selection_criteria": {
                        "exclude_when": ["장비 목록, 장비 대수, 장비 모델과 레시피 조합만 물어볼 때"],
                    },
                    "metric_semantics": {"UPH": {"default_rollup": "mean"}},
                    "columns": ["EQP_MODEL", "RECIPE_ID", "OPER_NAME", "UPH"],
                },
            },
        ],
        [],
        min_table_items=2,
        max_table_items=2,
    )
    by_key = {
        item["dataset_key"]: item["intent_selection_hint"]
        for item in result["metadata_candidates"]["table_catalog_items"]
    }
    assert "장비 모델과 레시피 조합을 조회할 때" in by_key["equipment_assign"]["matched_use_when"]
    assert "장비 모델과 레시피 조합만 물어볼 때" in by_key["eqp_uph"]["matched_exclude_when"]


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
    assert plan["pandas_function_cases"][0]["input_text"] == "FCB production SP 24G GDDR7 X32 226 FCBGA DDP"
    assert trace["status"] == "applied"


def test_v2_auto_selected_product_helper_excludes_typed_metric_words_only():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan, trace = normalizer._auto_select_metadata_function_case(
        {
            "output_contract": {"metric_columns": ["UPH"]},
            "pandas_execution_plan": [
                {
                    "operation": "groupby_and_aggregate",
                    "aggregations": [
                        {"column": "UPH", "method": "mean", "output_column": "AVG_UPH"}
                    ],
                }
            ],
        },
        [{"dataset_key": "eqp_uph", "source_alias": "eqp_uph_source"}],
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
        "F315 L-116 제품 WB 공정 차수별 UPH 알려줘",
    )

    case = plan["pandas_function_cases"][0]
    assert case["input_text"] == "F315 L-116 제품 WB 공정 차수별 UPH 알려줘"
    assert case["arguments"]["excluded_tokens"] == ["UPH", "AVG_UPH"]
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


def test_v2_previous_preview_identifier_is_not_mistaken_for_a_direct_required_parameter():
    """A model-copied identifier must retain the catalog-proven follow-up path."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs = [
        {
            "dataset_key": "hold_history",
            "source_alias": "hold_history_src",
            "required_params": {"LOT_ID": ["LOT-A"]},
            "filters": {},
        }
    ]
    candidates = _followup_catalog_candidates()

    assert normalizer._direct_required_parameter_evidence(
        jobs,
        candidates,
        "위 LOT의 HOLD 이력 알려줘",
    ) == []
    assert normalizer._direct_required_parameter_evidence(
        jobs,
        candidates,
        "LOT-A HOLD 이력 알려줘",
    ) == [
        {
            "dataset_key": "hold_history",
            "source_alias": "hold_history_src",
            "required_params": ["LOT_ID"],
        }
    ]


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


def test_v2_followup_normalizes_legacy_previous_result_reference_mode_with_new_retrieval():
    """A legacy spelling must not erase a valid previous-result row match."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_columns = [
        "TECH",
        "DEN",
        "MODE",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "LEAD",
        "MCP_NO",
    ]
    payload = {
        "request": {"question": "이 제품들에 할당된 장비 대수와 장비 목록을 보여줘"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_requery",
            "reuse_strategy_hint": "previous_result",
        },
        "state": {
            "current_data": {
                "columns": [*product_columns, "PRODUCTION"],
                "result_columns": [*product_columns, "PRODUCTION"],
            },
            "last_intent_plan": {
                "output_contract": {"grain_columns": product_columns},
            },
        },
    }
    response = {
        "intent_plan": {
            "analysis_kind": "generic_equipment_followup",
            "request_scope": "followup_transform",
            # This is the legacy spelling produced by some model responses.
            "reference_mode": "previous_result",
            "retrieval_jobs": [
                {
                    "dataset_key": "equipment_assign",
                    "source_alias": "equipment_assign_src",
                    "filters": {},
                }
            ],
            "pandas_execution_plan": [
                {
                    "operation": "apply_row_match_groups",
                    "source_alias": "equipment_assign_src",
                    # Some model responses put the reference-mode spelling in
                    # the step instead of the executable runtime alias.
                    "reference_source_alias": "previous_result_rows",
                },
                {
                    "operation": "groupby_and_aggregate",
                    "inputs": [
                        {"kind": "node_output", "ref": "equipment_assign_src"}
                    ],
                    "group_by": product_columns,
                    "aggregations": [
                        {
                            "column": "EQP_ID",
                            "method": "nunique",
                            "output_column": "EQP_COUNT",
                        }
                    ],
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": product_columns,
                "result_columns": [*product_columns, "EQP_COUNT"],
                "metric_columns": ["EQP_COUNT"],
                "metric_bindings": [
                    {
                        "source_alias": "equipment_assign_src",
                        "dataset_key": "equipment_assign",
                        "source_column": "EQP_ID",
                        "aggregation": "nunique",
                        "output_column": "EQP_COUNT",
                    }
                ],
            },
        }
    }
    candidates = {
        "domain_items": [],
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": [*product_columns, "EQP_ID"]},
            }
        ],
        "main_flow_filters": [],
    }

    normalized = normalizer.normalize_intent_plan(payload, json.dumps(response), candidates)
    plan = normalized["intent_plan"]

    assert plan["reference_mode"] == "previous_result_rows"
    assert plan["request_scope"] == "followup_requery"
    assert plan["reuse_strategy"] == "previous_result"
    row_match = next(
        step
        for step in plan["pandas_execution_plan"]
        if step.get("operation") == "apply_row_match_groups"
    )
    assert row_match["reference_source_alias"] == "previous_result"
    assert row_match["match_columns"] == product_columns
    assert not any(
        error.get("type") == "invalid_reference_mode_contract"
        for error in plan.get("validation_errors", [])
    )


def test_row_match_recovers_an_unresolved_model_reference_alias_to_previous_result():
    """A follow-up may safely repair only an alias no current DAG can provide."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_columns = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    payload = {
        "state": {
            "current_data": {"columns": [*product_columns, "PRODUCTION"]},
            "last_intent_plan": {
                "resolved_grain_plan": {"canonical_columns": product_columns}
            },
        }
    }
    steps, trace = normalizer._normalize_row_match_steps(
        [
            {
                "node_id": "node_row_match",
                "operation": "apply_row_match_groups",
                "inputs": [{"kind": "external_source", "ref": "eqp_src"}],
                "source_alias": "eqp_src",
                # ``prod_src`` is neither a retrieval job nor a node output;
                # it is a weak-model nickname for the restored prior result.
                "reference_source_alias": "prod_src",
                "match_columns": product_columns,
                "output_alias": "filtered_eqp_src",
            }
        ],
        [{"dataset_key": "equipment_assign", "source_alias": "eqp_src"}],
        "previous_result_rows",
        payload,
    )

    row_match = steps[0]
    assert row_match["reference_source_alias"] == "previous_result"
    assert row_match["match_columns"] == product_columns
    assert row_match["inputs"] == [{"kind": "external_source", "ref": "eqp_src"}]
    assert trace["steps"][0]["reference_alias_reconciliation"] == "unresolved_alias_to_previous_result"


def test_reference_join_resolves_right_metrics_after_filter_and_manual_join():
    """Follow-up metrics may be aggregated from a joined, not raw, alias.

    The deterministic contract must trace the right retrieval source through
    a filter and manual join while refusing a left-only measure.  This keeps
    the behavior portable for every catalog-backed enrichment query.
    """

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_columns = ["TECH", "DEN", "MODE"]
    payload = {
        "state": {
            "current_data": {"columns": [*product_columns, "PRODUCTION"]},
            "last_intent_plan": {
                "resolved_grain_plan": {
                    "column_mappings": [
                        {"canonical_key": column, "source_candidates": [column]}
                        for column in product_columns
                    ]
                }
            },
        }
    }
    candidates = {
        "domain_items": [],
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": [*product_columns, "EQP_ID"]},
            }
        ],
        "main_flow_filters": [],
    }
    jobs = [
        {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"}
    ]
    pandas_plan = [
        {
            "node_id": "match_equipment",
            "operation": "apply_row_match_groups",
            "source_alias": "equipment_assign",
            "reference_source_alias": "previous_result",
            "match_columns": product_columns,
            "output_alias": "matched_equipment",
        },
        {
            "node_id": "filter_equipment",
            "operation": "apply_filters",
            "source_alias": "equipment_assign",
            "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
            "output_alias": "filtered_equipment",
        },
        {
            "node_id": "join_equipment",
            "operation": "join",
            "source_alias": "previous_result",
            "inputs": [
                {"kind": "external_source", "ref": "previous_result"},
                {"kind": "node_output", "ref": "filtered_equipment"},
            ],
            "output_alias": "joined_equipment",
        },
        {
            "node_id": "aggregate_equipment",
            "operation": "groupby_and_aggregate",
            "source_alias": "joined_equipment",
            "inputs": [{"kind": "node_output", "ref": "joined_equipment"}],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                {"column": "PRODUCTION", "method": "sum", "output_column": "LEFT_ONLY_SUM"},
            ],
        },
    ]

    contract = normalizer._resolve_reference_join_plan(
        payload,
        candidates,
        jobs,
        pandas_plan,
        "previous_result_rows",
    )

    assert contract["right_source_alias"] == "equipment_assign"
    assert contract["aggregations"] == [
        {
            "source_column": "EQP_ID",
            "aggregation": "nunique",
            "output_column": "EQP_COUNT",
            "source_candidates": ["EQP_ID"],
        }
    ]


def test_reference_join_resolves_row_match_target_through_filtered_node_output():
    """A follow-up may match the output of a preceding filter, not a raw source.

    The source alias in a weak-model plan can legitimately be the preceding
    ``output_alias``.  The normalizer must resolve that Typed-IR lineage to the
    one catalog-backed retrieval job; otherwise the executor attempts to look
    up an alias that never existed in ``sources``.
    """

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_columns = ["TECH", "DEN", "MODE"]
    payload = {
        "state": {
            "current_data": {"columns": [*product_columns, "PRODUCTION"]},
            "last_intent_plan": {
                "resolved_grain_plan": {
                    "column_mappings": [
                        {"canonical_key": column, "source_candidates": [column]}
                        for column in product_columns
                    ]
                }
            },
        }
    }
    candidates = {
        "domain_items": [],
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": [*product_columns, "EQP_ID"]},
            }
        ],
        "main_flow_filters": [],
    }
    jobs = [{"dataset_key": "equipment_assign", "source_alias": "equipment_assign"}]
    pandas_plan = [
        {
            "node_id": "filter_equipment_assign",
            "operation": "apply_filters",
            "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
            "source_alias": "equipment_assign",
            "output_alias": "filtered_equipment_assign",
        },
        {
            "node_id": "match_previous_products",
            "operation": "apply_row_match_groups",
            "inputs": [{"kind": "node_output", "ref": "filter_equipment_assign"}],
            "source_alias": "filtered_equipment_assign",
            "reference_source_alias": "previous_result",
            "match_columns": product_columns,
            "blank_policy": "normalize_blank",
            "output_alias": "matched_equipment_assign",
        },
        {
            "node_id": "aggregate_equipment",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "node_output", "ref": "match_previous_products"}],
            "source_alias": "matched_equipment_assign",
            "output_alias": "product_equipment_summary",
            "group_by": product_columns,
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_LIST"},
            ],
        },
    ]

    contract = normalizer._resolve_reference_join_plan(
        payload,
        candidates,
        jobs,
        pandas_plan,
        "previous_result_rows",
    )

    assert contract["right_source_alias"] == "equipment_assign"
    assert [item["canonical_key"] for item in contract["key_mappings"]] == product_columns
    assert contract["aggregations"] == [
        {
            "source_column": "EQP_ID",
            "aggregation": "nunique",
            "output_column": "EQP_COUNT",
            "source_candidates": ["EQP_ID"],
        },
        {
            "source_column": "EQP_ID",
            "aggregation": "collect_unique",
            "output_column": "EQP_LIST",
            "source_candidates": ["EQP_ID"],
        },
    ]


def test_v2_typed_row_match_uses_filtered_node_output_and_prior_rows_without_model():
    """Validated follow-up DAGs execute filter -> row match -> aggregate in order."""

    resolver, executor, _ = _modules()
    product_columns = ["TECH", "DEN", "MODE"]
    payload = {
        "question": "assigned item count and list by prior groups",
        "intent_plan": {
            "reference_mode": "previous_result_rows",
            "retrieval_jobs": [
                {
                    "dataset_key": "equipment_assign",
                    "source_alias": "equipment_assign",
                    "filters": {},
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_equipment_assign",
                    "operation": "apply_filters",
                    "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
                    "source_alias": "equipment_assign",
                    "output_alias": "filtered_equipment_assign",
                },
                {
                    "node_id": "match_previous_products",
                    "operation": "apply_row_match_groups",
                    "inputs": [{"kind": "node_output", "ref": "filter_equipment_assign"}],
                    "source_alias": "filtered_equipment_assign",
                    "reference_source_alias": "previous_result",
                    "match_columns": product_columns,
                    "blank_policy": "normalize_blank",
                    "output_alias": "matched_equipment_assign",
                },
                {
                    "node_id": "aggregate_equipment",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "match_previous_products"}],
                    "source_alias": "matched_equipment_assign",
                    "output_alias": "product_equipment_summary",
                    "group_by": product_columns,
                    "aggregations": [
                        {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                        {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_LIST"},
                    ],
                },
            ],
            "output_contract": {
                "strict_result_columns": True,
                "result_mode": "aggregate",
                "grain_columns": product_columns,
                "metric_columns": ["EQP_COUNT", "EQP_LIST"],
                "required_columns": [*product_columns, "EQP_COUNT", "EQP_LIST"],
                "result_columns": [*product_columns, "EQP_COUNT", "EQP_LIST"],
            },
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": "equipment_assign",
                        "dataset_key": "equipment_assign",
                        "provider": "retrieval_job",
                        "required": True,
                    },
                    {
                        "source_alias": "previous_result",
                        "dataset_key": "previous_result",
                        "provider": "previous_result",
                        "required": True,
                    },
                ],
                "validation_errors": [],
            },
            "validation_errors": [],
        },
        "runtime_sources": {
            "previous_result": [
                {"TECH": "A", "DEN": "1", "MODE": "M", "PRODUCTION": 50},
                {"TECH": "B", "DEN": "2", "MODE": "N", "PRODUCTION": 40},
            ],
            "equipment_assign": [
                {"TECH": "A", "DEN": "1", "MODE": "M", "EQP_ID": "E-01"},
                {"TECH": "A", "DEN": "1", "MODE": "M", "EQP_ID": "E-02"},
                {"TECH": "A", "DEN": "1", "MODE": "M", "EQP_ID": "E-02"},
                {"TECH": "B", "DEN": "2", "MODE": "N", "EQP_ID": "E-03"},
                {"TECH": "C", "DEN": "3", "MODE": "X", "EQP_ID": "E-99"},
            ],
        },
        "source_results": [
            {
                "source_alias": "equipment_assign",
                "dataset_key": "equipment_assign",
                "status": "ok",
                "columns": [*product_columns, "EQP_ID"],
            }
        ],
        "trace": {"inspection": {}},
    }

    resolved = resolver.resolve_simple_analysis_contract(payload)
    contract = resolved["simple_analysis_contract"]
    assert contract["route"] == "complex"
    assert contract["operation"] == "execute_typed_pandas_plan"
    assert contract["requires_pandas_llm"] is False

    calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "must not be used for a valid typed follow-up",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        repair_prompt_template="repair",
    )

    assert calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"TECH": "A", "DEN": "1", "MODE": "M", "EQP_COUNT": 2, "EQP_LIST": "E-01, E-02"},
        {"TECH": "B", "DEN": "2", "MODE": "N", "EQP_COUNT": 1, "EQP_LIST": "E-03"},
    ]


def test_v2_pandas_fallback_materializes_filtered_alias_before_row_match():
    """The guarded LLM fallback preserves Typed filter/output alias semantics."""

    _, executor, _ = _modules()
    payload = {
        "runtime_sources": {
            "previous_result": [
                {"TECH": "A", "DEN": "1", "MODE": "M"},
            ],
            "equipment_assign": [
                {"TECH": "A", "DEN": "1", "MODE": "M", "OPER_NAME": "D/A1", "EQP_ID": "E-01"},
                {"TECH": "C", "DEN": "3", "MODE": "X", "OPER_NAME": "D/A1", "EQP_ID": "E-99"},
                {"TECH": "A", "DEN": "1", "MODE": "M", "OPER_NAME": "OTHER", "EQP_ID": "E-02"},
            ],
        },
        "intent_plan": {
            "retrieval_jobs": [
                {
                    "dataset_key": "equipment_assign",
                    "source_alias": "equipment_assign",
                    "filters": {
                        "OPER_NAME": {"operator": "eq", "value": "D/A1"},
                    },
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_equipment_assign",
                    "operation": "apply_filters",
                    "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
                    "source_alias": "equipment_assign",
                    "output_alias": "filtered_equipment_assign",
                },
                {
                    "node_id": "match_previous_products",
                    "operation": "apply_row_match_groups",
                    "inputs": [{"kind": "node_output", "ref": "filter_equipment_assign"}],
                    "source_alias": "filtered_equipment_assign",
                    "reference_source_alias": "previous_result",
                    "match_columns": ["TECH", "DEN", "MODE"],
                    "blank_policy": "normalize_blank",
                    "output_alias": "matched_equipment_assign",
                },
            ],
        },
    }
    filter_plan = executor._pandas_filter_plan(payload)
    row_match_plan = executor._pandas_row_match_plan(payload)
    code = executor._with_pandas_execution_preambles(
        "result = sources['matched_equipment_assign'].copy()",
        executor._pandas_row_match_preamble(row_match_plan),
        executor._pandas_filter_preamble(filter_plan),
    )
    namespace = {
        "pd": pd,
        "sources": {
            alias: pd.DataFrame(rows)
            for alias, rows in payload["runtime_sources"].items()
        },
    }

    exec(code, namespace)

    assert namespace["result"].to_dict(orient="records") == [
        {"TECH": "A", "DEN": "1", "MODE": "M", "OPER_NAME": "D/A1", "EQP_ID": "E-01"},
    ]


def test_v2_filtered_row_match_enrichment_preserves_each_prior_product():
    """The production→equipment follow-up keeps top products with zero equipment."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    product_columns = ["TECH", "DEN", "MODE"]
    payload = {
        "state": {
            "current_data": {"columns": [*product_columns, "PRODUCTION"]},
            "last_intent_plan": {
                "resolved_grain_plan": {
                    "column_mappings": [
                        {"canonical_key": column, "source_candidates": [column]}
                        for column in product_columns
                    ]
                }
            },
        }
    }
    candidates = {
        "domain_items": [],
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": [*product_columns, "EQP_ID"]},
            }
        ],
        "main_flow_filters": [],
    }
    plan = [
        {
            "node_id": "filter_equipment_assign",
            "operation": "apply_filters",
            "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
            "source_alias": "equipment_assign",
            "output_alias": "filtered_equipment_assign",
        },
        {
            "node_id": "match_previous_products",
            "operation": "apply_row_match_groups",
            "inputs": [{"kind": "node_output", "ref": "filter_equipment_assign"}],
            "source_alias": "filtered_equipment_assign",
            "reference_source_alias": "previous_result",
            "match_columns": product_columns,
            "output_alias": "matched_equipment_assign",
        },
        {
            "node_id": "aggregate_equipment",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "node_output", "ref": "match_previous_products"}],
            "source_alias": "matched_equipment_assign",
            "group_by": product_columns,
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_LIST"},
            ],
        },
    ]
    contract = normalizer._resolve_reference_join_plan(
        payload,
        candidates,
        [{"dataset_key": "equipment_assign", "source_alias": "equipment_assign"}],
        plan,
        "previous_result_rows",
    )

    result = executor._execute_deterministic_contract(
        contract,
        {
            "previous_result": pd.DataFrame(
                [
                    {"TECH": "A", "DEN": "1", "MODE": "M", "PRODUCTION": 50},
                    {"TECH": "B", "DEN": "2", "MODE": "N", "PRODUCTION": 40},
                ]
            ),
            "equipment_assign": pd.DataFrame(
                [
                    {"TECH": "A", "DEN": "1", "MODE": "M", "EQP_ID": "E-01"},
                    {"TECH": "A", "DEN": "1", "MODE": "M", "EQP_ID": "E-02"},
                ]
            ),
        },
        pd,
    )

    assert result.to_dict(orient="records") == [
        {"TECH": "A", "DEN": "1", "MODE": "M", "PRODUCTION": 50, "EQP_COUNT": 2, "EQP_LIST": "E-01, E-02"},
        {"TECH": "B", "DEN": "2", "MODE": "N", "PRODUCTION": 40, "EQP_COUNT": 0, "EQP_LIST": ""},
    ]


def test_v2_followup_normalizes_rows_mode_to_transform_without_new_retrieval():
    """A retrieval-free top-N follow-up operates on the prior result itself."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    payload = {
        "request": {"question": "그중 수량이 가장 많은 항목만 보여줘"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_transform",
            "reuse_strategy_hint": "previous_result",
        },
        "state": {
            "current_data": {
                "columns": ["PRODUCT", "COUNT"],
                "result_columns": ["PRODUCT", "COUNT"],
            },
            "last_intent_plan": {
                "output_contract": {"grain_columns": ["PRODUCT"]},
            },
        },
    }
    response = {
        "intent_plan": {
            "analysis_kind": "generic_previous_result_top_n",
            "request_scope": "followup_transform",
            # A weak model may use the requery-specific spelling here.
            "reference_mode": "previous_result_rows",
            "retrieval_jobs": [],
            "pandas_execution_plan": [
                {
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "external_source", "ref": "previous_result"}],
                    "source_alias": "previous_result",
                    "sort_by": "COUNT",
                    "order": "desc",
                    "limit": 1,
                }
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "grain_columns": ["PRODUCT"],
                "metric_columns": ["COUNT"],
                "metric_bindings": [
                    {
                        "source_alias": "previous_result",
                        "dataset_key": "previous_result",
                        "source_column": "COUNT",
                        "aggregation": "max",
                        "output_column": "COUNT",
                    }
                ],
                "result_columns": ["PRODUCT", "COUNT"],
                "strict_result_columns": True,
                "ordering": {"sort_by": "COUNT", "order": "desc", "limit": 1},
            },
        }
    }

    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps(response),
        {"domain_items": [], "table_catalog_items": [], "main_flow_filters": []},
    )
    plan = normalized["intent_plan"]

    assert plan["reference_mode"] == "previous_result_transform"
    assert plan["request_scope"] == "followup_transform"
    assert plan["reuse_strategy"] == "previous_result"
    assert plan["output_contract"]["metric_bindings"] == [
        {
            "source_alias": "previous_result",
            "dataset_key": "previous_result",
            "source_column": "COUNT",
            "aggregation": "max",
            "output_column": "COUNT",
            "semantic_scope": {},
        }
    ]
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


def test_v2_normalizer_repairs_markdown_list_key_typo_without_model_retry():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    parsed = normalizer._json(
        '{"intent_plan":{"analysis_kind":"sample",\n    - temporal_semantics: [],\n}}'
    )
    assert parsed["intent_plan"]["temporal_semantics"] == []


def test_dummy_target_fixture_covers_explicit_pkg_plan_date():
    dummy = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "08_dummy_data_retriever.py")
    rows = dummy._rows_for_dataset("target")
    assert any(str(row.get("DATE")) == "2026-07-06" for row in rows)


def test_effective_filters_follow_the_unique_retrieval_source_alias_after_process_scope_normalization():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    condition_resolution = {
        "effective_filters": {
            # A common LLM shape: this is keyed by dataset_key, while the
            # retrieval plan gives the same dataset a contextual source alias.
            "lot_status": {
                "dataset_key": "lot_status",
                "filters": {
                    "OPER_NAME": {"operator": "eq", "value": "W/B"},
                    "HOLD_STAT": {"operator": "eq", "value": "OnHold"},
                },
            }
        }
    }
    jobs = [
        {
            "dataset_key": "lot_status",
            "source_alias": "lot_status_src",
            "filters": {
                "OPER_NAME": {
                    "operator": "in",
                    "value": ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"],
                },
                "HOLD_STAT": {"operator": "eq", "value": "OnHold"},
            },
        }
    ]

    synchronized = normalizer._synchronize_effective_filters_with_retrieval_jobs(
        condition_resolution,
        jobs,
    )

    assert list(synchronized["effective_filters"]) == ["lot_status_src"]
    assert synchronized["effective_filters"]["lot_status_src"]["filters"]["OPER_NAME"] == {
        "operator": "in",
        "value": ["W/B1", "W/B2", "W/B3", "W/B4", "W/B5", "W/B6"],
    }


def test_process_scope_is_applied_only_to_catalog_sources_that_support_its_field():
    """A process condition must not make a non-process target catalog unusable."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "PKG_OUT",
                "payload": {
                    "aliases": ["PKG OUT"],
                    "field": "OPER_NAME",
                    "processes": ["SHIP PKT"],
                },
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "target",
                "payload": {
                    "columns": ["DATE", "OUT 계획"],
                    "filter_mappings": {"DATE": ["DATE"]},
                },
            },
            {
                "dataset_key": "production_today",
                "payload": {
                    "columns": ["DATE", "OPER_NAME", "PRODUCTION"],
                    "filter_mappings": {
                        "DATE": ["DATE"],
                        "OPER_NAME": ["OPER_NAME"],
                    },
                },
            },
        ],
    }
    jobs = [
        {
            "dataset_key": "target",
            "source_alias": "target_df",
            "filters": {
                "DATE": {"operator": "eq", "value": "20260811"},
                "OPER_NAME": {"operator": "eq", "value": "SHIP PKT"},
            },
        },
        {
            "dataset_key": "production_today",
            "source_alias": "production_df",
            "filters": {
                "OPER_NAME": {"operator": "eq", "value": "SHIP PKT"},
            },
        },
    ]

    normalized, guard = normalizer._apply_process_group_filter_fields(
        jobs,
        candidates,
        "오늘 PKG OUT 목표와 실적을 정리해줘",
        declared_processes=["SHIP PKT"],
    )
    by_alias = {job["source_alias"]: job for job in normalized}

    assert by_alias["target_df"]["filters"] == {
        "DATE": {"operator": "eq", "value": "20260811"}
    }
    assert by_alias["production_df"]["filters"]["OPER_NAME"] == {
        "operator": "eq",
        "value": "SHIP PKT",
    }
    assert guard["non_applicable_filters"] == [
        {
            "source_alias": "target_df",
            "dataset_key": "target",
            "field": "OPER_NAME",
            "condition": {"operator": "eq", "value": "SHIP PKT"},
            "process_group_keys": ["PKG_OUT"],
            "reason": "process_scope_field_not_supported_by_catalog",
        }
    ]

    scope = normalizer._validate_process_scope_contract(
        normalized,
        candidates,
        "오늘 PKG OUT 목표와 실적을 정리해줘",
        declared_processes=["SHIP PKT"],
    )
    assert scope["status"] == "complete"
    assert scope["non_applicable_sources"] == ["target_df"]

    # Exercise the same two-pass process normalization used by the Flow.  This
    # protects against a later normalizer stage restoring the discarded target
    # filter after the source-specific decision above.
    integrated = normalizer.normalize_intent_plan(
        {
            "request": {
                "question": "오늘 PKG OUT 목표와 실적을 정리해줘",
                "reference_date": "20260811",
            }
        },
        {
            "intent_plan": {
                "retrieval_jobs": jobs,
                "pandas_execution_plan": [],
            }
        },
        candidates,
    )
    integrated_by_alias = {
        job["source_alias"]: job
        for job in integrated["intent_plan"]["retrieval_jobs"]
    }
    assert integrated_by_alias["target_df"]["filters"] == {
        "DATE": {"operator": "eq", "value": "20260811"}
    }
    assert integrated_by_alias["production_df"]["filters"]["OPER_NAME"] == {
        "operator": "eq",
        "value": "SHIP PKT",
    }


def test_process_scope_stays_blocked_when_no_selected_source_supports_it():
    """Dropping a recognized process condition must never silently broaden every source."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "domain_items": [
            {
                "section": "process_groups",
                "key": "PKG_OUT",
                "payload": {
                    "aliases": ["PKG OUT"],
                    "field": "OPER_NAME",
                    "processes": ["SHIP PKT"],
                },
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "target",
                "payload": {"columns": ["DATE", "OUT 계획"]},
            }
        ],
    }
    jobs, _ = normalizer._apply_process_group_filter_fields(
        [
            {
                "dataset_key": "target",
                "source_alias": "target_df",
                "filters": {"OPER_NAME": {"operator": "eq", "value": "SHIP PKT"}},
            }
        ],
        candidates,
        "PKG OUT 공정 목표를 알려줘",
        declared_processes=["SHIP PKT"],
    )

    scope = normalizer._validate_process_scope_contract(
        jobs,
        candidates,
        "PKG OUT 공정 목표를 알려줘",
        declared_processes=["SHIP PKT"],
    )

    assert scope["status"] == "error"
    assert scope["validation_errors"][0]["type"] == (
        "process_scope_not_supported_by_selected_sources"
    )


def test_inherited_filter_is_removed_only_when_the_followup_catalog_cannot_execute_it():
    """A prior-turn date must not make a non-dated target catalog unusable."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan = {
        "condition_resolution": {
            "inherited": {
                "DATE": "20260701",
                "OPER_NAME": {"operator": "in", "value": ["FCB1"]},
            }
        }
    }
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "equipment_assign_src",
            "filters": {
                "DATE": {"operator": "eq", "value": "20260701"},
                "OPER_NAME": {"operator": "in", "value": ["FCB1"]},
            },
        }
    ]
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {
                    "columns": ["EQUIP_ID", "OPER_NM"],
                    "filter_mappings": {"OPER_NAME": ["OPER_NM"]},
                },
            }
        ]
    }

    normalized, guard = normalizer._drop_unsupported_inherited_filters(
        plan,
        jobs,
        candidates,
    )

    assert normalized[0]["filters"] == {
        "OPER_NAME": {"operator": "in", "value": ["FCB1"]}
    }
    assert guard["status"] == "applied"
    assert guard["dropped_filters"] == [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "equipment_assign_src",
            "field": "DATE",
            "condition": {"operator": "eq", "value": "20260701"},
            "reason": "inherited_field_not_supported_by_target_catalog",
        }
    ]


def test_relative_current_scope_does_not_become_an_unsupported_physical_date_filter():
    """Current snapshot wording is a selection scope, not always a DATE column."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "equipment_assign_src",
            "filters": {"DATE": {"operator": "eq", "value": "20260701"}},
        }
    ]
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": ["EQUIP_ID", "OPER_NM"]},
            }
        ]
    }

    normalized, guard = normalizer._drop_unsupported_inherited_filters(
        {"condition_resolution": {"changed": {"DATE": "20260701"}}},
        jobs,
        candidates,
        "이 제품들에 할당된 현재 장비 목록을 보여줘",
    )

    assert normalized[0]["filters"] == {}
    assert guard["dropped_filters"][0]["reason"] == (
        "implicit_date_scope_not_supported_by_target_catalog"
    )

    explicit, explicit_guard = normalizer._drop_unsupported_inherited_filters(
        {"condition_resolution": {"changed": {"DATE": "20260706"}}},
        jobs,
        candidates,
        "2026-07-06 장비 목록을 보여줘",
    )
    assert "DATE" in explicit[0]["filters"]
    assert explicit_guard["status"] == "not_needed"


def test_unsupported_implicit_date_is_removed_when_only_effective_filters_contain_it():
    """The executor-facing effective-filter map must match the retrieval job."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan = {
        "condition_resolution": {
            "changed": {"DATE": "20260701"},
            "effective_filters": {
                "equipment_assign_src": {
                    "dataset_key": "equipment_assign",
                    "filters": {"DATE": {"operator": "eq", "value": "20260701"}},
                }
            },
        }
    }
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "equipment_assign_src",
            "filters": {},
        }
    ]
    candidates = {
        "table_catalog_items": [
            {"dataset_key": "equipment_assign", "payload": {"columns": ["EQUIP_ID"]}}
        ]
    }

    _, guard = normalizer._drop_unsupported_inherited_filters(
        plan,
        jobs,
        candidates,
        "이 제품들에 현재 장비 목록을 보여줘",
    )
    displayed = normalizer._strip_dropped_inherited_filter_conditions(
        plan["condition_resolution"],
        guard,
    )

    assert guard["dropped_filters"][0]["field"] == "DATE"
    assert "changed" not in displayed
    assert displayed["effective_filters"]["equipment_assign_src"]["filters"] == {}


def test_unsupported_implicit_date_is_removed_from_optional_job_parameters_too():
    """A snapshot dataset without DATE must not receive a phantom DATE parameter."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs = [
        {
            "dataset_key": "equipment_assign",
            "source_alias": "equipment_assign_src",
            "required_params": {"DATE": "20260701"},
            "filters": {},
        }
    ]
    candidates = {
        "table_catalog_items": [
            {"dataset_key": "equipment_assign", "payload": {"columns": ["EQUIP_ID"]}}
        ]
    }

    normalized, guard = normalizer._drop_unsupported_inherited_filters(
        {"condition_resolution": {"changed": {"DATE": "20260701"}}},
        jobs,
        candidates,
        "이 제품들에 현재 할당된 장비를 보여줘",
    )

    assert normalized[0]["required_params"] == {}
    assert guard["dropped_filters"][0]["field"] == "DATE"


def test_reference_join_contract_precedes_single_source_fast_recipe():
    """A follow-up enrichment must retain previous-result columns and grain."""

    executor = load_module(V2_ROOT / "17_hybrid_analysis_executor.py")
    reference_join = {
        "strict": True,
        "operation": "enrich_previous_result",
        "left_source_alias": "previous_result",
        "right_source_alias": "equipment_assign_src",
    }
    payload = {
        "intent_plan": {"resolved_reference_join_plan": reference_join},
        "simple_analysis_contract": {
            "strict": True,
            "route": "fast",
            "operation": "execute_fast_path_recipe",
            "recipe": "group_summary",
        },
    }

    assert executor._deterministic_execution_contract(payload) == reference_join


def test_followup_previous_result_pseudo_job_becomes_a_transform_not_a_catalog_lookup():
    """Reserved runtime state is never an invented Table Catalog dataset."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    plan = {
        "request_scope": "followup_transform",
        "reference_mode": "previous_result_rows",
        "retrieval_jobs": [
            {
                "dataset_key": "previous_result",
                "source_alias": "previous_result",
                "filters": {},
            }
        ],
        "pandas_execution_plan": [
            {
                "operation": "sort_and_top_n",
                "source_alias": "previous_result",
                "inputs": [{"kind": "external_source", "ref": "previous_result"}],
                "sort_by": "EQP_COUNT",
                "order": "desc",
                "limit": 1,
            }
        ],
        "output_contract": {
            "result_mode": "aggregate",
            "grain_columns": ["PRODUCT"],
            "metric_columns": ["EQP_COUNT"],
            "result_columns": ["PRODUCT", "EQP_COUNT"],
            "strict_result_columns": True,
        },
    }
    payload = {
        "request": {"question": "그중 장비 대수가 가장 많은 제품만 보여줘"},
        "followup_hint": {
            "followup_candidate": True,
            "request_scope_hint": "followup_transform",
            "reuse_strategy_hint": "previous_result",
        },
        "state": {
            "current_data": {
                "columns": ["PRODUCT", "EQP_COUNT"],
                "result_columns": ["PRODUCT", "EQP_COUNT"],
            },
            "last_intent_plan": {
                "output_contract": {"grain_columns": ["PRODUCT"]},
            },
        },
    }

    normalized = normalizer.normalize_intent_plan(
        payload,
        json.dumps({"intent_plan": plan}),
        {"domain_items": [], "table_catalog_items": [], "main_flow_filters": []},
    )
    intent = normalized["intent_plan"]

    assert intent["retrieval_jobs"] == []
    assert intent["reference_mode"] == "previous_result_transform"
    assert intent["request_scope"] == "followup_transform"
    assert normalized["trace"]["inspection"]["intent"][
        "previous_result_pseudo_job_guard"
    ]["status"] == "applied"


def test_followup_transform_with_a_new_source_becomes_previous_result_row_enrichment():
    """A new source plus prior-result intent requires row matching, not a bare transform."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    resolved = normalizer._reference_mode_resolution(
        {
            "reference_mode": "previous_result_transform",
            "retrieval_jobs": [
                {"dataset_key": "equipment_assign", "source_alias": "equipment_assign_src"}
            ],
        },
        {
            "followup_hint": {
                "followup_candidate": True,
                "reuse_strategy_hint": "previous_result",
            }
        },
        "followup_transform",
    )

    assert resolved == {
        "mode": "previous_result_rows",
        "source": "reference_mode_shape_reconciliation",
        "input": "previous_result_transform",
        "issues": [],
    }


def test_reserved_previous_result_rows_node_input_becomes_an_external_provider():
    """Typed reference modes cannot be graph node IDs."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    normalized = normalizer._normalize_reserved_previous_result_references(
        [
            {
                "operation": "join",
                "left_source_alias": "previous_result_rows",
                "right_source_alias": "equipment_assign_src",
                "inputs": [
                    {"kind": "node_output", "ref": "previous_result_rows"},
                    {"kind": "node_output", "ref": "filtered_equipment"},
                ],
            }
        ],
        "previous_result_rows",
    )

    assert normalized[0]["left_source_alias"] == "previous_result"
    assert normalized[0]["inputs"][0] == {
        "kind": "external_source",
        "ref": "previous_result",
    }


def test_v2_function_case_step_materializes_one_unambiguous_typed_output_alias():
    """A selected source transform may satisfy one missing downstream alias.

    The compiler must not invent a transform for arbitrary aliases.  This test
    covers only the unambiguous Function Case shape emitted by weak intent
    responses: one selected case, one source, and one dangling node output.
    """

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    cases = [
        {
            "key": "product_token_match",
            "function_name": "select_product",
            "input_text": "L-256K9B",
            "source_alias": "equipment_assign",
        }
    ]
    jobs = [{"dataset_key": "equipment_assign", "source_alias": "equipment_assign"}]
    plan = normalizer._ensure_function_case_steps(
        cases,
        [
            {
                "node_id": "group_by_process",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "node_output", "ref": "filtered_product"}],
                "output_alias": "process_summary",
                "group_by": ["OPER_NAME"],
                "aggregations": [
                    {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"}
                ],
            }
        ],
        jobs,
    )

    transform = plan[0]
    assert transform["operation"] == "apply_pandas_function_case"
    assert transform["output_alias"] == "filtered_product"
    assert transform["inputs"] == [
        {"kind": "external_source", "ref": "equipment_assign"}
    ]
    graph = normalizer._compile_execution_graph(plan, jobs, {}, "none")
    assert graph["validation_errors"] == []
    group = next(item for item in graph["nodes"] if item["node_id"] == "group_by_process")
    assert group["inputs"] == [{"kind": "node_output", "ref": transform["node_id"]}]


def test_v2_typed_function_case_pipeline_executes_without_pandas_model():
    """A catalog-selected transform can feed a Typed aggregate deterministically."""

    resolver, executor, _ = _modules()
    payload = {
        "question": "assigned equipment by process for one product",
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "equipment_assign", "source_alias": "equipment_assign", "filters": {}}
            ],
            "pandas_function_cases": [
                {
                    "key": "product_token_match",
                    "function_name": "select_product",
                    "input_text": "L-256K9B",
                    "source_alias": "equipment_assign",
                }
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "select_product_case",
                    "operation": "apply_pandas_function_case",
                    "function_case_key": "product_token_match",
                    "function_name": "select_product",
                    "input_text": "L-256K9B",
                    "source_alias": "equipment_assign",
                    "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
                    "output_alias": "filtered_product",
                },
                {
                    "node_id": "group_by_process",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "node_output", "ref": "filtered_product"}],
                    "output_alias": "process_summary",
                    "group_by": ["OPER_NAME"],
                    "aggregations": [
                        {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                        {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_ID_LIST"},
                    ],
                },
            ],
            "output_contract": {
                "strict_result_columns": True,
                "result_mode": "aggregate",
                "grain_columns": ["OPER_NAME"],
                "metric_columns": ["EQP_COUNT", "EQP_ID_LIST"],
                "required_columns": ["OPER_NAME", "EQP_COUNT", "EQP_ID_LIST"],
                "result_columns": ["OPER_NAME", "EQP_COUNT", "EQP_ID_LIST"],
            },
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {
                        "source_alias": "equipment_assign",
                        "dataset_key": "equipment_assign",
                        "provider": "retrieval_job",
                        "required": True,
                    }
                ],
                "validation_errors": [],
            },
            "validation_errors": [],
        },
        "runtime_sources": {
            "equipment_assign": [
                {"MCP_NO": "L-256K9B-A", "OPER_NAME": "D/A1", "EQP_ID": "D701"},
                {"MCP_NO": "L-256K9B-A", "OPER_NAME": "D/A1", "EQP_ID": "D702"},
                {"MCP_NO": "OTHER", "OPER_NAME": "D/A1", "EQP_ID": "D799"},
            ]
        },
        "source_results": [
            {
                "source_alias": "equipment_assign",
                "dataset_key": "equipment_assign",
                "status": "ok",
                "columns": ["MCP_NO", "OPER_NAME", "EQP_ID"],
            }
        ],
        "trace": {"inspection": {}},
    }

    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["operation"] == "execute_typed_pandas_plan"
    assert resolved["simple_analysis_contract"]["requires_pandas_llm"] is False
    calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "pandas prompt must not be used",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        repair_prompt_template="repair",
        function_case_helper_code=(
            "def select_product(input_text, frame):\n"
            "    return frame[frame['MCP_NO'].astype(str).str.startswith(input_text)].copy()\n"
        ),
    )

    assert calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"OPER_NAME": "D/A1", "EQP_COUNT": 2, "EQP_ID_LIST": "D701, D702"}
    ]


def test_v2_typed_join_materializes_catalog_keys_and_terminal_output_schema():
    """Typed joins retain left/right lineage and show only terminal aggregate columns."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    resolver, executor, _ = _modules()
    raw_steps = [
        {
            "node_id": "join_assign_uph",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "assign_src"},
                {"kind": "external_source", "ref": "uph_src"},
            ],
            "output_alias": "assign_uph",
            "join_type": "left",
        },
        {
            "node_id": "group_by_lead",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "node_output", "ref": "assign_uph"}],
            "output_alias": "lead_summary",
            "group_by": ["LEAD"],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQUIPMENT_COUNT"},
                {"column": "UPH", "method": "mean", "output_column": "AVG_UPH"},
            ],
        },
    ]
    materialized, trace = normalizer._materialize_resolved_join_steps(
        raw_steps,
        [
            {
                "strict": True,
                "left_source_alias": "assign_src",
                "right_source_alias": "uph_src",
                "left_keys": ["EQP_ID", "OPER_NAME"],
                "right_keys": ["EQP_ID", "OPER_NAME"],
                "right_value_columns": ["UPH"],
                "join_type": "left",
            }
        ],
    )
    assert trace["status"] == "applied"
    assert materialized[0]["left_on"] == ["EQP_ID", "OPER_NAME"]
    assert materialized[0]["right_on"] == ["EQP_ID", "OPER_NAME"]

    output_contract, output_trace = normalizer._reconcile_terminal_typed_output_contract(
        {
            "result_mode": "aggregate",
            "required_columns": ["LEAD", "EQP_MODEL", "RECIPE_ID", "OPER_NAME", "EQUIPMENT_COUNT", "AVG_UPH"],
            "result_columns": ["LEAD", "EQP_MODEL", "RECIPE_ID", "OPER_NAME", "EQUIPMENT_COUNT", "AVG_UPH"],
            "metric_columns": ["EQUIPMENT_COUNT", "AVG_UPH"],
        },
        materialized,
        {
            "validation_errors": [],
            "external_source_requirements": [
                {"source_alias": "assign_src", "provider": "retrieval_job"},
                {"source_alias": "uph_src", "provider": "retrieval_job"},
            ],
        },
    )
    assert output_trace["status"] == "applied"
    assert output_contract["result_columns"] == ["LEAD", "EQUIPMENT_COUNT", "AVG_UPH"]
    assert output_contract["execution_required_columns"] == ["EQP_MODEL", "RECIPE_ID", "OPER_NAME"]

    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "equipment_assign", "source_alias": "assign_src", "filters": {}},
                {"dataset_key": "eqp_uph", "source_alias": "uph_src", "filters": {}},
            ],
            "pandas_execution_plan": materialized,
            "output_contract": output_contract,
            "resolved_execution_graph": {
                "validation_errors": [],
                "external_source_requirements": [
                    {"source_alias": "assign_src", "dataset_key": "equipment_assign", "provider": "retrieval_job", "required": True},
                    {"source_alias": "uph_src", "dataset_key": "eqp_uph", "provider": "retrieval_job", "required": True},
                ],
            },
            "validation_errors": [],
        },
        "runtime_sources": {
            "assign_src": [
                {"EQP_ID": "D701", "OPER_NAME": "M/D", "LEAD": "200"},
                {"EQP_ID": "D702", "OPER_NAME": "M/D", "LEAD": "200"},
            ],
            "uph_src": [
                {"EQP_ID": "D701", "OPER_NAME": "M/D", "UPH": 100},
                {"EQP_ID": "D702", "OPER_NAME": "M/D", "UPH": 120},
            ],
        },
        "source_results": [
            {"source_alias": "assign_src", "dataset_key": "equipment_assign", "status": "ok", "columns": ["EQP_ID", "OPER_NAME", "LEAD"]},
            {"source_alias": "uph_src", "dataset_key": "eqp_uph", "status": "ok", "columns": ["EQP_ID", "OPER_NAME", "UPH"]},
        ],
        "trace": {"inspection": {}},
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "must not invoke pandas model",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        repair_prompt_template="repair",
    )
    assert calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"LEAD": "200", "EQUIPMENT_COUNT": 2, "AVG_UPH": 110.0}
    ]


def test_v2_matching_aggregate_grains_materialize_a_safe_typed_join_without_llm():
    """Two already-aggregated sources can join on their identical declared grain."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    resolver, executor, _ = _modules()
    raw_steps = [
        {
            "node_id": "aggregate_production",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "production_src"}],
            "output_alias": "production_by_product",
            "group_by": ["PRODUCT"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION_SUM"}
            ],
        },
        {
            "node_id": "rank_production",
            "operation": "sort_and_top_n",
            "inputs": [{"kind": "node_output", "ref": "production_by_product"}],
            "output_alias": "top_products",
            "sort_by": "PRODUCTION_SUM",
            "order": "desc",
            "limit": 3,
        },
        {
            "node_id": "aggregate_equipment",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "equipment_src"}],
            "output_alias": "equipment_by_product",
            "group_by": ["PRODUCT"],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_LIST"},
            ],
        },
        {
            "node_id": "join_product_equipment",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "top_products"},
                {"kind": "node_output", "ref": "equipment_by_product"},
            ],
            "output_alias": "final_result",
            "join_type": "left",
        },
    ]
    steps, trace = normalizer._materialize_derived_aggregate_join_keys(raw_steps)
    assert trace == {
        "status": "applied",
        "applied": [
            {
                "node_id": "join_product_equipment",
                "on": ["PRODUCT"],
                "source": "matching_aggregate_grain",
            }
        ],
    }
    assert steps[-1]["on"] == ["PRODUCT"]

    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_src", "filters": {}},
                {"dataset_key": "equipment_assign", "source_alias": "equipment_src", "filters": {}},
            ],
            "pandas_execution_plan": steps,
            "output_contract": {
                "result_mode": "entity_list",
                "grain_columns": ["PRODUCT"],
                "metric_columns": ["PRODUCTION_SUM", "EQP_COUNT"],
                "result_columns": ["PRODUCT", "PRODUCTION_SUM", "EQP_COUNT", "EQP_LIST"],
                "required_columns": ["PRODUCT", "PRODUCTION_SUM", "EQP_COUNT", "EQP_LIST"],
                "strict_result_columns": True,
            },
            "resolved_execution_graph": {
                "validation_errors": [],
                "external_source_requirements": [
                    {"source_alias": "production_src", "dataset_key": "production", "provider": "retrieval_job", "required": True},
                    {"source_alias": "equipment_src", "dataset_key": "equipment_assign", "provider": "retrieval_job", "required": True},
                ],
            },
            "validation_errors": [],
        },
        "runtime_sources": {
            "production_src": [
                {"PRODUCT": "A", "PRODUCTION": 30},
                {"PRODUCT": "B", "PRODUCTION": 20},
            ],
            "equipment_src": [
                {"PRODUCT": "A", "EQP_ID": "E1"},
                {"PRODUCT": "A", "EQP_ID": "E2"},
                {"PRODUCT": "B", "EQP_ID": "E3"},
            ],
        },
        "source_results": [
            {"source_alias": "production_src", "dataset_key": "production", "status": "ok", "columns": ["PRODUCT", "PRODUCTION"]},
            {"source_alias": "equipment_src", "dataset_key": "equipment_assign", "status": "ok", "columns": ["PRODUCT", "EQP_ID"]},
        ],
        "trace": {"inspection": {}},
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    assert resolved["simple_analysis_contract"]["operation"] == "execute_typed_pandas_plan"
    calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "Pandas model must not be called",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        repair_prompt_template="repair",
    )
    assert calls == []
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"PRODUCT": "A", "PRODUCTION_SUM": 30, "EQP_COUNT": 2, "EQP_LIST": "E1, E2"},
        {"PRODUCT": "B", "PRODUCTION_SUM": 20, "EQP_COUNT": 1, "EQP_LIST": "E3"},
    ]


def test_v2_frame_contract_rebinds_catalog_raw_values_to_aggregate_outputs():
    """Catalog raw values cannot leak through a join fed by an aggregate frame."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    raw_steps = [
        {
            "node_id": "aggregate_production",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "production_src"}],
            "output_alias": "production_by_product",
            "group_by": ["PRODUCT"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION_SUM"}
            ],
        },
        {
            "node_id": "aggregate_equipment",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "equipment_src"}],
            "output_alias": "equipment_by_product",
            "group_by": ["PRODUCT"],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQP_LIST"},
            ],
        },
        {
            "node_id": "join_product_equipment",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "production_by_product"},
                {"kind": "node_output", "ref": "equipment_by_product"},
            ],
            "output_alias": "final_result",
            "join_type": "left",
        },
    ]
    steps, _ = normalizer._materialize_derived_aggregate_join_keys(raw_steps)
    steps, materialization = normalizer._materialize_resolved_join_steps(
        steps,
        [
            {
                "strict": True,
                "left_source_alias": "production_src",
                "right_source_alias": "equipment_src",
                "left_keys": ["PRODUCT"],
                "right_keys": ["PRODUCT"],
                "right_value_columns": ["EQP_ID"],
                "join_type": "left",
            }
        ],
    )
    assert materialization["status"] == "applied"
    assert steps[-1]["right_value_columns"] == ["EQP_ID"]

    candidates = {
        "table_catalog_items": [
            {"dataset_key": "production", "payload": {"columns": ["PRODUCT", "PRODUCTION"]}},
            {"dataset_key": "equipment_assign", "payload": {"columns": ["PRODUCT", "EQP_ID"]}},
        ],
        "domain_items": [],
        "main_flow_filters": [],
    }
    jobs = [
        {"dataset_key": "production", "source_alias": "production_src"},
        {"dataset_key": "equipment_assign", "source_alias": "equipment_src"},
    ]
    compiled, trace = normalizer._compile_typed_frame_contract(steps, candidates, jobs)

    assert trace["status"] == "repaired"
    assert trace["repairs"] == [
        {
            "node_id": "join_product_equipment",
            "kind": "catalog_raw_value_to_aggregate_output",
            "from": ["EQP_ID"],
            "to": ["EQP_COUNT", "EQP_LIST"],
        }
    ]
    assert compiled[-1]["right_value_columns"] == ["EQP_COUNT", "EQP_LIST"]
    assert "_catalog_materialized_right_value_columns" not in compiled[-1]

    resolver, executor, _ = _modules()
    payload = {
        "intent_plan": {
            "retrieval_jobs": jobs,
            "pandas_execution_plan": compiled,
            "typed_frame_contract": trace,
            "output_contract": {
                "result_mode": "aggregate",
                "result_columns": ["PRODUCT", "PRODUCTION_SUM", "EQP_COUNT", "EQP_LIST"],
                "required_columns": ["PRODUCT", "PRODUCTION_SUM", "EQP_COUNT", "EQP_LIST"],
                "strict_result_columns": True,
            },
            "resolved_execution_graph": {"validation_errors": []},
            "validation_errors": [],
        },
        "runtime_sources": {
            "production_src": [
                {"PRODUCT": "A", "PRODUCTION": 30},
                {"PRODUCT": "B", "PRODUCTION": 20},
            ],
            "equipment_src": [
                {"PRODUCT": "A", "EQP_ID": "E1"},
                {"PRODUCT": "A", "EQP_ID": "E2"},
                {"PRODUCT": "B", "EQP_ID": "E3"},
            ],
        },
        "source_results": [
            {"source_alias": "production_src", "columns": ["PRODUCT", "PRODUCTION"]},
            {"source_alias": "equipment_src", "columns": ["PRODUCT", "EQP_ID"]},
        ],
        "trace": {"inspection": {}},
    }
    resolved = resolver.resolve_simple_analysis_contract(payload)
    calls: list[str] = []
    executed = executor.execute_hybrid_analysis(
        resolved,
        "Pandas model must not be called",
        model_invoker=lambda prompt: calls.append(prompt) or "{}",
        repair_prompt_template="repair",
    )

    assert calls == []
    assert resolved["simple_analysis_contract"]["operation"] == "execute_typed_pandas_plan"
    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["rows"] == [
        {"PRODUCT": "A", "PRODUCTION_SUM": 30, "EQP_COUNT": 2, "EQP_LIST": "E1, E2"},
        {"PRODUCT": "B", "PRODUCTION_SUM": 20, "EQP_COUNT": 1, "EQP_LIST": "E3"},
    ]


def test_v2_frame_contract_keeps_catalog_value_for_a_direct_right_frame():
    """The same generic check preserves a Catalog value when the input is raw."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    steps = [
        {
            "node_id": "join_raw",
            "operation": "join",
            "inputs": [
                {"kind": "external_source", "ref": "left_src"},
                {"kind": "external_source", "ref": "right_src"},
            ],
            "output_alias": "joined",
            "on": ["PRODUCT"],
            "join_type": "left",
            "right_value_columns": ["UPH"],
            "_catalog_materialized_right_value_columns": ["UPH"],
        }
    ]
    candidates = {
        "table_catalog_items": [
            {"dataset_key": "left", "payload": {"columns": ["PRODUCT", "PRODUCTION"]}},
            {"dataset_key": "right", "payload": {"columns": ["PRODUCT", "UPH"]}},
        ],
        "domain_items": [],
        "main_flow_filters": [],
    }
    jobs = [
        {"dataset_key": "left", "source_alias": "left_src"},
        {"dataset_key": "right", "source_alias": "right_src"},
    ]
    compiled, trace = normalizer._compile_typed_frame_contract(steps, candidates, jobs)

    assert trace["status"] == "verified"
    assert compiled[0]["right_value_columns"] == ["UPH"]
    assert "_catalog_materialized_right_value_columns" not in compiled[0]


def test_v2_frame_contract_accepts_trusted_hydrated_column_aliases():
    """A physical Catalog schema is compatible with its trusted canonical mapping."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    steps = [
        {
            "node_id": "aggregate_source",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "source"}],
            "output_alias": "summary",
            "group_by": ["DEN", "PKG_TYPE1"],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"}
            ],
        }
    ]
    candidates = {
        "table_catalog_items": [
            {"dataset_key": "equipment", "payload": {"columns": ["DENSITY", "PKG1", "EQUIP_ID"]}}
        ],
        "domain_items": [],
        "main_flow_filters": [],
    }
    jobs = [
        {
            "dataset_key": "equipment",
            "source_alias": "source",
            "filter_mappings": {
                "DEN": ["DENSITY"],
                "PKG_TYPE1": ["PKG1"],
                "EQP_ID": ["EQUIP_ID"],
            },
        }
    ]

    _, trace = normalizer._compile_typed_frame_contract(steps, candidates, jobs)

    assert trace["status"] == "verified"
    assert trace["issues"] == []


def test_v2_runtime_frame_contract_keeps_invalid_aggregate_join_off_typed_execution():
    """A model-authored stale raw column falls back before deterministic execution."""

    resolver, _, _ = _modules()
    payload = {
        "intent_plan": {
            "retrieval_jobs": [
                {"dataset_key": "production", "source_alias": "production_src", "filters": {}},
                {"dataset_key": "equipment_assign", "source_alias": "equipment_src", "filters": {}},
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "aggregate_production",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "external_source", "ref": "production_src"}],
                    "output_alias": "production_by_product",
                    "group_by": ["PRODUCT"],
                    "aggregations": [
                        {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION_SUM"}
                    ],
                },
                {
                    "node_id": "aggregate_equipment",
                    "operation": "groupby_and_aggregate",
                    "inputs": [{"kind": "external_source", "ref": "equipment_src"}],
                    "output_alias": "equipment_by_product",
                    "group_by": ["PRODUCT"],
                    "aggregations": [
                        {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"}
                    ],
                },
                {
                    "node_id": "join_product_equipment",
                    "operation": "join",
                    "inputs": [
                        {"kind": "node_output", "ref": "production_by_product"},
                        {"kind": "node_output", "ref": "equipment_by_product"},
                    ],
                    "output_alias": "final_result",
                    "on": ["PRODUCT"],
                    "join_type": "left",
                    "right_value_columns": ["EQP_ID"],
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "result_columns": ["PRODUCT", "PRODUCTION_SUM", "EQP_COUNT"],
                "required_columns": ["PRODUCT", "PRODUCTION_SUM", "EQP_COUNT"],
                "strict_result_columns": True,
            },
            "resolved_execution_graph": {"validation_errors": []},
            "validation_errors": [],
        },
        "runtime_sources": {
            "production_src": [{"PRODUCT": "A", "PRODUCTION": 10}],
            "equipment_src": [{"PRODUCT": "A", "EQP_ID": "E1"}],
        },
        "source_results": [
            {"source_alias": "production_src", "columns": ["PRODUCT", "PRODUCTION"]},
            {"source_alias": "equipment_src", "columns": ["PRODUCT", "EQP_ID"]},
        ],
        "trace": {"inspection": {}},
    }

    resolved = resolver.resolve_simple_analysis_contract(payload)

    assert resolved["trace"]["inspection"]["typed_frame_runtime_contract"]["status"] == "invalid"
    assert resolved["simple_analysis_contract"].get("operation") != "execute_typed_pandas_plan"


def test_v2_typed_join_prefers_catalog_proven_declared_shared_grain_over_metric_ref():
    """A metric reference cannot replace a Typed join's common source grain."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_keys = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    pandas_plan = [
        {
            "node_id": "aggregate_production",
            "operation": "groupby_and_aggregate",
            "inputs": [{"kind": "external_source", "ref": "production_src"}],
            "output_alias": "top_products",
            "group_by": product_keys,
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
            ],
        },
        {
            "node_id": "join_equipment",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "top_products"},
                {"kind": "external_source", "ref": "equipment_src"},
            ],
            "output_alias": "joined_products",
            "left_source_alias": "top_products",
            "right_source_alias": "equipment_src",
            # The Typed executor already recognizes this compact spelling as
            # a shared join key declaration.
            "group_by": product_keys,
            "join_type": "left",
        },
    ]
    candidates = {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "equipment_count",
                "payload": {"columns": ["EQP_ID"]},
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "production_today",
                "payload": {"columns": [*product_keys, "PRODUCTION"]},
            },
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": [*product_keys, "EQP_ID"]},
            },
        ],
        "main_flow_filters": [],
    }
    jobs = [
        {"dataset_key": "production_today", "source_alias": "production_src"},
        {"dataset_key": "equipment_assign", "source_alias": "equipment_src"},
    ]
    resolved = normalizer._resolve_join_plan(
        {
            "join_plan": {
                "metadata_ref": {"section": "quantity_terms", "key": "equipment_count"},
                "left_source_alias": "top_products",
                "right_source_alias": "equipment_src",
            }
        },
        [{"section": "quantity_terms", "key": "equipment_count"}],
        candidates,
        jobs,
        pandas_plan,
    )

    assert resolved[0]["key_source"] == "typed_group_by"
    assert resolved[0]["left_keys"] == product_keys
    assert resolved[0]["right_keys"] == product_keys

    materialized, trace = normalizer._materialize_resolved_join_steps(pandas_plan, resolved)

    assert trace["status"] == "applied"
    assert materialized[1]["on"] == product_keys
    assert "left_on" not in materialized[1]


def test_v2_join_metadata_resolves_catalog_ownership_through_a_derived_alias():
    """A filter/helper output may be a join input without losing its source owner."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    pandas_plan = [
        {
            "node_id": "product_filter",
            "operation": "apply_pandas_function_case",
            "inputs": [{"kind": "external_source", "ref": "equipment_assign"}],
            "source_alias": "equipment_assign",
            "output_alias": "filtered_equipment_assign",
        },
        {
            "node_id": "join_assign_uph",
            "operation": "join",
            "inputs": [
                {"kind": "node_output", "ref": "filtered_equipment_assign"},
                {"kind": "external_source", "ref": "eqp_uph"},
            ],
            "left_source_alias": "filtered_equipment_assign",
            "right_source_alias": "eqp_uph",
            "right_value_columns": ["UPH"],
            "output_alias": "joined_assign_uph",
        },
    ]
    candidates = {
        "domain_items": [
            {
                "section": "analysis_recipes",
                "key": "equipment_uph_join",
                "payload": {"join_keys": ["EQP_ID", "OPER_NAME"]},
            }
        ],
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": ["EQP_ID", "OPER_NAME", "LEAD"]},
            },
            {
                "dataset_key": "eqp_uph",
                "payload": {"columns": ["EQP_ID", "OPER_NAME", "UPH"]},
            },
        ],
        "main_flow_filters": [],
    }
    resolved = normalizer._resolve_join_plan(
        {
            "join_plan": {
                "metadata_ref": {
                    "section": "analysis_recipes",
                    "key": "equipment_uph_join",
                },
                "left_source_alias": "filtered_equipment_assign",
                "right_source_alias": "eqp_uph",
                "right_value_columns": ["UPH"],
            }
        },
        [{"section": "analysis_recipes", "key": "equipment_uph_join"}],
        candidates,
        [
            {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"},
            {"dataset_key": "eqp_uph", "source_alias": "eqp_uph"},
        ],
        pandas_plan,
    )

    assert len(resolved) == 1
    assert resolved[0]["left_source_alias"] == "filtered_equipment_assign"
    assert resolved[0]["left_dataset_key"] == "equipment_assign"
    assert resolved[0]["right_dataset_key"] == "eqp_uph"
    assert resolved[0]["left_keys"] == ["EQP_ID", "OPER_NAME"]
    assert resolved[0]["right_keys"] == ["EQP_ID", "OPER_NAME"]


def test_retrieval_gate_labels_pure_execution_plan_error_without_claiming_source_failure():
    """A broken Typed DAG is actionable before any Oracle/Mongo retrieval occurs."""

    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    result = gate.apply_retrieval_execution_gate(
        {
            "intent_plan": {"retrieval_jobs": []},
            "trace": {
                "inspection": {
                    "data_retrieval": {
                        "job_validation": {
                            "error_count": 1,
                            "errors": [
                                {
                                    "type": "unresolved_execution_input",
                                    "node_id": "group_by_process",
                                    "node_output_ref": "filtered_product",
                                }
                            ],
                        }
                    }
                }
            },
        }
    )

    assert result["analysis"]["error"]["type"] == "execution_plan_invalid"
    assert "단계 연결" in result["answer_message"]
    assert "group_by_process ← filtered_product" in result["answer_message"]


def test_required_parameter_guard_blocks_same_run_handoff_before_retrieval():
    """A blank Catalog-required parameter cannot be filled by a pandas step."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "current_entities",
                "payload": {"columns": ["ENTITY_ID", "STATUS"]},
            },
            {
                "dataset_key": "entity_history",
                "payload": {
                    "columns": ["ENTITY_ID", "EVENT_TIME"],
                    "required_params": ["ENTITY_ID"],
                    "source_config": {
                        "upstream_bindings": [
                            {
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
    guard = normalizer._validate_required_retrieval_parameters(
        [
            {"dataset_key": "current_entities", "source_alias": "current_src"},
            {
                "dataset_key": "entity_history",
                "source_alias": "history_src",
                "required_params": {"ENTITY_ID": ""},
            },
        ],
        candidates,
        [],
    )

    assert guard["status"] == "blocked"
    assert guard["validation_errors"] == [
        {
            "type": "same_run_dependent_retrieval_unsupported",
            "message": "같은 실행 안의 선행 조회 결과를 다음 조회의 필수 조건으로 사용할 수 없습니다. 후속 실행으로 분리해야 합니다.",
            "source_alias": "history_src",
            "dataset_key": "entity_history",
            "required_param": "ENTITY_ID",
            "candidate_source_aliases": ["current_src"],
        }
    ]

    gate = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "14a_retrieval_execution_gate.py")
    blocked = gate.apply_retrieval_execution_gate(
        {
            "intent_plan": {"retrieval_jobs": []},
            "trace": {
                "inspection": {
                    "data_retrieval": {
                        "job_validation": {
                            "error_count": 1,
                            "errors": guard["validation_errors"],
                        }
                    }
                }
            },
        }
    )
    assert blocked["analysis"]["error"]["type"] == "execution_plan_invalid"
    assert "Flow 01" in blocked["answer_message"]
    assert "식별자를 포함해 새 질문" in blocked["answer_message"]


def test_required_parameter_guard_allows_a_direct_catalog_identifier():
    """Direct identifier questions remain valid even when the Catalog has a binding."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "entity_history",
                "payload": {
                    "required_params": ["ENTITY_ID"],
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "source_alias": "previous_result",
                                "source_column": "ENTITY_ID",
                                "target_param": "ENTITY_ID",
                            }
                        ]
                    },
                },
            }
        ]
    }
    guard = normalizer._validate_required_retrieval_parameters(
        [
            {
                "dataset_key": "entity_history",
                "source_alias": "history_src",
                "required_params": {"ENTITY_ID": ["E-100"]},
            }
        ],
        candidates,
        [],
    )

    assert guard["status"] == "ok"
    assert guard["validation_errors"] == []


def test_normalizer_blocks_same_run_history_handoff_before_any_retrieval():
    """A two-stage model plan is blocked in Flow 01 instead of reaching pandas."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "domain_items": [],
        "table_catalog_items": [
            {
                "dataset_key": "current_entities",
                "payload": {"columns": ["ENTITY_ID", "STATUS"]},
            },
            {
                "dataset_key": "entity_history",
                "payload": {
                    "columns": ["ENTITY_ID", "EVENT_TIME", "EVENT_CODE"],
                    "required_params": ["ENTITY_ID"],
                    "source_config": {
                        "upstream_bindings": [
                            {
                                "source_alias": "previous_result",
                                "source_column": "ENTITY_ID",
                                "target_param": "ENTITY_ID",
                                "operator": "in",
                            }
                        ]
                    },
                },
            },
        ],
        "main_flow_filters": [],
    }
    response = {
        "intent_plan": {
            "analysis_kind": "current_entity_history",
            "retrieval_jobs": [
                {"dataset_key": "current_entities", "source_alias": "current_src"},
                {
                    "dataset_key": "entity_history",
                    "source_alias": "history_src",
                    "required_params": {"ENTITY_ID": ""},
                },
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "filter_current",
                    "operation": "apply_filters",
                    "inputs": [{"kind": "external_source", "ref": "current_src"}],
                    "output_alias": "filtered_current",
                },
                {
                    "node_id": "latest_history",
                    "operation": "latest_earliest",
                    "inputs": [{"kind": "external_source", "ref": "history_src"}],
                    "output_alias": "latest_history",
                },
            ],
            "output_contract": {
                "result_mode": "detail",
                "result_columns": ["ENTITY_ID", "EVENT_TIME", "EVENT_CODE"],
            },
        }
    }

    normalized = normalizer.normalize_intent_plan(
        {"request": {"question": "current entities and their latest history"}},
        json.dumps(response),
        candidates,
    )

    errors = normalized["intent_plan"].get("validation_errors", [])
    assert any(
        item.get("type") == "same_run_dependent_retrieval_unsupported"
        and item.get("source_alias") == "history_src"
        for item in errors
        if isinstance(item, dict)
    )


def test_terminal_select_projection_owns_detail_display_contract():
    """A detail projection cannot be blocked by working/default columns it removed."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    contract, trace = normalizer._reconcile_terminal_typed_output_contract(
        {
            "result_mode": "detail",
            "required_columns": ["ENTITY_ID", "STATUS", "INTERNAL_NOTE"],
            "result_columns": ["ENTITY_ID", "STATUS", "INTERNAL_NOTE"],
            "metric_columns": ["STATUS"],
        },
        [
            {
                "node_id": "filter_current",
                "operation": "apply_filters",
                "inputs": [{"kind": "external_source", "ref": "current_src"}],
                "output_alias": "filtered_current",
            },
            {
                "node_id": "select_visible",
                "operation": "select_columns",
                "inputs": [{"kind": "node_output", "ref": "filtered_current"}],
                "output_alias": "visible_current",
                "projection": ["ENTITY_ID", "STATUS"],
            },
        ],
        {
            "validation_errors": [],
            "external_source_requirements": [
                {"source_alias": "current_src", "provider": "retrieval_job"}
            ],
        },
    )

    assert trace["status"] == "applied"
    assert trace["terminal_operation"] == "select_columns"
    assert contract["result_columns"] == ["ENTITY_ID", "STATUS"]
    assert contract["required_columns"] == ["ENTITY_ID", "STATUS"]
    assert contract["execution_required_columns"] == ["INTERNAL_NOTE"]


def test_metric_binding_lineage_reassigns_unique_join_source_owner():
    """A join aggregate keeps each metric tied to its actual external source."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    candidates = {
        "table_catalog_items": [
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": ["EQP_ID", "OPER_NAME", "LEAD"]},
            },
            {
                "dataset_key": "eqp_uph",
                "payload": {"columns": ["EQP_ID", "OPER_NAME", "UPH"]},
            },
        ]
    }
    plan = {
        "pandas_execution_plan": [
            {
                "node_id": "join_assign_uph",
                "operation": "join",
                "inputs": [
                    {"kind": "external_source", "ref": "assign_src"},
                    {"kind": "external_source", "ref": "uph_src"},
                ],
                "output_alias": "assign_uph",
                "left_on": ["EQP_ID", "OPER_NAME"],
                "right_on": ["EQP_ID", "OPER_NAME"],
                "right_value_columns": ["UPH"],
            },
            {
                "node_id": "group_by_lead",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "node_output", "ref": "assign_uph"}],
                "output_alias": "lead_summary",
                "group_by": ["LEAD"],
                "aggregations": [
                    {"column": "EQP_ID", "method": "nunique", "output_column": "EQP_COUNT"},
                    {"column": "UPH", "method": "mean", "output_column": "AVG_UPH"},
                ],
            },
        ]
    }
    reconciled = normalizer._reconcile_metric_binding_source_lineage(
        [
            {
                "source_alias": "uph_src",
                "dataset_key": "eqp_uph",
                "source_column": "EQP_ID",
                "aggregation": "nunique",
                "output_column": "EQP_COUNT",
            },
            {
                "source_alias": "assign_src",
                "dataset_key": "equipment_assign",
                "source_column": "UPH",
                "aggregation": "mean",
                "output_column": "AVG_UPH",
            },
        ],
        plan,
        [
            {"dataset_key": "equipment_assign", "source_alias": "assign_src"},
            {"dataset_key": "eqp_uph", "source_alias": "uph_src"},
        ],
        candidates,
    )

    assert reconciled[0]["source_alias"] == "assign_src"
    assert reconciled[0]["dataset_key"] == "equipment_assign"
    assert reconciled[1]["source_alias"] == "uph_src"
    assert reconciled[1]["dataset_key"] == "eqp_uph"


def test_candidate_byte_fit_preserves_exact_domain_backing_catalogs_before_incidental_tables():
    builder = load_module(
        ROOT / "langflow_components" / "data_analysis_flow" / "01d_metadata_candidates_builder.py"
    )
    candidates = {
        "domain_items": [],
        "table_catalog_items": [
            {"dataset_key": "production", "payload": {"columns": ["DATE", "PRODUCTION"]}},
            {"dataset_key": "wip", "payload": {"columns": ["DATE", "WIP"]}},
            {"dataset_key": "incidental", "payload": {"description": "x" * 8000}},
        ],
        "main_flow_filters": [],
        "runtime_function_helpers": [],
    }

    fitted, trace = builder._fit_bytes(
        candidates,
        4096,
        1,
        protected_table_dataset_keys={"production", "wip"},
    )

    assert trace["truncated"] is True
    assert [item["dataset_key"] for item in fitted["table_catalog_items"]] == [
        "production",
        "wip",
    ]


def test_v2_catalog_metric_coverage_recovers_missing_source_as_aggregate_then_outer_merge():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    product_grain = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    candidates = {
        "domain_items": [
            {
                "section": "quantity_terms",
                "key": "input_quantity",
                "payload": {
                    "aliases": ["input actual"],
                    "data_source": "production",
                    "column": "PRODUCTION",
                    "aggregation_method": "sum",
                    "filters": [{"column": "OPER_NAME", "operator": "eq", "value": "INPUT"}],
                },
            },
            {
                "section": "quantity_terms",
                "key": "generic_actual",
                "payload": {
                    "aliases": ["actual"],
                    "data_source": "production",
                    "column": "PRODUCTION",
                    "aggregation_method": "sum",
                },
            },
            {
                "section": "quantity_terms",
                "key": "wip_quantity",
                "payload": {
                    "aliases": ["wip"],
                    "data_source": "wip",
                    "column": "WIP",
                    "aggregation_method": "sum",
                },
            },
        ],
        "table_catalog_items": [
            {
                "dataset_key": "production",
                "payload": {
                    "dataset_family": "production",
                    "columns": ["DATE", "OPER_NAME", *product_grain, "PRODUCTION"],
                },
            },
            {
                "dataset_key": "wip",
                "payload": {
                    "dataset_family": "wip",
                    "columns": ["DATE", "OPER_NAME", *product_grain, "WIP"],
                },
            },
        ],
    }
    refs = [
        {"section": "quantity_terms", "key": "input_quantity"},
        {"section": "quantity_terms", "key": "generic_actual"},
        {"section": "quantity_terms", "key": "wip_quantity"},
    ]
    jobs, steps, trace = normalizer._ensure_selected_metric_sources(
        {"request": {"question": "input actual versus wip"}},
        [{"dataset_key": "wip", "source_alias": "wip_source"}],
        [
            {
                "node_id": "wip_aggregate",
                "operation": "groupby_and_aggregate",
                "inputs": [{"kind": "external_source", "ref": "wip_source"}],
                "output_alias": "wip_by_product",
                "source_alias": "wip_source",
                "group_by": product_grain,
                "aggregations": [{"column": "WIP", "method": "sum", "output_column": "WIP"}],
            },
            {
                "node_id": "wip_rank",
                "operation": "sort_and_top_n",
                "inputs": [{"kind": "node_output", "ref": "wip_by_product"}],
                "output_alias": "ranked_wip",
                "sort_by": "WIP",
                "order": "desc",
            },
        ],
        candidates,
        refs,
    )

    assert trace["status"] == "applied"
    assert [job["dataset_key"] for job in jobs] == ["wip", "production"]
    assert [step["operation"] for step in steps] == [
        "groupby_and_aggregate",
        "groupby_and_aggregate",
        "join",
        "sort_and_top_n",
    ]
    assert steps[2]["join_type"] == "outer"
    assert steps[2]["on"] == product_grain
    assert steps[2]["right_value_columns"] == ["PRODUCTION"]
    assert steps[-1]["inputs"] == [{"kind": "node_output", "ref": "catalog_metric_merge_1"}]

    scoped_jobs, scope_trace = normalizer._apply_selected_domain_conditions(
        jobs,
        candidates,
        refs,
    )
    assert scope_trace["status"] == "applied"
    by_dataset = {job["dataset_key"]: job for job in scoped_jobs}
    assert by_dataset["production"]["filters"] == {
        "OPER_NAME": {"operator": "eq", "value": "INPUT"}
    }
    assert "filters" not in by_dataset["wip"]


def test_v2_new_analysis_drops_unbound_single_source_row_match_and_rewires_downstream_input():
    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    steps, trace = normalizer._normalize_row_match_steps(
        [
            {
                "node_id": "invented_match",
                "operation": "apply_row_match_groups",
                "inputs": [{"kind": "external_source", "ref": "lot_source"}],
                "source_alias": "lot_source",
                "reference_source_alias": "not_a_real_source",
                "match_columns": ["LOT_ID"],
                "output_alias": "matched_lots",
            },
            {
                "node_id": "select_lots",
                "operation": "select_columns",
                "inputs": [{"kind": "node_output", "ref": "matched_lots"}],
                "source_alias": "matched_lots",
                "output_alias": "final_lots",
            },
        ],
        [{"dataset_key": "lot_status", "source_alias": "lot_source"}],
        "none",
        {},
    )

    assert trace["status"] == "recovered"
    assert trace["dropped_steps"][0]["node_id"] == "invented_match"
    assert [step["node_id"] for step in steps] == ["select_lots"]
    assert steps[0]["inputs"] == [{"kind": "external_source", "ref": "lot_source"}]
    assert steps[0]["source_alias"] == "lot_source"


def test_v2_materializes_implicit_prior_node_inputs_for_top_product_equipment_plan():
    """A compact model DAG may use a prior node id without an output alias."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_df"},
        {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"},
    ]
    raw_steps = [
        {
            "node_id": "agg_prod",
            "operation": "groupby_and_aggregate",
            "source_alias": "prod_df",
            "group_by": ["PRODUCT"],
            "aggregations": [
                {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
            ],
        },
        {
            "node_id": "sort_top_products",
            "operation": "sort_and_top_n",
            "inputs": [{"kind": "node_output", "ref": "agg_prod"}],
            "output_alias": "top_prod_df",
            "sort_by": "PRODUCTION",
            "order": "desc",
            "limit": 3,
        },
        {
            "node_id": "join_eqp",
            "operation": "join",
            "left_source_alias": "top_prod_df",
            "right_source_alias": "equipment_assign",
            "join_type": "left",
        },
        {
            "node_id": "agg_eqp_count_and_list",
            "operation": "groupby_and_aggregate",
            "source_alias": "join_eqp",
            "group_by": ["PRODUCT", "PRODUCTION"],
            "aggregations": [
                {"column": "EQP_ID", "method": "nunique", "output_column": "EQUIPMENT_COUNT"},
                {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQUIPMENT_LIST"},
            ],
        },
    ]

    steps, trace = normalizer._materialize_implicit_step_inputs(
        raw_steps,
        jobs,
        "none",
        {},
    )

    assert trace["status"] == "applied"
    assert steps[0]["inputs"] == [{"kind": "external_source", "ref": "prod_df"}]
    assert steps[2]["inputs"] == [
        {"kind": "node_output", "ref": "sort_top_products"},
        {"kind": "external_source", "ref": "equipment_assign"},
    ]
    assert steps[3]["inputs"] == [{"kind": "node_output", "ref": "join_eqp"}]

    graph = normalizer._compile_execution_graph(steps, jobs, {}, "none")
    assert graph["validation_errors"] == []


def test_v2_implicit_node_input_keeps_equipment_metric_bound_to_real_source():
    """An implicit join alias must not become a fictitious retrieval source."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_df"},
        {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"},
    ]
    steps, _ = normalizer._materialize_implicit_step_inputs(
        [
            {
                "node_id": "agg_prod",
                "operation": "groupby_and_aggregate",
                "source_alias": "prod_df",
                "group_by": ["PRODUCT"],
                "aggregations": [
                    {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                ],
            },
            {
                "node_id": "join_eqp",
                "operation": "join",
                "left_source_alias": "agg_prod",
                "right_source_alias": "equipment_assign",
                "join_type": "left",
                "right_value_columns": ["EQP_ID"],
            },
            {
                "node_id": "agg_eqp_count",
                "operation": "groupby_and_aggregate",
                "source_alias": "join_eqp",
                "group_by": ["PRODUCT"],
                "aggregations": [
                    {"column": "EQP_ID", "method": "nunique", "output_column": "EQUIPMENT_COUNT"}
                ],
            },
        ],
        jobs,
        "none",
        {},
    )
    candidates = {
        "table_catalog_items": [
            {"dataset_key": "production_today", "payload": {"columns": ["PRODUCT", "PRODUCTION"]}},
            {"dataset_key": "equipment_assign", "payload": {"columns": ["PRODUCT", "EQP_ID"]}},
        ]
    }

    bindings = normalizer._reconcile_metric_binding_source_lineage(
        [
            {
                "source_alias": "join_eqp",
                "dataset_key": "",
                "source_column": "EQP_ID",
                "aggregation": "nunique",
                "output_column": "EQUIPMENT_COUNT",
            }
        ],
        {"pandas_execution_plan": steps},
        jobs,
        candidates,
    )

    assert bindings[0]["source_alias"] == "equipment_assign"
    assert bindings[0]["dataset_key"] == "equipment_assign"


def test_v2_implicit_dag_generates_missing_node_id_and_reuses_explicit_output_alias():
    """Weak-model omission of a node id remains executable without a guess."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    steps, trace = normalizer._materialize_implicit_step_inputs(
        [
            {
                "operation": "apply_filters",
                "source_alias": "source",
                "output_alias": "filtered_source",
            },
            {
                "node_id": "aggregate",
                "operation": "groupby_and_aggregate",
                "source_alias": "filtered_source",
                "group_by": ["PRODUCT"],
                "aggregations": [
                    {"column": "QTY", "method": "sum", "output_column": "QTY_SUM"}
                ],
            },
        ],
        [{"dataset_key": "dataset", "source_alias": "source"}],
        "none",
        {},
    )

    assert trace["status"] == "applied"
    assert steps[0]["node_id"] == "step_1_apply_filters"
    assert trace["generated_node_ids"] == [
        {"node_id": "step_1_apply_filters", "operation": "apply_filters"}
    ]
    assert steps[1]["inputs"] == [
        {"kind": "node_output", "ref": "step_1_apply_filters"}
    ]


def test_v2_join_uses_product_key_metadata_when_an_unrelated_recipe_is_also_selected():
    """One reusable product-key contract wins over an incidental recipe ref."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    grain = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    candidates = {
        "domain_items": [
            {
                "section": "product_key_columns",
                "key": "standard_product_keys",
                "payload": {"columns": grain},
            },
            {
                "section": "analysis_recipes",
                "key": "product_grain_and_join_policy",
                "payload": {"description": "aggregation recipe only"},
            },
        ],
        "table_catalog_items": [
            {"dataset_key": "production_today", "payload": {"columns": [*grain, "PRODUCTION"]}},
            {"dataset_key": "equipment_assign", "payload": {"columns": [*grain, "EQP_ID"]}},
        ],
    }
    jobs = [
        {"dataset_key": "production_today", "source_alias": "prod_df"},
        {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"},
    ]
    steps, _ = normalizer._materialize_implicit_step_inputs(
        [
            {
                "node_id": "aggregate_prod",
                "operation": "groupby_and_aggregate",
                "source_alias": "prod_df",
                "output_alias": "prod_by_product",
                "group_by": grain,
                "aggregations": [
                    {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                ],
            },
            {
                "node_id": "join_assign",
                "operation": "join",
                "left_source_alias": "prod_by_product",
                "right_source_alias": "equipment_assign",
                "join_type": "left",
                "group_by": grain,
            },
        ],
        jobs,
        "none",
        {},
    )

    resolved = normalizer._resolve_join_plan(
        {},
        [
            {"section": "product_key_columns", "key": "standard_product_keys"},
            {"section": "analysis_recipes", "key": "product_grain_and_join_policy"},
        ],
        candidates,
        jobs,
        steps,
    )
    materialized, trace = normalizer._materialize_resolved_join_steps(steps, resolved)

    assert len(resolved) == 1
    assert resolved[0]["metadata_ref"] == {
        "section": "product_key_columns",
        "key": "standard_product_keys",
    }
    assert resolved[0]["canonical_keys"] == grain
    assert trace["status"] == "applied"
    assert materialized[1]["on"] == grain


def test_v2_weak_model_top_product_equipment_plan_executes_without_pandas_generation():
    """A compact five-step multi-source plan remains deterministic end to end."""

    normalizer = load_module(V2_ROOT / "04_intent_plan_normalizer.py")
    grain = ["TECH", "DEN", "MODE", "PKG_TYPE1", "PKG_TYPE2", "LEAD", "MCP_NO"]
    candidates = {
        "domain_items": [],
        "main_flow_filters": [],
        "table_catalog_items": [
            {
                "dataset_key": "production_today",
                "payload": {"columns": ["DATE", "OPER_NAME", *grain, "PRODUCTION"]},
            },
            {
                "dataset_key": "equipment_assign",
                "payload": {"columns": ["OPER_NAME", *grain, "EQP_ID"]},
            },
        ],
    }
    response = {
        "intent_plan": {
            "analysis_kind": "top_n_production_equipment_count_list",
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "retrieval_jobs": [
                {"dataset_key": "production_today", "source_alias": "prod_df"},
                {"dataset_key": "equipment_assign", "source_alias": "equipment_assign"},
            ],
            "pandas_execution_plan": [
                {
                    "node_id": "agg_prod",
                    "operation": "groupby_and_aggregate",
                    "source_alias": "prod_df",
                    "group_by": grain,
                    "aggregations": [
                        {"column": "PRODUCTION", "method": "sum", "output_column": "PRODUCTION"}
                    ],
                },
                {
                    "node_id": "sort_top_products",
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "node_output", "ref": "agg_prod"}],
                    "output_alias": "top_prod_df",
                    "sort_by": "PRODUCTION",
                    "order": "desc",
                    "limit": 3,
                },
                {
                    "node_id": "join_eqp",
                    "operation": "join",
                    "left_source_alias": "top_prod_df",
                    "right_source_alias": "equipment_assign",
                    "join_type": "left",
                    "group_by": grain,
                },
                {
                    "node_id": "agg_eqp_count_and_list",
                    "operation": "groupby_and_aggregate",
                    "source_alias": "join_eqp",
                    "group_by": [*grain, "PRODUCTION"],
                    "aggregations": [
                        {"column": "EQP_ID", "method": "nunique", "output_column": "EQUIPMENT_COUNT"},
                        {"column": "EQP_ID", "method": "collect_unique", "output_column": "EQUIPMENT_LIST"},
                    ],
                },
                {
                    "node_id": "final_sort",
                    "operation": "sort_and_top_n",
                    "inputs": [{"kind": "node_output", "ref": "agg_eqp_count_and_list"}],
                    "sort_by": "PRODUCTION",
                    "order": "desc",
                    "limit": 3,
                },
            ],
            "output_contract": {
                "result_mode": "aggregate",
                "result_columns": [*grain, "PRODUCTION", "EQUIPMENT_COUNT", "EQUIPMENT_LIST"],
                "required_columns": [*grain, "PRODUCTION", "EQUIPMENT_COUNT", "EQUIPMENT_LIST"],
                "strict_result_columns": True,
            },
        }
    }

    payload = normalizer.normalize_intent_plan(
        {"request": {"question": "top product equipment", "reference_date": "20260811"}},
        response,
        candidates,
    )
    payload["runtime_sources"] = {
        "prod_df": [
            dict(zip(grain, ["T", "D", "M", "P1", "P2", "L", "M1"]), PRODUCTION=100),
            dict(zip(grain, ["T", "D", "M", "P1", "P2", "L", "M2"]), PRODUCTION=90),
        ],
        "equipment_assign": [
            dict(zip(grain, ["T", "D", "M", "P1", "P2", "L", "M1"]), EQP_ID="E1"),
            dict(zip(grain, ["T", "D", "M", "P1", "P2", "L", "M1"]), EQP_ID="E2"),
        ],
    }
    payload["source_results"] = [
        {"source_alias": "prod_df", "columns": [*grain, "PRODUCTION"]},
        {"source_alias": "equipment_assign", "columns": [*grain, "EQP_ID"]},
    ]
    payload.setdefault("trace", {}).setdefault("inspection", {})

    resolved, executed, model_calls = _resolve_and_execute(payload)

    assert payload["intent_plan"].get("validation_errors", []) == []
    assert resolved["simple_analysis_contract"]["operation"] == "execute_typed_pandas_plan"
    assert executed["analysis"]["status"] == "ok"
    assert model_calls == []
    assert executed["data"]["rows"] == [
        {
            "TECH": "T",
            "DEN": "D",
            "MODE": "M",
            "PKG_TYPE1": "P1",
            "PKG_TYPE2": "P2",
            "LEAD": "L",
            "MCP_NO": "M1",
            "PRODUCTION": 100,
            "EQUIPMENT_COUNT": 2,
            "EQUIPMENT_LIST": "E1, E2",
        },
        {
            "TECH": "T",
            "DEN": "D",
            "MODE": "M",
            "PKG_TYPE1": "P1",
            "PKG_TYPE2": "P2",
            "LEAD": "L",
            "MCP_NO": "M2",
            "PRODUCTION": 90,
            "EQUIPMENT_COUNT": 0,
            "EQUIPMENT_LIST": "",
        },
    ]
