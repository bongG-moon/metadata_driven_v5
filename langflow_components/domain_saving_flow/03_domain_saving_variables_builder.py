from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message

def build_variables(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    refinement = payload.get("refinement", {}) if isinstance(payload.get("refinement"), dict) else {}
    request = payload.get("request", {}) if isinstance(payload.get("request"), dict) else {}
    return {"source_text": str(refinement.get("refined_text") or request.get("raw_text") or ""), "metadata_type": "domain"}


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


class DomainSavingVariablesBuilder(Component):
    display_name = "03 도메인 등록 변수 생성기"
    description = "원문 또는 이미 정제된 텍스트를 한 번의 metadata 추출 Agent에 전달합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="source_text", display_name="등록 원문", method="build_source_text", types=["Message"]),
    ]

    def build_source_text(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["source_text"])

