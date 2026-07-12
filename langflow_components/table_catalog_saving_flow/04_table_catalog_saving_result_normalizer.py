# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04 테이블 카탈로그 등록 결과 정규화기
# 역할: 테이블 카탈로그 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다.
# 주요 입력: 페이로드 (payload) · 필수, LLM 응답 (llm_response) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: Markdown code fence를 제거하고 LLM JSON의 호환 key를 테이블 카탈로그 저장 스키마로 정규화합니다.
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
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append({"type": "invalid_item", "message": f"items[{index}]가 object가 아닙니다."})
            continue
        item = deepcopy(raw)
        if "dataset_key" not in item and "key" in item:
            item["dataset_key"] = item["key"]
        item.setdefault("status", "active")
        item["payload"] = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        source_config = item["payload"].get("source_config") if isinstance(item["payload"].get("source_config"), dict) else {}
        for key in ("sql", "query", "oracle_sql", "query_template"):
            if key in item["payload"] and "query_template" not in source_config:
                source_config["query_template"] = item["payload"].pop(key)
        if source_config:
            item["payload"]["source_config"] = source_config
        items.append(item)
    next_payload = deepcopy(payload)
    next_payload["items"] = items
    next_payload.setdefault("errors", []).extend(errors)
    next_payload.setdefault("trace", {})["generated_items_preview"] = [{"key": item.get("dataset_key", ""), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    return next_payload


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
class TableCatalogSavingResultNormalizer(Component):
    display_name = "04 테이블 카탈로그 등록 결과 정규화기"
    description = "테이블 카탈로그 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="llm_response", display_name="LLM 응답", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_authoring(getattr(self, "payload", None), getattr(self, "llm_response", "")))
