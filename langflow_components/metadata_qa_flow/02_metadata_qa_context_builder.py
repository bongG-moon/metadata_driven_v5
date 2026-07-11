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
LIST_ALL_TABLE_MODES = {"available_sources"}
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_BYTES = 65536


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

    domain_items, domain_load = _extract_items(domain_items_value, "domain_items")
    table_items, table_load = _extract_items(table_catalog_items_value, "table_catalog_items")
    filter_items, filter_load = _extract_items(main_flow_filters_value, "main_flow_filters")
    domain_items = [_sanitize(item) for item in domain_items]
    table_items = [_sanitize(item) for item in table_items]
    filter_items = [_sanitize(item) for item in filter_items]

    answer_mode = _infer_answer_mode(question)
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
    warnings = []
    if not source_refs:
        warnings.append({"type": "metadata_qa_no_matches", "message": "질문과 직접 매칭되는 메타데이터 후보가 없습니다."})

    next_payload = deepcopy(payload)
    next_payload["metadata_route"] = {
        "route": "metadata_qa",
        "answer_mode": answer_mode,
        "confidence": "high" if source_refs else "low",
    }
    context = {
        "question": question,
        "answer_mode": answer_mode,
        "load_summary": load_summary,
        "matched_domain_items": matched_domain,
        "matched_datasets": matched_tables,
        "matched_filters": matched_filters,
        "candidate_rows": candidate_rows,
        "source_refs": source_refs,
    }
    if answer_mode == "available_sources":
        context["matched_datasets"] = []
    context, context_trimmed = _fit_context_bytes(context, byte_limit)
    if context_trimmed:
        warnings.append({"type": "metadata_qa_context_trimmed", "message": f"LLM context를 {byte_limit} bytes 이하로 축소했습니다."})
    next_payload["metadata_qa_context"] = context
    trace = _dict(next_payload.get("trace"))
    trace.setdefault("warnings", []).extend(warnings)
    trace.setdefault("errors", []).extend(_load_errors(load_summary))
    trace.setdefault("inspection", {})["metadata_qa_context"] = {
        "stage": "02_metadata_qa_context_builder",
        "status": "ok" if source_refs else "warning",
        "answer_mode": answer_mode,
        "domain_match_count": len(matched_domain),
        "dataset_match_count": len(matched_tables),
        "filter_match_count": len(matched_filters),
        "context_bytes": _json_bytes(context),
        "context_trimmed": context_trimmed,
    }
    next_payload["trace"] = trace
    return next_payload


def _extract_items(value: Any, key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return [], {}
    items = data.get(key)
    load = data.get("metadata_load") if isinstance(data.get("metadata_load"), dict) else {}
    return [deepcopy(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else [], deepcopy(load)


def _infer_answer_mode(question: str) -> str:
    lowered = question.lower()
    if any(token in lowered for token in ("쿼리", "sql", "query", "select", "with문")):
        return "dataset_sql"
    if _looks_like_data_value_question(lowered):
        return "data_analysis_redirect"
    if _looks_like_available_sources_question(lowered):
        return "available_sources"
    if any(token in lowered for token in ("필수 파라미터", "필수조건", "필수 조건", "required param", "required_param")):
        return "required_params"
    if any(token in lowered for token in ("어떤 데이터", "무슨 데이터", "어느 데이터", "어떤 테이블", "무슨 테이블", "어떤 source", "무슨 source", "어떤 소스")):
        return "question_to_dataset"
    if any(token in lowered for token in ("공정 그룹", "세부 공정", "포함", "차수", "공정에는")) and "공정" in lowered:
        return "process_group"
    if "pop" in lowered and any(token in lowered for token in ("도메인", "정의", "무엇", "뭐야", "설명")):
        return "product_domain_info"
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


def _looks_like_data_value_question(lowered: str) -> bool:
    if any(token in lowered for token in ("메타데이터", "metadata", "등록", "정의", "무슨 컬럼", "어떤 컬럼", "쿼리", "sql", "query", "데이터셋", "필수 조건")):
        return False
    has_time_or_target = any(token in lowered for token in ("오늘", "어제", "전일", "금일", "현시간", "현재", "/", "월", "일"))
    has_metric = any(token in lowered for token in ("생산량", "생산 실적", "실적", "재공", "수량", "투입", "input", "output", "out", "assign", "장비"))
    asks_value = any(token in lowered for token in ("알려줘", "확인", "보여줘", "몇", "상위", "많은"))
    return has_metric and asks_value and has_time_or_target


def _looks_like_available_sources_question(lowered: str) -> bool:
    catalog_tokens = ("조회 가능", "조회가능", "데이터셋", "데이터들", "데이터 목록", "data catalog", "연결 방식", "연결방식")
    list_tokens = ("목록", "전체", "각 데이터", "각 source", "각 소스", "뭐가", "무엇", "list", "표", "정리", "보여")
    return any(token in lowered for token in catalog_tokens) and any(token in lowered for token in list_tokens)


def _select_domain_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode == "calculation_logic_list":
        selected = [item for item in items if str(item.get("section") or "") in CALCULATION_SECTIONS]
        return selected[:limit]
    if answer_mode in {"product_domain_info", "product_condition", "product_token_rule"}:
        return _ranked(question + " pop product 제품", items, limit)
    if answer_mode == "process_group":
        return _ranked(question + " process_groups 공정", items, limit)
    if answer_mode == "term_definition":
        return _ranked(question + " quantity_terms metric_terms analysis_recipes", items, limit)
    if answer_mode in {"domain_info", "question_to_dataset"}:
        return _ranked(question, items, limit)
    if answer_mode == "data_analysis_redirect":
        return []
    selected = _ranked(question, items, limit)
    return selected if selected else items[: min(limit, 5)]


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


def _select_filter_items(question: str, answer_mode: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if answer_mode == "available_sources":
        return []
    if answer_mode in {"required_params", "term_definition", "question_to_dataset"}:
        return _ranked(question, items, min(limit, 6))
    if answer_mode == "data_analysis_redirect":
        return []
    return _ranked(question, items, min(limit, 6))


def _list_limit(limit: int, items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    return min(len(items), limit)


def _ranked(question: str, items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    tokens = _tokens(question)
    scored = []
    for item in items:
        score = _score(tokens, item)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def _score(tokens: set[str], item: dict[str, Any]) -> int:
    if not tokens:
        return 0
    blob = _text_blob(item).lower()
    score = sum(1 for token in tokens if token and token in blob)
    payload = _dict(item.get("payload"))
    display = str(payload.get("display_name") or item.get("display_name") or item.get("key") or item.get("dataset_key") or "").lower()
    score += sum(2 for token in tokens if token and token in display)
    return score


def _tokens(text: str) -> set[str]:
    raw = re.findall(r"[0-9a-zA-Z가-힣_/.-]+", str(text or "").lower())
    aliases = {"생산량": {"production", "output", "실적"}, "재공": {"wip"}, "투입": {"input"}, "쿼리": {"query", "sql"}}
    result = {token.strip() for token in raw if len(token.strip()) >= 2}
    for token in list(result):
        result.update(aliases.get(token, set()))
    return result


def _project_domain_item(item: dict[str, Any], answer_mode: str) -> dict[str, Any]:
    payload = _dict(item.get("payload"))
    keys = {"display_name", "aliases", "description", "usage_rule", "column", "aggregation_method"}
    if answer_mode == "process_group":
        keys.update({"processes", "process_groups", "members"})
    if answer_mode in {"product_domain_info", "product_condition", "product_token_rule"}:
        keys.update({"conditions", "condition", "patterns", "tokens", "include", "exclude", "product_key_columns"})
    if answer_mode == "calculation_logic_list":
        keys.update({"formula", "required_inputs", "outputs", "applicability", "conditions", "pseudocode", "function_name", "logic"})
    return _omit_empty(
        {
            "section": item.get("section"),
            "key": item.get("key"),
            "status": item.get("status"),
            "payload": _project_dict(payload, keys),
        }
    )


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


def _project_dict(value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: deepcopy(item) for key, item in value.items() if key in allowed and item not in (None, "", [], {})}


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
    return fitted, trimmed


def _truncate_context_strings(value: Any, key_name: str = "") -> Any:
    if isinstance(value, dict):
        return {key: _truncate_context_strings(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_context_strings(item, key_name) for item in value]
    if isinstance(value, str):
        limit = 16000 if key_name == "query_template" else 2000
        return value if len(value) <= limit else value[:limit] + "..."
    return deepcopy(value)


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))


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
            "quantity_columns": _quantity_columns(payload),
            "filter_mappings": _compact_list(payload.get("filter_mappings")),
            "description": payload.get("description"),
        }
    )


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
            "description": payload.get("description") or payload.get("usage_rule"),
        }
    )


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


def _source_refs(domain_items: list[dict[str, Any]], table_items: list[dict[str, Any]], filter_items: list[dict[str, Any]]) -> list[dict[str, str]]:
    refs = []
    refs.extend({"metadata_type": "domain", "section": str(item.get("section") or ""), "key": str(item.get("key") or "")} for item in domain_items)
    refs.extend({"metadata_type": "table_catalog", "key": str(item.get("dataset_key") or item.get("key") or "")} for item in table_items)
    refs.extend({"metadata_type": "main_flow_filter", "key": str(item.get("filter_key") or item.get("key") or "")} for item in filter_items)
    return [ref for ref in refs if ref.get("key")]


def _sanitize(item: dict[str, Any]) -> dict[str, Any]:
    value = _sanitize_value(item)
    return value if isinstance(value, dict) else {}


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


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)


def _compact_load(load: dict[str, Any]) -> dict[str, Any]:
    return _omit_empty(
        {
            "status": load.get("status"),
            "metadata_kind": load.get("metadata_kind"),
            "database": load.get("database"),
            "collection_name": load.get("collection_name"),
            "count": load.get("count"),
            "errors": load.get("errors"),
        }
    )


def _load_errors(load_summary: dict[str, Any]) -> list[dict[str, Any]]:
    errors = []
    for load in load_summary.values():
        if isinstance(load, dict):
            errors.extend(item for item in load.get("errors", []) if isinstance(item, dict))
    return errors


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


def _text_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _compact_list(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value[:12] if str(item or "").strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value) if value not in (None, "", [], {}) else ""


def _omit_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


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
