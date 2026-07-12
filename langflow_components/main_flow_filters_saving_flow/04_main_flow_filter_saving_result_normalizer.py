# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 메인 플로우 필터 등록 결과 정규화기
# 역할: 메인 플로우 필터 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다.
# 주요 입력: 페이로드 (payload) · 필수, LLM 응답 (llm_response) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: Markdown code fence를 제거하고 LLM JSON의 호환 key를 메인 플로우 필터 저장 스키마로 정규화합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

# 주요 함수: LLM 등록 후보 JSON을 추출·검증해 저장 전 표준 items 배열로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def normalize_authoring(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    items = []
    errors = []
    if not parsed:
        errors.append({"type": "llm_response_parse_error", "message": "LLM 등록 응답을 JSON object로 해석하지 못했습니다."})
    elif not isinstance(parsed.get("items"), list):
        errors.append({"type": "invalid_items", "message": "LLM 등록 응답의 items는 배열이어야 합니다."})
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append({"type": "invalid_item", "message": f"items[{index}]가 object가 아닙니다."})
            continue
        item = deepcopy(raw)
        if "filter_key" not in item and "key" in item:
            item["filter_key"] = item["key"]
        item.setdefault("status", "active")
        item["payload"] = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        items.append(item)
    next_payload = payload
    next_payload["items"] = items
    next_payload["refinement"] = _refinement(payload, parsed)
    if not items and not next_payload["refinement"]["needs_more_input"] and not errors:
        errors.append({"type": "no_valid_items", "message": "저장할 수 있는 메인 플로우 필터 후보가 생성되지 않았습니다."})
    next_payload.setdefault("errors", []).extend(errors)
    next_payload.setdefault("trace", {})["generated_items_preview"] = [{"key": item.get("filter_key", ""), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    return next_payload


# 함수 설명: `_refinement()`는 LLM이 반환한 보완 필요 정보와 가정을 저장 전 검수 단계까지 보존합니다.
def _refinement(payload: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(payload.get("refinement")) if isinstance(payload.get("refinement"), dict) else {}
    parsed_refinement = parsed.get("refinement") if isinstance(parsed.get("refinement"), dict) else {}
    missing = _string_list(parsed.get("missing_information")) or _string_list(parsed_refinement.get("missing_information"))
    assumptions = _string_list(parsed.get("assumptions")) or _string_list(parsed_refinement.get("assumptions"))
    current.update(
        {
            "refined_text": str(parsed_refinement.get("refined_text") or current.get("refined_text") or ""),
            "needs_more_input": _truthy(parsed.get("needs_more_input")) or _truthy(parsed_refinement.get("needs_more_input")) or bool(missing),
            "missing_information": missing,
            "assumptions": assumptions,
        }
    )
    return current


# 함수 설명: `_string_list()`는 보완 질문과 가정을 빈 문자열 없이 표준 문자열 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


# 함수 설명: `_truthy()`는 LLM의 bool 또는 문자열 값을 안전한 참/거짓 값으로 해석합니다.
def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_json()`는 Message·dict·JSON 문자열에서 Markdown fence를 제거하고 JSON object를 안전하게 추출합니다.
def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = str(value or "")
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MainFlowFilterSavingResultNormalizer(Component):
    display_name = "04 메인 플로우 필터 등록 결과 정규화기"
    description = "메인 플로우 필터 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="llm_response", display_name="LLM 응답", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_authoring(getattr(self, "payload", None), getattr(self, "llm_response", "")))
