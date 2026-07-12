# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03 메타데이터 QA 변수 생성기
# 역할: Prompt Template과 Langflow Agent/LLM에 연결할 메타데이터 QA 변수를 제공합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 사용자 질문 (question), 메타데이터 컨텍스트 JSON (metadata_context_json), 출력 스키마 JSON (output_schema_json)
# 처리 흐름: QA LLM에 전달할 질문, 축약 메타데이터 문맥과 출력 스키마를 각각의 Message로 분리합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.message import Message


# 주요 함수: LLM 프롬프트에 연결할 변수만 선별하고 JSON-safe 문자열 또는 dict로 정리합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_variables(payload_value: Any) -> dict[str, str]:
    payload = _payload(payload_value)
    context = _dict(payload.get("metadata_qa_context"))
    return {
        "question": str(_dict(payload.get("request")).get("question") or ""),
        "metadata_context_json": _json_dumps(context),
        "output_schema_json": _json_dumps(_output_schema()),
    }


# 함수 설명: `_output_schema()`는 LLM이 반드시 따라야 할 JSON 출력 필드와 자료형 계약을 만듭니다.
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


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_json_dumps()`는 datetime·Decimal 같은 값까지 JSON-safe 형태로 바꾼 뒤 문자열로 직렬화합니다.
def _json_dumps(value: Any) -> str:
    return json.dumps(_json_ready(value), ensure_ascii=False, indent=2)


# 함수 설명: `_json_ready()`는 datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.
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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MetadataQaVariablesBuilder(Component):
    display_name = "03 메타데이터 QA 변수 생성기"
    description = "Prompt Template과 Langflow Agent/LLM에 연결할 메타데이터 QA 변수를 제공합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="question", display_name="사용자 질문", method="build_question", types=["Message"], group_outputs=True),
        Output(name="metadata_context_json", display_name="메타데이터 컨텍스트 JSON", method="build_metadata_context", types=["Message"], group_outputs=True),
        Output(name="output_schema_json", display_name="출력 스키마 JSON", method="build_output_schema", types=["Message"], group_outputs=True),
    ]

    # 함수 설명: `_variables_once()`는 세 Prompt output이 동일 QA context를 반복 직렬화하지 않도록 한 번만 계산합니다.
    def _variables_once(self) -> dict[str, Any]:
        payload = getattr(self, "payload", None)
        cache_key = id(payload)
        if getattr(self, "_variables_cache_key", None) != cache_key:
            self._variables_cache_key = cache_key
            self._variables_cache = build_variables(payload)
        return self._variables_cache

    # Langflow 출력 함수: '사용자 질문 (question)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_question(self) -> Message:
        return Message(text=self._variables_once()["question"])

    # Langflow 출력 함수: '메타데이터 컨텍스트 JSON (metadata_context_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_metadata_context(self) -> Message:
        return Message(text=self._variables_once()["metadata_context_json"])

    # Langflow 출력 함수: '출력 스키마 JSON (output_schema_json)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_output_schema(self) -> Message:
        return Message(text=self._variables_once()["output_schema_json"])
