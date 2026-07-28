# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00C 실시간 생산 공정그룹 선택 Gate
# 역할: LLM 선택을 Domain 허용목록과 질문 원문으로 재검증하고 선택 그룹의 행만 Report 생성기에 전달합니다.
# 주요 입력: 사용자 질문, 공정그룹 카탈로그, LLM JSON 응답, 전체 판정 Snapshot
# 주요 출력: 선택된 production.judgement.dataset.v1 또는 clarification/error 계약 Data
# 처리 흐름: 질문 증거 검색 -> LLM JSON 검증 -> 허용 그룹 확정 -> payload.field/processes 필터
# 유지보수 포인트: LLM이 질문에 없는 그룹을 추측해도 통과시키지 않으며 미지정 시 HTML 생성 경계를 열지 않습니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = "production.process_group.selection.v1"
DATASET_CONTRACT_VERSION = "production.judgement.dataset.v1"
CATALOG_CONTRACT_VERSION = "domain.process_group.catalog.v1"
MAX_LLM_RESPONSE_CHARS = 20_000


# 함수 설명: `_text()`는 Message 또는 일반 입력을 질문·LLM 응답 검증용 문자열로 변환합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        candidate = getattr(value, "text", None) or data.get("text")
        if candidate is not None:
            return str(candidate).strip()
    return str(getattr(value, "text", value) or "").strip()


# 함수 설명: `_payload()`는 Langflow Data 또는 dict 입력을 독립 복사된 페이로드로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return deepcopy(raw) if isinstance(raw, dict) else {}


# 함수 설명: `_parse_llm_decision()`은 Language Model의 JSON 또는 fenced JSON 응답을 선택 object로 파싱합니다.
def _parse_llm_decision(value: Any) -> tuple[dict[str, Any], dict[str, str] | None]:
    if isinstance(value, dict):
        payload = deepcopy(value)
    else:
        raw = _text(value)[:MAX_LLM_RESPONSE_CHARS]
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
        candidate = fenced.group(1).strip() if fenced else raw
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return {}, {"type": "invalid_process_group_llm_json", "message": f"공정그룹 선택 LLM 응답이 JSON object가 아닙니다: {exc}"}
    if not isinstance(payload, dict):
        return {}, {"type": "invalid_process_group_llm_shape", "message": "공정그룹 선택 LLM 응답은 JSON object여야 합니다."}
    return payload, None


# 함수 설명: `_token_present()`는 짧은 key의 부분 문자열 오탐을 피하면서 질문에 Domain 표현이 실제로 있는지 검사합니다.
def _token_present(question: str, token: str, *, short_key: bool = False) -> bool:
    candidate = str(token or "").strip()
    if not candidate:
        return False
    if short_key and re.fullmatch(r"[A-Za-z0-9]{1,3}", candidate):
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])",
                question,
                flags=re.IGNORECASE,
            )
        )
    return candidate.casefold() in question.casefold()


# 함수 설명: `find_explicit_process_group_matches()`는 질문의 key·alias·display·세부 공정 근거를 그룹별로 수집합니다.
def find_explicit_process_group_matches(question: str, groups: list[dict[str, Any]]) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for group in groups:
        key = str(group.get("key") or "").strip().upper()
        evidence: list[str] = []
        for process in group.get("processes") or []:
            if _token_present(question, str(process)):
                evidence.append(str(process))
        for alias in group.get("aliases") or []:
            if _token_present(question, str(alias), short_key=str(alias).strip().upper() == key):
                evidence.append(str(alias))
        display_name = str(group.get("display_name") or "")
        if _token_present(question, display_name):
            evidence.append(display_name)
        if _token_present(question, key, short_key=True):
            evidence.append(key)
        unique = list(dict.fromkeys(item for item in evidence if item))
        if key and unique:
            matches[key] = unique
    return matches


# 함수 설명: `_candidate_summary()`는 재질문 응답에 노출할 공정그룹 후보 필드만 선택합니다.
def _candidate_summary(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": str(group.get("key") or ""),
            "display_name": str(group.get("display_name") or ""),
            "aliases": list(group.get("aliases") or []),
            "processes": list(group.get("processes") or []),
        }
        for group in groups
    ]


# 함수 설명: `_clarification_message()`는 미지정 또는 다중지정 상황에서 단일 공정그룹을 요청하는 안내문을 만듭니다.
def _clarification_message(groups: list[dict[str, Any]], matched_keys: list[str]) -> str:
    candidates = ", ".join(
        f"`{group.get('display_name') or group.get('key')}`"
        for group in groups
    ) or "등록된 공정그룹 없음"
    if len(matched_keys) > 1:
        matched = ", ".join(f"`{key}`" for key in matched_keys)
        lead = f"여러 공정그룹({matched})이 함께 확인되어 하나를 선택해야 합니다."
    else:
        lead = "실시간 생산 분석을 실행할 공정그룹이 질문에 없습니다."
    return (
        "### 공정그룹을 선택해 주세요.\n\n"
        f"{lead} Report는 아직 생성하지 않았습니다.\n\n"
        f"- 선택 가능한 공정그룹: {candidates}\n"
        "- 예시: `W/B 공정 그룹의 실시간 생산 분석 Report를 만들어줘`"
    )


# 함수 설명: `_selection_boundary()`는 Report를 생성하지 않는 선택 결과를 공통 clarification/error 계약으로 만듭니다.
def _selection_boundary(
    *,
    status: str,
    message: str,
    groups: list[dict[str, Any]],
    matches: dict[str, list[str]],
    llm_decision: dict[str, Any],
    errors: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "response_type": "realtime_production_process_group_selection",
        "status": status,
        "success": status == "clarification_required",
        "message": message,
        "selected_process_group": {},
        "process_group_candidates": _candidate_summary(groups),
        "matched_process_groups": [
            {"key": key, "evidence": evidence}
            for key, evidence in matches.items()
        ],
        "llm_decision": {
            "status": str(llm_decision.get("status") or ""),
            "process_group_key": str(llm_decision.get("process_group_key") or ""),
            "reason": str(llm_decision.get("reason") or "")[:500],
            "evidence": list(llm_decision.get("evidence") or [])[:20],
        },
        "errors": list(errors or []),
    }


# 함수 설명: `select_process_group_dataset()`는 LLM 선택을 질문 근거로 검증하고 Domain 허용 공정 행만 남깁니다.
def select_process_group_dataset(
    *,
    question_value: Any,
    catalog_value: Any,
    llm_response_value: Any,
    dataset_value: Any,
) -> dict[str, Any]:
    question = _text(question_value)
    catalog = _payload(catalog_value)
    groups = [
        deepcopy(group)
        for group in catalog.get("process_groups", [])
        if isinstance(group, dict) and str(group.get("key") or "").strip()
    ]
    if catalog.get("contract_version") != CATALOG_CONTRACT_VERSION or catalog.get("status") != "ok" or not groups:
        errors = list(catalog.get("errors") or [])
        if not errors:
            errors = [{"type": "invalid_process_group_catalog", "message": "사용 가능한 공정그룹 카탈로그가 없습니다."}]
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n공정그룹 정보를 불러오지 못해 Report를 생성하지 않았습니다.",
            groups=groups,
            matches={},
            llm_decision={},
            errors=errors,
        )

    matches = find_explicit_process_group_matches(question, groups)
    matched_keys = sorted(matches)
    llm_decision, llm_error = _parse_llm_decision(llm_response_value)
    if len(matched_keys) != 1:
        return _selection_boundary(
            status="clarification_required",
            message=_clarification_message(groups, matched_keys),
            groups=groups,
            matches=matches,
            llm_decision=llm_decision,
            errors=[],
        )
    if llm_error:
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n공정그룹 선택 응답을 검증하지 못해 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            llm_decision={},
            errors=[llm_error],
        )

    selected_key = matched_keys[0]
    llm_status = str(llm_decision.get("status") or "").strip().lower()
    llm_key = str(llm_decision.get("process_group_key") or "").strip().upper()
    if llm_status != "selected" or llm_key != selected_key:
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\nLLM의 공정그룹 선택과 질문 원문 검증 결과가 일치하지 않아 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            llm_decision=llm_decision,
            errors=[
                {
                    "type": "process_group_selection_mismatch",
                    "message": f"질문 근거 그룹={selected_key}, LLM 선택 그룹={llm_key or '-'}",
                }
            ],
        )

    selected_group = next(group for group in groups if str(group.get("key") or "").strip().upper() == selected_key)
    dataset = _payload(dataset_value)
    rows = dataset.get("rows")
    if not isinstance(rows, list):
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n판정 데이터 rows를 확인할 수 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            llm_decision=llm_decision,
            errors=[{"type": "missing_dataset_rows", "message": "판정 데이터 rows가 배열이 아닙니다."}],
        )
    field = str(selected_group.get("field") or "OPER_NAME").strip()
    allowed_processes = {str(item).strip().casefold() for item in selected_group.get("processes") or [] if str(item).strip()}
    selected_rows = [
        deepcopy(row)
        for row in rows
        if isinstance(row, dict) and str(row.get(field) or "").strip().casefold() in allowed_processes
    ]
    if not selected_rows:
        return _selection_boundary(
            status="error",
            message=f"### 실시간 생산 분석 오류\n선택한 `{selected_group['display_name']}`의 판정 데이터가 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            llm_decision=llm_decision,
            errors=[
                {
                    "type": "empty_selected_process_group_dataset",
                    "message": f"{field}에 등록 공정({', '.join(selected_group.get('processes') or [])})이 없습니다.",
                }
            ],
        )

    selected = deepcopy(dataset)
    selected["contract_version"] = DATASET_CONTRACT_VERSION
    selected["rows"] = selected_rows
    selected["row_count"] = len(selected_rows)
    selected["processes"] = sorted(
        {str(row.get(field) or "").strip() for row in selected_rows if str(row.get(field) or "").strip()}
    )
    selected["selected_process_group"] = {
        "key": selected_group["key"],
        "display_name": selected_group["display_name"],
        "aliases": list(selected_group.get("aliases") or []),
        "field": field,
        "processes": list(selected_group.get("processes") or []),
        "question_evidence": matches[selected_key],
        "llm_reason": str(llm_decision.get("reason") or "")[:500],
    }
    selected["selection_contract_version"] = CONTRACT_VERSION
    selected["unfiltered_row_count"] = len(rows)
    return selected


# Langflow 컴포넌트 클래스: 공정그룹 미지정 시 Report 생성을 닫고 단일 그룹 선택 데이터만 다음 노드로 전달합니다.
class RealtimeProductionProcessGroupSelectionGate(Component):
    display_name = "00C 실시간 생산 공정그룹 선택 Gate"
    description = "LLM 선택을 Domain 허용목록과 질문 원문으로 검증하고 해당 공정그룹 데이터만 통과시킵니다."
    name = "RealtimeProductionProcessGroupSelectionGate"
    icon = "ShieldCheck"
    inputs = [
        HandleInput(name="question", display_name="사용자 질문", input_types=["Message"], required=True),
        DataInput(name="process_group_catalog", display_name="공정그룹 카탈로그", required=True),
        HandleInput(name="llm_response", display_name="LLM 공정그룹 선택", input_types=["Message"], required=True),
        DataInput(name="dataset", display_name="전체 판정 데이터", required=True),
    ]
    outputs = [
        Output(
            name="selected_dataset",
            display_name="선택 공정그룹 판정 데이터",
            method="build_selected_dataset",
            types=["Data"],
        )
    ]

    # 함수 설명: `build_selected_dataset()`는 현재 입력을 검증해 선택 데이터 또는 재질문 계약을 Langflow Data로 반환합니다.
    def build_selected_dataset(self) -> Data:
        payload = select_process_group_dataset(
            question_value=getattr(self, "question", None),
            catalog_value=getattr(self, "process_group_catalog", None),
            llm_response_value=getattr(self, "llm_response", None),
            dataset_value=getattr(self, "dataset", None),
        )
        if payload.get("contract_version") == DATASET_CONTRACT_VERSION:
            group = payload.get("selected_process_group") or {}
            self.status = f"{group.get('display_name') or group.get('key')} / {payload.get('row_count', 0):,}행"
        else:
            self.status = str(payload.get("message") or payload.get("status") or "")
        return Data(data=payload)
