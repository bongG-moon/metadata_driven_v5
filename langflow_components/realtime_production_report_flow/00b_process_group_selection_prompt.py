# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00B 실시간 생산 공정그룹 선택 Prompt
# 역할: 사용자 질문과 허용 공정그룹 카탈로그를 LLM이 단일 그룹 판별에만 쓰도록 제한된 Prompt로 만듭니다.
# 주요 입력: 사용자 질문 Message, domain.process_group.catalog.v1 Data
# 주요 출력: Language Model 입력 Message
# 처리 흐름: 질문 추출 -> 후보 최소화 -> JSON 출력 계약과 금지 규칙 결합
# 유지보수 포인트: 집계·판정 규칙은 Prompt에 넣지 않고 공정그룹 선택만 맡깁니다.
# =============================================================================

from __future__ import annotations

import json
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, Output
from lfx.schema.message import Message


MAX_QUESTION_CHARS = 4_000


# 함수 설명: `_text()`는 Message 또는 일반 입력에서 사용자 질문 문자열을 안전하게 꺼냅니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        candidate = getattr(value, "text", None) or data.get("text")
        if candidate is not None:
            return str(candidate).strip()
    return str(getattr(value, "text", value) or "").strip()


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 공정그룹 카탈로그 페이로드를 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return raw if isinstance(raw, dict) else {}


# 함수 설명: `build_process_group_selection_prompt()`는 사용자 질문과 허용 카탈로그를 단일 그룹 선택 JSON Prompt로 결합합니다.
def build_process_group_selection_prompt(question_value: Any, catalog_value: Any) -> str:
    question = _text(question_value)[:MAX_QUESTION_CHARS]
    catalog = _payload(catalog_value)
    candidates = [
        {
            "key": str(group.get("key") or ""),
            "display_name": str(group.get("display_name") or ""),
            "aliases": list(group.get("aliases") or []),
            "field": str(group.get("field") or "OPER_NAME"),
            "processes": list(group.get("processes") or []),
        }
        for group in catalog.get("process_groups", [])
        if isinstance(group, dict)
    ]
    return f"""너는 실시간 생산 분석 Flow 앞단의 공정그룹 선택기다.
업무는 사용자 질문이 아래 허용 공정그룹 중 정확히 하나를 명시하거나 식별하는지 판별하는 것뿐이다.

[사용자 질문]
{question}

[허용 공정그룹 카탈로그]
{json.dumps(candidates, ensure_ascii=False, indent=2)}

[판별 규칙]
1. 출력은 JSON object 하나이며 Markdown code fence와 설명 문장을 붙이지 않는다.
2. process_group_key에는 카탈로그의 key 하나만 쓴다. 세부 공정명은 출력하지 않는다.
3. key, display_name, alias 또는 processes의 세부 공정명이 질문에 실제로 나타나 한 그룹을 식별할 때만 status=selected다.
4. "실시간 생산", "Report", "공정 분석" 같은 일반 표현만으로 그룹을 추측하지 않는다.
5. 그룹 증거가 없으면 status=missing, 서로 다른 그룹 증거가 둘 이상이면 status=ambiguous다.
6. "전체 공정", "모든 공정"은 단일 그룹 요청이 아니므로 status=ambiguous다.
7. 질문에 없는 그룹을 추천하거나 기본값으로 선택하지 않는다.

[출력 계약]
{{
  "status": "selected|missing|ambiguous",
  "process_group_key": "selected일 때만 허용 key, 아니면 빈 문자열",
  "reason": "짧은 한국어 근거",
  "evidence": ["질문에서 확인한 원문 표현"]
}}"""


# Langflow 컴포넌트 클래스: 공정그룹 선택만 수행하는 제한된 Language Model 입력 Message를 만듭니다.
class RealtimeProductionProcessGroupSelectionPrompt(Component):
    display_name = "00B 실시간 생산 공정그룹 선택 Prompt"
    description = "질문과 Domain 공정그룹 허용목록을 LLM의 단일 그룹 선택 Prompt로 만듭니다."
    name = "RealtimeProductionProcessGroupSelectionPrompt"
    icon = "MessageSquareCode"
    inputs = [
        HandleInput(
            name="question",
            display_name="사용자 질문",
            input_types=["Message"],
            required=True,
        ),
        DataInput(
            name="process_group_catalog",
            display_name="공정그룹 카탈로그",
            required=True,
        ),
    ]
    outputs = [
        Output(
            name="prompt",
            display_name="공정그룹 선택 Prompt",
            method="build_prompt",
            types=["Message"],
        )
    ]

    # 함수 설명: `build_prompt()`는 연결된 질문과 카탈로그로 Prompt Message를 생성합니다.
    def build_prompt(self) -> Message:
        prompt = build_process_group_selection_prompt(
            getattr(self, "question", None),
            getattr(self, "process_group_catalog", None),
        )
        self.status = prompt
        return Message(text=prompt)
