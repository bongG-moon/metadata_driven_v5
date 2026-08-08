# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 Continuation 분석 요청 로더
# 역할: 일반 질문과 구조화된 continuation 입력을 하나의 분석 요청 payload로 만듭니다.
# 주요 입력: 질문, 이전 상태, 상위 결과 참조, continuation 참조와 공개 계약
# 주요 출력: 세션·기준일·orchestration 정보가 정리된 Data payload
# 처리 흐름: 세션을 확정하고 공개 계약의 참조 일관성을 검증한 뒤 재개 표시를 기록합니다.
# 유지보수 포인트: 입력 계약을 검증할 뿐 dataset·컬럼·업무 조건을 추론하거나 추가하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, MessageTextInput, MultilineInput, Output
from lfx.schema.data import Data

KOREA_ZONE_NAME = "Asia/Seoul"
MAX_PUBLIC_CONTINUATION_BYTES = 4 * 1024


# 내부 연동 도우미 클래스: 공개 continuation 입력 계약의 구조화된 차단 사유와 실제 크기를 보존합니다.
class ContinuationContractError(ValueError):
    # 함수 설명: 차단 유형과 측정된 계약 byte 크기를 ValueError 인스턴스에 보존합니다.
    def __init__(self, error_type: str, message: str, contract_bytes: int = 0):
        super().__init__(message)
        self.error_type = error_type
        self.contract_bytes = contract_bytes


# 주요 함수: 질문과 선택 입력을 공통 분석 payload 및 명시적 재개 계약으로 구성합니다.
def build_request(
    question: Any,
    previous_state_value: Any = None,
    session_id: str = "",
    upstream_result_ref: Any = "",
    continuation_ref: Any = "",
    continuation_contract: Any = "",
    skip_intermediate_answer: Any = False,
) -> dict[str, Any]:
    """Build the common payload and validate explicit continuation inputs."""

    resolved_session_id = _resolve_session_id(previous_state_value, session_id)
    payload = {
        "request": {
            "question": str(question or ""),
            "session_id": resolved_session_id,
            "reference_date": _korea_today(),
        },
        "state": _payload(previous_state_value),
        "metadata_refs": [],
        "intent_plan": {},
        "source_results": [],
        "runtime_sources": {},
        "analysis": {},
        "data": {},
        "answer_message": "",
        "trace": {"warnings": [], "errors": [], "inspection": {}},
    }
    explicit_ref = _ref_id(upstream_result_ref)
    continuation_ref_text = str(continuation_ref or "").strip()
    try:
        contract = _parse_contract(continuation_contract)
    except ContinuationContractError as exc:
        error = {
            "type": exc.error_type,
            "message": str(exc),
            "contract_bytes": exc.contract_bytes,
            "max_contract_bytes": MAX_PUBLIC_CONTINUATION_BYTES,
        }
        payload["analysis"] = {"status": "blocked", "error": deepcopy(error)}
        payload["orchestration"] = {
            "explicit": bool(explicit_ref or continuation_ref_text or _has_value(continuation_contract)),
            "status": "blocked",
            "upstream_result_ref": explicit_ref,
            "source_alias": "upstream_result",
            "error": deepcopy(error),
        }
        payload["trace"]["errors"].append(deepcopy(error))
        payload["trace"]["inspection"]["continuation_contract_validation"] = {
            "status": "blocked",
            **deepcopy(error),
        }
        return payload
    if contract:
        embedded_ref = str(contract.get("continuation_ref") or "").strip()
        if continuation_ref_text and embedded_ref and continuation_ref_text != embedded_ref:
            raise ValueError("continuation_ref와 continuation_contract가 일치하지 않습니다.")
        continuation_ref_text = continuation_ref_text or embedded_ref
        if not explicit_ref:
            raise ValueError("continuation 재개에는 upstream_result_ref가 필요합니다.")
        payload["request"]["continuation"] = {
            "continuation_ref": continuation_ref_text,
            "continuation_contract": contract,
            "skip_intermediate_answer": _bool(skip_intermediate_answer, False),
        }
        inspection = payload["trace"]["inspection"]
        inspection["continuation_intent"] = {
            "status": "resume_requested",
            "continuation_ref": continuation_ref_text,
            "plan_id": str(contract.get("plan_id") or ""),
            "plan_hash": str(contract.get("plan_hash") or ""),
            "next_stage_index": contract.get("next_stage_index"),
            "intent_llm_skipped": True,
        }
        inspection["llm_calls"] = {"intent_skipped": True}
    if explicit_ref:
        payload["orchestration"] = {
            "explicit": True,
            "status": "pending",
            "upstream_result_ref": explicit_ref,
            "source_alias": "upstream_result",
        }
    return payload


# 함수 설명: dict 또는 JSON 문자열로 전달된 공개 continuation 계약을 안전하게 해석합니다.
def _parse_contract(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        candidate = data.get("continuation_contract", data)
        if candidate in (None, ""):
            return {}
        if not isinstance(candidate, dict):
            raise ContinuationContractError(
                "continuation_contract_invalid_type",
                "continuation_contract는 JSON object여야 합니다.",
            )
        encoded = _canonical_contract_bytes(candidate)
        _enforce_contract_size(len(encoded))
        return deepcopy(candidate)
    raw_text = getattr(value, "text", data)
    if raw_text in (None, ""):
        return {}
    if not isinstance(raw_text, str):
        raise ContinuationContractError(
            "continuation_contract_invalid_type",
            "continuation_contract는 JSON object 또는 JSON 문자열이어야 합니다.",
        )
    text = raw_text.strip()
    if not text:
        return {}
    raw_bytes = len(text.encode("utf-8"))
    _enforce_contract_size(raw_bytes)
    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ContinuationContractError(
            "continuation_contract_invalid_json",
            f"continuation_contract JSON을 해석할 수 없습니다: {exc}",
            raw_bytes,
        ) from exc
    if not isinstance(parsed, dict):
        raise ContinuationContractError(
            "continuation_contract_invalid_type",
            "continuation_contract는 JSON object여야 합니다.",
            raw_bytes,
        )
    canonical_bytes = _canonical_contract_bytes(parsed)
    _enforce_contract_size(len(canonical_bytes))
    return parsed


# 함수 설명: dict 공개 계약을 키 순서와 공백에 무관한 UTF-8 canonical JSON bytes로 직렬화합니다.
def _canonical_contract_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContinuationContractError(
            "continuation_contract_invalid_type",
            f"continuation_contract를 JSON object로 직렬화할 수 없습니다: {exc}",
        ) from exc


# 함수 설명: standalone Flow14 입력에서도 공개 continuation 계약의 4 KiB 상한을 동일하게 강제합니다.
def _enforce_contract_size(contract_bytes: int) -> None:
    if contract_bytes <= MAX_PUBLIC_CONTINUATION_BYTES:
        return
    raise ContinuationContractError(
        "continuation_contract_too_large",
        "continuation_contract가 4 KiB 제한을 초과했습니다.",
        contract_bytes,
    )


# 함수 설명: Langflow 입력 wrapper를 포함한 값이 실제로 제공되었는지 차단 trace용으로 판별합니다.
def _has_value(value: Any) -> bool:
    data = getattr(value, "data", value)
    if data is None:
        return False
    if isinstance(data, str):
        return bool(data.strip())
    if isinstance(data, (dict, list, tuple, set)):
        return bool(data)
    return True


# 함수 설명: 문자열·Data 참조에서 저장 결과 ID만 추출합니다.
def _ref_id(value: Any) -> str:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        return str(data.get("ref_id") or data.get("data_ref") or data.get("_id") or "").strip()
    return str(data or "").strip()


# 함수 설명: runtime 세션, 전달 metadata, 이전 상태 순으로 session_id를 확정합니다.
def _resolve_session_id(previous_state_value: Any = None, session_id: Any = "") -> str:
    text = str(session_id or "").strip()
    if text:
        return text
    metadata_session = _metadata_session_id(previous_state_value)
    if metadata_session:
        return metadata_session
    state = _payload(previous_state_value)
    for key in ("session_id", "conversation_id", "thread_id"):
        if state.get(key):
            return str(state[key])
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    return str(request.get("session_id") or "")


# 함수 설명: Langflow 메시지와 Data의 여러 metadata 위치에서 session_id를 찾습니다.
def _metadata_session_id(value: Any) -> str:
    for attribute in ("a2a_metadata", "framework2_metadata", "metadata"):
        result = _session_id_from_mapping(getattr(value, attribute, None))
        if result:
            return result
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        for key in ("metadata", "a2a_metadata", "framework2_metadata"):
            result = _session_id_from_mapping(data.get(key))
            if result:
                return result
    return ""


# 함수 설명: 다양한 외부 세션 키 표현을 하나의 session_id로 해석합니다.
def _session_id_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("session_id", "sessionId", "conversation_id", "thread_id"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    return ""


# 함수 설명: Langflow graph 또는 component runtime이 가진 현재 session_id를 읽습니다.
def _runtime_session_id(component: Any) -> str:
    graph = getattr(component, "graph", None)
    graph_session = str(getattr(graph, "session_id", "") or "").strip()
    return graph_session or str(getattr(component, "_session_id", "") or "").strip()


# 함수 설명: 분석 요청 기준일로 사용할 한국 시간의 YYYYMMDD 값을 만듭니다.
def _korea_today() -> str:
    return datetime.now(_korea_timezone()).strftime("%Y%m%d")


# 함수 설명: zoneinfo를 우선 사용하고 불가능하면 고정 KST timezone을 반환합니다.
def _korea_timezone():
    try:
        return import_module("zoneinfo").ZoneInfo(KOREA_ZONE_NAME)
    except Exception:
        return timezone(timedelta(hours=9), "KST")


# 함수 설명: Langflow Data 또는 일반 dict 입력을 독립 복사본으로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: bool과 일반 문자열 입력을 명시적인 참·거짓 값으로 변환합니다.
def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


# Langflow 컴포넌트 클래스: 네 개의 공개 재개 포트를 포함한 요청 로더를 캔버스에 제공합니다.
class ContinuationAnalysisRequestLoader(Component):
    display_name = "00 Continuation 분석 요청 로더"
    description = "일반 질문 또는 구조화된 2단계 continuation 계약을 분석 payload로 변환합니다."
    inputs = [
        MessageTextInput(name="question", display_name="사용자 질문", required=True, tool_mode=True),
        MessageTextInput(
            name="upstream_result_ref",
            display_name="상위 결과 참조",
            required=False,
            tool_mode=True,
        ),
        MessageTextInput(
            name="continuation_ref",
            display_name="Continuation 참조",
            required=False,
            tool_mode=True,
        ),
        MultilineInput(
            name="continuation_contract",
            display_name="Continuation 계약 JSON",
            required=False,
            advanced=True,
        ),
        BoolInput(
            name="skip_intermediate_answer",
            display_name="중간 답변 생략",
            value=False,
            required=False,
            advanced=True,
        ),
        DataInput(name="previous_state", display_name="이전 상태", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: 정규화된 continuation 분석 요청을 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(
            data=build_request(
                getattr(self, "question", ""),
                getattr(self, "previous_state", None),
                session_id=_runtime_session_id(self),
                upstream_result_ref=getattr(self, "upstream_result_ref", ""),
                continuation_ref=getattr(self, "continuation_ref", ""),
                continuation_contract=getattr(self, "continuation_contract", ""),
                skip_intermediate_answer=getattr(self, "skip_intermediate_answer", False),
            )
        )
