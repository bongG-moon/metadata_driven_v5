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
)
EXPLAIN_CUES = ("왜", "이유", "근거", "설명", "어떤 조건", "어떤 데이터", "조회 조건", "pandas", "코드")
TRANSFORM_CUES = ("상위", "하위", "정렬", "나눠", "분리", "제품별", "공정별", "차수별", "세부", "비율", "rank", "top", "bottom")
EXPAND_CUES = ("추가", "넣어", "붙여", "포함", "같이", "함께", "컬럼", "열", "항목", "code", "코드", "번호")
CHANGE_CUES = ("말고", "대신", "아니", "바꿔", "변경", "다른", "새로", "다시 조회", "재조회")
DATE_CUE_PATTERN = re.compile(r"(\b\d{1,2}/\d{1,2}\b|\b\d{1,2}월\s*\d{1,2}일\b|오늘|금일|어제|전일|내일|현시간|현재)")


def build_followup_hint(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    question = str(_dict(payload.get("request")).get("question") or "").strip()
    state = _dict(payload.get("state"))
    previous_context = _previous_context(state)
    has_previous = bool(previous_context.get("has_previous_context"))

    date_hint = _date_change_hint(question, _dict(payload.get("request")).get("reference_date"))
    matched_references = _matched_cues(question, FOLLOWUP_REFERENCE_CUES)
    matched_explain = _matched_cues(question, EXPLAIN_CUES)
    matched_transform = _matched_cues(question, TRANSFORM_CUES)
    matched_expand = _matched_cues(question, EXPAND_CUES)
    matched_change = _matched_cues(question, CHANGE_CUES)
    requested_columns = _matched_previous_columns(question, _available_previous_columns(state), matched_expand)
    context_dependent = _looks_context_dependent(question)

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
        elif matched_change or (date_hint and (matched_references or context_dependent)):
            scope_hint = "followup_requery"
            reuse_strategy_hint = "previous_intent_with_new_retrieval"
            confidence = "high" if matched_references or _looks_context_dependent(question) else "medium"
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
                }
            ),
            "requested_columns_hint": requested_columns,
            "required_previous_artifacts": required_artifacts,
            "inheritance_candidates": inheritance_candidates,
            "previous_context": previous_context,
            "notes": _notes(scope_hint, reuse_strategy_hint, requested_columns, date_hint),
        }
    )
    next_payload = deepcopy(payload)
    next_payload["followup_hint"] = hint
    next_payload.setdefault("trace", {}).setdefault("inspection", {})["followup_hint"] = {
        "stage": "01e_followup_hint_builder",
        **hint,
    }
    return next_payload


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


def _date_change_hint(question: str, reference_date: Any) -> dict[str, Any]:
    text = str(question or "")
    if not DATE_CUE_PATTERN.search(text):
        return {}
    ref_date = _parse_reference_date(reference_date)
    date_value = ""
    if "어제" in text or "전일" in text:
        date_value = (ref_date - timedelta(days=1)).strftime("%Y%m%d") if ref_date else "yesterday"
    elif "오늘" in text or "금일" in text or "현시간" in text or "현재" in text:
        date_value = ref_date.strftime("%Y%m%d") if ref_date else "today"
    elif "내일" in text:
        date_value = (ref_date + timedelta(days=1)).strftime("%Y%m%d") if ref_date else "tomorrow"
    explicit = _explicit_date(text, ref_date)
    if explicit:
        date_value = explicit
    return _omit_empty({"expression": _date_expression(text), "resolved_value": date_value})


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


def _date_expression(text: str) -> str:
    match = DATE_CUE_PATTERN.search(text)
    return match.group(0) if match else ""


def _parse_reference_date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d")
    except Exception:
        return None


def _matched_previous_columns(question: str, columns: list[str], expand_cues: list[str]) -> list[str]:
    if not columns or not expand_cues:
        return []
    normalized_question = _normalize(question)
    result = []
    for column in columns:
        if any(_normalize(alias) in normalized_question for alias in _column_aliases(column)):
            result.append(column)
    return result


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


def _looks_context_dependent(question: str) -> bool:
    text = str(question or "")
    compact = re.sub(r"\s+", "", text)
    if any(token in text.lower() for token in ("공정", "제품", "장비", "lot", "device", "데이터셋", "dataset")):
        return False
    return len(compact) <= 18


def _matched_cues(question: str, cues: tuple[str, ...]) -> list[str]:
    normalized_question = _normalize(question)
    return [cue for cue in cues if _normalize(cue) in normalized_question]


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
        notes.append("날짜/기준시점 변경 표현을 감지했습니다.")
    return notes


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _compact_source_columns(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _string_list(columns)[:80] for key, columns in value.items() if str(key or "").strip() and _string_list(columns)}


def _normalize(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


def _clip_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit] if len(text) > limit else text


def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


class FollowupHintBuilder(Component):
    display_name = "01E 후속 질문 힌트 생성기"
    description = "질문과 이전 state를 보고 후속 질문 가능성, 조건 변경, 이전 데이터 재사용 힌트를 생성합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=build_followup_hint(getattr(self, "payload", None)))
