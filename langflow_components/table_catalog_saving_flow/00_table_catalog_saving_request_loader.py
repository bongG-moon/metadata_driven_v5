# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 테이블 카탈로그 등록 요청 로더
# 역할: 자연어 테이블 카탈로그 등록 요청을 시작합니다. 기본값은 드라이런입니다.
# 주요 입력: 원문 텍스트 (raw_text) · 필수, 중복 처리 방식 (duplicate_action), 드라이런 (dry_run)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 자연어 테이블 카탈로그 등록 요청을 duplicate action과 기본 dry-run이 포함된 안전한 표준 페이로드로 초기화합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, MessageTextInput, Output
from lfx.schema.data import Data

ALLOWED_DUPLICATE_ACTIONS = {"merge", "replace", "skip", "create_new"}


# 주요 함수: 사용자 입력과 이전 상태를 후속 노드가 공유할 표준 요청 dict로 변환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_request(raw_text: Any, duplicate_action: str = "skip", dry_run: Any = True) -> dict[str, Any]:
    action = _duplicate_action(duplicate_action)
    return {
        "metadata_type": "table_catalog",
        "request": {"raw_text": str(raw_text or ""), "duplicate_action": action, "dry_run": _bool(dry_run, True)},
        "refinement": {"refined_text": "", "needs_more_input": False, "missing_information": [], "assumptions": []},
        "items": [],
        "existing_matches": [],
        "conflict_warnings": [],
        "duplicate_decision": {"action": action, "target_key": ""},
        "review": {},
        "write_result": {},
        "trace": {"raw_text_preview": str(raw_text or "")[:500], "generated_items_preview": []},
        "errors": [] if str(raw_text or "").strip() else [{"type": "empty_raw_text", "message": "등록할 자연어 원문이 비어 있습니다."}],
        "warnings": [],
    }


# 함수 설명: `_duplicate_action()`는 요청에 지정된 skip/merge/replace/create_new 중복 처리 정책을 안전한 기본값과 함께 해석합니다.
def _duplicate_action(value: Any) -> str:
    action = str(value or "skip").strip().lower()
    return action if action in ALLOWED_DUPLICATE_ACTIONS else "skip"


# 함수 설명: `_bool()`는 문자열·숫자·불리언 표기를 일관된 bool 값으로 해석합니다.
def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TableCatalogSavingRequestLoader(Component):
    display_name = "00 테이블 카탈로그 등록 요청 로더"
    description = "자연어 테이블 카탈로그 등록 요청을 시작합니다. 기본값은 드라이런입니다."
    inputs = [MessageTextInput(name="raw_text", display_name="원문 텍스트", required=True, tool_mode=True), DropdownInput(name="duplicate_action", display_name="중복 처리 방식", options=["skip", "merge", "replace", "create_new"], value="skip"), BoolInput(name="dry_run", display_name="드라이런", value=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_request(getattr(self, "raw_text", ""), getattr(self, "duplicate_action", "skip"), getattr(self, "dry_run", True)))
