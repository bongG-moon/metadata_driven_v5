# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00D Report Context 저장 페이로드 생성기
# 역할: 선택된 생산 판정 Snapshot을 공용 MongoDB 결과 저장소가 소비하는 Data Analysis 호환 payload로 변환합니다.
# 주요 입력: production.judgement.dataset.v1 Data, 현재 사용자 Message
# 주요 출력: 공용 23 MongoDB 결과 저장소 입력용 Data
# 처리 흐름: 계약·session 검증 -> Snapshot source/result buffer 구성 -> 저장용 compact metadata 구성
# 유지보수 포인트: rows는 result store 직전 내부 버퍼에만 두며 Report 공개 응답이나 세션 state에는 전달하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from math import isfinite
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = "realtime.production.report.context.payload.v1"
DATASET_CONTRACT_VERSION = "production.judgement.dataset.v1"
SOURCE_ALIAS = "report_snapshot"
DATASET_KEY = "production_judgement_snapshot"
SHORTAGE_SOURCE_ALIAS = "report_shortage_products"
SHORTAGE_DATASET_KEY = "report_shortage_products"
QUERY_SOURCE_CONTRACT_VERSION = "report.query_source.v1"
PRODUCT_KEY_COLUMNS = ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"]
SHORTAGE_PRODUCT_COLUMNS = [
    *PRODUCT_KEY_COLUMNS,
    "PRODUCTION",
    "OUT_PLAN",
    "생산실적달성율",
    "달성율*판정",
]
MAX_QUESTION_CHARS = 4_000


# 함수 설명: `_clean()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _clean(value: Any) -> str:
    return str(value or "").strip()


# 함수 설명: `_clip()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _clip(value: Any, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: `_payload()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    raw = getattr(value, "data", value)
    if isinstance(raw, dict):
        return raw
    text = getattr(value, "text", None) or getattr(value, "content", None)
    if not isinstance(text, str) and isinstance(value, str):
        text = value
    if not isinstance(text, str):
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: `_session_id_from_mapping()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _session_id_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("session_id", "sessionId", "conversation_id", "chat_id", "thread_id"):
        session_id = _clean(value.get(key))
        if session_id:
            return session_id
    return ""


# 함수 설명: `_question_parts()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _question_parts(value: Any) -> tuple[str, str]:
    data = getattr(value, "data", None)
    data = data if isinstance(data, dict) else {}
    question = _clean(getattr(value, "text", None) or data.get("text") or data.get("question"))
    if not question and isinstance(value, str):
        question = value.strip()
    session_id = ""
    for attribute in ("session_id", "conversation_id", "chat_id", "thread_id"):
        session_id = _clean(getattr(value, attribute, None))
        if session_id:
            break
    if not session_id:
        for candidate in (
            data,
            data.get("metadata"),
            data.get("properties"),
            data.get("request"),
            getattr(value, "metadata", None),
            getattr(value, "properties", None),
        ):
            session_id = _session_id_from_mapping(candidate)
            if session_id:
                break
    return _clip(question, MAX_QUESTION_CHARS), session_id


# 함수 설명: `_columns()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _columns(dataset: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    declared = dataset.get("columns") if isinstance(dataset.get("columns"), list) else []
    for column in declared:
        text = _clean(column)
        if text and text not in result:
            result.append(text)
    for row in rows:
        for column in row:
            text = str(column)
            if text not in result:
                result.append(text)
    return result


# 함수 설명: `_number()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _number(value: Any) -> float:
    """Return a finite quantity; missing or invalid quantity values are zero."""

    if value in (None, ""):
        return 0.0
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0
    return number if isfinite(number) else 0.0


# 함수 설명: `_compact_number()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 6)


# 함수 설명: `_materialize_shortage_products()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _materialize_shortage_products(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one deterministic row per physical Report product key.

    The view is intentionally derived from shortage cases only. Achievement
    rate is recomputed from the aggregated quantities; row-level percentages
    are never averaged.
    """

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        if _clean(row.get("달성율*판정")) != "생산부족":
            continue
        key = tuple(_clean(row.get(column)) for column in PRODUCT_KEY_COLUMNS)
        current = grouped.get(key)
        if current is None:
            current = {column: deepcopy(row.get(column)) for column in PRODUCT_KEY_COLUMNS}
            current["PRODUCTION"] = 0.0
            current["OUT_PLAN"] = 0.0
            grouped[key] = current
        current["PRODUCTION"] += _number(row.get("PRODUCTION"))
        current["OUT_PLAN"] += _number(row.get("OUT_PLAN"))

    result: list[dict[str, Any]] = []
    for current in grouped.values():
        production = float(current["PRODUCTION"])
        out_plan = float(current["OUT_PLAN"])
        current["PRODUCTION"] = _compact_number(production)
        current["OUT_PLAN"] = _compact_number(out_plan)
        current["생산실적달성율"] = round(production / out_plan * 100, 1) if out_plan else 0.0
        current["달성율*판정"] = "생산부족"
        result.append(current)
    return result


# 함수 설명: `_shortage_query_source_contract()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _shortage_query_source_contract() -> dict[str, Any]:
    """Declare only Report-native physical fields; no global column mapping."""

    return {
        "contract_version": QUERY_SOURCE_CONTRACT_VERSION,
        "source_alias": SHORTAGE_SOURCE_ALIAS,
        "dataset_key": SHORTAGE_DATASET_KEY,
        "purpose": "production_shortage_products",
        "aliases": ["생산부족 제품", "생산 부족 제품", "부족 제품"],
        "authoritative": True,
        "materialized_from": SOURCE_ALIAS,
        "columns": list(SHORTAGE_PRODUCT_COLUMNS),
        "grain": {
            "kind": "product",
            "columns": list(PRODUCT_KEY_COLUMNS),
            "unique": True,
        },
        "predicates": [
            {
                "column": "달성율*판정",
                "operator": "eq",
                "value": "생산부족",
                "materialized": True,
            }
        ],
        "metrics": [
            {
                "key": "production_quantity",
                "column": "PRODUCTION",
                "method": "sum",
                "null_policy": "zero",
            },
            {
                "key": "out_plan_quantity",
                "column": "OUT_PLAN",
                "method": "sum",
                "null_policy": "zero",
            },
            {
                "key": "production_achievement_rate",
                "column": "생산실적달성율",
                "method": "recompute_ratio",
                "numerator_column": "PRODUCTION",
                "denominator_column": "OUT_PLAN",
                "scale": 100,
                "zero_denominator_value": 0.0,
            },
        ],
        "allowed_operations": ["filter", "sort_and_top_n", "select_columns"],
    }


# 함수 설명: `_snapshot_query_source_contract()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _snapshot_query_source_contract(columns: list[str]) -> dict[str, Any]:
    return {
        "contract_version": QUERY_SOURCE_CONTRACT_VERSION,
        "source_alias": SOURCE_ALIAS,
        "dataset_key": DATASET_KEY,
        "purpose": "case_detail",
        "aliases": ["Report 원본", "Report 상세", "원본 판정 데이터", "케이스 상세"],
        "authoritative": True,
        "columns": list(columns),
        "grain": {
            "kind": "case",
            "columns": ["WORK_DATE", "OPER", *PRODUCT_KEY_COLUMNS],
            "unique": False,
        },
        "predicates": [],
        "allowed_operations": ["filter", "sort_and_top_n", "select_columns"],
    }


# 함수 설명: `_blocked_payload()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def _blocked_payload(question: str, session_id: str, issue_type: str, message: str) -> dict[str, Any]:
    issue = {"type": issue_type, "message": message}
    return {
        "contract_version": CONTRACT_VERSION,
        "request": {"question": question, "session_id": session_id},
        "metadata_refs": [],
        "intent_plan": {},
        "source_results": [],
        "analysis": {"status": "skipped", "row_count": 0, "columns": []},
        "data": {"row_count": 0, "columns": []},
        "execution_gate": {"status": "blocked", "reason": issue_type},
        "trace": {
            "warnings": [],
            "errors": [issue],
            "inspection": {
                "report_context_payload": {
                    "stage": "00d_report_context_payload_builder",
                    "status": "blocked",
                    "errors": [issue],
                }
            },
        },
    }


# 함수 설명: `build_report_context_payload()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
def build_report_context_payload(dataset_value: Any, question_value: Any) -> dict[str, Any]:
    """Build the standalone payload consumed by ``23 MongoDB 결과 저장소``.

    The returned runtime row buffers are intentionally internal. The Report
    builder only receives the result-store output and projects compact refs.
    """

    dataset = _payload(dataset_value)
    question, session_id = _question_parts(question_value)
    if dataset.get("contract_version") != DATASET_CONTRACT_VERSION:
        return _blocked_payload(
            question,
            session_id,
            "invalid_report_snapshot_contract",
            f"Report Context 저장에는 {DATASET_CONTRACT_VERSION} 계약이 필요합니다.",
        )
    if not session_id:
        return _blocked_payload(
            question,
            session_id,
            "missing_session_id",
            "현재 실행의 session_id를 확인할 수 없어 Report Context를 저장하지 않았습니다.",
        )
    raw_rows = dataset.get("rows")
    if not isinstance(raw_rows, list):
        return _blocked_payload(
            question,
            session_id,
            "missing_report_snapshot_rows",
            "선택 공정그룹 판정 데이터의 rows를 확인할 수 없습니다.",
        )
    rows = [deepcopy(row) for row in raw_rows if isinstance(row, dict)]
    if not rows:
        return _blocked_payload(
            question,
            session_id,
            "empty_report_snapshot_rows",
            "선택 공정그룹 판정 데이터가 비어 있어 Report Context를 저장하지 않았습니다.",
        )

    columns = _columns(dataset, rows)
    physical_columns = {str(column) for row in rows for column in row}
    materialization_columns = [
        *PRODUCT_KEY_COLUMNS,
        "PRODUCTION",
        "OUT_PLAN",
        "달성율*판정",
    ]
    missing_materialization_columns = [
        column for column in materialization_columns if column not in physical_columns
    ]
    if missing_materialization_columns:
        return _blocked_payload(
            question,
            session_id,
            "report_query_source_schema_invalid",
            "Report 후속 분석용 생산부족 제품 view에 필요한 실제 컬럼이 없습니다: "
            + ", ".join(missing_materialization_columns),
        )
    shortage_rows = _materialize_shortage_products(rows)
    selected_group = dataset.get("selected_process_group")
    selected_group = deepcopy(selected_group) if isinstance(selected_group, dict) else {}
    snapshot_id = _clip(dataset.get("snapshot_id"), 200)
    snapshot_at = _clip(dataset.get("snapshot_at"), 100)
    work_date = _clip(dataset.get("work_date"), 40)
    source_type = _clip(dataset.get("source_type") or "report_snapshot", 80)
    source_result = {
        "source_alias": SOURCE_ALIAS,
        "dataset_key": DATASET_KEY,
        "source_type": source_type,
        "columns": columns,
        "row_count": len(rows),
        "query_source_contract": _snapshot_query_source_contract(columns),
        "applied_params": {"snapshot_id": snapshot_id, "snapshot_at": snapshot_at},
        "applied_filters": {
            "WORK_DATE": work_date,
            "process_group": {
                "key": _clip(selected_group.get("key"), 80),
                "field": _clip(selected_group.get("field"), 120),
                "processes": [
                    _clip(item, 120)
                    for item in selected_group.get("processes", [])
                    if _clean(item)
                ][:100],
            },
        },
    }
    shortage_source_result = {
        "source_alias": SHORTAGE_SOURCE_ALIAS,
        "dataset_key": SHORTAGE_DATASET_KEY,
        "source_type": "report_materialized_view",
        "columns": list(SHORTAGE_PRODUCT_COLUMNS),
        "row_count": len(shortage_rows),
        "query_source_contract": _shortage_query_source_contract(),
        "applied_params": {"snapshot_id": snapshot_id, "snapshot_at": snapshot_at},
        "applied_filters": {"달성율*판정": {"operator": "eq", "value": "생산부족"}},
    }
    payload = {
        "contract_version": CONTRACT_VERSION,
        "request": {
            "question": question,
            "session_id": session_id,
            "request_scope": "report_snapshot",
        },
        "metadata_refs": [],
        "intent_plan": {
            "analysis_kind": "report_context_snapshot",
            "request_scope": "report_snapshot",
            "retrieval_jobs": [],
            "pandas_execution_plan": [],
            "resolved_execution_graph": {
                "external_source_requirements": [
                    {"source_alias": SOURCE_ALIAS, "dataset_key": DATASET_KEY},
                    {
                        "source_alias": SHORTAGE_SOURCE_ALIAS,
                        "dataset_key": SHORTAGE_DATASET_KEY,
                    },
                ]
            },
        },
        "source_results": [source_result, shortage_source_result],
        "runtime_sources": {
            SOURCE_ALIAS: rows,
            SHORTAGE_SOURCE_ALIAS: shortage_rows,
        },
        "_full_result_rows": rows,
        "analysis": {
            "status": "ok",
            "row_count": len(rows),
            "columns": columns,
            "analysis_code": "deterministic_report_snapshot",
        },
        "data": {"row_count": len(rows), "columns": columns},
        "execution_gate": {"status": "ready"},
        "trace": {
            "warnings": [],
            "errors": [],
            "inspection": {
                "report_context_payload": {
                    "stage": "00d_report_context_payload_builder",
                    "status": "ready",
                    "source_alias": SOURCE_ALIAS,
                    "dataset_key": DATASET_KEY,
                    "row_count": len(rows),
                    "query_source_aliases": [SOURCE_ALIAS, SHORTAGE_SOURCE_ALIAS],
                    "shortage_product_count": len(shortage_rows),
                    "snapshot_id": snapshot_id,
                    "session_id": session_id,
                    "errors": [],
                }
            },
        },
    }
    return payload


# 내부 연동 도우미 클래스: `RealtimeProductionReportContextPayloadBuilder`는 컴포넌트 실행에 필요한 상태와 동작을 캡슐화합니다.
class RealtimeProductionReportContextPayloadBuilder(Component):
    display_name = "00D Report Context 저장 페이로드 생성기"
    description = "선택 공정그룹 판정 Snapshot을 공용 MongoDB 결과 저장소 입력 계약으로 변환합니다."
    name = "RealtimeProductionReportContextPayloadBuilder"
    icon = "DatabaseZap"
    inputs = [
        HandleInput(
            name="question",
            display_name="Report 요청",
            info="Chat Input의 현재 요청과 session_id를 전달합니다.",
            input_types=["Message"],
            required=True,
        ),
        DataInput(
            name="dataset",
            display_name="선택 공정그룹 판정 데이터",
            info="00C Gate가 반환한 production.judgement.dataset.v1 데이터입니다.",
            required=True,
        ),
    ]
    outputs = [
        Output(
            name="context_payload",
            display_name="Context 저장 페이로드",
            method="build_context_payload",
            types=["Data"],
        )
    ]

    # 함수 설명: `build_context_payload()`는 입력 계약을 검증하고 해당 단계의 값을 안전하게 계산합니다.
    def build_context_payload(self) -> Data:
        result = build_report_context_payload(
            getattr(self, "dataset", None),
            getattr(self, "question", None),
        )
        self.status = result.get("trace", {}).get("inspection", {}).get("report_context_payload", {})
        return Data(data=result)
