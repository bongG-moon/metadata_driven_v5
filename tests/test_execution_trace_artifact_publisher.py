from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


PUBLISHER_PATH = ROOT / "langflow_components" / "data_analysis_flow_v2" / "25_execution_trace_artifact_publisher.py"


def _module():
    return load_module(PUBLISHER_PATH)


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
                    "generated_code": "api_key='do-not-publish'\nresult = sources['assign']",
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
