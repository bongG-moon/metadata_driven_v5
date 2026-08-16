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
LIST_ALL_DOMAIN_MODES = {"available_domains"}
NO_DOMAIN_CANDIDATE_MODES = {
    "available_sources",
    "scoped_sources",
    "dataset_comparison",
    "inventory_followup_missing_context",
    "dataset_sql",
    "dataset_detail",
    "required_params",
    "datasets_by_required_param",
    "data_analysis_redirect",
}
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_BYTES = 65536
REGISTRATION_TEXT_LIMIT = 2000
# 목록 요청이 아닌 일반 도메인 QA는 화면에도 최대 12건만 표시합니다. 따라서
# 더 많은 약한 후보를 LLM context에 넣는 것은 정확도·비용 측면에서 이점이 없습니다.
DEFAULT_NARROW_DOMAIN_ITEM_LIMIT = 12
DETERMINISTIC_ANSWER_MODES = {
    "available_sources",
    "scoped_sources",
    "dataset_comparison",
    "inventory_followup_missing_context",
    "available_domains",
    "dataset_detail",
    "dataset_sql",
    "required_params",
    "datasets_by_required_param",
    "calculation_logic_list",
    "product_domain_info",
    "process_group",
    "data_analysis_redirect",
}
METADATA_QA_INVENTORY_CONTRACT_VERSION = "metadata_qa.inventory.v1"
MAX_METADATA_QA_INVENTORY_DATASETS = 50

# 메타데이터 QA 목록 질문에서만 쓰는 사용자용 분류 별칭입니다. 제품·공정 조건처럼
# 실제 데이터 조회에 적용되는 도메인 규칙은 아니며, Table Catalog의 dataset_family를
# 사람이 질문한 표현과 안전하게 대응시키는 UI 범위의 표현 계약입니다.
CATALOG_FAMILY_QUERY_ALIASES = {
    "production": ("production", "생산"),
    "wip": ("wip", "재공"),
    "equipment": ("equipment", "장비", "설비"),
    "hold": ("hold",),
    "plan": ("plan", "계획"),
    "pkg_plan": ("pkg plan", "pkg_plan", "계획"),
    "goodocs_pkg_plan": ("goodocs pkg plan", "goodocs_pkg_plan", "계획"),
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
    domain_items = [_sanitize_domain_item(item) for item in domain_items]
    table_items = [_sanitize(item) for item in table_items]
    filter_items = [_sanitize(item) for item in filter_items]

    previous_inventory = _validated_metadata_qa_inventory(_dict(payload.get("state")), table_items)
    inventory_followup = _looks_like_inventory_followup_question(question)
    answer_mode = _infer_answer_mode(
        question,
        table_items,
        has_previous_inventory=bool(previous_inventory),
        inventory_followup=inventory_followup,
    )
    catalog_scope = _catalog_scope_from_question(
        question,
        table_items,
        previous_inventory if inventory_followup else {},
    )
    query_scope = _infer_query_scope(question, answer_mode)
    inventory_request_kind = _inventory_request_kind(question, answer_mode)
    matched_domain = [_project_domain_item(item, answer_mode) for item in _select_domain_items(question, answer_mode, domain_items, limit)]
    matched_table_items = _select_table_items(question, answer_mode, table_items, limit, catalog_scope)
    matched_tables = [_project_table_item(item, answer_mode) for item in matched_table_items]
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
        scoped_total_count=_catalog_scope_total_count(answer_mode, table_items, catalog_scope),
    )
    domain_summary = _domain_summary(
        answer_mode,
        inventory_request_kind,
        len(domain_items),
        len(candidate_rows),
        limit,
        domain_load,
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
    # 일반 용어 설명은 자연어 품질을 위해 모델 합성을 유지합니다.
    # 반면 실제 조회 컬럼·값을 묻는 조건 질문은 MongoDB 등록값을 그대로 보여줘야 하므로 결정형으로 처리합니다.
    deterministic_answer = answer_mode in DETERMINISTIC_ANSWER_MODES or (
        answer_mode == "term_definition" and _looks_like_domain_condition_question(question.lower())
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
        "answer_policy": {
            "mode": "deterministic_context" if deterministic_answer else "model_assisted",
            "use_model_response": not deterministic_answer,
            "reason": "authoritative_context_answer" if deterministic_answer else "model_synthesis_required",
        },
    }
    if catalog_scope:
        context["catalog_scope"] = catalog_scope
    if answer_mode == "datasets_by_required_param":
        context["requested_required_params"] = _required_param_names_in_question(question, table_items)
    if catalog_summary:
        context["catalog_summary"] = catalog_summary
    if domain_summary:
        context["domain_summary"] = domain_summary
    if answer_mode == "available_sources":
        context["matched_datasets"] = []
    if answer_mode == "available_domains":
        context["matched_domain_items"] = []
    context, context_trimmed = _fit_context_bytes(context, byte_limit)
    _refresh_inventory_summaries(context)
    if _json_bytes(context) > byte_limit:
        context, additionally_trimmed = _fit_context_bytes(context, byte_limit)
        context_trimmed = context_trimmed or additionally_trimmed
        _refresh_inventory_summaries(context)
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
        "domain_match_count": int(_dict(context.get("domain_summary")).get("returned_count", 0)) if answer_mode == "available_domains" else len(context.get("matched_domain_items")) if isinstance(context.get("matched_domain_items"), list) else 0,
        "dataset_match_count": int(_dict(context.get("catalog_summary")).get("returned_count", 0)) if answer_mode in {"available_sources", "scoped_sources"} else len(context.get("matched_datasets")) if isinstance(context.get("matched_datasets"), list) else 0,
        "filter_match_count": len(context.get("matched_filters")) if isinstance(context.get("matched_filters"), list) else 0,
        "context_bytes": _json_bytes(context),
        "context_trimmed": context_trimmed,
        "catalog_summary": deepcopy(_dict(context.get("catalog_summary"))),
        "domain_summary": deepcopy(_dict(context.get("domain_summary"))),
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
        "answer_policy": {"mode": "invalid_request", "use_model_response": False, "reason": "empty_question"},
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
def _infer_answer_mode(
    question: str,
    table_items: list[dict[str, Any]] | None = None,
    *,
    has_previous_inventory: bool = False,
    inventory_followup: bool = False,
) -> str:
    lowered = question.lower()
    tables = table_items or []
    if inventory_followup:
        return "scoped_sources" if has_previous_inventory else "inventory_followup_missing_context"
    if any(token in lowered for token in ("쿼리", "sql", "query", "select", "with문")):
        return "dataset_sql"
    # 비교 질문은 "필수 조건"이라는 단어를 함께 포함할 수 있으므로 단일
    # 데이터셋 필수 조건 질문보다 먼저 분기합니다. 그래야 main filter가 표에
    # 섞이지 않고 두 Table Catalog만 같은 형식으로 비교됩니다.
    if _looks_like_dataset_comparison_question(question, tables):
        return "dataset_comparison"
    if _looks_like_specific_dataset_required_params(question, tables):
        return "required_params"
    if _looks_like_datasets_by_required_param(question, tables):
        return "datasets_by_required_param"
    if _looks_like_specific_dataset_detail(lowered):
        return "dataset_detail"
    if _looks_like_task_dataset_selection(lowered):
        return "question_to_dataset"
    catalog_scope = _catalog_scope_from_question(question, tables)
    if _has_catalog_scope(catalog_scope) and _looks_like_scoped_sources_question(question):
        return "scoped_sources"
    # 명시적인 카탈로그 목록·건수 질문은 "현재 장비 테이블 목록"처럼 실제 값 질문과
    # 일부 단어가 겹쳐도 MongoDB 카탈로그의 결정론적 목록 경로를 우선합니다.
    if _looks_like_available_sources_question(question, tables):
        if _has_catalog_scope(catalog_scope):
            return "scoped_sources"
        return "available_sources"
    if _looks_like_data_value_question(lowered) or _looks_like_table_data_question(lowered):
        return "data_analysis_redirect"
    if any(token in lowered for token in ("필수 파라미터", "필수조건", "필수 조건", "required param", "required_param")):
        return "required_params"
    if any(token in lowered for token in ("어떤 데이터", "무슨 데이터", "어느 데이터", "어떤 테이블", "무슨 테이블", "어떤 source", "무슨 source", "어떤 소스")):
        return "question_to_dataset"
    if _looks_like_process_group_question(lowered):
        return "process_group"
    if _looks_like_product_domain_question(lowered):
        return "product_domain_info"
    if _looks_like_product_aggregation_rule_question(lowered):
        return "calculation_logic_list"
    if any(token in lowered for token in ("제품 조건", "제품군", "hbm", "mobile", "pop", "tsv", "3ds")):
        return "product_condition"
    if any(token in lowered for token in ("제품 표현", "제품 token", "제품 토큰", "어떻게 찾", "매칭", "token")):
        return "product_token_rule"
    if _looks_like_domain_condition_question(lowered):
        return "term_definition"
    if any(token in lowered for token in ("어떤 컬럼", "무슨 컬럼", "컬럼이야", "의미", "정의", "용어")):
        return "term_definition"
    if any(token in lowered for token in ("뭐야", "무엇", "설명", "어떤 데이터야", "어떤 source야", "어떤 소스야")) and any(token in lowered for token in ("today", "history", "production", "wip", "target", "equipment", "lot", "hold", "_")):
        return "dataset_detail"
    if any(token in lowered for token in ("계산 로직", "계산", "로직", "recipe", "function", "함수")):
        return "calculation_logic_list"
    if _looks_like_available_domains_question(lowered):
        return "available_domains"
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
    if (
        any(token in lowered for token in ("제품", "product"))
        and any(token in lowered for token in ("조건", "필터", "구분"))
        and any(token in lowered for token in metadata_detail_tokens + ("어떻게", "기준"))
    ):
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


# 함수 설명: 등록된 도메인 용어가 실제 조회 필터에서 어떤 컬럼·값으로 적용되는지 묻는 질문을 식별합니다.
def _looks_like_domain_condition_question(lowered: str) -> bool:
    condition_tokens = ("조건", "필터", "filter")
    application_tokens = ("어떤 값", "무슨 값", "값으로", "적용", "조회에서", "조회 시", "조회시")
    return any(token in lowered for token in condition_tokens) and any(
        token in lowered for token in application_tokens
    )


# 함수 설명: 공정 그룹의 목록·포함 공정·두 그룹 차이를 묻는 표현을 띄어쓰기 변형과 함께 식별합니다.
def _looks_like_process_group_question(lowered: str) -> bool:
    group_tokens = ("공정 그룹", "공정그룹", "공정 group", "공정group", "process group")
    detail_tokens = ("세부 공정", "세부공정", "포함 공정", "포함공정", "공정에는")
    return any(token in lowered for token in group_tokens) or (
        "공정" in lowered and any(token in lowered for token in detail_tokens)
    ) or (
        lowered.count("공정") >= 2 and _looks_like_comparison_question(lowered)
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
    elif answer_mode == "process_group":
        subject = "process_groups"
        aspect = "comparison" if _looks_like_comparison_question(lowered) else "members"
    else:
        subject = "general"
        aspect = ""

    if _looks_like_comparison_question(lowered):
        request_kind = "comparison"
    elif any(token in lowered for token in ("총", "몇 개", "몇개", "개수", "건수", "count")):
        request_kind = "count"
    elif any(token in lowered for token in ("목록", "list", "뭐가 있", "무엇이 있", "전부", "전체")):
        request_kind = "list"
    elif any(token in lowered for token in ("어떻게", "기준", "규칙", "컬럼으로", "group by", "groupby", "그룹핑", "그루핑")):
        request_kind = "how_to"
    else:
        request_kind = "detail"
    return _omit_empty({"subject": subject, "aspect": aspect, "request_kind": request_kind})


# 함수 설명: 두 메타데이터 항목의 차이·비교·구분을 요청하는 공통 표현을 식별합니다.
def _looks_like_comparison_question(lowered: str) -> bool:
    return any(token in lowered for token in ("비교", "차이", "구분", "각각", "어떻게 다", "다른지", "어떤 차이"))


# 함수 설명: `_looks_like_data_value_question()`는 입력값이 LIKE·데이터·값·question 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _looks_like_data_value_question(lowered: str) -> bool:
    if any(token in lowered for token in ("메타데이터", "metadata", "등록", "정의", "무슨 컬럼", "어떤 컬럼", "쿼리", "sql", "query", "데이터셋", "필수 조건")):
        return False
    has_time_or_target = any(token in lowered for token in ("오늘", "어제", "전일", "금일", "현시간", "현재", "/", "월", "일"))
    has_metric = any(token in lowered for token in ("생산량", "생산 실적", "생산 데이터", "실적", "재공", "수량", "투입", "input", "output", "out", "assign", "장비"))
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


# 함수 설명: `_looks_like_dataset_comparison_question()`은 표시명·key를 포함해 명시된 둘 이상의 Table Catalog를 비교하는 질문만 식별합니다.
def _looks_like_dataset_comparison_question(question: str, table_items: list[dict[str, Any]]) -> bool:
    explicit_items = _explicit_dataset_items(question, table_items)
    unique_keys = {
        str(item.get("dataset_key") or item.get("key") or "").strip()
        for item in explicit_items
        if str(item.get("dataset_key") or item.get("key") or "").strip()
    }
    lowered = str(question or "").lower()
    comparison_cues = (
        "비교",
        "차이",
        "구분",
        "각각",
        "어떻게 다",
        "다른지",
        "언제 사용",
        "언제 각각",
        "어떤 경우",
    )
    return len(unique_keys) >= 2 and any(cue in lowered for cue in comparison_cues)


# 함수 설명: `_looks_like_inventory_followup_question()`은 직전 데이터셋 목록을 가리키는 후속 목록 질문만 좁게 식별합니다.
def _looks_like_inventory_followup_question(question: str) -> bool:
    lowered = str(question or "").lower()
    deictic_cues = ("여기서", "그중", "위 목록", "위 데이터셋", "이 목록", "이 데이터셋")
    catalog_subjects = ("데이터셋", "데이터 세트", "테이블", "source", "소스", "연결 방식", "연결방식")
    return any(cue in lowered for cue in deictic_cues) and any(subject in lowered for subject in catalog_subjects)


# 함수 설명: `_looks_like_scoped_sources_question()`은 연결 방식·분류를 명시한 데이터셋 목록 질문을 실제 데이터 조회와 구분합니다.
def _looks_like_scoped_sources_question(question: str) -> bool:
    lowered = str(question or "").lower()
    catalog_subjects = ("데이터셋", "데이터 세트", "테이블", "dataset", "source", "소스")
    inventory_cues = (
        "조회 가능",
        "조회가능",
        "목록",
        "리스트",
        "무엇이야",
        "무엇인가",
        "뭐야",
        "관련",
        "연결된",
        "연결 방식",
        "연결방식",
    )
    return any(subject in lowered for subject in catalog_subjects) and any(cue in lowered for cue in inventory_cues)


# 함수 설명: `_validated_metadata_qa_inventory()`는 세션의 이전 목록을 작은 allowlist 계약으로 검증하고 현재 카탈로그에 존재할 때만 재사용합니다.
def _validated_metadata_qa_inventory(state: dict[str, Any], table_items: list[dict[str, Any]]) -> dict[str, Any]:
    inventory = _dict(state.get("metadata_qa_inventory"))
    if str(inventory.get("contract_version") or "").strip() != METADATA_QA_INVENTORY_CONTRACT_VERSION:
        return {}
    raw_keys = inventory.get("dataset_keys")
    if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > MAX_METADATA_QA_INVENTORY_DATASETS:
        return {}
    dataset_keys = [str(value).strip() for value in raw_keys if str(value or "").strip()]
    if len(dataset_keys) != len(raw_keys) or len(set(dataset_keys)) != len(dataset_keys):
        return {}
    catalog_keys = {
        str(item.get("dataset_key") or item.get("key") or "").strip()
        for item in table_items
        if str(item.get("dataset_key") or item.get("key") or "").strip()
    }
    if not set(dataset_keys).issubset(catalog_keys):
        return {}
    scope = _inventory_scope(inventory.get("scope"))
    return _omit_empty(
        {
            "contract_version": METADATA_QA_INVENTORY_CONTRACT_VERSION,
            "dataset_keys": dataset_keys,
            "scope": scope,
        }
    )


# 함수 설명: `_inventory_scope()`는 세션에 저장된 목록 범위를 문자열 allowlist로 정규화합니다.
def _inventory_scope(value: Any) -> dict[str, list[str]]:
    raw = _dict(value)
    result: dict[str, list[str]] = {}
    for key in ("dataset_families", "source_types", "db_keys"):
        values = raw.get(key)
        if not isinstance(values, list) or len(values) > MAX_METADATA_QA_INVENTORY_DATASETS:
            continue
        normalized = [str(item).strip() for item in values if str(item or "").strip()]
        if len(normalized) != len(values) or len(set(normalized)) != len(normalized):
            continue
        result[key] = normalized
    return result


# 함수 설명: `_catalog_scope_from_question()`은 Table Catalog의 family·source·DB와 검증된 이전 목록을 교집합 범위로 해석합니다.
def _catalog_scope_from_question(
    question: str,
    table_items: list[dict[str, Any]],
    previous_inventory: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    lowered = str(question or "").lower()
    ascii_terms = set(re.findall(r"[a-z0-9][a-z0-9_.-]*", lowered))
    families = sorted(
        {
            str(_dict(item.get("payload")).get("dataset_family") or "").strip()
            for item in table_items
            if str(_dict(item.get("payload")).get("dataset_family") or "").strip()
        }
    )
    source_types = sorted(
        {
            _table_source_type(item)
            for item in table_items
            if _table_source_type(item)
        }
    )
    db_keys = sorted(
        {
            _table_db_key(item)
            for item in table_items
            if _table_db_key(item)
        }
    )
    selected_families = [
        family
        for family in families
        if _family_scope_matches_question(family, lowered, ascii_terms)
    ]
    selected_source_types = [
        source_type
        for source_type in source_types
        if source_type.casefold() in ascii_terms
    ]
    selected_db_keys = [
        db_key
        for db_key in db_keys
        if db_key.casefold() in ascii_terms
    ]
    result: dict[str, list[str]] = _omit_empty(
        {
            "dataset_families": selected_families,
            "source_types": selected_source_types,
            "db_keys": selected_db_keys,
        }
    )
    inventory = _dict(previous_inventory)
    dataset_keys = inventory.get("dataset_keys")
    if isinstance(dataset_keys, list) and dataset_keys:
        result["dataset_keys"] = [str(key) for key in dataset_keys]
    return result


# 함수 설명: `_family_scope_matches_question()`은 dataset_family 값과 목록 질문의 사용자용 분류 표현을 안전하게 대응합니다.
def _family_scope_matches_question(family: str, lowered: str, ascii_terms: set[str]) -> bool:
    normalized = str(family or "").strip().casefold()
    if not normalized:
        return False
    aliases = CATALOG_FAMILY_QUERY_ALIASES.get(normalized, (normalized,))
    for alias in aliases:
        text = str(alias).casefold()
        if re.fullmatch(r"[a-z0-9_. -]+", text):
            if text in ascii_terms:
                return True
        elif text and text in lowered:
            return True
    return normalized in ascii_terms


# 함수 설명: `_has_catalog_scope()`는 목록 후보를 실제로 좁힐 수 있는 범위가 하나라도 있는지 확인합니다.
def _has_catalog_scope(scope: dict[str, Any]) -> bool:
    return any(
        isinstance(scope.get(key), list) and bool(scope.get(key))
        for key in ("dataset_keys", "dataset_families", "source_types", "db_keys")
    )


# 함수 설명: `_looks_like_specific_dataset_required_params()`는 등록된 특정 dataset의 필수 파라미터 질문을 전체 카탈로그 질문보다 먼저 식별합니다.
def _looks_like_specific_dataset_required_params(question: str, table_items: list[dict[str, Any]]) -> bool:
    lowered = str(question or "").lower()
    required_tokens = ("필수 파라미터", "필수조건", "필수 조건", "required param", "required_param")
    return bool(_explicit_dataset_items(question, table_items)) and any(token in lowered for token in required_tokens)


# 함수 설명: `_looks_like_datasets_by_required_param()`는 등록된 필수 조건 이름으로 데이터셋 목록을 묻는 질문만 전용 결정론 경로로 분류합니다.
def _looks_like_datasets_by_required_param(question: str, table_items: list[dict[str, Any]]) -> bool:
    lowered = str(question or "").lower()
    if _explicit_dataset_items(question, table_items):
        return False
    inventory_tokens = ("데이터셋", "dataset", "테이블", "table", "source", "소스")
    required_tokens = ("필요", "필수", "required")
    if not any(token in lowered for token in inventory_tokens) or not any(token in lowered for token in required_tokens):
        return False
    return bool(_required_param_names_in_question(question, table_items))


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
    has_row_value = any(token in lowered for token in row_value_tokens) or bool(re.search(r"\d+\s*건", lowered))
    has_business = any(token in lowered for token in business_tokens)
    has_time = any(token in lowered for token in time_tokens)
    has_list_word = any(token in lowered for token in ("테이블 목록", "데이터 목록", "table list"))
    return (has_row_value and (specific_dataset or has_business or has_time)) or (has_list_word and has_business and has_time)


# 함수 설명: `_looks_like_available_sources_question()`는 등록된 전체 카탈로그 목록 질문을 구체 dataset·조건 질문과 구분합니다.
def _looks_like_available_sources_question(question: str, table_items: list[dict[str, Any]] | None = None) -> bool:
    lowered = str(question or "").lower()
    catalog_subject_tokens = (
        "데이터셋",
        "데이터 세트",
        "데이터 셋",
        "데이터 목록",
        "데이터들",
        "테이블 카탈로그",
        "데이터 카탈로그",
        "테이블",
        "data catalog",
        "table catalog",
        "dataset",
        "data set",
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
        "리스트",
        "총",
        "몇 개",
        "몇개",
        "개수",
        "건수",
    )
    # "조회 가능한 데이터 list"처럼 데이터셋이라는 명사를 생략하고 한글·영문을
    # 섞어 쓴 목록 질문도 전체 Table Catalog 조회로 해석한다. 단순 "데이터"만으로
    # 판정하지 않고, 조회 가능성 표현과 목록 표현이 함께 있을 때만 일반 데이터 명사를
    # 카탈로그 subject로 인정해 실제 데이터 값 질문을 가로채지 않는다.
    availability_tokens = (
        "등록된",
        "등록되어",
        "조회 가능",
        "조회가능",
        "조회할 수 있",
        "사용 가능",
        "사용가능",
        "볼 수 있",
    )
    generic_data_list_pattern = r"(?<!메타)데이터\s*(?:목록|리스트|list|전체|전부)"
    has_generic_data_inventory_subject = bool(re.search(generic_data_list_pattern, lowered)) and any(
        token in lowered for token in availability_tokens
    )
    has_subject = any(token in lowered for token in catalog_subject_tokens) or has_generic_data_inventory_subject
    has_inventory_intent = any(token in lowered for token in inventory_tokens)
    if not has_subject or not has_inventory_intent:
        return False

    # 구체 업무용 데이터셋 선택, 특정 dataset 상세, 실제 행 조회는 전체 카탈로그 목록이 아닙니다.
    if (
        _looks_like_task_dataset_selection(lowered)
        or _looks_like_specific_dataset_required_params(question, table_items or [])
        or _looks_like_specific_dataset_detail(lowered)
        or _looks_like_table_data_question(lowered)
    ):
        return False
    return True


# 함수 설명: `_looks_like_available_domains_question()`은 특정 용어 설명이 아니라 등록된 도메인 전체 목록·건수를 묻는 표현을 판정합니다.
def _looks_like_available_domains_question(lowered: str) -> bool:
    if not any(token in lowered for token in ("도메인", "domain")):
        return False
    # `BG 도메인 전체 정보`처럼 특정 도메인의 설명 범위를 강조하는 `전체`는
    # inventory 요청이 아닙니다. 목록·건수 의도가 명시된 경우에만 전체 도메인
    # inventory로 전환해 구체 용어 질문의 기존 domain_info 검색을 보존합니다.
    list_tokens = (
        "목록",
        "list",
        "리스트",
        "나열",
        "뭐가 있",
        "무엇이 있",
    )
    count_tokens = (
        "총",
        "몇 개",
        "몇개",
        "개수",
        "건수",
    )
    inventory_qualifiers = (
        "등록된",
        "등록되어",
        "등록한",
        "조회 가능",
        "조회가능",
        "조회할 수 있",
        "사용 가능",
        "사용가능",
        "볼 수 있",
    )
    whole_scope_tokens = ("전체", "전부", "모두")
    has_explicit_list_or_count = any(token in lowered for token in list_tokens) or any(
        token in lowered for token in count_tokens
    )
    has_qualified_whole_scope = any(token in lowered for token in inventory_qualifiers) and any(
        token in lowered for token in whole_scope_tokens
    )
    return has_explicit_list_or_count or has_qualified_whole_scope


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
    if answer_mode not in {"available_sources", "scoped_sources", "available_domains"}:
        return ""
    lowered = str(question or "").lower()
    count_tokens = ("총", "몇 개", "몇개", "개수", "건수", "how many", "count")
    return "count" if any(token in lowered for token in count_tokens) else "list"


# 함수 설명: `_select_domain_items()`는 질문 token 점수로 관련 도메인 항목만 max_items 범위에서 선택합니다.
def _select_domain_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode in NO_DOMAIN_CANDIDATE_MODES:
        return []
    if answer_mode in LIST_ALL_DOMAIN_MODES:
        return items[: _list_limit(limit, items)]
    # 질문이 하나의 등록 도메인 항목을 직접 가리키는 설명형 질문이면, 공통 단어
    # (예: 공정, 필터, 사용)로 점수화된 다수 후보를 모두 전달하지 않습니다.
    # 이 경로는 section/key/display_name/aliases만으로 확인하므로 raw registration_text
    # 를 검색하거나 특정 업무 질문을 하드코딩하지 않습니다.
    direct_detail_items = _direct_domain_detail_items(question, items)
    if _looks_like_direct_domain_detail_question(question, direct_detail_items):
        return direct_detail_items

    candidate_limit = min(_list_limit(limit, items), DEFAULT_NARROW_DOMAIN_ITEM_LIMIT)
    if answer_mode == "calculation_logic_list":
        if _looks_like_product_aggregation_rule_question(question.lower()):
            return _select_product_aggregation_items(question, items, candidate_limit)
        selected = [item for item in items if str(item.get("section") or "") in CALCULATION_SECTIONS]
        ranked = _ranked(question, selected, candidate_limit)
        return ranked if ranked else selected[:candidate_limit]
    if answer_mode == "product_domain_info":
        product_items = [item for item in items if str(item.get("section") or "") in PRODUCT_DOMAIN_SECTIONS]
        if _looks_like_product_domain_inventory(question.lower()):
            return product_items[:candidate_limit]
        exact_items = _explicit_named_items(question, product_items)
        if exact_items:
            return exact_items[:candidate_limit]
        ranked = _ranked(question + " product_terms 제품군 제품 조건", product_items, candidate_limit)
        return ranked if ranked else product_items[:candidate_limit]
    if answer_mode == "product_condition":
        product_items = [item for item in items if str(item.get("section") or "") in PRODUCT_DOMAIN_SECTIONS]
        exact_items = _explicit_named_items(question, product_items)
        if exact_items:
            return exact_items[:candidate_limit]
        ranked = _ranked(question + " product_terms 제품군 제품 조건", product_items, candidate_limit)
        return ranked if ranked else product_items[:candidate_limit]
    if answer_mode == "product_token_rule":
        function_items = [item for item in items if str(item.get("section") or "") == "pandas_function_cases"]
        ranked = _ranked(question + " product token match_product_tokens 제품 토큰", function_items, candidate_limit)
        return ranked if ranked else function_items[:candidate_limit]
    if answer_mode == "process_group":
        process_group_items = [
            item
            for item in items
            if str(item.get("section") or "") == "process_groups"
        ]
        if _looks_like_process_group_inventory(question.lower()):
            return process_group_items[:candidate_limit]
        exact_items = _explicit_named_items(question, process_group_items)
        if exact_items:
            return exact_items[:candidate_limit]
        return _ranked(question + " process_groups 공정", process_group_items, candidate_limit)
    if answer_mode == "term_definition":
        exact_items = _explicit_named_items(question, items)
        if exact_items:
            return exact_items[:candidate_limit]
        return _ranked(question + " quantity_terms metric_terms analysis_recipes", items, candidate_limit)
    if answer_mode in {"domain_info", "question_to_dataset"}:
        return _ranked(question, items, candidate_limit)
    selected = _ranked(question, items, candidate_limit)
    # 점수가 0인 임의의 앞 5건은 질문 근거가 아니므로 후보와 confidence를 오염시키지 않습니다.
    return selected


# 함수 설명: `_direct_domain_detail_items()`는 key·표시명·별칭의 직접 일치 또는 모든 이름 token 일치로 단 하나의 도메인 항목만 안전하게 찾습니다.
def _direct_domain_detail_items(question: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact_items = _explicit_named_items(question, items)
    if len(exact_items) == 1:
        return exact_items
    if len(exact_items) > 1:
        return []

    question_tokens = _literal_tokens(question)
    if len(question_tokens) < 2:
        return []
    matched: list[dict[str, Any]] = []
    for item in items:
        payload = _dict(item.get("payload"))
        aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
        names = [payload.get("display_name"), item.get("display_name"), *aliases]
        if any(
            len(name_tokens := _literal_tokens(name)) >= 2 and name_tokens.issubset(question_tokens)
            for name in names
        ):
            matched.append(item)
    return matched if len(matched) == 1 else []


# 함수 설명: `_looks_like_direct_domain_detail_question()`은 단일 도메인 항목의 설명·용도 질문만 좁은 후보 경로로 허용합니다.
def _looks_like_direct_domain_detail_question(question: str, items: list[dict[str, Any]]) -> bool:
    if len(items) != 1:
        return False
    lowered = str(question or "").lower()
    if _looks_like_data_value_question(lowered):
        return False
    inventory_cues = ("목록", "리스트", "list", "전체", "전부", "모든", "몇 개", "몇개", "건수")
    if any(cue in lowered for cue in inventory_cues):
        return False
    detail_cues = ("어떤", "어떻게", "사용", "용도", "설명", "정의", "규칙", "조건", "필터", "뭐야", "무엇")
    return any(cue in lowered for cue in detail_cues)


# 함수 설명: `_literal_tokens()`는 별칭·표시명과 질문의 직접 표현만 비교하도록 별칭 확장 없이 token을 정규화합니다.
def _literal_tokens(value: Any) -> set[str]:
    return {
        token.strip().lower()
        for token in re.findall(r"[0-9a-zA-Z가-힣_/.-]+", str(value or ""))
        if len(token.strip()) >= 2
    }


# 함수 설명: `_looks_like_product_domain_inventory()`는 특정 제품 하나가 아니라 등록된 제품 그룹 전체를 묻는 표현인지 판정합니다.
def _looks_like_product_domain_inventory(lowered: str) -> bool:
    group_tokens = ("제품 그룹", "제품그룹", "제품군", "product group")
    inventory_tokens = ("등록", "목록", "뭐가 있", "무엇이 있", "전부", "전체", "관련")
    return any(token in lowered for token in group_tokens) and any(token in lowered for token in inventory_tokens)


# 함수 설명: 등록된 공정 그룹 전체 목록·건수를 묻는 질문인지 판정합니다.
def _looks_like_process_group_inventory(lowered: str) -> bool:
    inventory_tokens = (
        "목록",
        "리스트",
        "list",
        "전부",
        "전체",
        "뭐가 있",
        "무엇이 있",
        "몇 개",
        "몇개",
        "개수",
        "건수",
    )
    return any(
        token in lowered
        for token in ("공정 그룹", "공정그룹", "공정 group", "공정group", "process group")
    ) and any(
        token in lowered for token in inventory_tokens
    )


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
def _select_table_items(
    question: str,
    answer_mode: str,
    items: list[dict[str, Any]],
    limit: int,
    catalog_scope: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if answer_mode in LIST_ALL_DOMAIN_MODES:
        return []
    if answer_mode in LIST_ALL_TABLE_MODES:
        return items[: _list_limit(limit, items)]
    if answer_mode == "scoped_sources":
        selected = _filter_catalog_items_by_scope(items, _dict(catalog_scope))
        return sorted(selected, key=_catalog_dataset_sort_key)[:limit]
    if answer_mode == "dataset_comparison":
        explicit_items = _explicit_dataset_items(question, items)
        return explicit_items[:limit] if len(explicit_items) >= 2 else []
    if answer_mode == "inventory_followup_missing_context":
        return []
    if answer_mode == "datasets_by_required_param":
        requested_params = _required_param_names_in_question(question, items)
        selected = [
            item
            for item in items
            if any(name in _table_required_param_names(item) for name in requested_params)
        ]
        return sorted(selected, key=_catalog_dataset_sort_key)[:limit]
    explicit_items = _explicit_dataset_items(question, items)
    if explicit_items and (
        answer_mode in {"dataset_sql", "dataset_detail", "required_params"}
        or len(explicit_items) > 1
    ):
        return explicit_items[:limit]
    if answer_mode in {"dataset_sql", "dataset_detail", "required_params", "question_to_dataset"}:
        selected = _ranked(question, items, limit)
        return _merge_unique_items(explicit_items, selected if selected else items[: min(limit, 5)])[:limit]
    if answer_mode == "data_analysis_redirect":
        return []
    selected = _ranked(question, items, limit)
    return _merge_unique_items(explicit_items, selected)[: min(limit, 5)]


# 함수 설명: `_table_required_param_names()`는 Table Catalog의 실제 required_params만 이름 목록으로 정규화합니다.
def _table_required_param_names(item: dict[str, Any]) -> list[str]:
    payload = _dict(item.get("payload"))
    value = payload.get("required_params")
    if isinstance(value, dict):
        values = list(value.keys())
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = [value]
    names = [str(value).strip().upper() for value in values if str(value or "").strip()]
    return list(dict.fromkeys(names))


# 함수 설명: `_required_param_names_in_question()`는 등록된 필수 조건 중 질문에 독립 토큰으로 등장한 이름만 반환합니다.
def _required_param_names_in_question(question: str, table_items: list[dict[str, Any]]) -> list[str]:
    known_names = sorted(
        {name for item in table_items for name in _table_required_param_names(item)},
        key=lambda value: (-len(value), value),
    )
    return [name for name in known_names if _name_in_question(name, question)]


# 함수 설명: `_catalog_dataset_sort_key()`는 목록형 응답을 데이터셋 키 기준으로 안정적으로 정렬합니다.
def _catalog_dataset_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    key = str(item.get("dataset_key") or item.get("key") or "").strip()
    return key.casefold(), key


# 함수 설명: `_table_source_type()`은 Table Catalog의 payload 또는 source_config에서 등록된 연결 방식을 하나로 읽습니다.
def _table_source_type(item: dict[str, Any]) -> str:
    payload = _dict(item.get("payload"))
    source_config = _dict(payload.get("source_config"))
    return str(payload.get("source_type") or source_config.get("source_type") or "").strip()


# 함수 설명: `_table_db_key()`는 Table Catalog의 source_config에서 DB 또는 문서 소스 식별자를 읽습니다.
def _table_db_key(item: dict[str, Any]) -> str:
    payload = _dict(item.get("payload"))
    source_config = _dict(payload.get("source_config"))
    return str(source_config.get("db_key") or "").strip()


# 함수 설명: `_filter_catalog_items_by_scope()`는 검증된 목록 키와 family·source·DB 범위를 모두 만족하는 Table Catalog만 남깁니다.
def _filter_catalog_items_by_scope(items: list[dict[str, Any]], scope: dict[str, Any]) -> list[dict[str, Any]]:
    dataset_keys = {
        str(value).strip()
        for value in scope.get("dataset_keys", [])
        if str(value or "").strip()
    }
    families = {
        str(value).strip().casefold()
        for value in scope.get("dataset_families", [])
        if str(value or "").strip()
    }
    source_types = {
        str(value).strip().casefold()
        for value in scope.get("source_types", [])
        if str(value or "").strip()
    }
    db_keys = {
        str(value).strip().casefold()
        for value in scope.get("db_keys", [])
        if str(value or "").strip()
    }
    if not any((dataset_keys, families, source_types, db_keys)):
        return []
    selected: list[dict[str, Any]] = []
    for item in items:
        key = str(item.get("dataset_key") or item.get("key") or "").strip()
        family = str(_dict(item.get("payload")).get("dataset_family") or "").strip().casefold()
        source_type = _table_source_type(item).casefold()
        db_key = _table_db_key(item).casefold()
        if dataset_keys and key not in dataset_keys:
            continue
        if families and family not in families:
            continue
        if source_types and source_type not in source_types:
            continue
        if db_keys and db_key not in db_keys:
            continue
        selected.append(item)
    return selected


# 함수 설명: `_catalog_scope_total_count()`는 전체 목록이 아닌 범위 목록의 정확한 후보 수를 summary에 전달합니다.
def _catalog_scope_total_count(answer_mode: str, items: list[dict[str, Any]], scope: dict[str, Any]) -> int | None:
    if answer_mode != "scoped_sources":
        return None
    return len(_filter_catalog_items_by_scope(items, scope))


# 함수 설명: 질문에 dataset key나 표시명이 직접 등장한 카탈로그 항목을 빠짐없이 고정합니다.
def _explicit_dataset_items(question: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _explicit_named_items(question, items, include_dataset_key=True)


# 함수 설명: key·display_name·aliases가 질문에 직접 등장한 메타데이터 항목을 원래 순서대로 찾습니다.
def _explicit_named_items(
    question: str,
    items: list[dict[str, Any]],
    *,
    include_dataset_key: bool = False,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for item in items:
        payload = _dict(item.get("payload"))
        alias_value = payload.get("aliases")
        aliases = alias_value if isinstance(alias_value, list) else []
        names = [
            item.get("key"),
            payload.get("display_name"),
            item.get("display_name"),
            *aliases,
        ]
        if include_dataset_key:
            names.insert(0, item.get("dataset_key"))
        if any(_name_in_question(name, question) for name in names):
            matched.append(item)
    return matched


# 함수 설명: 짧은 영문 key나 `BM공정` 별칭이 WBM 같은 더 긴 token 내부에서 잘못 매칭되지 않도록 경계를 확인합니다.
def _name_in_question(name: Any, question: str) -> bool:
    text = str(name or "").strip().lower()
    lowered = str(question or "").lower()
    if not text or not lowered:
        return False
    # 영문으로 등록된 일반 제품명도 자연스러운 한글 음역 질문에서 같은 명시 이름으로 취급합니다.
    name_equivalents = {"mobile": ("모바일",)}
    if any(alias in lowered for alias in name_equivalents.get(text, ())):
        return True
    starts_with_ascii_token = bool(re.match(r"[a-z0-9_]", text))
    ends_with_ascii_token = bool(re.search(r"[a-z0-9_]$", text))
    if starts_with_ascii_token or ends_with_ascii_token:
        prefix = r"(?<![0-9a-z_])" if starts_with_ascii_token else ""
        suffix = r"(?![0-9a-z_])" if ends_with_ascii_token else ""
        return bool(
            re.search(
                rf"{prefix}{re.escape(text)}{suffix}",
                lowered,
            )
        )
    return text in lowered


# 함수 설명: 명시 후보와 점수 후보를 metadata 식별자 기준으로 중복 없이 합칩니다.
def _merge_unique_items(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for item in group:
            marker = (
                str(item.get("section") or ""),
                str(item.get("dataset_key") or ""),
                str(item.get("key") or ""),
            )
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(item)
    return merged


# 함수 설명: `_select_filter_items()`는 메인 필터 후보를 질문 token과 별칭 일치 기준으로 선택합니다.
def _select_filter_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode in {
        "available_sources",
        "scoped_sources",
        "dataset_comparison",
        "inventory_followup_missing_context",
        "available_domains",
        "datasets_by_required_param",
    }:
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
    scoped_total_count: int | None = None,
) -> dict[str, Any]:
    if answer_mode not in {"available_sources", "scoped_sources"}:
        return {}
    if answer_mode == "scoped_sources":
        total_count = max(0, int(scoped_total_count or 0))
        response_limit = max(1, int(limit))
        total_count_exact = not bool(table_load.get("truncated"))
        return {
            "request_kind": request_kind or "list",
            "total_count": total_count,
            "returned_count": max(0, int(returned_count)),
            "truncated": (not total_count_exact) or returned_count < total_count,
            "total_count_exact": total_count_exact,
            "limit": response_limit,
            "response_limit": response_limit,
            "load_limit": response_limit,
        }
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


# 함수 설명: `_domain_summary()`는 도메인 목록 질문의 전체 조회 건수와 실제 반환 건수를 분리해 기록합니다.
def _domain_summary(
    answer_mode: str,
    request_kind: str,
    loaded_item_count: int,
    returned_count: int,
    limit: int,
    domain_load: dict[str, Any],
) -> dict[str, Any]:
    if answer_mode != "available_domains":
        return {}
    return _inventory_summary(request_kind, loaded_item_count, returned_count, limit, domain_load)


# 함수 설명: `_inventory_summary()`는 목록 대상과 무관하게 전체·표시·잘림 건수를 동일 계약으로 계산합니다.
def _inventory_summary(
    request_kind: str,
    loaded_item_count: int,
    returned_count: int,
    limit: int,
    load: dict[str, Any],
) -> dict[str, Any]:
    try:
        load_count = max(0, int(load.get("count", loaded_item_count)))
    except Exception:
        load_count = loaded_item_count
    try:
        lower_bound_count = max(0, int(load.get("total_count_lower_bound", load_count)))
    except Exception:
        lower_bound_count = load_count
    try:
        load_limit = max(1, int(load.get("limit", limit)))
    except Exception:
        load_limit = max(1, int(limit))
    response_limit = max(1, int(limit))
    total_count = max(loaded_item_count, load_count, lower_bound_count)
    total_count_exact = not bool(load.get("truncated"))
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


# 함수 설명: `_refresh_inventory_summaries()`는 바이트 제한으로 행이 줄어든 뒤 목록 summary와 실제 rows 건수를 다시 맞춥니다.
def _refresh_inventory_summaries(context: dict[str, Any]) -> None:
    returned_count = len(context.get("candidate_rows")) if isinstance(context.get("candidate_rows"), list) else 0
    for summary_key in ("catalog_summary", "domain_summary"):
        summary = _dict(context.get(summary_key))
        if not summary:
            continue
        try:
            total_count = max(0, int(summary.get("total_count", returned_count)))
        except Exception:
            total_count = returned_count
        total_count_exact = bool(summary.get("total_count_exact", True))
        summary["returned_count"] = returned_count
        summary["truncated"] = (not total_count_exact) or returned_count < total_count
        context[summary_key] = summary


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
    aliases = {
        "생산량": {"production", "output", "실적"},
        "재공": {"wip"},
        "투입": {"input"},
        "쿼리": {"query", "sql"},
        "모바일": {"mobile"},
    }
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
    keys = {"display_name", "aliases", "description", "usage_rule"} if answer_mode == "available_domains" else {
        "display_name",
        "aliases",
        "description",
        "usage_rule",
        "column",
        "aggregation_method",
    }
    if answer_mode == "process_group":
        keys.update({"processes", "process_groups", "members"})
    if answer_mode == "term_definition":
        keys.update(
            {
                "condition",
                "conditions",
                "condition_by_family",
                "condition_by_dataset",
                "filters",
                "value",
                "operator",
            }
        )
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
            "registration_text": item.get("registration_text") if answer_mode != "available_domains" else "",
        }
    )


# 함수 설명: `_project_table_item()`는 테이블 문서에서 dataset/source/컬럼/조회 설명만 안전하게 projection합니다.
def _project_table_item(item: dict[str, Any], answer_mode: str) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    payload_keys = {
        "display_name", "dataset_family", "source_type", "required_params", "required_param_mappings",
        "filter_mappings", "standard_column_aliases", "description", "columns", "quantity_column",
        "quantity_columns", "metric_columns", "measure_columns", "value_columns", "column", "aggregation_column",
        "default_detail_columns",
    }
    if answer_mode in {"datasets_by_required_param", "dataset_comparison"}:
        payload_keys.update({"selection_criteria", "usage_rule"})
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
    if answer_mode in {"dataset_sql", "available_sources", "scoped_sources"}:
        return [_dataset_row(item) for item in table_items]
    if answer_mode == "dataset_comparison":
        return [_dataset_comparison_row(item) for item in table_items]
    if answer_mode == "available_domains":
        return [_domain_inventory_row(item) for item in domain_items]
    if answer_mode == "dataset_detail":
        return [_dataset_detail_row(item) for item in table_items]
    if answer_mode == "required_params":
        return [_required_param_row(item) for item in table_items] + [_filter_row(item) for item in filter_items]
    if answer_mode == "datasets_by_required_param":
        return [_required_param_dataset_row(item) for item in table_items]
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
            "default_detail_columns": _compact_list(payload.get("default_detail_columns")),
            "quantity_columns": _quantity_columns(payload),
            "filter_mappings": _compact_list(payload.get("filter_mappings")),
            "description": payload.get("description"),
        }
    )


# 함수 설명: `_dataset_comparison_row()`는 명시된 데이터셋의 등록된 용도·기준·연결·필수 조건만 같은 형식으로 비교합니다.
def _dataset_comparison_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    source_config = _dict(payload.get("source_config"))
    selection_criteria = _dict(payload.get("selection_criteria"))
    dataset_key = item.get("dataset_key") or item.get("key")
    return {
        "데이터셋": payload.get("display_name") or item.get("display_name") or dataset_key,
        "데이터셋 키": dataset_key,
        "용도·사용 시점": _registered_dataset_usage(payload, selection_criteria),
        "기준 구분": _registered_time_scope(selection_criteria),
        "연결 방식": _source_type_display(payload.get("source_type") or source_config.get("source_type")),
        "필수 조건": _compact_list(_table_required_param_names(item)) or "없음",
    }


# 함수 설명: `_registered_dataset_usage()`는 Table Catalog에 등록된 use_when·usage_rule·description 순서로 용도를 읽고 미등록 값은 추정하지 않습니다.
def _registered_dataset_usage(payload: dict[str, Any], selection_criteria: dict[str, Any]) -> str:
    for value in (
        selection_criteria.get("use_when"),
        payload.get("usage_rule"),
        payload.get("description"),
    ):
        text = _compact_list(value)
        if text:
            return text
    return "등록된 용도 설명 없음"


# 함수 설명: `_registered_time_scope()`는 명시적으로 저장된 time_scope만 사용자용 기준 구분으로 표시합니다.
def _registered_time_scope(selection_criteria: dict[str, Any]) -> str:
    raw = str(selection_criteria.get("time_scope") or "").strip().casefold()
    labels = {
        "current_day": "당일/현재 기준",
        "history": "이력/과거일 기준",
    }
    return labels.get(raw, "등록된 기준 구분 없음")


# 함수 설명: `_source_type_display()`는 Table Catalog 연결 방식의 내부 식별자를 짧은 사용자 표시명으로 바꿉니다.
def _source_type_display(value: Any) -> str:
    text = str(value or "").strip()
    return {"oracle": "Oracle", "goodocs": "Goodocs"}.get(text.casefold(), text or "등록된 연결 방식 없음")


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


# 함수 설명: `_required_param_dataset_row()`는 필수 조건별 데이터셋 목록을 고정된 사용자용 네 개 열로 projection합니다.
def _required_param_dataset_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    dataset_key = item.get("dataset_key") or item.get("key")
    selection_criteria = _dict(payload.get("selection_criteria"))
    registered_usage = _compact_list(selection_criteria.get("use_when"))
    return {
        "데이터셋": payload.get("display_name") or item.get("display_name") or dataset_key,
        "데이터셋 키": dataset_key,
        "용도": payload.get("description") or payload.get("usage_rule") or registered_usage or payload.get("display_name") or "등록된 용도 설명 없음",
        "필수 조건": _compact_list(_table_required_param_names(item)),
    }


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
            "processes": _compact_list(
                payload.get("processes")
                or payload.get("process_groups")
                or payload.get("members")
            ),
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
            "registration_text": item.get("registration_text"),
        }
    )


# 함수 설명: `_domain_inventory_row()`는 전체 도메인 목록에서 복잡한 조건 JSON을 제외하고 사람이 식별할 핵심 필드만 남깁니다.
def _domain_inventory_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    return _omit_empty(
        {
            "metadata_type": "domain",
            "section": item.get("section"),
            "key": item.get("key"),
            "display_name": payload.get("display_name") or item.get("display_name") or item.get("key"),
            "aliases": _compact_list(payload.get("aliases")),
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


# 함수 설명: 도메인 문서의 내부 registration trace는 제거하되, 비밀값을 마스킹한 등록 원문만 별도 표시 필드로 보존합니다.
def _sanitize_domain_item(item: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize(item)
    registration_trace = _dict(item.get("registration_trace"))
    registration_text = _sanitize_registration_text(registration_trace.get("raw_text"))
    if registration_text:
        sanitized["registration_text"] = registration_text
    return sanitized


# 함수 설명: 과거 문서에 마스킹되지 않은 credential이 있더라도 QA 응답으로 노출되지 않도록 등록 원문을 재마스킹합니다.
def _sanitize_registration_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    pattern = re.compile(
        r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential|mongo[_-]?uri)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
    )
    text = pattern.sub(r"\1\2***", text)
    text = re.sub(r"(?i)\b(bearer)\s+[^\s,;\"'}]+", r"\1 ***", text)
    text = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@",
        r"\1***:***@",
        text,
    )
    return text[:REGISTRATION_TEXT_LIMIT]


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
