# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00D Realtime Report View Bundle 생성기
# 역할: 실시간 생산 Report의 업무 계산 결과를 사람이 작성할 JSON 계약이 아닌 간단한 Report View Bundle으로 만듭니다.
# 주요 입력: production.judgement.dataset.v1 Data, 현재 사용자 Message
# 주요 출력: 00E 공용 Report Context Publisher가 소비할 report.view.bundle.v1 Data
# 처리 흐름: 계약·session 검증 -> 생산부족 제품 View 계산 -> View 데이터·표시명·업무 계산 규칙만 Bundle에 기록
# 유지보수 포인트: Query Source 계약·Snapshot 저장·후속 분석 정책은 공용 00E가 자동으로 발행합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, Output
from lfx.schema.data import Data


BUNDLE_CONTRACT_VERSION = "report.view.bundle.v1"
DATASET_CONTRACT_VERSION = "production.judgement.dataset.v1"
SOURCE_ALIAS = "report_snapshot"
DATASET_KEY = "production_judgement_snapshot"
SHORTAGE_SOURCE_ALIAS = "report_shortage_products"
SHORTAGE_DATASET_KEY = "report_shortage_products"
PRODUCT_KEY_COLUMNS = ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"]
SHORTAGE_PRODUCT_COLUMNS = [*PRODUCT_KEY_COLUMNS, "PRODUCTION", "OUT_PLAN", "생산실적달성율", "달성율*판정"]
MAX_QUESTION_CHARS = 4_000


# 함수 설명: `_clean()`은 입력 값을 공백이 제거된 문자열로 정규화합니다.
def _clean(value: Any) -> str:
    return str(value or "").strip()


# 함수 설명: `_clip()`은 사용자·추적 정보에 넣을 문자열 길이를 안전하게 제한합니다.
def _clip(value: Any, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: `_payload()`는 Langflow Data 또는 dict 입력에서 원본 dataset mapping을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return raw if isinstance(raw, dict) else {}


# 함수 설명: `_session_id_from_mapping()`은 호환 가능한 mapping에서 session 식별자를 찾습니다.
def _session_id_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("session_id", "sessionId", "conversation_id", "chat_id", "thread_id"):
        session_id = _clean(value.get(key))
        if session_id:
            return session_id
    return ""


# 함수 설명: `_question_parts()`는 Message에서 질문과 session_id를 함께 추출합니다.
def _question_parts(value: Any) -> tuple[str, str]:
    data = getattr(value, "data", None)
    data = data if isinstance(data, dict) else {}
    question = _clean(getattr(value, "text", None) or data.get("text") or data.get("question"))
    session_id = _clean(getattr(value, "session_id", None) or data.get("session_id"))
    if not session_id:
        for candidate in (data, data.get("metadata"), data.get("properties"), data.get("request")):
            session_id = _session_id_from_mapping(candidate)
            if session_id:
                break
    return _clip(question, MAX_QUESTION_CHARS), session_id


# 함수 설명: `_columns()`는 declared schema와 실제 row key를 결합해 표시 가능한 컬럼을 계산합니다.
def _columns(dataset: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for column in dataset.get("columns", []) if isinstance(dataset.get("columns"), list) else []:
        text = _clean(column)
        if text and text not in result:
            result.append(text)
    for row in rows:
        for column in row:
            text = _clean(column)
            if text and text not in result:
                result.append(text)
    return result


# 함수 설명: `_number()`는 결측·비정상 수량을 0으로 처리하는 유한 숫자 변환기입니다.
def _number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0
    return number if isfinite(number) else 0.0


# 함수 설명: `_compact_number()`는 정수처럼 보이는 수량을 정수로 표현해 View 표시를 단순화합니다.
def _compact_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 6)


# 함수 설명: `_materialize_shortage_products()`는 생산부족 Case를 실제 제품 키 기준으로 집계한 Report View를 생성합니다.
def _materialize_shortage_products(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


# 함수 설명: `_blocked_bundle()`은 유효하지 않은 Report 입력을 공용 Publisher가 이해할 수 있는 차단 Bundle으로 반환합니다.
def _blocked_bundle(question: str, session_id: str, issue_type: str, message: str) -> dict[str, Any]:
    issue = {"type": issue_type, "message": message}
    return {
        "contract_version": BUNDLE_CONTRACT_VERSION,
        "request": {"question": question, "session_id": session_id},
        "report": {},
        "views": [],
        "trace": {
            "warnings": [],
            "errors": [issue],
            "inspection": {"realtime_report_view_bundle": {"stage": "00d_realtime_report_view_bundle", "status": "blocked", "errors": [issue]}},
        },
    }


# 주요 함수: 실시간 생산 Report의 계산 결과를 공용 Context Publisher가 자동 계약화할 간단한 View Bundle로 변환합니다.
def build_realtime_report_view_bundle(dataset_value: Any, question_value: Any) -> dict[str, Any]:
    dataset = _payload(dataset_value)
    question, session_id = _question_parts(question_value)
    if dataset.get("contract_version") != DATASET_CONTRACT_VERSION:
        return _blocked_bundle(question, session_id, "invalid_report_snapshot_contract", f"Realtime Report에는 {DATASET_CONTRACT_VERSION} 계약이 필요합니다.")
    if not session_id:
        return _blocked_bundle(question, session_id, "missing_session_id", "현재 실행의 session_id를 확인할 수 없어 Report Context를 저장하지 않았습니다.")
    raw_rows = dataset.get("rows")
    if not isinstance(raw_rows, list):
        return _blocked_bundle(question, session_id, "missing_report_snapshot_rows", "선택 공정그룹 판정 데이터의 rows를 확인할 수 없습니다.")
    rows = [deepcopy(row) for row in raw_rows if isinstance(row, dict)]
    if not rows:
        return _blocked_bundle(question, session_id, "empty_report_snapshot_rows", "선택 공정그룹 판정 데이터가 비어 있어 Report Context를 저장하지 않았습니다.")
    columns = _columns(dataset, rows)
    required_shortage_columns = [*PRODUCT_KEY_COLUMNS, "PRODUCTION", "OUT_PLAN", "달성율*판정"]
    missing_columns = [column for column in required_shortage_columns if column not in set(columns)]
    if missing_columns:
        return _blocked_bundle(question, session_id, "report_recipe_schema_invalid", "생산부족 제품 View 계산에 필요한 실제 컬럼이 없습니다: " + ", ".join(missing_columns))
    shortage_rows = _materialize_shortage_products(rows)
    selected_group = dataset.get("selected_process_group") if isinstance(dataset.get("selected_process_group"), dict) else {}
    snapshot_id = _clip(dataset.get("snapshot_id"), 200)
    snapshot_at = _clip(dataset.get("snapshot_at"), 100)
    work_date = _clip(dataset.get("work_date"), 40)
    source_type = _clip(dataset.get("source_type") or "report_snapshot", 80)
    return {
        "contract_version": BUNDLE_CONTRACT_VERSION,
        "request": {"question": question, "session_id": session_id},
        "report": {
            "report_type": "realtime_production",
            "title": "실시간 생산 분석 Report",
            "snapshot_id": snapshot_id,
            "snapshot_at": snapshot_at,
            "scope": {
                "work_date": work_date,
                "process_group": {
                    "key": _clip(selected_group.get("key"), 80),
                    "display_name": _clip(selected_group.get("display_name"), 200),
                    "field": _clip(selected_group.get("field"), 120),
                    "processes": [_clip(item, 120) for item in selected_group.get("processes", []) if _clean(item)][:100],
                },
            },
        },
        "views": [
            {
                "view_key": SOURCE_ALIAS,
                "dataset_key": DATASET_KEY,
                "display_name": "Report 상세",
                "aliases": ["Report 원본", "Report 상세", "원본 판정 데이터", "케이스 상세"],
                "purpose": "case_detail",
                "rows": rows,
                "columns": columns,
                "identity_columns": ["WORK_DATE", "OPER", *PRODUCT_KEY_COLUMNS],
                "grain": {"kind": "case", "unique": False},
                "default_view": True,
                "followup_enabled": True,
                "source_type": source_type,
            },
            {
                "view_key": SHORTAGE_SOURCE_ALIAS,
                "dataset_key": SHORTAGE_DATASET_KEY,
                "display_name": "생산부족 제품",
                "aliases": ["생산부족 제품", "생산 부족 제품", "부족 제품"],
                "purpose": "production_shortage_products",
                "rows": shortage_rows,
                "columns": list(SHORTAGE_PRODUCT_COLUMNS),
                "identity_columns": list(PRODUCT_KEY_COLUMNS),
                "grain": {"kind": "product", "unique": True},
                "metrics": [
                    {"key": "production_quantity", "column": "PRODUCTION", "method": "sum"},
                    {"key": "out_plan_quantity", "column": "OUT_PLAN", "method": "sum"},
                    {"key": "production_achievement_rate", "column": "생산실적달성율", "method": "recompute_ratio", "numerator_column": "PRODUCTION", "denominator_column": "OUT_PLAN", "scale": 100},
                ],
                "predicates": [{"column": "달성율*판정", "operator": "eq", "value": "생산부족", "materialized": True}],
                "lineage": [SOURCE_ALIAS],
                "followup_enabled": True,
                "source_type": "report_materialized_view",
            },
        ],
        "trace": {
            "warnings": [],
            "errors": [],
            "inspection": {
                "realtime_report_view_bundle": {
                    "stage": "00d_realtime_report_view_bundle",
                    "status": "ready",
                    "view_keys": [SOURCE_ALIAS, SHORTAGE_SOURCE_ALIAS],
                    "shortage_product_count": len(shortage_rows),
                    "snapshot_id": snapshot_id,
                    "session_id": session_id,
                    "errors": [],
                }
            },
        },
    }


# Langflow 컴포넌트 클래스: Realtime Report의 업무 계산 결과를 공용 Publisher용 Bundle으로 분리합니다.
class RealtimeProductionReportViewBundleBuilder(Component):
    display_name = "00D Realtime Report View Bundle 생성기"
    description = "실시간 생산 Report의 원본·생산부족 제품 View를 만들고 공용 Context Publisher에 전달합니다."
    name = "RealtimeProductionReportViewBundleBuilder"
    icon = "Layers3"
    inputs = [
        HandleInput(name="question", display_name="Report 요청", info="Chat Input의 현재 요청과 session_id를 전달합니다.", input_types=["Message"], required=True),
        DataInput(name="dataset", display_name="선택 공정그룹 판정 데이터", info="00C Gate가 반환한 production.judgement.dataset.v1 데이터입니다.", required=True),
    ]
    outputs = [Output(name="report_bundle", display_name="Report View Bundle", method="build_report_bundle", types=["Data"])]

    # 함수 설명: `build_report_bundle()`은 Realtime Report View Bundle을 생성해 다음 공용 Publisher에 전달합니다.
    def build_report_bundle(self) -> Data:
        result = build_realtime_report_view_bundle(getattr(self, "dataset", None), getattr(self, "question", None))
        self.status = result.get("trace", {}).get("inspection", {}).get("realtime_report_view_bundle", {})
        return Data(data=result)
