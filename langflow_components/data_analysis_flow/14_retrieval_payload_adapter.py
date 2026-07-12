# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 14 조회 페이로드 어댑터
# 역할: 소스 조회 결과 행을 pandas용 런타임 소스로 옮기고 요약 조회 결과를 유지합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 전체 행은 pandas 실행용 runtime_sources에 두고 LLM에는 schema와 작은 preview만 전달해 토큰 사용량을 줄입니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

# 주요 함수: 조회 행과 LLM용 요약을 분리하는 pandas 실행 직전 페이로드를 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_retrieval_payload(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    next_payload = deepcopy(payload)
    if "_runtime_rows_by_alias" in next_payload:
        next_payload["runtime_sources"] = next_payload.pop("_runtime_rows_by_alias", {})
    return next_payload


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class RetrievalPayloadAdapter(Component):
    display_name = "14 조회 페이로드 어댑터"
    description = "소스 조회 결과 행을 pandas용 런타임 소스로 옮기고 요약 조회 결과를 유지합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_retrieval_payload(getattr(self, "payload", None)))
