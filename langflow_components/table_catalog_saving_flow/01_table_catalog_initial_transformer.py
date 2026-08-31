# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 테이블 카탈로그 초기 변환기
# 역할: 등록 원문을 읽기 쉬운 정제안으로만 변환하고, 기존 저장 경로는 그대로 유지합니다.
# 주요 입력: 페이로드 (payload) · 필수, 초기 변환 LLM 응답 (llm_response) · 선택
# 주요 출력: 정제안이 포함된 페이로드 (payload_out)
# 처리 흐름: 구조화된 SQL/매핑 계약은 원문을 보존하고, 일반 설명만 LLM 정제안으로 바꿉니다.
# 유지보수 포인트: 이 노드는 후보 검증·차단·metadata 조회를 하지 않습니다. 실제 저장 계약은 기존
#                 04 결과 정규화기와 07 Writer가 계속 결정합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


# query_template/filter_mappings처럼 실행 계약이 직접 적힌 원문은 LLM이 문장만 다듬다가
# 방향이나 SQL 표현을 바꾸면 안 된다. 이 경우에는 원문 자체가 이미 가장 안전한 정제안이다.
STRUCTURED_CONTRACT_PATTERN = re.compile(
    r"(?is)\b(?:query[\s_]*template|filter[\s_]*mappings|required[\s_]*param(?:eter)?[\s_]*mappings|"
    r"standard[\s_]*column[\s_]*aliases|default[\s_]*detail[\s_]*columns)\b|"
    r"\bSELECT\b[\s\S]*?\bFROM\b|"
    r"\b[A-Z][A-Z0-9_]*\s*->\s*[A-Z][A-Z0-9_]*\b",
    re.IGNORECASE,
)


def transform_initial_text(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    """Attach a best-effort initial refinement without introducing a gate.

    The legacy Table Catalog pipeline already accepts ``payload.refinement.refined_text``.
    This function only fills that field.  It never creates ``errors`` or turns on
    ``needs_more_input``; the existing normalizer and writer remain the single
    source of truth for candidate validation and persistence.
    """

    payload = _payload(payload_value)
    request = _dict(payload.get("request"))
    raw_text = str(request.get("raw_text") or "")
    parsed, response_format = _parse_response(llm_response)
    candidate = _candidate_text(parsed, llm_response, response_format)
    is_structured_contract = bool(STRUCTURED_CONTRACT_PATTERN.search(raw_text))

    if is_structured_contract:
        selected_text = raw_text
        status = "preserved_structured_contract"
        reason = "query_or_mapping_contract_present"
    elif candidate:
        selected_text = candidate
        status = "applied"
        reason = "plain_request_refined"
    else:
        selected_text = raw_text
        status = "fallback_raw"
        reason = "usable_refined_text_not_returned"

    refinement = _dict(payload.get("refinement"))
    refinement.update(
        {
            "refined_text": selected_text,
            # Initial wording conversion is advisory only.  It must never
            # suppress a candidate that the legacy path could otherwise save.
            "needs_more_input": False,
            "missing_information": [],
            "assumptions": _string_list(refinement.get("assumptions")),
        }
    )
    payload["refinement"] = refinement

    trace = _dict(payload.get("trace"))
    trace["initial_transform"] = {
        "version": "table_catalog_initial_transform_v1",
        "status": status,
        "reason": reason,
        "input_shape": "structured_contract" if is_structured_contract else "plain_request",
        "llm_response_format": response_format,
        "raw_text_length": len(raw_text),
        "refined_text_length": len(selected_text),
    }
    payload["trace"] = trace
    return payload


def source_text_from_payload(payload_value: Any) -> str:
    """Return the untouched request text for the initial-transform prompt."""

    payload = _payload(payload_value)
    request = _dict(payload.get("request"))
    return str(request.get("raw_text") or "")


def _candidate_text(parsed: dict[str, Any], raw_response: Any, response_format: str) -> str:
    """Get a safe text candidate from the intentionally tiny transform contract."""

    if parsed:
        nested = _dict(parsed.get("refinement"))
        value = parsed.get("refined_text") or nested.get("refined_text")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ""
    if response_format != "plain_text":
        return ""
    text = _response_text(raw_response).strip()
    # A plain response is allowed for provider compatibility.  Empty, fenced
    # JSON-like, and error-only responses are ignored rather than propagated.
    if not text or text.startswith("{") or text.startswith("["):
        return ""
    return text


def _parse_response(value: Any) -> tuple[dict[str, Any], str]:
    if isinstance(value, dict):
        if "refined_text" not in value and isinstance(value.get("text"), str):
            return _parse_response(value["text"])
        return deepcopy(value), "json_object"
    text = _response_text(value).strip()
    if not text:
        return {}, "empty"

    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for candidate in candidates:
        parsed = _json_object(candidate)
        if parsed:
            return parsed, "json_fenced" if fenced and candidate == fenced.group(1) else "json"
    return {}, "plain_text"


def _json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        if start < 0:
            return {}
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        except Exception:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _response_text(value: Any) -> str:
    if hasattr(value, "text"):
        text = getattr(value, "text", "")
        if text is not None:
            return str(text)
    if isinstance(value, list):
        for item in value:
            if hasattr(item, "text"):
                text = getattr(item, "text", "")
                if text is not None and str(text).strip():
                    return str(text)
            if isinstance(item, dict) and str(item.get("type") or "").lower() == "text":
                text = item.get("text")
                if text is not None:
                    return str(text)
    return str(value or "")


def _dict(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


class TableCatalogInitialTransformer(Component):
    display_name = "01 테이블 카탈로그 초기 변환기"
    description = "원문을 정돈하되 SQL·컬럼 매핑 계약은 원문 그대로 보존합니다."
    inputs = [
        DataInput(name="payload", display_name="등록 요청 페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="초기 변환 LLM 응답", required=False),
    ]
    # The transform LLM must see the original text, not a Data serialization.
    # Keeping this as a dedicated Message port also lets the builder connect the
    # prompt before the optional LLM response returns to ``payload_out``.
    outputs = [
        Output(name="source_text", display_name="원문 텍스트", method="build_source_text", types=["Message"]),
        Output(name="payload_out", display_name="변환된 페이로드", method="build_payload"),
    ]

    def build_source_text(self) -> Message:
        return Message(text=source_text_from_payload(getattr(self, "payload", None)))

    def build_payload(self) -> Data:
        return Data(data=transform_initial_text(getattr(self, "payload", None), getattr(self, "llm_response", "")))
