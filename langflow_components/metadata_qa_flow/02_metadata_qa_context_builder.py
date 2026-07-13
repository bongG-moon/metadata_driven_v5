# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 메타데이터 QA 컨텍스트 생성기
# 역할: 질문과 MongoDB 메타데이터를 읽어 QA에 필요한 후보만 선별합니다.
# 주요 입력: 페이로드 (payload) · 필수, 도메인 메타데이터 (domain_items), 테이블 카탈로그 (table_catalog_items), 메인 필터 (main_flow_filters),
#        최대 후보 수 (max_items), 최대 Context 바이트 (max_bytes)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 질문 유형을 판정하고 비밀값을 제거한 뒤 도메인·테이블·필터 후보를 점수화·projection·바이트 제한해 QA 문맥을 만듭니다.
# 유지보수 포인트: secret/credential/raw trace를 문맥에 넣지 않고 max_items·max_bytes 제한을 넘으면 낮은 우선순위 후보부터 줄입니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

PRUNED_METADATA_KEYS = {
    "_id",
    "registration_trace",
    "raw_trace",
    "raw_text",
    "raw_text_preview",
    "refined_text",
    "review",
    "write_result",
    "llm_response",
    "existing_matches",
    "duplicate_decision",
    "created_by_prompt",
    "created_at",
    "updated_at",
    "text",
}

SECRET_KEY_PATTERNS = ("password", "passwd", "token", "secret", "api_key", "apikey", "mongo_uri", "uri")
CALCULATION_SECTIONS = {"analysis_recipes", "metric_terms", "pandas_function_cases", "calculation_rules", "quantity_terms"}
PRODUCT_DOMAIN_SECTIONS = {"product_terms"}
PRODUCT_AGGREGATION_SECTIONS = {"product_key_columns", "analysis_recipes"}
LIST_ALL_TABLE_MODES = {"available_sources"}
NO_DOMAIN_CANDIDATE_MODES = {
    "available_sources",
    "dataset_sql",
    "dataset_detail",
    "required_params",
    "data_analysis_redirect",
}
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_BYTES = 65536
DETERMINISTIC_ANSWER_MODES = {
    "available_sources",
    "dataset_detail",
    "dataset_sql",
    "required_params",
    "calculation_logic_list",
    "process_group",
    "data_analysis_redirect",
}


# 주요 함수: 질문 유형에 맞는 안전하고 작은 메타데이터 근거 문맥을 구성합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_metadata_qa_context(
    payload_value: Any,
    domain_items_value: Any = None,
    table_catalog_items_value: Any = None,
    main_flow_filters_value: Any = None,
    max_items: Any = str(DEFAULT_MAX_ITEMS),
    max_bytes: Any = str(DEFAULT_MAX_BYTES),
) -> dict[str, Any]:
    payload = _payload(payload_value)
    question = str(_dict(payload.get("request")).get("question") or "").strip()
    limit = _int(max_items, DEFAULT_MAX_ITEMS)
    byte_limit = _int(max_bytes, DEFAULT_MAX_BYTES)

    if not question:
        return _empty_question_context(payload)

    domain_items, domain_load = _extract_items(domain_items_value, "domain_items")
    table_items, table_load = _extract_items(table_catalog_items_value, "table_catalog_items")
    filter_items, filter_load = _extract_items(main_flow_filters_value, "main_flow_filters")
    domain_items = [_sanitize(item) for item in domain_items]
    table_items = [_sanitize(item) for item in table_items]
    filter_items = [_sanitize(item) for item in filter_items]

    answer_mode = _infer_answer_mode(question)
    query_scope = _infer_query_scope(question, answer_mode)
    inventory_request_kind = _inventory_request_kind(question, answer_mode)
    matched_domain = [_project_domain_item(item, answer_mode) for item in _select_domain_items(question, answer_mode, domain_items, limit)]
    matched_tables = [_project_table_item(item, answer_mode) for item in _select_table_items(question, answer_mode, table_items, limit)]
    matched_filters = [_project_filter_item(item) for item in _select_filter_items(question, answer_mode, filter_items, limit)]
    source_refs = _source_refs(matched_domain, matched_tables, matched_filters)
    candidate_rows = _candidate_rows(answer_mode, matched_domain, matched_tables, matched_filters)

    load_summary = {
        "domain_items": _compact_load(domain_load),
        "table_catalog_items": _compact_load(table_load),
        "main_flow_filters": _compact_load(filter_load),
    }
    catalog_summary = _catalog_summary(
        answer_mode,
        inventory_request_kind,
        len(table_items),
        len(candidate_rows),
        limit,
        table_load,
    )
    warnings = []
    if not source_refs:
        warnings.append({"type": "metadata_qa_no_matches", "message": "질문과 직접 매칭되는 메타데이터 후보가 없습니다."})

    next_payload = payload
    next_payload["metadata_route"] = {
        "route": "metadata_qa",
        "answer_mode": answer_mode,
        "confidence": "high" if source_refs else "low",
    }
    llm_skip = answer_mode in DETERMINISTIC_ANSWER_MODES and not (
        answer_mode == "calculation_logic_list" and query_scope.get("request_kind") == "how_to"
    )
    context = {
        "question": question,
        "answer_mode": answer_mode,
        "query_scope": query_scope,
        "load_summary": load_summary,
        "matched_domain_items": matched_domain,
        "matched_datasets": matched_tables,
        "matched_filters": matched_filters,
        "candidate_rows": candidate_rows,
        "source_refs": source_refs,
        "llm_control": {
            "skip": llm_skip,
            "eligible_to_skip": answer_mode in DETERMINISTIC_ANSWER_MODES,
            "reason": "deterministic_answer_mode" if llm_skip else "llm_synthesis_required",
        },
    }
    if catalog_summary:
        context["catalog_summary"] = catalog_summary
    if answer_mode == "available_sources":
        context["matched_datasets"] = []
    context, context_trimmed = _fit_context_bytes(context, byte_limit)
    _refresh_catalog_summary(context)
    if _json_bytes(context) > byte_limit:
        context, additionally_trimmed = _fit_context_bytes(context, byte_limit)
        context_trimmed = context_trimmed or additionally_trimmed
        _refresh_catalog_summary(context)
    final_source_refs = context.get("source_refs") if isinstance(context.get("source_refs"), list) else []
    if source_refs and not final_source_refs:
        warnings.append({"type": "metadata_qa_all_candidates_trimmed", "message": "Context 바이트 제한으로 메타데이터 후보가 모두 제외되었습니다."})
    if _json_bytes(context) > byte_limit:
        warnings.append({"type": "metadata_qa_minimum_context_exceeds_budget", "message": f"필수 Context가 설정한 {byte_limit} bytes를 초과합니다."})
    next_payload["metadata_route"]["confidence"] = "high" if final_source_refs else "low"
    if context_trimmed:
        warnings.append({"type": "metadata_qa_context_trimmed", "message": f"LLM context를 {byte_limit} bytes 이하로 축소했습니다."})
    next_payload["metadata_qa_context"] = context
    trace = _dict(next_payload.get("trace"))
    trace.setdefault("warnings", []).extend(warnings)
    trace.setdefault("errors", []).extend(_load_errors(load_summary))
    trace.setdefault("inspection", {})["metadata_qa_context"] = {
        "stage": "02_metadata_qa_context_builder",
        "status": "ok" if final_source_refs else "warning",
        "answer_mode": answer_mode,
        "domain_match_count": len(context.get("matched_domain_items")) if isinstance(context.get("matched_domain_items"), list) else 0,
        "dataset_match_count": int(_dict(context.get("catalog_summary")).get("returned_count", 0)) if answer_mode == "available_sources" else len(context.get("matched_datasets")) if isinstance(context.get("matched_datasets"), list) else 0,
        "filter_match_count": len(context.get("matched_filters")) if isinstance(context.get("matched_filters"), list) else 0,
        "context_bytes": _json_bytes(context),
        "context_trimmed": context_trimmed,
        "catalog_summary": deepcopy(_dict(context.get("catalog_summary"))),
    }
    next_payload["trace"] = trace
    return next_payload


# 함수 설명: `_empty_question_context()`는 빈 질문을 메타데이터 조회·LLM 실행 없이 종료할 수 있는 명시적 계약으로 만듭니다.
def _empty_question_context(payload: dict[str, Any]) -> dict[str, Any]:
    next_payload = payload
    next_payload["metadata_route"] = {"route": "metadata_qa", "answer_mode": "invalid_request", "confidence": "none", "status": "error"}
    next_payload["metadata_qa_context"] = {
        "question": "",
        "answer_mode": "invalid_request",
        "load_summary": {},
        "matched_domain_items": [],
        "matched_datasets": [],
        "matched_filters": [],
        "candidate_rows": [],
        "source_refs": [],
        "llm_control": {"skip": True, "reason": "empty_question"},
    }
    trace = _dict(next_payload.get("trace"))
    errors = trace.setdefault("errors", [])
    if not any(isinstance(item, dict) and item.get("type") == "empty_question" for item in errors):
        errors.append({"type": "empty_question", "message": "메타데이터 QA 질문이 비어 있습니다."})
    trace.setdefault("inspection", {})["metadata_qa_context"] = {
        "stage": "02_metadata_qa_context_builder",
        "status": "skipped",
        "reason": "empty_question",
        "context_bytes": 0,
    }
    next_payload["trace"] = trace
    return next_payload


# 함수 설명: `_extract_items()`는 복합 입력이나 응답에서 항목을 찾아 검증 가능한 기본 Python 값으로 변환합니다.
def _extract_items(value: Any, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return [], {}
    items = data.get(key)
    load = data.get("metadata_load") if isinstance(data.get("metadata_load"), dict) else {}
    return [deepcopy(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else [], deepcopy(load)


# 함수 설명: `_infer_answer_mode()`는 질문 표현을 정의·소스 목록·SQL 설명·실데이터 redirect 등 QA 답변 모드로 분류합니다.
def _infer_answer_mode(question: str) -> str:
    lowered = question.lower()
    if any(token in lowered for token in ("쿼리", "sql", "query", "select", "with문")):
        return "dataset_sql"
    if _looks_like_specific_dataset_required_params(lowered):
        return "required_params"
    if _looks_like_specific_dataset_detail(lowered):
        return "dataset_detail"
    if _looks_like_task_dataset_selection(lowered):
        return "question_to_dataset"
    # 명시적인 카탈로그 목록·건수 질문은 "현재 장비 테이블 목록"처럼 실제 값 질문과
    # 일부 단어가 겹쳐도 MongoDB 카탈로그의 결정론적 목록 경로를 우선합니다.
    if _looks_like_available_sources_question(lowered):
        return "available_sources"
    if _looks_like_data_value_question(lowered) or _looks_like_table_data_question(lowered):
        return "data_analysis_redirect"
    if any(token in lowered for token in ("필수 파라미터", "필수조건", "필수 조건", "required param", "required_param")):
        return "required_params"
    if any(token in lowered for token in ("어떤 데이터", "무슨 데이터", "어느 데이터", "어떤 테이블", "무슨 테이블", "어떤 source", "무슨 source", "어떤 소스")):
        return "question_to_dataset"
    if any(token in lowered for token in ("공정 그룹", "세부 공정", "포함", "차수", "공정에는")) and "공정" in lowered:
        return "process_group"
    if _looks_like_product_domain_question(lowered):
        return "product_domain_info"
    if _looks_like_product_aggregation_rule_question(lowered):
        return "calculation_logic_list"
    if any(token in lowered for token in ("제품 조건", "제품군", "hbm", "mobile", "pop", "tsv", "3ds")):
        return "product_condition"
    if any(token in lowered for token in ("제품 표현", "제품 token", "제품 토큰", "어떻게 찾", "매칭", "token")):
        return "product_token_rule"
    if any(token in lowered for token in ("어떤 컬럼", "무슨 컬럼", "컬럼이야", "의미", "정의", "용어")):
        return "term_definition"
    if any(token in lowered for token in ("뭐야", "무엇", "설명", "어떤 데이터야", "어떤 source야", "어떤 소스야")) and any(token in lowered for token in ("today", "history", "production", "wip", "target", "equipment", "lot", "hold", "_")):
        return "dataset_detail"
    if any(token in lowered for token in ("계산 로직", "계산", "로직", "recipe", "function", "함수")):
        return "calculation_logic_list"
    if "도메인" in lowered or "domain" in lowered:
        return "domain_info"
    return "general_metadata_search"


# 함수 설명: `_looks_like_product_domain_question()`은 제품 그룹·제품군의 등록 정보나 조건 설명을 묻는 QA 표현을 식별합니다.
def _looks_like_product_domain_question(lowered: str) -> bool:
    product_group_tokens = ("제품 그룹", "제품그룹", "제품군", "제품 조건", "product group")
    metadata_detail_tokens = (
        "도메인",
        "domain",
        "메타데이터",
        "metadata",
        "등록",
        "정보",
        "목록",
        "뭐가 있",
        "무엇이 있",
        "뭐야",
        "정의",
        "설명",
    )
    if any(token in lowered for token in product_group_tokens) and any(token in lowered for token in metadata_detail_tokens):
        return True
    return "pop" in lowered and any(token in lowered for token in ("도메인", "정의", "무엇", "뭐야", "설명"))


# 함수 설명: `_looks_like_product_aggregation_rule_question()`은 실제 값 조회가 아닌 제품 단위 집계 grain·컬럼·규칙 설명 질문을 판정합니다.
def _looks_like_product_aggregation_rule_question(lowered: str) -> bool:
    product_tokens = ("제품", "product")
    aggregation_tokens = ("집계", "그룹핑", "그루핑", "group by", "groupby", "group_by", "묶어서")
    explanation_tokens = (
        "어떻게",
        "기준",
        "규칙",
        "어떤 컬럼",
        "무슨 컬럼",
        "컬럼으로",
        "메타데이터",
        "metadata",
        "등록",
        "설명",
    )
    return (
        any(token in lowered for token in product_tokens)
        and any(token in lowered for token in aggregation_tokens)
        and any(token in lowered for token in explanation_tokens)
    )


# 함수 설명: `_infer_query_scope()`는 answer_type을 바꾸지 않고 조회 대상과 요청 형태를 구조화해 후보 선택과 LLM 설명에 제공합니다.
def _infer_query_scope(question: str, answer_mode: str) -> dict[str, str]:
    lowered = str(question or "").lower()
    if answer_mode == "product_domain_info":
        subject = "product_terms"
        aspect = "group_condition"
    elif answer_mode == "calculation_logic_list" and _looks_like_product_aggregation_rule_question(lowered):
        subject = "product_aggregation"
        aspect = "grain_and_grouping"
    else:
        subject = "general"
        aspect = ""

    if any(token in lowered for token in ("총", "몇 개", "몇개", "개수", "건수", "count")):
        request_kind = "count"
    elif any(token in lowered for token in ("목록", "list", "뭐가 있", "무엇이 있", "전부", "전체")):
        request_kind = "list"
    elif any(token in lowered for token in ("어떻게", "기준", "규칙", "컬럼으로", "group by", "groupby", "그룹핑", "그루핑")):
        request_kind = "how_to"
    else:
        request_kind = "detail"
    return _omit_empty({"subject": subject, "aspect": aspect, "request_kind": request_kind})


# 함수 설명: `_looks_like_data_value_question()`는 입력값이 LIKE·데이터·값·question 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _looks_like_data_value_question(lowered: str) -> bool:
    if any(token in lowered for token in ("메타데이터", "metadata", "등록", "정의", "무슨 컬럼", "어떤 컬럼", "쿼리", "sql", "query", "데이터셋", "필수 조건")):
        return False
    has_time_or_target = any(token in lowered for token in ("오늘", "어제", "전일", "금일", "현시간", "현재", "/", "월", "일"))
    has_metric = any(token in lowered for token in ("생산량", "생산 실적", "실적", "재공", "수량", "투입", "input", "output", "out", "assign", "장비"))
    asks_value = any(token in lowered for token in ("알려줘", "확인", "보여줘", "몇", "상위", "많은"))
    return has_metric and asks_value and has_time_or_target


# 함수 설명: `_has_specific_dataset_reference()`는 영문 dataset key나 현재 v4 카탈로그의 대표 key가 질문에 명시됐는지 판정합니다.
def _has_specific_dataset_reference(lowered: str) -> bool:
    known_dataset_keys = (
        "production_today",
        "production",
        "wip_today",
        "wip",
        "target",
        "equipment_assign",
        "eqp_uph",
        "lot_status",
        "hold_history",
    )
    if any(re.search(rf"(?<![0-9a-z_]){re.escape(key)}(?![0-9a-z_])", lowered) for key in known_dataset_keys):
        return True
    return bool(re.search(r"(?<![0-9a-z])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![0-9a-z])", lowered))


# 함수 설명: `_looks_like_specific_dataset_required_params()`는 특정 dataset의 필수 파라미터 질문을 전체 카탈로그 질문보다 먼저 식별합니다.
def _looks_like_specific_dataset_required_params(lowered: str) -> bool:
    required_tokens = ("필수 파라미터", "필수조건", "필수 조건", "required param", "required_param")
    return _has_specific_dataset_reference(lowered) and any(token in lowered for token in required_tokens)


# 함수 설명: `_looks_like_specific_dataset_detail()`은 특정 dataset의 컬럼·스키마·연결 상세 질문을 전체 목록과 구분합니다.
def _looks_like_specific_dataset_detail(lowered: str) -> bool:
    detail_tokens = (
        "컬럼",
        "column",
        "스키마",
        "schema",
        "상세",
        "설명",
        "구조",
        "연결 방식",
        "연결방식",
        "source type",
        "소스 유형",
        "어떤 데이터야",
    )
    return _has_specific_dataset_reference(lowered) and any(token in lowered for token in detail_tokens)


# 함수 설명: `_looks_like_table_data_question()`은 카탈로그 문서가 아니라 특정 테이블의 실제 행·값·건수를 묻는 요청인지 판정합니다.
def _looks_like_table_data_question(lowered: str) -> bool:
    explicit_catalog_tokens = ("메타데이터", "metadata", "테이블 카탈로그", "데이터 카탈로그", "table catalog", "data catalog")
    specific_dataset = _has_specific_dataset_reference(lowered)
    if any(token in lowered for token in explicit_catalog_tokens) and not specific_dataset:
        return False
    has_table_subject = specific_dataset or any(token in lowered for token in ("테이블", "데이터셋", "dataset", "table"))
    if not has_table_subject:
        return False
    row_value_tokens = (
        "전체 데이터",
        "원본 데이터",
        "실제 데이터",
        "데이터를 보여",
        "데이터 보여",
        "전체 행",
        "행을 보여",
        "레코드",
        "record",
        "rows",
        "row ",
        "값을 보여",
        "건수",
        "몇 건",
    )
    business_tokens = (
        "생산량",
        "생산 실적",
        "재공",
        "투입",
        "uph",
        "hold",
        "lot",
        "assign",
        "장비",
        "공정",
        "제품",
    )
    time_tokens = ("오늘", "어제", "전일", "금일", "현재", "현시간", "월", "일", "202")
    has_row_value = any(token in lowered for token in row_value_tokens)
    has_business = any(token in lowered for token in business_tokens)
    has_time = any(token in lowered for token in time_tokens)
    has_list_word = any(token in lowered for token in ("테이블 목록", "데이터 목록", "table list"))
    return (has_row_value and (specific_dataset or has_business or has_time)) or (has_list_word and has_business and has_time)


# 함수 설명: `_looks_like_available_sources_question()`는 입력값이 LIKE·사용 가능 항목·sources·question 조건에 해당하는지 부작용 없이 bool로
#        판정합니다.
def _looks_like_available_sources_question(lowered: str) -> bool:
    catalog_subject_tokens = (
        "데이터셋",
        "데이터 세트",
        "데이터 목록",
        "데이터들",
        "테이블 카탈로그",
        "데이터 카탈로그",
        "테이블",
        "data catalog",
        "table catalog",
        "dataset",
        "data source",
        "source",
        "소스",
        "연결 방식",
        "연결방식",
    )
    inventory_tokens = (
        "등록된",
        "등록되어",
        "등록한",
        "조회 가능",
        "조회가능",
        "사용 가능",
        "사용가능",
        "목록",
        "전체",
        "전부",
        "나열",
        "뭐가 있",
        "무엇이 있",
        "list",
        "총",
        "몇 개",
        "몇개",
        "개수",
        "건수",
    )
    has_subject = any(token in lowered for token in catalog_subject_tokens)
    has_inventory_intent = any(token in lowered for token in inventory_tokens)
    if not has_subject or not has_inventory_intent:
        return False

    # 구체 업무용 데이터셋 선택, 특정 dataset 상세, 실제 행 조회는 전체 카탈로그 목록이 아닙니다.
    if (
        _looks_like_task_dataset_selection(lowered)
        or _looks_like_specific_dataset_required_params(lowered)
        or _looks_like_specific_dataset_detail(lowered)
        or _looks_like_table_data_question(lowered)
    ):
        return False
    return True


# 함수 설명: `_looks_like_task_dataset_selection()`은 전체 목록이 아니라 구체 업무 질문에 사용할 데이터셋을 고르는 표현인지 판정합니다.
def _looks_like_task_dataset_selection(lowered: str) -> bool:
    selection_tokens = (
        "어떤 테이블",
        "무슨 테이블",
        "어느 테이블",
        "어떤 데이터셋",
        "무슨 데이터셋",
        "어느 데이터셋",
        "필요한 테이블",
        "필요한 데이터셋",
        "적합한 테이블",
        "적합한 데이터셋",
        "사용할 수 있는 테이블",
        "사용할 수 있는 데이터셋",
        "사용할 테이블",
        "사용할 데이터셋",
        "테이블 중 어떤",
        "데이터셋 중 어떤",
        "소스 중 어떤",
    )
    usage_tokens = ("써야", "사용해야", "사용할", "필요", "적합", "조회하려면", "보려면", "답하려면", "사용할 수 있는")
    task_tokens = (
        "생산량",
        "생산 실적",
        "재공",
        "투입",
        "uph",
        "hold",
        "lot",
        "assign",
        "장비 배정",
        "공정",
        "제품",
    )
    return (
        any(token in lowered for token in selection_tokens)
        and any(token in lowered for token in task_tokens)
        and any(token in lowered for token in usage_tokens)
    )


# 함수 설명: `_inventory_request_kind()`는 카탈로그 질문이 목록 조회인지 건수 확인인지 응답 요약에 기록합니다.
def _inventory_request_kind(question: str, answer_mode: str) -> str:
    if answer_mode != "available_sources":
        return ""
    lowered = str(question or "").lower()
    count_tokens = ("총", "몇 개", "몇개", "개수", "건수", "how many", "count")
    return "count" if any(token in lowered for token in count_tokens) else "list"


# 함수 설명: `_select_domain_items()`는 질문 token 점수로 관련 도메인 항목만 max_items 범위에서 선택합니다.
def _select_domain_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode in NO_DOMAIN_CANDIDATE_MODES:
        return []
    if answer_mode == "calculation_logic_list":
        if _looks_like_product_aggregation_rule_question(question.lower()):
            return _select_product_aggregation_items(question, items, limit)
        selected = [item for item in items if str(item.get("section") or "") in CALCULATION_SECTIONS]
        ranked = _ranked(question, selected, limit)
        return ranked if ranked else selected[:limit]
    if answer_mode == "product_domain_info":
        product_items = [item for item in items if str(item.get("section") or "") in PRODUCT_DOMAIN_SECTIONS]
        if _looks_like_product_domain_inventory(question.lower()):
            return product_items[:limit]
        ranked = _ranked(question + " product_terms 제품군 제품 조건", product_items, limit)
        return ranked if ranked else product_items[:limit]
    if answer_mode == "product_condition":
        product_items = [item for item in items if str(item.get("section") or "") in PRODUCT_DOMAIN_SECTIONS]
        ranked = _ranked(question + " product_terms 제품군 제품 조건", product_items, limit)
        return ranked if ranked else product_items[:limit]
    if answer_mode == "product_token_rule":
        function_items = [item for item in items if str(item.get("section") or "") == "pandas_function_cases"]
        ranked = _ranked(question + " product token match_product_tokens 제품 토큰", function_items, limit)
        return ranked if ranked else function_items[:limit]
    if answer_mode == "process_group":
        return _ranked(question + " process_groups 공정", items, limit)
    if answer_mode == "term_definition":
        return _ranked(question + " quantity_terms metric_terms analysis_recipes", items, limit)
    if answer_mode in {"domain_info", "question_to_dataset"}:
        return _ranked(question, items, limit)
    selected = _ranked(question, items, limit)
    # 점수가 0인 임의의 앞 5건은 질문 근거가 아니므로 후보와 confidence를 오염시키지 않습니다.
    return selected


# 함수 설명: `_looks_like_product_domain_inventory()`는 특정 제품 하나가 아니라 등록된 제품 그룹 전체를 묻는 표현인지 판정합니다.
def _looks_like_product_domain_inventory(lowered: str) -> bool:
    group_tokens = ("제품 그룹", "제품그룹", "제품군", "product group")
    inventory_tokens = ("등록", "목록", "뭐가 있", "무엇이 있", "전부", "전체", "관련")
    return any(token in lowered for token in group_tokens) and any(token in lowered for token in inventory_tokens)


# 함수 설명: `_select_product_aggregation_items()`는 제품 grain 설명에 필요한 제품 키와 관련 recipe만 우선 선택하고 지표가 명시된 경우만 계산 항목을 보강합니다.
def _select_product_aggregation_items(question: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    aggregation_items = [item for item in items if str(item.get("section") or "") in PRODUCT_AGGREGATION_SECTIONS]
    product_keys = [item for item in aggregation_items if str(item.get("section") or "") == "product_key_columns"]
    selected.extend(_ranked(question + " product_key_columns 제품 키 제품별 group_by", product_keys, limit) or product_keys)

    recipes = [item for item in aggregation_items if str(item.get("section") or "") == "analysis_recipes"]
    product_recipes = [item for item in recipes if _is_product_aggregation_item(item)]
    selected.extend(_ranked(question + " product aggregation grain group_by 제품 집계", product_recipes, limit) or product_recipes)

    if _has_named_metric(question.lower()):
        metric_items = [item for item in items if str(item.get("section") or "") in {"quantity_terms", "metric_terms", "calculation_rules"}]
        selected.extend(_ranked(question, metric_items, limit))

    deduped = []
    seen = set()
    for item in selected:
        identity = (str(item.get("section") or ""), str(item.get("key") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


# 함수 설명: `_is_product_aggregation_item()`은 recipe payload가 제품 단위 grain·group by 규칙을 실제로 설명하는지 확인합니다.
def _is_product_aggregation_item(item: dict[str, Any]) -> bool:
    payload = _dict(item.get("payload"))
    if any(payload.get(key) not in (None, "", [], {}) for key in ("grain_policy", "group_by")):
        blob = _text_blob(item).lower()
        return any(token in blob for token in ("제품", "product", "product_key", "question_or_product_grain"))
    return False


# 함수 설명: `_has_named_metric()`은 제품 집계 설명에 수량·지표 메타데이터까지 포함해야 하는 구체 지표 표현을 찾습니다.
def _has_named_metric(lowered: str) -> bool:
    return any(
        token in lowered
        for token in (
            "생산량",
            "생산실적",
            "생산 실적",
            "재공",
            "투입",
            "input",
            "output",
            "uph",
            "달성률",
            "달성율",
            "계획",
            "lot 수",
            "lot수",
            "unit 수",
            "unit수",
        )
    )


# 함수 설명: `_select_table_items()`는 질문과 답변 모드에 맞는 테이블 카탈로그 후보를 점수순으로 선택합니다.
def _select_table_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode in LIST_ALL_TABLE_MODES:
        return items[: _list_limit(limit, items)]
    if answer_mode in {"dataset_sql", "dataset_detail", "required_params", "question_to_dataset"}:
        selected = _ranked(question, items, limit)
        return selected if selected else items[: min(limit, 5)]
    if answer_mode == "data_analysis_redirect":
        return []
    selected = _ranked(question, items, limit)
    return selected[: min(limit, 5)]


# 함수 설명: `_select_filter_items()`는 메인 필터 후보를 질문 token과 별칭 일치 기준으로 선택합니다.
def _select_filter_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode == "available_sources":
        return []
    if answer_mode in {"required_params", "term_definition", "question_to_dataset"}:
        return _ranked(question, items, min(limit, 6))
    if answer_mode == "data_analysis_redirect":
        return []
    return _ranked(question, items, min(limit, 6))


# 함수 설명: `_list_limit()`는 QA 후보 최대 개수를 허용 범위 안의 정수로 보정합니다.
def _list_limit(limit: int, items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    return min(len(items), limit)


# 함수 설명: `_catalog_summary()`는 전체 조회 건수와 실제 반환 건수를 분리해 목록 제한을 투명하게 기록합니다.
def _catalog_summary(
    answer_mode: str,
    request_kind: str,
    loaded_item_count: int,
    returned_count: int,
    limit: int,
    table_load: dict[str, Any],
) -> dict[str, Any]:
    if answer_mode != "available_sources":
        return {}
    try:
        load_count = max(0, int(table_load.get("count", loaded_item_count)))
    except Exception:
        load_count = loaded_item_count
    try:
        lower_bound_count = max(0, int(table_load.get("total_count_lower_bound", load_count)))
    except Exception:
        lower_bound_count = load_count
    try:
        load_limit = max(1, int(table_load.get("limit", limit)))
    except Exception:
        load_limit = max(1, int(limit))
    response_limit = max(1, int(limit))
    total_count = max(loaded_item_count, load_count, lower_bound_count)
    total_count_exact = not bool(table_load.get("truncated"))
    return {
        "request_kind": request_kind or "list",
        "total_count": total_count,
        "returned_count": max(0, int(returned_count)),
        "truncated": (not total_count_exact) or returned_count < total_count,
        "total_count_exact": total_count_exact,
        "limit": min(response_limit, load_limit),
        "response_limit": response_limit,
        "load_limit": load_limit,
    }


# 함수 설명: `_refresh_catalog_summary()`는 바이트 제한으로 행이 줄어든 뒤 summary와 실제 rows 건수를 다시 맞춥니다.
def _refresh_catalog_summary(context: dict[str, Any]) -> None:
    summary = _dict(context.get("catalog_summary"))
    if not summary:
        return
    returned_count = len(context.get("candidate_rows")) if isinstance(context.get("candidate_rows"), list) else 0
    try:
        total_count = max(0, int(summary.get("total_count", returned_count)))
    except Exception:
        total_count = returned_count
    total_count_exact = bool(summary.get("total_count_exact", True))
    summary["returned_count"] = returned_count
    summary["truncated"] = (not total_count_exact) or returned_count < total_count
    context["catalog_summary"] = summary


# 함수 설명: `_ranked()`는 메타데이터 항목을 질문 일치 점수와 원래 순서로 안정 정렬합니다.
def _ranked(question: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    tokens = _tokens(question)
    scored = []
    for item in items:
        score = _score(tokens, item)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


# 함수 설명: `_score()`는 질문 token과 후보 메타데이터의 일치 정도를 점수로 계산합니다.
def _score(tokens: set[str], item: dict[str, Any]) -> int:
    if not tokens:
        return 0
    blob = _text_blob(item).lower()
    score = sum(1 for token in tokens if token and token in blob)
    payload = _dict(item.get("payload"))
    display = str(payload.get("display_name") or item.get("display_name") or item.get("key") or item.get("dataset_key") or "").lower()
    score += sum(2 for token in tokens if token and token in display)
    return score


# 함수 설명: `_tokens()`는 문자열을 비교 가능한 검색 token 목록으로 분리·정규화합니다.
def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    raw = re.findall(r"[0-9a-zA-Z가-힣_/.-]+", lowered)
    aliases = {"생산량": {"production", "output", "실적"}, "재공": {"wip"}, "투입": {"input"}, "쿼리": {"query", "sql"}}
    result = {token.strip() for token in raw if len(token.strip()) >= 2}
    for token in list(result):
        result.update(aliases.get(token, set()))
    if any(token in lowered for token in ("제품 그룹", "제품그룹", "제품군", "product group")):
        result.update({"제품군", "제품 조건", "product", "product_terms"})
    if any(token in lowered for token in ("집계", "그룹핑", "그루핑", "group by", "groupby", "group_by")):
        result.update({"집계", "aggregation", "group_by", "grain"})
    if "제품" in lowered or "product" in lowered:
        result.update({"제품", "product"})
    return result


# 함수 설명: `_project_domain_item()`는 도메인 문서에서 QA 답변에 필요한 설명·별칭·공정 정보만 projection합니다.
def _project_domain_item(item: dict[str, Any], answer_mode: str) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    keys = {"display_name", "aliases", "description", "usage_rule", "column", "aggregation_method"}
    if answer_mode == "process_group":
        keys.update({"processes", "process_groups", "members"})
    if answer_mode in {"product_domain_info", "product_condition", "product_token_rule"}:
        keys.update(
            {
                "conditions",
                "condition",
                "condition_by_family",
                "condition_by_dataset",
                "filters",
                "patterns",
                "tokens",
                "include",
                "exclude",
                "product_key_columns",
                "question_cues",
                "required_question_cues",
                "examples",
                "usage_examples",
            }
        )
    if answer_mode == "calculation_logic_list":
        keys.update(
            {
                "formula",
                "calculation",
                "calculation_rule",
                "aggregation",
                "required_inputs",
                "outputs",
                "output_column",
                "output_columns",
                "applicability",
                "conditions",
                "pseudocode",
                "function_name",
                "logic",
                "columns",
                "product_key_columns",
                "grain_policy",
                "group_by",
                "step_plan_template",
                "required_quantity_terms",
                "required_dataset_families",
                "metric_terms",
                "question_cues",
                "required_question_cues",
                "forbidden_question_cues",
                "quantity_column",
                "dataset_key",
                "dataset_family",
            }
        )
    return _omit_empty(
        {
            "section": item.get("section"),
            "key": item.get("key"),
            "status": item.get("status"),
            "payload": _project_dict(payload, keys),
        }
    )


# 함수 설명: `_project_table_item()`는 테이블 문서에서 dataset/source/컬럼/조회 설명만 안전하게 projection합니다.
def _project_table_item(item: dict[str, Any], answer_mode: str) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    payload_keys = {
        "display_name", "dataset_family", "source_type", "required_params", "required_param_mappings",
        "filter_mappings", "standard_column_aliases", "description", "columns", "quantity_column",
        "quantity_columns", "metric_columns", "measure_columns", "value_columns", "column", "aggregation_column",
    }
    projected_payload = _project_dict(payload, payload_keys)
    source_config = _dict(payload.get("source_config"))
    source_keys = {"source_type", "db_key", "doc_id", "sheet_name", "endpoint", "endpoint_id", "api_url", "url", "method", "response_path"}
    if answer_mode == "dataset_sql":
        source_keys.add("query_template")
    projected_source = _project_dict(source_config, source_keys)
    if projected_source:
        projected_payload["source_config"] = projected_source
    return _omit_empty(
        {
            "dataset_key": item.get("dataset_key") or item.get("key"),
            "status": item.get("status"),
            "payload": projected_payload,
        }
    )


# 함수 설명: `_project_filter_item()`는 메인 필터 문서에서 별칭·연산자·값 형식만 안전하게 projection합니다.
def _project_filter_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    keys = {"display_name", "aliases", "description", "semantic_role", "operator", "value_type", "value_shape", "column_candidates"}
    return _omit_empty(
        {
            "filter_key": item.get("filter_key") or item.get("key"),
            "status": item.get("status"),
            "payload": _project_dict(payload, keys),
        }
    )


# 함수 설명: `_project_dict()`는 DICT에서 현재 질문과 응답에 필요한 허용 필드만 projection합니다.
def _project_dict(value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: deepcopy(item) for key, item in value.items() if key in allowed and item not in (None, "", [], {})}


# 함수 설명: `_fit_context_bytes()`는 QA context가 max_bytes를 넘으면 낮은 우선순위 후보와 긴 문자열부터 단계적으로 줄입니다.
def _fit_context_bytes(context: dict[str, Any], byte_limit: int) -> tuple[dict[str, Any], bool]:
    fitted = _truncate_context_strings(deepcopy(context))
    if _json_bytes(fitted) <= byte_limit:
        return fitted, False
    trimmed = True
    answer_mode = str(fitted.get("answer_mode") or "")
    for key in ("matched_domain_items", "matched_filters", "matched_datasets"):
        values = fitted.get(key)
        keep = 1 if key == "matched_datasets" and answer_mode == "dataset_sql" else 0
        if isinstance(values, list):
            while len(values) > keep and _json_bytes(fitted) > byte_limit:
                values.pop()
    rows = fitted.get("candidate_rows")
    refs = fitted.get("source_refs")
    while isinstance(rows, list) and rows and _json_bytes(fitted) > byte_limit:
        removed = rows.pop()
        removed_key = str(_dict(removed).get("key") or "")
        if isinstance(refs, list) and removed_key:
            for index in range(len(refs) - 1, -1, -1):
                if str(_dict(refs[index]).get("key") or "") == removed_key:
                    refs.pop(index)
                    break
    if _json_bytes(fitted) > byte_limit:
        for key in ("matched_domain_items", "matched_datasets", "matched_filters", "candidate_rows", "source_refs"):
            if fitted.get(key) == []:
                fitted.pop(key, None)
    if _json_bytes(fitted) > byte_limit and isinstance(fitted.get("load_summary"), dict):
        fitted["load_summary"] = {
            key: _omit_empty(
                {
                    "status": _dict(value).get("status"),
                    "count": _dict(value).get("count"),
                    "truncated": _dict(value).get("truncated"),
                    "total_count_lower_bound": _dict(value).get("total_count_lower_bound"),
                }
            )
            for key, value in fitted["load_summary"].items()
        }
    if _json_bytes(fitted) > byte_limit:
        fitted.pop("load_summary", None)
    return fitted, trimmed


# 함수 설명: `_truncate_context_strings()`는 문맥·strings이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _truncate_context_strings(value: Any, key_name: str = "") -> Any:
    if isinstance(value, dict):
        return {key: _truncate_context_strings(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_context_strings(item, key_name) for item in value]
    if isinstance(value, str):
        limit = 16000 if key_name == "query_template" else 2000
        return value if len(value) <= limit else value[:limit] + "..."
    return deepcopy(value)


# 함수 설명: `_json_bytes()`는 현재 값을 UTF-8 JSON으로 직렬화했을 때의 실제 바이트 크기를 계산합니다.
def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


# 함수 설명: `_candidate_rows()`는 QA 답변 모드에 맞춰 도메인·테이블·필터 후보를 공통 표 행으로 변환합니다.
def _candidate_rows(answer_mode: str, domain_items: list[dict[str, Any]], table_items: list[dict[str, Any]], filter_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if answer_mode in {"dataset_sql", "available_sources"}:
        return [_dataset_row(item) for item in table_items]
    if answer_mode == "dataset_detail":
        return [_dataset_detail_row(item) for item in table_items]
    if answer_mode == "required_params":
        return [_required_param_row(item) for item in table_items] + [_filter_row(item) for item in filter_items]
    if answer_mode == "question_to_dataset":
        rows = [_dataset_row(item) for item in table_items]
        rows.extend(_domain_row(item, include_section=True) for item in domain_items)
        rows.extend(_filter_row(item) for item in filter_items)
        return rows
    if answer_mode == "calculation_logic_list":
        return [_domain_row(item, include_section=True) for item in domain_items]
    if answer_mode in {"product_domain_info", "product_condition", "product_token_rule", "process_group", "term_definition"}:
        return [_domain_row(item, include_section=True) for item in domain_items]
    if answer_mode == "data_analysis_redirect":
        return []
    rows = [_domain_row(item, include_section=True) for item in domain_items]
    rows.extend(_filter_row(item) for item in filter_items)
    rows.extend(_dataset_row(item) for item in table_items)
    return rows


# 함수 설명: `_dataset_row()`는 행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _dataset_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    source_config = _dict(payload.get("source_config"))
    return _omit_empty(
        {
            "metadata_type": "table_catalog",
            "key": item.get("dataset_key") or item.get("key"),
            "display_name": payload.get("display_name") or item.get("display_name"),
            "dataset_family": payload.get("dataset_family"),
            "source_type": payload.get("source_type") or source_config.get("source_type"),
            "db_key": source_config.get("db_key"),
            "required_params": _compact_list(payload.get("required_params")),
            "description": payload.get("description"),
        }
    )


# 함수 설명: `_dataset_detail_row()`는 상세 정보·행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _dataset_detail_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    source_config = _dict(payload.get("source_config"))
    return _omit_empty(
        {
            "metadata_type": "table_catalog",
            "key": item.get("dataset_key") or item.get("key"),
            "display_name": payload.get("display_name") or item.get("display_name"),
            "dataset_family": payload.get("dataset_family"),
            "source_type": payload.get("source_type") or source_config.get("source_type"),
            "db_key": source_config.get("db_key"),
            "required_params": _compact_list(payload.get("required_params")),
            "columns": _compact_list(payload.get("columns")),
            "quantity_columns": _quantity_columns(payload),
            "filter_mappings": _compact_list(payload.get("filter_mappings")),
            "description": payload.get("description"),
        }
    )


# 함수 설명: `_required_param_row()`는 파라미터·행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _required_param_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    source_config = _dict(payload.get("source_config"))
    return _omit_empty(
        {
            "metadata_type": "table_catalog",
            "key": item.get("dataset_key") or item.get("key"),
            "display_name": payload.get("display_name") or item.get("display_name"),
            "required_params": _compact_list(payload.get("required_params")),
            "source_type": payload.get("source_type") or source_config.get("source_type"),
            "db_key": source_config.get("db_key"),
            "filter_mappings": _compact_list(payload.get("filter_mappings")),
        }
    )


# 함수 설명: `_quantity_columns()`는 테이블 카탈로그 컬럼 중 수량·실적·계획 지표로 설명할 컬럼만 선별합니다.
def _quantity_columns(payload: dict[str, Any]) -> str:
    columns = []
    for key in ("quantity_column", "quantity_columns", "metric_columns", "measure_columns", "value_columns"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            columns.append(_compact_list(value))
    for key in ("column", "aggregation_column"):
        value = payload.get(key)
        if value not in (None, "", [], {}) and str(value) not in columns:
            columns.append(str(value))
    return ", ".join(item for item in columns if item)


# 함수 설명: `_domain_row()`는 행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _domain_row(item: dict[str, Any], include_section: bool = False) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    return _omit_empty(
        {
            "metadata_type": "domain",
            "section": item.get("section") if include_section else "",
            "key": item.get("key"),
            "display_name": payload.get("display_name") or item.get("display_name"),
            "aliases": _compact_list(payload.get("aliases")),
            "column": payload.get("column"),
            "aggregation_method": payload.get("aggregation_method"),
            "aggregation": payload.get("aggregation"),
            "condition": payload.get("condition") or payload.get("conditions"),
            "condition_by_family": payload.get("condition_by_family"),
            "condition_by_dataset": payload.get("condition_by_dataset"),
            "filters": payload.get("filters"),
            "columns": payload.get("columns") or payload.get("product_key_columns"),
            "grain_policy": payload.get("grain_policy"),
            "group_by": payload.get("group_by"),
            "formula": payload.get("formula") or payload.get("calculation"),
            "calculation_rule": payload.get("calculation_rule"),
            "quantity_column": payload.get("quantity_column"),
            "required_quantity_terms": payload.get("required_quantity_terms"),
            "required_dataset_families": payload.get("required_dataset_families"),
            "output_column": payload.get("output_column"),
            "output_columns": payload.get("output_columns"),
            "step_plan_template": payload.get("step_plan_template"),
            "question_cues": payload.get("question_cues") or payload.get("required_question_cues"),
            "description": payload.get("description") or payload.get("usage_rule"),
        }
    )


# 함수 설명: `_filter_row()`는 조건과 우선순위에 맞는 행만 골라 원래 순서를 유지해 반환합니다.
def _filter_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    return _omit_empty(
        {
            "metadata_type": "main_flow_filter",
            "key": item.get("filter_key") or item.get("key"),
            "display_name": payload.get("display_name") or item.get("display_name"),
            "aliases": _compact_list(payload.get("aliases")),
            "semantic_role": payload.get("semantic_role"),
            "operator": payload.get("operator"),
            "column_candidates": _compact_list(payload.get("column_candidates")),
        }
    )


# 함수 설명: `_source_refs()`는 선택된 메타데이터 후보의 section/key를 중복 없는 근거 참조 목록으로 만듭니다.
def _source_refs(domain_items: list[dict[str, Any]], table_items: list[dict[str, Any]], filter_items: list[dict[str, Any]]) -> list[dict[str, str]]:
    refs = []
    refs.extend({"metadata_type": "domain", "section": str(item.get("section") or ""), "key": str(item.get("key") or "")} for item in domain_items)
    refs.extend({"metadata_type": "table_catalog", "key": str(item.get("dataset_key") or item.get("key") or "")} for item in table_items)
    refs.extend({"metadata_type": "main_flow_filter", "key": str(item.get("filter_key") or item.get("key") or "")} for item in filter_items)
    return [ref for ref in refs if ref.get("key")]


# 함수 설명: `_sanitize()`는 sanitize에서 비밀값·내부 필드·직렬화 불가 값을 제거하거나 마스킹합니다.
def _sanitize(item: dict[str, Any]) -> dict[str, Any]:
    value = _sanitize_value(item)
    return value if isinstance(value, dict) else {}


# 함수 설명: `_sanitize_value()`는 LLM 문맥에서 trace·credential·내부 필드를 제거하고 비밀값을 마스킹합니다.
def _sanitize_value(value: Any, key_name: str = "") -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in PRUNED_METADATA_KEYS:
                continue
            if _is_secret_key(key_text):
                result[key_text] = "***"
            else:
                result[key_text] = _sanitize_value(item, key_text)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item, key_name) for item in value]
    if _is_secret_key(key_name):
        return "***"
    return deepcopy(value)


# 함수 설명: `_is_secret_key()`는 필드 이름이 credential·token·password 등 저장 금지 비밀 key인지 판정합니다.
def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


# 함수 설명: `_compact_load()`는 조회 상태에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_load(load: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "status": load.get("status"),
            "metadata_kind": load.get("metadata_kind"),
            "database": load.get("database"),
            "collection_name": load.get("collection_name"),
            "count": load.get("count"),
            "limit": load.get("limit"),
            "truncated": load.get("truncated"),
            "total_count_lower_bound": load.get("total_count_lower_bound"),
            "cache_hit": load.get("cache_hit"),
            "errors": load.get("errors"),
        }
    )


# 함수 설명: `_load_errors()`는 입력 또는 외부 저장소에서 오류을 읽고 호출자가 사용할 형태로 반환합니다.
def _load_errors(load_summary: dict[str, Any]) -> list[dict[str, Any]]:
    errors = []
    for load in load_summary.values():
        if isinstance(load, dict):
            errors.extend(item for item in load.get("errors", []) if isinstance(item, dict))
    return errors


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


# 함수 설명: `_text_blob()`는 메타데이터의 주요 문자열 값을 하나의 검색용 텍스트로 합칩니다.
def _text_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


# 함수 설명: `_compact_list()`는 목록의 개수와 각 항목 크기를 제한해 LLM·상태 payload가 과도하게 커지지 않게 합니다.
def _compact_list(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value[:12] if str(item or "").strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value) if value not in (None, "", [], {}) else ""


# 함수 설명: `_omit_empty()`는 dict에서 빈 문자열·빈 목록·None 항목을 제거해 전달 payload를 작게 유지합니다.
def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataQaContextBuilder(Component):
    display_name = "02 메타데이터 QA 컨텍스트 생성기"
    description = "질문과 MongoDB 메타데이터를 읽어 QA에 필요한 후보만 선별합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        DataInput(name="domain_items", display_name="도메인 메타데이터", required=False),
        DataInput(name="table_catalog_items", display_name="테이블 카탈로그", required=False),
        DataInput(name="main_flow_filters", display_name="메인 필터", required=False),
        MessageTextInput(name="max_items", display_name="최대 후보 수", value=str(DEFAULT_MAX_ITEMS), required=False, advanced=True),
        MessageTextInput(name="max_bytes", display_name="최대 Context 바이트", value=str(DEFAULT_MAX_BYTES), required=False, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=build_metadata_qa_context(
                getattr(self, "payload", None),
                getattr(self, "domain_items", None),
                getattr(self, "table_catalog_items", None),
                getattr(self, "main_flow_filters", None),
                getattr(self, "max_items", str(DEFAULT_MAX_ITEMS)),
                getattr(self, "max_bytes", str(DEFAULT_MAX_BYTES)),
            )
        )
