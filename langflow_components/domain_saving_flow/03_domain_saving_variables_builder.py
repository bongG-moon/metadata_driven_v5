# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 도메인 등록 변수 생성기
# 역할: 원문 또는 이미 정제된 텍스트를 한 번의 metadata 추출 Agent에 전달합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 등록 원문 (source_text)
# 처리 흐름: 정제된 원문을 우선해 도메인 authoring LLM에 필요한 텍스트 하나만 전달합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    refinement = payload.get("refinement", {}) if isinstance(payload.get("refinement"), dict) else {}
    request = payload.get("request", {}) if isinstance(payload.get("request"), dict) else {}
    return {"source_text": str(refinement.get("refined_text") or request.get("raw_text") or ""), "metadata_type": "domain"}


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class DomainSavingVariablesBuilder(Component):
    display_name = "03 도메인 등록 변수 생성기"
    description = "원문 또는 이미 정제된 텍스트를 한 번의 metadata 추출 Agent에 전달합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="source_text", display_name="등록 원문", method="build_source_text", types=["Message"]),
    ]

    # Langflow 출력 함수: '등록 원문 (source_text)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_source_text(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["source_text"])
