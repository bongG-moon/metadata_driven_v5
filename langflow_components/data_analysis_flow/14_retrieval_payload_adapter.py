from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

def build_retrieval_payload(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    next_payload = deepcopy(payload)
    if "_runtime_rows_by_alias" in next_payload:
        next_payload["runtime_sources"] = next_payload.pop("_runtime_rows_by_alias", {})
    return next_payload


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


class RetrievalPayloadAdapter(Component):
    display_name = "14 조회 페이로드 어댑터"
    description = "소스 조회 결과 행을 pandas용 런타임 소스로 옮기고 요약 조회 결과를 유지합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=build_retrieval_payload(getattr(self, "payload", None)))
