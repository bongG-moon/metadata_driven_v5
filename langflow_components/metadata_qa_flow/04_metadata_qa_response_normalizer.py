# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 메타데이터 QA 응답 정규화기
# 역할: Langflow Agent/LLM 응답을 메타데이터 QA 표준 페이로드로 정규화합니다.
# 주요 입력: 페이로드 (payload) · 필수, LLM 응답 (llm_response)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: LLM 응답을 정규화하고 authoritative context로 표와 source 참조를 보강해 결정론적 QA 결과를 만듭니다.
# 유지보수 포인트: 표의 실제 rows와 source 참조는 메타데이터 context를 authoritative 근거로 사용하고 LLM 임의 값을 그대로 신뢰하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

AUTHORITATIVE_CONTEXT_TABLE_TYPES = {
    "available_sources",
    "required_params",
    "calculation_logic_list",
    "question_to_dataset",
}
ALWAYS_USE_CONTEXT_TABLE_TYPES = {"available_sources"}


# 주요 함수: LLM QA 결과를 근거 문맥과 결합해 안정적인 답변 계약으로 정규화합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def normalize_metadata_qa_response(payload_value: Any, llm_response_value: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    context = _dict(payload.get("metadata_qa_context"))
    question = str(_dict(payload.get("request")).get("question") or context.get("question") or "").strip()
    parsed = _parse_llm_response(llm_response_value)
    fallback = _fallback_answer(question, context)
    answer_type = str(parsed.get("answer_type") or fallback.get("answer_type") or context.get("answer_mode") or "general_metadata_search").strip()
    answer_message = str(parsed.get("answer_message") or parsed.get("answer") or fallback["answer_message"]).strip()
    summary = str(parsed.get("summary") or fallback["summary"]).strip()
    parsed_sections = _dict(parsed.get("answer_sections"))
    parsed_table = _dict(parsed.get("table")) or _dict(parsed_sections.get("detail_table"))
    fallback_table = _service_table(answer_type, fallback["table"])
    use_context_table = _should_use_context_table(answer_type, context, parsed_table, fallback_table)
    if use_context_table:
        answer_message = str(fallback["answer_message"]).strip()
        summary = str(fallback["summary"]).strip()
    table = fallback_table if use_context_table else (parsed_table or fallback_table)
    columns = _string_list(table.get("columns")) or _columns_from_rows(_row_list(table.get("rows")))
    rows = _row_list(table.get("rows"))
    source_refs = _source_refs_for_answer(answer_type, context, parsed, use_context_table)
    warnings = _list(parsed.get("warnings"))
    sql_blocks = _list(parsed_sections.get("sql_blocks")) or _list(parsed.get("sql_blocks")) or fallback.get("sql_blocks", [])
    answer_sections = parsed_sections or fallback.get("answer_sections") or _build_answer_sections(answer_type, answer_message, summary, table, sql_blocks, source_refs, context, warnings)
    if use_context_table:
        answer_sections = _sync_answer_sections_from_context(answer_sections, answer_type, answer_message, summary, table, source_refs)
    answer_sections = _compact_answer_sections(answer_sections, columns, len(rows))

    next_payload = deepcopy(payload)
    next_payload["response_type"] = "metadata_qa"
    next_payload["status"] = "ok"
    next_payload["direct_response_ready"] = True
    next_payload["answer_type"] = answer_type
    next_payload["answer_message"] = answer_message
    next_payload["answer_sections"] = answer_sections
    next_payload["metadata_qa"] = {
        "summary": summary,
        "answer_type": answer_type,
        "answer_mode": context.get("answer_mode") or _dict(next_payload.get("metadata_route")).get("answer_mode"),
        "source_refs": source_refs,
    }
    next_payload["data"] = {"columns": columns, "rows": rows, "row_count": len(rows)}
    next_payload["state"] = {
        **_dict(next_payload.get("state")),
        "current_metadata_qa": {
            "question": question,
            "answer_mode": next_payload["metadata_qa"].get("answer_mode"),
            "source_refs": source_refs[:10],
        },
    }
    trace = _dict(next_payload.get("trace"))
    trace.setdefault("warnings", []).extend(warnings)
    trace.setdefault("inspection", {})["metadata_qa_response"] = {
        "stage": "04_metadata_qa_response_normalizer",
        "status": "ok",
        "answer_type": answer_type,
        "row_count": len(rows),
        "used_llm_response": bool(parsed),
        "used_context_table": use_context_table,
    }
    next_payload["trace"] = trace
    return next_payload


def _compact_answer_sections(answer_sections: dict[str, Any], columns: list[str], row_count: int) -> dict[str, Any]:
    sections = deepcopy(answer_sections) if isinstance(answer_sections, dict) else {}
    detail = _dict(sections.get("detail_table"))
    if detail or row_count:
        detail.pop("rows", None)
        detail["columns"] = _string_list(detail.get("columns")) or list(columns)
        detail["row_count"] = row_count
        detail["row_source"] = "data.rows"
        detail.setdefault("display_limit", 50 if row_count > 12 else 12)
        sections["detail_table"] = detail
    return sections


def _should_use_context_table(answer_type: str, context: dict[str, Any], parsed_table: dict[str, Any], fallback_table: dict[str, Any]) -> bool:
    context_mode = str(context.get("answer_mode") or "").strip()
    if answer_type in ALWAYS_USE_CONTEXT_TABLE_TYPES or context_mode in ALWAYS_USE_CONTEXT_TABLE_TYPES:
        return bool(_row_list(fallback_table.get("rows")))
    if answer_type not in AUTHORITATIVE_CONTEXT_TABLE_TYPES and context_mode not in AUTHORITATIVE_CONTEXT_TABLE_TYPES:
        return False
    fallback_rows = _row_list(fallback_table.get("rows"))
    if not fallback_rows:
        return False
    parsed_rows = _row_list(parsed_table.get("rows"))
    return len(parsed_rows) < len(fallback_rows)


def _source_refs_for_answer(answer_type: str, context: dict[str, Any], parsed: dict[str, Any], use_context_table: bool) -> list[Any]:
    context_refs = _list(context.get("source_refs"))
    parsed_refs = _list(parsed.get("source_refs"))
    context_mode = str(context.get("answer_mode") or "").strip()
    if answer_type in ALWAYS_USE_CONTEXT_TABLE_TYPES or context_mode in ALWAYS_USE_CONTEXT_TABLE_TYPES:
        return context_refs
    if use_context_table or answer_type in AUTHORITATIVE_CONTEXT_TABLE_TYPES or context_mode in AUTHORITATIVE_CONTEXT_TABLE_TYPES:
        return context_refs if len(context_refs) >= len(parsed_refs) else parsed_refs
    return parsed_refs or context_refs


def _sync_answer_sections_from_context(
    answer_sections: dict[str, Any],
    answer_type: str,
    answer_message: str,
    summary: str,
    table: dict[str, Any],
    source_refs: list[Any],
) -> dict[str, Any]:
    sections = deepcopy(answer_sections) if isinstance(answer_sections, dict) else {}
    rows = _row_list(table.get("rows"))
    if not rows:
        return sections
    columns = _string_list(table.get("columns")) or _columns_from_rows(rows)
    current = _dict(sections.get("detail_table"))
    sections["summary"] = {"headline": answer_message, "description": summary}
    sections["key_points"] = _key_points(answer_type, rows)
    title = _table_title(answer_type) if answer_type in {"available_sources"} else str(current.get("title") or _table_title(answer_type))
    sections["detail_table"] = {
        "title": title,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "display_limit": _display_limit(answer_type),
    }
    sections["usage_examples"] = _usage_examples(answer_type, {})
    sections["related_items"] = [] if answer_type in {"available_sources"} else [ref for ref in source_refs if isinstance(ref, dict)]
    sections["show_related_items"] = answer_type not in {"available_sources"}
    return sections


def _display_limit(answer_type: str) -> int:
    return 50 if answer_type in {"available_sources"} else 12


def _fallback_answer(question: str, context: dict[str, Any]) -> dict[str, Any]:
    answer_mode = str(context.get("answer_mode") or "general_metadata_search")
    rows = _row_list(context.get("candidate_rows"))
    source_refs = _list(context.get("source_refs"))
    datasets = _list(context.get("matched_datasets"))
    answer_type = answer_mode
    if not rows and not source_refs:
        if answer_mode == "data_analysis_redirect":
            message = "이 질문은 실제 데이터 값을 계산해야 하므로 metadata QA가 아니라 data_analysis flow에서 처리하는 것이 적절합니다."
            table = {"columns": ["항목", "내용"], "rows": [{"항목": "권장 route", "내용": "data_analysis"}]}
            return _fallback_payload(answer_type, message, table, [], source_refs, context)
        message = "질문과 직접 매칭되는 등록 메타데이터를 찾지 못했습니다. 데이터셋명, 도메인 용어, 또는 등록 key를 조금 더 구체적으로 알려주세요."
        return _fallback_payload(answer_type, message, {"columns": [], "rows": []}, [], source_refs, context)

    if answer_mode == "dataset_sql":
        sql_blocks = [_sql_block(item) for item in datasets if _sql_block(item)]
        target = _display_name(datasets[0]) if datasets else "요청한 데이터셋"
        message = f"{target}에 등록된 조회 설정과 query_template 기준으로 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, sql_blocks, source_refs, context)
    if answer_mode == "available_sources":
        message = _available_sources_message(rows)
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "dataset_detail":
        target = str(rows[0].get("display_name") or rows[0].get("key") or "요청한 데이터셋") if rows else "요청한 데이터셋"
        message = f"{target}의 등록 정보와 사용 기준을 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "required_params":
        message = f"질문과 관련된 데이터셋의 필수 조회 조건 {len(rows)}건을 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "calculation_logic_list":
        message = f"등록된 계산/분석 관련 메타데이터 후보 {len(rows)}개를 정리했습니다. 실제 계산 실행은 data_analysis_flow의 pandas 단계에서 수행됩니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode in {"product_domain_info", "product_condition"}:
        message = f"제품/POP 조건과 관련된 도메인 메타데이터 후보 {len(rows)}개를 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "product_token_rule":
        message = f"제품 속성 token 해석과 관련된 메타데이터 후보 {len(rows)}개를 정리했습니다. 실제 제품 매칭은 data_analysis_flow의 분석 단계에서 수행됩니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "process_group":
        message = f"공정 그룹과 세부 공정 해석에 관련된 메타데이터 후보 {len(rows)}개를 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "term_definition":
        message = f"질문과 관련된 용어 정의 메타데이터 후보 {len(rows)}개를 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)
    if answer_mode == "question_to_dataset":
        message = f"이 질문에 답할 때 참고할 데이터셋과 조건 후보 {len(rows)}개를 정리했습니다."
        return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)

    message = f"질문 '{question}'과 관련된 메타데이터 후보 {len(rows)}개를 정리했습니다."
    return _fallback_payload(answer_type, message, {"columns": _columns_from_rows(rows), "rows": rows}, [], source_refs, context)


def _fallback_payload(
    answer_type: str,
    message: str,
    table: dict[str, Any],
    sql_blocks: list[Any],
    source_refs: list[Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = context or {}
    sections = _build_answer_sections(answer_type, message, message, table, sql_blocks, source_refs, context, [])
    return {
        "answer_type": answer_type,
        "answer_message": message,
        "summary": message,
        "table": table,
        "sql_blocks": sql_blocks,
        "answer_sections": sections,
    }


def _build_answer_sections(
    answer_type: str,
    answer_message: str,
    summary: str,
    table: dict[str, Any],
    sql_blocks: list[Any],
    source_refs: list[Any],
    context: dict[str, Any],
    warnings: list[Any],
) -> dict[str, Any]:
    rows = _row_list(table.get("rows"))
    columns = _string_list(table.get("columns")) or _columns_from_rows(rows)
    return {
        "summary": {"headline": answer_message, "description": summary},
        "key_points": _key_points(answer_type, rows),
        "detail_table": {
            "title": _table_title(answer_type),
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "display_limit": _display_limit(answer_type),
        },
        "sql_blocks": [block for block in sql_blocks if isinstance(block, dict)],
        "usage_examples": _usage_examples(answer_type, context),
        "related_items": [] if answer_type in {"available_sources"} else [ref for ref in source_refs if isinstance(ref, dict)][:10],
        "show_related_items": answer_type not in {"available_sources"},
        "route_hint": _route_hint(answer_type),
        "warnings": [warning for warning in warnings if isinstance(warning, dict)],
    }


def _service_table(answer_type: str, table: dict[str, Any]) -> dict[str, Any]:
    rows = _row_list(table.get("rows"))
    if answer_type == "available_sources":
        return {
            "columns": ["데이터셋", "데이터셋 키", "분류", "연결 방식", "DB/소스", "필수 조건"],
            "rows": [_available_source_row(row) for row in rows],
        }
    return table


def _available_source_row(row: dict[str, Any]) -> dict[str, Any]:
    source_type = str(row.get("source_type") or "").strip()
    db_source = str(row.get("db_key") or "").strip()
    if not db_source and source_type:
        db_source = _source_type_label(source_type)
    required_params = str(row.get("required_params") or "").strip()
    result = {
            "데이터셋": row.get("display_name") or row.get("key"),
            "데이터셋 키": row.get("key"),
            "분류": _family_label(row.get("dataset_family")),
        "연결 방식": _source_type_label(source_type),
        "DB/소스": db_source,
        "필수 조건": required_params or "없음",
    }
    return {key: item for key, item in result.items() if item not in (None, "", [], {})}


def _source_type_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    labels = {"oracle": "Oracle", "goodocs": "Goodocs"}
    return labels.get(text.lower(), text)


def _family_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    labels = {
        "production": "생산",
        "wip": "재공",
        "plan": "계획",
        "equipment": "장비",
        "hold": "HOLD",
    }
    return labels.get(text.lower(), text)


def _available_sources_message(rows: list[dict[str, Any]]) -> str:
    rows = [_available_source_row(row) for row in rows] if rows and "연결 방식" not in rows[0] else rows
    total = len(rows)
    source_counts = _count_by(rows, "연결 방식")
    required_count = sum(1 for row in rows if str(row.get("필수 조건") or "").strip() not in {"", "없음"})
    no_required_count = total - required_count
    source_text = ", ".join(f"{key} {value}개" for key, value in source_counts.items() if key) or "연결 방식 미등록"
    return (
        f"현재 등록된 조회 데이터셋은 총 {total}개입니다. "
        f"연결 방식은 {source_text}로 구성되어 있고, "
        f"필수 조건이 있는 데이터셋은 {required_count}개, 별도 필수 조건이 없는 데이터셋은 {no_required_count}개입니다."
    )


def _key_points(answer_type: str, rows: list[dict[str, Any]]) -> list[str]:
    if answer_type != "available_sources" or not rows:
        return []
    source_counts = _count_by(rows, "연결 방식")
    required_rows = [row for row in rows if str(row.get("필수 조건") or "").strip() not in {"", "없음"}]
    points = [f"총 {len(rows)}개 데이터셋이 등록되어 있습니다."]
    if source_counts:
        points.append("연결 방식: " + ", ".join(f"{key} {value}개" for key, value in source_counts.items() if key))
    if required_rows:
        params = sorted({str(row.get("필수 조건") or "").strip() for row in required_rows if str(row.get("필수 조건") or "").strip()})
        points.append("필수 조건이 있는 데이터셋은 " + f"{len(required_rows)}개이며, 사용되는 조건은 {', '.join(params)}입니다.")
    points.append("필수 조건이 '없음'인 데이터셋은 별도 파라미터 없이 조회 설정이 가능하도록 등록된 항목입니다.")
    return points


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _table_title(answer_type: str) -> str:
    return {
        "available_sources": "조회 가능한 데이터",
        "dataset_detail": "데이터셋 등록 정보",
        "required_params": "필수 조회 조건",
        "dataset_sql": "데이터셋 등록 정보",
        "term_definition": "등록된 용어 정의",
        "process_group": "공정 그룹",
        "product_condition": "제품 조건",
        "product_domain_info": "제품 조건",
        "product_token_rule": "제품 token 해석 규칙",
        "calculation_logic_list": "계산/분석 로직",
        "question_to_dataset": "질문에 필요한 데이터와 조건",
        "data_analysis_redirect": "권장 실행 경로",
    }.get(answer_type, "관련 메타데이터")


def _usage_examples(answer_type: str, context: dict[str, Any]) -> list[str]:
    question = str(context.get("question") or "").strip()
    examples = {
        "available_sources": [
            "production_today 데이터셋의 쿼리문을 보여줘",
            "wip_today 데이터셋의 필수 조건과 용도를 알려줘",
            "생산량 분석에는 어떤 데이터셋을 써야 해?",
        ],
        "dataset_detail": ["이 데이터로 답할 수 있는 대표 질문을 알려줘"],
        "required_params": ["어제 기준으로 다시 조회해줘"],
        "term_definition": ["생산량 기준으로 제품별 상위 5개 알려줘"],
        "process_group": ["DA공정 차수별 생산량 알려줘"],
        "product_condition": ["HBM제품의 오늘 아침재공 제품별로 알려줘"],
        "product_token_rule": ["RG 32G DDR4 FBGA 96 DDP 제품 생산량 알려줘"],
        "calculation_logic_list": ["등록된 계산 로직 중 생산 달성률 기준 알려줘"],
        "question_to_dataset": [question] if question else [],
    }
    return examples.get(answer_type, [])


def _route_hint(answer_type: str) -> dict[str, str]:
    if answer_type == "data_analysis_redirect":
        return {"target_route": "data_analysis", "message": "실제 수량 계산은 data_analysis flow에서 실행합니다."}
    return {}


def _sql_block(item: Any) -> dict[str, str]:
    table = _dict(item)
    payload = _dict(table.get("payload"))
    source_config = _dict(payload.get("source_config"))
    sql = str(source_config.get("query_template") or payload.get("query_template") or "").strip()
    if not sql:
        return {}
    return {"label": _display_name(table), "sql": sql}


def _display_name(item: Any) -> str:
    table = _dict(item)
    payload = _dict(table.get("payload"))
    return str(payload.get("display_name") or table.get("display_name") or table.get("dataset_key") or table.get("key") or "").strip()


def _parse_llm_response(value: Any) -> dict[str, Any]:
    text = _text(value)
    if not text:
        return {}
    candidates = [text.strip()]
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        candidates.insert(0, match.group(1).strip())
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return deepcopy(parsed)
    return {"answer_message": text}


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text.strip()
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False, default=str)
    return str(value or "").strip()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _row_list(value: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    return columns


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataQaResponseNormalizer(Component):
    display_name = "04 메타데이터 QA 응답 정규화기"
    description = "Langflow Agent/LLM 응답을 메타데이터 QA 표준 페이로드로 정규화합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="LLM 응답", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_metadata_qa_response(getattr(self, "payload", None), getattr(self, "llm_response", "")))
