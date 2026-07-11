from __future__ import annotations

from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from web_app.data_ref_store import DEFAULT_RESULT_COLLECTION
from web_app.langflow_client import (
    DEFAULT_LANGFLOW_TIMEOUT_SECONDS,
    LangflowApiClient,
    LangflowSettings,
    build_authoring_node_input_settings,
    build_data_analysis_node_input_settings,
    build_router_node_input_settings,
    build_split_flow_node_input_settings,
    normalize_authoring_response,
    normalize_duplicate_action,
    normalize_query_response,
)
from web_app.metadata_store import DEFAULT_COLLECTIONS
from web_app.session_state_store import DEFAULT_SESSION_COLLECTION
from web_app.ui_helpers import display_table_frame


ROOT = Path(__file__).resolve().parents[1]


def test_web_defaults_use_shared_v4_collections() -> None:
    assert DEFAULT_COLLECTIONS["domain"] == "agent_v4_domain_items"
    assert DEFAULT_COLLECTIONS["table_catalog"] == "agent_v4_table_catalog_items"
    assert DEFAULT_COLLECTIONS["main_flow_filter"] == "agent_v4_main_flow_filters"
    assert DEFAULT_RESULT_COLLECTION == "agent_v4_result_store"
    assert DEFAULT_SESSION_COLLECTION == "agent_v4_session_states"
    assert DEFAULT_LANGFLOW_TIMEOUT_SECONDS == 300
    assert LangflowSettings().timeout == 300


def test_langflow_settings_chat_ready_with_data_analysis_only() -> None:
    settings = LangflowSettings(data_analysis_api_url="http://127.0.0.1:7860/api/v1/run/analysis")

    configured = settings.configured_summary()

    assert configured["query"] is True
    assert configured["router"] is False
    assert configured["data_analysis"] is True


def test_data_analysis_direct_call_uses_previous_state_input() -> None:
    tweaks = build_data_analysis_node_input_settings({"current_data": {"row_count": 1}}, "web-session")

    assert tweaks == {
        "00 분석 요청 로더": {"previous_state": {"current_data": {"row_count": 1}}},
    }


def test_split_flow_tweaks_use_v5_korean_loader_names() -> None:
    state = {"current_data": {"row_count": 1}}

    metadata_tweaks = build_split_flow_node_input_settings("metadata_qa_flow", {}, state, "web-session")
    analysis_tweaks = build_split_flow_node_input_settings("data_analysis_flow", {}, state, "web-session")

    assert metadata_tweaks == {"00 메타데이터 QA 요청 로더": {"previous_state": state}}
    assert analysis_tweaks == {"00 분석 요청 로더": {"previous_state": state}}


def test_router_call_does_not_tweak_removed_custom_loader() -> None:
    tweaks = build_router_node_input_settings({"current_data": {"row_count": 1}}, "web-session")

    assert tweaks is None


def test_settings_sidebar_initializes_api_client_when_called_without_ensure_state(tmp_path) -> None:
    script = tmp_path / "settings_sidebar_smoke.py"
    script.write_text(
        "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(ROOT)!r})",
                "from web_app.app import settings_sidebar",
                "settings_sidebar()",
            ]
        ),
        encoding="utf-8",
    )

    app = AppTest.from_file(str(script), default_timeout=15)
    app.run()

    assert not app.exception


def test_authoring_api_tweaks_use_v5_korean_node_names(monkeypatch) -> None:
    monkeypatch.setenv("MONGODB_DOMAIN_COLLECTION", "agent_v4_domain_items")

    tweaks = build_authoring_node_input_settings("domain")

    assert tweaks == {
        "00 도메인 기존 항목 로더": {"collection_name": "agent_v4_domain_items"},
        "05 도메인 동일 Key 조회기": {"collection_name": "agent_v4_domain_items"},
        "07 도메인 검수/저장 처리기": {"collection_name": "agent_v4_domain_items"},
    }


def test_normalize_query_response_accepts_v5_data_analysis_payload() -> None:
    result = normalize_query_response(
        {
            "api_response": {
                "response_type": "data_analysis",
                "status": "ok",
                "message": "상위 제품은 DEV-A입니다.",
                "data": {
                    "columns": ["DEVICE", "TOTAL_PRODUCTION"],
                    "rows": [{"DEVICE": "DEV-A", "TOTAL_PRODUCTION": 120}],
                    "row_count": 1,
                },
                "state": {"current_data": {"row_count": 1}},
                "trace": {"warnings": []},
            }
        }
    )

    assert result["message_only"] is False
    assert result["answer_message"] == "상위 제품은 DEV-A입니다."
    assert result["data"]["row_count"] == 1
    assert result["data"]["rows"][0]["DEVICE"] == "DEV-A"
    assert result["response_type"] == "data_analysis"


def test_normalize_query_response_prefers_display_message_for_web() -> None:
    result = normalize_query_response(
        {
            "api_response": {
                "response_type": "data_analysis",
                "status": "ok",
                "answer_message": "상위 제품은 DEV-A입니다.",
                "display_message": "### 답변\n상위 제품은 DEV-A입니다.\n\n### 결과 테이블\n| DEVICE | 값 |\n| --- | ---: |\n| DEV-A | 12K |",
                "data": {
                    "columns": ["DEVICE", "값"],
                    "rows": [{"DEVICE": "DEV-A", "값": 12000}],
                    "row_count": 1,
                },
            }
        }
    )

    assert result["answer_message"].startswith("### 답변")
    assert result["display_message"] == result["answer_message"]
    assert result["plain_answer_message"] == "상위 제품은 DEV-A입니다."


def test_normalize_query_response_derives_pandas_developer_info_from_trace() -> None:
    result = normalize_query_response(
        {
            "api_response": {
                "response_type": "data_analysis",
                "status": "ok",
                "message": "분석 완료",
                "intent_plan": {
                    "pandas_execution_plan": [
                        {"step": "제품별 생산량 집계", "source_alias": "production_data"},
                    ]
                },
                "analysis": {
                    "status": "ok",
                    "row_count": 1,
                    "columns": ["DEVICE", "TOTAL_PRODUCTION"],
                },
                "data": {
                    "columns": ["DEVICE", "TOTAL_PRODUCTION"],
                    "rows": [{"DEVICE": "DEV-A", "TOTAL_PRODUCTION": 120}],
                    "row_count": 1,
                },
                "trace": {
                    "inspection": {
                        "intent": {"decision_reason": ["생산량 요청으로 판단했습니다."]},
                        "pandas_execution": {
                            "status": "ok",
                            "generated_code": "result = sources['production_data']",
                            "llm_generated_code": "result = sources['production_data']",
                            "pandas_filter_preamble": "production_data = production_data.copy()",
                            "pandas_filter_plan": [{"source_alias": "production_data", "conditions": []}],
                            "execution_result": {"row_count": 1, "columns": ["DEVICE", "TOTAL_PRODUCTION"]},
                        },
                    }
                },
            }
        }
    )

    developer = result["developer"]
    assert developer["analysis_plan"][0]["step"] == "제품별 생산량 집계"
    assert developer["analysis_code"] == "result = sources['production_data']"
    assert developer["data_preparation_code"] == "production_data = production_data.copy()"
    assert developer["filter_notes"][0]["source_alias"] == "production_data"
    assert developer["pandas_execution_status"]["execution_result"]["row_count"] == 1


def test_normalize_query_response_dedupes_data_refs_and_ignores_trace_ref_strings() -> None:
    analysis_ref = {
        "store": "mongodb",
        "ref_id": "result:demo-session:abc",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.result_rows",
        "role": "analysis_result",
        "label": "분석 결과 데이터",
        "row_count": 1,
    }
    source_ref = {
        "store": "mongodb",
        "ref_id": "result:demo-session:abc",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.runtime_sources.production_data",
        "role": "source_rows",
        "label": "사용 원본 데이터: production_data",
        "source_alias": "production_data",
        "row_count": 10,
    }
    source_ref_without_collection = {
        "ref_id": "result:demo-session:abc",
        "path": "payload.runtime_sources.production_data",
        "role": "source_rows",
        "label": "사용 원본 데이터: production_data",
        "source_alias": "production_data",
    }

    result = normalize_query_response(
        {
            "api_response": {
                "response_type": "data_analysis",
                "status": "ok",
                "message": "분석 완료",
                "data": {
                    "columns": ["DEVICE", "PRODUCTION"],
                    "rows": [{"DEVICE": "DEV-A", "PRODUCTION": 10}],
                    "row_count": 1,
                    "data_ref": analysis_ref,
                },
                "data_refs": [analysis_ref, source_ref],
                "answer_sections": {"downloads": [analysis_ref, source_ref_without_collection]},
                "state": {
                    "current_data": {"data_ref": analysis_ref},
                    "followup_source_results": [{"data_ref": source_ref_without_collection}],
                    "runtime_source_refs": {"production_data": source_ref},
                },
                "trace": {
                    "inspection": {
                        "result_store": {
                            "data_ref": "result:demo-session:abc",
                            "data_refs": [analysis_ref, source_ref_without_collection],
                        }
                    }
                },
            }
        }
    )

    refs = result["data_refs"]

    assert len(refs) == 2
    assert [ref["path"] for ref in refs] == ["payload.result_rows", "payload.runtime_sources.production_data"]
    assert all(ref.get("store") == "mongodb" for ref in refs)
    assert refs[1]["collection_name"] == "agent_v4_result_store"
    assert not any(ref.get("store") == "external" for ref in refs)


def test_web_intent_summary_uses_plural_pandas_function_cases() -> None:
    from web_app.app import intent_plan_summary_lines

    lines = intent_plan_summary_lines(
        {
            "analysis_kind": "product_token_analysis",
            "pandas_function_cases": [
                {
                    "key": "product_token_match",
                    "function_name": "match_product_tokens",
                    "input_text": "RG 32G DDR4 FBGA 96 DDP",
                    "source_alias": "production_data",
                }
            ],
        }
    )

    text = "\n".join(lines)

    assert "pandas 함수 케이스 `product_token_match`" in text
    assert "match_product_tokens" in text
    assert "RG 32G DDR4 FBGA 96 DDP" in text


def test_web_display_table_uses_auto_k_number_policy() -> None:
    frame = pd.DataFrame(
        [
            {"DEVICE": "DEV-A", "WIP": 9850, "PRODUCTION": 12400},
        ]
    )

    displayed = display_table_frame(frame, "auto_k")

    assert displayed.loc[0, "WIP"] == "9,850"
    assert displayed.loc[0, "PRODUCTION"] == "12.4K"


def test_web_display_table_uses_explicit_answer_section_labels_only() -> None:
    from web_app.app import result_table_display_options

    frame = pd.DataFrame(
        [
            {"DEVICE": "DEV-A", "WIP": 9850, "PRODUCTION": 12400},
        ]
    )
    result = {
        "answer_sections": {
            "result_table": {
                "column_labels": {"WIP": "재공수량", "PRODUCTION": "생산량"},
                "display_columns": ["PRODUCTION", "DEVICE", "WIP"],
            }
        }
    }

    displayed = display_table_frame(frame, "auto_k", **result_table_display_options(result))

    assert list(displayed.columns) == ["생산량", "DEVICE", "재공수량"]
    assert displayed.loc[0, "재공수량"] == "9,850"
    assert displayed.loc[0, "생산량"] == "12.4K"


def test_web_strips_markdown_result_table_when_structured_table_is_available() -> None:
    from web_app.app import strip_result_table_section

    text = (
        "### 답변\n"
        "상위 제품은 DEV-A입니다.\n\n"
        "### 결과 테이블\n"
        "| DEVICE | 값 |\n"
        "| --- | ---: |\n"
        "| DEV-A | 12K |\n\n"
        "### 참고\n"
        "- 구조화 표는 별도로 표시합니다."
    )

    stripped = strip_result_table_section(text)

    assert "### 답변" in stripped
    assert "상위 제품은 DEV-A입니다." in stripped
    assert "### 결과 테이블" not in stripped
    assert "| DEVICE | 값 |" not in stripped
    assert "### 참고" in stripped


def test_normalize_authoring_response_accepts_v5_trace_preview_items() -> None:
    result = normalize_authoring_response(
        {
            "api_response": {
                "response_type": "metadata_authoring",
                "metadata_type": "domain",
                "success": True,
                "message": "저장되었습니다.",
                "write_result": {"success": True, "saved_count": 1},
                "trace": {
                    "generated_items_preview": [{"section": "process_groups", "key": "WB"}],
                    "existing_matches": [{"section": "process_groups", "key": "W/B"}],
                    "conflict_warnings": [{"message": "유사 항목 확인 필요"}],
                },
            }
        }
    )

    assert result["items"] == [{"section": "process_groups", "key": "WB"}]
    assert result["existing_matches"][0]["key"] == "W/B"
    assert result["conflict_warnings"][0]["message"] == "유사 항목 확인 필요"
    assert result["ui_status"] == "saved"


def test_authoring_skip_and_dry_run_are_not_reported_as_saved_or_needing_choice() -> None:
    skipped = normalize_authoring_response(
        {
            "response_type": "metadata_authoring",
            "status": "skipped",
            "existing_matches": [{"new_key": "WB", "existing_key": "WB"}],
            "write_result": {"success": True, "saved_count": 0, "skipped_count": 1, "status": "skipped"},
        }
    )
    dry_run = normalize_authoring_response(
        {
            "response_type": "metadata_authoring",
            "status": "dry_run",
            "write_result": {"success": True, "dry_run": True, "saved_count": 0, "would_save_count": 1},
        }
    )

    assert skipped["ui_status"] == "skipped"
    assert dry_run["ui_status"] == "dry_run"
    assert normalize_duplicate_action(None) == "skip"
    assert normalize_duplicate_action("ask") == "skip"
    assert normalize_duplicate_action("merge") == "merge"


def test_router_authoring_envelope_normalizes_as_authoring(monkeypatch) -> None:
    from web_app import langflow_client

    def fake_call_langflow_api(*args, **kwargs):
        return {
            "api_response": {
                "response_type": "routed_flow_execution",
                "status": "ok",
                "route": "domain_saving",
                "selected_flow": "domain_saving_flow",
                "execution_mode": "api_call",
                "route_decision": {"route": "domain_saving", "confidence": "high"},
                "raw_response": {
                    "api_response": {
                        "response_type": "metadata_authoring",
                        "metadata_type": "domain",
                        "success": True,
                        "message": "저장되었습니다.",
                        "write_result": {"success": True, "saved_count": 1},
                        "trace": {"generated_items_preview": [{"section": "process_groups", "key": "DA"}]},
                    }
                },
                "message": "저장되었습니다.",
            }
        }

    monkeypatch.setattr(langflow_client, "call_langflow_api", fake_call_langflow_api)
    client = LangflowApiClient(LangflowSettings(router_api_url="http://127.0.0.1:7860/api/v1/run/router"))

    result = client.run_router_query("DA 공정 그룹 등록", "web-session")

    assert result["response_type"] == "metadata_authoring"
    assert result["metadata_type"] == "domain"
    assert result["selected_flow"] == "domain_saving_flow"
    assert result["route_decision"]["route"] == "domain_saving"
    assert result["items"] == [{"section": "process_groups", "key": "DA"}]


def test_router_clarification_envelope_does_not_default_to_data_analysis(monkeypatch) -> None:
    from web_app import langflow_client

    def fake_call_langflow_api(*args, **kwargs):
        return {
            "api_response": {
                "response_type": "routed_flow_execution",
                "status": "needs_more_input",
                "route": "clarification",
                "selected_flow": "",
                "execution_mode": "direct",
                "route_decision": {
                    "route": "clarification",
                    "confidence": "high",
                    "needs_clarification": True,
                    "clarification_question": "어떤 요청인지 알려주세요.",
                },
                "raw_response": {
                    "api_response": {
                        "response_type": "clarification",
                        "status": "needs_more_input",
                        "message": "어떤 요청인지 알려주세요.",
                    }
                },
                "message": "어떤 요청인지 알려주세요.",
            }
        }

    monkeypatch.setattr(langflow_client, "call_langflow_api", fake_call_langflow_api)
    client = LangflowApiClient(LangflowSettings(router_api_url="http://127.0.0.1:7860/api/v1/run/router"))

    result = client.run_router_query("음", "web-session")

    assert result["route_decision"]["route"] == "clarification"
    assert result["selected_flow"] == ""
    assert result["response_type"] == "clarification"
    assert result["status"] == "needs_more_input"
    assert result["direct_response_ready"] is True
    assert result["message"] == "어떤 요청인지 알려주세요."


def test_router_flow_error_envelope_preserves_error_status(monkeypatch) -> None:
    from web_app import langflow_client

    def fake_call_langflow_api(*args, **kwargs):
        return {
            "api_response": {
                "response_type": "routed_flow_execution",
                "status": "error",
                "route": "flow_error",
                "selected_flow": "",
                "execution_mode": "api_call",
                "route_decision": {"route": "flow_error", "confidence": "high"},
                "raw_response": {
                    "api_response": {
                        "response_type": "flow_error",
                        "status": "error",
                        "message": "선택 flow 실행 실패",
                        "errors": [{"type": "empty_child_flow_response", "message": "비어 있음"}],
                    }
                },
                "message": "선택 flow 실행 실패",
            }
        }

    monkeypatch.setattr(langflow_client, "call_langflow_api", fake_call_langflow_api)
    client = LangflowApiClient(LangflowSettings(router_api_url="http://127.0.0.1:7860/api/v1/run/router"))

    result = client.run_router_query("음", "web-session")

    assert result["route_decision"]["route"] == "flow_error"
    assert result["response_type"] == "flow_error"
    assert result["status"] == "error"
    assert result["errors"][0]["type"] == "empty_child_flow_response"


def test_router_selected_flow_error_preserves_error_status_and_state(monkeypatch) -> None:
    from web_app import langflow_client

    def fake_call_langflow_api(*args, **kwargs):
        return {
            "api_response": {
                "response_type": "routed_flow_execution",
                "status": "error",
                "route": "data_analysis",
                "selected_flow": "data_analysis_flow",
                "execution_mode": "api_call",
                "route_decision": {"route": "data_analysis", "confidence": "high"},
                "raw_response": {},
                "selected_flow_response": {},
                "message": "하위 Flow API 응답이 비어 있습니다.",
                "trace": {
                    "execution": {
                        "errors": [
                            {
                                "type": "empty_child_flow_response",
                                "message": "하위 Flow API 응답이 비어 있습니다.",
                            }
                        ]
                    }
                },
            }
        }

    monkeypatch.setattr(langflow_client, "call_langflow_api", fake_call_langflow_api)
    client = LangflowApiClient(LangflowSettings(router_api_url="http://127.0.0.1:7860/api/v1/run/router"))

    result = client.run_router_query("오늘 DA공정 생산량", "web-session", {"current_data": {"row_count": 1}})

    assert result["route_decision"]["route"] == "data_analysis"
    assert result["selected_flow"] == "data_analysis_flow"
    assert result["status"] == "error"
    assert result["response_type"] == "routed_flow_execution"
    assert result["state"] == {"current_data": {"row_count": 1}}
    assert result["errors"][0]["type"] == "empty_child_flow_response"
