from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

ALLOWED_SECTIONS = {"process_groups", "product_terms", "quantity_terms", "metric_terms", "analysis_recipes", "status_terms", "product_key_columns", "pandas_function_cases"}


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
        if "gbn" in item and "section" not in item:
            item["section"] = item["gbn"]
        item.setdefault("status", "active")
        item["payload"] = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("section") not in ALLOWED_SECTIONS:
            errors.append({"type": "unsupported_section", "message": f"지원하지 않는 domain section입니다: {item.get('section')}", "index": index})
        items.append(item)
    next_payload = deepcopy(payload)
    next_payload["items"] = items
    next_payload.setdefault("errors", []).extend(errors)
    next_payload.setdefault("trace", {})["generated_items_preview"] = [{"key": _key(item), "payload_keys": sorted(item.get("payload", {}).keys())} for item in items]
    return next_payload


def _key(item: dict[str, Any]) -> str:
    return f"{item.get('section', '')}:{item.get('key', '')}" if item.get("section") else str(item.get("key", ""))


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


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


class DomainSavingResultNormalizer(Component):
    display_name = "04 도메인 등록 결과 정규화기"
    description = "도메인 등록 JSON 응답을 저장 후보 항목 목록으로 정규화합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="llm_response", display_name="LLM 응답", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=normalize_authoring(getattr(self, "payload", None), getattr(self, "llm_response", "")))
