# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 25 분석 처리 과정 HTML 발행기
# 역할: 최종 분석 payload에서 사용자용 실행 설명을 만들고 API_SERVER에 HTML Report로 발행합니다.
# 주요 입력: 정리된 페이로드, 발행 사용 여부, Report API 주소, 링크 유효 시간, 요청 제한 시간
# 주요 출력: HTML artifact descriptor가 추가된 동일 payload
# 처리 흐름: 허용된 실행 사실만 추출 -> 정적 HTML 렌더링 -> API_SERVER POST /reports -> 링크 descriptor 추가
# 유지보수 포인트: 발행 실패는 분석·결과·세션 상태를 절대 실패로 바꾸지 않는 best-effort 단계입니다.
# =============================================================================

from __future__ import annotations

import ast
import builtins
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html import escape
from io import StringIO
import json
import keyword
import os
import re
import tokenize
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, IntInput, MessageTextInput, Output
from lfx.schema.data import Data


EXPLANATION_VERSION = "analysis.execution.explanation.v4"
ARTIFACT_TYPE = "analysis_execution_html"
DESCRIPTOR_TYPE = "analysis_execution_report"
DEFAULT_REPORT_API_URL = "http://127.0.0.1:5000"
# The publisher runs inside Langflow while API_SERVER runs as a separate
# process.  Keep an explicit node input as the highest-priority override, but
# allow an imported Flow to follow the deployment's shared base URL when that
# input is intentionally left blank.  The internal endpoint may differ from
# the browser-facing public URL behind a reverse proxy.
REPORT_API_ENV_NAMES = (
    "API_SERVER_REPORT_API_URL",
    "API_SERVER_PUBLIC_BASE_URL",
)
DEFAULT_TTL_HOURS = 1
MAX_TTL_HOURS = 24 * 7
# This sidecar runs synchronously only so the current answer can contain its
# view/download links.  Keep the default deliberately short: an unavailable
# report service must not make a normal analysis response feel stalled.
DEFAULT_TIMEOUT_SECONDS = 2
MAX_TIMEOUT_SECONDS = 30
MAX_HTML_BYTES = 1_000_000
MAX_TEXT_LENGTH = 500
MAX_COLUMNS_PER_SOURCE = 32
MAX_RETRIEVALS = 12
MAX_STEPS = 16
MAX_ISSUES = 8
MAX_DOMAINS = 24
# Pandas execution code is useful for diagnosis, but a report is not a raw
# trace dump.  Keep the human-facing analysis body/contract bounded even when
# a model or helper produced a very large program.
MAX_EXECUTION_CODE_CHARACTERS = 32_000
MAX_EXECUTION_CODE_LINES = 600
MAX_EXECUTION_CODE_HELPERS = 16
MAX_EXECUTION_CODE_SCAN_CHARACTERS = 128_000
MAX_EXECUTION_CODE_SCAN_LINES = 2_000
MAX_HIGHLIGHTED_CODE_TOKENS = 5_000
MAX_HIGHLIGHTED_CODE_HTML_CHARACTERS = 240_000
PYTHON_BUILTIN_NAMES = frozenset(dir(builtins))
# Node 24 creates this short-lived sidecar before releasing large runtime row
# buffers.  Node 25 must consume and remove it before any answer/API/session
# projection so row previews never enlarge normal analysis payloads.
EXECUTION_REPORT_DATA_PREVIEW_KEY = "_execution_report_data_preview"
MAX_PREVIEW_SOURCE_TABLES = 8
MAX_PREVIEW_INTERMEDIATE_TABLES = 4
MAX_PREVIEW_FINAL_TABLES = 1
MAX_PREVIEW_COLUMNS = 20
MAX_PREVIEW_SOURCE_ROWS = 10
MAX_PREVIEW_INTERMEDIATE_ROWS = 10
MAX_PREVIEW_FINAL_ROWS = 30
MAX_PREVIEW_CELL_CHARACTERS = 160
MAX_PREVIEW_CELLS = 3_000
KST = timezone(timedelta(hours=9), "KST")

# The report is persisted by API_SERVER.  Treat anything that looks like a
# credential, connection detail, or executable query as non-reportable at the
# boundary rather than relying on every individual caller to omit it.
SENSITIVE_MAPPING_KEY_COMPACT = {
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "credential",
    "credentials",
    "connectionstring",
    "connection",
    "dsn",
    "uri",
    "url",
    "databaseurl",
    "proxyurl",
    "serviceurl",
    "sourceurl",
    "query",
    "querytemplate",
    "sql",
}
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|cookie|password|passwd|secret|"
    r"(?:access|refresh)[_ -]?token|token|credential(?:s)?|"
    r"connection(?:[_ -]?string)?|dsn)\b\s*[:=]\s*[^,;]+"
)
SENSITIVE_QUERY_VALUE_PATTERN = re.compile(
    r"(?i)([?&](?:api[_-]?key|authorization|cookie|password|passwd|secret|"
    r"(?:access|refresh)[_-]?token|token|credential(?:s)?|"
    r'''connection(?:[_-]?string)?|dsn)=)[^&#\s"']+'''
)
SENSITIVE_AUTH_SCHEME_PATTERN = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}")
CONNECTION_URI_PATTERN = re.compile(
    r'''(?i)(?:mongodb(?:\+srv)?|postgres(?:ql)?|oracle|mysql|mariadb|mssql|redis)://[^\s"'<>]+'''
)
CREDENTIAL_URL_PATTERN = re.compile(
    r'''(?i)(?:[a-z][a-z0-9+.-]*://)[^/\s:@"']+:[^@/\s"']+@[^\s"'<>]+'''
)
SENSITIVE_CODE_VALUE_PATTERN = re.compile(
    r'''(?ix)
    (?P<prefix>
        ["']?
        (?:
            api[_ -]?key|authorization|cookie|password|passwd|secret|
            (?:access|refresh|oauth)[_ -]?token|token|credential(?:s)?|
            connection(?:[_ -]?string)?|dsn|database[_ -]?url|proxy[_ -]?url|
            query(?:[_ -]?template)?|sql(?:[_ -]?template)?|source[_ -]?config|uri|url
        )
        ["']?\s*[:=]\s*
    )
    (?P<value>[^,;\}\]]+)
    '''
)
PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----"
)
HIGH_CONFIDENCE_SECRET_PATTERN = re.compile(
    r"(?i)(?:\bsk-[a-z0-9_-]{16,}\b|\bAKIA[A-Z0-9]{16}\b|\beyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\b)"
)

SECTION_LABELS = {
    "process_groups": "공정 그룹",
    "analysis_recipes": "분석 규칙",
    "quantity_terms": "수량 지표",
    "metric_terms": "지표 정의",
    "status_terms": "상태 기준",
    "product_key_columns": "제품 기준",
    "pandas_function_cases": "처리 규칙",
    "table_catalog": "데이터셋",
}
OPERATION_LABELS = {
    "apply_filters": "조건 적용",
    "apply_pandas_function_case": "특화 조건 처리",
    "groupby_and_aggregate": "그룹별 집계",
    "join": "데이터 결합",
    "derive_formula": "계산식 적용",
    "select_columns": "결과 컬럼 선택",
    "sort_and_top_n": "정렬 및 상위 결과 선택",
    "count_rows": "건수 계산",
    "pivot": "피벗 변환",
}


# 주요 함수: 사용자용 실행 설명 HTML을 API_SERVER에 발행하고 artifact descriptor를 payload에 붙입니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 구현합니다.
def publish_execution_trace_artifact(
    payload_value: Any,
    enabled: Any = True,
    report_api_url: Any = "",
    ttl_hours: Any = DEFAULT_TTL_HOURS,
    timeout_seconds: Any = DEFAULT_TIMEOUT_SECONDS,
    post_json_fn: Callable[[str, dict[str, Any], int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish a user-facing, non-sensitive analysis execution report.

    This function is intentionally a sidecar: every failure is recorded as an
    internal, non-user-visible warning and returns the original analysis result.
    """

    payload = _payload_copy(payload_value)
    if not payload:
        return {}

    # The cleanup node deliberately keeps this bounded projection only for the
    # immediately following HTML publisher.  Remove it before every return
    # path, including a disabled publisher or a publish failure, so it can
    # never be forwarded to the chat/API/session branches.
    report_data_preview = _dict(payload.pop(EXECUTION_REPORT_DATA_PREVIEW_KEY, {}))

    if not _truthy(enabled):
        return _record_artifact_status(payload, "disabled", reason="publisher_disabled")

    post_url = _reports_post_url(_resolve_report_api_url(report_api_url))
    if not post_url:
        return _record_artifact_failure(
            payload,
            "report_api_url_invalid",
            "분석 과정 Report API 주소가 올바르지 않아 HTML 링크를 만들지 않았습니다.",
        )

    # HTML preparation is part of the same best-effort boundary as the network
    # publish.  A malformed trace must never turn a successfully completed
    # analysis into an error merely because its explanatory artifact cannot be
    # produced.
    try:
        explanation = build_execution_explanation(payload, report_data_preview)
        html_document = render_execution_report_html(explanation)
        html_size = len(html_document.encode("utf-8"))
        if html_size > MAX_HTML_BYTES:
            return _record_artifact_failure(
                payload,
                "execution_report_html_too_large",
                "분석 과정 HTML 크기가 허용 상한을 초과해 링크를 만들지 않았습니다.",
            )

        selected_ttl = _bounded_int(ttl_hours, DEFAULT_TTL_HOURS, 1, MAX_TTL_HOURS)
        selected_timeout = _bounded_int(
            timeout_seconds,
            DEFAULT_TIMEOUT_SECONDS,
            1,
            MAX_TIMEOUT_SECONDS,
        )
        request_body = _report_request_body(explanation, html_document, selected_ttl)
    except Exception as exc:  # noqa: BLE001 - intentional sidecar boundary.
        return _record_artifact_failure(
            payload,
            "execution_report_render_failed",
            "분석 과정 HTML을 준비하지 못했습니다.",
            detail=_safe_exception_message(exc),
        )

    publisher = post_json_fn or _post_report_json
    try:
        response = publisher(post_url, request_body, selected_timeout)
        descriptor = _artifact_descriptor(response, selected_ttl)
        if not descriptor.get("view_url") and not descriptor.get("download_url"):
            raise RuntimeError("Report API 응답에 유효한 보기 또는 다운로드 URL이 없습니다.")
    except Exception as exc:  # noqa: BLE001 - this is intentionally a soft-fail boundary.
        return _record_artifact_failure(
            payload,
            "execution_report_publish_failed",
            "분석 과정 HTML을 저장하지 못했습니다.",
            detail=_safe_exception_message(exc),
        )

    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
    retained = [item for item in artifacts if not _is_same_execution_artifact(item)]
    retained.append(descriptor)
    payload["artifacts"] = retained
    inspection = _inspection(payload)
    inspection["execution_trace_artifact"] = {
        "stage": "25_execution_trace_artifact_publisher",
        "status": "published",
        "report_id": descriptor.get("report_id", ""),
        "ttl_hours": descriptor.get("ttl_hours", selected_ttl),
        "html_bytes": html_size,
        "trace_id": explanation.get("trace_id", ""),
    }
    return payload


# 함수 설명: `build_execution_explanation()`은 raw trace와 제한된 표 preview를 사용자용 실행 사실로 정규화합니다.
def build_execution_explanation(
    payload_value: Any,
    report_data_preview: Any = None,
) -> dict[str, Any]:
    payload = _payload_view(payload_value)
    plan = _dict(payload.get("intent_plan"))
    analysis = _dict(payload.get("analysis"))
    data = _dict(payload.get("data"))
    inspection = _inspection(payload)
    execution_gate = _dict(payload.get("execution_gate"))
    route = _route_value(payload, plan, analysis, inspection, execution_gate)
    status = _status_value(payload, analysis, execution_gate)
    result_rows = _safe_int(data.get("row_count"), len(data.get("rows", [])) if isinstance(data.get("rows"), list) else 0)
    result_columns = _string_list(data.get("columns"))
    if not result_columns:
        result_columns = _columns_from_rows(data.get("rows"))
    report_preview = _dict(report_data_preview)

    explanation = {
        "contract_version": EXPLANATION_VERSION,
        "generated_at": datetime.now(KST).isoformat(),
        "trace_id": _trace_id(payload),
        "question": _question(payload),
        "summary": {
            "status": status,
            "status_label": _status_label(status),
            "route": route,
            "route_label": _route_label(route),
            "result_row_count": result_rows,
            "partial": _is_partial_result(analysis, data),
            "analysis_type": _analysis_type_value(plan, analysis, inspection),
        },
        "domains": _domain_items(payload, plan, report_preview.get("domains")),
        "domain_reasons": _domain_reasons(plan, inspection),
        # The answer adapter can optionally show a verbose intent-analysis
        # section.  The report carries the same user-safe planning facts as a
        # separate disclosure, never as a raw trace dump.
        "intent_analysis": _intent_analysis_projection(payload, plan, inspection),
        "retrievals": _retrieval_items(plan, payload.get("source_results")),
        "processing_steps": _processing_steps(plan, payload, inspection, data),
        "execution_code": _execution_code_projection(payload, inspection),
        "result": {
            "row_count": result_rows,
            "columns": result_columns[:MAX_COLUMNS_PER_SOURCE],
            "mode": _safe_text(
                _dict(plan.get("output_contract")).get("result_mode")
                or data.get("mode")
                or analysis.get("result_mode"),
                80,
            ),
            "partial": _is_partial_result(analysis, data),
            "last_successful_step": _last_successful_step(payload, analysis, inspection),
        },
        "data_tables": _execution_data_tables(report_preview, payload, data),
        "issues": _issue_items(payload, analysis, inspection, execution_gate),
    }
    return explanation


# 함수 설명: `render_execution_report_html()`은 외부 리소스 없이 반응형 분석 과정 HTML을 만듭니다.
def render_execution_report_html(explanation_value: Any) -> str:
    explanation = _dict(explanation_value)
    summary = _dict(explanation.get("summary"))
    status = _safe_text(summary.get("status"), 40).lower() or "unknown"
    status_class = _status_class(status)
    question = _html_text(explanation.get("question"), 2_000) or "질문을 확인할 수 없습니다."
    route_label = _html_text(summary.get("route_label"), 100) or "처리 경로 미확정"
    status_label = _html_text(summary.get("status_label"), 100) or "상태 확인 필요"
    row_count = _format_number(summary.get("result_row_count"))
    partial_note = "부분 결과가 사용되었습니다" if summary.get("partial") else "최종 결과 기준"
    generated_at = _format_timestamp(explanation.get("generated_at"))
    trace_id = _html_text(explanation.get("trace_id"), 160) or "-"
    data_workbench_script = _data_workbench_script()
    intent_analysis = _intent_analysis_html(explanation.get("intent_analysis"))
    data_confirmation = _data_tables_html(explanation.get("data_tables"))

    timeline = "\n".join(
        [
            _timeline_item(
                "01",
                "사용한 도메인 정보",
                "질문 해석에 적용된 공정·지표·분석 규칙입니다.",
                _domains_html(explanation.get("domains"), explanation.get("domain_reasons")),
                "domain",
            ),
            _timeline_item(
                "02",
                "데이터 조회",
                "조회한 데이터셋과 필수 파라미터, 적용 조건, 실제 조회 결과입니다.",
                _retrievals_html(explanation.get("retrievals")),
                "retrieval",
            ),
            _timeline_item(
                "03",
                "데이터 처리 과정",
                "조건·집계·결합·계산 순서와 실행에 사용된 pandas 코드·고정 계약을 보여줍니다.",
                _processing_html(
                    explanation.get("processing_steps"),
                    explanation.get("execution_code"),
                ),
                "processing",
            ),
            _timeline_item(
                "04",
                "결과 생성",
                "최종 결과의 건수와 표시 기준입니다.",
                _result_html(explanation.get("result")),
                "result",
            ),
        ]
    )
    issues = _issues_html(explanation.get("issues"))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>분석 처리 과정</title>
  <style>
    :root {{
      --ink: #202431;
      --muted: #6d7284;
      --line: #e3e8f2;
      --surface: #ffffff;
      --background: #f1f2f6;
      --blue: #5878ef;
      --blue-strong: #4f70e8;
      --blue-deep: #3e60dc;
      --blue-soft: #edf1ff;
      --blue-pale: #f7f8ff;
      --green: #268365;
      --green-soft: #eaf8f2;
      --amber: #a36a12;
      --amber-soft: #fff6e4;
      --red: #bd4c5a;
      --red-soft: #fff1f3;
      --shadow: 0 18px 42px rgba(42, 54, 96, .10);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--background); color: var(--ink); font-family: "Noto Sans KR", "Noto Sans CJK KR", "Malgun Gothic", "Apple SD Gothic Neo", Arial, sans-serif; font-synthesis: none; line-height: 1.6; }}
    button, input, select {{ font-family: inherit; }}
    .page {{ width: min(1160px, calc(100% - 40px)); margin: 28px auto 60px; }}
    .hero {{ position: relative; isolation: isolate; min-height: 226px; padding: 36px 40px; overflow: hidden; border-radius: 20px; color: #fff; background: linear-gradient(135deg, #5f80f1 0%, #5576ea 58%, #5272e4 100%); box-shadow: var(--shadow); }}
    .hero::before {{ content: ""; position: absolute; z-index: -1; width: 750px; height: 750px; top: -515px; left: -340px; border: 1px solid rgba(255,255,255,.15); border-radius: 50%; background: radial-gradient(circle at 50% 50%, rgba(67, 92, 216, .32) 0 51%, rgba(255,255,255,.07) 51.3% 100%); }}
    .hero::after {{ content: ""; position: absolute; z-index: -1; width: 380px; height: 380px; right: -172px; bottom: -286px; border: 1px solid rgba(255,255,255,.12); border-radius: 50%; background: rgba(255,255,255,.045); }}
    .eyebrow {{ margin: 0 0 8px; color: rgba(255,255,255,.77); font-size: 11px; font-weight: 800; letter-spacing: .095em; }}
    h1 {{ margin: 0; font-size: clamp(27px, 4vw, 37px); font-weight: 800; letter-spacing: -.045em; }}
    .hero-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 22px; align-items: end; margin-top: 24px; }}
    .question {{ display: flex; align-items: flex-start; gap: .15em; margin: 0; max-width: 760px; color: rgba(255,255,255,.96); font-size: clamp(18px, 2vw, 20px); font-weight: 650; line-height: 1.5; letter-spacing: -.025em; word-break: break-word; }}
    .question-quote {{ flex: 0 0 auto; color: rgba(255,255,255,.72); font-family: Georgia, "Times New Roman", serif; font-size: 1.18em; font-weight: 700; line-height: 1.16; }}
    .question > span:not(.question-quote) {{ min-width: 0; }}
    .status-chip {{ display: inline-flex; align-items: center; justify-content: center; min-width: 106px; min-height: 40px; padding: 7px 14px; border: 1px solid rgba(255,255,255,.38); border-radius: 9px; background: rgba(255,255,255,.16); color: #fff; font-size: 13px; font-weight: 800; box-shadow: 0 8px 18px rgba(48, 69, 169, .12); }}
    .status-chip.blocked, .status-chip.error {{ background: rgba(132, 24, 36, .48); }}
    .status-chip.partial {{ background: rgba(143, 91, 0, .55); }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 18px 0 30px; }}
    .summary-card {{ min-height: 114px; padding: 20px; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); box-shadow: 0 9px 22px rgba(47, 58, 102, .045); }}
    .summary-card .label {{ margin: 0 0 8px; color: var(--muted); font-size: 13px; }}
    .summary-card .value {{ margin: 0; color: var(--ink); font-size: clamp(17px, 1.45vw, 19px); font-weight: 800; line-height: 1.38; letter-spacing: -.02em; overflow-wrap: anywhere; }}
    .summary-card .sub {{ margin: 4px 0 0; color: var(--muted); font-size: 13px; }}
    .intent-analysis-panel {{ position: relative; overflow: hidden; margin: 0 0 30px; border: 1px solid #dce5fa; border-radius: 17px; background: #fff; box-shadow: 0 13px 28px rgba(47, 62, 122, .055); }}
    .intent-analysis-panel::before {{ content: ""; position: absolute; z-index: 0; top: 0; right: 0; left: 0; height: 3px; background: linear-gradient(90deg, #5274eb 0%, #7994f7 50%, #d8e2ff 100%); }}
    .intent-analysis-panel summary {{ position: relative; z-index: 1; display: grid; grid-template-columns: 34px minmax(0, 1fr) auto auto; align-items: center; gap: 13px; padding: 19px 20px 18px; cursor: pointer; list-style: none; background: linear-gradient(135deg, #fcfdff 0%, #f5f7ff 74%, #f1f4ff 100%); }}
    .intent-analysis-panel summary::-webkit-details-marker {{ display: none; }}
    .intent-summary-icon {{ display: grid; grid-template-columns: repeat(3, 1fr); align-items: end; gap: 3px; width: 34px; height: 34px; padding: 8px; border: 1px solid #cbd8fb; border-radius: 10px; background: linear-gradient(145deg, #eef2ff, #dfe8ff); box-shadow: inset 0 1px 0 rgba(255,255,255,.72); }}
    .intent-summary-icon i {{ display: block; border-radius: 3px 3px 2px 2px; background: linear-gradient(180deg, #6e8bf3, #4e6dde); }}
    .intent-summary-icon i:nth-child(1) {{ height: 42%; }}
    .intent-summary-icon i:nth-child(2) {{ height: 76%; }}
    .intent-summary-icon i:nth-child(3) {{ height: 58%; }}
    .intent-summary-main {{ display: grid; min-width: 0; gap: 2px; }}
    .intent-summary-eyebrow {{ color: #7784a4; font-size: 10px; font-weight: 900; letter-spacing: .105em; line-height: 1.25; }}
    .intent-summary-title-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; }}
    .intent-summary-main strong {{ color: var(--ink); font-size: 17px; font-weight: 850; letter-spacing: -.022em; }}
    .intent-summary-subtitle {{ min-width: 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
    .intent-route-pill {{ display: inline-flex; align-items: center; min-height: 21px; padding: 3px 8px; border: 1px solid #cbd9fb; border-radius: 999px; color: var(--blue-deep); background: rgba(237,241,255,.9); font-size: 11px; font-weight: 850; line-height: 1; white-space: nowrap; }}
    .intent-summary-stat {{ display: grid; flex: 0 0 auto; gap: 1px; min-width: 98px; padding: 7px 10px; border: 1px solid #e1e7f7; border-radius: 10px; color: var(--muted); background: rgba(255,255,255,.72); font-size: 11px; line-height: 1.35; text-align: right; }}
    .intent-summary-stat b {{ color: #485576; font-size: 12px; font-weight: 850; }}
    .intent-disclosure {{ display: inline-flex; align-items: center; gap: 7px; flex: 0 0 auto; color: var(--blue-deep); font-size: 12px; font-weight: 850; white-space: nowrap; }}
    .intent-disclosure::after {{ content: ""; width: 7px; height: 7px; border-right: 1.8px solid currentColor; border-bottom: 1.8px solid currentColor; transform: rotate(45deg) translateY(-2px); transition: transform .18s ease; }}
    .intent-analysis-panel[open] .intent-disclosure::after {{ transform: rotate(225deg) translate(-2px, -2px); }}
    .intent-analysis-panel[open] summary {{ border-bottom: 1px solid #e7ecf8; }}
    .intent-analysis-body {{ padding: 20px; background: linear-gradient(180deg, #fbfcff 0%, #fff 250px); }}
    .intent-overview-grid {{ display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 9px; }}
    .intent-overview-card {{ grid-column: span 3; min-width: 0; padding: 13px 14px; border: 1px solid #e2e8f7; border-radius: 11px; background: rgba(255,255,255,.86); box-shadow: 0 4px 12px rgba(56, 73, 130, .026); }}
    .intent-overview-card.primary {{ grid-column: span 4; border-color: #d4defa; background: linear-gradient(145deg, #f3f6ff, #fff); }}
    .intent-overview-card.route {{ grid-column: span 3; }}
    .intent-overview-card.scope {{ grid-column: span 3; }}
    .intent-overview-card.recipe {{ grid-column: span 2; }}
    .intent-overview-card span {{ display: block; color: #7a85a1; font-size: 10px; font-weight: 850; letter-spacing: .025em; }}
    .intent-overview-card strong {{ display: block; margin-top: 5px; color: var(--ink); font-size: 13px; font-weight: 800; line-height: 1.42; overflow-wrap: anywhere; }}
    .intent-overview-card.primary strong {{ color: #314eae; }}
    .intent-section {{ margin-top: 19px; }}
    .intent-section h4 {{ display: flex; align-items: center; gap: 7px; margin: 0 0 9px; color: #4a5674; font-size: 12px; font-weight: 850; letter-spacing: -.01em; }}
    .intent-section h4::before {{ content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--blue); box-shadow: 0 0 0 4px var(--blue-soft); }}
    .intent-reason-list {{ display: grid; gap: 7px; margin: 0; padding: 14px 16px 14px 34px; border: 1px solid #e3e8f5; border-radius: 11px; color: #5c667e; background: rgba(255,255,255,.9); font-size: 13px; }}
    .intent-reason-list li::marker {{ color: var(--blue-deep); font-weight: 800; }}
    .intent-meta-list {{ display: flex; flex-wrap: wrap; gap: 7px; }}
    .intent-meta-chip {{ display: inline-flex; align-items: center; gap: 5px; max-width: 100%; padding: 5px 9px; border: 1px solid #dfe6f6; border-radius: 999px; color: #596583; background: rgba(255,255,255,.88); font-size: 12px; overflow-wrap: anywhere; }}
    .intent-meta-chip b {{ color: var(--blue-deep); font-weight: 850; }}
    .intent-plan-list {{ display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }}
    .intent-plan-item {{ position: relative; padding: 13px 14px 13px 16px; border: 1px solid #e3e9f6; border-radius: 11px; background: rgba(255,255,255,.92); }}
    .intent-plan-item::before {{ content: ""; position: absolute; top: 14px; bottom: 14px; left: 0; width: 3px; border-radius: 0 3px 3px 0; background: #b9cafc; }}
    .intent-plan-item-head {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px; }}
    .intent-plan-item-head strong {{ color: var(--ink); font-size: 13px; font-weight: 800; }}
    .intent-plan-item-head span {{ color: #7a849b; font-size: 12px; }}
    .intent-plan-detail {{ margin: 7px 0 0; color: #5d687f; font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }}
    .data-confirmation-section {{ margin: 0 0 30px; }}
    .section-title {{ display: flex; align-items: center; gap: 9px; margin: 0 0 16px; color: #292d3a; font-size: 21px; font-weight: 800; letter-spacing: -.035em; }}
    .section-title::before {{ content: ""; width: 5px; height: 23px; border-radius: 6px; background: var(--blue); box-shadow: 0 3px 8px rgba(88,120,239,.28); }}
    .timeline {{ position: relative; margin-left: 13px; padding-left: 33px; border-left: 2px solid #dce4ff; }}
    .timeline-item {{ position: relative; padding: 0 0 22px; }}
    .timeline-item:last-child {{ padding-bottom: 0; }}
    .timeline-dot {{ position: absolute; left: -48px; top: 21px; display: grid; place-items: center; width: 28px; height: 28px; border: 3px solid var(--background); border-radius: 50%; background: var(--blue); color: #fff; font-size: 10px; font-weight: 800; box-shadow: 0 0 0 1px #c9d5ff, 0 5px 12px rgba(88,120,239,.23); }}
    .timeline-card {{ overflow: hidden; border: 1px solid var(--line); border-radius: 14px; background: var(--surface); box-shadow: 0 9px 22px rgba(47, 58, 102, .045); }}
    .timeline-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 19px 21px 14px; border-bottom: 1px solid #edf1f6; }}
    .timeline-head h3 {{ margin: 0; font-size: 18px; letter-spacing: -.02em; }}
    .timeline-head p {{ margin: 0; color: var(--muted); font-size: 13px; text-align: right; }}
    .timeline-body {{ padding: 18px 21px 21px; }}
    .empty {{ margin: 0; padding: 18px; border: 1px dashed #cfd8ed; border-radius: 10px; color: var(--muted); background: var(--blue-pale); font-size: 14px; }}
    .domain-list {{ display: grid; gap: 10px; }}
    .domain-card {{ overflow: hidden; border: 1px solid #e1e7f5; border-radius: 12px; background: #fff; box-shadow: 0 5px 15px rgba(48, 59, 106, .028); }}
    .domain-card summary {{ display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 13px 14px; cursor: pointer; list-style: none; }}
    .domain-card summary::-webkit-details-marker {{ display: none; }}
    .domain-card summary::before {{ content: ""; display: block; flex: 0 0 auto; width: 20px; height: 20px; border-radius: 6px; background: linear-gradient(var(--blue-deep), var(--blue-deep)) center / 10px 1.8px no-repeat, linear-gradient(var(--blue-deep), var(--blue-deep)) center / 1.8px 10px no-repeat, var(--blue-soft); }}
    .domain-card[open] summary::before {{ background: linear-gradient(var(--blue-deep), var(--blue-deep)) center / 10px 1.8px no-repeat, var(--blue-soft); }}
    .domain-card-main {{ display: grid; flex: 1; min-width: 0; gap: 2px; }}
    .domain-card-main strong {{ color: var(--ink); overflow-wrap: anywhere; }}
    .domain-category {{ width: fit-content; padding: 2px 7px; border-radius: 999px; background: var(--blue-soft); color: var(--blue-deep); font-size: 11px; font-weight: 800; }}
    .domain-key {{ color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
    .domain-disclosure {{ flex: 0 0 auto; color: var(--blue-deep); font-size: 12px; font-weight: 700; }}
    .domain-card-body {{ padding: 0 14px 15px; border-top: 1px solid #edf0f8; }}
    .domain-summary {{ margin: 12px 0; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
    .domain-detail-grid {{ display: grid; grid-template-columns: minmax(120px, .31fr) 1fr; gap: 8px 14px; margin: 0; padding: 12px; border-radius: 9px; background: var(--blue-pale); font-size: 12px; }}
    .domain-detail-grid dt {{ color: #616b8c; font-weight: 800; overflow-wrap: anywhere; }}
    .domain-detail-grid dd {{ min-width: 0; margin: 0; color: var(--ink); overflow-wrap: anywhere; }}
    .domain-detail-grid .domain-detail-grid {{ margin: 0; padding: 9px; border: 1px solid #dfe6f7; background: #fff; }}
    .domain-detail-list {{ display: grid; gap: 5px; margin: 0; padding-left: 18px; }}
    .domain-value {{ overflow-wrap: anywhere; white-space: pre-wrap; }}
    .reason-list {{ display: grid; gap: 7px; margin: 15px 0 0; padding: 14px 15px; border: 1px solid #e6ebf8; border-radius: 11px; background: var(--blue-pale); color: var(--muted); font-size: 13px; }}
    .reason-list strong {{ color: var(--ink); }}
    .reason-list ul {{ display: grid; gap: 5px; margin: 0; padding-left: 18px; }}
    .mini-card {{ margin-top: 12px; padding: 15px; border: 1px solid #e5eaf4; border-radius: 12px; background: #fff; box-shadow: 0 5px 15px rgba(48, 59, 106, .025); }}
    .mini-card:first-child {{ margin-top: 0; }}
    .mini-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 11px; }}
    .mini-title {{ margin: 0; font-weight: 800; }}
    .badge {{ display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 999px; background: var(--green-soft); color: var(--green); font-size: 12px; font-weight: 800; }}
    .badge.error {{ background: var(--red-soft); color: var(--red); }}
    .badge.pending {{ background: var(--amber-soft); color: var(--amber); }}
    .kv-grid {{ display: grid; grid-template-columns: minmax(90px, .32fr) 1fr; gap: 7px 14px; margin: 0; font-size: 13px; }}
    .kv-grid dt {{ color: var(--muted); }}
    .kv-grid dd {{ margin: 0; color: var(--ink); overflow-wrap: anywhere; }}
    .step-list {{ display: grid; gap: 10px; }}
    .step {{ display: grid; grid-template-columns: 34px 1fr; gap: 12px; padding: 14px; border: 1px solid #e7ecf7; border-radius: 12px; background: var(--blue-pale); }}
    .step-number {{ display: grid; place-items: center; width: 30px; height: 30px; border-radius: 8px; background: #dfe7ff; color: var(--blue-deep); font-size: 12px; font-weight: 800; }}
    .step h4 {{ margin: 0 0 4px; font-size: 15px; }}
    .step p {{ margin: 0; color: var(--muted); font-size: 13px; }}
    .code-panel {{ margin-top: 14px; overflow: hidden; border: 1px solid #dce4f6; border-radius: 12px; background: #fff; box-shadow: 0 7px 18px rgba(48, 59, 106, .035); }}
    .code-panel summary {{ display: flex; align-items: center; gap: 12px; padding: 14px; cursor: pointer; list-style: none; background: linear-gradient(180deg, #fbfcff 0%, #f6f8ff 100%); }}
    .code-panel summary::-webkit-details-marker {{ display: none; }}
    .code-panel summary::before {{ content: ""; display: block; flex: 0 0 auto; width: 20px; height: 20px; border-radius: 6px; background: linear-gradient(var(--blue-deep), var(--blue-deep)) center / 10px 1.8px no-repeat, linear-gradient(var(--blue-deep), var(--blue-deep)) center / 1.8px 10px no-repeat, var(--blue-soft); }}
    .code-panel[open] summary::before {{ background: linear-gradient(var(--blue-deep), var(--blue-deep)) center / 10px 1.8px no-repeat, var(--blue-soft); }}
    .code-summary-main {{ display: grid; flex: 1; min-width: 0; gap: 2px; }}
    .code-summary-main strong {{ color: var(--ink); font-size: 14px; overflow-wrap: anywhere; }}
    .code-summary-main > span {{ color: var(--muted); font-size: 12px; }}
    .code-badges {{ display: flex; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }}
    .code-panel-body {{ padding: 14px; border-top: 1px solid #e8edf7; }}
    .code-note, .code-helper-note {{ margin: 0 0 10px; color: var(--muted); font-size: 12px; }}
    .code-helper-note strong {{ margin-right: 6px; color: var(--ink); }}
    .code-helper-note span {{ color: #8b92a6; }}
    .execution-code {{ max-height: 520px; margin: 0; padding: 16px 18px; overflow: auto; border: 1px solid #30363d; border-radius: 10px; color: #d4d4d4; background: #0d1117; font: 12px/1.65 Consolas, "SFMono-Regular", "Cascadia Code", "Courier New", monospace; white-space: pre; tab-size: 4; scrollbar-color: #58637a #161b22; text-shadow: none; }}
    .execution-code code {{ font: inherit; }}
    .syntax-keyword {{ color: #569cd6; font-weight: 650; }}
    .syntax-string {{ color: #ce9178; }}
    .syntax-number {{ color: #b5cea8; }}
    .syntax-comment {{ color: #6a9955; font-style: italic; }}
    .syntax-builtin {{ color: #4ec9b0; }}
    .syntax-function {{ color: #dcdcaa; }}
    .syntax-class {{ color: #4ec9b0; font-weight: 650; }}
    .syntax-decorator {{ color: #c586c0; }}
    .syntax-attribute {{ color: #9cdcfe; }}
    .syntax-name {{ color: #9cdcfe; }}
    .syntax-operator {{ color: #d4d4d4; }}
    .data-workbench {{ overflow: hidden; border: 1px solid #e1e6f1; border-radius: 16px; background: #fff; box-shadow: 0 12px 27px rgba(45, 57, 102, .045); }}
    .data-tabs {{ position: sticky; top: 0; z-index: 2; display: flex; gap: 8px; padding: 12px; border-bottom: 1px solid #e7ebf4; background: rgba(247,248,255,.96); backdrop-filter: blur(12px); overflow-x: auto; }}
    .data-tab {{ display: inline-flex; align-items: center; gap: 7px; min-height: 36px; padding: 7px 12px; border: 1px solid #dce3f3; border-radius: 8px; color: #656f8d; background: rgba(255,255,255,.88); font-size: 13px; font-weight: 800; white-space: nowrap; cursor: pointer; transition: background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease; }}
    .data-tab:hover:not(:disabled) {{ border-color: #aebef4; color: var(--blue-deep); background: #fff; transform: translateY(-1px); }}
    .data-tab.active {{ border-color: var(--blue); color: #fff; background: var(--blue); box-shadow: 0 6px 14px rgba(88,120,239,.22); }}
    .data-tab:disabled {{ opacity: .48; cursor: not-allowed; }}
    .data-tab span {{ display: grid; place-items: center; min-width: 19px; height: 19px; padding: 0 5px; border-radius: 999px; background: var(--blue-soft); color: var(--blue-deep); font-size: 11px; }}
    .data-tab.active span {{ background: rgba(255,255,255,.2); color: #fff; }}
    .data-workbench-head {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 16px 16px 10px; }}
    .data-workbench-head h4 {{ margin: 0; font-size: 16px; letter-spacing: -.015em; }}
    .data-workbench-head p {{ margin: 3px 0 0; color: var(--muted); font-size: 12px; }}
    .data-group-count {{ flex: 0 0 auto; padding: 4px 9px; border-radius: 999px; color: var(--blue-deep); background: var(--blue-soft); font-size: 12px; font-weight: 800; }}
    .data-toolbar {{ display: grid; grid-template-columns: minmax(160px, 1.35fr) minmax(145px, 1fr) minmax(130px, .8fr) minmax(130px, .8fr) auto auto; gap: 9px; align-items: end; padding: 8px 16px 13px; border-bottom: 1px solid #edf0f6; }}
    .data-control {{ display: grid; gap: 4px; min-width: 0; color: #777e91; font-size: 11px; font-weight: 800; }}
    .data-control select, .data-control input {{ width: 100%; min-height: 35px; padding: 6px 9px; border: 1px solid #dbe1ef; border-radius: 8px; color: var(--ink); background: #fff; font: inherit; font-size: 12px; outline: none; transition: border-color .15s ease, box-shadow .15s ease; }}
    .data-control select:focus, .data-control input:focus {{ border-color: var(--blue); box-shadow: 0 0 0 3px rgba(88,120,239,.13); }}
    .data-sort-direction, .data-pagination button {{ min-height: 35px; padding: 6px 10px; border: 1px solid #d7dfef; border-radius: 8px; color: var(--blue-deep); background: #fff; font-size: 12px; font-weight: 800; cursor: pointer; transition: background .15s ease, border-color .15s ease, transform .15s ease; }}
    .data-sort-direction:hover:not(:disabled), .data-pagination button:hover:not(:disabled) {{ border-color: #b8c7f5; background: var(--blue-pale); transform: translateY(-1px); }}
    .data-sort-direction:disabled, .data-pagination button:disabled {{ opacity: .48; cursor: not-allowed; }}
    .data-link {{ display: inline-flex; align-items: center; justify-content: center; min-height: 35px; padding: 6px 11px; border: 1px solid var(--blue); border-radius: 8px; color: #fff; background: var(--blue); font-size: 12px; font-weight: 800; text-decoration: none; white-space: nowrap; box-shadow: 0 6px 14px rgba(88,120,239,.17); transition: background .15s ease, transform .15s ease; }}
    .data-link:hover {{ background: var(--blue-deep); transform: translateY(-1px); }}
    .data-workbench-meta {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 16px 0; }}
    .data-workbench-status, .data-workbench-expiry {{ margin: 0; color: var(--muted); font-size: 12px; }}
    .data-workbench-expiry {{ text-align: right; }}
    .data-note {{ margin: 11px 0 0; color: var(--muted); font-size: 12px; }}
    .data-table {{ margin-top: 10px; overflow: hidden; border: 1px solid #dce6f1; border-radius: 11px; background: #fff; }}
    .data-table:first-of-type {{ margin-top: 0; }}
    .data-table summary {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 14px; cursor: pointer; list-style: none; }}
    .data-table summary::-webkit-details-marker {{ display: none; }}
    .data-table summary::before {{ content: ""; display: block; flex: 0 0 auto; width: 19px; height: 19px; margin-right: 8px; border-radius: 50%; background: linear-gradient(var(--blue-deep), var(--blue-deep)) center / 9px 1.7px no-repeat, linear-gradient(var(--blue-deep), var(--blue-deep)) center / 1.7px 9px no-repeat, var(--blue-soft); }}
    .data-table[open] summary::before {{ background: linear-gradient(var(--blue-deep), var(--blue-deep)) center / 9px 1.7px no-repeat, var(--blue-soft); }}
    .data-table-title {{ flex: 1; color: var(--ink); font-size: 14px; font-weight: 800; overflow-wrap: anywhere; }}
    .data-table-meta {{ flex: 0 0 auto; color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .data-table-body {{ padding: 0 14px 14px; border-top: 1px solid #edf1f6; }}
    .data-description {{ margin: 12px 0 0; color: var(--muted); font-size: 13px; }}
    .data-tools {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
    .data-table-wrap {{ max-height: 470px; margin: 12px 16px 0; overflow: auto; border: 1px solid #e0e5ef; border-radius: 10px; background: #fff; }}
    .data-table-wrap table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .data-table-wrap th, .data-table-wrap td {{ padding: 8px 10px; border-bottom: 1px solid #edf1f5; text-align: left; vertical-align: top; white-space: nowrap; }}
    .data-table-wrap th {{ position: sticky; top: 0; z-index: 1; padding: 0; background: #f4f6ff; color: #596383; font-weight: 800; }}
    .data-table-wrap th:first-child {{ padding: 8px 10px; }}
    .data-table-wrap tr:last-child td {{ border-bottom: 0; }}
    .data-column-sort {{ display: block; width: 100%; padding: 8px 10px; border: 0; color: inherit; background: transparent; text-align: left; font: inherit; font-weight: 800; cursor: pointer; }}
    .data-column-sort:hover {{ background: #e9eeff; }}
    .data-pagination {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 12px 16px 16px; color: var(--muted); font-size: 12px; }}
    .data-page-size {{ display: flex; align-items: center; gap: 5px; margin-left: 4px; }}
    .data-page-size select {{ width: auto; min-height: 30px; }}
    .row-number {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
    .issue-section {{ margin-top: 30px; }}
    .issue {{ margin-top: 11px; padding: 17px 19px; border: 1px solid #f0d2d7; border-radius: 14px; background: var(--red-soft); }}
    .issue.warning {{ border-color: #eedba3; background: var(--amber-soft); }}
    .issue h3 {{ margin: 0 0 5px; color: var(--red); font-size: 15px; }}
    .issue.warning h3 {{ color: var(--amber); }}
    .issue p {{ margin: 0; font-size: 14px; overflow-wrap: anywhere; }}
    .footer {{ margin: 30px 0 0; color: var(--muted); font-size: 12px; text-align: center; }}
    .data-tab:focus-visible, .data-sort-direction:focus-visible, .data-pagination button:focus-visible, .data-link:focus-visible, .data-column-sort:focus-visible, .domain-card summary:focus-visible, .code-panel summary:focus-visible, .intent-analysis-panel summary:focus-visible {{ outline: 3px solid rgba(88,120,239,.35); outline-offset: 2px; }}
    @media (max-width: 860px) {{ .data-toolbar {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .data-download-link {{ grid-column: span 2; }} }}
    @media (max-width: 760px) {{ .page {{ width: min(100% - 24px, 1120px); margin-top: 12px; }} .hero {{ padding: 24px; border-radius: 17px; }} .hero-grid, .summary-grid, .intent-overview-grid {{ grid-template-columns: 1fr; }} .intent-analysis-panel summary {{ grid-template-columns: 34px minmax(0, 1fr) auto; align-items: start; padding: 17px 16px; }} .intent-summary-stat {{ grid-column: 2 / 3; grid-row: 2; justify-self: start; min-width: 0; grid-auto-flow: column; align-items: center; gap: 7px; padding: 5px 8px; text-align: left; }} .intent-disclosure {{ grid-column: 3; grid-row: 1; white-space: nowrap; }} .intent-overview-card, .intent-overview-card.primary, .intent-overview-card.route, .intent-overview-card.scope, .intent-overview-card.recipe {{ grid-column: 1; }} .timeline {{ margin-left: 10px; padding-left: 25px; }} .timeline-dot {{ left: -40px; }} .timeline-head {{ display: block; }} .timeline-head p {{ margin-top: 5px; text-align: left; }} .kv-grid, .domain-detail-grid {{ grid-template-columns: 1fr; gap: 2px; }} .kv-grid dd {{ margin-bottom: 8px; }} .domain-detail-grid dd {{ margin-bottom: 9px; }} .code-panel summary {{ align-items: flex-start; flex-wrap: wrap; }} .code-badges {{ width: 100%; padding-left: 32px; justify-content: flex-start; }} .execution-code {{ max-height: 430px; padding: 14px; }} .data-workbench-head, .data-workbench-meta {{ display: block; }} .data-group-count {{ display: inline-flex; margin-top: 8px; }} .data-workbench-expiry {{ margin-top: 4px; text-align: left; }} .data-toolbar {{ grid-template-columns: 1fr; }} .data-download-link {{ grid-column: auto; }} .data-pagination {{ flex-wrap: wrap; justify-content: flex-start; }} .data-table summary {{ align-items: flex-start; }} .data-table-meta {{ white-space: normal; text-align: right; }} }}
  </style>
</head>
<body>
  <main class="page">
    <header class="hero">
      <p class="eyebrow">DATA ANALYSIS · EXECUTION TRACE</p>
      <h1>분석 처리 과정</h1>
      <div class="hero-grid">
        <p class="question"><span class="question-quote" aria-hidden="true">&ldquo;</span><span>{question}</span><span class="question-quote" aria-hidden="true">&rdquo;</span></p>
        <span class="status-chip {status_class}">{status_label}</span>
      </div>
    </header>

    <section class="summary-grid" aria-label="처리 요약">
      <article class="summary-card"><p class="label">실행 경로</p><p class="value">{route_label}</p><p class="sub">질문에 맞춰 선택된 처리 방식</p></article>
      <article class="summary-card"><p class="label">최종 결과</p><p class="value">{row_count}건</p><p class="sub">{partial_note}</p></article>
      <article class="summary-card"><p class="label">분석 유형</p><p class="value">{_html_text(summary.get("analysis_type"), 120) or "일반 데이터 분석"}</p><p class="sub">생성 시각: {generated_at}</p></article>
    </section>

    {intent_analysis}

    <section class="data-confirmation-section" aria-labelledby="data-confirmation-title">
      <h2 id="data-confirmation-title" class="section-title">데이터 확인</h2>
      {data_confirmation}
    </section>

    <section aria-labelledby="timeline-title">
      <h2 id="timeline-title" class="section-title">처리 흐름</h2>
      <div class="timeline">{timeline}</div>
    </section>

    {issues}
    <p class="footer">추적 ID: {trace_id} · 이 문서는 실행 사실과 제한된 데이터 미리보기, 마스킹된 실행 코드를 담은 안내용 Report입니다. 데이터 탭에서는 전체 저장 데이터를 같은 화면에서 검색·정렬·페이지 이동할 수 있으며, CSV 다운로드도 유지됩니다. 인증 정보·raw trace·helper 내부 구현은 포함하지 않습니다.</p>
  </main>
  {data_workbench_script}
</body>
</html>"""


# 함수 설명: `_domain_items()`는 선택된 metadata reference와 일회성 detail projection을 사용자용 도메인 카드로 정리합니다.
def _domain_items(
    payload: dict[str, Any],
    plan: dict[str, Any],
    detail_value: Any = None,
) -> list[dict[str, Any]]:
    candidates = []
    for container in (payload.get("metadata_refs"), plan.get("metadata_refs")):
        if isinstance(container, list):
            candidates.extend(container)

    # Canonical Flow에서는 Node 04가 선택된 항목만 이 projection으로
    # 전달합니다.  payload에 후보가 남아 있는 오래된 Flow/direct caller도
    # 읽을 수 있게 하되, 원문이 없으면 ref만 보여 주고 임의로 복원하지 않습니다.
    details_by_ref = _domain_detail_index(detail_value)
    if not details_by_ref:
        details_by_ref = _domain_detail_index_from_payload(payload)

    domains: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        section = _safe_text(item.get("section") or item.get("type"), 120)
        key = _safe_text(item.get("key") or item.get("dataset_key"), 180)
        if not section and not key:
            continue
        signature = f"{section.casefold()}|{key.casefold()}"
        if signature in seen:
            continue
        seen.add(signature)
        detail = _dict(details_by_ref.get(signature))
        title = _safe_text(detail.get("title"), 180) or key or "등록 정보"
        summary = _safe_text(detail.get("summary"), MAX_TEXT_LENGTH)
        domains.append(
            {
                "category": SECTION_LABELS.get(section.casefold(), section or "도메인 정보"),
                "section": section,
                "key": key or "등록 정보",
                "title": title,
                "summary": summary,
                "details": _safe_domain_detail(detail.get("details")),
            }
        )
        if len(domains) >= MAX_DOMAINS:
            break
    return domains


def _domain_detail_index(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in _list(value)[:MAX_DOMAINS]:
        if not isinstance(item, dict):
            continue
        section = _safe_text(item.get("section") or item.get("type"), 120)
        key = _safe_text(item.get("key") or item.get("dataset_key"), 180)
        if not section or not key:
            continue
        result.setdefault(f"{section.casefold()}|{key.casefold()}", item)
    return result


def _domain_detail_index_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, Any] = {}
    direct = _dict(payload.get("metadata_candidates"))
    envelope = _dict(payload.get("metadata_candidate_envelope"))
    nested = _dict(envelope.get("metadata_candidates"))
    for value in (direct, nested):
        if isinstance(value.get("domain_items"), list):
            candidates = value
            break
    items = candidates.get("domain_items") if isinstance(candidates.get("domain_items"), list) else []
    projected: list[dict[str, Any]] = []
    for item in items[:MAX_DOMAINS]:
        if not isinstance(item, dict):
            continue
        metadata = _dict(item.get("payload")) or item
        projected.append(
            {
                "section": item.get("section") or item.get("type") or metadata.get("section"),
                "key": item.get("key") or item.get("dataset_key") or metadata.get("key"),
                "title": metadata.get("display_name") or metadata.get("name"),
                "summary": metadata.get("description") or metadata.get("definition") or metadata.get("usage_rule"),
                "details": metadata,
            }
        )
    return _domain_detail_index(projected)


def _safe_domain_detail(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:28]:
            key = _safe_text(raw_key, 120)
            if not key or _is_sensitive_mapping_key(key):
                continue
            if _domain_detail_hidden_key(key):
                continue
            result[key] = _safe_domain_detail(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_safe_domain_detail(item, depth + 1) for item in value[:16]]
    return _safe_text(value, MAX_TEXT_LENGTH)


def _domain_detail_hidden_key(value: Any) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", _safe_text(value, 120).casefold())
    return compact in {
        "sourceconfig",
        "query",
        "querytemplate",
        "sql",
        "sqltemplate",
        "endpoint",
        "url",
    }


# 함수 설명: 정규화된 계획 근거만 도메인 선택 이유로 표시합니다.
def _domain_reasons(plan: dict[str, Any], inspection: dict[str, Any]) -> list[str]:
    reasons = _safe_reason_list(plan.get("decision_reason"))
    if not reasons:
        intent = _dict(inspection.get("intent"))
        reasons = _safe_reason_list(intent.get("decision_reason"))
    return reasons[:4]


# 함수 설명: 21번 답변 노드의 의도 분석과 같은 planning 사실을 Report용 제한 구조로 정리합니다.
def _intent_analysis_projection(
    payload: dict[str, Any],
    plan: dict[str, Any],
    inspection: dict[str, Any],
) -> dict[str, Any]:
    """Project the user-safe subset of the answer adapter's intent section.

    The report already explains applied retrievals and processing later in the
    timeline.  This projection deliberately captures the *plan* separately so
    an operator can compare what was intended with what later executed,
    without serializing raw trace, connection, query, or code fields.
    """

    intent_trace = _dict(inspection.get("intent"))
    route_resolution = _dict(plan.get("route_resolution"))
    analysis = _dict(payload.get("analysis"))
    retrieval_jobs = [
        item
        for item in _list(plan.get("retrieval_jobs"))[:MAX_RETRIEVALS]
        if isinstance(item, dict)
    ]
    # Use the same actual-over-planned projection as the main retrieval
    # timeline.  A report must not show a stale planned DATE/filter in the
    # intent disclosure once execution records the value actually applied.
    retrieval_plan_items = _retrieval_items(plan, payload.get("source_results"))
    pandas_steps = [
        item
        for item in _list(plan.get("pandas_execution_plan"))[:MAX_STEPS]
        if isinstance(item, dict)
    ]
    metadata_refs = _intent_metadata_refs(payload, plan)
    analysis_type = _safe_text(
        plan.get("analysis_kind")
        or plan.get("analysis_type")
        or plan.get("intent_type")
        or intent_trace.get("analysis_kind"),
        160,
    )
    expected_route = _intent_route_label(route_resolution.get("intent_candidate"))
    final_route = _intent_route_label(
        route_resolution.get("final_route")
        or plan.get("execution_path")
        or analysis.get("execution_path")
    )
    recipe = _safe_text(
        route_resolution.get("final_recipe")
        or route_resolution.get("candidate_recipe")
        or plan.get("fast_recipe"),
        160,
    )
    route_reasons = _string_list(
        route_resolution.get("final_reason_codes")
        or route_resolution.get("candidate_reason_codes")
    )[:MAX_ISSUES]
    reasons = _safe_reason_list(
        intent_trace.get("decision_reason") or plan.get("decision_reason")
    )
    result = {
        "analysis_type": analysis_type,
        "expected_route": expected_route,
        "final_route": final_route,
        "recipe": recipe,
        "route_reasons": route_reasons,
        "retrieval_job_count": _safe_int(
            intent_trace.get("retrieval_job_count"), len(retrieval_jobs)
        ),
        "pandas_step_count": _safe_int(
            intent_trace.get("pandas_step_count"), len(pandas_steps)
        ),
        "metadata_refs": metadata_refs,
        "decision_reasons": reasons,
        "retrieval_plan": [
            _intent_retrieval_plan_item(item) for item in retrieval_plan_items
        ],
        "pandas_plan": [_intent_pandas_plan_item(item) for item in pandas_steps],
    }
    return {
        key: value
        for key, value in result.items()
        if value not in (None, "", [], {})
    }


# 함수 설명: 선택된 metadata ref를 21번 답변의 참조 메타데이터처럼 짧게 노출합니다.
def _intent_metadata_refs(payload: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for values in (payload.get("metadata_refs"), plan.get("metadata_refs")):
        for item in _list(values):
            if not isinstance(item, dict):
                continue
            section = _safe_text(item.get("section") or item.get("type"), 100)
            key = _safe_text(item.get("key") or item.get("dataset_key"), 180)
            signature = f"{section.casefold()}|{key.casefold()}"
            if not key or signature in seen:
                continue
            seen.add(signature)
            result.append(
                {
                    "label": SECTION_LABELS.get(section.casefold(), section or "메타데이터"),
                    "key": key,
                }
            )
            if len(result) >= MAX_DOMAINS:
                return result
    return result


# 함수 설명: 조회 계획은 사용자에게 필요한 dataset, alias, params, filters만 안전하게 보입니다.
def _intent_retrieval_plan_item(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset": _safe_text(job.get("dataset") or job.get("dataset_key"), 180) or "데이터셋",
        "alias": _safe_text(job.get("alias") or job.get("source_alias"), 180),
        "source_type": _safe_text(job.get("source_type"), 80),
        "required_params": _safe_mapping(job.get("required_params") or job.get("params")),
        "filters": _safe_mapping(job.get("filters") or job.get("filter_mappings")),
        "params_label": "실제 적용 파라미터" if job.get("params_origin") == "actual" else "계획 파라미터",
        "filters_label": "실제 적용 조건" if job.get("filters_origin") == "actual" else "계획 조건",
    }


# 함수 설명: pandas 계획은 raw dict가 아닌 operation/grain/formula 등 실행 의도만 안전하게 보여줍니다.
def _intent_pandas_plan_item(step: dict[str, Any]) -> dict[str, Any]:
    operation = _safe_text(step.get("operation"), 100)
    source = _safe_text(
        step.get("source_alias")
        or step.get("left_source_alias")
        or step.get("input_alias"),
        180,
    )
    return {
        "operation": operation,
        "operation_label": OPERATION_LABELS.get(operation, _humanize_operation(operation) or "처리 단계"),
        "node_id": _safe_text(step.get("node_id"), 180),
        "source": source,
        "output": _safe_text(step.get("output_alias"), 180),
        "group_by": _string_list(step.get("group_by"))[:MAX_COLUMNS_PER_SOURCE],
        "aggregations": _safe_value(step.get("aggregations")),
        "formula": _safe_mapping(step.get("formula")),
        "sort_by": _safe_text(step.get("sort_by"), 120),
        "order": _safe_text(step.get("order"), 40),
        "limit": _safe_int(step.get("limit"), 0),
    }


# 함수 설명: intent route 내부값은 21번 답변과 같은 Fast/Complex/Blocked 표기로 통일합니다.
def _intent_route_label(value: Any) -> str:
    labels = {
        "fast_candidate": "Fast 후보",
        "complex_candidate": "Complex 후보",
        "complex_required": "Complex 필요",
        "fast": "Fast",
        "complex": "Complex",
        "blocked": "Blocked",
    }
    text = _safe_text(value, 80).casefold()
    return labels.get(text, _safe_text(value, 80))


# 함수 설명: `_retrieval_items()`는 계획과 실제 source 결과를 합쳐 조회 단계를 표시합니다.
def _retrieval_items(plan: dict[str, Any], source_results_value: Any) -> list[dict[str, Any]]:
    source_results = source_results_value if isinstance(source_results_value, list) else []
    by_alias: dict[str, dict[str, Any]] = {}
    for item in source_results:
        if isinstance(item, dict):
            alias = _safe_text(item.get("source_alias") or item.get("dataset_key"), 180)
            if alias:
                by_alias[_lookup_key(alias)] = item
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    retrievals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = _safe_text(job.get("source_alias") or job.get("dataset_key"), 180)
        dataset = _safe_text(job.get("dataset_key"), 180) or alias or "데이터셋"
        actual = by_alias.get(_lookup_key(alias)) or by_alias.get(_lookup_key(dataset)) or {}
        status = _safe_text(actual.get("status"), 60).lower() or "planned"
        if status == "planned" and _safe_error_message(
            actual.get("error_message") or actual.get("errors") or actual.get("error")
        ):
            status = "failed"
        if alias in seen:
            continue
        seen.add(alias)
        actual_params, params_origin = _actual_mapping(actual, "applied_params")
        actual_filters, filters_origin = _actual_mapping(
            actual,
            "pandas_filters",
            "applied_filters",
        )
        planned_params = _safe_mapping(job.get("required_params") or job.get("params"))
        planned_filters = _safe_mapping(job.get("filters") or job.get("filter_mappings"))
        retrievals.append(
            {
                "dataset": dataset,
                "alias": alias or dataset,
                "source_type": _safe_text(job.get("source_type") or actual.get("source_type"), 80),
                # Actual source output wins.  The plan is shown only when a
                # retrieval never began and therefore has no applied record.
                "required_params": actual_params if params_origin else planned_params,
                "params_origin": params_origin or ("planned" if planned_params else ""),
                "filters": actual_filters if filters_origin else planned_filters,
                "filters_origin": filters_origin or ("planned" if planned_filters else ""),
                "status": status,
                "row_count": _safe_int(actual.get("row_count"), 0),
                "columns": _string_list(actual.get("columns"))[:MAX_COLUMNS_PER_SOURCE],
                "error": _safe_error_message(
                    actual.get("error_message")
                    or actual.get("errors")
                    or actual.get("error")
                    or actual.get("skip_reason")
                ),
            }
        )
        if len(retrievals) >= MAX_RETRIEVALS:
            break
    if not retrievals:
        for actual in source_results[:MAX_RETRIEVALS]:
            if not isinstance(actual, dict):
                continue
            dataset = _safe_text(actual.get("dataset_key") or actual.get("source_alias"), 180)
            if not dataset:
                continue
            retrievals.append(
                {
                    "dataset": dataset,
                    "alias": _safe_text(actual.get("source_alias"), 180) or dataset,
                    "source_type": _safe_text(actual.get("source_type"), 80),
                    "required_params": _safe_mapping(actual.get("applied_params")),
                    "params_origin": "actual" if isinstance(actual.get("applied_params"), dict) else "",
                    "filters": _safe_mapping(actual.get("pandas_filters") or actual.get("applied_filters")),
                    "filters_origin": "actual" if isinstance(actual.get("pandas_filters") or actual.get("applied_filters"), dict) else "",
                    "status": _safe_text(actual.get("status"), 60).lower() or "completed",
                    "row_count": _safe_int(actual.get("row_count"), 0),
                    "columns": _string_list(actual.get("columns"))[:MAX_COLUMNS_PER_SOURCE],
                    "error": _safe_error_message(
                        actual.get("error_message")
                        or actual.get("errors")
                        or actual.get("error")
                        or actual.get("skip_reason")
                    ),
                }
            )
    return retrievals


# 함수 설명: `_processing_steps()`는 Typed plan에서 사용자에게 의미 있는 처리 단계만 추출합니다.
def _processing_steps(
    plan: dict[str, Any],
    payload: dict[str, Any],
    inspection: dict[str, Any],
    data: dict[str, Any],
) -> list[dict[str, str]]:
    raw_steps = plan.get("pandas_execution_plan")
    if not isinstance(raw_steps, list):
        raw_steps = plan.get("analysis_steps") if isinstance(plan.get("analysis_steps"), list) else []
    row_counts = _execution_row_counts(payload, inspection, data, raw_steps)
    steps: list[dict[str, str]] = []
    for index, item in enumerate(raw_steps[:MAX_STEPS], start=1):
        if not isinstance(item, dict):
            continue
        operation = _safe_text(item.get("operation"), 80).casefold()
        title = OPERATION_LABELS.get(operation, _humanize_operation(operation) or "분석 처리")
        detail = _step_detail(item, row_counts)
        steps.append(
            {
                "number": str(index),
                "title": title,
                "detail": detail or "등록된 분석 계획에 따라 처리합니다.",
            }
        )
    if not steps:
        fast_contract = _dict(inspection.get("fast_path"))
        if fast_contract:
            recipe = _safe_text(fast_contract.get("recipe"), 100) or "고정 분석 규칙"
            steps.append({"number": "1", "title": "고정 분석 규칙 실행", "detail": f"{recipe} 계약으로 결과를 생성했습니다."})
    return steps


# 함수 설명: `_execution_code_projection()`은 실제 pandas 실행 trace에서 사용자에게
# 의미 있는 분석 본문 또는 deterministic 계약만 골라 제한·마스킹된 report 모델로 만듭니다.
# helper 원문, raw LLM 응답, traceback, 연결 설정은 이 projection에 포함하지 않습니다.
def _execution_code_projection(
    payload: dict[str, Any],
    inspection: dict[str, Any],
) -> dict[str, Any]:
    pandas_execution = _dict(inspection.get("pandas_execution"))
    analysis = _dict(payload.get("analysis"))
    status = _safe_text(
        pandas_execution.get("status") or analysis.get("status"),
        40,
    ).casefold()
    # 조회 전 차단, terminal response, unsafe guard 이전 실패에는 실제로 실행된
    # pandas 코드가 없으므로 code panel을 만들지 않습니다. Runtime에 진입한
    # 뒤 발생한 error는 명시적인 실행 증거가 있을 때만 진단용으로 허용합니다.
    if status not in {"ok", "partial", "error"}:
        return {}

    execution_mode = _safe_text(
        pandas_execution.get("execution_mode")
        or analysis.get("execution_mode"),
        120,
    ).casefold()
    deterministic_code = _execution_code_candidate(
        pandas_execution.get("deterministic_logic_code")
    )
    llm_code = _execution_code_candidate(pandas_execution.get("llm_generated_code"))

    kind = ""
    label = ""
    note = ""
    source_field = ""
    raw_code = ""
    if (
        deterministic_code
        and pandas_execution.get("deterministic_contract_started") is True
    ):
        deterministic_function = _dict(pandas_execution.get("deterministic_function"))
        recipe = _safe_text(deterministic_function.get("recipe"), 100)
        if execution_mode == "execute_fast_path_recipe" or recipe:
            kind = "fast_deterministic"
            label = "Fast 고정 실행 계약"
            note = "LLM 생성 코드가 아니라 등록된 Fast recipe와 고정 실행 함수 계약입니다."
        else:
            kind = "typed_deterministic"
            label = "Typed deterministic 실행 계약"
            note = "LLM 생성 코드가 아니라 Typed 실행 계획에서 만든 고정 실행 계약입니다."
        source_field = "deterministic_logic_code"
        raw_code = deterministic_code
    elif llm_code and pandas_execution.get("llm_code_executed") is True:
        kind = "llm_pandas"
        label = "LLM 생성 pandas 분석 코드"
        note = "실행에 사용된 pandas 분석 본문입니다. 자동 필터·검증·helper 내부 구현은 별도로 표시하지 않습니다."
        source_field = "llm_generated_code"
        raw_code = llm_code
    if not raw_code:
        return {}

    code, truncated, redacted, original_count, shown_count = _safe_execution_code_text(raw_code)
    if not code:
        return {}
    used_helpers = []
    for helper in _string_list(pandas_execution.get("used_helpers"))[:MAX_EXECUTION_CODE_HELPERS]:
        safe_helper = _safe_text(helper, 120)
        if safe_helper and safe_helper not in used_helpers:
            used_helpers.append(safe_helper)
    return {
        "available": True,
        "kind": kind,
        "label": label,
        "execution_status": (
            "partial"
            if status == "partial"
            else "error"
            if status == "error"
            else "executed"
        ),
        "language": "text" if kind == "typed_deterministic" else "python",
        "source_field": source_field,
        "code": code,
        "note": note,
        "used_helpers": used_helpers,
        "helpers_included": False,
        "omitted_sections": ["trusted helper definitions", "runtime instrumentation"],
        "truncated": truncated,
        "redacted": redacted,
        "original_char_count": original_count,
        "shown_char_count": shown_count,
    }


def _execution_code_candidate(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value if value and not value.isspace() else ""


# 함수 설명: 실제 실행 checkpoint와 source 결과에서 알려진 행 수만 인덱싱합니다.
# 계획만으로 임의의 행 수를 추론하지 않아, 보고서가 "실행 예정"과 "실제 실행"을
# 혼동하지 않도록 합니다.
def _execution_row_counts(
    payload: dict[str, Any],
    inspection: dict[str, Any],
    data: dict[str, Any],
    raw_steps: Any,
) -> dict[str, int]:
    counts: dict[str, int] = {}

    def record(key: Any, value: Any, *, replace: bool = True) -> None:
        count = _known_row_count(value)
        if count is None:
            return
        for normalized in _row_count_keys(key):
            if replace or normalized not in counts:
                counts[normalized] = count

    for source in _list(payload.get("source_results")):
        if not isinstance(source, dict):
            continue
        for key in (source.get("source_alias"), source.get("dataset_key")):
            record(key, source.get("row_count"))

    analysis = _dict(payload.get("analysis"))
    pandas_execution = _dict(inspection.get("pandas_execution"))
    checkpoint_lists = (
        analysis.get("step_outputs"),
        analysis.get("intermediate_results"),
        payload.get("intermediate_results"),
        pandas_execution.get("step_outputs"),
        pandas_execution.get("intermediate_results"),
        _dict(pandas_execution.get("semantic_execution_certificate")).get("intermediate_results"),
        _dict(analysis.get("semantic_execution_certificate")).get("intermediate_results"),
    )
    for records in checkpoint_lists:
        for checkpoint in _list(records):
            if not isinstance(checkpoint, dict):
                continue
            row_count = (
                checkpoint.get("output_row_count")
                if "output_row_count" in checkpoint
                else checkpoint.get("row_count")
            )
            for key in (
                checkpoint.get("key"),
                checkpoint.get("node_id"),
                checkpoint.get("output_alias"),
                checkpoint.get("source_alias"),
                checkpoint.get("alias"),
            ):
                record(key, row_count)

    # The final data frame is a valid actual output count.  Only use it as a
    # fallback for the terminal plan output so that checkpoint evidence always
    # takes precedence when it is available.
    if isinstance(raw_steps, list) and raw_steps:
        final_step = next((item for item in reversed(raw_steps) if isinstance(item, dict)), {})
        for key in _step_output_keys(final_step):
            record(key, data.get("row_count"), replace=False)
    return counts


# 함수 설명: checkpoint naming prefixes와 node/output alias를 같은 실행 단계로 인식합니다.
def _row_count_keys(value: Any) -> list[str]:
    text = _lookup_key(value)
    if not text:
        return []
    result = [text]
    for prefix in ("source:", "filtered:", "typed_step:", "last_available:", "step:"):
        if text.startswith(prefix):
            stripped = text[len(prefix) :].strip()
            if stripped and stripped not in result:
                result.append(stripped)
    return result


# 함수 설명: 숫자로 확정된 행 수만 report에 표시합니다. 미기록 값은 0으로 바꾸지 않습니다.
def _known_row_count(value: Any) -> int | None:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return max(resolved, 0)


# 함수 설명: 처리 단계의 upstream alias를 계획 입력과 명시 source alias에서 모읍니다.
def _step_input_keys(step: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        step.get("source_alias"),
        step.get("left_source_alias"),
        step.get("right_source_alias"),
        step.get("input_alias"),
    ]
    for item in _list(step.get("inputs")):
        if isinstance(item, dict):
            values.append(item.get("ref"))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_text(value, 180)
        key = _lookup_key(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


# 함수 설명: output alias가 없는 plan도 node_id checkpoint와 연결할 수 있도록 후보를 만듭니다.
def _step_output_keys(step: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in (step.get("output_alias"), step.get("node_id")):
        text = _safe_text(value, 180)
        key = _lookup_key(text)
        if text and key and key not in seen:
            seen.add(key)
            result.append(text)
    return result


# 함수 설명: report에 가능한 실제 행 수만 짧게 덧붙입니다.
def _step_row_count_text(step: dict[str, Any], row_counts: dict[str, int]) -> str:
    inputs = [
        (alias, row_counts.get(_lookup_key(alias)))
        for alias in _step_input_keys(step)
    ]
    inputs = [(alias, count) for alias, count in inputs if count is not None]
    output_count = next(
        (row_counts.get(_lookup_key(alias)) for alias in _step_output_keys(step) if _lookup_key(alias) in row_counts),
        None,
    )
    if len(inputs) == 1 and output_count is not None:
        return f"행 수: {inputs[0][1]:,}건 → {output_count:,}건"
    if len(inputs) > 1 and output_count is not None:
        source_text = ", ".join(f"{alias} {count:,}건" for alias, count in inputs[:3])
        return f"입력 행 수: {source_text} · 출력: {output_count:,}건"
    if output_count is not None:
        return f"출력 행 수: {output_count:,}건"
    return ""


# 함수 설명: 부분 실행/오류 시 마지막 확인 가능한 checkpoint를 raw row 없이 요약합니다.
def _last_successful_step(
    payload: dict[str, Any],
    analysis: dict[str, Any],
    inspection: dict[str, Any],
) -> dict[str, Any]:
    recovered = _dict(analysis.get("recovered_result"))
    if recovered.get("available"):
        return {
            "key": _safe_text(recovered.get("checkpoint_key"), 180),
            "role": _safe_text(recovered.get("checkpoint_role"), 120),
            "row_count": _known_row_count(recovered.get("row_count")),
            "description": "오류 직전 마지막으로 정상 확인된 결과",
        }
    pandas_execution = _dict(inspection.get("pandas_execution"))
    checkpoints: list[dict[str, Any]] = []
    for records in (
        payload.get("intermediate_results"),
        analysis.get("intermediate_results"),
        pandas_execution.get("intermediate_results"),
    ):
        checkpoints.extend(item for item in _list(records) if isinstance(item, dict))
    for item in reversed(checkpoints):
        key = _safe_text(item.get("key") or item.get("output_alias") or item.get("node_id"), 180)
        if not key:
            continue
        return {
            "key": key,
            "role": _safe_text(item.get("role"), 120),
            "row_count": _known_row_count(item.get("row_count")),
            "description": _safe_text(item.get("description"), 240),
        }
    return {}


# 함수 설명: `_issue_items()`는 safe message가 있는 오류·경고·부분 결과 복구 상태를 요약합니다.
def _issue_items(
    payload: dict[str, Any],
    analysis: dict[str, Any],
    inspection: dict[str, Any],
    execution_gate: dict[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(stage: str, severity: str, value: Any) -> None:
        message = _safe_error_message(value)
        if not message:
            return
        signature = f"{stage}|{severity}|{message}".casefold()
        if signature in seen:
            return
        seen.add(signature)
        issues.append({"stage": stage, "severity": severity, "message": message})

    def add_all(stage: str, severity: str, value: Any) -> None:
        for message in _error_messages(value):
            add(stage, severity, message)
            if len(issues) >= MAX_ISSUES:
                return

    gate_status = _safe_text(execution_gate.get("status"), 80).lower()
    if gate_status == "blocked":
        # 14A records its authoritative details under critical_failures rather
        # than a single reason field.  Surface those first so a Blocked report
        # explains why execution never started.
        add_all(
            "데이터 조회 시작 전",
            "error",
            execution_gate.get("critical_failures")
            or execution_gate.get("reason")
            or execution_gate.get("errors"),
        )
    retrieval_gate = _dict(inspection.get("retrieval_execution_gate"))
    if _safe_text(retrieval_gate.get("status"), 80).lower() == "blocked":
        add_all(
            "데이터 조회 시작 전",
            "error",
            retrieval_gate.get("critical_failures")
            or retrieval_gate.get("reason")
            or retrieval_gate.get("errors"),
        )

    # Planning/metadata blocks often have a short, user-safe rationale that is
    # more actionable than the generic retrieval gate error.  It is already
    # normalized by the intent plan and is included only for blocked paths.
    if gate_status == "blocked" or _safe_text(analysis.get("status"), 80).lower() == "blocked":
        plan = _dict(payload.get("intent_plan"))
        answer_sections = _dict(payload.get("answer_sections"))
        confirmation = _dict(answer_sections.get("confirmation_required"))
        for reason in _safe_reason_list(confirmation.get("items")) + _safe_reason_list(plan.get("decision_reason")):
            add("확인 필요", "warning", reason)
            if len(issues) >= MAX_ISSUES:
                return issues[:MAX_ISSUES]

    add_all("분석 처리", "error", analysis.get("error"))
    pandas_execution = _dict(inspection.get("pandas_execution"))
    if _safe_text(pandas_execution.get("status"), 40).lower() in {"error", "failed"}:
        add_all("데이터 처리", "error", pandas_execution.get("error") or pandas_execution.get("errors"))
    if _is_partial_result(analysis, _dict(payload.get("data"))):
        last_step = _last_successful_step(payload, analysis, inspection)
        step_name = _safe_text(last_step.get("key"), 180)
        step_rows = _known_row_count(last_step.get("row_count"))
        last_step_text = ""
        if step_name:
            last_step_text = f" 마지막 정상 단계는 {step_name}"
            if step_rows is not None:
                last_step_text += f" ({step_rows:,}건)"
            last_step_text += "입니다."
        issues.append(
            {
                "stage": "결과 생성",
                "severity": "warning",
                "message": "일부 단계에서 오류가 있었지만 직전 정상 단계의 결과를 함께 제공했습니다." + last_step_text,
            }
        )
    for warning in _list(payload.get("warnings")) + _list(_dict(payload.get("trace")).get("warnings")):
        if isinstance(warning, dict) and warning.get("user_visible") is False:
            continue
        add("처리 참고", "warning", warning)
        if len(issues) >= MAX_ISSUES:
            break
    return issues[:MAX_ISSUES]


# 함수 설명: `_report_request_body()`는 API_SERVER ReportCreateRequest의 허용 필드만 구성합니다.
def _report_request_body(explanation: dict[str, Any], html_document: str, ttl_hours: int) -> dict[str, Any]:
    retrievals = explanation.get("retrievals") if isinstance(explanation.get("retrievals"), list) else []
    available_datasets = [
        {
            "dataset_key": item.get("dataset", ""),
            "source_alias": item.get("alias", ""),
            "source_type": item.get("source_type", ""),
            "row_count": _safe_int(item.get("row_count"), 0),
            "columns": item.get("columns", []),
        }
        for item in retrievals
        if isinstance(item, dict)
    ]
    summary = _dict(explanation.get("summary"))
    trace_token = re.sub(r"[^a-zA-Z0-9_-]", "", _safe_text(explanation.get("trace_id"), 80)) or "trace"
    return {
        "html": html_document,
        "title": "분석 처리 과정",
        "question": _safe_text(explanation.get("question"), 4_000),
        "view_request": "metadata_driven_v5 data analysis execution explanation",
        "available_datasets": available_datasets,
        "report_plan": {
            "contract_version": EXPLANATION_VERSION,
            "status": summary.get("status", ""),
            "route": summary.get("route", ""),
            "analysis_type": summary.get("analysis_type", ""),
            "metadata_refs": explanation.get("domains", []),
            "processing_steps": explanation.get("processing_steps", []),
        },
        "ttl_hours": ttl_hours,
        "filename_hint": f"analysis-execution-{trace_token}.html",
    }


# 함수 설명: `_artifact_descriptor()`는 API_SERVER 응답을 하위 답변·API 노드가 쓰는 최소 artifact 계약으로 정리합니다.
def _artifact_descriptor(response: Any, fallback_ttl: int) -> dict[str, Any]:
    payload = _dict(response)
    storage = _dict(payload.get("storage"))
    descriptor = {
        "type": DESCRIPTOR_TYPE,
        "artifact_type": ARTIFACT_TYPE,
        "status": "published",
        "title": "분석 처리 과정",
        "label": "분석 처리 과정 HTML",
        "mime_type": "text/html",
        "report_id": _safe_text(payload.get("report_id"), 160),
        "view_url": _safe_public_url(payload.get("view_url")),
        "download_url": _safe_public_url(payload.get("download_url")),
        "expires_at": _safe_text(payload.get("expires_at"), 120),
        "ttl_hours": _bounded_int(payload.get("ttl_hours"), fallback_ttl, 1, MAX_TTL_HOURS),
        "storage_backend": _safe_text(storage.get("backend"), 80) or "mongodb_collection",
    }
    return {key: value for key, value in descriptor.items() if value not in (None, "")}


# 함수 설명: `_post_report_json()`은 API_SERVER 요청을 표준 라이브러리 HTTP로 전송합니다.
def _post_report_json(url: str, body: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - validated HTTP(S) URL.
            raw = response.read(128 * 1024)
    except HTTPError as exc:
        raise RuntimeError(f"Report API HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Report API 연결에 실패했습니다.") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Report API 응답 형식을 확인할 수 없습니다.") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Report API 응답이 객체 형식이 아닙니다.")
    return decoded


# 함수 설명: `_record_artifact_failure()`는 내부 경고만 남겨 기존 분석 성공·오류 계약을 보존합니다.
def _record_artifact_failure(payload: dict[str, Any], issue_type: str, message: str, detail: str = "") -> dict[str, Any]:
    warning = {
        "type": issue_type,
        "message": _safe_text(message, MAX_TEXT_LENGTH),
        "user_visible": False,
    }
    if detail:
        warning["detail"] = _safe_text(detail, MAX_TEXT_LENGTH)
    trace = _dict(payload.get("trace"))
    payload["trace"] = trace
    warnings = trace.get("warnings") if isinstance(trace.get("warnings"), list) else []
    if not any(isinstance(item, dict) and item.get("type") == issue_type for item in warnings):
        warnings.append(warning)
    trace["warnings"] = warnings
    _inspection(payload)["execution_trace_artifact"] = {
        "stage": "25_execution_trace_artifact_publisher",
        "status": "warning",
        "reason": issue_type,
        "user_visible": False,
    }
    return payload


# 함수 설명: `_record_artifact_status()`는 disabled·skipped 상태를 trace inspection에만 기록합니다.
def _record_artifact_status(payload: dict[str, Any], status: str, reason: str = "") -> dict[str, Any]:
    _inspection(payload)["execution_trace_artifact"] = {
        "stage": "25_execution_trace_artifact_publisher",
        "status": status,
        "reason": reason,
        "user_visible": False,
    }
    return payload


# 함수 설명: `_timeline_item()`은 timeline의 공통 카드 HTML을 렌더링합니다.
def _timeline_item(number: str, title: str, subtitle: str, body: str, _kind: str) -> str:
    return f"""<article class="timeline-item">
  <span class="timeline-dot">{escape(number)}</span>
  <div class="timeline-card">
    <div class="timeline-head"><h3>{escape(title)}</h3><p>{escape(subtitle)}</p></div>
    <div class="timeline-body">{body}</div>
  </div>
</article>"""


def _domains_html(value: Any, reasons_value: Any = None) -> str:
    domains = value if isinstance(value, list) else []
    reasons = _safe_reason_list(reasons_value)
    if not domains and not reasons:
        return '<p class="empty">선택된 도메인 정보가 기록되지 않았습니다.</p>'
    cards = []
    for item in domains:
        if isinstance(item, dict):
            category = _html_text(item.get("category"), 100) or "도메인 정보"
            key = _html_text(item.get("key"), 180) or "등록 정보"
            title = _html_text(item.get("title"), 180) or key
            summary = _html_text(item.get("summary"), MAX_TEXT_LENGTH)
            details = _domain_detail_html(item.get("details"))
            cards.append(f'''<details class="domain-card">
  <summary>
    <span class="domain-card-main"><span class="domain-category">{category}</span><strong>{title}</strong><span class="domain-key">{key}</span></span>
    <span class="domain-disclosure">세부 정보 보기</span>
  </summary>
  <div class="domain-card-body">
    {f'<p class="domain-summary">{summary}</p>' if summary else ''}
    {details}
  </div>
</details>''')
    domain_html = '<div class="domain-list">' + "".join(cards) + "</div>" if cards else '<p class="empty">표시할 도메인 정보가 없습니다.</p>'
    if not reasons:
        return domain_html
    reason_items = "".join(f"<li>{_html_text(reason, MAX_TEXT_LENGTH)}</li>" for reason in reasons)
    return domain_html + f'<div class="reason-list"><strong>적용 근거</strong><ul>{reason_items}</ul></div>'


def _domain_detail_html(value: Any, depth: int = 0) -> str:
    if depth > 4:
        return '<span class="domain-value">…</span>'
    if isinstance(value, dict):
        entries = []
        for key, item in value.items():
            label = _html_text(key, 120)
            if not label:
                continue
            rendered = _domain_detail_html(item, depth + 1)
            entries.append(f"<dt>{label}</dt><dd>{rendered}</dd>")
        return '<dl class="domain-detail-grid">' + "".join(entries) + "</dl>" if entries else '<p class="empty">표시 가능한 세부 정의가 없습니다.</p>'
    if isinstance(value, list):
        items = [
            f"<li>{_domain_detail_html(item, depth + 1)}</li>"
            for item in value
        ]
        return '<ul class="domain-detail-list">' + "".join(items) + "</ul>" if items else '<span class="domain-value">없음</span>'
    return f'<span class="domain-value">{_html_text(value, MAX_TEXT_LENGTH) or "-"}</span>'


def _intent_analysis_html(value: Any) -> str:
    """Render the answer adapter's intent facts as a closed HTML5 disclosure."""

    intent = _dict(value)
    if not intent:
        return ""
    retrieval_count = _safe_int(intent.get("retrieval_job_count"), 0)
    pandas_count = _safe_int(intent.get("pandas_step_count"), 0)
    analysis_type = _html_text(intent.get("analysis_type"), 160) or "분석 계획"
    expected_route = _html_text(intent.get("expected_route"), 100)
    final_route = _html_text(intent.get("final_route"), 100)
    route_label = final_route or expected_route
    route_detail = " → ".join(item for item in (expected_route, final_route) if item)
    summary_cards = []
    for label, raw_value, card_class in (
        ("분석 유형", intent.get("analysis_type"), "primary"),
        ("실행 경로", route_detail, "route"),
        ("실행 규모", f"조회 {retrieval_count}개 · pandas {pandas_count}단계", "scope"),
        ("Fast 레시피", intent.get("recipe"), "recipe"),
    ):
        text = _html_text(raw_value, 200)
        if text:
            summary_cards.append(
                f'<article class="intent-overview-card {card_class}"><span>{escape(label)}</span><strong>{text}</strong></article>'
            )

    sections = []
    reasons = _safe_reason_list(intent.get("decision_reasons"))
    if reasons:
        items = "".join(f"<li>{_html_text(reason, MAX_TEXT_LENGTH)}</li>" for reason in reasons)
        sections.append(f'<section class="intent-section"><h4>의도 판단 근거</h4><ol class="intent-reason-list">{items}</ol></section>')

    route_reasons = _string_list(intent.get("route_reasons"))
    if route_reasons:
        items = "".join(
            f'<span class="intent-meta-chip"><b>경로</b>{_html_text(reason, 160)}</span>'
            for reason in route_reasons
        )
        sections.append(f'<section class="intent-section"><h4>경로 결정 근거</h4><div class="intent-meta-list">{items}</div></section>')

    refs = intent.get("metadata_refs") if isinstance(intent.get("metadata_refs"), list) else []
    if refs:
        items = "".join(
            f'<span class="intent-meta-chip"><b>{_html_text(_dict(item).get("label"), 100)}</b>{_html_text(_dict(item).get("key"), 180)}</span>'
            for item in refs
            if isinstance(item, dict)
        )
        if items:
            sections.append(f'<section class="intent-section"><h4>참조 메타데이터</h4><div class="intent-meta-list">{items}</div></section>')

    retrieval_plan = intent.get("retrieval_plan") if isinstance(intent.get("retrieval_plan"), list) else []
    if retrieval_plan:
        items = "".join(
            _intent_retrieval_plan_html(_dict(item), index)
            for index, item in enumerate(retrieval_plan, start=1)
            if isinstance(item, dict)
        )
        if items:
            sections.append(f'<section class="intent-section"><h4>조회 계획</h4><ol class="intent-plan-list">{items}</ol></section>')

    pandas_plan = intent.get("pandas_plan") if isinstance(intent.get("pandas_plan"), list) else []
    if pandas_plan:
        items = "".join(
            _intent_pandas_plan_html(_dict(item), index)
            for index, item in enumerate(pandas_plan, start=1)
            if isinstance(item, dict)
        )
        if items:
            sections.append(f'<section class="intent-section"><h4>pandas 실행 계획</h4><ol class="intent-plan-list">{items}</ol></section>')

    route_pill = f'<span class="intent-route-pill">{route_label}</span>' if route_label else ""
    return f'''<details class="intent-analysis-panel">
  <summary>
    <span class="intent-summary-icon" aria-hidden="true"><i></i><i></i><i></i></span>
    <span class="intent-summary-main">
      <span class="intent-summary-eyebrow">ANALYSIS PLAN</span>
      <span class="intent-summary-title-row"><strong>의도 분석</strong>{route_pill}</span>
      <span class="intent-summary-subtitle">{analysis_type}</span>
    </span>
    <span class="intent-summary-stat"><b>조회 {retrieval_count}개</b><span>pandas {pandas_count}단계</span></span>
    <span class="intent-disclosure"><span>세부 계획</span></span>
  </summary>
  <div class="intent-analysis-body">
    {f'<div class="intent-overview-grid">{"".join(summary_cards)}</div>' if summary_cards else ''}
    {"".join(sections) if sections else '<p class="empty">표시할 의도 분석 정보가 없습니다.</p>'}
  </div>
</details>'''


def _intent_retrieval_plan_html(item: dict[str, Any], index: int) -> str:
    dataset = _safe_text(item.get("dataset"), 180) or "데이터셋"
    alias = _safe_text(item.get("alias"), 180)
    source_type = _safe_text(item.get("source_type"), 80)
    params = _mapping_text(item.get("required_params")) or "없음"
    filters = _mapping_text(item.get("filters")) or "없음"
    params_label = _safe_text(item.get("params_label"), 80) or "계획 파라미터"
    filters_label = _safe_text(item.get("filters_label"), 80) or "계획 조건"
    labels = [f"별칭: {alias}" if alias else "", source_type]
    labels = [label for label in labels if label]
    details = [f"{params_label}: {params}", f"{filters_label}: {filters}"]
    return f'''<li class="intent-plan-item">
  <div class="intent-plan-item-head"><strong>{index}. {_html_text(dataset, 180)}</strong><span>{_html_text(' · '.join(labels), 300)}</span></div>
  <p class="intent-plan-detail">{_html_text(' · '.join(details), 900)}</p>
</li>'''


def _intent_pandas_plan_html(item: dict[str, Any], index: int) -> str:
    operation = _safe_text(item.get("operation_label"), 160) or "처리 단계"
    node_id = _safe_text(item.get("node_id"), 180)
    source = _safe_text(item.get("source"), 180)
    output = _safe_text(item.get("output"), 180)
    group_by = ", ".join(_safe_text(column, 100) for column in _string_list(item.get("group_by")))
    aggregations = _safe_value_text(item.get("aggregations"), 500)
    formula = _mapping_text(item.get("formula"))
    sort_by = _safe_text(item.get("sort_by"), 120)
    order = _safe_text(item.get("order"), 40)
    detail_parts = []
    if source:
        detail_parts.append(f"입력: {source}")
    if output:
        detail_parts.append(f"출력: {output}")
    if group_by:
        detail_parts.append(f"기준: {group_by}")
    if aggregations:
        detail_parts.append(f"집계: {aggregations}")
    if formula:
        detail_parts.append(f"계산: {formula}")
    if sort_by:
        detail_parts.append(f"정렬: {sort_by}{(' ' + order) if order else ''}")
    if item.get("limit"):
        detail_parts.append(f"표시 상한: {_format_number(item.get('limit'))}")
    title_suffix = f" · {node_id}" if node_id else ""
    return f'''<li class="intent-plan-item">
  <div class="intent-plan-item-head"><strong>{index}. {_html_text(operation, 160)}</strong><span>{_html_text(title_suffix, 220)}</span></div>
  <p class="intent-plan-detail">{_html_text(' · '.join(detail_parts) or '계획 상세 정보가 없습니다.', 1_500)}</p>
</li>'''


def _retrievals_html(value: Any) -> str:
    retrievals = value if isinstance(value, list) else []
    if not retrievals:
        return '<p class="empty">실행된 데이터 조회가 없거나, 조회 전에 처리 과정이 중단되었습니다.</p>'
    cards = []
    for item in retrievals:
        if not isinstance(item, dict):
            continue
        status = _safe_text(item.get("status"), 40).lower() or "planned"
        badge_class = "error" if status in {"error", "failed", "blocked"} else "pending" if status in {"planned", "pending", "skipped"} else ""
        params = _mapping_text(item.get("required_params")) or "없음"
        filters = _mapping_text(item.get("filters")) or "없음"
        params_label = "실제 적용 파라미터" if item.get("params_origin") == "actual" else "계획 파라미터"
        filters_label = "실제 적용 조건" if item.get("filters_origin") == "actual" else "계획 조건"
        columns = ", ".join(_html_text(column, 100) for column in _string_list(item.get("columns"))) or "확인되지 않음"
        error = _html_text(item.get("error"), MAX_TEXT_LENGTH)
        cards.append(f"""<article class="mini-card">
  <div class="mini-head"><p class="mini-title">{_html_text(item.get('dataset'), 180)}</p><span class="badge {badge_class}">{_retrieval_status_label(status)}</span></div>
  <dl class="kv-grid">
    <dt>소스 별칭</dt><dd>{_html_text(item.get('alias'), 180) or '-'}</dd>
    <dt>{params_label}</dt><dd>{escape(params)}</dd>
    <dt>{filters_label}</dt><dd>{escape(filters)}</dd>
    <dt>조회 행 수</dt><dd>{_format_number(item.get('row_count'))}건</dd>
    <dt>확인 컬럼</dt><dd>{columns}</dd>
    {f'<dt>확인 필요</dt><dd>{error}</dd>' if error else ''}
  </dl>
</article>""")
    return "".join(cards) if cards else '<p class="empty">표시할 데이터 조회 정보가 없습니다.</p>'


def _processing_html(value: Any, execution_code_value: Any = None) -> str:
    steps = value if isinstance(value, list) else []
    items = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        items.append(f"""<article class="step">
  <span class="step-number">{index:02d}</span>
  <div><h4>{_html_text(step.get('title'), 180)}</h4><p>{_html_text(step.get('detail'), 900)}</p></div>
</article>""")
    step_html = (
        '<div class="step-list">' + "".join(items) + "</div>"
        if items
        else '<p class="empty">처리 계획이 확정되기 전에 분석이 중단되었거나, 별도 처리 단계가 필요하지 않았습니다.</p>'
    )
    return step_html + _execution_code_html(execution_code_value)


def _execution_code_html(value: Any) -> str:
    code_info = _dict(value)
    code_text = str(code_info.get("code") or "")
    if not code_info.get("available") or not code_text.strip():
        return ""
    label = _html_text(code_info.get("label"), 180) or "pandas 실행 코드"
    kind = _safe_text(code_info.get("kind"), 80).casefold()
    disclosure = "실행 계약 펼쳐서 보기" if kind in {"fast_deterministic", "typed_deterministic"} else "실행 코드 펼쳐서 보기"
    note = _html_text(code_info.get("note"), 500)
    status = _safe_text(code_info.get("execution_status"), 40).casefold()
    status_label = (
        "부분 실행"
        if status == "partial"
        else "실행 중 오류"
        if status == "error"
        else "실행됨"
    )
    status_class = "pending" if status == "partial" else "error" if status == "error" else ""
    helpers = _string_list(code_info.get("used_helpers"))[:MAX_EXECUTION_CODE_HELPERS]
    helper_text = ", ".join(_html_text(helper, 120) for helper in helpers)
    badges = [f'<span class="badge {status_class}">{status_label}</span>']
    if code_info.get("redacted"):
        badges.append('<span class="badge pending">민감정보 마스킹</span>')
    if code_info.get("truncated"):
        badges.append('<span class="badge pending">일부 생략</span>')
    metadata = ""
    if helper_text:
        metadata = f'<p class="code-helper-note"><strong>사용 helper</strong> {helper_text} <span>· 내부 구현은 제외</span></p>'
    language = _safe_text(code_info.get("language"), 40).casefold()
    escaped_code = (
        _highlight_python_code_html(code_text)
        if language == "python"
        else escape(code_text, quote=False)
    )
    return f'''<details class="code-panel">
  <summary>
    <span class="code-summary-main"><strong>{label}</strong><span>{disclosure}</span></span>
    <span class="code-badges">{"".join(badges)}</span>
  </summary>
  <div class="code-panel-body">
    {f'<p class="code-note">{note}</p>' if note else ''}
    {metadata}
    <pre class="execution-code"><code>{escaped_code}</code></pre>
  </div>
</details>'''


# 함수 설명: 마스킹이 끝난 Python 코드만 표준 tokenizer로 분리해 정적 span을
# 생성합니다. 모든 원문 조각은 HTML escape하며, 불완전 코드면 plain text로
# 되돌아가므로 syntax highlight가 보고서 생성 실패 원인이 되지 않습니다.
def _highlight_python_code_html(value: Any) -> str:
    source = str(value or "")
    if not source:
        return ""
    try:
        tokens = list(tokenize.generate_tokens(StringIO(source).readline))
    except (
        tokenize.TokenError,
        IndentationError,
        SyntaxError,
        UnicodeError,
        ValueError,
    ):
        return escape(source, quote=False)
    if len(tokens) > MAX_HIGHLIGHTED_CODE_TOKENS:
        return escape(source, quote=False)

    line_offsets = [0]
    for line in source.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))

    def absolute_position(position: tuple[int, int]) -> int:
        row, column = position
        if row <= 0:
            return 0
        if row > len(line_offsets):
            return len(source)
        line_start = line_offsets[min(row - 1, len(line_offsets) - 1)]
        return min(len(source), line_start + max(0, column))

    ignored_types = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
    next_significant: list[tokenize.TokenInfo | None] = [None] * len(tokens)
    next_token: tokenize.TokenInfo | None = None
    for index in range(len(tokens) - 1, -1, -1):
        next_significant[index] = next_token
        candidate = tokens[index]
        if candidate.type not in ignored_types and candidate.type != tokenize.COMMENT:
            next_token = candidate

    pieces: list[str] = []
    cursor = 0
    previous_significant: tokenize.TokenInfo | None = None
    for index, token_info in enumerate(tokens):
        if token_info.type in {tokenize.ENCODING, tokenize.ENDMARKER}:
            continue
        start = max(cursor, absolute_position(token_info.start))
        end = max(start, absolute_position(token_info.end))
        if start > cursor:
            pieces.append(escape(source[cursor:start], quote=False))
        fragment = source[start:end]
        css_class = _python_token_css_class(
            token_info,
            previous_significant,
            next_significant[index],
        )
        escaped_fragment = escape(fragment, quote=False)
        pieces.append(
            f'<span class="{css_class}">{escaped_fragment}</span>'
            if css_class and fragment
            else escaped_fragment
        )
        cursor = end
        if token_info.type not in ignored_types and token_info.type != tokenize.COMMENT:
            previous_significant = token_info
    if cursor < len(source):
        pieces.append(escape(source[cursor:], quote=False))
    highlighted = "".join(pieces)
    if len(highlighted) > MAX_HIGHLIGHTED_CODE_HTML_CHARACTERS:
        return escape(source, quote=False)
    return highlighted


def _python_token_css_class(
    token_info: tokenize.TokenInfo,
    previous: tokenize.TokenInfo | None,
    following: tokenize.TokenInfo | None,
) -> str:
    token_type = token_info.type
    text = token_info.string
    if token_type == tokenize.COMMENT:
        return "syntax-comment"
    if token_type == tokenize.STRING:
        return "syntax-string"
    fstring_types = {
        getattr(tokenize, "FSTRING_START", -1),
        getattr(tokenize, "FSTRING_MIDDLE", -1),
        getattr(tokenize, "FSTRING_END", -1),
    }
    if token_type in fstring_types:
        return "syntax-string"
    if token_type == tokenize.NUMBER:
        return "syntax-number"
    if token_type == tokenize.OP:
        return "syntax-operator"
    if token_type != tokenize.NAME:
        return ""
    if keyword.iskeyword(text):
        return "syntax-keyword"
    previous_text = previous.string if previous is not None else ""
    following_text = following.string if following is not None else ""
    if previous_text == "def":
        return "syntax-function"
    if previous_text == "class":
        return "syntax-class"
    if previous_text == "@":
        return "syntax-decorator"
    if text in PYTHON_BUILTIN_NAMES:
        return "syntax-builtin"
    if following_text == "(":
        return "syntax-function"
    if previous_text == ".":
        return "syntax-attribute"
    return "syntax-name"


def _result_html(value: Any) -> str:
    result = _dict(value)
    row_count = _format_number(result.get("row_count"))
    columns = ", ".join(_html_text(column, 100) for column in _string_list(result.get("columns"))) or "결과 컬럼이 기록되지 않았습니다."
    mode = _html_text(result.get("mode"), 100) or "일반 결과"
    partial = "예 — 직전 정상 단계의 결과를 함께 제공합니다." if result.get("partial") else "아니오 — 최종 결과 기준입니다."
    last_step = _dict(result.get("last_successful_step"))
    last_step_key = _html_text(last_step.get("key"), 180)
    last_step_count = _known_row_count(last_step.get("row_count"))
    last_step_description = _html_text(last_step.get("description"), 240)
    last_step_text = ""
    if last_step_key:
        last_step_text = last_step_key
        if last_step_count is not None:
            last_step_text += f" · {last_step_count:,}건"
        if last_step_description:
            last_step_text += f" · {last_step_description}"
    return f"""<dl class="kv-grid">
  <dt>결과 유형</dt><dd>{mode}</dd>
  <dt>결과 행 수</dt><dd>{row_count}건</dd>
  <dt>최종 표시 컬럼</dt><dd>{columns}</dd>
  <dt>부분 결과 사용</dt><dd>{partial}</dd>
  {f'<dt>마지막 정상 단계</dt><dd>{last_step_text}</dd>' if last_step_text else ''}
</dl>"""


# 함수 설명: `_execution_data_tables()`는 Node 24의 제한된 snapshot을 HTML 전용 표 모델로 한 번 더 정리합니다.
# 이 경계에서 행·컬럼·셀 상한과 민감 컬럼 제외를 재적용하므로, 오래된 Flow 또는 수동 입력도 답변 payload로
# 대용량 데이터나 인증 정보가 새어 나가지 않습니다.
def _execution_data_tables(
    value: Any,
    payload: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    raw_preview = _dict(value)
    remaining_cells = [MAX_PREVIEW_CELLS]
    groups = {
        "original": _normalized_preview_tables(
            raw_preview.get("original"),
            group="original",
            max_tables=MAX_PREVIEW_SOURCE_TABLES,
            max_rows=MAX_PREVIEW_SOURCE_ROWS,
            remaining_cells=remaining_cells,
        ),
        "intermediate": _normalized_preview_tables(
            raw_preview.get("intermediate"),
            group="intermediate",
            max_tables=MAX_PREVIEW_INTERMEDIATE_TABLES,
            max_rows=MAX_PREVIEW_INTERMEDIATE_ROWS,
            remaining_cells=remaining_cells,
        ),
        "final": _normalized_preview_tables(
            raw_preview.get("final"),
            group="final",
            max_tables=MAX_PREVIEW_FINAL_TABLES,
            max_rows=MAX_PREVIEW_FINAL_ROWS,
            remaining_cells=remaining_cells,
        ),
    }

    # Older imported Flows do not yet have Node 24's sidecar.  They can still
    # show the already-bounded final result and public intermediate previews,
    # but intentionally never inspect runtime source buffers here.
    if not groups["intermediate"]:
        fallback_intermediate = _public_intermediate_preview_tables(payload)
        groups["intermediate"] = _normalized_preview_tables(
            fallback_intermediate,
            group="intermediate",
            max_tables=MAX_PREVIEW_INTERMEDIATE_TABLES,
            max_rows=MAX_PREVIEW_INTERMEDIATE_ROWS,
            remaining_cells=remaining_cells,
        )
    if not groups["final"] and (
        isinstance(data.get("rows"), list)
        or data.get("row_count") is not None
        or isinstance(data.get("columns"), list)
    ):
        groups["final"] = _normalized_preview_tables(
            [
                {
                    "key": "final_result",
                    "title": "최종 결과 데이터",
                    "description": "최종 결과 계약을 적용한 결과",
                    "row_count": data.get("row_count"),
                    "columns": data.get("columns"),
                    "rows": data.get("rows"),
                    "download": _data_ref_download(payload.get("data_refs"), "analysis_result"),
                }
            ],
            group="final",
            max_tables=MAX_PREVIEW_FINAL_TABLES,
            max_rows=MAX_PREVIEW_FINAL_ROWS,
            remaining_cells=remaining_cells,
        )
    return groups


def _public_intermediate_preview_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = payload.get("intermediate_results")
    if not isinstance(candidates, list):
        analysis = _dict(payload.get("analysis"))
        candidates = analysis.get("intermediate_results")
    refs = payload.get("data_refs")
    tables: list[dict[str, Any]] = []
    for index, item in enumerate(_list(candidates)[:MAX_PREVIEW_INTERMEDIATE_TABLES], start=1):
        if not isinstance(item, dict):
            continue
        rows = item.get("preview_rows") if isinstance(item.get("preview_rows"), list) else item.get("rows")
        if not isinstance(rows, list) and not item.get("columns"):
            continue
        checkpoint_key = _safe_text(
            item.get("checkpoint_key") or item.get("download_key") or item.get("key") or item.get("node_id"),
            180,
        )
        tables.append(
            {
                "key": checkpoint_key or f"intermediate:{index}",
                "title": item.get("label") or item.get("title") or "중간 결과",
                "description": item.get("description") or "최종 결과를 만들기 전 선택된 실행 단계",
                "row_count": item.get("row_count") or item.get("output_row_count"),
                "columns": item.get("columns"),
                "rows": rows or [],
                "download": _data_ref_download(refs, "intermediate_result", checkpoint_key=checkpoint_key),
            }
        )
    return tables


def _normalized_preview_tables(
    value: Any,
    *,
    group: str,
    max_tables: int,
    max_rows: int,
    remaining_cells: list[int],
) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for index, item in enumerate(_list(value)[:max_tables], start=1):
        if not isinstance(item, dict):
            continue
        table = _normalized_preview_table(
            item,
            group=group,
            index=index,
            max_rows=max_rows,
            remaining_cells=remaining_cells,
        )
        if table:
            tables.append(table)
    return tables


def _normalized_preview_table(
    item: dict[str, Any],
    *,
    group: str,
    index: int,
    max_rows: int,
    remaining_cells: list[int],
) -> dict[str, Any]:
    raw_rows = item.get("rows") if isinstance(item.get("rows"), list) else []
    rows = [row if isinstance(row, dict) else {"value": row} for row in raw_rows]
    declared_columns = item.get("columns") if isinstance(item.get("columns"), list) else []
    columns, columns_truncated = _preview_columns(rows, declared_columns)
    if columns:
        allowed_rows = min(max_rows, len(rows), max(remaining_cells[0] // len(columns), 0))
    else:
        allowed_rows = 0
    visible_rows: list[dict[str, str]] = []
    for row in rows[:allowed_rows]:
        visible_rows.append({column: _safe_preview_cell(_preview_row_value(row, column)) for column in columns})
    remaining_cells[0] = max(remaining_cells[0] - (len(visible_rows) * len(columns)), 0)

    row_count = _safe_int(item.get("row_count"), len(rows))
    row_count = max(row_count, len(rows), 0)
    title_default = {
        "original": f"사용 원본 데이터 {index}",
        "intermediate": f"중간 결과 {index}",
        "final": "최종 결과 데이터",
    }.get(group, "데이터")
    download = _preview_download(item.get("download") or item)
    return {
        "key": _safe_text(item.get("key"), 180) or f"{group}:{index}",
        "title": _safe_text(item.get("title"), 240) or title_default,
        "description": _safe_text(item.get("description"), 300),
        "row_count": row_count,
        "shown_row_count": len(visible_rows),
        "truncated": _truthy(item.get("truncated")) or row_count > len(visible_rows),
        "columns": columns,
        "columns_truncated": _truthy(item.get("columns_truncated")) or columns_truncated,
        "rows": visible_rows,
        "download": download,
    }


def _preview_columns(rows: list[dict[str, Any]], declared_columns: list[Any]) -> tuple[list[str], bool]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = _safe_text(value, 160)
        key = text.casefold()
        if not text or key in seen or _is_preview_column_hidden(text):
            return
        seen.add(key)
        candidates.append(text)

    for column in declared_columns:
        add(column)
    for row in rows[:MAX_PREVIEW_SOURCE_ROWS]:
        for column in row:
            add(column)
    return candidates[:MAX_PREVIEW_COLUMNS], len(candidates) > MAX_PREVIEW_COLUMNS


def _is_preview_column_hidden(value: Any) -> bool:
    text = _safe_text(value, 160)
    compact = re.sub(r"[^a-z0-9가-힣]+", "", text.casefold())
    if not text or _is_sensitive_mapping_key(text):
        return True
    # The report explains code/trace separately in a safe narrative.  Raw
    # code, stack traces, and internal execution objects are never table data.
    return compact in {
        "displayrowno",
        "generatedcode",
        "pandascode",
        "pythoncode",
        "llmresponse",
        "rawllmresponse",
        "trace",
        "traceback",
        "stacktrace",
        "executioncertificate",
    }


def _preview_row_value(row: dict[str, Any], column: str) -> Any:
    if column in row:
        return row.get(column)
    target = column.casefold()
    for key, value in row.items():
        if _safe_text(key, 160).casefold() == target:
            return value
    return None


def _safe_preview_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(_safe_preview_json_value(value), ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            value = ""
    return _safe_text(value, MAX_PREVIEW_CELL_CHARACTERS)


def _safe_preview_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _safe_text(key, 100): _safe_preview_json_value(item)
            for key, item in list(value.items())[:12]
            if _safe_text(key, 100) and not _is_sensitive_mapping_key(key)
        }
    if isinstance(value, list):
        return [_safe_preview_json_value(item) for item in value[:12]]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _safe_text(value, MAX_PREVIEW_CELL_CHARACTERS)


def _preview_download(value: Any) -> dict[str, str]:
    item = _dict(value)
    download_url = _safe_public_url(item.get("url") or item.get("download_url"))
    json_url = _safe_public_url(item.get("json_url")) or _data_json_url(download_url)
    result = {
        "download_url": download_url,
        "json_url": json_url,
        "expires_at": _safe_text(item.get("expires_at"), 120),
    }
    return {key: item_value for key, item_value in result.items() if item_value}


def _data_ref_download(value: Any, role: str, *, checkpoint_key: str = "") -> dict[str, str]:
    for item in _list(value):
        if not isinstance(item, dict) or _safe_text(item.get("role"), 80) != role:
            continue
        if checkpoint_key and _safe_text(item.get("checkpoint_key"), 180) != checkpoint_key:
            continue
        return _preview_download(item)
    return {}


def _data_json_url(download_url: Any) -> str:
    url = _safe_public_url(download_url)
    if not url:
        return ""
    parsed = urlsplit(url)
    path = parsed.path or ""
    lower_path = path.casefold()
    for suffix in ("/download.csv", "/download.json", "/download"):
        if lower_path.endswith(suffix):
            next_path = path[: len(path) - len(suffix)] + "/download.json"
            return _safe_public_url(urlunsplit((parsed.scheme, parsed.netloc, next_path, parsed.query, "")))
    return ""


def _data_tables_html(value: Any) -> str:
    groups = _data_workbench_groups(value)
    specs = (
        ("original", "원본 데이터", "조회 직후의 원본 데이터입니다."),
        ("intermediate", "중간 결과", "필터·결합·계산 사이에서 선택된 확인용 단계입니다."),
        ("final", "최종 결과", "최종 결과 계약을 적용한 표입니다."),
    )
    if not any(groups.get(key) for key, _, _ in specs):
        return '<p class="empty">표시할 데이터가 없습니다.</p>'
    initial_key = next(
        (key for key in ("final", "original", "intermediate") if groups.get(key)),
        "original",
    )
    initial_tables = groups.get(initial_key) if isinstance(groups.get(initial_key), list) else []
    initial_table = initial_tables[0] if initial_tables else {}
    tab_buttons = []
    for key, title, _ in specs:
        count = len(groups.get(key) or [])
        selected = key == initial_key
        tab_buttons.append(
            f'<button type="button" class="data-tab{" active" if selected else ""}" '
            f'role="tab" aria-selected="{"true" if selected else "false"}" '
            f'data-data-group="{escape(key, quote=True)}"{" disabled" if not count else ""}>'
            f'{escape(title)}<span>{count}</span></button>'
        )
    model = {"groups": groups, "initial_group": initial_key}
    return f'''<section class="data-workbench" data-execution-data-workbench aria-label="분석 데이터 탐색">
  <nav class="data-tabs" role="tablist" aria-label="데이터 구분">{"".join(tab_buttons)}</nav>
  <div class="data-workbench-head">
    <div>
      <h4 data-data-group-title>{escape(next((title for key, title, _ in specs if key == initial_key), "데이터"))}</h4>
      <p data-data-group-subtitle>{escape(next((subtitle for key, _, subtitle in specs if key == initial_key), ""))}</p>
    </div>
    <span class="data-group-count" data-data-table-count>{len(initial_tables)}개 표</span>
  </div>
  <div class="data-toolbar" aria-label="표 제어">
    <label class="data-control data-table-selector"><span>표 선택</span><select data-data-table-select></select></label>
    <label class="data-control data-search-control"><span>검색</span><input type="search" data-data-search placeholder="값 검색"></label>
    <label class="data-control"><span>검색 컬럼</span><select data-data-filter-column></select></label>
    <label class="data-control"><span>정렬 기준</span><select data-data-sort-column></select></label>
    <button type="button" class="data-sort-direction" data-data-sort-direction aria-label="정렬 방향">오름차순 ↑</button>
    <a class="data-link data-download-link" data-data-download hidden>전체 CSV 다운로드</a>
  </div>
  <div class="data-workbench-meta">
    <p class="data-workbench-status" data-data-status aria-live="polite">표를 준비하는 중입니다.</p>
    <p class="data-workbench-expiry" data-data-expiry></p>
  </div>
  <div class="data-table-wrap data-workbench-table-wrap">
    <table data-data-table aria-label="분석 데이터 표">
      <thead data-data-table-head></thead>
      <tbody data-data-table-body></tbody>
    </table>
  </div>
  <div class="data-pagination" data-data-pagination>
    <button type="button" data-data-page-prev>이전</button>
    <span data-data-page-info>1 / 1</span>
    <button type="button" data-data-page-next>다음</button>
    <label class="data-control data-page-size"><span>행 수</span><select data-data-page-size><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></label>
  </div>
  <script id="execution-data-model" type="application/json">{_json_for_script(model)}</script>
  <noscript>
    <p class="data-note">표 안 검색·정렬·페이지 이동은 브라우저 스크립트를 켜면 사용할 수 있습니다.</p>
    {_data_table_html(initial_table, open_by_default=True)}
  </noscript>
</section>'''


def _data_workbench_groups(value: Any) -> dict[str, list[dict[str, Any]]]:
    raw_groups = _dict(value)
    result: dict[str, list[dict[str, Any]]] = {
        "original": [],
        "intermediate": [],
        "final": [],
    }
    for group in result:
        for item in _list(raw_groups.get(group)):
            if not isinstance(item, dict):
                continue
            table = _data_workbench_table(item)
            if table:
                result[group].append(table)
    return result


def _data_workbench_table(item: dict[str, Any]) -> dict[str, Any]:
    columns = _string_list(item.get("columns"))
    rows = [
        {
            column: _safe_preview_cell(_preview_row_value(row, column))
            for column in columns
        }
        for row in _list(item.get("rows"))
        if isinstance(row, dict)
    ]
    download = _dict(item.get("download"))
    return {
        "key": _safe_text(item.get("key"), 180) or "data",
        "title": _safe_text(item.get("title"), 240) or "데이터",
        "description": _safe_text(item.get("description"), 300),
        "row_count": max(_safe_int(item.get("row_count"), len(rows)), len(rows), 0),
        "shown_row_count": len(rows),
        "columns": columns,
        "rows": rows,
        "columns_truncated": _truthy(item.get("columns_truncated")),
        "download_url": _safe_public_url(download.get("download_url")),
        "json_url": _safe_public_url(download.get("json_url")) or _data_json_url(download.get("download_url")),
        "expires_at": _safe_text(download.get("expires_at"), 120),
    }


def _data_table_html(table: dict[str, Any], *, open_by_default: bool) -> str:
    title = _html_text(table.get("title"), 240) or "데이터"
    description = _html_text(table.get("description"), 300)
    row_count = max(_safe_int(table.get("row_count"), 0), 0)
    shown_count = max(_safe_int(table.get("shown_row_count"), 0), 0)
    columns = _string_list(table.get("columns"))
    rows = _list(table.get("rows"))
    download = _dict(table.get("download"))
    download_url = _safe_public_url(download.get("download_url")) or _safe_public_url(table.get("download_url"))
    expires_at_value = download.get("expires_at") or table.get("expires_at")
    expires_at = _format_timestamp(expires_at_value) if expires_at_value else ""
    tools = []
    if download_url:
        tools.append(f'<a class="data-link" href="{escape(download_url, quote=True)}">전체 CSV 다운로드</a>')
    notes = []
    if row_count > shown_count:
        notes.append(f"HTML에는 총 {row_count:,}건 중 {shown_count:,}건만 표시합니다.")
    if table.get("columns_truncated"):
        notes.append("일부 컬럼은 HTML 미리보기에서 생략되었습니다.")
    if expires_at:
        notes.append(f"데이터 링크 만료: {expires_at}")
    if rows and columns:
        header = "<th scope=\"col\">No.</th>" + "".join(
            f'<th scope="col">{_html_text(column, 160)}</th>' for column in columns
        )
        body_rows = []
        for index, row in enumerate(rows, start=1):
            values = row if isinstance(row, dict) else {}
            cells = "".join(
                f'<td>{_html_text(values.get(column), MAX_PREVIEW_CELL_CHARACTERS)}</td>'
                for column in columns
            )
            body_rows.append(f'<tr><td class="row-number">{index}</td>{cells}</tr>')
        table_body = f'<div class="data-table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'
    elif row_count > 0:
        table_body = '<p class="empty">HTML 미리보기 상한으로 행을 표시하지 않았습니다. 전체 CSV 다운로드를 이용해 주세요.</p>'
    else:
        table_body = '<p class="empty">조건에 맞는 데이터가 없습니다.</p>'
    return f'''<details class="data-table"{' open' if open_by_default else ''}>
  <summary><span class="data-table-title">{title}</span><span class="data-table-meta">총 {row_count:,}건 · {shown_count:,}건 표시</span></summary>
  <div class="data-table-body">
    {f'<p class="data-description">{description}</p>' if description else ''}
    {f'<div class="data-tools">{"".join(tools)}</div>' if tools else ''}
    {f'<p class="data-note">{" ".join(escape(note) for note in notes)}</p>' if notes else ''}
    {table_body}
  </div>
</details>'''


def _json_for_script(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = "{}"
    # A JSON script element must never be terminable by a cell value.  The
    # runtime renderer also creates every table cell with textContent.
    return (
        encoded.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _data_workbench_script() -> str:
    """Return the same-page data explorer behavior without external assets."""

    return """<script>
(() => {
  const root = document.querySelector("[data-execution-data-workbench]");
  const modelNode = document.getElementById("execution-data-model");
  if (!root || !modelNode || root.dataset.initialized === "true") return;
  root.dataset.initialized = "true";

  let model;
  try {
    model = JSON.parse(modelNode.textContent || "{}");
  } catch (_) {
    return;
  }
  const groups = model && typeof model.groups === "object" && model.groups ? model.groups : {};
  const groupInfo = {
    original: { title: "원본 데이터", subtitle: "조회 직후의 원본 데이터입니다." },
    intermediate: { title: "중간 결과", subtitle: "필터·결합·계산 사이에서 선택된 확인용 단계입니다." },
    final: { title: "최종 결과", subtitle: "최종 결과 계약을 적용한 표입니다." }
  };
  const groupKeys = ["original", "intermediate", "final"].filter((key) => Array.isArray(groups[key]) && groups[key].length);
  if (!groupKeys.length) return;

  const state = {
    group: groupKeys.includes(model.initial_group) ? model.initial_group : groupKeys[0],
    tableKey: "",
    search: "",
    filterColumn: "__all__",
    sortColumn: "",
    sortDirection: "asc",
    page: 1,
    pageSize: 25
  };
  const tableSelect = root.querySelector("[data-data-table-select]");
  const searchInput = root.querySelector("[data-data-search]");
  const filterColumnSelect = root.querySelector("[data-data-filter-column]");
  const sortColumnSelect = root.querySelector("[data-data-sort-column]");
  const sortDirectionButton = root.querySelector("[data-data-sort-direction]");
  const downloadLink = root.querySelector("[data-data-download]");
  const statusNode = root.querySelector("[data-data-status]");
  const expiryNode = root.querySelector("[data-data-expiry]");
  const titleNode = root.querySelector("[data-data-group-title]");
  const subtitleNode = root.querySelector("[data-data-group-subtitle]");
  const tableCountNode = root.querySelector("[data-data-table-count]");
  const headNode = root.querySelector("[data-data-table-head]");
  const bodyNode = root.querySelector("[data-data-table-body]");
  const paginationNode = root.querySelector("[data-data-pagination]");
  const previousButton = root.querySelector("[data-data-page-prev]");
  const nextButton = root.querySelector("[data-data-page-next]");
  const pageInfoNode = root.querySelector("[data-data-page-info]");
  const pageSizeSelect = root.querySelector("[data-data-page-size]");

  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value;
    if (typeof value === "number") return Number.isFinite(value)
      ? value.toLocaleString("ko-KR", { maximumFractionDigits: 6 })
      : String(value);
    if (typeof value === "boolean") return value ? "True" : "False";
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }

  function compactKey(value) {
    return String(value || "").toLowerCase().replace(/[^a-z0-9가-힣]+/g, "");
  }

  function hiddenColumn(value) {
    const key = compactKey(value);
    return [
      "apikey", "authorization", "cookie", "password", "passwd", "secret", "token",
      "credential", "credentials", "connection", "connectionstring", "dsn", "uri",
      "url", "sourceurl", "proxyurl", "serviceurl", "query", "querytemplate", "sql",
      "generatedcode", "pandascode", "pythoncode", "llmresponse", "rawllmresponse",
      "trace", "traceback", "stacktrace", "executioncertificate"
    ].includes(key);
  }

  function tableColumns(declared, rows) {
    const values = [];
    const seen = new Set();
    const add = (value) => {
      const name = String(value || "").trim();
      const marker = name.toLocaleLowerCase();
      if (!name || seen.has(marker) || hiddenColumn(name)) return;
      seen.add(marker);
      values.push(name);
    };
    (Array.isArray(declared) ? declared : []).forEach(add);
    (Array.isArray(rows) ? rows : []).slice(0, 100).forEach((row) => {
      if (row && typeof row === "object" && !Array.isArray(row)) Object.keys(row).forEach(add);
    });
    return values.slice(0, 120);
  }

  function tablesForGroup() {
    return Array.isArray(groups[state.group]) ? groups[state.group] : [];
  }

  function currentTable() {
    const tables = tablesForGroup();
    let table = tables.find((item) => String(item && item.key) === state.tableKey);
    if (!table) {
      table = tables[0] || null;
      state.tableKey = table ? String(table.key) : "";
    }
    return table;
  }

  function tableState(table) {
    if (!table) return null;
    if (!table.__workbenchState) {
      const previewRows = Array.isArray(table.rows) ? table.rows : [];
      table.__workbenchState = {
        rows: previewRows,
        columns: tableColumns(table.columns, previewRows),
        fullLoaded: false,
        loading: false,
        loadAttempted: false,
        loadError: "",
        rowCount: Number.isFinite(Number(table.row_count)) ? Number(table.row_count) : previewRows.length
      };
    }
    return table.__workbenchState;
  }

  function setOptions(select, options, selected) {
    if (!select) return;
    select.replaceChildren();
    options.forEach((option) => {
      const element = document.createElement("option");
      element.value = option.value;
      element.textContent = option.label;
      select.appendChild(element);
    });
    select.value = options.some((option) => option.value === selected) ? selected : (options[0] ? options[0].value : "");
  }

  function resetTableControls() {
    state.search = "";
    state.filterColumn = "__all__";
    state.sortColumn = "";
    state.sortDirection = "asc";
    state.page = 1;
    if (searchInput) searchInput.value = "";
  }

  function rowValue(row, column) {
    if (!row || typeof row !== "object") return "";
    if (Object.prototype.hasOwnProperty.call(row, column)) return row[column];
    const target = String(column).toLocaleLowerCase();
    const match = Object.keys(row).find((key) => String(key).toLocaleLowerCase() === target);
    return match ? row[match] : "";
  }

  function filteredRows(rows, columns) {
    const query = state.search.trim().toLocaleLowerCase();
    if (!query) return rows.slice();
    const searchColumns = state.filterColumn === "__all__"
      ? columns
      : columns.filter((column) => column === state.filterColumn);
    return rows.filter((row) => searchColumns.some((column) => text(rowValue(row, column)).toLocaleLowerCase().includes(query)));
  }

  function compareValues(left, right) {
    const leftText = text(left).trim();
    const rightText = text(right).trim();
    if (!leftText && !rightText) return 0;
    if (!leftText) return 1;
    if (!rightText) return -1;
    const leftNumber = Number(leftText.replace(/,/g, ""));
    const rightNumber = Number(rightText.replace(/,/g, ""));
    const numericPattern = /^[+-]?(?:\\d+\\.?\\d*|\\.\\d+)$/;
    if (numericPattern.test(leftText.replace(/,/g, "")) && numericPattern.test(rightText.replace(/,/g, ""))
      && Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
      return leftNumber - rightNumber;
    }
    return leftText.localeCompare(rightText, "ko-KR", { numeric: true, sensitivity: "base" });
  }

  function sortedRows(rows) {
    if (!state.sortColumn) return rows;
    const multiplier = state.sortDirection === "desc" ? -1 : 1;
    return rows.slice().sort((left, right) => multiplier * compareValues(
      rowValue(left, state.sortColumn),
      rowValue(right, state.sortColumn)
    ));
  }

  function formatExpiry(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("ko-KR");
  }

  function updateTabs() {
    root.querySelectorAll("[data-data-group]").forEach((button) => {
      const selected = button.dataset.dataGroup === state.group;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
    });
    const info = groupInfo[state.group] || groupInfo.original;
    if (titleNode) titleNode.textContent = info.title;
    if (subtitleNode) subtitleNode.textContent = info.subtitle;
    if (tableCountNode) tableCountNode.textContent = String(tablesForGroup().length) + "개 표";
  }

  function renderHeader(columns) {
    if (!headNode) return;
    headNode.replaceChildren();
    const row = document.createElement("tr");
    const numberHead = document.createElement("th");
    numberHead.scope = "col";
    numberHead.textContent = "No.";
    row.appendChild(numberHead);
    columns.forEach((column) => {
      const head = document.createElement("th");
      head.scope = "col";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "data-column-sort";
      button.dataset.dataSortHeader = column;
      const active = state.sortColumn === column;
      button.textContent = column + (active ? (state.sortDirection === "desc" ? " ↓" : " ↑") : "");
      button.setAttribute("aria-label", column + " 기준 정렬");
      head.appendChild(button);
      row.appendChild(head);
    });
    headNode.appendChild(row);
  }

  function renderBody(rows, columns, startIndex) {
    if (!bodyNode) return;
    bodyNode.replaceChildren();
    const fragment = document.createDocumentFragment();
    rows.forEach((row, index) => {
      const tableRow = document.createElement("tr");
      const numberCell = document.createElement("td");
      numberCell.className = "row-number";
      numberCell.textContent = String(startIndex + index + 1);
      tableRow.appendChild(numberCell);
      columns.forEach((column) => {
        const cell = document.createElement("td");
        cell.textContent = text(rowValue(row, column)) || "-";
        tableRow.appendChild(cell);
      });
      fragment.appendChild(tableRow);
    });
    bodyNode.appendChild(fragment);
  }

  function render() {
    updateTabs();
    const table = currentTable();
    if (!table) return;
    const details = tableState(table);
    const tables = tablesForGroup();
    setOptions(tableSelect, tables.map((item) => ({ value: String(item.key), label: String(item.title || item.key || "데이터") })), state.tableKey);
    state.tableKey = tableSelect ? tableSelect.value : state.tableKey;
    const columns = details.columns;
    if (!columns.includes(state.filterColumn)) state.filterColumn = "__all__";
    if (!columns.includes(state.sortColumn)) state.sortColumn = "";
    setOptions(filterColumnSelect, [{ value: "__all__", label: "전체 컬럼" }, ...columns.map((column) => ({ value: column, label: column }))], state.filterColumn);
    state.filterColumn = filterColumnSelect ? filterColumnSelect.value : state.filterColumn;
    setOptions(sortColumnSelect, [{ value: "", label: "정렬 안 함" }, ...columns.map((column) => ({ value: column, label: column }))], state.sortColumn);
    state.sortColumn = sortColumnSelect ? sortColumnSelect.value : state.sortColumn;
    if (sortDirectionButton) {
      sortDirectionButton.textContent = state.sortDirection === "desc" ? "내림차순 ↓" : "오름차순 ↑";
      sortDirectionButton.disabled = !state.sortColumn;
    }
    if (downloadLink) {
      downloadLink.hidden = !table.download_url;
      if (table.download_url) downloadLink.href = table.download_url;
    }
    if (expiryNode) expiryNode.textContent = table.expires_at ? "데이터 링크 만료: " + formatExpiry(table.expires_at) : "";

    const matched = sortedRows(filteredRows(details.rows, columns));
    const pageCount = Math.max(Math.ceil(matched.length / state.pageSize), 1);
    state.page = Math.min(Math.max(state.page, 1), pageCount);
    const start = (state.page - 1) * state.pageSize;
    const pageRows = matched.slice(start, start + state.pageSize);
    renderHeader(columns);
    renderBody(pageRows, columns, start);
    if (pageInfoNode) pageInfoNode.textContent = state.page + " / " + pageCount + " 페이지";
    if (previousButton) previousButton.disabled = state.page <= 1;
    if (nextButton) nextButton.disabled = state.page >= pageCount;
    if (paginationNode) paginationNode.hidden = !columns.length;

    if (statusNode) {
      if (details.loading) {
        statusNode.textContent = "전체 데이터를 불러오는 중입니다. 현재 제한된 미리보기를 표시합니다.";
      } else if (details.fullLoaded) {
        statusNode.textContent = "전체 " + details.rowCount.toLocaleString("ko-KR") + "건 중 "
          + matched.length.toLocaleString("ko-KR") + "건 · " + columns.length + "개 컬럼";
      } else if (details.loadError) {
        statusNode.textContent = details.loadError + " 현재 제한된 미리보기 "
          + details.rows.length.toLocaleString("ko-KR") + "건에서 검색·정렬할 수 있습니다.";
      } else if (!table.json_url) {
        statusNode.textContent = "전체 데이터 링크가 없어 제한된 미리보기 "
          + details.rows.length.toLocaleString("ko-KR") + "건을 표시합니다.";
      } else {
        statusNode.textContent = "미리보기 " + details.rows.length.toLocaleString("ko-KR")
          + "건 · 전체 " + details.rowCount.toLocaleString("ko-KR") + "건";
      }
    }
  }

  async function loadFullTable(table) {
    const details = tableState(table);
    if (!details || details.loading || details.fullLoaded || details.loadAttempted) return;
    details.loadAttempted = true;
    if (!table.json_url) {
      render();
      return;
    }
    let endpoint;
    try {
      endpoint = new URL(table.json_url, window.location.href);
    } catch (_) {
      details.loadError = "전체 데이터 주소를 확인할 수 없습니다.";
      render();
      return;
    }
    if (endpoint.origin !== window.location.origin) {
      details.loadError = "리포트와 같은 API 주소의 데이터만 표 안에서 바로 열 수 있습니다.";
      render();
      return;
    }
    details.loading = true;
    render();
    try {
      const response = await fetch(endpoint.toString(), {
        method: "GET",
        credentials: "same-origin",
        headers: { "Accept": "application/json" }
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const body = await response.json();
      const loaded = body && typeof body.loaded === "object" && body.loaded ? body.loaded : {};
      if (!Array.isArray(loaded.rows)) throw new Error("invalid_data");
      details.rows = loaded.rows;
      details.columns = tableColumns(Array.isArray(loaded.columns) ? loaded.columns : table.columns, loaded.rows);
      details.rowCount = Number.isFinite(Number(loaded.row_count)) ? Number(loaded.row_count) : loaded.rows.length;
      details.fullLoaded = true;
      details.loadError = "";
    } catch (_) {
      details.loadError = "전체 데이터를 불러오지 못했습니다.";
    } finally {
      details.loading = false;
      render();
    }
  }

  function selectGroup(group) {
    if (!groupKeys.includes(group)) return;
    state.group = group;
    state.tableKey = "";
    resetTableControls();
    render();
    const table = currentTable();
    if (table) void loadFullTable(table);
  }

  root.querySelectorAll("[data-data-group]").forEach((button) => {
    button.addEventListener("click", () => selectGroup(button.dataset.dataGroup || ""));
  });
  if (tableSelect) tableSelect.addEventListener("change", () => {
    state.tableKey = tableSelect.value;
    resetTableControls();
    render();
    const table = currentTable();
    if (table) void loadFullTable(table);
  });
  if (searchInput) searchInput.addEventListener("input", () => {
    state.search = searchInput.value || "";
    state.page = 1;
    render();
  });
  if (filterColumnSelect) filterColumnSelect.addEventListener("change", () => {
    state.filterColumn = filterColumnSelect.value || "__all__";
    state.page = 1;
    render();
  });
  if (sortColumnSelect) sortColumnSelect.addEventListener("change", () => {
    state.sortColumn = sortColumnSelect.value || "";
    state.page = 1;
    render();
  });
  if (sortDirectionButton) sortDirectionButton.addEventListener("click", () => {
    state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    render();
  });
  if (pageSizeSelect) pageSizeSelect.addEventListener("change", () => {
    state.pageSize = Math.max(Number(pageSizeSelect.value) || 25, 1);
    state.page = 1;
    render();
  });
  if (previousButton) previousButton.addEventListener("click", () => {
    state.page = Math.max(state.page - 1, 1);
    render();
  });
  if (nextButton) nextButton.addEventListener("click", () => {
    state.page += 1;
    render();
  });
  root.addEventListener("click", (event) => {
    const button = event.target && event.target.closest ? event.target.closest("[data-data-sort-header]") : null;
    if (!button) return;
    const column = button.dataset.dataSortHeader || "";
    if (!column) return;
    if (state.sortColumn === column) {
      state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    } else {
      state.sortColumn = column;
      state.sortDirection = "asc";
    }
    state.page = 1;
    render();
  });

  render();
  const initialTable = currentTable();
  if (initialTable) void loadFullTable(initialTable);
})();
</script>"""


def _issues_html(value: Any) -> str:
    issues = value if isinstance(value, list) else []
    cards = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        severity = _safe_text(item.get("severity"), 40).lower()
        warning = severity != "error"
        cards.append(f"""<article class="issue{' warning' if warning else ''}">
  <h3>{_html_text(item.get('stage'), 160) or '확인 필요'}</h3>
  <p>{_html_text(item.get('message'), MAX_TEXT_LENGTH)}</p>
</article>""")
    if not cards:
        return ""
    return '<section class="issue-section" aria-labelledby="issue-title"><h2 id="issue-title" class="section-title">오류 및 확인 사항</h2>' + "".join(cards) + "</section>"


def _step_detail(step: dict[str, Any], row_counts: dict[str, int] | None = None) -> str:
    operation = _safe_text(step.get("operation"), 80).casefold()
    source = _safe_text(
        step.get("source_alias") or step.get("left_source_alias") or step.get("input_alias"),
        180,
    )
    output = _safe_text(step.get("output_alias"), 180)
    group_by = _string_list(step.get("group_by"))
    aggregations = step.get("aggregations") if isinstance(step.get("aggregations"), list) else []
    pieces: list[str] = []
    if source:
        pieces.append(f"입력: {source}")
    if operation == "join":
        right = _safe_text(step.get("right_source_alias"), 180)
        join_type = _safe_text(step.get("join_type"), 80) or "결합"
        if right:
            pieces.append(f"결합 대상: {right} ({join_type})")
    if group_by:
        pieces.append("기준 컬럼: " + ", ".join(group_by[:12]))
    if aggregations:
        aggregate_labels = []
        for item in aggregations[:8]:
            if isinstance(item, dict):
                column = _safe_text(item.get("column") or item.get("source_column"), 120)
                method = _safe_text(item.get("method") or item.get("aggregation"), 80)
                output_column = _safe_text(item.get("output_column"), 120)
                text = " ".join(part for part in (column, method, f"→ {output_column}" if output_column else "") if part)
                if text:
                    aggregate_labels.append(text)
        if aggregate_labels:
            pieces.append("집계: " + ", ".join(aggregate_labels))
    formula = _dict(step.get("formula"))
    if formula:
        target = _safe_text(formula.get("output_column"), 120)
        operator = _safe_text(formula.get("operator"), 80)
        expression = _formula_expression(target, operator, formula.get("operands"))
        if expression:
            pieces.append(f"계산식: {expression}")
        elif target or operator:
            pieces.append(f"계산 결과: {target or '새 지표'} ({operator or '수식'})")
    if output:
        pieces.append(f"출력: {output}")
    row_count_text = _step_row_count_text(step, row_counts or {})
    if row_count_text:
        pieces.append(row_count_text)
    return " · ".join(pieces)


# 함수 설명: 수식 계약의 column/constant operand를 사람이 읽을 수 있는 식으로 표현합니다.
def _formula_expression(target: str, operator: str, operands_value: Any) -> str:
    operands: list[str] = []
    for item in _list(operands_value):
        if not isinstance(item, dict):
            continue
        column = _safe_text(item.get("column"), 120)
        if column:
            operands.append(column)
            continue
        if "constant" in item:
            constant = _safe_value_text(item.get("constant"), 80)
            if constant:
                operands.append(constant)
    if not operands:
        return ""
    symbols = {
        "add": "+",
        "subtract": "−",
        "multiply": "×",
        "divide": "÷",
    }
    joiner = f" {symbols.get(operator.casefold(), operator or '·')} "
    expression = joiner.join(operands)
    return f"{target} = {expression}" if target else expression


def _mapping_text(value: Any) -> str:
    mapping = value if isinstance(value, dict) else {}
    if not mapping:
        return ""
    parts = []
    for key, item in list(mapping.items())[:16]:
        label = _safe_text(key, 100)
        rendered = _condition_text(item)
        if label and rendered:
            parts.append(f"{label}={rendered}")
    return ", ".join(parts)


def _condition_text(value: Any) -> str:
    if isinstance(value, dict):
        operator = _safe_text(value.get("operator"), 60)
        raw = value.get("value") if "value" in value else value.get("values")
        rendered = _safe_value_text(raw, 220)
        return " ".join(part for part in (operator, rendered) if part)
    return _safe_value_text(value, 220)


# 함수 설명: source 실제 결과의 map은 존재 여부 자체를 "실제 적용" 근거로 취급합니다.
def _actual_mapping(actual: dict[str, Any], *keys: str) -> tuple[dict[str, Any], str]:
    for key in keys:
        if key in actual and isinstance(actual.get(key), dict):
            return _safe_mapping(actual.get(key)), "actual"
    return {}, ""


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value_item in list(value.items())[:16]:
        safe_key = _safe_text(key, 100)
        # Omit credential/query metadata completely.  This component publishes
        # to a persistent report store, so a visible `[hidden]` placeholder is
        # less useful than not serializing the field at all.
        if not safe_key or _is_sensitive_mapping_key(safe_key):
            continue
        result[safe_key] = _safe_value(value_item)
    return result


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:12]:
            safe_key = _safe_text(key, 100)
            if not safe_key or _is_sensitive_mapping_key(safe_key):
                continue
            result[safe_key] = _safe_value(item)
        return result
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:12]]
    return _safe_text(value, 240)


def _safe_value_text(value: Any, limit: int) -> str:
    if isinstance(value, (dict, list)):
        try:
            return _safe_text(json.dumps(_safe_value(value), ensure_ascii=False), limit)
        except (TypeError, ValueError):
            return ""
    return _safe_text(value, limit)


def _safe_error_message(value: Any) -> str:
    messages = _error_messages(value)
    return messages[0] if messages else ""


# 함수 설명: nested gate/error 구조에서 raw object를 stringify하지 않고 안전한 문장만 순회합니다.
def _error_messages(value: Any, _depth: int = 0) -> list[str]:
    if _depth > 4:
        return []
    if isinstance(value, str):
        text = _safe_text(value, MAX_TEXT_LENGTH)
        return [text] if text else []
    if isinstance(value, list):
        messages: list[str] = []
        for item in value:
            messages.extend(_error_messages(item, _depth + 1))
        return _dedupe_texts(messages, MAX_ISSUES)
    if not isinstance(value, dict):
        text = _safe_text(value, MAX_TEXT_LENGTH)
        return [text] if text else []

    messages: list[str] = []
    primary = value.get("error_message") or value.get("message") or value.get("reason") or value.get("skip_reason")
    if primary not in (None, ""):
        messages.extend(_error_messages(primary, _depth + 1))
    for nested_key in (
        "critical_failures",
        "failures",
        "validation_errors",
        "errors",
        "issues",
    ):
        if nested_key in value:
            messages.extend(_error_messages(value.get(nested_key), _depth + 1))
    if not messages:
        fallback = _safe_text(value.get("type") or value.get("failure_type"), MAX_TEXT_LENGTH)
        if fallback:
            messages.append(fallback)
    return _dedupe_texts(messages, MAX_ISSUES)


# 함수 설명: blocked 확인사항은 canonical 문자열만 받아 report에 보입니다.
def _safe_reason_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = _safe_text(item, MAX_TEXT_LENGTH)
        if text:
            result.append(text)
    return _dedupe_texts(result, MAX_ISSUES)


def _dedupe_texts(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_text(value, MAX_TEXT_LENGTH)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _safe_exception_message(exc: Exception) -> str:
    return _safe_text(str(exc), MAX_TEXT_LENGTH)


# 함수 설명: 실행 코드의 줄바꿈·들여쓰기는 보존하되 민감 assignment/URI/auth 값을
# 먼저 제거하고, report 전용 크기 상한을 적용합니다. raw code는 반환하지 않습니다.
def _safe_execution_code_text(value: Any) -> tuple[str, bool, bool, int, int]:
    raw = str(value or "")
    original_count = len(raw)
    if not raw or raw.isspace():
        return "", False, False, original_count, 0
    scan_truncated = len(raw) > MAX_EXECUTION_CODE_SCAN_CHARACTERS
    scan_text = raw[:MAX_EXECUTION_CODE_SCAN_CHARACTERS]
    normalized = scan_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", normalized)
    line_end = 0
    for _ in range(MAX_EXECUTION_CODE_SCAN_LINES):
        next_end = normalized.find("\n", line_end)
        if next_end < 0:
            break
        line_end = next_end + 1
    if line_end and normalized.find("\n", line_end) >= 0:
        normalized = normalized[:line_end]
        scan_truncated = True
    redacted_text, ast_redacted, parsed = _redact_sensitive_code_statements(normalized)
    if not parsed and _contains_sensitive_code_assignment(normalized):
        # Invalid Python plus a multi-line credential assignment cannot be
        # redacted with reliable statement boundaries.  Fail closed for this
        # optional report section while leaving the analysis result untouched.
        return "", False, True, original_count, 0

    redacted = ast_redacted
    replacements = (
        (PRIVATE_KEY_BLOCK_PATTERN, "# [개인 키 숨김]"),
        (CONNECTION_URI_PATTERN, "[연결 정보 숨김]"),
        (CREDENTIAL_URL_PATTERN, "[연결 정보 숨김]"),
        (SENSITIVE_QUERY_VALUE_PATTERN, r"\1[숨김]"),
        (SENSITIVE_AUTH_SCHEME_PATTERN, "인증 정보 [숨김]"),
        (HIGH_CONFIDENCE_SECRET_PATTERN, "[인증 정보 숨김]"),
    )
    for pattern, replacement in replacements:
        next_text = pattern.sub(replacement, redacted_text)
        if next_text != redacted_text:
            redacted = True
        redacted_text = next_text

    if "-----BEGIN" in redacted_text.upper() and "PRIVATE KEY" in redacted_text.upper():
        return "", False, True, original_count, 0

    # Only use the line fallback when AST parsing was impossible.  Parsed
    # Python is handled by statement boundaries above; applying the broad text
    # regex again would misread valid result columns such as TOKEN/URL/URI as
    # runtime credentials.
    if not parsed:
        safe_lines: list[str] = []
        for line in redacted_text.split("\n"):
            next_line = SENSITIVE_CODE_VALUE_PATTERN.sub(
                lambda match: match.group("prefix") + "'[숨김]'",
                line,
            )
            if next_line != line:
                redacted = True
            safe_lines.append(next_line)
        redacted_text = "\n".join(safe_lines)
    redacted_text = redacted_text.strip("\n")
    if not redacted_text.strip():
        return "", False, redacted, original_count, 0

    lines = redacted_text.split("\n")
    truncated = scan_truncated or len(lines) > MAX_EXECUTION_CODE_LINES
    if truncated:
        lines = lines[:MAX_EXECUTION_CODE_LINES]
    bounded = "\n".join(lines)
    if len(bounded) > MAX_EXECUTION_CODE_CHARACTERS:
        bounded = bounded[:MAX_EXECUTION_CODE_CHARACTERS]
        truncated = True
    bounded = bounded.rstrip()
    shown_count = len(bounded)
    if truncated:
        bounded += (
            "\n\n# … 보고서 표시 한도로 이후 코드를 생략했습니다. "
            f"(원문 {original_count:,}자)"
        )
    return bounded, truncated, redacted, original_count, shown_count


def _redact_sensitive_code_statements(value: str) -> tuple[str, bool, bool]:
    try:
        tree = ast.parse(value)
    except (SyntaxError, ValueError, TypeError):
        return value, False, False
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    sensitive_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_sensitive_code_key(name) for target in targets for name in _assignment_target_names(target)):
                sensitive_nodes.append(node)
        elif isinstance(node, ast.Dict):
            keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
            if any(_sensitive_code_mapping_literal_key(key) for key in keys):
                sensitive_nodes.append(node)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)) and _contains_sensitive_transport_pair(node):
            sensitive_nodes.append(node)
        elif isinstance(node, ast.keyword) and node.arg and _sensitive_code_key(node.arg):
            sensitive_nodes.append(node)
    ranges: set[tuple[int, int]] = set()
    for node in sensitive_nodes:
        statement = node
        while not isinstance(statement, ast.stmt) and statement in parents:
            statement = parents[statement]
        start = getattr(statement, "lineno", None)
        end = getattr(statement, "end_lineno", start)
        if isinstance(start, int) and isinstance(end, int):
            ranges.add((max(start, 1), max(end, start)))
    if not ranges:
        return value, False, True
    lines = value.split("\n")
    hidden: set[int] = set()
    markers: dict[int, str] = {}
    for start, end in sorted(ranges):
        hidden.update(range(start, end + 1))
        indent_source = lines[start - 1] if start - 1 < len(lines) else ""
        indent = indent_source[: len(indent_source) - len(indent_source.lstrip())]
        markers.setdefault(start, indent + "pass  # [민감 실행 설정 숨김]")
    output = []
    for number, line in enumerate(lines, start=1):
        if number in markers:
            output.append(markers[number])
        if number not in hidden:
            output.append(line)
    return "\n".join(output), True, True


def _assignment_target_names(value: ast.AST) -> list[str]:
    if isinstance(value, ast.Name):
        return [value.id]
    if isinstance(value, ast.Attribute):
        return [value.attr]
    if isinstance(value, ast.Subscript):
        slice_value = value.slice
        if isinstance(slice_value, ast.Constant) and isinstance(slice_value.value, str):
            return [slice_value.value]
        return []
    if isinstance(value, (ast.Tuple, ast.List)):
        return [name for item in value.elts for name in _assignment_target_names(item)]
    return []


def _sensitive_code_key(value: Any) -> bool:
    text = str(value or "")
    compact = re.sub(r"[^a-z0-9]+", "", text.casefold())
    if _is_sensitive_mapping_key(text):
        return True
    if any(
        marker in compact
        for marker in (
            "apikey",
            "password",
            "passwd",
            "secret",
            "credential",
            "cookie",
            "connectionstring",
            "privatekey",
        )
    ):
        return True
    if any(
        marker in compact
        for marker in (
            "accesstoken",
            "refreshtoken",
            "idtoken",
            "authtoken",
            "oauthtoken",
            "bearertoken",
            "sessiontoken",
            "securitytoken",
        )
    ) and not compact.startswith(("producttoken", "processtoken")):
        return True
    if compact.startswith(("auth", "oauth", "privatekey")):
        return True
    if compact.startswith("dsn"):
        return True
    return compact in {
        "auth",
        "authentication",
        "creds",
        "headers",
        "privatekey",
        "query",
        "querytemplate",
        "sql",
        "sqltemplate",
        "sourceconfig",
    }


def _sensitive_code_mapping_literal_key(value: Any) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    # TOKEN/URL/URI are valid manufacturing result-column names.  A variable
    # assignment with those names is still treated conservatively, but a row
    # literal such as {"TOKEN": "A"} must remain inspectable.
    if compact in {"token", "url", "uri"}:
        return False
    return _sensitive_code_key(value)


def _sensitive_transport_key(value: Any) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    if not compact:
        return False
    return (
        "authorization" in compact
        or "apikey" in compact
        or "cookie" in compact
        or compact.startswith(("xauth", "httpauth", "oauth"))
        or compact in {"proxyauthorization", "wwwauthenticate"}
    )


def _contains_sensitive_transport_pair(value: ast.List | ast.Tuple | ast.Set) -> bool:
    candidates: list[ast.AST] = [value]
    candidates.extend(
        item
        for item in value.elts
        if isinstance(item, (ast.List, ast.Tuple))
    )
    for pair in candidates:
        if not isinstance(pair, (ast.List, ast.Tuple)) or len(pair.elts) != 2:
            continue
        key = pair.elts[0]
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and _sensitive_transport_key(key.value)
        ):
            return True
    return False


def _contains_sensitive_code_assignment(value: str) -> bool:
    if SENSITIVE_CODE_VALUE_PATTERN.search(value) or SENSITIVE_ASSIGNMENT_PATTERN.search(value):
        return True
    for line in value.splitlines():
        match = re.match(
            r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=",
            line,
        )
        if match and _sensitive_code_key(match.group(1)):
            return True
    return False


def _safe_text(value: Any, limit: int) -> str:
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    text = CONNECTION_URI_PATTERN.sub("[연결 정보 숨김]", text)
    text = CREDENTIAL_URL_PATTERN.sub("[연결 정보 숨김]", text)
    text = SENSITIVE_QUERY_VALUE_PATTERN.sub(r"\1[숨김]", text)
    text = SENSITIVE_ASSIGNMENT_PATTERN.sub(lambda match: match.group(1) + "=[숨김]", text)
    text = SENSITIVE_AUTH_SCHEME_PATTERN.sub("인증 정보 [숨김]", text)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _is_sensitive_mapping_key(value: Any) -> bool:
    raw = str(value or "").casefold()
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    if compact in SENSITIVE_MAPPING_KEY_COMPACT:
        return True
    # Common transport/header forms do not have a single canonical spelling:
    # X-API-Key, HTTP_AUTHORIZATION, x-auth-token, OAuth token, etc.  These
    # are never useful in a user-facing execution report, while normal
    # manufacturing fields (LEAD/MCP_NO/OPER_NAME) do not match these markers.
    if "authorization" in compact or "apikey" in compact or compact.startswith("oauth"):
        return True
    if compact.startswith("xauth") or compact.startswith("httpauth"):
        return True
    if compact.endswith("token") and compact not in {"producttoken", "processtoken"}:
        return True
    if compact.endswith("key") and compact not in {"productkey", "processkey", "groupkey", "businesskey"}:
        return True
    return False


def _lookup_key(value: Any) -> str:
    return _safe_text(value, 180).casefold()


def _safe_public_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 4_096 or any(ord(character) < 32 for character in url):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return ""
    return url


def _has_explicit_text(value: Any) -> bool:
    return bool(str(value if value is not None else "").strip())


def _resolve_report_api_url(value: Any) -> str:
    """Resolve the POST target without hiding an explicitly invalid setting.

    An imported Flow intentionally leaves the node input blank so the
    Langflow runtime can use its deployment environment.  A nonblank input is
    an operator override and must therefore either be used as-is or fail
    visibly in the node's best-effort trace; silently falling back would mask
    a typo such as a stale port number.
    """

    if _has_explicit_text(value):
        return _safe_public_url(value)
    for env_name in REPORT_API_ENV_NAMES:
        configured = _safe_public_url(os.getenv(env_name))
        if configured:
            return configured
    return DEFAULT_REPORT_API_URL


def _reports_post_url(value: Any) -> str:
    base = _safe_public_url(value)
    if not base:
        return ""
    return base.rstrip("/") if base.rstrip("/").endswith("/reports") else base.rstrip("/") + "/reports"


def _is_same_execution_artifact(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _safe_text(value.get("type"), 80).casefold() == DESCRIPTOR_TYPE
        or _safe_text(value.get("artifact_type"), 80).casefold() == ARTIFACT_TYPE
    )


def _payload_copy(value: Any) -> dict[str, Any]:
    payload = _payload_view(value)
    return deepcopy(payload) if payload else {}


def _payload_view(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return data if isinstance(data, dict) else {}


def _inspection(payload: dict[str, Any]) -> dict[str, Any]:
    trace = _dict(payload.get("trace"))
    payload["trace"] = trace
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    trace["inspection"] = inspection
    return inspection


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _safe_text(item, 160)
        if text and text not in result:
            result.append(text)
    return result


def _columns_from_rows(value: Any) -> list[str]:
    columns: list[str] = []
    if not isinstance(value, list):
        return columns
    for row in value[:5]:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = _safe_text(key, 160)
            if text and text not in columns:
                columns.append(text)
    return columns


def _route_value(
    payload: dict[str, Any],
    plan: dict[str, Any],
    analysis: dict[str, Any],
    inspection: dict[str, Any] | None = None,
    execution_gate: dict[str, Any] | None = None,
) -> str:
    """Resolve the actual route using the canonical post-resolver contract.

    ``route_resolution.final_route`` is the normal V2 field written after the
    Fast/Complex resolver.  The older flat ``execution_path`` values remain
    supported because they can carry a later executor result.  A blocked gate
    is authoritative: no planned route was actually allowed to execute.
    """
    gate = execution_gate if isinstance(execution_gate, dict) else _dict(payload.get("execution_gate"))
    if _safe_text(gate.get("status"), 80).casefold() == "blocked":
        return "blocked"
    route_resolution = _dict(plan.get("route_resolution"))
    inspection = inspection if isinstance(inspection, dict) else _inspection(payload)
    pandas_execution = _dict(inspection.get("pandas_execution"))
    routing = _dict(payload.get("routing"))
    return _safe_text(
        analysis.get("execution_path")
        or analysis.get("final_execution_path")
        or pandas_execution.get("execution_path")
        or route_resolution.get("final_route")
        or plan.get("execution_path")
        or _dict(payload.get("simple_analysis_contract")).get("route")
        or routing.get("final_execution_path")
        or routing.get("route")
        or payload.get("execution_path"),
        80,
    ).lower()


def _analysis_type_value(plan: dict[str, Any], analysis: dict[str, Any], inspection: dict[str, Any]) -> str:
    """Read the canonical analysis kind before legacy display aliases."""
    intent_trace = _dict(inspection.get("intent"))
    return _safe_text(
        plan.get("analysis_kind")
        or plan.get("analysis_type")
        or plan.get("intent_type")
        or analysis.get("analysis_kind")
        or analysis.get("analysis_type")
        or intent_trace.get("analysis_kind")
        or intent_trace.get("analysis_type"),
        160,
    )


def _status_value(payload: dict[str, Any], analysis: dict[str, Any], execution_gate: dict[str, Any]) -> str:
    if _safe_text(execution_gate.get("status"), 80).lower() == "blocked":
        return "blocked"
    status = _safe_text(analysis.get("status") or payload.get("status"), 80).lower()
    if status in {"ok", "success", "completed"}:
        return "ok"
    if status in {"partial", "recovered"}:
        return "partial"
    if status in {"blocked", "skipped"}:
        return "blocked"
    if status in {"error", "failed", "failure"}:
        return "error"
    return status or "unknown"


def _status_label(status: str) -> str:
    return {
        "ok": "분석 완료",
        "partial": "부분 결과 제공",
        "blocked": "처리 시작 전 중단",
        "error": "처리 중 오류 발생",
        "planned": "처리 계획 수립",
        "completed": "조회 완료",
        "failed": "조회 실패",
        "skipped": "조회 생략",
        "pending": "조회 대기",
    }.get(status, "상태 확인 필요")


def _retrieval_status_label(status: str) -> str:
    """Keep the retrieval card vocabulary distinct from overall analysis status."""
    return {
        "ok": "조회 완료",
        "success": "조회 완료",
        "completed": "조회 완료",
        "partial": "일부 조회 완료",
        "blocked": "조회 차단",
        "error": "조회 실패",
        "failed": "조회 실패",
        "skipped": "조회 생략",
        "planned": "조회 계획 수립",
        "pending": "조회 대기",
    }.get(status, "상태 확인 필요")


def _status_class(status: str) -> str:
    return status if status in {"blocked", "error", "partial"} else "ok"


def _route_label(route: str) -> str:
    normalized = route.casefold()
    if normalized == "fast":
        return "Fast 처리"
    if normalized == "complex":
        return "Complex 처리"
    if normalized == "blocked":
        return "실행 차단"
    return route or "처리 경로 미확정"


def _is_partial_result(analysis: dict[str, Any], data: dict[str, Any]) -> bool:
    return bool(data.get("partial")) or bool(_dict(analysis.get("recovered_result")).get("available")) or _safe_text(analysis.get("status"), 80).lower() == "partial"


def _trace_id(payload: dict[str, Any]) -> str:
    trace = _dict(payload.get("trace"))
    request = _dict(payload.get("request"))
    return _safe_text(trace.get("trace_id") or payload.get("trace_id") or request.get("request_id") or request.get("session_id"), 160)


def _question(payload: dict[str, Any]) -> str:
    request = _dict(payload.get("request"))
    return _safe_text(payload.get("question") or request.get("question") or request.get("text"), 4_000)


def _format_number(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    return f"{max(number, 0):,}"


def _format_timestamp(value: Any) -> str:
    text = _safe_text(value, 100)
    if not text:
        return "-"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text.replace("T", " ")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _html_text(value: Any, limit: int) -> str:
    return escape(_safe_text(value, limit), quote=True)


def _humanize_operation(value: str) -> str:
    text = re.sub(r"[_-]+", " ", value).strip()
    return text.title() if text else ""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        resolved = int(float(value))
    except (TypeError, ValueError):
        resolved = default
    return max(minimum, min(resolved, maximum))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "예", "사용", "표시"}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Langflow 컴포넌트 클래스: 결과·세션·런타임 정리 뒤에만 연결해 분석 실행 경로와 분리합니다.
class ExecutionTraceArtifactPublisher(Component):
    display_name = "25 분석 처리 과정 HTML 발행기"
    description = "분석 실행 과정, 마스킹된 pandas 실행 코드, 제한된 원본·중간·최종 데이터 미리보기를 사용자용 HTML로 발행하고 보기·다운로드 링크를 추가합니다."
    icon = "Map"
    name = "ExecutionTraceArtifactPublisher"
    inputs = [
        DataInput(name="payload", display_name="정리된 응답 페이로드", required=True),
        BoolInput(
            name="enabled",
            display_name="분석 과정 HTML 발행",
            info="꺼도 분석 결과에는 영향을 주지 않으며 HTML 링크만 만들지 않습니다.",
            value=True,
            required=False,
            advanced=False,
        ),
        MessageTextInput(
            name="report_api_url",
            display_name="발행 대상 HTML Report API 주소",
            info=(
                "API_SERVER의 POST /reports 주소 또는 Base URL입니다. 비워두면 "
                "Langflow 환경의 API_SERVER_REPORT_API_URL, API_SERVER_PUBLIC_BASE_URL, "
                "http://127.0.0.1:5000 순으로 사용합니다. API_SERVER가 반환하는 "
                "보기·다운로드 링크는 API_SERVER_PUBLIC_BASE_URL을 따릅니다."
            ),
            value="",
            required=False,
            advanced=False,
        ),
        IntInput(
            name="ttl_hours",
            display_name="HTML 링크 유효시간(시간)",
            value=DEFAULT_TTL_HOURS,
            required=False,
            advanced=False,
        ),
        IntInput(
            name="timeout_seconds",
            display_name="발행 요청 제한 시간(초)",
            value=DEFAULT_TIMEOUT_SECONDS,
            required=False,
            advanced=True,
        ),
    ]
    outputs = [Output(name="payload_out", display_name="Report 링크 포함 페이로드", method="build_payload", types=["Data"])]

    # 함수 설명: 발행 결과를 Langflow Data로 감싸 다음 답변/API 노드에 전달합니다.
    def build_payload(self) -> Data:
        result = publish_execution_trace_artifact(
            getattr(self, "payload", None),
            enabled=getattr(self, "enabled", True),
            report_api_url=getattr(self, "report_api_url", ""),
            ttl_hours=getattr(self, "ttl_hours", DEFAULT_TTL_HOURS),
            timeout_seconds=getattr(self, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        )
        self.status = _dict(_inspection(result).get("execution_trace_artifact")) if result else {}
        return Data(data=result)
