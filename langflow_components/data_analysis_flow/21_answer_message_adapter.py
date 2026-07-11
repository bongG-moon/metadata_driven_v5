# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 21 답변 메시지 어댑터
# 역할: 최종 답변과 결과 테이블을 서비스 채팅 출력용 메시지로 변환합니다.
# 주요 입력: 페이로드 (payload) · 필수, 다운로드 링크 Base URL (download_base_url), 개발자 진단 포함 (include_diagnostics), 결과 테이블 표시
#        (show_result_table), 중간 산출물/helper 결과 표시 (show_analysis_evidence), 다운로드 링크 표시 (show_download_links), 경고/참고
#        표시 (show_notices), 적용 기준 표시 (show_applied_criteria), 다음 질문 표시 (show_next_questions), 의도 분석 표시
#        (show_intent_analysis), 데이터 조회 진단 표시 (show_data_retrieval), pandas 코드 표시 (show_pandas_code)
# 주요 출력: 메시지 (message)
# 처리 흐름: 구조화 답변을 표·진단·조회 계획이 포함된 Markdown Message 하나로 렌더링합니다.
# 유지보수 포인트: 이 노드만 최종 Chat Output에 연결해 중간 질문이나 JSON이 대화 기록에 중복 출력되지 않게 합니다.
# =============================================================================

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, MessageTextInput, Output
from lfx.schema.message import Message

TABLE_PREVIEW_LIMIT = 10
CELL_TEXT_LIMIT = 120
VALUE_TEXT_LIMIT = 900
DEFAULT_DOWNLOAD_BASE_URL = "http://localhost:8765"


# 주요 함수: 구조화 결과를 사용자가 읽을 수 있는 단일 Markdown Message로 변환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_message(
    payload_value: Any,
    download_base_url: Any = "",
    include_diagnostics: Any = False,
    show_result_table: Any = True,
    show_analysis_evidence: Any = False,
    show_download_links: Any = True,
    show_notices: Any = True,
    show_intent_analysis: Any = "",
    show_data_retrieval: Any = "",
    show_pandas_code: Any = "",
    show_applied_criteria: Any = True,
    show_next_questions: Any = True,
) -> str:
    payload = _payload(payload_value)
    if not payload:
        return ""
    options = _message_options(
        include_diagnostics,
        show_result_table,
        show_analysis_evidence,
        show_download_links,
        show_notices,
        show_intent_analysis,
        show_data_retrieval,
        show_pandas_code,
        show_applied_criteria,
        show_next_questions,
    )
    answer_sections = payload.get("answer_sections") if isinstance(payload.get("answer_sections"), dict) else {}

    if answer_sections:
        sections = _message_sections_from_answer_sections(payload, answer_sections, download_base_url, options)
        for section in _diagnostic_sections(payload, options):
            if section:
                sections.append(section)
        if sections:
            return "\n\n".join(sections)

    sections: list[str] = []
    answer = str(payload.get("answer_message") or "").strip()
    answer = _display_answer_text(answer, options)
    if answer:
        sections.append("### 답변\n" + _answer_markdown(answer))

    result_table_section = "" if _contains_markdown_table(answer) or not options["result_table"] else _result_table_section(payload)
    optional_sections = []
    if options["analysis_evidence"]:
        optional_sections.extend([_step_outputs_section(payload), _function_case_results_section(payload)])
    optional_sections.append(result_table_section)
    if options["download_links"]:
        optional_sections.append(_download_links_section(payload, download_base_url))
    if options["notices"]:
        optional_sections.append(_notice_section(payload))
    for section in optional_sections:
        if section:
            sections.append(section)

    for section in _diagnostic_sections(payload, options):
        if section:
            sections.append(section)

    if sections:
        return "\n\n".join(sections)
    return json.dumps(payload, ensure_ascii=False, default=str)


def _message_sections_from_answer_sections(
    payload: dict[str, Any],
    answer_sections: dict[str, Any],
    download_base_url: Any = "",
    options: dict[str, bool] | None = None,
) -> list[str]:
    options = options or _message_options(False, True, True, True, True, "", "", "", True, True)
    sections: list[str] = []
    summary = answer_sections.get("summary") if isinstance(answer_sections.get("summary"), dict) else {}
    answer = str(summary.get("headline") or payload.get("answer_message") or "").strip()
    answer = _display_answer_text(answer, options)
    if answer:
        sections.append("### 답변\n" + _answer_markdown(answer))

    if options["result_table"] and not _contains_markdown_table(answer):
        result_table = _result_table_section_from_answer_sections(answer_sections, payload)
        if result_table:
            sections.append(result_table)

    if options["applied_criteria"]:
        applied = _applied_criteria_section_from_answer_sections(answer_sections)
        if applied:
            sections.append(applied)

    optional_sections = []
    if options["analysis_evidence"]:
        optional_sections.extend([_step_outputs_section(payload), _function_case_results_section(payload)])
    if options["download_links"]:
        optional_sections.append(_download_links_section(payload, download_base_url))
    if options["notices"]:
        optional_sections.append(_notice_section_from_answer_sections(answer_sections))
    if options["next_questions"]:
        optional_sections.append(_next_questions_section_from_answer_sections(answer_sections))
    for section in optional_sections:
        if section:
            sections.append(section)
    return sections


def _result_table_section_from_answer_sections(answer_sections: dict[str, Any], payload: dict[str, Any] | None = None) -> str:
    result_table = answer_sections.get("result_table") if isinstance(answer_sections.get("result_table"), dict) else {}
    rows = result_table.get("display_rows")
    if not isinstance(rows, list) or not rows:
        rows = result_table.get("rows")
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
    if not isinstance(rows, list) or not rows:
        rows = data.get("rows")
    rows = rows if isinstance(rows, list) else []
    columns = result_table.get("columns") if isinstance(result_table.get("columns"), list) else []
    if not columns and isinstance(data.get("columns"), list):
        columns = data.get("columns")
    display_columns = _string_list(result_table.get("display_columns"))
    column_labels = _dict_value(result_table.get("column_labels"))
    row_count = _safe_int(result_table.get("row_count"), len(rows))
    preview_limit = _safe_int(result_table.get("preview_limit"), TABLE_PREVIEW_LIMIT)

    if not rows and not columns:
        return ""
    if not columns:
        columns = _columns_from_rows(rows)
    columns = _display_columns(columns, rows, display_columns)
    if not rows:
        column_text = ", ".join(str(column) for column in columns) if columns else "없음"
        return "### 결과 테이블\n표시할 결과 행이 없습니다.\n\n- 컬럼: `" + column_text + "`"

    preview_rows = rows[:preview_limit]
    note = f"\n\n총 {row_count}건 중 {len(preview_rows)}건을 표시했습니다."
    if row_count <= len(preview_rows):
        note = f"\n\n총 {row_count}건입니다."
    return "### 결과 테이블\n" + _markdown_table(preview_rows, columns, column_labels) + note


def _applied_criteria_section_from_answer_sections(answer_sections: dict[str, Any]) -> str:
    criteria = answer_sections.get("applied_criteria") if isinstance(answer_sections.get("applied_criteria"), dict) else {}
    if not criteria:
        return ""
    lines = ["### 적용 기준"]
    for label, key in (
        ("사용 데이터", "datasets"),
        ("조회 필수 조건", "required_params"),
        ("분석 조건", "analysis_filters"),
        ("조회 단계 필터", "retrieval_filters"),
        ("그룹 기준", "group_by"),
        ("계산 지표", "metrics"),
    ):
        value = criteria.get(key)
        if value not in (None, "", [], {}):
            lines.extend(_criteria_display_lines(label, value))
    return "\n".join(lines) if len(lines) > 1 else ""


def _criteria_display_lines(label: str, value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    lines = [f"**{label}**"]
    lines.extend(f"- {item}" for item in _criteria_items(value))
    return lines


def _criteria_items(value: Any) -> list[str]:
    if isinstance(value, dict):
        items: list[str] = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            items.append(f"{key}: {_criteria_item_text(item)}")
        return items or [_criteria_item_text(value)]
    if isinstance(value, list):
        items = [_criteria_item_text(item) for item in value if item not in (None, "", [], {})]
        return items or [_criteria_item_text(value)]
    return [_criteria_item_text(value)]


def _criteria_item_text(value: Any) -> str:
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            pairs.append(f"{key}={_display_value(item)}")
        return ", ".join(pairs) if pairs else "{}"
    if isinstance(value, list):
        return ", ".join(_display_value(item) for item in value)
    return _display_value(value)


def _notice_section_from_answer_sections(answer_sections: dict[str, Any]) -> str:
    notices = answer_sections.get("notices")
    notices = notices if isinstance(notices, list) else []
    if not notices:
        return ""
    lines = ["### 참고"]
    for item in notices[:8]:
        if isinstance(item, dict):
            message = str(item.get("message") or item.get("type") or "").strip()
        else:
            message = str(item or "").strip()
        if message:
            lines.append(f"- {_escape_markdown_tilde(message)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _next_questions_section_from_answer_sections(answer_sections: dict[str, Any]) -> str:
    questions = answer_sections.get("next_questions")
    questions = [str(item).strip() for item in questions if str(item or "").strip()] if isinstance(questions, list) else []
    if not questions:
        return ""
    lines = ["### 다음에 볼 만한 질문"]
    lines.extend(f"- {_escape_markdown_tilde(question)}" for question in questions[:3])
    return "\n".join(lines)


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _contains_markdown_table(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines()]
    for index in range(len(lines) - 1):
        if "|" not in lines[index] or "|" not in lines[index + 1]:
            continue
        divider = lines[index + 1].replace("|", "").replace(":", "").replace("-", "").strip()
        if not divider:
            return True
    return False


def _answer_markdown(text: Any) -> str:
    return _escape_markdown_tilde(_readable_answer_text(str(text or "").strip()))


def _display_answer_text(text: str, options: dict[str, bool]) -> str:
    disabled_headings: list[str] = []
    if not options.get("result_table"):
        disabled_headings.extend(["결과 테이블", "결과표"])
    if not options.get("analysis_evidence"):
        disabled_headings.extend(["분석 과정 요약", "분석 근거", "중간 분석 산출물", "helper 실행 결과", "Helper 실행 결과"])
    if not options.get("download_links"):
        disabled_headings.extend(["데이터 다운로드", "다운로드"])
    if not options.get("notices"):
        disabled_headings.extend(["경고/오류", "경고", "오류", "참고"])
    if not options.get("applied_criteria"):
        disabled_headings.append("적용 기준")
    if not options.get("next_questions"):
        disabled_headings.extend(["다음에 볼 만한 질문", "다음 질문"])
    if not options.get("intent_analysis"):
        disabled_headings.append("의도 분석")
    if not options.get("data_retrieval"):
        disabled_headings.append("데이터 조회")
    if not options.get("pandas_code"):
        disabled_headings.extend(["pandas 코드/실행", "pandas 코드", "Pandas 코드", "PANDAS 코드"])
    return _strip_markdown_sections(text, disabled_headings)


def _strip_markdown_sections(text: str, headings: list[str]) -> str:
    if not text or not headings:
        return text
    normalized_headings = {_normalize_heading(heading) for heading in headings if str(heading or "").strip()}
    if not normalized_headings:
        return text
    kept: list[str] = []
    skipping = False
    skip_level = 0
    for line in str(text).splitlines():
        heading = _markdown_heading(line)
        if heading:
            level, title = heading
            if _normalize_heading(title) in normalized_headings:
                skipping = True
                skip_level = level
                continue
            if skipping and level <= skip_level:
                skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def _markdown_heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", str(line or ""))
    if not match:
        return None
    title = re.sub(r"\s+#*$", "", match.group(2)).strip()
    return len(match.group(1)), title


def _normalize_heading(value: Any) -> str:
    return re.sub(r"[\s`*_:\-/]+", "", str(value or "").strip()).lower()


def _readable_answer_text(text: str) -> str:
    clean = re.sub(r"[ \t]+", " ", str(text or "").strip())
    if not clean:
        return ""
    if "\n" in clean or _contains_markdown_table(clean):
        return clean
    sentences = _split_sentences(clean)
    if len(sentences) >= 3:
        return "\n\n".join(sentences)
    if len(clean) <= 180:
        return clean
    if len(sentences) <= 2:
        return clean
    return "\n\n".join(sentences)


def _split_sentences(text: str) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+(?=\S)", text) if item.strip()]
    return sentences if sentences else [text]


def _result_table_section(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    columns = data.get("columns") if isinstance(data.get("columns"), list) else []
    display_columns = _string_list(data.get("display_columns"))
    column_labels = _dict_value(data.get("column_labels"))
    row_count = int(data.get("row_count") or len(rows) or 0)

    if not rows and not columns and not data:
        return ""
    if not columns:
        columns = _columns_from_rows(rows)
    columns = _display_columns(columns, rows, display_columns)
    if not rows:
        column_text = ", ".join(str(column) for column in columns) if columns else "없음"
        return "### 결과 테이블\n표시할 결과 행이 없습니다.\n\n- 컬럼: `" + column_text + "`"

    preview_rows = rows[:TABLE_PREVIEW_LIMIT]
    note = f"\n\n총 {row_count}건 중 {len(preview_rows)}건을 표시했습니다."
    if row_count <= len(preview_rows):
        note = f"\n\n총 {row_count}건입니다."
    return "### 결과 테이블\n" + _markdown_table(preview_rows, columns, column_labels) + note


def _step_outputs_section(payload: dict[str, Any]) -> str:
    outputs = _analysis_items(payload, "step_outputs")
    if not outputs:
        return ""
    lines = ["### 중간 분석 산출물"]
    for item in outputs[:6]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("description") or item.get("key") or item.get("role") or "중간 결과").strip()
        row_count = item.get("row_count")
        columns = item.get("columns") if isinstance(item.get("columns"), list) else []
        display_columns = _string_list(item.get("display_columns"))
        column_labels = _dict_value(item.get("column_labels"))
        preview_rows = item.get("preview_rows") if isinstance(item.get("preview_rows"), list) else []
        lines.append(f"- {label}: 행 수 `{_display_value(row_count)}`")
        if columns:
            lines.append(f"  - 컬럼: `{_display_value(columns)}`")
        if preview_rows:
            lines.append(_markdown_table(preview_rows[:3], _display_columns(columns, preview_rows, display_columns), column_labels))
    return "\n".join(lines)


def _function_case_results_section(payload: dict[str, Any]) -> str:
    results = _analysis_items(payload, "function_case_results")
    if not results:
        return ""
    lines = ["### helper 실행 결과"]
    seen_previews: set[str] = set()
    for item in results[:6]:
        if not isinstance(item, dict):
            continue
        function_name = str(item.get("function_name") or "function_case").strip()
        input_text = str(item.get("input_text") or "").strip()
        description = str(item.get("description") or "").strip()
        matched_count = item.get("matched_count", item.get("row_count"))
        columns = item.get("columns") if isinstance(item.get("columns"), list) else []
        preview_rows = item.get("preview_rows") if isinstance(item.get("preview_rows"), list) else []
        display_columns = _string_list(item.get("display_columns"))
        if function_name == "match_product_tokens" and not display_columns:
            display_columns = _function_case_product_columns(columns, preview_rows)
        column_labels = _dict_value(item.get("column_labels"))
        compact_rows, compact_columns = _compact_function_case_preview(preview_rows, columns, display_columns)
        dedupe_key = json.dumps({"function_name": function_name, "input_text": input_text, "rows": compact_rows}, ensure_ascii=False, sort_keys=True, default=str)
        if dedupe_key in seen_previews:
            continue
        seen_previews.add(dedupe_key)
        display_count = matched_count if matched_count not in (None, "") else len(compact_rows)
        label = description or function_name
        lines.append("")
        lines.append(f"**{_escape_markdown_tilde(label)}**")
        if input_text:
            lines.append(f"- 입력: `{_escape_markdown_tilde(input_text)}`")
        lines.append(f"- 전체 매칭: `{_display_value(display_count)}`건")
        if compact_rows:
            preview_count = len(compact_rows[:3])
            lines.append(f"- 미리보기: `{preview_count}`건 표시")
            lines.append("")
            lines.append(_markdown_table(compact_rows[:3], compact_columns, column_labels))
    return "\n".join(lines)


def _function_case_product_columns(columns: list[Any], rows: list[Any]) -> list[str]:
    existing = [str(column) for column in columns if str(column or "").strip()]
    if not existing:
        existing = _columns_from_rows(rows)
    priority = [
        "TECH",
        "DENSITY",
        "DEN",
        "MODE",
        "ORG",
        "PKG1",
        "PKG_TYPE1",
        "PKG2",
        "PKG_TYPE2",
        "LEAD",
        "MCP_NO",
        "DEVICE",
        "DEVICE_DESC",
        "OPER_NAME",
        "WIP",
        "PRODUCTION",
    ]
    return [column for column in priority if column in existing]


def _compact_function_case_preview(rows: list[Any], columns: list[Any], display_columns: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    existing = [str(column) for column in columns if str(column or "").strip()]
    if not existing:
        existing = _columns_from_rows(rows)
    preferred = [str(column) for column in (display_columns or []) if str(column or "").strip()]
    compact_columns = [column for column in preferred if column in existing] if preferred else _display_columns(existing, rows, [])
    if not compact_columns:
        compact_columns = existing or _columns_from_rows(rows)
    seen: set[tuple[Any, ...]] = set()
    compact_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        compact_row = {column: row.get(column, "") for column in compact_columns if column in row}
        key = tuple(compact_row.get(column, "") for column in compact_columns)
        if key in seen:
            continue
        seen.add(key)
        compact_rows.append(compact_row)
    return compact_rows, compact_columns


def _analysis_items(payload: dict[str, Any], key: str) -> list[Any]:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    items = analysis.get(key)
    if isinstance(items, list) and items:
        return items
    pandas_trace = _inspection(payload).get("pandas_execution")
    pandas_trace = pandas_trace if isinstance(pandas_trace, dict) else {}
    items = pandas_trace.get(key)
    return items if isinstance(items, list) else []


def _intent_section(payload: dict[str, Any]) -> str:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    metadata_refs = payload.get("metadata_refs") if isinstance(payload.get("metadata_refs"), list) else []
    inspection = _inspection(payload).get("intent")
    intent_trace = inspection if isinstance(inspection, dict) else {}
    if not plan and not metadata_refs and not intent_trace:
        return ""

    retrieval_jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    pandas_plan = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    lines = ["### 의도 분석"]
    for label, value in (
        ("분석 유형", plan.get("analysis_kind") or intent_trace.get("analysis_kind")),
        ("조회 작업 수", intent_trace.get("retrieval_job_count") if "retrieval_job_count" in intent_trace else len(retrieval_jobs)),
        ("pandas 단계 수", intent_trace.get("pandas_step_count") if "pandas_step_count" in intent_trace else len(pandas_plan)),
        ("참조 메타데이터", metadata_refs),
    ):
        if value not in (None, "", [], {}):
            lines.append(f"- {label}: `{_display_value(value)}`")

    reasons = _intent_decision_reasons(plan, intent_trace)
    if reasons:
        lines.append("- 의도 판단 근거:")
        for index, reason in enumerate(reasons[:8], start=1):
            lines.append(f"  {index}. {_display_text(reason)}")

    if retrieval_jobs:
        lines.append("- 조회 계획:")
        for job in retrieval_jobs[:8]:
            lines.append("  - " + _retrieval_job_label(job))

    if pandas_plan:
        lines.append("- pandas 실행 계획:")
        for index, step in enumerate(pandas_plan[:8], start=1):
            lines.append(f"  {index}. {_display_value(step)}")

    return "\n".join(lines)


def _intent_decision_reasons(plan: dict[str, Any], intent_trace: dict[str, Any]) -> list[Any]:
    raw_reasons = _list_value(intent_trace.get("decision_reason")) or _list_value(plan.get("decision_reason"))
    if raw_reasons and not _looks_like_english_reasons(raw_reasons):
        return raw_reasons
    derived = _derived_korean_intent_reasons(plan, intent_trace)
    return derived or raw_reasons


def _looks_like_english_reasons(reasons: list[Any]) -> bool:
    texts = [str(reason or "") for reason in reasons if str(reason or "").strip()]
    if not texts:
        return False
    combined = " ".join(texts)
    latin_count = len(re.findall(r"[A-Za-z]", combined))
    hangul_count = len(re.findall(r"[가-힣]", combined))
    english_markers = (
        "the ",
        "user ",
        "asking ",
        "follow-up",
        "followup",
        "previous ",
        "filter ",
        "query ",
        "retrieval",
        "strategy",
        "condition",
    )
    lower = combined.lower()
    return latin_count > max(hangul_count * 2, 20) and any(marker in lower for marker in english_markers)


def _derived_korean_intent_reasons(plan: dict[str, Any], intent_trace: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    request_scope = str(plan.get("request_scope") or intent_trace.get("request_scope") or "").strip()
    reuse_strategy = str(plan.get("reuse_strategy") or intent_trace.get("reuse_strategy") or "").strip()
    condition_resolution = _dict_value(plan.get("condition_resolution"))
    retrieval_jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    pandas_plan = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []

    if request_scope.startswith("followup"):
        reasons.append("현재 질문은 이전 대화의 조건을 참고해야 하는 후속 질문으로 판단했습니다.")
    elif request_scope:
        reasons.append("현재 질문은 이전 조건을 필수로 상속하지 않는 새 분석 요청으로 판단했습니다.")

    if reuse_strategy == "previous_intent_with_new_retrieval":
        reasons.append("이전 의도 계획을 바탕으로 조건을 반영한 새 데이터 조회를 수행하도록 설정했습니다.")
    elif reuse_strategy == "previous_source":
        reasons.append("이전 원본 데이터를 재사용해 추가 분석 또는 컬럼 확장을 수행하도록 설정했습니다.")
    elif reuse_strategy == "previous_result":
        reasons.append("이전 결과 테이블을 재사용해 재정렬, 재그룹화, 추가 계산을 수행하도록 설정했습니다.")
    elif reuse_strategy == "trace_only":
        reasons.append("새 조회 없이 이전 의도와 실행 근거를 설명하는 요청으로 설정했습니다.")

    for label, key in (
        ("상속한 조건", "inherited"),
        ("변경한 조건", "changed"),
        ("추가한 조건", "new"),
        ("제외한 조건", "dropped"),
    ):
        value = condition_resolution.get(key)
        if value not in (None, "", [], {}):
            reasons.append(f"{label}: {_display_value(value)}")

    required_params = _retrieval_required_params_summary(retrieval_jobs)
    if required_params:
        reasons.append(f"조회 필수 파라미터는 {required_params}로 설정했습니다.")

    filters = _retrieval_filters_summary(retrieval_jobs)
    if filters:
        reasons.append(f"분석 필터는 {filters}로 설정했습니다.")

    datasets = _retrieval_dataset_summary(retrieval_jobs)
    if datasets:
        reasons.append(f"조회 데이터셋은 {datasets}입니다.")

    group_columns = _group_columns_summary(pandas_plan)
    if group_columns:
        reasons.append(f"pandas 분석에서는 {group_columns} 기준으로 집계 또는 구분하도록 계획했습니다.")

    return _dedupe_texts(reasons)[:8]


def _retrieval_dataset_summary(retrieval_jobs: list[Any]) -> str:
    datasets = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        dataset = str(job.get("dataset_key") or "").strip()
        alias = str(job.get("source_alias") or "").strip()
        if dataset and alias and dataset != alias:
            datasets.append(f"{dataset}({alias})")
        elif dataset or alias:
            datasets.append(dataset or alias)
    return ", ".join(_dedupe_texts(datasets))


def _retrieval_required_params_summary(retrieval_jobs: list[Any]) -> str:
    parts = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        params = _dict_value(job.get("required_params"))
        if alias and params:
            parts.append(f"{alias}: {_display_value(params)}")
    return "; ".join(parts)


def _retrieval_filters_summary(retrieval_jobs: list[Any]) -> str:
    parts = []
    for job in retrieval_jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        filters = _dict_value(job.get("filters"))
        if alias and filters:
            parts.append(f"{alias}: {_display_value(filters)}")
    return "; ".join(parts)


def _group_columns_summary(pandas_plan: list[Any]) -> str:
    columns = []
    for step in pandas_plan:
        if not isinstance(step, dict):
            continue
        for key in ("groupby_columns", "group_by", "group_by_columns", "group_columns"):
            value = step.get(key)
            if isinstance(value, list):
                columns.extend(str(item) for item in value if str(item or "").strip())
            elif isinstance(value, str) and value.strip():
                columns.append(value.strip())
    return ", ".join(_dedupe_texts(columns))


def _dedupe_texts(items: list[Any]) -> list[str]:
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _retrieval_section(payload: dict[str, Any]) -> str:
    source_results = payload.get("source_results") if isinstance(payload.get("source_results"), list) else []
    retrieval_trace = _inspection(payload).get("data_retrieval")
    retrieval_trace = retrieval_trace if isinstance(retrieval_trace, dict) else {}
    if not source_results and not retrieval_trace:
        return ""

    lines = ["### 데이터 조회"]
    for label, value in (
        ("상태", retrieval_trace.get("status")),
        ("실행 소스 수", retrieval_trace.get("executed_source_count")),
        ("스킵 소스", retrieval_trace.get("skipped_sources")),
    ):
        if value not in (None, "", [], {}):
            lines.append(f"- {label}: `{_display_value(value)}`")

    sources = source_results or retrieval_trace.get("sources")
    if isinstance(sources, list) and sources:
        lines.append("- 조회 결과:")
        for source in sources[:8]:
            lines.append("  - " + _source_result_label(source))
    return "\n".join(lines)


def _pandas_section(payload: dict[str, Any]) -> str:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    inspection = _inspection(payload)
    pandas_trace = inspection.get("pandas_execution")
    pandas_trace = pandas_trace if isinstance(pandas_trace, dict) else {}
    repair_trace = inspection.get("pandas_repair")
    repair_trace = repair_trace if isinstance(repair_trace, dict) else {}
    if not analysis and not pandas_trace:
        return ""

    execution_result = pandas_trace.get("execution_result") if isinstance(pandas_trace.get("execution_result"), dict) else {}
    lines = ["### pandas 코드/실행"]
    for label, value in (
        ("상태", pandas_trace.get("status") or analysis.get("status")),
        ("결과 행 수", execution_result.get("row_count") if "row_count" in execution_result else analysis.get("row_count")),
        ("결과 컬럼", execution_result.get("columns") or analysis.get("columns")),
        ("pandas 필터 전처리", pandas_trace.get("pandas_filter_plan")),
    ):
        if value not in (None, "", [], {}):
            lines.append(f"- {label}: `{_display_value(value)}`")

    error = pandas_trace.get("error") or analysis.get("error")
    if error not in (None, "", [], {}):
        lines.append(f"- 실행 오류: `{_display_value(error)}`")

    safe_imports = pandas_trace.get("safe_import_normalization")
    if isinstance(safe_imports, dict) and safe_imports.get("removed_imports"):
        lines.append(f"- 허용 import 정규화: `{_display_value(safe_imports)}`")

    if repair_trace:
        lines.append("- Repair 상태:")
        for label, value in (
            ("시도", repair_trace.get("attempted")),
            ("LLM 호출", repair_trace.get("llm_called")),
            ("선택 결과", repair_trace.get("selected")),
            ("사유", repair_trace.get("reason")),
            ("최초 오류", repair_trace.get("initial_error")),
            ("재시도 오류", repair_trace.get("retry_error")),
            ("Repair 호출 오류", repair_trace.get("repair_error")),
        ):
            if value not in (None, "", [], {}):
                lines.append(f"  - {label}: `{_display_value(value)}`")

    used_helpers = pandas_trace.get("used_helpers") or analysis.get("used_helpers")
    if used_helpers not in (None, "", [], {}):
        lines.append(f"- 사용 helper: `{_display_value(used_helpers)}`")

    effective_code = str(pandas_trace.get("effective_code_with_helpers") or analysis.get("effective_code_with_helpers") or "").strip()
    code = effective_code or str(pandas_trace.get("generated_code") or analysis.get("analysis_code") or "").strip()
    pandas_code_json = analysis.get("pandas_code_json") if isinstance(analysis.get("pandas_code_json"), dict) else {}
    if not code:
        code = str(pandas_code_json.get("code") or "").strip()
    if code:
        label = "실제 실행 pandas 코드" if effective_code else "생성된 pandas 코드"
        lines.append(f"- {label}:")
        lines.append("```python\n" + code + "\n```")

    return "\n".join(lines)


def _notice_section(payload: dict[str, Any]) -> str:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    warnings = _list_value(trace.get("warnings")) + _list_value(payload.get("warnings"))
    errors = _list_value(trace.get("errors")) + _list_value(payload.get("errors"))
    if not warnings and not errors:
        return ""

    lines = ["### 경고/오류"]
    if warnings:
        lines.append("- 경고:")
        for item in warnings[:12]:
            lines.append(f"  - {_display_value(item)}")
    if errors:
        lines.append("- 오류:")
        for item in errors[:12]:
            lines.append(f"  - {_display_value(item)}")
    return "\n".join(lines)


def _download_links_section(payload: dict[str, Any], download_base_url: Any = "") -> str:
    refs = _downloadable_data_refs(payload)
    if not refs:
        return ""
    lines = ["### 데이터 다운로드"]
    for ref in refs[:12]:
        label = _download_label(ref)
        url = _download_url(ref, download_base_url)
        lines.append(f"- [{_escape_markdown_tilde(label)}]({url})")
    lines.append("- 링크는 MongoDB result store의 `data_ref`를 웹 다운로드 화면으로 여는 용도입니다.")
    return "\n".join(lines)


def _downloadable_data_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for ref in _list_value(payload.get("data_refs")):
        if isinstance(ref, dict):
            _append_ref(refs, ref)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data_ref = data.get("data_ref")
    if isinstance(data_ref, dict):
        _append_ref(refs, data_ref)
    return refs


def _append_ref(refs: list[dict[str, Any]], ref: dict[str, Any]) -> None:
    ref_id = str(ref.get("ref_id") or "").strip()
    if not ref_id:
        return
    signature = "|".join(str(ref.get(key) or "") for key in ("ref_id", "path", "role", "source_alias"))
    if any("|".join(str(existing.get(key) or "") for key in ("ref_id", "path", "role", "source_alias")) == signature for existing in refs):
        return
    refs.append(ref)


def _download_label(ref: dict[str, Any]) -> str:
    label = str(ref.get("label") or "").strip()
    if label:
        return label + " CSV 다운로드"
    role = str(ref.get("role") or "").strip()
    alias = str(ref.get("source_alias") or ref.get("dataset_key") or "").strip()
    if role == "source_rows" and alias:
        return f"사용 원본 데이터 {alias} CSV 다운로드"
    if role == "analysis_result":
        return "분석 결과 데이터 CSV 다운로드"
    return "저장 데이터 CSV 다운로드"


def _download_url(ref: dict[str, Any], download_base_url: Any = "") -> str:
    base_url = str(download_base_url or "").strip() or DEFAULT_DOWNLOAD_BASE_URL
    token = base64.urlsafe_b64encode(json.dumps(ref, ensure_ascii=False, default=str).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{base_url.rstrip('/')}/?download_ref={token}"


def _diagnostic_sections(payload: dict[str, Any], options: dict[str, bool]) -> list[str]:
    sections = []
    if options.get("intent_analysis"):
        sections.append(_intent_section(payload))
    if options.get("data_retrieval"):
        sections.append(_retrieval_section(payload))
    if options.get("pandas_code"):
        sections.append(_pandas_section(payload))
    return sections


def _inspection(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection")
    return inspection if isinstance(inspection, dict) else {}


def _retrieval_job_label(job: Any) -> str:
    if not isinstance(job, dict):
        return _display_value(job)
    parts = []
    for label, key in (
        ("데이터셋", "dataset_key"),
        ("소스 별칭", "source_alias"),
        ("소스 유형", "source_type"),
        ("조회 파라미터", "required_params"),
        ("조회 필터", "filters"),
    ):
        value = job.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{label}={_display_value(value)}")
    return ", ".join(parts) if parts else _display_value(job)


def _source_result_label(source: Any) -> str:
    if not isinstance(source, dict):
        return _display_value(source)
    execution = source.get("source_execution") if isinstance(source.get("source_execution"), dict) else {}
    parts = []
    for label, key in (
        ("데이터셋", "dataset_key"),
        ("소스 별칭", "source_alias"),
        ("소스 유형", "source_type"),
        ("상태", "status"),
        ("행 수", "row_count"),
        ("data_ref", "data_ref"),
        ("적용 파라미터", "applied_params"),
        ("pandas 필터", "pandas_filters"),
    ):
        value = source.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{label}={_display_value(value)}")
    legacy_filters = source.get("applied_filters")
    if legacy_filters not in (None, "", [], {}):
        parts.append(f"pandas 필터={_display_value(legacy_filters)}")
    if execution.get("used_dummy_data") not in (None, "", [], {}):
        parts.append(f"더미 사용={_display_value(execution.get('used_dummy_data'))}")
    if source.get("errors") not in (None, "", [], {}):
        parts.append(f"오류={_display_value(source.get('errors'))}")
    return ", ".join(parts) if parts else _display_value(source)


def _markdown_table(rows: list[Any], columns: list[Any], column_labels: dict[str, Any] | None = None) -> str:
    cleaned_columns = [str(column) for column in columns if str(column or "").strip()]
    if not cleaned_columns:
        cleaned_columns = _columns_from_rows(rows)
    header = "| " + " | ".join(_escape_table_cell(_display_column_label(column, column_labels)) for column in cleaned_columns) + " |"
    divider = "| " + " | ".join("---" for _ in cleaned_columns) + " |"
    body = []
    for row in rows:
        row_dict = row if isinstance(row, dict) else {}
        body.append("| " + " | ".join(_escape_table_cell(row_dict.get(column, "")) for column in cleaned_columns) + " |")
    return "\n".join([header, divider] + body)


def _display_columns(columns: list[Any], rows: list[Any], preferred_columns: list[str] | None = None) -> list[str]:
    existing = [str(column) for column in columns if str(column or "").strip()]
    if not existing:
        existing = _columns_from_rows(rows)
    preferred = [str(column) for column in (preferred_columns or []) if str(column or "").strip()]
    ordered = [column for column in preferred if column in existing]
    ordered.extend(column for column in existing if column not in ordered)
    return ordered


def _display_column_label(column: Any, column_labels: dict[str, Any] | None = None) -> str:
    text = str(column or "")
    labels = column_labels or {}
    label = labels.get(text)
    return str(label) if label not in (None, "") else text


def _columns_from_rows(rows: list[Any]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def _escape_table_cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        formatted = _format_display_number(value)
        text = str(formatted) if formatted is not None else ("" if value is None else str(value))
    text = _truncate(text.replace("\n", "<br>"), CELL_TEXT_LIMIT)
    return _escape_markdown_tilde(text.replace("|", "\\|"))


def _escape_markdown_tilde(text: str) -> str:
    return re.sub(r"(?<!\\)~", r"\\~", text)


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "예" if value else "아니오"
    formatted_number = _format_display_number(value)
    if formatted_number is not None:
        return formatted_number
    if isinstance(value, str):
        return _truncate(value.strip(), VALUE_TEXT_LIMIT)
    if isinstance(value, (list, dict)):
        return _truncate(json.dumps(value, ensure_ascii=False, default=str), VALUE_TEXT_LIMIT)
    return str(value)


def _format_display_number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number != number:
        return None
    if abs(number) >= 10000:
        k_value = number / 1000
        return f"{int(k_value):,}K" if float(k_value).is_integer() else f"{k_value:,.1f}K"
    return f"{int(number):,}" if float(number).is_integer() else f"{number:,.1f}"


def _display_text(value: Any) -> str:
    if isinstance(value, str):
        return _escape_markdown_tilde(value.strip())
    return "`" + _display_value(value) + "`"


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "예", "사용", "표시"}


def _message_options(
    include_diagnostics: Any,
    show_result_table: Any,
    show_analysis_evidence: Any,
    show_download_links: Any,
    show_notices: Any,
    show_intent_analysis: Any,
    show_data_retrieval: Any,
    show_pandas_code: Any,
    show_applied_criteria: Any,
    show_next_questions: Any,
) -> dict[str, bool]:
    diagnostics_default = _truthy(include_diagnostics)
    return {
        "result_table": _option_enabled(show_result_table, True),
        "analysis_evidence": _option_enabled(show_analysis_evidence, True),
        "download_links": _option_enabled(show_download_links, True),
        "notices": _option_enabled(show_notices, True),
        "applied_criteria": _option_enabled(show_applied_criteria, True),
        "next_questions": _option_enabled(show_next_questions, True),
        "intent_analysis": diagnostics_default or _option_enabled(show_intent_analysis, False),
        "data_retrieval": diagnostics_default or _option_enabled(show_data_retrieval, False),
        "pandas_code": diagnostics_default or _option_enabled(show_pandas_code, False),
    }


def _option_enabled(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return bool(default)
    return _truthy(value)


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class AnswerMessageAdapter(Component):
    display_name = "21 답변 메시지 어댑터"
    description = "최종 답변과 결과 테이블을 서비스 채팅 출력용 메시지로 변환합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(
            name="download_base_url",
            display_name="다운로드 링크 Base URL",
            value=DEFAULT_DOWNLOAD_BASE_URL,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="include_diagnostics",
            display_name="개발자 진단 포함",
            value=False,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_result_table",
            display_name="결과 테이블 표시",
            value=True,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_analysis_evidence",
            display_name="중간 산출물/helper 결과 표시",
            value=False,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_download_links",
            display_name="다운로드 링크 표시",
            value=True,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_notices",
            display_name="경고/참고 표시",
            value=True,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_applied_criteria",
            display_name="적용 기준 표시",
            value=True,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_next_questions",
            display_name="다음 질문 표시",
            value=True,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_intent_analysis",
            display_name="의도 분석 표시",
            value=False,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_data_retrieval",
            display_name="데이터 조회 진단 표시",
            value=False,
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="show_pandas_code",
            display_name="pandas 코드 표시",
            value=False,
            required=False,
            advanced=True,
        ),
    ]
    outputs = [Output(name="message", display_name="메시지", method="build_output_message", types=["Message"])]

    # Langflow 출력 함수: '메시지 (message)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_message(self) -> Message:
        return Message(
            text=build_message(
                getattr(self, "payload", None),
                getattr(self, "download_base_url", ""),
                getattr(self, "include_diagnostics", False),
                getattr(self, "show_result_table", True),
                getattr(self, "show_analysis_evidence", False),
                getattr(self, "show_download_links", True),
                getattr(self, "show_notices", True),
                getattr(self, "show_intent_analysis", False),
                getattr(self, "show_data_retrieval", False),
                getattr(self, "show_pandas_code", False),
                getattr(self, "show_applied_criteria", True),
                getattr(self, "show_next_questions", True),
            )
        )
