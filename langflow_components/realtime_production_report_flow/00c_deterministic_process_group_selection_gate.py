# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00C 실시간 생산 결정론 공정그룹 선택 Gate
# 역할: 질문 원문에 명시된 Domain 공정그룹을 규칙으로 하나만 선택하고 해당 행만 통과시킵니다.
# 주요 입력: 사용자 질문, 공정그룹 카탈로그, 전체 판정 Snapshot
# 주요 출력: 선택된 production.judgement.dataset.v1 또는 clarification/error 계약 Data
# 처리 흐름: 계약 검증 -> 질문 증거 검색 -> 단일 그룹 확정 -> field/processes 허용목록 필터
# 유지보수 포인트: 의미 추측이나 기본 그룹 선택 없이 metadata에 등록된 표현만 정확히 허용합니다.
# =============================================================================

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, Output
from lfx.schema.data import Data


CONTRACT_VERSION = "production.process_group.selection.v1"
DATASET_CONTRACT_VERSION = "production.judgement.dataset.v1"
CATALOG_CONTRACT_VERSION = "domain.process_group.catalog.v1"
SELECTION_METHOD = "deterministic_explicit_match"
MAX_QUESTION_CHARS = 4_000


# 함수 설명: `_text()`는 Message 또는 일반 입력을 질문 검증용 문자열로 변환합니다.
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


# 함수 설명: `_empty_llm_decision()`은 기존 selection.v1 공개 shape와의 호환을 유지합니다.
def _empty_llm_decision() -> dict[str, Any]:
    return {
        "status": "",
        "process_group_key": "",
        "reason": "",
        "evidence": [],
    }


# 함수 설명: `_selection_provenance()`는 모델을 사용하지 않은 선택 근거만 제한된 크기로 기록합니다.
def _selection_provenance(
    matches: dict[str, list[str]],
    *,
    selected_key: str = "",
) -> dict[str, Any]:
    return {
        "method": SELECTION_METHOD,
        "catalog_contract_version": CATALOG_CONTRACT_VERSION,
        "matched_group_count": len(matches),
        "selected_key": str(selected_key or ""),
        "question_evidence": {
            str(key): [str(item)[:200] for item in evidence[:20]]
            for key, evidence in matches.items()
        },
    }


# 함수 설명: `_token_present()`는 등록 표현의 영숫자 경계를 지켜 D/A1과 D/A10 같은 prefix 오탐을 차단합니다.
def _token_present(question: str, token: str, *, short_key: bool = False) -> bool:
    candidate = str(token or "").strip()
    if not candidate:
        return False
    if short_key or re.search(r"[A-Za-z0-9]", candidate):
        left_boundary = r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", candidate) else ""
        right_boundary = r"(?![A-Za-z0-9])" if re.search(r"[A-Za-z0-9]$", candidate) else ""
        return bool(
            re.search(
                rf"{left_boundary}{re.escape(candidate)}{right_boundary}",
                question,
                flags=re.IGNORECASE,
            )
        )
    return candidate.casefold() in question.casefold()


# 함수 설명: `find_explicit_process_group_matches()`는 key·alias·display·세부 공정 근거를 그룹별로 수집합니다.
def find_explicit_process_group_matches(
    question: str,
    groups: list[dict[str, Any]],
) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for group in groups:
        key = str(group.get("key") or "").strip().upper()
        evidence: list[str] = []
        for process in group.get("processes") or []:
            if _token_present(question, str(process)):
                evidence.append(str(process))
        for alias in group.get("aliases") or []:
            if _token_present(
                question,
                str(alias),
                short_key=str(alias).strip().upper() == key,
            ):
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


# 함수 설명: `_validated_groups()`는 카탈로그 항목의 필수 schema와 key 유일성을 fail-closed로 검증합니다.
def _validated_groups(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    raw_groups = catalog.get("process_groups")
    if not isinstance(raw_groups, list):
        return [], [{"type": "invalid_process_group_catalog_shape", "message": "process_groups가 배열이 아닙니다."}]

    groups: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            errors.append(
                {
                    "type": "invalid_process_group_item",
                    "message": f"공정그룹 #{index + 1}이 object가 아닙니다.",
                }
            )
            continue
        group = deepcopy(raw_group)
        key = str(group.get("key") or "").strip().upper()
        display_name = str(group.get("display_name") or "").strip()
        field = str(group.get("field") or "").strip()
        aliases = group.get("aliases")
        processes = group.get("processes")
        if (
            not key
            or not display_name
            or not field
            or not isinstance(aliases, list)
            or not isinstance(processes, list)
            or not any(str(item or "").strip() for item in processes)
        ):
            errors.append(
                {
                    "type": "invalid_process_group_item",
                    "message": f"공정그룹 #{index + 1}의 key/display_name/aliases/field/processes 계약이 불완전합니다.",
                }
            )
            continue
        if key in seen_keys:
            errors.append(
                {
                    "type": "duplicate_process_group_key",
                    "message": f"중복 공정그룹 key가 있습니다: {key}",
                }
            )
            continue
        seen_keys.add(key)
        group["key"] = key
        group["display_name"] = display_name
        group["field"] = field
        group["aliases"] = [str(item).strip() for item in aliases if str(item or "").strip()]
        group["processes"] = [str(item).strip() for item in processes if str(item or "").strip()]
        groups.append(group)
    return groups, errors


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


# 함수 설명: `_clarification_message()`는 미지정 또는 다중지정 상황에서 단일 공정그룹을 요청합니다.
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


# 함수 설명: `_selection_boundary()`는 Report를 만들지 않는 clarification/error 계약을 생성합니다.
def _selection_boundary(
    *,
    status: str,
    message: str,
    groups: list[dict[str, Any]],
    matches: dict[str, list[str]],
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
        "llm_decision": _empty_llm_decision(),
        "selection_provenance": _selection_provenance(matches),
        "errors": list(errors or []),
    }


# 함수 설명: `select_process_group_dataset()`는 질문의 단일 명시 그룹만 선택해 허용된 판정 행을 반환합니다.
def select_process_group_dataset(
    *,
    question_value: Any,
    catalog_value: Any,
    dataset_value: Any,
) -> dict[str, Any]:
    question = _text(question_value)[:MAX_QUESTION_CHARS]
    catalog = _payload(catalog_value)
    groups, group_errors = _validated_groups(catalog)
    if (
        catalog.get("contract_version") != CATALOG_CONTRACT_VERSION
        or catalog.get("status") != "ok"
        or not groups
        or group_errors
    ):
        errors = [
            deepcopy(item)
            for item in catalog.get("errors", [])
            if isinstance(item, dict)
        ]
        errors.extend(group_errors)
        if not errors:
            errors = [{"type": "invalid_process_group_catalog", "message": "사용 가능한 공정그룹 카탈로그가 없습니다."}]
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n공정그룹 정보를 불러오지 못해 Report를 생성하지 않았습니다.",
            groups=groups,
            matches={},
            errors=errors,
        )

    matches = find_explicit_process_group_matches(question, groups)
    matched_keys = sorted(matches)
    if len(matched_keys) != 1:
        return _selection_boundary(
            status="clarification_required",
            message=_clarification_message(groups, matched_keys),
            groups=groups,
            matches=matches,
            errors=[],
        )

    selected_key = matched_keys[0]
    selected_group = next(
        group
        for group in groups
        if str(group.get("key") or "").strip().upper() == selected_key
    )
    dataset = _payload(dataset_value)
    if dataset.get("contract_version") != DATASET_CONTRACT_VERSION:
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n판정 데이터 계약을 확인할 수 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            errors=[
                {
                    "type": "invalid_production_judgement_dataset_contract",
                    "message": f"판정 데이터 contract_version은 {DATASET_CONTRACT_VERSION}이어야 합니다.",
                }
            ],
        )

    rows = dataset.get("rows")
    columns = dataset.get("columns")
    if not isinstance(rows, list):
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n판정 데이터 rows를 확인할 수 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            errors=[{"type": "missing_dataset_rows", "message": "판정 데이터 rows가 배열이 아닙니다."}],
        )
    if not isinstance(columns, list) or any(not isinstance(row, dict) for row in rows):
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n판정 데이터 schema를 확인할 수 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            errors=[
                {
                    "type": "invalid_production_judgement_dataset_schema",
                    "message": "판정 데이터 columns는 배열이고 모든 rows 항목은 object여야 합니다.",
                }
            ],
        )

    field = str(selected_group.get("field") or "").strip()
    if field not in {str(column) for column in columns} or (rows and any(field not in row for row in rows)):
        return _selection_boundary(
            status="error",
            message="### 실시간 생산 분석 오류\n공정그룹 필터 컬럼이 판정 데이터에 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
            errors=[
                {
                    "type": "missing_process_group_field",
                    "message": f"판정 데이터에 metadata field `{field}`가 없습니다.",
                }
            ],
        )

    allowed_processes = {
        str(item).strip().casefold()
        for item in selected_group.get("processes") or []
        if str(item).strip()
    }
    selected_rows = [
        deepcopy(row)
        for row in rows
        if str(row.get(field) or "").strip().casefold() in allowed_processes
    ]
    if not selected_rows:
        return _selection_boundary(
            status="error",
            message=f"### 실시간 생산 분석 오류\n선택한 `{selected_group['display_name']}`의 판정 데이터가 없어 Report를 생성하지 않았습니다.",
            groups=groups,
            matches=matches,
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
        {
            str(row.get(field) or "").strip()
            for row in selected_rows
            if str(row.get(field) or "").strip()
        }
    )
    selected["selected_process_group"] = {
        "key": selected_group["key"],
        "display_name": selected_group["display_name"],
        "aliases": list(selected_group.get("aliases") or []),
        "field": field,
        "processes": list(selected_group.get("processes") or []),
        "question_evidence": matches[selected_key],
        "llm_reason": "",
    }
    selected["selection_contract_version"] = CONTRACT_VERSION
    selected["selection_provenance"] = _selection_provenance(
        matches,
        selected_key=selected_key,
    )
    selected["unfiltered_row_count"] = len(rows)
    return selected


# Langflow 컴포넌트 클래스: 명시된 단일 공정그룹만 선택하고 미지정·모호 입력에서는 Report 생성을 닫습니다.
class RealtimeProductionDeterministicProcessGroupSelectionGate(Component):
    display_name = "00C 실시간 생산 결정론 공정그룹 선택 Gate"
    description = "질문 원문과 Domain 허용목록만으로 단일 공정그룹을 확정하고 해당 데이터만 통과시킵니다."
    name = "RealtimeProductionDeterministicProcessGroupSelectionGate"
    icon = "ShieldCheck"
    inputs = [
        HandleInput(name="question", display_name="사용자 질문", input_types=["Message"], required=True),
        DataInput(name="process_group_catalog", display_name="공정그룹 카탈로그", required=True),
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

    # 함수 설명: `build_selected_dataset()`는 선택 데이터 또는 재질문/error 계약을 Langflow Data로 반환합니다.
    def build_selected_dataset(self) -> Data:
        payload = select_process_group_dataset(
            question_value=getattr(self, "question", None),
            catalog_value=getattr(self, "process_group_catalog", None),
            dataset_value=getattr(self, "dataset", None),
        )
        if payload.get("contract_version") == DATASET_CONTRACT_VERSION:
            group = payload.get("selected_process_group") or {}
            self.status = f"{group.get('display_name') or group.get('key')} / {payload.get('row_count', 0):,}행"
        else:
            self.status = str(payload.get("message") or payload.get("status") or "")
        return Data(data=payload)
