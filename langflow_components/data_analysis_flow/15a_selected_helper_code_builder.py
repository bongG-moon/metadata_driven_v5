# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 15A 선택 helper 코드 생성기
# 역할: 전체 helper library에서 intent가 실제 선택한 standalone 함수 정의만 pandas prompt로 전달합니다.
# 주요 입력: Function Case 선택 JSON (function_case_selection_json) · 필수, 전체 helper library (helper_library)
# 주요 출력: 선택 helper 코드 (selected_helper_code)
# 처리 흐름: 전체 helper 라이브러리에서 의도 계획이 선택한 함수 정의만 찾아 코드 생성 프롬프트에 전달합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

import ast
import json
import re
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message


# 주요 함수: 선택된 function case에 해당하는 helper 함수 코드만 추출합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_selected_helper_code(selection_value: Any, helper_library_value: Any = "") -> str:
    selection = _json(selection_value)
    library = _text(helper_library_value)
    names = _selected_names(selection)
    if not names or not library.strip():
        return ""
    try:
        tree = ast.parse(library)
    except SyntaxError:
        return ""
    lines = library.replace("\r\n", "\n").splitlines()
    blocks: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in names:
            continue
        start = max(0, int(node.lineno) - 1)
        end = int(getattr(node, "end_lineno", node.lineno))
        blocks.append("\n".join(lines[start:end]).strip())
    return "\n\n".join(block for block in blocks if block)


def _selected_names(selection: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("available_helpers", "selected_cases", "selected_steps"):
        values = selection.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            name = str(item.get("function_name") or item.get("helper_name") or "").strip()
            if name.isidentifier() and name not in result:
                result.append(name)
    return result


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = _text(value).strip()
    if not text:
        return {}
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        for key in ("text", "content", "message", "output"):
            if isinstance(data.get(key), str):
                return data[key]
    return str(value)


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class SelectedHelperCodeBuilder(Component):
    display_name = "15A 선택 helper 코드 생성기"
    description = "전체 helper library에서 intent가 실제 선택한 standalone 함수 정의만 pandas prompt로 전달합니다."
    inputs = [
        MessageTextInput(name="function_case_selection_json", display_name="Function Case 선택 JSON", required=True),
        MessageTextInput(name="helper_library", display_name="전체 helper library", required=False),
    ]
    outputs = [Output(name="selected_helper_code", display_name="선택 helper 코드", method="build_code", types=["Message"])]

    # Langflow 출력 함수: '선택 helper 코드 (selected_helper_code)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_code(self) -> Message:
        return Message(
            text=build_selected_helper_code(
                getattr(self, "function_case_selection_json", ""),
                getattr(self, "helper_library", ""),
            )
        )
