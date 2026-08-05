# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: CUBE 스케줄 채팅 메시지 어댑터
# 역할: 구조화 저장 결과의 공개 message만 Chat Output에 전달합니다.
# 주요 입력: MongoDB Writer의 구조화 저장 결과입니다.
# 주요 출력: Langflow 기본 Chat Output에 연결할 Message입니다.
# 유지보수 포인트: 구조화 API 응답과 채팅 표시 경로를 서로 분리합니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


# 함수 설명: Writer 결과에서 사용자에게 공개할 message만 문자열로 추출하고 비정상 입력에는 안전한 기본문을 반환합니다.
def display_message(value: Any) -> str:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return "CUBE 스케줄 처리 결과가 없습니다."
    return str(data.get("message") or "CUBE 스케줄 처리 결과가 없습니다.")


# Langflow 컴포넌트 클래스: 저장 결과의 공개 문구를 한 개의 native Chat Output 경로로 전달하는 standalone 노드입니다.
class CubeScheduleMessageAdapter(Component):
    display_name = "04 CUBE 스케줄 채팅 메시지 어댑터"
    description = "저장 결과의 공개 메시지만 Chat Output으로 전달합니다."
    inputs = [DataInput(name="write_result", display_name="저장 결과", required=True)]
    outputs = [Output(name="message", display_name="채팅 메시지", method="build_message", types=["Message"])]

    # Langflow 출력 함수: 공개 message 문자열을 Langflow Message 객체로 감싸 반환합니다.
    def build_message(self) -> Message:
        return Message(text=display_message(getattr(self, "write_result", None)))
