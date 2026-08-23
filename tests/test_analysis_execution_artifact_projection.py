from __future__ import annotations

from copy import deepcopy

from component_test_support import ROOT, load_module


ANSWER_ADAPTER_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow_v2"
    / "21_v2_answer_message_adapter.py"
)
API_RESPONSE_PATH = (
    ROOT
    / "langflow_components"
    / "data_analysis_flow"
    / "22_api_response_builder.py"
)


def _execution_report_artifact(**overrides: object) -> dict[str, object]:
    artifact: dict[str, object] = {
        "artifact_type": "analysis_execution_html",
        "type": "analysis_execution_report",
        "status": "ok",
        "title": "분석 처리 과정",
        "label": "분석 처리 과정 HTML",
        "mime_type": "text/html",
        "report_id": "report-20260823-01",
        "view_url": "https://reports.example.internal/reports/view/report-20260823-01?token=view-token",
        "download_url": "https://reports.example.internal/reports/download/report-20260823-01?token=download-token",
        "expires_at": "2026-08-23T13:00:00+09:00",
        "ttl_hours": 1,
        "storage_backend": "mongodb_collection",
    }
    artifact.update(overrides)
    return artifact


def test_answer_message_renders_execution_report_as_a_separate_html_section():
    adapter = load_module(ANSWER_ADAPTER_PATH)
    artifact = _execution_report_artifact()
    payload = {
        "answer_sections": {"summary": {"headline": "분석 결과입니다."}},
        "data_refs": [
            {
                "ref_id": "result:csv",
                "role": "analysis_result",
                "download_url": "https://reports.example.internal/download.csv?download_ref=result",
            }
        ],
        "artifacts": [artifact],
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "### 분석 처리 과정" in message
    assert "🧭 <a href=\"https://reports.example.internal/reports/view/report-20260823-01?token=view-token\"" in message
    assert "<strong>분석 과정 보기</strong>" in message
    assert "📥 <a href=\"https://reports.example.internal/reports/download/report-20260823-01?token=download-token\"" in message
    assert "<strong>HTML 다운로드</strong>" in message
    assert "CSV 다운로드" not in message
    assert message.index("### 분석 처리 과정") > message.index("### 답변")


def test_answer_message_omits_unavailable_or_invalid_execution_report_artifacts():
    adapter = load_module(ANSWER_ADAPTER_PATH)
    payload = {
        "answer_message": "분석 결과입니다.",
        "artifacts": [
            _execution_report_artifact(status="failed"),
            _execution_report_artifact(
                report_id="unsafe-url",
                view_url="javascript:alert(1)",
                download_url="",
            ),
            {
                "type": "unrelated_report",
                "view_url": "https://reports.example.internal/reports/view/unrelated",
            },
        ],
        # A normal data ref must never be treated as an execution report.
        "data_refs": [
            {
                "ref_id": "result:csv",
                "role": "analysis_result",
                "download_url": "https://reports.example.internal/download.csv?download_ref=result",
            }
        ],
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_download_links=False,
        show_notices=False,
        show_applied_criteria=False,
    )

    assert "### 분석 처리 과정" not in message
    assert "javascript:" not in message
    assert "CSV 다운로드" not in message


def test_answer_message_hides_internal_execution_report_publish_warning_but_keeps_normal_warning():
    adapter = load_module(ANSWER_ADAPTER_PATH)
    payload = {
        "answer_message": "분석 결과입니다.",
        "trace": {
            "warnings": [
                {
                    "type": "execution_trace_artifact_publish_failed",
                    "message": "분석 과정 HTML 발행에 실패했습니다.",
                    "user_visible": False,
                },
                {
                    "type": "source_data_delayed",
                    "message": "일부 원본 데이터가 지연되었습니다.",
                },
            ]
        },
    }

    message = adapter.build_message(
        payload,
        show_result_table=False,
        show_download_links=False,
        show_notices=True,
        show_applied_criteria=False,
    )

    assert "일부 원본 데이터가 지연되었습니다." in message
    assert "분석 과정 HTML 발행에 실패했습니다." not in message
    assert "execution_trace_artifact_publish_failed" not in message


def test_api_exposes_sanitized_execution_artifacts_without_mixing_data_refs():
    api = load_module(API_RESPONSE_PATH)
    artifact = _execution_report_artifact(
        raw_html="<html>must not be returned</html>",
        raw_trace={"secret": "must not be returned"},
        publisher_error="internal-only",
    )
    payload = {
        "analysis": {"status": "ok"},
        "data_refs": [
            {
                "ref_id": "result:csv",
                "role": "analysis_result",
                "download_url": "https://reports.example.internal/download.csv?download_ref=result",
            }
        ],
        "artifacts": [
            artifact,
            deepcopy(artifact),
            _execution_report_artifact(
                report_id="invalid-url",
                view_url="https://user:password@reports.example.internal/reports/view/invalid",
                download_url="",
            ),
        ],
    }

    response = api.build_api_response(payload)

    assert response["data_refs"] == payload["data_refs"]
    assert response["artifacts"] == [
        {
            "artifact_type": "analysis_execution_html",
            "type": "analysis_execution_report",
            "status": "ok",
            "title": "분석 처리 과정",
            "label": "분석 처리 과정 HTML",
            "mime_type": "text/html",
            "report_id": "report-20260823-01",
            "expires_at": "2026-08-23T13:00:00+09:00",
            "storage_backend": "mongodb_collection",
            "view_url": "https://reports.example.internal/reports/view/report-20260823-01?token=view-token",
            "download_url": "https://reports.example.internal/reports/download/report-20260823-01?token=download-token",
            "ttl_hours": 1,
        }
    ]
    rendered = repr(response["artifacts"])
    assert "raw_html" not in rendered
    assert "raw_trace" not in rendered
    assert "publisher_error" not in rendered


def test_api_always_returns_empty_artifacts_for_missing_or_invalid_descriptors():
    api = load_module(API_RESPONSE_PATH)

    missing = api.build_api_response({"analysis": {"status": "ok"}})
    invalid = api.build_api_response(
        {
            "analysis": {"status": "ok"},
            "artifacts": [
                {
                    "type": "analysis_execution_report",
                    "status": "ok",
                    "view_url": "javascript:alert(1)",
                }
            ],
        }
    )

    assert missing["artifacts"] == []
    assert invalid["artifacts"] == []
