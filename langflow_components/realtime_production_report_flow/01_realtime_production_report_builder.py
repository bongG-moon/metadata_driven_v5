# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 실시간 생산 분석 Report 생성기
# 역할: 판정 Snapshot을 결정론적으로 집계하고 채팅 요약, interactive HTML, Report API 링크를 생성합니다.
# 주요 입력: production.judgement.dataset.v1 Data, 사용자 질문, Report API 주소·TTL, HTML 표시 행 수
# 주요 출력: Message, realtime.production.report.v1 Data
# 처리 흐름: Schema 검증 -> Case/제품 grain 집계 -> 4개 분석 영역 분류 -> HTML 저장/게시 -> compact 응답
# 유지보수 포인트: 원본 rows와 HTML은 API/Workflow payload에 넣지 않고 artifact와 KPI만 전달합니다.
# =============================================================================

from __future__ import annotations

import asyncio
import html
import json
import re
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from math import pi
from typing import Any
from urllib.parse import urlsplit

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, MessageTextInput, Output, StrInput
from lfx.schema.data import Data
from lfx.schema.message import Message


CONTRACT_VERSION = "realtime.production.report.v1"
DATASET_CONTRACT_VERSION = "production.judgement.dataset.v1"
SELECTION_CONTRACT_VERSION = "production.process_group.selection.v1"
RULES_VERSION = "realtime.production.report.rules.v1"
REPORT_CONTEXT_VERSION = "report.context.v1"
QUERY_SOURCE_CONTRACT_VERSION = "report.query_source.v1"
REPORT_TYPE = "realtime_production"
REPORT_CONTEXT_ALLOWED_OPERATIONS = ["filter", "sort_and_top_n", "select_columns"]
DEFAULT_REPORT_API_URL = "http://127.0.0.1:5000"
DEFAULT_REPORT_TTL_HOURS = 4
MAX_REPORT_TTL_HOURS = 24 * 7
DEFAULT_MAX_HTML_ROWS = 1_000
MAX_HTML_ROWS = 5_000
MAX_HTML_BYTES = 8_000_000
MAX_REPORT_RESPONSE_BYTES = 64 * 1024
MAX_PUBLIC_URL_CHARS = 2_048
MAX_ERROR_CHARS = 600
MAX_QUESTION_CHARS = 4_000
MAX_STATE_MESSAGE_CHARS = 12_000
MAX_REPORT_ID_CHARS = 160
REPORT_API_TIMEOUT_SECONDS = 30

ALL_COLUMNS = [
    "WORK_DATE", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO",
    "OPER", "OPER_NAME", "OPER_SEQ", "NETDIE_300_CNT", "PRODUCTION", "WIP", "INPUT_PLAN",
    "OUT_PLAN", "생산실적달성율", "달성율*판정", "적정재공수량", "적정재공율", "적정재공*판정",
    "EQP_COUNT", "DOWN_CNT", "OVER_2H_DOWN", "기준UPH", "보유UPH", "보유CAPA(24H)",
    "보유CAPA(잔여)", "잔여목표수량", "CAPA확보율", "장비BAL", "CAPA판정", "CAPA이상판단",
    "이전공정재공", "현재작업재공", "장비교체판단재공", "재공보유율", "장비교체판단",
    "장비필요대수", "평균가동율", "평균NOWIP", "가동율목표", "가동율달성률", "가동율판정",
]
CORE_COLUMNS = [
    "WORK_DATE", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO",
    "OPER", "OPER_NAME", "달성율*판정", "적정재공*판정", "CAPA판정", "CAPA이상판단",
    "장비교체판단", "가동율판정",
]
PRODUCT_KEY_COLUMNS = ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"]
CASE_KEY_COLUMNS = ["WORK_DATE", "OPER", *PRODUCT_KEY_COLUMNS]
CAPA_ANOMALY_VALUES = {"생산부진1", "생산부진2", "CAPA부족"}
PRODUCTION_VALUES = {"정상", "정상(초과생산)", "Abnormal", "생산부족"}
WIP_VALUES = {"정상", "재공과다", "Abnormal"}
CAPA_JUDGEMENT_VALUES = {"CAPA과다", "CAPA부족", "잉여장비"}
CAPA_DETAIL_VALUES = {"정상", "Abnormal", *CAPA_ANOMALY_VALUES}
EQUIPMENT_VALUES = {"정상", "장비필요", "교체필요", "교체불필요"}
UTILIZATION_VALUES = {"정상", "Abnormal"}
REPORT_CONTEXT_SEMANTIC_FILTERS = [
    {
        "key": "production_normal_or_excess",
        "aliases": ["정상 생산", "정상/초과 생산", "정상 또는 초과 생산"],
        "source_alias": "report_snapshot",
        "column": "달성율*판정",
        "operator": "in",
        "value": ["정상", "정상(초과생산)"],
    },
    {
        "key": "production_abnormal",
        "aliases": ["생산 이상", "생산 Abnormal", "생산 비정상"],
        "source_alias": "report_snapshot",
        "column": "달성율*판정",
        "operator": "eq",
        "value": "Abnormal",
    },
    {
        "key": "production_shortage",
        "aliases": ["생산부족", "생산 부족", "생산 부족 제품", "생산부족 제품"],
        "source_alias": "report_snapshot",
        "column": "달성율*판정",
        "operator": "eq",
        "value": "생산부족",
    },
    {
        "key": "shortage_wip_cause",
        "aliases": ["적정재공부족", "적정 재공 부족", "재공 부족"],
        "source_alias": "report_snapshot",
        "column": "적정재공*판정",
        "operator": "eq",
        "value": "Abnormal",
    },
    {
        "key": "wip_excess",
        "aliases": ["재공과다", "재공 과다"],
        "source_alias": "report_snapshot",
        "column": "적정재공*판정",
        "operator": "eq",
        "value": "재공과다",
    },
    {
        "key": "capa_shortage",
        "aliases": ["CAPA부족", "CAPA 부족", "캐파 부족"],
        "source_alias": "report_snapshot",
        "column": "CAPA판정",
        "operator": "eq",
        "value": "CAPA부족",
    },
    {
        "key": "low_utilization",
        "aliases": ["가동율저조", "가동율 저조", "가동률 저조"],
        "source_alias": "report_snapshot",
        "column": "가동율판정",
        "operator": "eq",
        "value": "Abnormal",
    },
    {
        "key": "equipment_needed",
        "aliases": ["장비필요", "장비 필요", "장비필요 제품", "장비 필요 제품"],
        "source_alias": "report_snapshot",
        "column": "장비교체판단",
        "operator": "eq",
        "value": "장비필요",
    },
    {
        "key": "equipment_change_needed",
        "aliases": ["교체필요", "교체 필요", "장비 교체 필요"],
        "source_alias": "report_snapshot",
        "column": "장비교체판단",
        "operator": "eq",
        "value": "교체필요",
    },
]
REPORT_CONTEXT_VALUE_DOMAINS = [
    {"source_alias": "report_snapshot", "column": "달성율*판정", "values": sorted(PRODUCTION_VALUES)},
    {"source_alias": "report_snapshot", "column": "적정재공*판정", "values": sorted(WIP_VALUES)},
    {"source_alias": "report_snapshot", "column": "CAPA판정", "values": sorted(CAPA_JUDGEMENT_VALUES)},
    {"source_alias": "report_snapshot", "column": "CAPA이상판단", "values": sorted(CAPA_DETAIL_VALUES)},
    {"source_alias": "report_snapshot", "column": "장비교체판단", "values": sorted(EQUIPMENT_VALUES)},
    {"source_alias": "report_snapshot", "column": "가동율판정", "values": sorted(UTILIZATION_VALUES)},
]

IDENTITY_COLUMNS = [
    "WORK_DATE", "OPER_NAME", "OPER", "MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2",
    "LEAD", "MCP_NO",
]
SECTION_COLUMNS = {
    "production": [
        *IDENTITY_COLUMNS, "PRODUCTION", "INPUT_PLAN", "OUT_PLAN", "생산실적달성율", "달성율*판정",
        "WIP", "적정재공율", "CAPA확보율", "평균가동율", "가동율판정",
    ],
    "shortage": [
        *IDENTITY_COLUMNS, "PRODUCTION", "OUT_PLAN", "생산실적달성율", "적정재공수량", "적정재공율",
        "적정재공*판정", "CAPA확보율", "CAPA판정", "평균가동율", "가동율목표", "가동율달성률",
        "가동율판정", "이전공정재공", "현재작업재공",
    ],
    "capa": [
        *IDENTITY_COLUMNS, "잔여목표수량", "기준UPH", "보유UPH", "보유CAPA(24H)", "보유CAPA(잔여)",
        "CAPA확보율", "EQP_COUNT", "DOWN_CNT", "OVER_2H_DOWN", "장비BAL", "CAPA판정", "CAPA이상판단",
        "생산실적달성율",
    ],
    "equipment": [
        *IDENTITY_COLUMNS, "CAPA이상판단", "EQP_COUNT", "DOWN_CNT", "OVER_2H_DOWN", "장비BAL",
        "CAPA판정", "CAPA확보율", "이전공정재공", "현재작업재공", "장비교체판단재공",
        "재공보유율", "장비교체판단", "장비필요대수", "평균가동율", "가동율판정",
    ],
}

COLORS = {
    "green": "#16a34a",
    "blue": "#2563eb",
    "amber": "#d97706",
    "red": "#dc2626",
    "purple": "#7c3aed",
    "slate": "#64748b",
}


# 함수 설명: `_text()`는 Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


# 함수 설명: `_clip()`는 CLIP이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _clip(value: Any, limit: int) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: `_issue()`는 조회 작업 hydration 중 발견한 문제를 type·dataset·message 구조로 만듭니다.
def _issue(issue_type: str, message: Any) -> dict[str, str]:
    return {"type": _clip(issue_type, 100), "message": _clip(message, MAX_ERROR_CHARS)}


# 함수 설명: `_bounded_int()`는 INT이 허용된 개수·길이·바이트 제한을 넘지 않도록 안전하게 줄입니다.
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(float(_text(value))) if _text(value) else default
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(lower, min(parsed, upper))


# 함수 설명: `_message_parts()`는 parts에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _message_parts(value: Any) -> tuple[str, str]:
    data = getattr(value, "data", None)
    data = data if isinstance(data, dict) else {}
    question = _text(getattr(value, "text", None) or data.get("text") or value)
    session_id = _text(getattr(value, "session_id", None) or data.get("session_id"))
    return _clip(question, MAX_QUESTION_CHARS), session_id


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return raw if isinstance(raw, dict) else {}


# 함수 설명: `_ref_id()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _ref_id(value: Any) -> str:
    if isinstance(value, dict):
        return _clip(value.get("ref_id") or value.get("data_ref") or value.get("_id"), 500)
    return _clip(value, 500)


# 함수 설명: `_query_source_contract()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _query_source_contract(
    value: Any,
    *,
    source_alias: str,
    dataset_key: str,
    available_columns: list[str],
) -> dict[str, Any]:
    """Project a stored Report-owned query contract without global mappings."""

    raw = value if isinstance(value, dict) else {}
    if (
        _text(raw.get("contract_version")) != QUERY_SOURCE_CONTRACT_VERSION
        or raw.get("authoritative") is not True
        or _text(raw.get("source_alias")) != source_alias
        or _text(raw.get("dataset_key")) != dataset_key
    ):
        return {}
    columns = [
        _clip(column, 200)
        for column in raw.get("columns", [])
        if _text(column) and _text(column) in available_columns
    ] if isinstance(raw.get("columns"), list) else []
    grain = raw.get("grain") if isinstance(raw.get("grain"), dict) else {}
    grain_columns = [
        _clip(column, 200)
        for column in grain.get("columns", [])
        if _text(column) and _text(column) in columns
    ] if isinstance(grain.get("columns"), list) else []
    purpose = _clip(raw.get("purpose"), 120)
    if not purpose or not columns or not grain_columns:
        return {}
    projected: dict[str, Any] = {
        "contract_version": QUERY_SOURCE_CONTRACT_VERSION,
        "source_alias": source_alias,
        "dataset_key": dataset_key,
        "purpose": purpose,
        "aliases": [
            _clip(item, 120)
            for item in raw.get("aliases", [])[:20]
            if _text(item)
        ] if isinstance(raw.get("aliases"), list) else [],
        "authoritative": True,
        "columns": columns,
        "grain": {
            "kind": _clip(grain.get("kind"), 80),
            "columns": grain_columns,
            "unique": grain.get("unique") is True,
        },
        "predicates": deepcopy(raw.get("predicates", [])[:20])
        if isinstance(raw.get("predicates"), list)
        else [],
        "metrics": deepcopy(raw.get("metrics", [])[:20])
        if isinstance(raw.get("metrics"), list)
        else [],
        "allowed_operations": [
            _clip(item, 80)
            for item in raw.get("allowed_operations", [])[:12]
            if _text(item)
        ] if isinstance(raw.get("allowed_operations"), list) else [],
    }
    materialized_from = _clip(raw.get("materialized_from"), 120)
    if materialized_from:
        projected["materialized_from"] = materialized_from
    return projected


# 함수 설명: `_context_projection()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _context_projection(value: Any, expected_session_id: str) -> dict[str, Any]:
    """Project a result-store payload to refs only; never copy runtime rows."""

    payload = _payload(value)
    unavailable = {
        "available": False,
        "context_ref": "",
        "result_ref": {},
        "source_data_refs": [],
        "available_datasets": [],
        "reason": "context_payload_missing",
    }
    if not payload:
        return unavailable
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    stored_session_id = _text(request.get("session_id"))
    if not expected_session_id or not stored_session_id or stored_session_id != expected_session_id:
        return {**unavailable, "reason": "context_session_mismatch"}
    inspection = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = inspection.get("inspection") if isinstance(inspection.get("inspection"), dict) else {}
    result_store = inspection.get("result_store") if isinstance(inspection.get("result_store"), dict) else {}
    if _text(result_store.get("status")).lower() != "ok":
        return {
            **unavailable,
            "reason": _clip(result_store.get("status") or "context_store_unavailable", 100),
        }
    refs = payload.get("data_refs") if isinstance(payload.get("data_refs"), list) else []
    safe_refs = [dict(item) for item in refs if isinstance(item, dict) and _ref_id(item)]
    result_ref = next(
        (item for item in safe_refs if _text(item.get("role")) == "analysis_result"),
        {},
    )
    context_ref = _ref_id(result_ref)
    source_refs = [
        item
        for item in safe_refs
        if _text(item.get("role")) == "source_rows" and _ref_id(item) == context_ref
    ]
    if not context_ref or not source_refs:
        return {**unavailable, "reason": "context_data_refs_missing"}
    source_summaries = {
        _text(item.get("source_alias") or item.get("dataset_key")): item
        for item in payload.get("source_results", [])
        if isinstance(item, dict) and _text(item.get("source_alias") or item.get("dataset_key"))
    } if isinstance(payload.get("source_results"), list) else {}
    available_datasets = []
    for ref in source_refs:
        alias = _clip(ref.get("source_alias"), 120)
        dataset_key = _clip(ref.get("dataset_key"), 160)
        summary = source_summaries.get(alias) if isinstance(source_summaries.get(alias), dict) else {}
        raw_columns = summary.get("columns") if isinstance(summary.get("columns"), list) else ref.get("columns", [])
        columns = [
            _clip(column, 200)
            for column in raw_columns
            if _text(column)
        ] if isinstance(raw_columns, list) else []
        query_contract = _query_source_contract(
            summary.get("query_source_contract"),
            source_alias=alias,
            dataset_key=dataset_key,
            available_columns=columns,
        )
        dataset_projection = {
            "source_alias": alias,
            "dataset_key": dataset_key,
            "source_type": _clip(ref.get("source_type") or summary.get("source_type") or "report_snapshot", 80),
            "row_count": _bounded_int(ref.get("row_count"), 0, 0, 1_000_000_000),
            "columns": columns,
            "data_ref": dict(ref),
        }
        if query_contract:
            dataset_projection["query_source_contract"] = query_contract
        available_datasets.append(dataset_projection)
    query_sources = [
        deepcopy(item["query_source_contract"])
        for item in available_datasets
        if isinstance(item.get("query_source_contract"), dict)
    ]
    return {
        "available": True,
        "context_ref": context_ref,
        "result_ref": dict(result_ref),
        "source_data_refs": source_refs,
        "available_datasets": available_datasets,
        "query_sources": query_sources,
        "reason": "",
    }


# 함수 설명: `_report_followup_state()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _report_followup_state(
    *,
    session_id: str,
    question: str,
    message: str,
    analysis: dict[str, Any],
    kpis: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if not context.get("available"):
        return _report_context_tombstone_state(
            session_id=session_id,
            question=question,
            message=message,
            reason=_text(context.get("reason") or "report_context_unavailable"),
        )
    datasets = context.get("available_datasets") if isinstance(context.get("available_datasets"), list) else []
    source_aliases = [
        _text(item.get("source_alias"))
        for item in datasets
        if isinstance(item, dict) and _text(item.get("source_alias"))
    ]
    source_dataset_keys = [
        _text(item.get("dataset_key"))
        for item in datasets
        if isinstance(item, dict) and _text(item.get("dataset_key"))
    ]
    source_columns_by_alias = {
        _text(item.get("source_alias")): list(item.get("columns") or [])
        for item in datasets
        if isinstance(item, dict) and _text(item.get("source_alias"))
    }
    report_context = {
        "context_version": REPORT_CONTEXT_VERSION,
        "context_ref": _text(context.get("context_ref")),
        "report_type": REPORT_TYPE,
        "snapshot_id": _clip(analysis.get("scope", {}).get("snapshot_id"), 200),
        "as_of": _clip(analysis.get("scope", {}).get("snapshot_at"), 100),
        "expires_at": _clip(
            (context.get("result_ref") or {}).get("expires_at")
            if isinstance(context.get("result_ref"), dict)
            else "",
            100,
        ),
        "report_scope": dict(analysis.get("scope") or {}),
        "kpi_facts": dict(kpis),
        "rules": {"rules_version": RULES_VERSION},
        "allowed_operations": list(REPORT_CONTEXT_ALLOWED_OPERATIONS),
        "semantic_filters": deepcopy(REPORT_CONTEXT_SEMANTIC_FILTERS),
        "value_domains": deepcopy(REPORT_CONTEXT_VALUE_DOMAINS),
        "query_sources": deepcopy(context.get("query_sources") or []),
    }
    first_dataset = datasets[0] if datasets and isinstance(datasets[0], dict) else {}
    current_data = {
        "row_count": _bounded_int(
            first_dataset.get("row_count") or analysis.get("scope", {}).get("case_count"),
            0,
            0,
            1_000_000_000,
        ),
        "columns": list(first_dataset.get("columns") or []),
        "result_columns": list(first_dataset.get("columns") or []),
        "source_aliases": source_aliases,
        "source_dataset_keys": source_dataset_keys,
        "source_columns_by_alias": source_columns_by_alias,
        "query_sources": deepcopy(context.get("query_sources") or []),
        "data_ref": dict(context.get("result_ref") or {}),
        "report_context": report_context,
    }
    followup_sources = [
        {
            "source_alias": item.get("source_alias"),
            "dataset_key": item.get("dataset_key"),
            "source_type": item.get("source_type"),
            "columns": list(item.get("columns") or []),
            "row_count": _bounded_int(item.get("row_count"), 0, 0, 1_000_000_000),
            "data_ref": dict(item.get("data_ref") or {}),
            "data_is_reference": True,
            **(
                {"query_source_contract": deepcopy(item.get("query_source_contract"))}
                if isinstance(item.get("query_source_contract"), dict)
                else {}
            ),
        }
        for item in datasets
        if isinstance(item, dict)
    ]
    runtime_source_refs = {
        _text(item.get("source_alias")): dict(item.get("data_ref") or {})
        for item in datasets
        if isinstance(item, dict) and _text(item.get("source_alias"))
    }
    return {
        "session_id": session_id,
        "last_question": question,
        "last_answer_message": message,
        "current_data": current_data,
        "followup_source_results": followup_sources,
        "runtime_source_refs": runtime_source_refs,
    }


# 함수 설명: `_report_context_tombstone_state()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _report_context_tombstone_state(
    *,
    session_id: str,
    question: str,
    message: str,
    reason: str,
) -> dict[str, Any]:
    """Replace an older Report context when the newest Report is not reusable."""

    if not _text(session_id):
        return {}
    return {
        "session_id": _text(session_id),
        "last_question": _clip(question, MAX_QUESTION_CHARS),
        "last_answer_message": _clip(message, MAX_STATE_MESSAGE_CHARS),
        "current_data": {
            "row_count": 0,
            "columns": [],
            "result_columns": [],
            "source_aliases": [],
            "source_dataset_keys": [],
            "source_columns_by_alias": {},
            "report_context_status": {
                "context_version": REPORT_CONTEXT_VERSION,
                "report_type": REPORT_TYPE,
                "status": "invalidated",
                "reason": _clip(reason or "report_context_unavailable", 200),
            },
        },
        "followup_source_results": [],
        "runtime_source_refs": {},
    }


# 함수 설명: `_with_report_context_tombstone()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _with_report_context_tombstone(
    result: dict[str, Any],
    *,
    session_id: str,
    question: str,
    reason: str,
) -> dict[str, Any]:
    next_result = deepcopy(result)
    next_result["state"] = _report_context_tombstone_state(
        session_id=session_id,
        question=question,
        message=_text(next_result.get("message")),
        reason=reason,
    )
    return next_result


# 함수 설명: `_percent()`는 01 실시간 생산 분석 Report 생성기 처리 중 percent 관련 값을 계산·변환하는 내부 helper입니다.
def _percent(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


# 함수 설명: `_key()`는 메타데이터 항목에서 비교·표시에 사용할 논리 key를 안전하게 꺼냅니다.
def _key(row: dict[str, Any], columns: list[str]) -> tuple[str, ...]:
    return tuple(_text(row.get(column)) for column in columns)


# 함수 설명: `_distinct_count()`는 01 실시간 생산 분석 Report 생성기 처리 중 count 관련 값을 계산·변환하는 내부 helper입니다.
def _distinct_count(rows: list[dict[str, Any]], columns: list[str]) -> int:
    return len({_key(row, columns) for row in rows})


# 함수 설명: `_production_bucket()`는 01 실시간 생산 분석 Report 생성기 처리 중 bucket 관련 값을 계산·변환하는 내부 helper입니다.
def _production_bucket(row: dict[str, Any]) -> str:
    value = _text(row.get("달성율*판정"))
    if value in {"정상", "정상(초과생산)"}:
        return "normal_excess"
    if value == "Abnormal":
        return "abnormal"
    if value == "생산부족":
        return "shortage"
    return "unclassified"


# 함수 설명: `_shortage_flags()`는 01 실시간 생산 분석 Report 생성기 처리 중 flags 관련 값을 계산·변환하는 내부 helper입니다.
def _shortage_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if _text(row.get("적정재공*판정")) == "Abnormal":
        flags.append("wip")
    if _text(row.get("CAPA판정")) == "CAPA부족":
        flags.append("capa")
    if _text(row.get("가동율판정")) == "Abnormal":
        flags.append("utilization")
    return flags


# 함수 설명: `_shortage_primary()`는 01 실시간 생산 분석 Report 생성기 처리 중 primary 관련 값을 계산·변환하는 내부 helper입니다.
def _shortage_primary(flags: list[str]) -> str:
    for candidate in ("wip", "capa", "utilization"):
        if candidate in flags:
            return candidate
    return "unclassified"


# 함수 설명: `_capa_bucket()`는 01 실시간 생산 분석 Report 생성기 처리 중 bucket 관련 값을 계산·변환하는 내부 helper입니다.
def _capa_bucket(row: dict[str, Any]) -> str:
    value = _text(row.get("CAPA이상판단"))
    if value == "정상":
        return "normal"
    if value == "Abnormal":
        return "abnormal"
    if value in CAPA_ANOMALY_VALUES:
        return "anomaly"
    return "unclassified"


# 함수 설명: `_prepare_rows()`는 행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _prepare_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for original in rows:
        row = {column: original.get(column) for column in ALL_COLUMNS}
        production = _production_bucket(row)
        shortage_flags = _shortage_flags(row) if production == "shortage" else []
        row["_production_bucket"] = production
        row["_shortage_flags"] = shortage_flags
        row["_shortage_primary"] = _shortage_primary(shortage_flags)
        row["_capa_bucket"] = _capa_bucket(row)
        row["_capa_detail"] = _text(row.get("CAPA이상판단"))
        row["_equipment_scope"] = row["_capa_detail"] in CAPA_ANOMALY_VALUES
        row["_equipment_bucket"] = _text(row.get("장비교체판단")) or "미분류"
        prepared.append(row)
    return prepared


# 함수 설명: `_validate_dataset()`는 데이터셋이 실행·저장 계약을 만족하는지 검사하고 위반 내용을 명시적으로 반환합니다.
def _validate_dataset(dataset: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, str] | None]:
    rows_value = dataset.get("rows")
    if not isinstance(rows_value, list) or not rows_value:
        return [], [], _issue("missing_dataset_rows", "판정 데이터 rows가 비어 있습니다.")
    rows = [dict(row) for row in rows_value if isinstance(row, dict)]
    if len(rows) != len(rows_value):
        return [], [], _issue("invalid_dataset_row", "모든 판정 데이터 행은 object 형식이어야 합니다.")
    present = {str(key) for row in rows for key in row}
    missing = [column for column in CORE_COLUMNS if column not in present]
    if missing:
        return [], [], _issue("missing_required_columns", f"필수 컬럼이 없습니다: {', '.join(missing)}")

    warnings: list[dict[str, str]] = []
    expected_contract = _text(dataset.get("contract_version"))
    if expected_contract and expected_contract != DATASET_CONTRACT_VERSION:
        warnings.append(_issue("dataset_contract_mismatch", f"입력 계약이 {DATASET_CONTRACT_VERSION}이 아닙니다: {expected_contract}"))
    missing_optional = [column for column in ALL_COLUMNS if column not in present]
    if missing_optional:
        warnings.append(_issue("missing_optional_columns", f"표시 컬럼 {len(missing_optional)}개가 없어 빈 값으로 표시합니다."))
    value_checks = (
        ("달성율*판정", PRODUCTION_VALUES),
        ("적정재공*판정", WIP_VALUES),
        ("CAPA판정", CAPA_JUDGEMENT_VALUES),
        ("CAPA이상판단", CAPA_DETAIL_VALUES),
        ("장비교체판단", EQUIPMENT_VALUES),
        ("가동율판정", UTILIZATION_VALUES),
    )
    for column, allowed in value_checks:
        unknown = sorted({_text(row.get(column)) for row in rows if _text(row.get(column)) not in allowed})
        if unknown:
            warnings.append(_issue("unknown_judgement_value", f"{column} 미등록 값: {', '.join(unknown[:8])}"))
    work_dates = sorted({_text(row.get("WORK_DATE")) for row in rows if _text(row.get("WORK_DATE"))})
    if len(work_dates) > 1:
        warnings.append(_issue("mixed_work_dates", f"한 Snapshot에 WORK_DATE {len(work_dates)}개가 포함되어 있습니다."))
    case_count = len({_key(row, CASE_KEY_COLUMNS) for row in rows})
    if case_count != len(rows):
        warnings.append(_issue("duplicate_case_key", f"Case key 중복 {len(rows) - case_count:,}건을 발견했습니다. 집계 행은 유지했습니다."))
    return _prepare_rows(rows), warnings, None


# 함수 설명: `analyze_production_rows()`는 production·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def analyze_production_rows(rows: list[dict[str, Any]], dataset: dict[str, Any]) -> dict[str, Any]:
    production = {
        "normal_excess": sum(row["_production_bucket"] == "normal_excess" for row in rows),
        "abnormal": sum(row["_production_bucket"] == "abnormal" for row in rows),
        "shortage": sum(row["_production_bucket"] == "shortage" for row in rows),
        "unclassified": sum(row["_production_bucket"] == "unclassified" for row in rows),
    }
    shortage_rows = [row for row in rows if row["_production_bucket"] == "shortage"]
    shortage_primary = {
        key: sum(row["_shortage_primary"] == key for row in shortage_rows)
        for key in ("wip", "capa", "utilization", "unclassified")
    }
    shortage_flags = {
        key: sum(key in row["_shortage_flags"] for row in shortage_rows)
        for key in ("wip", "capa", "utilization")
    }
    capa = {
        "normal": sum(row["_capa_bucket"] == "normal" for row in rows),
        "abnormal": sum(row["_capa_bucket"] == "abnormal" for row in rows),
        "anomaly": sum(row["_capa_bucket"] == "anomaly" for row in rows),
        "unclassified": sum(row["_capa_bucket"] == "unclassified" for row in rows),
    }
    capa_detail = {
        value: sum(row["_capa_detail"] == value for row in rows)
        for value in ("생산부진1", "생산부진2", "CAPA부족")
    }
    capa_abnormal_rows = [row for row in rows if row["_capa_detail"] == "Abnormal"]
    equipment_rows = [row for row in rows if row["_equipment_scope"]]
    equipment = {
        value: sum(row["_equipment_bucket"] == value for row in equipment_rows)
        for value in ("정상", "교체불필요", "장비필요", "교체필요")
    }
    equipment["normal_no_change"] = equipment["정상"] + equipment["교체불필요"]
    equipment["unclassified"] = len(equipment_rows) - sum(equipment[value] for value in ("정상", "교체불필요", "장비필요", "교체필요"))
    processes = sorted({_text(row.get("OPER_NAME")) for row in rows if _text(row.get("OPER_NAME"))})
    work_dates = sorted({_text(row.get("WORK_DATE")) for row in rows if _text(row.get("WORK_DATE"))})
    selected_process_group = (
        dict(dataset.get("selected_process_group"))
        if isinstance(dataset.get("selected_process_group"), dict)
        else {}
    )
    return {
        "rules_version": RULES_VERSION,
        "scope": {
            "snapshot_id": _clip(dataset.get("snapshot_id"), 200),
            "snapshot_at": _clip(dataset.get("snapshot_at"), 80),
            "work_dates": work_dates,
            "processes": processes,
            "process_count": len(processes),
            "case_count": len(rows),
            "distinct_case_count": _distinct_count(rows, CASE_KEY_COLUMNS),
            "product_count": _distinct_count(rows, PRODUCT_KEY_COLUMNS),
            "source_type": _clip(dataset.get("source_type") or "unknown", 40),
            "process_group": {
                "key": _clip(selected_process_group.get("key"), 80),
                "display_name": _clip(selected_process_group.get("display_name"), 200),
                "field": _clip(selected_process_group.get("field"), 120),
                "configured_processes": [
                    _clip(item, 120)
                    for item in selected_process_group.get("processes", [])
                    if _text(item)
                ][:100],
                "question_evidence": [
                    _clip(item, 200)
                    for item in selected_process_group.get("question_evidence", [])
                    if _text(item)
                ][:20],
            },
        },
        "production": production,
        "shortage": {
            "case_count": len(shortage_rows),
            "primary": shortage_primary,
            "flags": shortage_flags,
            "multi_cause_count": sum(len(row["_shortage_flags"]) > 1 for row in shortage_rows),
        },
        "capa": {
            **capa,
            "detail": capa_detail,
            "abnormal_product_count": _distinct_count(capa_abnormal_rows, PRODUCT_KEY_COLUMNS),
        },
        "equipment": {
            **equipment,
            "case_count": len(equipment_rows),
        },
    }


# 함수 설명: `_metric()`는 01 실시간 생산 분석 Report 생성기 처리 중 metric 관련 값을 계산·변환하는 내부 helper입니다.
def _metric(label: str, value: str, tone: str, note: str = "") -> str:
    note_html = f"<span>{html.escape(note)}</span>" if note else ""
    return (
        f'<article class="metric {tone}"><small>{html.escape(label)}</small>'
        f'<strong>{html.escape(value)}</strong>{note_html}</article>'
    )


# 함수 설명: `_donut_chart()`는 01 실시간 생산 분석 Report 생성기 처리 중 chart 관련 값을 계산·변환하는 내부 helper입니다.
def _donut_chart(title: str, entries: list[tuple[str, int, str]]) -> str:
    total = sum(max(0, int(count)) for _, count, _ in entries)
    radius = 52
    circumference = 2 * pi * radius
    offset = 0.0
    circles: list[str] = []
    legend: list[str] = []
    for label, count, color in entries:
        length = circumference * count / total if total else 0
        circles.append(
            f'<circle cx="70" cy="70" r="{radius}" fill="none" stroke="{color}" stroke-width="18" '
            f'stroke-dasharray="{length:.3f} {max(0.0, circumference - length):.3f}" '
            f'stroke-dashoffset="{-offset:.3f}" transform="rotate(-90 70 70)"/>'
        )
        offset += length
        legend.append(
            f'<li><i style="background:{color}"></i><span>{html.escape(label)}</span>'
            f'<b>{count:,}건</b><em>{_percent(count, total):.1f}%</em></li>'
        )
    return (
        '<article class="chart-card">'
        f'<h3>{html.escape(title)}</h3><div class="donut-wrap"><svg viewBox="0 0 140 140" role="img" '
        f'aria-label="{html.escape(title)}"><circle cx="70" cy="70" r="{radius}" fill="none" '
        f'stroke="#e2e8f0" stroke-width="18"/>{"".join(circles)}'
        f'<text x="70" y="66" text-anchor="middle" class="donut-total">{total:,}</text>'
        '<text x="70" y="84" text-anchor="middle" class="donut-label">Case</text></svg>'
        f'<ul class="legend">{"".join(legend)}</ul></div></article>'
    )


# 함수 설명: `_filter_controls()`는 조건과 우선순위에 맞는 controls만 골라 원래 순서를 유지해 반환합니다.
def _filter_controls(section: str, items: list[tuple[str, str, int]]) -> str:
    controls: list[str] = []
    for index, (value, label, count) in enumerate(items):
        checked = " checked" if index == 0 else ""
        controls.append(
            f'<label class="filter-chip"><input type="radio" name="filter-{section}" '
            f'value="{html.escape(value)}"{checked}><span>{html.escape(label)} <b>{count:,}</b></span></label>'
        )
    return "".join(controls)


# 함수 설명: `_table_shell()`는 shell을 현재 컴포넌트의 표준 반환 형태로 변환합니다.
def _table_shell(section: str, controls: str) -> str:
    return f"""
<div class="table-tools">
  <div class="filters">{controls}</div>
  <div class="table-actions">
    <label class="search-wrap"><span>검색</span><input type="search" data-search="{section}" placeholder="제품·공정 검색"></label>
    <label class="column-toggle"><input type="checkbox" data-full-columns="{section}"> 전체 컬럼</label>
    <button type="button" class="download-button" data-download="{section}">엑셀용 CSV 다운로드</button>
  </div>
</div>
<div class="table-status" data-status="{section}"></div>
<div class="table-wrap"><table><thead data-head="{section}"></thead><tbody data-body="{section}"></tbody></table></div>
"""


# 함수 설명: `_section()`는 응답 section을 최종 Message에 넣을 독립 Markdown section으로 렌더링합니다.
def _section(
    section_id: str,
    eyebrow: str,
    title: str,
    summary: str,
    actions: list[str],
    charts: str,
    table: str,
    detail_note: str,
) -> str:
    action_items = "".join(f"<li>{html.escape(item)}</li>" for item in actions)
    return f"""
<section class="report-section" data-panel="{section_id}">
  <header class="section-heading"><div><span>{html.escape(eyebrow)}</span><h2>{html.escape(title)}</h2></div></header>
  <div class="charts">{charts}</div>
  <div class="narrative"><article><h3>결과요약</h3><p>{summary}</p></article>
  <article><h3>이후 Action 방안</h3><ul>{action_items}</ul></article></div>
  <div class="detail-block"><div class="detail-heading"><div><span>DETAIL DATA</span><h3>세부 데이터 List</h3></div>
  <p>{html.escape(detail_note)}</p></div>{table}</div>
</section>
"""


# 함수 설명: `_safe_json_for_script()`는 01 실시간 생산 분석 Report 생성기 처리 중 JSON·대상·script 관련 값을 계산·변환하는 내부 helper입니다.
def _safe_json_for_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# 함수 설명: `render_production_report_html()`는 production·report·HTML을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다.
def render_production_report_html(
    rows: list[dict[str, Any]],
    analysis: dict[str, Any],
    *,
    warnings: list[dict[str, str]] | None = None,
) -> str:
    scope = analysis["scope"]
    production = analysis["production"]
    shortage = analysis["shortage"]
    capa = analysis["capa"]
    equipment = analysis["equipment"]
    total = scope["case_count"]
    shortage_total = shortage["case_count"]
    capa_anomaly_total = capa["anomaly"]
    equipment_total = equipment["case_count"]
    process_label = ", ".join(scope["processes"]) or "-"
    process_group = scope.get("process_group") or {}
    process_group_label = process_group.get("display_name") or process_group.get("key") or "미지정"
    work_date_label = ", ".join(scope["work_dates"]) or "-"
    snapshot_label = scope["snapshot_at"] or "생성 시각 정보 없음"

    production_chart = _donut_chart(
        "생산실적 분석 PIE CHART",
        [
            ("정상/초과", production["normal_excess"], COLORS["green"]),
            ("Abnormal", production["abnormal"], COLORS["red"]),
            ("생산부족", production["shortage"], COLORS["amber"]),
        ],
    )
    shortage_chart = _donut_chart(
        "생산부족 원인 PIE CHART",
        [
            ("적정재공부족", shortage["primary"]["wip"], COLORS["purple"]),
            ("CAPA부족", shortage["primary"]["capa"], COLORS["red"]),
            ("가동율저조", shortage["primary"]["utilization"], COLORS["amber"]),
            ("원인미분류", shortage["primary"]["unclassified"], COLORS["slate"]),
        ],
    )
    capa_chart = _donut_chart(
        "CAPA실적 PIE CHART",
        [
            ("정상", capa["normal"], COLORS["green"]),
            ("Abnormal", capa["abnormal"], COLORS["red"]),
            ("CAPA실적이상", capa["anomaly"], COLORS["amber"]),
        ],
    )
    capa_detail_chart = _donut_chart(
        "CAPA실적 이상 PIE CHART",
        [
            ("생산부진1", capa["detail"]["생산부진1"], COLORS["purple"]),
            ("생산부진2", capa["detail"]["생산부진2"], COLORS["amber"]),
            ("CAPA부족", capa["detail"]["CAPA부족"], COLORS["red"]),
        ],
    )
    equipment_chart = _donut_chart(
        "장비Assign 조정 PIE CHART",
        [
            ("정상/교체불필요", equipment["normal_no_change"], COLORS["green"]),
            ("장비필요", equipment["장비필요"], COLORS["purple"]),
            ("교체필요", equipment["교체필요"], COLORS["red"]),
        ],
    )

    production_summary = (
        f"{scope['process_count']:,}개 공정({html.escape(process_label)})의 {total:,}개 공정/품별 Case 중 "
        f"정상/초과 {production['normal_excess']:,}건({_percent(production['normal_excess'], total):.1f}%), "
        f"Abnormal {production['abnormal']:,}건({_percent(production['abnormal'], total):.1f}%), "
        f"생산부족 {production['shortage']:,}건({_percent(production['shortage'], total):.1f}%)입니다."
    )
    shortage_summary = (
        f"생산부족 {shortage_total:,}건의 주원인 분석 결과 적정재공부족 {shortage['primary']['wip']:,}건"
        f"({_percent(shortage['primary']['wip'], shortage_total):.1f}%), CAPA부족 {shortage['primary']['capa']:,}건"
        f"({_percent(shortage['primary']['capa'], shortage_total):.1f}%), 가동율저조 "
        f"{shortage['primary']['utilization']:,}건({_percent(shortage['primary']['utilization'], shortage_total):.1f}%)입니다. "
        f"복합 원인은 {shortage['multi_cause_count']:,}건이며 표의 원인별 필터에서 중복 확인할 수 있습니다."
    )
    capa_summary = (
        f"현재 재공이 존재하지만 장비 미보유로 분석이 제한되는 Abnormal 제품은 "
        f"{capa['abnormal_product_count']:,}개입니다. CAPA실적이상 {capa_anomaly_total:,}건 중 "
        f"생산부진1 {capa['detail']['생산부진1']:,}건, 생산부진2 {capa['detail']['생산부진2']:,}건, "
        f"CAPA부족 {capa['detail']['CAPA부족']:,}건입니다."
    )
    equipment_summary = (
        f"CAPA실적이상 {equipment_total:,}건 중 정상/교체불필요 {equipment['normal_no_change']:,}건"
        f"({_percent(equipment['normal_no_change'], equipment_total):.1f}%), 장비필요 {equipment['장비필요']:,}건"
        f"({_percent(equipment['장비필요'], equipment_total):.1f}%), 교체필요 {equipment['교체필요']:,}건"
        f"({_percent(equipment['교체필요'], equipment_total):.1f}%)입니다."
    )

    production_filters = _filter_controls(
        "production",
        [
            ("all", "전체", total),
            ("normal_excess", "정상/초과", production["normal_excess"]),
            ("abnormal", "Abnormal", production["abnormal"]),
            ("shortage", "생산부족", production["shortage"]),
        ],
    )
    shortage_filters = _filter_controls(
        "shortage",
        [
            ("all", "Summary(전체)", shortage_total),
            ("wip", "적정재공부족", shortage["flags"]["wip"]),
            ("capa", "CAPA부족", shortage["flags"]["capa"]),
            ("utilization", "가동율저조", shortage["flags"]["utilization"]),
            ("multi", "복합원인", shortage["multi_cause_count"]),
        ],
    )
    capa_filters = _filter_controls(
        "capa",
        [
            ("all", "전체", total),
            ("정상", "정상", capa["normal"]),
            ("Abnormal", "Abnormal", capa["abnormal"]),
            ("생산부진1", "생산부진1", capa["detail"]["생산부진1"]),
            ("생산부진2", "생산부진2", capa["detail"]["생산부진2"]),
            ("CAPA부족", "CAPA부족", capa["detail"]["CAPA부족"]),
        ],
    )
    equipment_filters = _filter_controls(
        "equipment",
        [
            ("all", "전체", equipment_total),
            ("정상", "정상", equipment["정상"]),
            ("교체불필요", "교체불필요", equipment["교체불필요"]),
            ("장비필요", "장비필요", equipment["장비필요"]),
            ("교체필요", "교체필요", equipment["교체필요"]),
        ],
    )

    sections = "".join(
        [
            _section(
                "production", "01 · PRODUCTION", "생산실적 분석", production_summary,
                [
                    "Abnormal: 생산목표는 없으나 현재 공정에 재공이 존재하는 Case로 장비 미보유 제품은 장비 Assign 검토가 필요합니다.",
                    "생산부족: 재공, CAPA, 장비가동율에 대한 추가 분석이 필요합니다.",
                ],
                production_chart + shortage_chart,
                _table_shell("production", production_filters),
                "전체 데이터, 정상/초과 생산, Abnormal, 생산부족 Case를 선택해 볼 수 있으며 CSV는 현재 선택 결과만 내려받습니다.",
            ),
            _section(
                "shortage", "02 · SHORTAGE", "생산부족 Case 세부 분석", shortage_summary,
                [
                    "적정재공부족: 이전공정의 생산실적을 확인하여 필요시 긴급생산 메일 요청 검토가 필요합니다.",
                    "CAPA부족: CAPA실적 판단을 통해 장비교체 필요 여부를 추가 점검해야 합니다.",
                    "가동율저조: 저가동·Down 장비의 생산기여 가능성을 확인하여 긴급 장비조치 또는 장비교체를 검토해야 합니다.",
                ],
                shortage_chart,
                _table_shell("shortage", shortage_filters),
                "Summary(전체 생산부족), 적정재공부족, CAPA부족, 가동율저조 및 복합원인을 선택해 볼 수 있습니다.",
            ),
            _section(
                "capa", "03 · CAPACITY", "CAPA실적 분석", capa_summary,
                [
                    "Abnormal: SCH'D 담당자에게 생산 긴급도를 확인하고 장비 Assign을 검토해야 합니다.",
                    "생산부진1·생산부진2·CAPA부족: 이전공정 재공현황을 고려한 장비교체 판단이 추가로 필요합니다.",
                ],
                capa_chart + capa_detail_chart,
                _table_shell("capa", capa_filters),
                "전체, Abnormal, 생산부진1, 생산부진2, CAPA부족 Case를 선택해 볼 수 있습니다.",
            ),
            _section(
                "equipment", "04 · EQUIPMENT", "장비Assign 조정 세부 분석", equipment_summary,
                [
                    "장비필요: 재공은 존재하지만 장비 미보유인 제품으로 SCH'D 담당자와 협의하여 장비보유 일정을 검토해야 합니다.",
                    "교체필요: 장비 Balance와 Down 장비의 재가동 가능 여부를 확인하여 장비 Assign 조정을 검토해야 합니다.",
                ],
                equipment_chart,
                _table_shell("equipment", equipment_filters),
                "Down장비를 포함한 CAPA 기준입니다. 전체, 정상, 교체불필요, 장비필요, 교체필요 Case를 선택해 볼 수 있습니다.",
            ),
        ]
    )

    warning_items = "".join(
        f"<li>{html.escape(str(item.get('message') or ''))}</li>"
        for item in (warnings or [])
        if isinstance(item, dict) and item.get("message")
    )
    warning_block = f'<aside class="warning"><strong>데이터 품질 참고</strong><ul>{warning_items}</ul></aside>' if warning_items else ""

    rows_json = _safe_json_for_script(rows)
    columns_json = _safe_json_for_script(ALL_COLUMNS)
    section_columns_json = _safe_json_for_script(SECTION_COLUMNS)
    template = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>실시간 생산 분석 Report</title>
<style>
:root{font-family:Pretendard,"Noto Sans KR",Arial,sans-serif;color:#172033;background:#eef2f6;color-scheme:light}
*{box-sizing:border-box}body{margin:0;background:#eef2f6}.page{max-width:1440px;margin:auto;padding:24px}
.hero{position:relative;overflow:hidden;padding:32px;border-radius:24px;background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;box-shadow:0 18px 50px rgba(15,23,42,.22)}
.hero:after{content:"";position:absolute;right:-90px;top:-120px;width:330px;height:330px;border-radius:50%;background:rgba(56,189,248,.14)}
.hero .eyebrow,.section-heading span,.detail-heading span{font-size:12px;letter-spacing:.14em;font-weight:800;color:#38bdf8}
.hero h1{font-size:32px;margin:8px 0 10px}.hero p{margin:0;color:#cbd5e1}.scope{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
.scope span{padding:8px 12px;border:1px solid rgba(255,255,255,.16);border-radius:999px;background:rgba(255,255,255,.08);font-size:13px}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:18px 0}.metric{background:#fff;border:1px solid #dbe3ec;border-radius:16px;padding:16px;box-shadow:0 8px 24px rgba(15,23,42,.06)}
.metric small,.metric span{display:block;color:#64748b}.metric strong{display:block;font-size:25px;margin:6px 0}.metric span{font-size:12px}.metric.red{border-top:4px solid #dc2626}.metric.amber{border-top:4px solid #d97706}.metric.purple{border-top:4px solid #7c3aed}.metric.green{border-top:4px solid #16a34a}.metric.blue{border-top:4px solid #2563eb}
.tabs{position:sticky;top:0;z-index:20;display:flex;gap:8px;padding:12px;margin:16px 0;background:rgba(238,242,246,.94);backdrop-filter:blur(12px);border-radius:14px;overflow:auto}
.tabs button{white-space:nowrap;border:0;border-radius:10px;padding:10px 14px;background:#fff;color:#475569;font-weight:800;cursor:pointer}.tabs button.active{background:#0f172a;color:#fff}
.report-section{display:none}.report-section.active{display:block}.section-heading{display:flex;justify-content:space-between;align-items:end;margin:26px 2px 14px}.section-heading h2{margin:5px 0 0;font-size:25px}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.chart-card{background:#fff;border:1px solid #dbe3ec;border-radius:18px;padding:20px}.chart-card h3{margin:0 0 16px}
.donut-wrap{display:flex;align-items:center;gap:20px}.donut-wrap svg{width:190px;max-width:42%}.donut-total{font-size:20px;font-weight:900;fill:#172033}.donut-label{font-size:10px;fill:#64748b}
.legend{list-style:none;padding:0;margin:0;flex:1}.legend li{display:grid;grid-template-columns:12px 1fr auto auto;gap:8px;align-items:center;padding:7px 0;border-bottom:1px solid #edf1f5;font-size:13px}.legend i{width:10px;height:10px;border-radius:50%}.legend em{font-style:normal;color:#64748b}
.narrative{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.narrative article{background:#fff;border:1px solid #dbe3ec;border-radius:18px;padding:20px}.narrative h3{margin:0 0 10px}.narrative p,.narrative li{line-height:1.65;color:#475569}.narrative ul{margin:0;padding-left:20px}
.detail-block{margin-top:14px;background:#fff;border:1px solid #dbe3ec;border-radius:18px;padding:20px}.detail-heading{display:flex;justify-content:space-between;align-items:end;gap:12px}.detail-heading h3{margin:5px 0 0}.detail-heading p{color:#64748b;font-size:13px}
.table-tools{display:flex;justify-content:space-between;gap:14px;align-items:center;margin:18px 0 10px;flex-wrap:wrap}.filters{display:flex;flex-wrap:wrap;gap:8px}.filter-chip input{position:absolute;opacity:0}.filter-chip span{display:block;padding:8px 11px;border:1px solid #cbd5e1;border-radius:999px;color:#475569;font-size:13px;cursor:pointer}.filter-chip input:checked+span{background:#e0f2fe;border-color:#0284c7;color:#075985}.filter-chip b{margin-left:4px}
.table-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.search-wrap{display:flex;align-items:center;gap:7px;color:#64748b;font-size:12px}.search-wrap input{width:190px;border:1px solid #cbd5e1;border-radius:9px;padding:9px}.column-toggle{font-size:13px;color:#475569}
.download-button{border:0;border-radius:9px;padding:10px 13px;background:#0f172a;color:#fff;font-weight:800;cursor:pointer}.table-status{font-size:13px;color:#64748b;margin-bottom:8px}
.table-wrap{max-height:560px;overflow:auto;border:1px solid #dbe3ec;border-radius:12px}table{border-collapse:separate;border-spacing:0;width:100%;font-size:12px}th,td{padding:9px 11px;border-right:1px solid #edf1f5;border-bottom:1px solid #edf1f5;text-align:left;white-space:nowrap}th{position:sticky;top:0;background:#f8fafc;z-index:2;color:#334155}tbody tr:hover{background:#f8fafc}
.warning{margin:14px 0;background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;padding:14px;color:#9a3412}.warning ul{margin:7px 0 0}.footer{padding:24px 4px;color:#64748b;font-size:12px;text-align:center}
@media(max-width:900px){.metrics{grid-template-columns:repeat(2,1fr)}.charts,.narrative{grid-template-columns:1fr}.donut-wrap{align-items:flex-start}.page{padding:12px}.hero{padding:24px}.hero h1{font-size:26px}}
</style></head><body><main class="page">
<header class="hero"><span class="eyebrow">REAL-TIME PRODUCTION INTELLIGENCE</span><h1>실시간 생산 분석 Report</h1>
<p>판정 Snapshot을 기반으로 생산실적, 생산부족 원인, CAPA실적, 장비Assign 조정을 일관된 Rule로 분석했습니다.</p>
<div class="scope"><span>공정그룹 · __PROCESS_GROUP__</span><span>WORK_DATE · __WORK_DATE__</span><span>Snapshot · __SNAPSHOT__</span><span>공정 · __PROCESSES__</span><span>Rule · __RULES_VERSION__</span></div></header>
<section class="metrics">__METRICS__</section>__WARNING__
<nav class="tabs" aria-label="Report 영역"><button class="active" data-tab="production">생산실적</button><button data-tab="shortage">생산부족 세부</button><button data-tab="capa">CAPA실적</button><button data-tab="equipment">장비Assign</button></nav>
__SECTIONS__
<footer class="footer">본 Report의 Pie Chart는 상호배타적인 주분류를 사용하며, 복합 생산부족 원인은 세부 표 필터에서 중복 조회됩니다.</footer>
</main>
<script>
const ROWS=__ROWS_JSON__;
const ALL_COLUMNS=__ALL_COLUMNS_JSON__;
const SECTION_COLUMNS=__SECTION_COLUMNS_JSON__;
const state={production:{filter:"all",search:"",full:false},shortage:{filter:"all",search:"",full:false},capa:{filter:"all",search:"",full:false},equipment:{filter:"all",search:"",full:false}};
function baseMatch(section,row){
  if(section==="shortage") return row._production_bucket==="shortage";
  if(section==="equipment") return row._equipment_scope===true;
  return true;
}
function filterMatch(section,row,filter){
  if(filter==="all") return true;
  if(section==="production") return row._production_bucket===filter;
  if(section==="shortage"){
    if(filter==="multi") return Array.isArray(row._shortage_flags)&&row._shortage_flags.length>1;
    return Array.isArray(row._shortage_flags)&&row._shortage_flags.includes(filter);
  }
  if(section==="capa") return row.CAPA이상판단===filter;
  if(section==="equipment") return row.장비교체판단===filter;
  return true;
}
function selectedRows(section){
  const current=state[section];const query=current.search.trim().toLowerCase();
  return ROWS.filter(row=>baseMatch(section,row)&&filterMatch(section,row,current.filter)&&(!query||Object.values(row).some(value=>String(value??"").toLowerCase().includes(query))));
}
function selectedColumns(section){return state[section].full?ALL_COLUMNS:SECTION_COLUMNS[section];}
function displayValue(value){if(value===null||value===undefined||value==="")return "-";if(typeof value==="number")return new Intl.NumberFormat("ko-KR",{maximumFractionDigits:2}).format(value);return String(value);}
function renderTable(section){
  const rows=selectedRows(section),columns=selectedColumns(section),head=document.querySelector(`[data-head="${section}"]`),body=document.querySelector(`[data-body="${section}"]`);
  head.replaceChildren();body.replaceChildren();const hr=document.createElement("tr");
  columns.forEach(column=>{const th=document.createElement("th");th.textContent=column;hr.appendChild(th)});head.appendChild(hr);
  const fragment=document.createDocumentFragment();rows.forEach(row=>{const tr=document.createElement("tr");columns.forEach(column=>{const td=document.createElement("td");td.textContent=displayValue(row[column]);tr.appendChild(td)});fragment.appendChild(tr)});body.appendChild(fragment);
  document.querySelector(`[data-status="${section}"]`).textContent=`${rows.length.toLocaleString("ko-KR")}건 표시 · ${columns.length}개 컬럼`;
}
function csvCell(value){const text=value===null||value===undefined?"":String(value);return `"${text.replaceAll('"','""')}"`;}
function downloadCsv(section){
  const rows=selectedRows(section),columns=selectedColumns(section),lines=[columns.map(csvCell).join(",")];
  rows.forEach(row=>lines.push(columns.map(column=>csvCell(row[column])).join(",")));
  const blob=new Blob(["\\ufeff"+lines.join("\\r\\n")],{type:"text/csv;charset=utf-8"}),url=URL.createObjectURL(blob),link=document.createElement("a");
  link.href=url;link.download=`realtime-production-${section}-${new Date().toISOString().slice(0,10)}.csv`;document.body.appendChild(link);link.click();link.remove();URL.revokeObjectURL(url);
}
document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>{
  document.querySelectorAll("[data-tab]").forEach(item=>item.classList.toggle("active",item===button));
  document.querySelectorAll("[data-panel]").forEach(panel=>panel.classList.toggle("active",panel.dataset.panel===button.dataset.tab));
  renderTable(button.dataset.tab);
}));
document.querySelectorAll('.filter-chip input').forEach(input=>input.addEventListener("change",()=>{const section=input.name.replace("filter-","");state[section].filter=input.value;renderTable(section)}));
document.querySelectorAll("[data-search]").forEach(input=>input.addEventListener("input",()=>{state[input.dataset.search].search=input.value;renderTable(input.dataset.search)}));
document.querySelectorAll("[data-full-columns]").forEach(input=>input.addEventListener("change",()=>{state[input.dataset.fullColumns].full=input.checked;renderTable(input.dataset.fullColumns)}));
document.querySelectorAll("[data-download]").forEach(button=>button.addEventListener("click",()=>downloadCsv(button.dataset.download)));
document.querySelector('[data-panel="production"]').classList.add("active");renderTable("production");
</script></body></html>"""
    metrics = "".join(
        [
            _metric("분석 공정", f"{scope['process_count']:,}개", "blue", process_label),
            _metric("공정/품별 Case", f"{total:,}건", "green", f"고유 제품 {scope['product_count']:,}개"),
            _metric("생산부족", f"{production['shortage']:,}건", "amber", f"{_percent(production['shortage'], total):.1f}%"),
            _metric("CAPA실적이상", f"{capa['anomaly']:,}건", "purple", f"{_percent(capa['anomaly'], total):.1f}%"),
            _metric("장비조정 대상", f"{equipment['장비필요'] + equipment['교체필요']:,}건", "red", f"장비필요 {equipment['장비필요']:,} · 교체필요 {equipment['교체필요']:,}"),
        ]
    )
    replacements = {
        "__PROCESS_GROUP__": html.escape(process_group_label),
        "__WORK_DATE__": html.escape(work_date_label),
        "__SNAPSHOT__": html.escape(snapshot_label),
        "__PROCESSES__": html.escape(process_label),
        "__RULES_VERSION__": html.escape(RULES_VERSION),
        "__METRICS__": metrics,
        "__WARNING__": warning_block,
        "__SECTIONS__": sections,
        "__ROWS_JSON__": rows_json,
        "__ALL_COLUMNS_JSON__": columns_json,
        "__SECTION_COLUMNS_JSON__": section_columns_json,
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


# 함수 설명: `_safe_public_url()`는 public·URL에 접근할 URL을 설정과 식별자로부터 안전하게 구성합니다.
def _safe_public_url(value: Any) -> str:
    candidate = _clip(value, MAX_PUBLIC_URL_CHARS)
    if not candidate or any(ord(character) < 32 for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return ""
    return candidate


# 함수 설명: `_reports_post_url()`는 POST·URL에 접근할 URL을 설정과 식별자로부터 안전하게 구성합니다.
def _reports_post_url(value: Any) -> str:
    base_url = _safe_public_url(value)
    if not base_url:
        return ""
    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/reports") else f"{normalized}/reports"


# 함수 설명: `_report_ttl_hours()`는 01 실시간 생산 분석 Report 생성기 처리 중 TTL·hours 관련 값을 계산·변환하는 내부 helper입니다.
def _report_ttl_hours(value: Any) -> int:
    return _bounded_int(value, DEFAULT_REPORT_TTL_HOURS, 1, MAX_REPORT_TTL_HOURS)


# 함수 설명: `_post_report_json()`는 01 실시간 생산 분석 Report 생성기 처리 중 report·JSON 관련 값을 계산·변환하는 내부 helper입니다.
def _post_report_json(url: str, body: dict[str, Any], timeout_seconds: int = REPORT_API_TIMEOUT_SECONDS) -> dict[str, Any]:
    encoded = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_REPORT_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2_000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Report API HTTP {exc.code}: {_clip(detail, MAX_ERROR_CHARS)}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Report API 연결 실패: {_clip(getattr(exc, 'reason', exc), MAX_ERROR_CHARS)}") from exc
    if len(raw) > MAX_REPORT_RESPONSE_BYTES:
        raise RuntimeError("Report API 응답이 허용 크기를 초과했습니다.")
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Report API 응답이 올바른 UTF-8 JSON이 아닙니다.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Report API 응답이 JSON object 형식이 아닙니다.")
    return parsed


# 함수 설명: `publish_production_report()`는 01 실시간 생산 분석 Report 생성기 처리 중 production·report 관련 값을 계산·변환하는 내부 helper입니다.
def publish_production_report(
    *,
    html_document: str,
    question: str,
    download_name: str,
    analysis: dict[str, Any],
    report_api_url: Any,
    report_ttl_hours: Any,
    available_datasets: Any = None,
    context_ref: Any = "",
) -> dict[str, Any]:
    post_url = _reports_post_url(report_api_url)
    if not post_url:
        raise ValueError("Report API 주소에는 절대 http(s) URL을 입력해야 합니다.")
    response = _post_report_json(
        post_url,
        {
            "html": html_document,
            "title": "실시간 생산 분석 Report",
            "question": _clip(question, MAX_QUESTION_CHARS),
            "view_request": "metadata_driven_v5 realtime production report",
            "available_datasets": (
                list(available_datasets)
                if isinstance(available_datasets, list)
                else []
            ),
            "report_plan": {
                "source_flow": "07. v5_realtime_production_report",
                "rules_version": RULES_VERSION,
                "snapshot_id": _clip(analysis.get("scope", {}).get("snapshot_id"), 200),
                "case_count": int(analysis.get("scope", {}).get("case_count") or 0),
                "context_version": REPORT_CONTEXT_VERSION,
                "context_ref": _clip(context_ref, 500),
            },
            "ttl_hours": _report_ttl_hours(report_ttl_hours),
            "filename_hint": download_name,
        },
    )
    view_url = _safe_public_url(response.get("view_url"))
    download_url = _safe_public_url(response.get("download_url"))
    if not view_url or not download_url:
        raise RuntimeError("Report API 응답에 유효한 view_url과 download_url이 모두 필요합니다.")
    return {
        "report_id": _clip(response.get("report_id"), MAX_REPORT_ID_CHARS),
        "view_url": view_url,
        "download_url": download_url,
        "expires_at": _clip(response.get("expires_at"), 80),
        "ttl_hours": _report_ttl_hours(response.get("ttl_hours") or report_ttl_hours),
        "storage_backend": _clip(
            (response.get("storage") or {}).get("backend")
            if isinstance(response.get("storage"), dict)
            else "mongodb_collection",
            100,
        )
        or "mongodb_collection",
    }


# 함수 설명: `_artifact_descriptor()`는 01 실시간 생산 분석 Report 생성기 처리 중 descriptor 관련 값을 계산·변환하는 내부 helper입니다.
def _artifact_descriptor(
    *,
    download_name: str,
    size_bytes: int,
    rendered_row_count: int,
    total_row_count: int,
    links: dict[str, Any] | None = None,
) -> dict[str, Any]:
    descriptor = {
        "artifact_type": "html_report",
        "storage_backend": "mongodb_collection",
        "mime_type": "text/html",
        "title": "실시간 생산 분석 Report",
        "download_name": download_name,
        "size_bytes": int(size_bytes),
        "row_count": int(total_row_count),
        "rendered_row_count": int(rendered_row_count),
        "rules_version": RULES_VERSION,
    }
    link_data = links if isinstance(links, dict) else {}
    for key in (
        "report_id",
        "view_url",
        "download_url",
        "expires_at",
        "ttl_hours",
        "storage_backend",
    ):
        value = link_data.get(key)
        if value not in (None, ""):
            descriptor[key] = value
    return descriptor


# 함수 설명: `_artifact_links()`는 01 실시간 생산 분석 Report 생성기 처리 중 links 관련 값을 계산·변환하는 내부 helper입니다.
def _artifact_links(descriptor: dict[str, Any]) -> str:
    links: list[str] = []
    view_url = _safe_public_url(descriptor.get("view_url"))
    download_url = _safe_public_url(descriptor.get("download_url"))
    if view_url:
        links.append(f"[상세 HTML Report 보기]({view_url})")
    if download_url:
        links.append(f"[HTML 다운로드]({download_url})")
    return " · ".join(links)


# 함수 설명: `_chat_message()`는 01 실시간 생산 분석 Report 생성기 처리 중 Message 관련 값을 계산·변환하는 내부 helper입니다.
def _chat_message(analysis: dict[str, Any], descriptor: dict[str, Any]) -> str:
    scope = analysis["scope"]
    production = analysis["production"]
    shortage = analysis["shortage"]
    capa = analysis["capa"]
    equipment = analysis["equipment"]
    process_group = scope.get("process_group") or {}
    process_group_label = process_group.get("display_name") or process_group.get("key") or "미지정"
    process_label = ", ".join(scope["processes"]) or "-"
    work_date = ", ".join(scope["work_dates"]) or "-"
    lines = [
        "### 실시간 생산 분석이 완료되었습니다.",
        "",
        f"- 기준: `{work_date}` / 공정그룹 `{process_group_label}` / 세부 공정 {process_label}",
        f"- 데이터 시점: `{scope['snapshot_at'] or '-'}` / {scope['process_count']:,}개 공정 / {scope['case_count']:,} Case / {scope['product_count']:,}개 제품",
        f"- 생산실적: 정상/초과 {production['normal_excess']:,}건({_percent(production['normal_excess'], scope['case_count']):.1f}%), Abnormal {production['abnormal']:,}건, 생산부족 {production['shortage']:,}건({_percent(production['shortage'], scope['case_count']):.1f}%)",
        f"- 생산부족 주원인: 적정재공부족 {shortage['primary']['wip']:,}건, CAPA부족 {shortage['primary']['capa']:,}건, 가동율저조 {shortage['primary']['utilization']:,}건",
        f"- CAPA 이상: 생산부진1 {capa['detail']['생산부진1']:,}건, 생산부진2 {capa['detail']['생산부진2']:,}건, CAPA부족 {capa['detail']['CAPA부족']:,}건",
        f"- 장비조정 대상: 장비필요 {equipment['장비필요']:,}건, 교체필요 {equipment['교체필요']:,}건",
        "",
        f"우선 확인 대상은 장비필요 {equipment['장비필요']:,}건과 교체필요 {equipment['교체필요']:,}건입니다.",
    ]
    link_markdown = _artifact_links(descriptor)
    if link_markdown:
        lines.extend(["", link_markdown])
        if descriptor.get("expires_at"):
            lines.append(f"- 링크 만료: `{descriptor['expires_at']}`")
    else:
        lines.extend(["", "HTML 파일은 생성했지만 공개 링크를 만들지 못했습니다. Report API 실행 상태와 주소를 확인해 주세요."])
    return "\n".join(lines)


# 함수 설명: `_selection_boundary_result()`는 공정그룹 미지정·모호·오류 결과를 HTML 생성 없이 Report API 계약으로 전달합니다.
def _selection_boundary_result(selection: dict[str, Any]) -> dict[str, Any]:
    status = _text(selection.get("status")) or "error"
    is_clarification = status == "clarification_required"
    message = _text(selection.get("message")) or (
        "### 공정그룹을 선택해 주세요.\n실시간 생산 분석을 실행할 공정그룹을 말씀해 주세요."
        if is_clarification
        else "### 실시간 생산 분석 오류\n공정그룹 선택 결과를 확인할 수 없습니다."
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": (
            "realtime_production_process_group_clarification"
            if is_clarification
            else "realtime_production_process_group_error"
        ),
        "status": status,
        "success": is_clarification,
        "summary": "공정그룹 입력 필요" if is_clarification else "",
        "message": message,
        "report_scope": {},
        "rules_version": RULES_VERSION,
        "kpis": {},
        "artifacts": [],
        "context_ref": "",
        "result_ref": {},
        "source_data_refs": [],
        "available_datasets": [],
        "followup": {"available": False, "context_ref": "", "reason": "process_group_not_selected"},
        "state": {},
        "process_group_candidates": list(selection.get("process_group_candidates") or []),
        "matched_process_groups": list(selection.get("matched_process_groups") or []),
        "llm_decision": dict(selection.get("llm_decision") or {}),
        "warnings": [],
        "errors": list(selection.get("errors") or []),
    }


# 함수 설명: `_error_result()`는 예외 정보를 공통 errors 배열과 status가 포함된 실패 결과 구조로 만듭니다.
def _error_result(error: dict[str, str]) -> dict[str, Any]:
    message = str(error.get("message") or "실시간 생산 분석 Report를 생성하지 못했습니다.")
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": "realtime_production_report",
        "status": "error",
        "success": False,
        "summary": "",
        "message": f"### 실시간 생산 분석 오류\n{message}",
        "report_scope": {},
        "kpis": {},
        "artifacts": [],
        "context_ref": "",
        "result_ref": {},
        "source_data_refs": [],
        "available_datasets": [],
        "followup": {"available": False, "context_ref": "", "reason": "report_generation_failed"},
        "state": {},
        "warnings": [],
        "errors": [error],
    }


# 함수 설명: `build_realtime_production_report()`는 realtime·production·report 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def build_realtime_production_report(
    *,
    dataset_value: Any,
    question_value: Any,
    context_payload_value: Any = None,
    max_html_rows: Any = DEFAULT_MAX_HTML_ROWS,
    report_api_url: Any = DEFAULT_REPORT_API_URL,
    report_ttl_hours: Any = DEFAULT_REPORT_TTL_HOURS,
    report_publisher_fn: Any = None,
    file_token: str = "",
):
    # 함수 설명: `_run()`는 RUN 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    async def _run() -> dict[str, Any]:
        dataset = _payload(dataset_value)
        question, session_id = _message_parts(question_value)
        if dataset.get("contract_version") == SELECTION_CONTRACT_VERSION:
            return _with_report_context_tombstone(
                _selection_boundary_result(dataset),
                session_id=session_id,
                question=question,
                reason="process_group_not_selected",
            )
        rows, warnings, validation_error = _validate_dataset(dataset)
        if validation_error:
            return _with_report_context_tombstone(
                _error_result(validation_error),
                session_id=session_id,
                question=question,
                reason=_text(validation_error.get("type") or "report_generation_failed"),
            )
        context = _context_projection(context_payload_value, session_id)
        if not context.get("available"):
            warnings.append(
                _issue(
                    "report_context_unavailable",
                    f"Report는 정상 생성하지만 후속 분석 Context를 사용할 수 없습니다: {context.get('reason') or 'unknown'}",
                )
            )
        analysis = analyze_production_rows(rows, dataset)
        render_limit = _bounded_int(max_html_rows, DEFAULT_MAX_HTML_ROWS, 10, MAX_HTML_ROWS)
        rendered_rows = rows[:render_limit]
        if len(rendered_rows) < len(rows):
            warnings.append(_issue("html_rows_limited", f"HTML 표는 {len(rendered_rows):,}행만 표시합니다. 전체 집계는 {len(rows):,}행 기준입니다."))
        html_document = render_production_report_html(rendered_rows, analysis, warnings=warnings)
        encoded = html_document.encode("utf-8")
        if len(encoded) > MAX_HTML_BYTES:
            error = _issue("html_size_limit_exceeded", f"생성 HTML이 {MAX_HTML_BYTES:,} byte 상한을 초과했습니다.")
            return _with_report_context_tombstone(
                _error_result(error),
                session_id=session_id,
                question=question,
                reason=error["type"],
            )
        safe_token = re.sub(r"[^a-zA-Z0-9]", "", file_token)[:32] or uuid.uuid4().hex
        download_name = f"realtime-production-report-{safe_token}.html"
        configured_report_api = _text(report_api_url)
        if not configured_report_api:
            error = _issue(
                "report_api_required",
                "MongoDB Report API 주소가 비어 있어 HTML을 저장하지 않았습니다.",
            )
            return _with_report_context_tombstone(
                _error_result(error),
                session_id=session_id,
                question=question,
                reason=error["type"],
            )
        publisher = report_publisher_fn if callable(report_publisher_fn) else publish_production_report
        try:
            report_links = await asyncio.to_thread(
                publisher,
                html_document=html_document,
                question=question,
                download_name=download_name,
                analysis=analysis,
                report_api_url=configured_report_api,
                report_ttl_hours=report_ttl_hours,
                available_datasets=context.get("available_datasets", []),
                context_ref=context.get("context_ref", ""),
            )
        except Exception as exc:  # noqa: BLE001
            error = _issue(
                "report_api_publish_error",
                f"MongoDB Report API에 HTML을 저장하지 못했습니다: {exc}",
            )
            return _with_report_context_tombstone(
                _error_result(error),
                session_id=session_id,
                question=question,
                reason=error["type"],
            )
        descriptor = _artifact_descriptor(
            download_name=download_name,
            size_bytes=len(encoded),
            rendered_row_count=len(rendered_rows),
            total_row_count=len(rows),
            links=report_links,
        )
        message = _chat_message(analysis, descriptor)
        kpis = {
            "production": analysis["production"],
            "shortage": analysis["shortage"],
            "capa": analysis["capa"],
            "equipment": analysis["equipment"],
        }
        state = _report_followup_state(
            session_id=session_id,
            question=question,
            message=message,
            analysis=analysis,
            kpis=kpis,
            context=context,
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "response_type": "realtime_production_report",
            "status": "ok",
            "success": True,
            "summary": _clip(
                f"{analysis['scope']['process_group'].get('display_name') or analysis['scope']['process_group'].get('key') or '미지정 그룹'} / "
                f"{analysis['scope']['process_count']}개 공정 {analysis['scope']['case_count']} Case 분석: "
                f"생산부족 {analysis['production']['shortage']}건, 장비필요 {analysis['equipment']['장비필요']}건, "
                f"교체필요 {analysis['equipment']['교체필요']}건",
                500,
            ),
            "message": message,
            "report_scope": analysis["scope"],
            "rules_version": RULES_VERSION,
            "kpis": kpis,
            "artifacts": [descriptor],
            "context_ref": _text(context.get("context_ref")),
            "result_ref": dict(context.get("result_ref") or {}),
            "source_data_refs": list(context.get("source_data_refs") or []),
            "available_datasets": list(context.get("available_datasets") or []),
            "followup": {
                "available": bool(context.get("available")),
                "context_ref": _text(context.get("context_ref")),
                "reason": _text(context.get("reason")),
            },
            "state": state,
            "warnings": warnings,
            "errors": [],
        }

    return _run()


# Langflow 컴포넌트 클래스: 판정 데이터를 규칙 기반으로 집계해 채팅 요약과 독립형 HTML Report를 함께 생성합니다.
class RealtimeProductionReportBuilder(Component):
    display_name = "01 실시간 생산 분석 Report 생성기"
    description = "판정 데이터로 standalone HTML Report를 만들고 API_SERVER의 단일 MongoDB 컬렉션에 발행합니다."
    name = "RealtimeProductionReportBuilder"
    icon = "ChartPie"
    inputs = [
        HandleInput(
            name="question",
            display_name="Report 요청",
            info="Chat Input에서 전달된 현재 사용자 요청과 session입니다.",
            input_types=["Message"],
            required=True,
        ),
        DataInput(
            name="dataset",
            display_name="선택 공정그룹 판정 데이터",
            info="00C Gate가 반환한 선택 그룹 production.judgement.dataset.v1 또는 clarification/error 계약입니다.",
            required=True,
        ),
        DataInput(
            name="context_payload",
            display_name="저장된 Report Context",
            info="00D -> 공용 23 MongoDB 결과 저장소가 반환한 payload입니다. 저장 실패 시에도 Report 자체는 계속 생성합니다.",
            required=False,
        ),
        MessageTextInput(
            name="report_api_url",
            display_name="MongoDB Report API 주소",
            info="API_SERVER의 POST /reports 주소입니다. HTML과 메타데이터는 하나의 MongoDB 컬렉션에 저장되고 보기·다운로드 URL이 반환됩니다.",
            value=DEFAULT_REPORT_API_URL,
            required=False,
            advanced=False,
        ),
        StrInput(
            name="report_ttl_hours",
            display_name="HTML 링크 유효시간",
            info="실시간 Report 기본값은 4시간이며 1~168시간 범위입니다.",
            value=str(DEFAULT_REPORT_TTL_HOURS),
            required=False,
            advanced=False,
        ),
        StrInput(
            name="max_html_rows",
            display_name="HTML 최대 표시 행 수",
            info="초과 행은 집계에는 포함하되 HTML 상세 표에서는 제외합니다.",
            value=str(DEFAULT_MAX_HTML_ROWS),
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(
            name="message",
            display_name="Report 메시지",
            method="build_message",
            types=["Message"],
            group_outputs=True,
        ),
        Output(
            name="api_response",
            display_name="API 응답",
            method="build_api_response",
            types=["Data"],
            group_outputs=True,
        ),
    ]

    # 함수 설명: `_result_once()`는 ONCE에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
    async def _result_once(self) -> dict[str, Any]:
        task = getattr(self, "_report_result_task", None)
        if task is None:
            task = asyncio.create_task(self._execute_report())
            self._report_result_task = task
        return await task

    # 함수 설명: `_execute_report()`는 report 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    async def _execute_report(self) -> dict[str, Any]:
        return await build_realtime_production_report(
            dataset_value=getattr(self, "dataset", None),
            question_value=getattr(self, "question", None),
            context_payload_value=getattr(self, "context_payload", None),
            max_html_rows=getattr(self, "max_html_rows", DEFAULT_MAX_HTML_ROWS),
            report_api_url=getattr(self, "report_api_url", DEFAULT_REPORT_API_URL),
            report_ttl_hours=getattr(self, "report_ttl_hours", DEFAULT_REPORT_TTL_HOURS),
        )

    # 함수 설명: `build_message()`는 구조화 결과를 사용자가 읽을 수 있는 단일 Markdown Message로 변환합니다.
    async def build_message(self) -> Message:
        result = await self._result_once()
        message = Message(text=str(result.get("message") or ""))
        message.files = []
        message.error = str(result.get("status") or "") == "error"
        message.category = "error" if message.error else "message"
        self.status = message
        return message

    # 함수 설명: `build_api_response()`는 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
    async def build_api_response(self) -> Data:
        result = await self._result_once()
        self.status = str(result.get("message") or "")
        return Data(data=result)
