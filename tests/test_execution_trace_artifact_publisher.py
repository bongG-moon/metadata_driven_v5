from __future__ import annotations

from copy import deepcopy
from html import unescape
import re

import pytest

from component_test_support import ROOT, load_module


PUBLISHER_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "25_execution_trace_artifact_publisher.py"


def _module():
    return load_module(PUBLISHER_PATH)


def _plain_html_text(value: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", value))


def _payload(*, status: str = "ok") -> dict:
    return {
        "question": "D/A공정 78LEAD 제품별 장비현황과 CAPA 알려줘",
        "request": {"session_id": "session-01", "request_id": "request-01"},
        "metadata_refs": [
            {"section": "process_groups", "key": "DA"},
            {"section": "analysis_recipes", "key": "HELD_CAPA_CALCULATION"},
            {"section": "table_catalog", "key": "equipment_assign"},
            {"section": "table_catalog", "key": "eqp_uph"},
        ],
        "intent_plan": {
            "analysis_type": "held_capacity_by_product",
            "execution_path": "Complex",
            "retrieval_jobs": [
                {
                    "dataset_key": "equipment_assign",
                    "source_alias": "assign",
                    "source_type": "oracle",
                    "required_params": {"DATE": "20260823"},
                    "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2"]}, "LEAD": {"operator": "eq", "value": 78}},
                },
                {
                    "dataset_key": "eqp_uph",
                    "source_alias": "uph",
                    "source_type": "oracle",
                    "filters": {"OPER_NAME": {"operator": "in", "value": ["D/A1", "D/A2"]}, "LEAD": {"operator": "eq", "value": 78}},
                },
            ],
            "pandas_execution_plan": [
                {"node_id": "filter_assign", "operation": "apply_filters", "source_alias": "assign", "output_alias": "filtered_assign"},
                {
                    "node_id": "aggregate_assign",
                    "operation": "groupby_and_aggregate",
                    "source_alias": "filtered_assign",
                    "output_alias": "assign_count",
                    "group_by": ["OPER_NAME", "EQP_MODEL", "RECIPE_ID"],
                    "aggregations": [{"column": "EQP_ID", "method": "nunique", "output_column": "equipment_count"}],
                },
                {
                    "node_id": "join_uph",
                    "operation": "join",
                    "left_source_alias": "assign_count",
                    "right_source_alias": "uph",
                    "join_type": "left",
                    "output_alias": "joined",
                },
                {
                    "node_id": "calc_capacity",
                    "operation": "derive_formula",
                    "source_alias": "joined",
                    "output_alias": "capacity",
                    "formula": {
                        "output_column": "holding_capacity",
                        "operator": "multiply",
                        "operands": [
                            {"column": "equipment_count"},
                            {"column": "avg_uph"},
                            {"constant": 24},
                        ],
                    },
                },
            ],
            "output_contract": {"result_mode": "aggregate"},
        },
        "source_results": [
            {
                "dataset_key": "equipment_assign",
                "source_alias": "assign",
                "source_type": "oracle",
                "status": "ok",
                "row_count": 8,
                "columns": ["OPER_NAME", "LEAD", "EQP_ID", "EQP_MODEL", "RECIPE_ID"],
            },
            {
                "dataset_key": "eqp_uph",
                "source_alias": "uph",
                "source_type": "oracle",
                "status": "ok",
                "row_count": 4,
                "columns": ["OPER_NAME", "LEAD", "EQP_MODEL", "RECIPE_ID", "UPH"],
            },
        ],
        "analysis": {"status": status, "execution_path": "Complex"},
        "data": {
            "row_count": 2,
            "columns": ["LEAD", "equipment_count", "avg_uph", "holding_capacity"],
            "rows": [{"LEAD": 78, "equipment_count": 2, "avg_uph": 120, "holding_capacity": 5760}],
        },
        "trace": {
            "trace_id": "trace-01",
            "inspection": {
                "pandas_execution": {
                    "status": "ok",
                    "generated_code": "def internal_runtime_helper():\n    return 'must-not-publish-helper'\n\nresult = sources['assign']",
                    "llm_generated_code": "api_key='do-not-publish'\nresult = sources['assign']",
                    "code_generation_type": "llm_generated",
                    "execution_mode": "llm_generated_code",
                    "llm_code_executed": True,
                    "used_helpers": ["match_product_tokens"],
                }
            },
        },
    }


def test_execution_report_html_is_user_facing_and_does_not_leak_raw_trace_or_rows():
    publisher = _module()
    payload = _payload()
    payload["question"] = "<script>alert('xss')</script> D/A공정 CAPA"
    payload["intent_plan"]["decision_reason"] = [
        "D/A 공정과 78 LEAD 조건에 맞는 장비 Assign 및 UPH 데이터를 선택했습니다."
    ]
    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)

    assert "분석 처리 과정" in document
    assert "사용한 도메인 정보" in document
    assert "적용 근거" in document
    assert "D/A 공정과 78 LEAD 조건에 맞는 장비 Assign 및 UPH 데이터를 선택했습니다." in document
    assert "데이터 조회" in document
    assert "조회 완료" in document
    assert "데이터 처리 과정" in document
    assert "조건 적용" in document
    assert "그룹별 집계" in document
    assert "데이터 결합" in document
    assert "계산식 적용" in document
    assert "equipment_assign" in document
    assert "<script>alert('xss')</script>" not in document
    assert "\\u003cscript\\u003e" in publisher._json_for_script(
        {"value": "<script>alert('xss')</script>"}
    )
    assert "do-not-publish" not in document
    assert "api_key" not in document
    assert "holding_capacity\": 5760" not in document
    assert '<details class="code-panel">' in document
    assert '<details class="code-panel" open>' not in document
    assert "LLM 생성 pandas 분석 코드" in document
    assert "result = sources['assign']" in _plain_html_text(document)
    assert "match_product_tokens" in document
    assert "must-not-publish-helper" not in document


def test_execution_report_renders_answer_intent_analysis_as_closed_html5_disclosure():
    publisher = _module()
    payload = _payload()
    payload["intent_plan"].update(
        {
            "analysis_kind": "held_capacity_by_product",
            "route_resolution": {
                "intent_candidate": "fast_candidate",
                "final_route": "complex",
                "candidate_recipe": "group_summary",
                "final_reason_codes": ["typed_plan_contract_resolved"],
            },
            "decision_reason": [
                "계획 원문보다 intent trace의 사용자 안전 근거가 우선됩니다."
            ],
        }
    )
    payload["intent_plan"]["retrieval_jobs"][0]["query"] = "select password from do_not_publish"
    payload["trace"]["inspection"]["intent"] = {
        "analysis_kind": "ignored_by_plan",
        "retrieval_job_count": 2,
        "pandas_step_count": 4,
        "decision_reason": ["D/A 공정과 78 LEAD 조건으로 CAPA를 계산하도록 분석했습니다."],
    }

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)
    intent = explanation["intent_analysis"]

    assert document.startswith("<!doctype html>")
    assert '<details class="intent-analysis-panel">' in document
    assert '<details class="intent-analysis-panel" open>' not in document
    assert "의도 분석" in document
    assert "ANALYSIS PLAN" in document
    assert 'class="intent-summary-icon"' in document
    assert 'class="intent-route-pill">Complex</span>' in document
    assert 'class="intent-summary-stat"' in document
    assert "세부 계획" in document
    assert "Fast 후보" in document
    assert "Complex" in document
    assert "의도 판단 근거" in document
    assert "조회 계획" in document
    assert "pandas 실행 계획" in document
    assert "typed_plan_contract_resolved" in document
    assert "D/A 공정과 78 LEAD 조건으로 CAPA를 계산하도록 분석했습니다." in document
    assert "select password from do_not_publish" not in document
    assert intent["analysis_type"] == "held_capacity_by_product"
    assert intent["retrieval_job_count"] == 2
    assert intent["pandas_step_count"] == 4
    assert "query" not in intent["retrieval_plan"][0]
    assert ".intent-analysis-panel::before" in document
    assert ".intent-overview-card.primary" in document


def test_execution_code_projection_preserves_multiline_body_and_escapes_html_without_helper_source():
    publisher = _module()
    payload = _payload()
    pandas_execution = payload["trace"]["inspection"]["pandas_execution"]
    pandas_execution["generated_code"] = "def trusted_helper():\n    return 'helper-body-must-not-render'"
    pandas_execution["llm_generated_code"] = (
        "df = sources['assign'].copy()\n"
        "df['LABEL'] = \"</code></pre><script>alert('xss')</script>\"\n"
        "result = df[['LEAD', 'LABEL']]"
    )

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)
    code = explanation["execution_code"]

    assert code["kind"] == "llm_pandas"
    assert code["source_field"] == "llm_generated_code"
    assert "\n" in code["code"]
    assert "helper-body-must-not-render" not in repr(explanation)
    assert "helper-body-must-not-render" not in document
    assert "<script>alert('xss')</script>" not in document
    assert "&lt;script&gt;alert('xss')&lt;/script&gt;" in document
    assert "df = sources['assign'].copy()\n" in _plain_html_text(document)


def test_python_execution_code_uses_safe_tokenized_syntax_highlighting():
    publisher = _module()
    source = (
        "def calculate(value=24):\n"
        "    # capacity calculation\n"
        "    label = '<script>alert(1)</script>'\n"
        "    return round(value * 1.5)\n"
        "result = calculate()"
    )

    highlighted = publisher._highlight_python_code_html(source)

    assert '<span class="syntax-keyword">def</span>' in highlighted
    assert '<span class="syntax-function">calculate</span>' in highlighted
    assert '<span class="syntax-number">24</span>' in highlighted
    assert '<span class="syntax-comment"># capacity calculation</span>' in highlighted
    assert '<span class="syntax-string">' in highlighted
    assert '<span class="syntax-builtin">round</span>' in highlighted
    assert "<script>alert(1)</script>" not in highlighted
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in highlighted


def test_python_highlighter_preserves_source_text_and_falls_back_for_incomplete_code():
    publisher = _module()
    source = "df = sources['assign'].copy()\nresult = df.groupby('LEAD').sum()"
    highlighted = publisher._highlight_python_code_html(source)
    restored = unescape(publisher.re.sub(r"<[^>]+>", "", highlighted))

    assert restored == source
    assert '<span class="syntax-attribute">groupby</span>' not in highlighted
    assert '<span class="syntax-function">groupby</span>' in highlighted

    incomplete = "result = '''unterminated <script>"
    fallback = publisher._highlight_python_code_html(incomplete)
    assert "syntax-" not in fallback
    assert "<script>" not in fallback
    assert "&lt;script&gt;" in fallback


def test_python_highlighter_round_trip_preserves_tabs_blank_lines_korean_and_fstring():
    publisher = _module()
    source = (
        "def greet(name):\n"
        "\t# 한글 주석\n"
        "\tmessage = f\"\"\"안녕하세요 {name}\n두 번째 줄\"\"\"\n"
        "\n"
        "\treturn message"
    )

    highlighted = publisher._highlight_python_code_html(source)
    restored = unescape(re.sub(r"<[^>]+>", "", highlighted))

    assert restored == source
    assert '<span class="syntax-comment"># 한글 주석</span>' in highlighted
    assert "syntax-string" in highlighted
    assert "\t" in restored
    assert "\n\n" in restored


def test_python_highlighter_falls_back_when_token_or_markup_limit_is_exceeded():
    publisher = _module()
    source = "+".join("x" for _ in range(3_001))

    highlighted = publisher._highlight_python_code_html(source)

    assert len(source) < publisher.MAX_EXECUTION_CODE_CHARACTERS
    assert "syntax-" not in highlighted
    assert unescape(highlighted) == source


def test_typed_deterministic_text_is_escaped_without_python_highlighting():
    publisher = _module()
    html = publisher._execution_code_html(
        {
            "available": True,
            "kind": "typed_deterministic",
            "label": "Typed deterministic 실행 계약",
            "language": "text",
            "execution_status": "executed",
            "code": "typed_plan = <unsafe>",
        }
    )

    assert "syntax-" not in html
    assert "<unsafe>" not in html
    assert "&lt;unsafe&gt;" in html


def test_execution_report_contains_dark_editor_palette_without_external_highlighter():
    publisher = _module()
    payload = _payload()
    payload["trace"]["inspection"]["pandas_execution"]["llm_generated_code"] = (
        "result = sources['assign'].groupby('LEAD').sum()"
    )

    document = publisher.render_execution_report_html(
        publisher.build_execution_explanation(payload)
    )

    assert "background: #0d1117" in document
    assert ".syntax-keyword" in document
    assert '<span class="syntax-function">groupby</span>' in document
    assert "prism.js" not in document.casefold()
    assert "highlight.js" not in document.casefold()
    assert "cdnjs" not in document.casefold()


def test_fast_and_typed_execution_are_labeled_as_deterministic_contracts():
    publisher = _module()
    fast_payload = _payload()
    fast_payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "ok",
        "code_generation_type": "deterministic_function",
        "execution_mode": "execute_fast_path_recipe",
        "deterministic_function": {"recipe": "group_summary"},
        "deterministic_logic_code": "result, certificate = _execute_fast_path_recipe(contract, sources, pd)",
        "generated_code": "must_not_win = True",
        "llm_generated_code": "",
        "llm_code_executed": False,
        "execution_started": True,
        "deterministic_contract_started": True,
    }
    typed_payload = _payload()
    typed_payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "ok",
        "code_generation_type": "deterministic_function",
        "execution_mode": "execute_typed_pandas_plan",
        "deterministic_logic_code": "result = _execute_typed_pandas_plan(typed_plan, sources, pd)",
        "generated_code": "must_not_win = True",
        "llm_generated_code": "",
        "llm_code_executed": False,
        "execution_started": True,
        "deterministic_contract_started": True,
    }

    fast = publisher.build_execution_explanation(fast_payload)["execution_code"]
    typed = publisher.build_execution_explanation(typed_payload)["execution_code"]
    fast_html = publisher.render_execution_report_html(
        publisher.build_execution_explanation(fast_payload)
    )
    typed_html = publisher.render_execution_report_html(
        publisher.build_execution_explanation(typed_payload)
    )

    assert fast["kind"] == "fast_deterministic"
    assert fast["label"] == "Fast 고정 실행 계약"
    assert "_execute_fast_path_recipe" in fast["code"]
    assert "must_not_win" not in fast["code"]
    assert "LLM 생성 코드가 아니라" in fast_html
    assert typed["kind"] == "typed_deterministic"
    assert typed["label"] == "Typed deterministic 실행 계약"
    assert "_execute_typed_pandas_plan" in typed["code"]
    assert "LLM 생성 코드가 아니라" in typed_html


def test_execution_code_redacts_credentials_and_is_bounded_before_report_post():
    publisher = _module()
    payload = _payload()
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    long_body = "\n".join(f"value_{index} = {index}" for index in range(900))
    payload["trace"]["inspection"]["pandas_execution"]["llm_generated_code"] = (
        f"api_key = '{secret}'\n"
        "headers = {'Authorization': 'Bearer abcdefghijklmnop'}\n"
        "database_url = 'postgresql://alice:Passw0rd@example.invalid/db'\n"
        "result = sources['assign'].copy()\n"
        + long_body
    )

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)
    body = publisher._report_request_body(explanation, document, 1)
    code = explanation["execution_code"]
    serialized = repr(body)

    assert code["truncated"] is True
    assert code["redacted"] is True
    assert code["shown_char_count"] <= publisher.MAX_EXECUTION_CODE_CHARACTERS
    assert "보고서 표시 한도로 이후 코드를 생략했습니다" in code["code"]
    assert "result = sources['assign'].copy()" in code["code"]
    assert secret not in repr(explanation)
    assert "abcdefghijklmnop" not in serialized
    assert "Passw0rd" not in serialized
    assert "api_key" not in code["code"]
    assert "Authorization" not in code["code"]
    assert "database_url" not in code["code"]


def test_skipped_execution_does_not_create_an_empty_code_panel():
    publisher = _module()
    payload = _payload(status="blocked")
    payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "skipped",
        "reason": "required_source_retrieval_failed",
        "generated_code": "result = must_not_render",
    }

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)

    assert explanation["execution_code"] == {}
    assert 'class="code-panel"' not in document
    assert "must_not_render" not in document


def test_partial_or_legacy_trace_without_explicit_execution_proof_does_not_publish_code():
    publisher = _module()
    partial_payload = _payload(status="partial")
    partial_payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "partial",
        "llm_generated_code": "unsafe_code = open('must-not-run')",
        "generated_code": "unsafe_code = open('must-not-run')",
        "error": {"type": "unsafe_code", "message": "guard rejected code"},
        "intermediate_results": [{"key": "source:assign", "row_count": 8}],
    }
    legacy_payload = _payload()
    legacy_payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "ok",
        "generated_code": "def trusted_helper():\n    return 'helper-body-must-not-render'",
    }

    partial_explanation = publisher.build_execution_explanation(partial_payload)
    legacy_explanation = publisher.build_execution_explanation(legacy_payload)
    partial_html = publisher.render_execution_report_html(partial_explanation)
    legacy_html = publisher.render_execution_report_html(legacy_explanation)

    assert partial_explanation["execution_code"] == {}
    assert legacy_explanation["execution_code"] == {}
    assert "must-not-run" not in partial_html
    assert "helper-body-must-not-render" not in legacy_html
    assert 'class="code-panel"' not in partial_html
    assert 'class="code-panel"' not in legacy_html


def test_partial_trace_with_explicit_execution_proof_can_show_selected_analysis_body():
    publisher = _module()
    payload = _payload(status="partial")
    payload["trace"]["inspection"]["pandas_execution"].update(
        {
            "status": "partial",
            "llm_code_executed": True,
            "llm_generated_code": "result = sources['assign'].head(1)",
        }
    )

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)

    assert explanation["execution_code"]["execution_status"] == "partial"
    assert "result = sources['assign'].head(1)" in _plain_html_text(document)
    assert "부분 실행" in document


def test_runtime_error_with_execution_proof_shows_diagnostic_code_but_pre_execution_error_does_not():
    publisher = _module()
    runtime_payload = _payload(status="error")
    runtime_payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "error",
        "execution_started": True,
        "llm_code_executed": True,
        "llm_generated_code": "result = 1 / 0",
    }
    pre_execution_payload = _payload(status="error")
    pre_execution_payload["trace"]["inspection"]["pandas_execution"] = {
        "status": "error",
        "execution_started": False,
        "llm_code_executed": False,
        "llm_generated_code": "result = open('must-not-run')",
    }

    runtime_explanation = publisher.build_execution_explanation(runtime_payload)
    runtime_html = publisher.render_execution_report_html(runtime_explanation)
    pre_execution_explanation = publisher.build_execution_explanation(pre_execution_payload)

    assert runtime_explanation["execution_code"]["execution_status"] == "error"
    assert "result = 1 / 0" in _plain_html_text(runtime_html)
    assert "실행 중 오류" in runtime_html
    assert pre_execution_explanation["execution_code"] == {}


def test_code_sanitizer_removes_nested_auth_forms_and_keeps_valid_suite_shape():
    publisher = _module()
    source = (
        "if ready:\n"
        "    api_key = 'first-secret'\n"
        "headers = dict([('Authorization', 'opaque-secret-value')])\n"
        "client = fn(auth=('alice', 'Passw0rd'))\n"
        "creds = ('bob', 'another-password')\n"
        "client_secret_value = 'opaque-secret-value-2'\n"
        "auth_tuple = ('carol', 'SecondPass!')\n"
        "result = sources['assign']"
    )

    code, truncated, redacted, _, _ = publisher._safe_execution_code_text(source)

    assert truncated is False
    assert redacted is True
    assert "first-secret" not in code
    assert "opaque-secret-value" not in code
    assert "Passw0rd" not in code
    assert "another-password" not in code
    assert "opaque-secret-value-2" not in code
    assert "SecondPass" not in code
    assert "Authorization" not in code
    assert "pass  # [민감 실행 설정 숨김]" in code
    assert "result = sources['assign']" in code
    compile(code, "<report-code>", "exec")


def test_code_sanitizer_keeps_normal_token_and_url_column_references():
    publisher = _module()
    source = (
        'result = df[df["TOKEN"] == "A"]\n'
        'result = result[["URL", "LEAD"]]'
    )

    code, truncated, redacted, _, _ = publisher._safe_execution_code_text(source)

    assert truncated is False
    assert redacted is False
    assert 'df["TOKEN"] == "A"' in code
    assert 'result[["URL", "LEAD"]]' in code


def test_code_sanitizer_redacts_sensitive_suffix_variables_and_transport_pairs():
    publisher = _module()
    source = (
        "access_token_value = 'opaque-access-token'\n"
        "session_cookie_value = 'opaque-cookie'\n"
        "connection_string_value = 'Server=db;User=x;Password=y'\n"
        "header_pairs = [('Authorization', 'opaque-header-secret')]\n"
        "config.update([('X-API-Key', 'opaque-api-secret')])\n"
        "result = sources['assign']"
    )

    code, truncated, redacted, _, _ = publisher._safe_execution_code_text(source)

    assert truncated is False
    assert redacted is True
    assert "opaque-access-token" not in code
    assert "opaque-cookie" not in code
    assert "Server=db" not in code
    assert "opaque-header-secret" not in code
    assert "opaque-api-secret" not in code
    assert "result = sources['assign']" in code
    compile(code, "<report-code>", "exec")


def test_code_sanitizer_keeps_token_url_uri_result_row_literals():
    publisher = _module()
    source = "result = pd.DataFrame([{'TOKEN': 'A', 'URL': 'factory', 'URI': 'line'}])"

    code, truncated, redacted, _, _ = publisher._safe_execution_code_text(source)

    assert truncated is False
    assert redacted is False
    assert "'TOKEN': 'A'" in code
    assert "'URL': 'factory'" in code
    assert "'URI': 'line'" in code


@pytest.mark.parametrize(
    ("source", "secret"),
    [
        ('endpoint = "mongodb://alice:Passw0rd@db.invalid/app"\nresult = 1', "Passw0rd"),
        ('endpoint = "https://alice:Passw0rd@example.invalid/path"\nresult = 1', "Passw0rd"),
        ('endpoint = "https://example.invalid/?token=abcdef123456"\nresult = 1', "abcdef123456"),
    ],
)
def test_code_sanitizer_preserves_python_quotes_when_redacting_urls(source, secret):
    publisher = _module()

    code, truncated, redacted, _, _ = publisher._safe_execution_code_text(source)

    assert truncated is False
    assert redacted is True
    assert secret not in code
    assert "result = 1" in code
    compile(code, "<report-code>", "exec")


def test_code_sanitizer_caps_scan_work_before_ast_and_marks_large_source_truncated():
    publisher = _module()
    source = "\n".join(f"value_{index} = {index}" for index in range(20_000))

    code, truncated, _, original_count, shown_count = publisher._safe_execution_code_text(source)

    assert original_count == len(source)
    assert original_count > publisher.MAX_EXECUTION_CODE_SCAN_CHARACTERS
    assert truncated is True
    assert shown_count <= publisher.MAX_EXECUTION_CODE_CHARACTERS
    assert len(code) < publisher.MAX_EXECUTION_CODE_CHARACTERS + 200


def test_execution_report_uses_local_noto_sans_kr_and_blue_card_design_tokens():
    publisher = _module()
    document = publisher.render_execution_report_html(
        publisher.build_execution_explanation(_payload())
    )

    # The report is standalone, so it prefers a locally available Korean font
    # instead of relying on a blocked external web-font request.
    assert 'font-family: "Noto Sans KR"' in document
    assert "--blue: #5878ef;" in document
    assert ".hero::before" in document
    assert ".data-workbench" in document
    assert '.domain-card summary::before { content: "";' in document
    assert '.domain-card[open] summary::before { content: "−";' not in document


def test_publisher_renders_bounded_original_intermediate_and_final_tables_then_consumes_preview():
    publisher = _module()
    payload = _payload()
    payload[publisher.EXECUTION_REPORT_DATA_PREVIEW_KEY] = {
        "original": [
            {
                "key": "source:assign",
                "title": "사용 원본 데이터: equipment_assign",
                "description": "소스 별칭: assign",
                "row_count": 14,
                "columns": ["DATE", "OPER_NAME", "LEAD", "EVENT_DESC", "API_KEY"],
                "rows": [
                    {
                        "DATE": "20260823",
                        "OPER_NAME": "D/A1",
                        "LEAD": 78,
                        "EVENT_DESC": "<script>alert('xss')</script>",
                        "API_KEY": "must-not-publish",
                    }
                ],
                "truncated": True,
                "download": {
                    "url": "https://api.example/download.csv?download_ref=ref-original",
                    "expires_at": "2026-08-23T03:00:00+00:00",
                },
            }
        ],
        "intermediate": [
            {
                "key": "filtered_assign",
                "title": "LEAD 필터 적용 후",
                "row_count": 1,
                "columns": ["OPER_NAME", "LEAD", "EQP_ID"],
                "rows": [{"OPER_NAME": "D/A1", "LEAD": 78, "EQP_ID": "D724"}],
                "download": {"url": "https://api.example/download.csv?download_ref=ref-middle"},
            }
        ],
        "final": [
            {
                "key": "final_result",
                "title": "최종 결과 데이터",
                "row_count": 1,
                "columns": ["LEAD", "holding_capacity"],
                "rows": [{"LEAD": 78, "holding_capacity": 5760}],
                "download": {"url": "https://api.example/download.csv?download_ref=ref-final"},
            }
        ],
    }
    captured: dict = {}

    def post(_: str, body: dict, __: int) -> dict:
        captured["html"] = body["html"]
        return {"report_id": "report-preview", "view_url": "https://reports.example/view"}

    result = publisher.publish_execution_trace_artifact(
        payload,
        report_api_url="https://reports.example",
        post_json_fn=post,
    )

    document = captured["html"]
    assert "원본 데이터" in document
    assert "중간 결과" in document
    assert "최종 결과" in document
    assert "사용 원본 데이터: equipment_assign" in document
    assert "LEAD 필터 적용 후" in document
    assert "전체 데이터 탐색" not in document
    assert "전체 CSV 다운로드" in document
    assert "data-execution-data-workbench" in document
    assert "data-data-filter-column" in document
    assert "data-data-sort-header" in document
    assert "data-data-page-next" in document
    assert document.index('class="intent-analysis-panel"') < document.index('id="data-confirmation-title"')
    assert document.index('id="data-confirmation-title"') < document.index('id="timeline-title"')
    assert document.count('class="timeline-dot"') == 4
    assert '<span class="timeline-dot">05</span>' not in document
    assert "https://api.example/download.json?download_ref=ref-final" in document
    assert "https://api.example/view?download_ref=ref-final" not in document
    assert "must-not-publish" not in document
    assert "API_KEY" not in document
    assert "<script>alert('xss')</script>" not in document
    assert publisher.EXECUTION_REPORT_DATA_PREVIEW_KEY not in result


def test_publisher_posts_api_server_contract_and_appends_only_safe_descriptor():
    publisher = _module()
    payload = _payload()
    captured: dict = {}

    def post(url: str, body: dict, timeout: int) -> dict:
        captured.update({"url": url, "body": body, "timeout": timeout})
        return {
            "report_id": "report-01",
            "view_url": "https://reports.example/reports/view/report-01?token=keep-this-token",
            "download_url": "https://reports.example/reports/download/report-01?token=keep-this-token",
            "expires_at": "2026-08-23T12:00:00+00:00",
            "ttl_hours": 1,
            "storage": {"backend": "mongodb_collection"},
        }

    result = publisher.publish_execution_trace_artifact(
        payload,
        report_api_url="https://reports.example",
        ttl_hours=1,
        timeout_seconds=4,
        post_json_fn=post,
    )

    assert result["analysis"]["status"] == "ok"
    assert result["data"]["rows"] == payload["data"]["rows"]
    assert captured["url"] == "https://reports.example/reports"
    assert captured["timeout"] == 4
    assert set(captured["body"]) == {
        "html",
        "title",
        "question",
        "view_request",
        "available_datasets",
        "report_plan",
        "ttl_hours",
        "filename_hint",
    }
    assert "do-not-publish" not in captured["body"]["html"]
    artifact = result["artifacts"][-1]
    assert artifact["type"] == "analysis_execution_report"
    assert artifact["artifact_type"] == "analysis_execution_html"
    assert artifact["view_url"].endswith("token=keep-this-token")
    assert result["trace"]["inspection"]["execution_trace_artifact"]["status"] == "published"


def test_blank_report_api_url_uses_public_base_url_from_langflow_environment(monkeypatch):
    publisher = _module()
    payload = _payload()
    captured: dict = {}
    monkeypatch.delenv("API_SERVER_REPORT_API_URL", raising=False)
    monkeypatch.setenv("API_SERVER_PUBLIC_BASE_URL", "https://reports.example.internal")

    def post(url: str, _: dict, __: int) -> dict:
        captured["url"] = url
        return {"report_id": "report-02", "view_url": "https://reports.example.internal/view", "ttl_hours": 1}

    result = publisher.publish_execution_trace_artifact(
        payload,
        report_api_url="",
        post_json_fn=post,
    )

    assert captured["url"] == "https://reports.example.internal/reports"
    assert result["trace"]["inspection"]["execution_trace_artifact"]["status"] == "published"


def test_explicit_report_api_url_overrides_environment_and_invalid_value_does_not_hide_error(monkeypatch):
    publisher = _module()
    payload = _payload()
    captured: dict = {}
    monkeypatch.setenv("API_SERVER_REPORT_API_URL", "https://internal.example")
    monkeypatch.setenv("API_SERVER_PUBLIC_BASE_URL", "https://public.example")

    def post(url: str, _: dict, __: int) -> dict:
        captured["url"] = url
        return {"report_id": "report-03", "view_url": "https://override.example/view", "ttl_hours": 1}

    published = publisher.publish_execution_trace_artifact(
        payload,
        report_api_url="https://override.example",
        post_json_fn=post,
    )
    invalid = publisher.publish_execution_trace_artifact(
        payload,
        report_api_url="not-a-url",
        post_json_fn=lambda *_: (_ for _ in ()).throw(AssertionError("must not publish")),
    )

    assert captured["url"] == "https://override.example/reports"
    assert published["trace"]["inspection"]["execution_trace_artifact"]["status"] == "published"
    assert invalid["trace"]["inspection"]["execution_trace_artifact"]["reason"] == "report_api_url_invalid"


def test_blank_report_api_url_keeps_local_default_when_no_environment_is_set(monkeypatch):
    publisher = _module()
    captured: dict = {}
    monkeypatch.delenv("API_SERVER_REPORT_API_URL", raising=False)
    monkeypatch.delenv("API_SERVER_PUBLIC_BASE_URL", raising=False)

    def post(url: str, _: dict, __: int) -> dict:
        captured["url"] = url
        return {"report_id": "report-04", "download_url": "https://reports.example/download", "ttl_hours": 1}

    publisher.publish_execution_trace_artifact(_payload(), report_api_url="", post_json_fn=post)

    assert captured["url"] == "http://127.0.0.1:5000/reports"


def test_report_request_body_is_accepted_by_current_api_server_schema():
    from API_SERVER.app import ReportCreateRequest

    publisher = _module()
    explanation = publisher.build_execution_explanation(_payload())
    body = publisher._report_request_body(
        explanation,
        publisher.render_execution_report_html(explanation),
        1,
    )

    request = ReportCreateRequest.model_validate(body)

    assert request.title == "분석 처리 과정"
    assert request.ttl_hours == 1
    assert request.report_plan["contract_version"] == publisher.EXPLANATION_VERSION


def test_publish_failure_preserves_analysis_result_and_stays_hidden_from_user_notices():
    publisher = _module()
    payload = _payload()
    original = deepcopy(payload)

    def failing_post(_: str, __: dict, ___: int) -> dict:
        raise RuntimeError("connection refused mongodb://secret-host")

    result = publisher.publish_execution_trace_artifact(
        payload,
        report_api_url="http://127.0.0.1:5000",
        post_json_fn=failing_post,
    )

    assert result["analysis"] == original["analysis"]
    assert result["data"] == original["data"]
    assert result.get("artifacts", []) == []
    warning = result["trace"]["warnings"][-1]
    assert warning["type"] == "execution_report_publish_failed"
    assert warning["user_visible"] is False
    assert "secret-host" not in warning.get("detail", "")
    assert result["trace"]["inspection"]["execution_trace_artifact"]["status"] == "warning"


def test_blocked_execution_still_renders_an_explanation_without_retrieval_rows():
    publisher = _module()
    payload = _payload(status="blocked")
    payload["source_results"] = []
    payload["intent_plan"]["retrieval_jobs"] = []
    payload["execution_gate"] = {
        "status": "blocked",
        "reason": "필수 입력 컬럼을 확인할 수 없어 데이터 조회를 시작하지 않았습니다.",
    }

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)

    assert explanation["summary"]["status"] == "blocked"
    assert "처리 시작 전 중단" in document
    assert "필수 입력 컬럼을 확인할 수 없어" in document
    assert "실행된 데이터 조회가 없거나" in document


def test_disabled_publisher_is_a_silent_noop_for_analysis_payload():
    publisher = _module()
    payload = _payload()
    payload[publisher.EXECUTION_REPORT_DATA_PREVIEW_KEY] = {"final": [{"rows": [{"LEAD": 78}]}]}
    result = publisher.publish_execution_trace_artifact(payload, enabled=False)

    assert result["analysis"]["status"] == "ok"
    assert "artifacts" not in result
    assert publisher.EXECUTION_REPORT_DATA_PREVIEW_KEY not in result
    inspection = result["trace"]["inspection"]["execution_trace_artifact"]
    assert inspection["status"] == "disabled"
    assert inspection["user_visible"] is False


def test_report_prefers_actual_retrieval_facts_and_captures_error_message():
    publisher = _module()
    payload = _payload()
    payload["intent_plan"]["retrieval_jobs"][0]["required_params"] = {"DATE": "planned-date"}
    payload["intent_plan"]["retrieval_jobs"][0]["filters"] = {"LEAD": {"operator": "eq", "value": 999}}
    payload["source_results"][0].update(
        {
            "status": "failed",
            "applied_params": {"DATE": "20260823"},
            "pandas_filters": {"LEAD": {"operator": "eq", "value": 78}},
            "error_message": "Oracle 연결 대기 시간이 초과되었습니다.",
        }
    )

    explanation = publisher.build_execution_explanation(payload)
    retrieval = explanation["retrievals"][0]
    document = publisher.render_execution_report_html(explanation)

    assert retrieval["required_params"] == {"DATE": "20260823"}
    assert retrieval["params_origin"] == "actual"
    assert retrieval["filters"] == {"LEAD": {"operator": "eq", "value": "78"}}
    assert retrieval["filters_origin"] == "actual"
    assert retrieval["error"] == "Oracle 연결 대기 시간이 초과되었습니다."
    assert "실제 적용 파라미터" in document
    assert "실제 적용 조건" in document
    assert "planned-date" not in document
    assert "Oracle 연결 대기 시간이 초과되었습니다." in document


def test_blocked_report_keeps_gate_failure_and_user_safe_confirmation_reason():
    publisher = _module()
    payload = _payload(status="blocked")
    payload["source_results"] = []
    payload["intent_plan"]["decision_reason"] = [
        "평균UPH가 포함된 데이터셋을 찾을 수 없어 보유CAPA를 계산할 수 없습니다."
    ]
    payload["execution_gate"] = {
        "status": "blocked",
        "critical_failures": [
            {
                "type": "metric_dataset_selection_unresolved",
                "message": "필수 입력 데이터셋을 하나로 확정하지 못했습니다.",
            }
        ],
    }
    payload["answer_sections"] = {
        "confirmation_required": {
            "title": "확인필요사항",
            "items": ["평균UPH가 포함된 데이터셋을 등록해 주세요."],
        }
    }

    explanation = publisher.build_execution_explanation(payload)
    messages = [issue["message"] for issue in explanation["issues"]]
    document = publisher.render_execution_report_html(explanation)

    assert "필수 입력 데이터셋을 하나로 확정하지 못했습니다." in messages
    assert "평균UPH가 포함된 데이터셋을 찾을 수 없어 보유CAPA를 계산할 수 없습니다." in messages
    assert "평균UPH가 포함된 데이터셋을 등록해 주세요." in messages
    assert "필수 입력 데이터셋을 하나로 확정하지 못했습니다." in document
    assert "평균UPH가 포함된 데이터셋을 등록해 주세요." in document


def test_processing_report_shows_known_row_counts_formula_and_last_normal_checkpoint():
    publisher = _module()
    payload = _payload(status="partial")
    payload["analysis"].update(
        {
            "step_outputs": [
                {"key": "filtered_assign", "row_count": 6},
                {"key": "assign_count", "row_count": 4},
                {"key": "joined", "row_count": 3},
                {"key": "capacity", "row_count": 2},
            ],
            "recovered_result": {
                "available": True,
                "checkpoint_key": "joined",
                "checkpoint_role": "step_output",
                "row_count": 3,
            },
        }
    )

    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)
    details = [step["detail"] for step in explanation["processing_steps"]]

    assert any("행 수: 8건 → 6건" in detail for detail in details)
    assert any("계산식: holding_capacity = equipment_count × avg_uph × 24" in detail for detail in details)
    assert explanation["result"]["last_successful_step"]["key"] == "joined"
    assert "마지막 정상 단계" in document
    assert "joined · 3건" in document


def test_report_redacts_sensitive_metadata_before_html_and_report_post():
    publisher = _module()
    payload = _payload()
    payload["source_results"][0].update(
        {
            "applied_params": {
                "DATE": "20260823",
                "API_KEY": "sk-supersecret",
                "Authorization": "Bearer abcdefghijkl",
                "connection_string": "https://service.example/?token=abcdef",
            },
            "error_message": "Authorization: Bearer abcdefghijkl; token=abcdef",
        }
    )
    explanation = publisher.build_execution_explanation(payload)
    document = publisher.render_execution_report_html(explanation)
    body = publisher._report_request_body(explanation, document, 1)
    serialized = repr(body)

    assert explanation["retrievals"][0]["required_params"] == {"DATE": "20260823"}
    assert "sk-supersecret" not in serialized
    assert "abcdefghijkl" not in serialized
    assert "token=abcdef" not in serialized
    assert "Authorization=[숨김]" in document


def test_report_redaction_omits_credential_urls_and_basic_auth_values():
    publisher = _module()
    safe = publisher._safe_mapping(
        {
            "DATABASE_URL": "https://alice:Passw0rd@example.invalid/service",
            "proxy_url": "https://svc-user:proxySecret@example.invalid/proxy",
            "Authorization": "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
            "X-API-Key": "sk-xapikey-secret",
            "x-auth-token": "token-value-secret",
            "HTTP_AUTHORIZATION": "Negotiate secret-token-goes-here",
            "oauth_token": "oauth-secret",
            "LEAD": 78,
        }
    )
    error = publisher._safe_error_message(
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==; "
        "Authorization: Negotiate secret-token-goes-here; "
        "target=https://alice:Passw0rd@example.invalid/service"
    )

    assert safe == {"LEAD": "78"}
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ" not in error
    assert "secret-token-goes-here" not in error
    assert "sk-xapikey-secret" not in repr(safe)
    assert "token-value-secret" not in repr(safe)
    assert "oauth-secret" not in repr(safe)
    assert "Passw0rd" not in error
    assert "alice:" not in error


def test_render_failure_is_soft_and_preserves_completed_analysis(monkeypatch):
    publisher = _module()
    payload = _payload()
    original = deepcopy(payload)

    def fail_render(_: dict) -> str:
        raise RuntimeError("render failed password=not-for-report")

    monkeypatch.setattr(publisher, "render_execution_report_html", fail_render)
    result = publisher.publish_execution_trace_artifact(payload, post_json_fn=lambda *_: {})

    assert result["analysis"] == original["analysis"]
    assert result["data"] == original["data"]
    warning = result["trace"]["warnings"][-1]
    assert warning["type"] == "execution_report_render_failed"
    assert warning["user_visible"] is False
    assert "not-for-report" not in warning.get("detail", "")


def test_selected_domain_detail_projection_is_bounded_and_redacts_execution_config():
    normalizer = load_module(
        ROOT / "langflow_components" / "data_analysis_flow_v2" / "04_intent_plan_normalizer.py"
    )
    refs = [{"section": "analysis_recipes", "key": "HELD_CAPA_CALCULATION"}]
    details = normalizer._selected_execution_report_domain_details(
        {
            "domain_items": [
                {
                    "section": "analysis_recipes",
                    "key": "HELD_CAPA_CALCULATION",
                    "payload": {
                        "display_name": "보유CAPA 계산",
                        "description": "장비 보유 대수와 평균 UPH를 사용해 보유 CAPA를 계산합니다.",
                        "formula": "equipment_count × avg_uph × 24",
                        "aliases": ["보유Capa", "보유 CAPA"],
                        "source_config": {"uri": "mongodb://must-not-report"},
                        "query_template": "select must_not_report",
                    },
                }
            ],
            "table_catalog_items": [],
            "main_flow_filters": [],
        },
        refs,
    )

    assert details[0]["title"] == "보유CAPA 계산"
    assert details[0]["details"]["formula"] == "equipment_count × avg_uph × 24"
    assert "source_config" not in details[0]["details"]
    assert "query_template" not in details[0]["details"]


def test_report_renders_clickable_domain_details_without_sensitive_configuration():
    publisher = _module()
    payload = _payload()
    report_preview = {
        "domains": [
            {
                "section": "analysis_recipes",
                "key": "HELD_CAPA_CALCULATION",
                "title": "보유CAPA 계산",
                "summary": "장비 보유 대수와 평균 UPH를 사용합니다.",
                "details": {
                    "formula": "equipment_count × avg_uph × 24",
                    "aliases": ["보유Capa", "보유 CAPA"],
                    "source_config": "must-not-publish",
                    "API_KEY": "must-not-publish",
                },
            }
        ]
    }

    explanation = publisher.build_execution_explanation(payload, report_preview)
    document = publisher.render_execution_report_html(explanation)

    assert '<details class="domain-card">' in document
    assert "세부 정보 보기" in document
    assert "equipment_count × avg_uph × 24" in document
    assert "source_config" not in document
    assert "must-not-publish" not in document
