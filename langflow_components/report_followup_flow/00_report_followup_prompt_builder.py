# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 Report 후속 질문 Prompt 생성기
# 역할: 같은 세션의 Report anchor와 Report가 직접 선언한 query source만 사용해 작은 계획 Prompt를 만듭니다.
# 주요 입력: 사용자 질문, 공용 Session State Loader가 반환한 compact state, Prompt Template
# 주요 출력: 정규화 전 요청 payload, Intent LLM용 Message
# 처리 흐름: state/context 검증 -> materialized view 후보 선택 -> bounded schema 직렬화 -> Prompt 생성
# 유지보수 포인트: Table Catalog/Main Filter/전역 컬럼 alias를 읽지 않으며 Report의 실제 물리 컬럼만 전달합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


REQUEST_CONTRACT_VERSION = "report.followup.request.v1"
QUERY_SOURCE_CONTRACT_VERSION = "report.query_source.v1"
MAX_QUERY_SOURCES = 12
MAX_COLUMNS_PER_SOURCE = 160
MAX_QUESTION_CHARS = 4_000
SAFE_SOURCE_ALIAS = re.compile(r"^[A-Za-z0-9_-]+$")
FRESH_DATA_PHRASES = (
    "현재 기준",
    "현재 데이터",
    "지금 시점",
    "최신 데이터",
    "다시 조회",
    "재조회",
    "새로 조회",
    "원천 조회",
)
CROSS_SOURCE_PHRASES = (
    "다른 데이터",
    "외부 데이터",
    "원천 데이터",
    "추가 조회",
    "조회해서",
    "조회하여",
    "데이터와 비교",
    "데이터랑 비교",
    "데이터를 결합",
    "데이터와 결합",
    "조인해서",
)
RESULT_CONTINUATION_MARKERS = ("그중", "위 결과", "해당 결과")
KNOWN_VIEW_ALIASES = {
    "case_detail": ["Report 원본", "Report 상세", "원본 판정 데이터", "케이스 상세"],
    "production_shortage_products": ["생산부족 제품", "생산 부족 제품", "부족 제품"],
}
DEFAULT_PROMPT_TEMPLATE = """당신은 저장된 Report 내부 데이터를 조회하는 제한형 계획기입니다.

사용자 질문:
{question}

선택 가능한 Report query source 계약:
{query_sources_json}

직전 View 계획:
{current_view_plan_json}

직전 결과 이어가기:
{inherit_current_view_json}

규칙:
- query source는 위 목록에서 정확히 하나만 선택합니다.
- source_alias와 column은 제공된 실제 문자열을 그대로 사용합니다.
- 전역 데이터셋, Table Catalog, Main Filter, 표준 컬럼명 또는 새 컬럼명을 만들지 않습니다.
- 외부 조회, join, groupby, 계산식, 자유 pandas/Python 코드는 만들지 않습니다.
- 허용 operation은 filter, sort, top_n, select뿐입니다.
- materialized view의 predicates는 이미 적용된 조건이므로 중복 filter로 만들지 않습니다.
- 직전 결과 이어가기가 true이면 직전 계획은 서버가 합성하므로 이번 질문에서 새로 요구한 차이 연산만 반환합니다.
- null 정렬은 항상 last입니다.
- 질문에서 요구하지 않은 filter, sort, limit을 추가하지 않습니다.
- 처리할 수 없으면 status=clarification_required와 reason을 반환합니다.

다음 JSON object 하나만 반환합니다.
{{
  "status": "ready | clarification_required",
  "source_alias": "선택한 source_alias",
  "operations": [
    {{"operation": "filter", "conditions": [{{"column": "실제 컬럼", "operator": "eq", "value": "질문에 명시된 값"}}]}},
    {{"operation": "sort", "column": "실제 컬럼", "direction": "asc | desc", "nulls": "last"}},
    {{"operation": "top_n", "limit": 5}},
    {{"operation": "select", "columns": ["실제 컬럼"]}}
  ],
  "reason": "짧은 판단 근거"
}}
"""


# 함수 설명: 입력 값을 공백이 제거된 문자열로 정규화합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: Langflow Data 또는 dict에서 안전한 payload 사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return deepcopy(raw) if isinstance(raw, dict) else {}


# 함수 설명: Message와 문자열 입력에서 길이가 제한된 사용자 질문을 추출합니다.
def _question(value: Any) -> str:
    data = getattr(value, "data", None)
    data = data if isinstance(data, dict) else {}
    text = _text(getattr(value, "text", None) or data.get("text") or data.get("question"))
    if not text and isinstance(value, str):
        text = value.strip()
    return text[:MAX_QUESTION_CHARS]


# 함수 설명: Session Loader 출력에서 상태와 로드 상태를 분리합니다.
def _state(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _payload(value)
    state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
    load_status = payload.get("session_state_load") if isinstance(payload.get("session_state_load"), dict) else {}
    return deepcopy(state) if isinstance(state, dict) else {}, deepcopy(load_status)


# 함수 설명: 호환 가능한 상태 키에서 세션 식별자를 찾습니다.
def _session_id(state: dict[str, Any]) -> str:
    for key in ("session_id", "conversation_id", "chat_id", "thread_id"):
        if _text(state.get(key)):
            return _text(state.get(key))
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    return _text(request.get("session_id"))


# 함수 설명: 세션 상태의 낙관적 잠금 revision을 검증해 반환합니다.
def _session_revision(state: dict[str, Any]) -> int | None:
    value = state.get("_session_state_revision")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


# 함수 설명: 중복과 빈 값을 제거한 제한 길이 문자열 목록을 만듭니다.
def _string_list(value: Any, limit: int = MAX_COLUMNS_PER_SOURCE) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = _text(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


# 함수 설명: View 별칭 비교에 사용할 정규화 문구를 만듭니다.
def _normalized_phrase(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", _text(value)).casefold()


# 함수 설명: 스냅샷 범위를 벗어나는 최신 조회 또는 교차 원천 요청을 탐지합니다.
def _requires_live_retrieval(question: str) -> bool:
    """Detect explicit refresh/cross-source requests without matching column names.

    In particular, ``현재작업재공`` is a physical snapshot column and must not
    be mistaken for the standalone freshness cue ``현재``.
    """

    lowered = question.casefold()
    if any(phrase.casefold() in lowered for phrase in (*FRESH_DATA_PHRASES, *CROSS_SOURCE_PHRASES)):
        return True
    standalone = re.compile(r"(?<![0-9A-Za-z가-힣_])(현재|지금|최신)(?![0-9A-Za-z가-힣_])", re.IGNORECASE)
    return bool(standalone.search(question))


# 함수 설명: 레거시 source alias에 대응하는 기본 Report 용도를 결정합니다.
def _default_purpose(alias: str) -> str:
    lowered = alias.casefold()
    if "shortage" in lowered and "product" in lowered:
        return "production_shortage_products"
    return "case_detail" if alias == "report_snapshot" else alias


# 함수 설명: Report 계약의 허용 연산 이름을 제한형 연산 집합으로 정규화합니다.
def _normalized_allowed_operations(value: Any, fallback: Any = None) -> list[str]:
    aliases = {
        "apply_filters": "filter",
        "sort_and_top_n": "sort",
        "sort": "sort",
        "top_n": "top_n",
        "select_columns": "select",
        "select": "select",
        "filter": "filter",
    }
    result: list[str] = []
    raw = value if isinstance(value, list) and value else fallback
    for item in raw if isinstance(raw, list) else []:
        normalized = aliases.get(_text(item).casefold())
        if normalized and normalized not in result:
            result.append(normalized)
        if _text(item).casefold() == "sort_and_top_n" and "top_n" not in result:
            result.append("top_n")
    if "select" not in result:
        result.append("select")
    return result


# 함수 설명: 저장된 Report 상태에서 후속 조회용 query source 계약만 추출합니다.
def _query_source_contracts(state: dict[str, Any]) -> list[dict[str, Any]]:
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    context = current_data.get("report_context") if isinstance(current_data.get("report_context"), dict) else {}
    candidates = context.get("query_sources") if isinstance(context.get("query_sources"), list) else []
    if not candidates and isinstance(current_data.get("query_sources"), list):
        candidates = current_data["query_sources"]
    source_columns = current_data.get("source_columns_by_alias") if isinstance(current_data.get("source_columns_by_alias"), dict) else {}
    dataset_keys = _string_list(current_data.get("source_dataset_keys"), MAX_QUERY_SOURCES)
    aliases_in_state = _string_list(current_data.get("source_aliases"), MAX_QUERY_SOURCES)
    fallback_allowed = context.get("allowed_operations")
    normalized: list[dict[str, Any]] = []

    for raw_item in candidates[:MAX_QUERY_SOURCES]:
        if not isinstance(raw_item, dict):
            continue
        raw = raw_item.get("query_source_contract") if isinstance(raw_item.get("query_source_contract"), dict) else raw_item
        alias = _text(raw.get("source_alias"))
        if not alias or not SAFE_SOURCE_ALIAS.fullmatch(alias):
            continue
        purpose = _text(raw.get("purpose")) or _default_purpose(alias)
        columns = _string_list(raw.get("columns")) or _string_list(source_columns.get(alias))
        grain = raw.get("grain") if isinstance(raw.get("grain"), dict) else {}
        known_aliases = [*KNOWN_VIEW_ALIASES.get(purpose, []), *_string_list(raw.get("aliases"), 30)]
        contract = {
            "contract_version": _text(raw.get("contract_version")) or QUERY_SOURCE_CONTRACT_VERSION,
            "source_alias": alias,
            "dataset_key": _text(raw.get("dataset_key")) or alias,
            "purpose": purpose,
            "aliases": list(dict.fromkeys(known_aliases)),
            "authoritative": raw.get("authoritative") is True,
            "columns": columns,
            "grain": {
                "kind": _text(grain.get("kind")) or "detail",
                "columns": _string_list(grain.get("columns")),
            },
            "metrics": [deepcopy(item) for item in raw.get("metrics", []) if isinstance(item, dict)][:50],
            "predicates": [deepcopy(item) for item in raw.get("predicates", []) if isinstance(item, dict)][:50],
            "allowed_operations": _normalized_allowed_operations(raw.get("allowed_operations"), fallback_allowed),
            "default_display_columns": _string_list(raw.get("default_display_columns")),
        }
        if contract["contract_version"] == QUERY_SOURCE_CONTRACT_VERSION and columns:
            normalized.append(contract)

    # Additive compatibility for Report contexts created before query_sources existed.
    if not normalized:
        for index, alias in enumerate(aliases_in_state):
            columns = _string_list(source_columns.get(alias))
            if not columns:
                continue
            normalized.append(
                {
                    "contract_version": QUERY_SOURCE_CONTRACT_VERSION,
                    "source_alias": alias,
                    "dataset_key": dataset_keys[index] if index < len(dataset_keys) else alias,
                    "purpose": _default_purpose(alias),
                    "aliases": KNOWN_VIEW_ALIASES.get(_default_purpose(alias), []),
                    # Legacy fallback is visible to the planner but is not authoritative;
                    # the executor will fail closed until the stored source declares its contract.
                    "authoritative": False,
                    "columns": columns,
                    "grain": {"kind": "detail", "columns": []},
                    "metrics": [],
                    "predicates": [],
                    "allowed_operations": _normalized_allowed_operations(fallback_allowed),
                    "default_display_columns": [],
                }
            )
    return normalized[:MAX_QUERY_SOURCES]


# 함수 설명: 질문에 명시된 Report View 별칭과 가장 잘 맞는 source만 반환합니다.
def _explicit_source_matches(question: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_question = _normalized_phrase(question)
    scored: list[tuple[int, dict[str, Any]]] = []
    for source in sources:
        terms = [source.get("purpose"), source.get("source_alias"), *source.get("aliases", [])]
        matches = [len(_normalized_phrase(term)) for term in terms if _normalized_phrase(term) and _normalized_phrase(term) in normalized_question]
        if matches:
            scored.append((max(matches), source))
    if scored:
        best = max(score for score, _ in scored)
        return [source for score, source in scored if score == best]
    return []


# 함수 설명: 질문이 Report 전체가 아니라 직전 결과 subset을 가리키는지 판정합니다.
def _continues_previous_result(question: str) -> bool:
    lowered = question.casefold()
    return any(marker.casefold() in lowered for marker in RESULT_CONTINUATION_MARKERS)


# 함수 설명: 명시 별칭, 직전 결과 참조, Report 원본 순서로 source 후보를 좁힙니다.
def _matched_sources(question: str, sources: list[dict[str, Any]], current_view_plan: dict[str, Any]) -> list[dict[str, Any]]:
    explicit = _explicit_source_matches(question, sources)
    if explicit:
        return explicit

    previous_alias = _text(current_view_plan.get("source_alias"))
    if previous_alias and _continues_previous_result(question):
        previous = [source for source in sources if source.get("source_alias") == previous_alias]
        if previous:
            return previous
    defaults = [source for source in sources if source.get("purpose") == "case_detail"]
    if len(defaults) == 1:
        return defaults
    return sources if len(sources) == 1 else []


# 함수 설명: 상태에서 직전 Report 후속 View 계획을 복원합니다.
def _current_view_plan(state: dict[str, Any]) -> dict[str, Any]:
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    if isinstance(current_data.get("current_view_plan"), dict):
        return deepcopy(current_data["current_view_plan"])
    if isinstance(state.get("current_view_plan"), dict):
        return deepcopy(state["current_view_plan"])
    last_plan = state.get("last_intent_plan") if isinstance(state.get("last_intent_plan"), dict) else {}
    return deepcopy(last_plan.get("report_execution_plan")) if isinstance(last_plan.get("report_execution_plan"), dict) else {}


# 함수 설명: 일관된 오류 항목을 생성합니다.
def _error(issue_type: str, message: str) -> dict[str, str]:
    return {"type": issue_type, "message": message}


# 주요 함수: 질문과 세션 상태를 검증하고 Report 후속 요청 계약을 생성합니다.
def build_report_followup_request(question_value: Any, loaded_state_value: Any) -> dict[str, Any]:
    question = _question(question_value)
    state, load_status = _state(loaded_state_value)
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    report_context = current_data.get("report_context") if isinstance(current_data.get("report_context"), dict) else {}
    query_sources = _query_source_contracts(state)
    current_plan = _current_view_plan(state)
    session_id = _session_id(state)
    session_revision = _session_revision(state)
    errors: list[dict[str, str]] = []
    status = "ready"

    if not question:
        errors.append(_error("empty_report_followup_question", "Report 후속 질문이 비어 있습니다."))
        status = "blocked"
    elif not session_id:
        errors.append(_error("report_followup_session_missing", "현재 실행의 session_id를 확인할 수 없습니다."))
        status = "blocked"
    elif session_revision is None:
        errors.append(_error("report_session_revision_missing", "Report 후속 분석의 세션 revision을 확인할 수 없어 상태 경합 보호를 적용할 수 없습니다."))
        status = "blocked"
    elif not (_text(report_context.get("context_ref")) and _text(report_context.get("report_type"))):
        errors.append(_error("report_context_missing", "같은 세션에서 재사용 가능한 Report Context를 찾을 수 없습니다."))
        status = "blocked"
    elif not query_sources:
        errors.append(_error("report_query_sources_missing", "Report가 후속 조회용 query source를 선언하지 않았습니다."))
        status = "blocked"
    elif _requires_live_retrieval(question):
        status = "handoff_required"
        errors.append(_error("live_retrieval_required", "현재·최신 또는 다른 원천 데이터 요청은 일반 데이터 분석 Flow의 신규 조회가 필요합니다."))

    matched = _matched_sources(question, query_sources, current_plan) if status == "ready" else []
    explicit_matches = _explicit_source_matches(question, query_sources) if status == "ready" else []
    inherit_current_view = bool(
        status == "ready"
        and not explicit_matches
        and _continues_previous_result(question)
        and len(matched) == 1
        and _text(current_plan.get("source_alias")) == _text(matched[0].get("source_alias"))
    )
    if status == "ready" and not matched:
        status = "clarification_required"
        errors.append(_error("report_query_source_ambiguous", "질문에 사용할 Report View를 하나로 확정할 수 없습니다."))

    request = {
        "contract_version": REQUEST_CONTRACT_VERSION,
        "request": {
            "question": question,
            "session_id": session_id,
            "reference_date": datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d"),
        },
        "state": state,
        # 공용 Result Loader가 일반 data_ref보다 Report context_ref를 우선하고
        # Report 전용 TTL/완전성 검증을 적용하도록 명시합니다.
        "followup_hint": {
            "report_reference": bool(_text(report_context.get("context_ref"))),
            "unresolved_report_reference": not bool(_text(report_context.get("context_ref"))),
            "fresh_data_requested": status == "handoff_required",
            "request_scope_hint": "followup_transform" if status == "ready" else status,
        },
        "report_followup": {
            "status": status,
            "report_anchor": {
                "context_version": _text(report_context.get("context_version")),
                "context_ref": _text(report_context.get("context_ref")),
                "report_type": _text(report_context.get("report_type")),
                "as_of": _text(report_context.get("as_of")),
                "expires_at": _text(report_context.get("expires_at")),
                "session_revision": session_revision,
            },
            "query_sources": query_sources,
            "candidate_source_aliases": [item["source_alias"] for item in matched],
            "inherit_current_view": inherit_current_view,
            "current_view_plan": current_plan,
        },
        "intent_plan": {},
        "source_results": [],
        "runtime_sources": {},
        "analysis": {},
        "data": {},
        "execution_gate": {"status": "ready" if status == "ready" else status},
        "trace": {
            "warnings": [],
            "errors": errors,
            "inspection": {
                "report_followup_prompt": {
                    "stage": "00_report_followup_prompt_builder",
                    "status": status,
                    "session_state_loaded": load_status.get("loaded"),
                    "query_source_count": len(query_sources),
                    "candidate_source_aliases": [item["source_alias"] for item in matched],
                    "errors": deepcopy(errors),
                }
            },
        },
    }
    return request


# 주요 함수: 선택된 Report query source만 포함하는 제한형 계획 Prompt를 생성합니다.
def build_report_followup_prompt(payload_value: Any, prompt_template: Any = "") -> str:
    payload = _payload(payload_value)
    report_followup = payload.get("report_followup") if isinstance(payload.get("report_followup"), dict) else {}
    if report_followup.get("status") != "ready":
        return ""
    candidates = set(_string_list(report_followup.get("candidate_source_aliases"), MAX_QUERY_SOURCES))
    sources = [
        deepcopy(item)
        for item in report_followup.get("query_sources", [])
        if isinstance(item, dict) and (not candidates or item.get("source_alias") in candidates)
    ]
    compact_sources = []
    for item in sources:
        compact_sources.append(
            {
                "source_alias": item.get("source_alias"),
                "dataset_key": item.get("dataset_key"),
                "purpose": item.get("purpose"),
                "aliases": item.get("aliases", []),
                "columns": item.get("columns", []),
                "grain": item.get("grain", {}),
                "metrics": item.get("metrics", []),
                "predicates_already_applied": item.get("predicates", []),
                "allowed_operations": item.get("allowed_operations", []),
                "default_display_columns": item.get("default_display_columns", []),
            }
        )
    template = _text(prompt_template) or DEFAULT_PROMPT_TEMPLATE
    return template.format(
        question=_text(payload.get("request", {}).get("question")),
        query_sources_json=json.dumps(compact_sources, ensure_ascii=False, separators=(",", ":"), default=str),
        current_view_plan_json=json.dumps(report_followup.get("current_view_plan", {}), ensure_ascii=False, separators=(",", ":"), default=str),
        inherit_current_view_json=json.dumps(bool(report_followup.get("inherit_current_view"))),
    )


# Langflow 컴포넌트 클래스: Report 후속 요청 payload와 계획 Prompt를 함께 출력합니다.
class ReportFollowupPromptBuilder(Component):
    display_name = "00 Report 후속 질문 Prompt 생성기"
    description = "Report가 선언한 materialized query source만 사용해 제한형 실행 계획 Prompt를 만듭니다."
    name = "ReportFollowupPromptBuilder"
    icon = "FileSearch"
    inputs = [
        HandleInput(name="question", display_name="사용자 질문", input_types=["Message"], required=True),
        DataInput(name="loaded_state", display_name="불러온 세션 상태", required=True),
        MultilineInput(name="prompt_template", display_name="Report 후속 계획 Prompt", value=DEFAULT_PROMPT_TEMPLATE, required=False, advanced=True),
    ]
    outputs = [
        Output(name="payload_out", display_name="요청 페이로드", method="build_payload", types=["Data"], group_outputs=True),
        Output(name="prompt", display_name="계획 Prompt", method="build_prompt", types=["Message"], group_outputs=True),
    ]

    # 주요 메서드: 동일 실행 내에서 요청 payload를 한 번만 생성해 재사용합니다.
    def _request_payload(self) -> dict[str, Any]:
        return build_report_followup_request(getattr(self, "question", None), getattr(self, "loaded_state", None))

    # Langflow 출력 함수: 정규화기로 전달할 요청 payload를 Data로 반환합니다.
    def build_payload(self) -> Data:
        result = self._request_payload()
        self.status = result.get("trace", {}).get("inspection", {}).get("report_followup_prompt", {})
        return Data(data=result)

    # Langflow 출력 함수: Language Model에 전달할 계획 Prompt Message를 반환합니다.
    def build_prompt(self) -> Message:
        prompt = build_report_followup_prompt(self._request_payload(), getattr(self, "prompt_template", ""))
        message = Message(text=prompt)
        self.status = message
        return message
