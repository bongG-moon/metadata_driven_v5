# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01E 후속 질문 힌트 생성기
# 역할: 질문과 이전 state를 보고 후속 질문 가능성, 조건 변경, 이전 데이터 재사용 힌트를 생성합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 이전 분석 상태를 작게 압축하고 날짜·지표·그룹 조건의 상속/변경 가능성을 후속 질문 힌트로 만듭니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

FOLLOWUP_REFERENCE_CUES = (
    "그", "이전", "방금", "위", "아까", "저 결과", "결과에서", "표에서", "여기서", "거기서",
    "같은 조건", "동일 조건", "그 제품", "그 공정", "해당 제품", "해당 공정",
    "이날", "이 날", "이 일자", "이 날짜", "그날", "그 날", "그 일자", "그 날짜", "해당 일자", "같은 날", "동일 일자",
)
EXPLAIN_CUES = ("왜", "이유", "근거", "설명", "어떤 조건", "어떤 데이터", "조회 조건", "pandas", "코드")
TRANSFORM_CUES = ("상위", "하위", "정렬", "나눠", "분리", "제품별", "공정별", "차수별", "세부", "비율", "rank", "top", "bottom")
EXPAND_CUES = ("추가", "넣어", "붙여", "포함", "같이", "함께", "컬럼", "열", "항목", "code", "코드", "번호")
CHANGE_CUES = ("말고", "대신", "아니", "바꿔", "변경", "다른", "새로", "다시 조회", "재조회")
ENTITY_SWITCH_CUES = ("에서는", "에선", "은 어때", "는 어때", "은 어땠", "는 어땠", "쪽은", "경우는")
STANDALONE_REQUEST_CUES = (
    "알려줘", "알려 줘", "조회해", "조회 해", "보여줘", "보여 줘", "찾아줘", "찾아 줘",
    "확인해", "확인 해", "계산해", "계산 해", "분석해", "분석 해", "구해줘", "구해 줘",
)
ANALYSIS_SUBJECT_CUES = (
    "재공", "wip", "생산량", "생산 실적", "실적", "production", "uph", "장비", "설비",
    "equipment", "eqp", "계획", "target", "hold", "홀드", "lot", "랏", "로트",
)
DATE_CUE_PATTERN = re.compile(r"(\b20\d{6}\b|\b\d{1,2}/\d{1,2}\b|\b\d{1,2}월\s*\d{1,2}일\b|오늘|금일|어제|전일|내일|현시간|현재)")
CONTEXT_DATE_CUE_PATTERN = re.compile(r"(이\s*일자|이\s*날짜|이\s*날|그\s*일자|그\s*날짜|그\s*날|해당\s*일자|같은\s*날|동일\s*일자)")


# 주요 함수: 현재 질문과 이전 상태를 비교해 상속·변경·제거 가능 조건을 힌트로 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_followup_hint(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    question = str(_dict(payload.get("request")).get("question") or "").strip()
    state = _dict(payload.get("state"))
    previous_context = _previous_context(state)
    has_previous = bool(previous_context.get("has_previous_context"))

    date_hint = _date_change_hint(question, _dict(payload.get("request")).get("reference_date"), state)
    matched_references = _matched_cues(question, FOLLOWUP_REFERENCE_CUES)
    matched_explain = _matched_cues(question, EXPLAIN_CUES)
    matched_transform = _matched_cues(question, TRANSFORM_CUES)
    matched_expand = _matched_cues(question, EXPAND_CUES)
    matched_change = _matched_cues(question, CHANGE_CUES)
    matched_entity_switch = _matched_cues(question, ENTITY_SWITCH_CUES)
    requested_columns = _matched_previous_columns(question, _available_previous_columns(state), matched_expand)
    context_dependent = _looks_context_dependent(question)
    entity_switch_followup = _looks_entity_switch_followup(question, matched_entity_switch)
    complete_independent_request = _looks_complete_independent_request(
        question,
        matched_references=matched_references,
        matched_change=matched_change,
        date_hint=date_hint,
    )

    scope_hint = "new_analysis"
    reuse_strategy_hint = "none"
    confidence = "low"
    required_artifacts: list[str] = []
    inheritance_candidates: list[str] = []

    if has_previous:
        if matched_explain and (matched_references or context_dependent):
            scope_hint = "followup_explain"
            reuse_strategy_hint = "trace_only"
            confidence = "medium" if matched_references else "low"
            required_artifacts = ["previous_trace", "previous_intent_plan", "previous_answer"]
        elif requested_columns or (matched_expand and (matched_references or context_dependent)):
            scope_hint = "followup_expand_source"
            reuse_strategy_hint = "previous_source"
            confidence = "high" if requested_columns or matched_references else "medium"
            required_artifacts = ["previous_source", "previous_result", "previous_intent_plan"]
            inheritance_candidates = ["metric", "required_params", "analysis_filters", "group_by", "pandas_function_cases"]
        elif matched_change or (
            date_hint
            and (matched_references or context_dependent)
            and not complete_independent_request
        ) or entity_switch_followup:
            scope_hint = "followup_requery"
            reuse_strategy_hint = "previous_intent_with_new_retrieval"
            confidence = "high" if matched_references or entity_switch_followup or _looks_context_dependent(question) else "medium"
            required_artifacts = ["previous_intent_plan", "previous_applied_criteria"]
            inheritance_candidates = ["metric", "analysis_filters", "group_by", "pandas_function_cases", "output_contract"]
        elif matched_transform and (matched_references or context_dependent):
            scope_hint = "followup_transform"
            reuse_strategy_hint = "previous_result"
            confidence = "high" if matched_references else "medium"
            required_artifacts = ["previous_result", "previous_source", "previous_intent_plan"]
            inheritance_candidates = ["required_params", "analysis_filters", "pandas_function_cases"]
        elif matched_references:
            scope_hint = "followup_requery"
            reuse_strategy_hint = "previous_intent_with_new_retrieval"
            confidence = "medium"
            required_artifacts = ["previous_result", "previous_intent_plan", "previous_applied_criteria"]
            inheritance_candidates = ["metric", "required_params", "analysis_filters", "group_by", "pandas_function_cases"]

    hint = _omit_empty(
        {
            "followup_candidate": scope_hint != "new_analysis",
            "request_scope_hint": scope_hint,
            "reuse_strategy_hint": reuse_strategy_hint,
            "confidence": confidence,
            "changed_conditions_hint": _omit_empty({"date": date_hint}) if date_hint else {},
            "matched_cues": _omit_empty(
                {
                    "reference": matched_references,
                    "explain": matched_explain,
                    "transform": matched_transform,
                    "expand": matched_expand,
                    "change": matched_change,
                    "entity_switch": matched_entity_switch,
                }
            ),
            "requested_columns_hint": requested_columns,
            "required_previous_artifacts": required_artifacts,
            "inheritance_candidates": inheritance_candidates,
            "previous_context": previous_context,
            "complete_independent_request": complete_independent_request,
            "notes": _notes(scope_hint, reuse_strategy_hint, requested_columns, date_hint),
        }
    )
    next_payload = payload
    next_payload["followup_hint"] = hint
    next_payload.setdefault("trace", {}).setdefault("inspection", {})["followup_hint"] = {
        "stage": "01e_followup_hint_builder",
        **hint,
    }
    return next_payload


# 함수 설명: `_previous_context()`는 이전 질문·의도·조건·결과 컬럼에서 후속 질문 판단에 필요한 문맥만 추출합니다.
def _previous_context(state: dict[str, Any]) -> dict[str, Any]:
    current_data = _dict(state.get("current_data"))
    last_intent = _dict(state.get("last_intent_plan"))
    last_applied = _dict(state.get("last_applied_criteria"))
    source_columns = _compact_source_columns(current_data.get("source_columns_by_alias"))
    columns = _string_list(current_data.get("columns")) or _string_list(current_data.get("result_columns"))
    return _omit_empty(
        {
            "has_previous_context": bool(current_data or last_intent or last_applied or state.get("last_question")),
            "last_question": state.get("last_question") or _dict(state.get("request")).get("question"),
            "last_answer_message": _clip_text(state.get("last_answer_message"), 700),
            "current_data": _omit_empty(
                {
                    "row_count": current_data.get("row_count"),
                    "columns": columns[:60],
                    "data_ref": current_data.get("data_ref"),
                    "source_aliases": _string_list(current_data.get("source_aliases"))[:20],
                    "source_dataset_keys": _string_list(current_data.get("source_dataset_keys"))[:20],
                    "source_columns_by_alias": source_columns,
                }
            ),
            "last_intent_plan": _compact_intent_plan(last_intent),
            "last_applied_criteria": _compact_applied_criteria(last_applied),
        }
    )


# 함수 설명: `_compact_intent_plan()`는 의도 계획·PLAN에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_intent_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "analysis_kind": plan.get("analysis_kind"),
            "retrieval_jobs": _compact_retrieval_jobs(_list(plan.get("retrieval_jobs"))),
            "pandas_execution_plan": deepcopy(_list(plan.get("pandas_execution_plan"))[:8]),
            "pandas_function_cases": deepcopy(_list(plan.get("pandas_function_cases"))[:5]),
            "output_contract": deepcopy(_dict(plan.get("output_contract"))),
        }
    )


# 함수 설명: `_compact_retrieval_jobs()`는 데이터 조회·조회 작업에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_retrieval_jobs(jobs: list[Any]) -> list[dict[str, Any]]:
    result = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        result.append(
            _omit_empty(
                {
                    "dataset_key": job.get("dataset_key"),
                    "source_alias": job.get("source_alias"),
                    "source_type": job.get("source_type"),
                    "required_params": deepcopy(job.get("required_params")),
                    "filters": deepcopy(job.get("filters")),
                }
            )
        )
    return result


# 함수 설명: `_compact_applied_criteria()`는 적용 조건·적용 기준에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_applied_criteria(criteria: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "required_params": deepcopy(_dict(criteria.get("required_params"))),
            "analysis_filters": deepcopy(_dict(criteria.get("analysis_filters"))),
            "retrieval_filters": deepcopy(_dict(criteria.get("retrieval_filters"))),
            "group_by": _string_list(criteria.get("group_by")),
            "metrics": _string_list(criteria.get("metrics")),
            "datasets": deepcopy(_list(criteria.get("datasets"))[:10]),
        }
    )


# 함수 설명: `_date_change_hint()`는 change·힌트 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _date_change_hint(question: str, reference_date: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    text = str(question or "")
    explicit_match = DATE_CUE_PATTERN.search(text)
    context_match = CONTEXT_DATE_CUE_PATTERN.search(text)
    if not explicit_match and not context_match:
        return {}
    if context_match and not explicit_match:
        return _previous_context_date_hint(context_match.group(0), _dict(state))
    ref_date = _parse_reference_date(reference_date)
    mentions = _date_mentions(text, ref_date)
    resolved_values = []
    for mention in mentions:
        value = str(mention.get("resolved_value") or "")
        if value and value not in resolved_values:
            resolved_values.append(value)
    if len(resolved_values) <= 1:
        return _omit_empty(
            {
                "expression": str(mentions[0].get("expression") or "") if mentions else "",
                "resolved_value": resolved_values[0] if resolved_values else "",
            }
        )
    return {"scope": "multiple", "mentions": mentions}


# 함수 설명: `_previous_context_date_hint()`는 `이날`, `이 일자`처럼 직전 분석을 가리키는 표현을 오늘 날짜가 아니라 이전 DATE 조건으로 해석합니다.
def _previous_context_date_hint(expression: str, state: dict[str, Any]) -> dict[str, Any]:
    candidates = _previous_date_candidates(state)
    unique_values: list[str] = []
    for item in candidates:
        value = str(item.get("resolved_value") or "").strip()
        if value and value not in unique_values:
            unique_values.append(value)
    if len(unique_values) == 1:
        return {
            "expression": expression,
            "resolved_value": unique_values[0],
            "source": "previous_context",
            "inherit": True,
        }
    if len(unique_values) > 1:
        return {
            "expression": expression,
            "scope": "previous_context_multiple",
            "source": "previous_context",
            "candidates": candidates,
            "requires_clarification": True,
        }
    return {
        "expression": expression,
        "source": "previous_context",
        "requires_clarification": True,
    }


# 함수 설명: `_previous_date_candidates()`는 직전 조회 job과 실제 적용 조건에서 source alias별 DATE 값을 중복 없이 수집합니다.
def _previous_date_candidates(state: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    # 함수 설명: `append()`는 source alias와 YYYYMMDD 조합을 중복 없이 이전 날짜 후보에 추가합니다.
    def append(alias: Any, value: Any) -> None:
        text = str(value or "").strip()
        if not re.fullmatch(r"20\d{6}", text):
            return
        source_alias = str(alias or "").strip()
        marker = (source_alias, text)
        if marker in seen:
            return
        seen.add(marker)
        item = {"resolved_value": text}
        if source_alias:
            item["source_alias"] = source_alias
        result.append(item)

    applied = _dict(state.get("last_applied_criteria"))
    required_params = _dict(applied.get("required_params"))
    direct_date = required_params.get("DATE")
    if direct_date not in (None, ""):
        append("", direct_date)
    for alias, params in required_params.items():
        if isinstance(params, dict):
            append(alias, params.get("DATE"))

    last_plan = _dict(state.get("last_intent_plan"))
    for job in _list(last_plan.get("retrieval_jobs")):
        if not isinstance(job, dict):
            continue
        alias = job.get("source_alias") or job.get("dataset_key")
        append(alias, _dict(job.get("required_params")).get("DATE"))
    return result


# 함수 설명: `_date_mentions()`는 질문에 등장한 날짜 표현을 순서대로 모두 해석해 metric/dataset별 바인딩 근거를 제공합니다.
def _date_mentions(text: str, ref_date: datetime | None) -> list[dict[str, Any]]:
    mentions = []
    for match in DATE_CUE_PATTERN.finditer(text):
        expression = match.group(0)
        resolved_value = _resolve_date_expression(expression, ref_date)
        mentions.append(
            _omit_empty(
                {
                    "expression": expression,
                    "resolved_value": resolved_value,
                    "position": match.start(),
                }
            )
        )
    return mentions


# 함수 설명: `_resolve_date_expression()`은 한 날짜 표현만 기준일에 대해 YYYYMMDD 또는 상대 토큰으로 변환합니다.
def _resolve_date_expression(expression: str, ref_date: datetime | None) -> str:
    text = str(expression or "")
    if re.fullmatch(r"20\d{6}", text):
        return text
    if text in {"어제", "전일"}:
        return (ref_date - timedelta(days=1)).strftime("%Y%m%d") if ref_date else "yesterday"
    if text in {"오늘", "금일", "현시간", "현재"}:
        return ref_date.strftime("%Y%m%d") if ref_date else "today"
    if text == "내일":
        return (ref_date + timedelta(days=1)).strftime("%Y%m%d") if ref_date else "tomorrow"
    return _explicit_date(text, ref_date)


# 함수 설명: `_explicit_date()`는 사용자 질문에 명시된 절대/상대 날짜 표현을 찾아 표준 날짜로 해석합니다.
def _explicit_date(text: str, ref_date: datetime | None) -> str:
    slash = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    korean = re.search(r"\b(\d{1,2})월\s*(\d{1,2})일\b", text)
    match = slash or korean
    if not match:
        return ""
    year = ref_date.year if ref_date else datetime.now().year
    month = int(match.group(1))
    day = int(match.group(2))
    try:
        return datetime(year, month, day).strftime("%Y%m%d")
    except Exception:
        return ""


# 함수 설명: `_date_expression()`는 expression 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _date_expression(text: str) -> str:
    match = DATE_CUE_PATTERN.search(text)
    return match.group(0) if match else ""


# 함수 설명: `_parse_reference_date()`는 복합 입력이나 응답에서 reference·날짜을 찾아 검증 가능한 기본 Python 값으로 변환합니다.
def _parse_reference_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d")
    except Exception:
        return None


# 함수 설명: `_matched_previous_columns()`는 입력 조건과 일치하는 이전 값·컬럼을 찾아 비교·필터 결과로 반환합니다.
def _matched_previous_columns(question: str, columns: list[str], expand_cues: list[str]) -> list[str]:
    if not columns or not expand_cues:
        return []
    normalized_question = _normalize(question)
    result = []
    for column in columns:
        if any(_normalize(alias) in normalized_question for alias in _column_aliases(column)):
            result.append(column)
    return result


# 함수 설명: `_available_previous_columns()`는 이전 값·컬럼 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _available_previous_columns(state: dict[str, Any]) -> list[str]:
    current_data = _dict(state.get("current_data"))
    columns: list[str] = []
    _extend_unique(columns, _string_list(current_data.get("columns")))
    _extend_unique(columns, _string_list(current_data.get("result_columns")))
    source_columns = current_data.get("source_columns_by_alias")
    if isinstance(source_columns, dict):
        for value in source_columns.values():
            _extend_unique(columns, _string_list(value))
    for source in _list(state.get("followup_source_results")):
        if isinstance(source, dict):
            _extend_unique(columns, _string_list(source.get("columns")))
    return columns


# 함수 설명: `_column_aliases()`는 aliases 관련 정보를 계산·선별해 후속 분석 또는 표시 단계에 전달합니다.
def _column_aliases(column: str) -> list[str]:
    raw = str(column or "").strip()
    if not raw:
        return []
    aliases = {raw, raw.replace("_", " "), raw.replace("_", "-")}
    upper = raw.upper()
    for suffix in ("_NO", "_CODE", "_CD", "_ID"):
        if upper.endswith(suffix):
            base = raw[: -len(suffix)].strip("_- ")
            if base:
                aliases.update({base, f"{base} no", f"{base} number", f"{base} code", f"{base} 코드", f"{base} 번호"})
    return sorted(aliases, key=len, reverse=True)


# 함수 설명: `_looks_context_dependent()`는 입력값이 문맥·dependent 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _looks_context_dependent(question: str) -> bool:
    text = str(question or "")
    compact = re.sub(r"\s+", "", text)
    if any(token in text.lower() for token in ("공정", "제품", "장비", "lot", "device", "데이터셋", "dataset")):
        return False
    return len(compact) <= 18


# 함수 설명: `WB공정에서는 어땠어?`처럼 새 엔티티만 말하고 이전 지표·날짜를 묻는 후속 질문을 감지합니다.
def _looks_entity_switch_followup(question: str, matched_entity_switch: list[str]) -> bool:
    text = str(question or "")
    normalized = _normalize(text)
    if not normalized or not matched_entity_switch:
        return False
    has_entity = any(_normalize(token) in normalized for token in ("공정", "제품", "장비", "설비", "lot", "랏", "로트"))
    has_open_question = any(_normalize(token) in normalized for token in ("어때", "어땠", "어떻게", "어떤가"))
    has_new_analysis_subject = any(_normalize(cue) in normalized for cue in ANALYSIS_SUBJECT_CUES)
    has_request = any(_normalize(cue) in normalized for cue in STANDALONE_REQUEST_CUES)
    # 지표와 요청 동사가 모두 명시된 완결 질문은 새 분석 후보로 남기고,
    # 엔티티와 비교 표현만 있는 짧은 질문만 이전 조건을 상속하도록 힌트를 줍니다.
    return has_entity and has_open_question and not (has_new_analysis_subject and has_request)


# 함수 설명: 날짜가 있어도 지표와 요청 동사가 모두 명시된 완결 질문은 이전 세션에 의존하지 않는 새 분석으로 판정합니다.
def _looks_complete_independent_request(
    question: str,
    *,
    matched_references: list[str],
    matched_change: list[str],
    date_hint: dict[str, Any],
) -> bool:
    text = _normalize(question)
    if not text:
        return False
    if matched_references or matched_change:
        return False
    if date_hint.get("source") == "previous_context":
        return False
    has_request = any(_normalize(cue) in text for cue in STANDALONE_REQUEST_CUES)
    has_subject = any(_normalize(cue) in text for cue in ANALYSIS_SUBJECT_CUES)
    return has_request and has_subject


# 함수 설명: `_matched_cues()`는 입력 조건과 일치하는 CUES을 찾아 비교·필터 결과로 반환합니다.
def _matched_cues(question: str, cues: tuple[str, ...]) -> list[str]:
    normalized_question = _normalize(question)
    return [cue for cue in cues if _normalize(cue) in normalized_question]


# 함수 설명: `_notes()`는 후속 질문 해석에서 사용자에게 알릴 조건 상속·변경 주의사항을 구성합니다.
def _notes(scope_hint: str, reuse_strategy: str, requested_columns: list[str], date_hint: dict[str, Any]) -> list[str]:
    notes = []
    if scope_hint == "new_analysis":
        notes.append("독립 질문으로 보이며 이전 조건 상속은 필수로 판단하지 않았습니다.")
    else:
        notes.append("후속 질문 가능성이 있으므로 이전 조건의 상속/변경 여부를 의도 분석에서 최종 판단해야 합니다.")
    if reuse_strategy == "previous_intent_with_new_retrieval":
        notes.append("이전 intent의 metric/filter/group 조건을 검토하고, 변경 조건이 required_params에 영향을 주면 새 조회를 생성합니다.")
    if reuse_strategy == "previous_source":
        notes.append("이전 최종 결과만으로 부족하면 이전 원본 source data_ref를 복원해 pandas 재분석합니다.")
    if requested_columns:
        notes.append("이전 데이터 컬럼에서 사용자가 다시 보고 싶어 하는 컬럼 후보를 찾았습니다: " + ", ".join(requested_columns))
    if date_hint:
        if date_hint.get("source") == "previous_context":
            notes.append("'이날/이 일자' 표현은 오늘이 아니라 직전 분석의 DATE 조건을 상속합니다.")
        else:
            notes.append("날짜/기준시점 변경 표현을 감지했습니다.")
    return notes


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_extend_unique()`는 대상 목록에 새 문자열을 중복 없이 원래 순서대로 추가합니다.
def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


# 함수 설명: `_compact_source_columns()`는 데이터 소스·컬럼에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_source_columns(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _string_list(columns)[:80] for key, columns in value.items() if str(key or "").strip() and _string_list(columns)}


# 함수 설명: `_normalize()`는 normalize의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다.
def _normalize(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


# 함수 설명: `_clip_text()`는 문자열을 허용 길이 안으로 자르되 비어 있는 값과 말줄임 표시를 일관되게 처리합니다.
def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


# 함수 설명: `_omit_empty()`는 dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.
def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class FollowupHintBuilder(Component):
    display_name = "01E 후속 질문 힌트 생성기"
    description = "질문과 이전 state를 보고 후속 질문 가능성, 조건 변경, 이전 데이터 재사용 힌트를 생성합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_followup_hint(getattr(self, "payload", None)))
