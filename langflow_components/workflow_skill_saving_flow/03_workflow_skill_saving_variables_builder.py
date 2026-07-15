# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 Workflow Skill 등록 변수 생성기
# 역할: 등록 요청에서 LLM 후보 추출에 필요한 원문만 분리합니다.
# 주요 입력: 페이로드(payload)
# 주요 출력: 등록 원문(source_text)
# 처리 흐름: 보정된 원문이 있으면 우선 사용하고 없으면 최초 사용자 원문을 Prompt Template에 전달합니다.
# 유지보수 포인트: Tool 허용 목록과 출력 스키마는 Prompt 파일에 고정하고 원문 외 전체 페이로드는 LLM에 전달하지 않습니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


# 주요 함수: Workflow Skill 등록 페이로드에서 후보 추출용 원문을 선택합니다.
def build_variables(payload_value: Any) -> dict[str, str]:
    payload = _payload(payload_value)
    refinement = payload.get("refinement") if isinstance(payload.get("refinement"), dict) else {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    return {"source_text": str(refinement.get("refined_text") or request.get("raw_text") or "").strip()}


# 함수 설명: `_payload()`는 Langflow Data 또는 일반 dict 입력에서 원본을 변경하지 않는 dict 값을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return data if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: 등록 원문을 Prompt Template에 연결할 Message 포트로 제공합니다.
class WorkflowSkillSavingVariablesBuilder(Component):
    display_name = "03 Workflow Skill 등록 변수 생성기"
    description = "전체 페이로드 대신 Workflow Skill 등록 원문만 추출 LLM에 전달합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="source_text", display_name="등록 원문", method="build_source_text", types=["Message"])]

    # Langflow 출력 함수: 후보 추출 Prompt에 연결할 등록 원문 Message를 반환합니다.
    def build_source_text(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["source_text"])
