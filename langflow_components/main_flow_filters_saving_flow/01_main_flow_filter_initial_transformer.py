# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 메인 플로우 필터 초기 변환기
# 역할: 등록 원문을 읽기 쉬운 정제안으로만 변환하고, 기존 04 저장 경로는 그대로 유지합니다.
# 주요 입력: 페이로드 (payload) · 필수, 초기 변환 LLM 응답 (llm_response) · 선택
# 주요 출력: 정제안이 포함된 페이로드 (payload_out)
# 처리 흐름: filter_key·operator 같은 사용자가 직접 쓴 필터 계약은 원문을 보존하고,
#             일반 설명만 LLM 정제안으로 바꿉니다.
# 유지보수 포인트: 이 노드는 후보 검증·차단·metadata 조회를 하지 않습니다. 실제 저장 계약은
#                 기존 04 결과 정규화기와 07 Writer가 계속 결정합니다.
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


# filter_key와 실행 필드가 원문에 직접 선언된 경우 LLM이 문장만 다듬는 과정에서
# 방향이나 명칭을 바꾸지 않도록 원문 자체를 다음 기존 저장 경로에 전달한다.
STRUCTURED_FILTER_CONTRACT_PATTERN = re.compile(
    r"(?is)\b(?:filter[\s_]*key|column[\s_]*candidates|semantic[\s_]*role|"
    r"value[\s_]*type|value[\s_]*shape|operator|aliases|display[\s_]*name)\b"
)


def transform_initial_text(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    """Attach a best-effort wording refinement without creating a save gate.

    The legacy Main Flow Filter pipeline already owns candidate validation and
    persistence.  This function only fills ``payload.refinement.refined_text``;
    it never adds errors or turns on ``needs_more_input``.
    """

    payload = _payload(payload_value)
    request = _dict(payload.get("request"))
    raw_text = str(request.get("raw_text") or "")
    parsed, response_format = _parse_response(llm_response)
    candidate = _candidate_text(parsed, llm_response, response_format)
    is_structured_contract = bool(STRUCTURED_FILTER_CONTRACT_PATTERN.search(raw_text))

    if is_structured_contract:
        selected_text = raw_text
        status = "preserved_structured_contract"
        reason = "filter_contract_fields_present"
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
            # This stage must never suppress a candidate that legacy 04 could
            # otherwise normalize and save.
            "needs_more_input": False,
            "missing_information": [],
            "assumptions": _string_list(refinement.get("assumptions")),
        }
    )
    payload["refinement"] = refinement

    trace = _dict(payload.get("trace"))
    trace["initial_transform"] = {
        "version": "main_flow_filter_initial_transform_v1",
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
    return str(_dict(payload.get("request")).get("raw_text") or "")


def _candidate_text(parsed: dict[str, Any], raw_response: Any, response_format: str) -> str:
    if parsed:
        nested = _dict(parsed.get("refinement"))
        value = parsed.get("refined_text") or nested.get("refined_text")
        return value.strip() if isinstance(value, str) and value.strip() else ""
    if response_format != "plain_text":
        return ""
    text = _response_text(raw_response).strip()
    return "" if not text or text.startswith("{") or text.startswith("[") else text


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
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


class MainFlowFilterInitialTransformer(Component):
    display_name = "01 메인 플로우 필터 초기 변환기"
    description = "원문을 정돈하되 filter_key·필터 계약은 원문 그대로 보존합니다."
    inputs = [
        DataInput(name="payload", display_name="등록 요청 페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="초기 변환 LLM 응답", required=False),
    ]
    outputs = [
        Output(name="source_text", display_name="원문 텍스트", method="build_source_text", types=["Message"]),
        Output(name="payload_out", display_name="변환된 페이로드", method="build_payload"),
    ]

    def build_source_text(self) -> Message:
        return Message(text=source_text_from_payload(getattr(self, "payload", None)))

    def build_payload(self) -> Data:
        return Data(data=transform_initial_text(getattr(self, "payload", None), getattr(self, "llm_response", "")))
