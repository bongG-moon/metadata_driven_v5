# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 14B 조회 실패 차단 Agent
# 역할: 필수 조회 실패 시 모델을 호출하지 않고, 정상 경로에서는 Langflow 1.8.2 Agent 동작을 그대로 실행합니다.
# 주요 입력: 기존 Agent 전체 입력 + 실행 제어 페이로드 (control_payload)
# 주요 출력: 기존 Agent Response
# 처리 흐름: execution_gate.status가 blocked면 빈 Message를 즉시 반환하고, 아니면 AgentComponent.message_response를 그대로 호출합니다.
# 유지보수 포인트: provider/model/tool/history 계약을 재구현하지 않고 공식 AgentComponent를 상속하므로 standalone provider 설정을 보존합니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.components.models_and_agents.agent import AgentComponent
from lfx.custom.custom_component.component import Component  # noqa: F401 - standalone component import contract
from lfx.io import DataInput
from lfx.schema.message import Message


# 함수 설명: `_execution_blocked()`는 Data 또는 dict payload의 execution_gate 상태만 확인합니다.
def _execution_blocked(value: Any) -> bool:
    payload = getattr(value, "data", value)
    if not isinstance(payload, dict):
        return False
    gate = payload.get("execution_gate") if isinstance(payload.get("execution_gate"), dict) else {}
    return str(gate.get("status") or "").strip().lower() == "blocked"


# Langflow 컴포넌트 클래스: Langflow 1.8.2 공식 Agent를 그대로 상속하고 실패 시점의 모델 호출만 차단합니다.
class RetrievalGuardedAgent(AgentComponent):
    display_name = "조회 실패 차단 Agent"
    description = "필수 조회 실패 시 LLM 호출을 생략하고 정상 요청은 기존 Agent 계약으로 처리합니다."
    inputs = [
        *AgentComponent.inputs,
        DataInput(name="control_payload", display_name="실행 제어 페이로드", required=True),
    ]

    # Langflow 출력 함수: 정상 경로는 super()를 그대로 호출하고 blocked 경로만 빈 Message로 종료합니다.
    async def message_response(self) -> Message:
        if _execution_blocked(getattr(self, "control_payload", None)):
            self.status = "필수 조회 실패로 Agent LLM 호출을 생략했습니다."
            return Message(text="")
        return await super().message_response()
