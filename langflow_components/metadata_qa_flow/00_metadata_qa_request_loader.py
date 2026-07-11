from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


def build_request(question: Any, previous_state_value: Any = None) -> dict[str, Any]:
    text = str(question or "").strip()
    errors = [] if text else [{"type": "empty_question", "message": "메타데이터 QA 질문이 비어 있습니다."}]
    return {
        "request": {"question": text},
        "state": _payload(previous_state_value),
        "metadata_route": {"route": "metadata_qa", "status": "ready" if text else "warning"},
        "trace": {
            "warnings": [],
            "errors": errors,
            "inspection": {
                "metadata_qa_request": {
                    "stage": "00_metadata_qa_request_loader",
                    "status": "ok" if text else "warning",
                }
            },
        },
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


class MetadataQaRequestLoader(Component):
    display_name = "00 메타데이터 QA 요청 로더"
    description = "메타데이터 조회/설명 질문을 읽기 전용 QA 페이로드로 변환합니다."
    inputs = [
        MessageTextInput(name="question", display_name="사용자 질문", required=True, tool_mode=True),
        DataInput(name="previous_state", display_name="이전 상태", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        return Data(data=build_request(getattr(self, "question", ""), getattr(self, "previous_state", None)))
