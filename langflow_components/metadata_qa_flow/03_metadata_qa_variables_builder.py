from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


def build_variables(payload_value: Any) -> dict[str, str]:
    payload = _payload(payload_value)
    context = _dict(payload.get("metadata_qa_context"))
    return {
        "question": str(_dict(payload.get("request")).get("question") or ""),
        "metadata_context_json": _json_dumps(context),
        "output_schema_json": _json_dumps(_output_schema()),
    }


def _output_schema() -> dict[str, Any]:
    return {
        "answer_type": "string",
        "answer_message": "string",
        "summary": "string",
        "answer_sections": {
            "summary": {"headline": "string", "description": "string"},
            "detail_table": {"title": "string", "columns": ["string"], "row_count": "integer", "row_source": "data.rows"},
            "sql_blocks": [{"label": "string", "sql": "string"}],
            "usage_examples": ["string"],
            "related_items": [{"metadata_type": "string", "key": "string"}],
            "route_hint": {"target_route": "string", "message": "string"},
            "warnings": [{"type": "string", "message": "string"}],
        },
        "table": {"columns": ["string"], "rows": [{"column": "value"}]},
        "source_refs": [{"metadata_type": "string", "key": "string"}],
        "warnings": [{"type": "string", "message": "string"}],
    }


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, indent=2)


def _json_ready(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return None if value != value or value in (float("inf"), -float("inf")) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_ready(item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_ready(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item_value) for item_value in value]
    return str(value)


class MetadataQaVariablesBuilder(Component):
    display_name = "03 메타데이터 QA 변수 생성기"
    description = "Prompt Template과 Langflow Agent/LLM에 연결할 메타데이터 QA 변수를 제공합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="question", display_name="사용자 질문", method="build_question", types=["Message"], group_outputs=True),
        Output(name="metadata_context_json", display_name="메타데이터 컨텍스트 JSON", method="build_metadata_context", types=["Message"], group_outputs=True),
        Output(name="output_schema_json", display_name="출력 스키마 JSON", method="build_output_schema", types=["Message"], group_outputs=True),
    ]

    def build_question(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["question"])

    def build_metadata_context(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["metadata_context_json"])

    def build_output_schema(self) -> Message:
        return Message(text=build_variables(getattr(self, "payload", None))["output_schema_json"])
