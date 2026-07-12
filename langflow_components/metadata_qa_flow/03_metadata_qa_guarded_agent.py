# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 메타데이터 QA 조건부 Agent
# 역할: 결정론적 QA와 빈 질문에서는 모델 호출을 생략하고, 자유 서술 QA에서는 기존 Langflow Agent 동작을 그대로 수행합니다.
# 주요 입력: 기존 Agent 입력 전체, 실행 제어 페이로드(control_payload)
# 주요 출력: Agent 응답(response)
# 처리 흐름: `metadata_qa_context.llm_control.skip`을 먼저 검사하고 true면 `get_agent_requirements()` 전에 빈 Message를 반환합니다.
# 유지보수 포인트: Langflow 1.8.2의 AgentComponent를 상속하므로 provider/model/tool 계약을 유지하며 별도 branch/merger를 만들지 않습니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.components.models_and_agents.agent import AgentComponent
from lfx.custom.custom_component.component import Component  # noqa: F401 - repository compatibility contract
from lfx.io import DataInput
from lfx.schema.message import Message


# 주요 함수: 현재 Metadata QA payload가 Agent/LLM 호출을 생략해야 하는지 판정합니다.
def should_skip_agent(control_payload_value: Any) -> bool:
    payload = _payload(control_payload_value)
    question = str(_dict(payload.get("request")).get("question") or "").strip()
    control = _dict(_dict(payload.get("metadata_qa_context")).get("llm_control"))
    return not question or bool(control.get("skip"))


# 함수 설명: `_skip_reason()`은 status/trace에서 확인할 수 있도록 현재 skip 사유를 반환합니다.
def _skip_reason(control_payload_value: Any) -> str:
    payload = _payload(control_payload_value)
    control = _dict(_dict(payload.get("metadata_qa_context")).get("llm_control"))
    return str(control.get("reason") or ("empty_question" if not str(_dict(payload.get("request")).get("question") or "").strip() else "deterministic_answer_mode"))


# 함수 설명: `_payload()`는 guard 판정에 필요한 원본 dict 참조만 읽어 대용량 metadata payload 복사를 피합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return data if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력이 dict가 아니면 빈 dict를 반환해 후속 key 접근을 안전하게 합니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# Langflow 컴포넌트 클래스: 기존 AgentComponent의 provider/config와 성공 경로를 그대로 재사용합니다.
class MetadataQaGuardedAgent(AgentComponent):
    display_name = "03 메타데이터 QA 조건부 Agent"
    description = "결정론적 QA는 모델 호출을 생략하고 자유 서술 QA만 기존 Agent로 실행합니다."
    inputs = [
        *AgentComponent.inputs,
        DataInput(name="control_payload", display_name="실행 제어 페이로드", required=True),
    ]

    # Langflow 출력 함수: skip이면 부모 Agent 초기화 전에 종료하고, 아니면 기존 Agent 구현을 그대로 호출합니다.
    async def message_response(self) -> Message:
        if should_skip_agent(getattr(self, "control_payload", None)):
            reason = _skip_reason(getattr(self, "control_payload", None))
            self.status = f"LLM skipped: {reason}"
            self._agent_result = None
            return Message(text="")
        self.status = "LLM enabled: free-form metadata QA"
        return await super().message_response()
