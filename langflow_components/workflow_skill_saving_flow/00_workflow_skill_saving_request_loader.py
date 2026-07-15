# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 Workflow Skill 등록 요청 로더
# 역할: 자연어 Workflow Skill 정의를 안전한 등록 요청 페이로드로 초기화합니다.
# 주요 입력: 원문 텍스트(raw_text), 중복 처리 방식(duplicate_action), 드라이런(dry_run)
# 주요 출력: 페이로드 출력(payload_out)
# 처리 흐름: 원문과 저장 정책을 정규화하고 후속 후보 생성·검수·저장 노드가 공유할 기본 계약을 만듭니다.
# 유지보수 포인트: 실제 저장 기본값은 dry_run이며, 지원하지 않는 중복 정책은 skip으로 제한합니다.
# =============================================================================

from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, MessageTextInput, Output
from lfx.schema.data import Data

METADATA_TYPE = "workflow_skill"
ALLOWED_DUPLICATE_ACTIONS = {"skip", "merge", "replace", "create_new"}


# 주요 함수: 사용자 원문과 실행 옵션을 Workflow Skill 등록 파이프라인의 공통 페이로드로 변환합니다.
def build_request(raw_text: Any, duplicate_action: str = "skip", dry_run: Any = True) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    action = _duplicate_action(duplicate_action)
    return {
        "metadata_type": METADATA_TYPE,
        "request": {"raw_text": text, "duplicate_action": action, "dry_run": _bool(dry_run, True)},
        "refinement": {"refined_text": "", "needs_more_input": False, "missing_information": [], "assumptions": []},
        "items": [],
        "existing_matches": [],
        "conflict_warnings": [],
        "duplicate_decision": {"action": action, "target_key": ""},
        "review": {},
        "write_result": {},
        "trace": {"raw_text_preview": text[:500], "generated_items_preview": []},
        "errors": [] if text else [{"type": "empty_raw_text", "message": "등록할 Workflow Skill 원문이 비어 있습니다."}],
        "warnings": [],
    }


# 함수 설명: `_duplicate_action()`은 허용된 중복 처리 방식만 통과시키고 나머지는 안전한 skip으로 바꿉니다.
def _duplicate_action(value: Any) -> str:
    action = str(value or "skip").strip().lower()
    return action if action in ALLOWED_DUPLICATE_ACTIONS else "skip"


# 함수 설명: `_bool()`은 Bool 또는 문자열 형태의 toggle 값을 일관된 bool 값으로 해석합니다.
def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


# Langflow 컴포넌트 클래스: 캔버스 입력과 출력 포트를 Workflow Skill 등록 요청 계약에 연결합니다.
class WorkflowSkillSavingRequestLoader(Component):
    display_name = "00 Workflow Skill 등록 요청 로더"
    description = "자연어 Workflow Skill 정의와 저장 정책을 등록 페이로드로 초기화합니다."
    inputs = [
        MessageTextInput(name="raw_text", display_name="원문 텍스트", required=True, tool_mode=True),
        DropdownInput(name="duplicate_action", display_name="중복 처리 방식", options=["skip", "merge", "replace", "create_new"], value="skip"),
        BoolInput(name="dry_run", display_name="드라이런", value=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 현재 입력값으로 Workflow Skill 등록 페이로드를 만들어 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(
            data=build_request(
                getattr(self, "raw_text", ""),
                getattr(self, "duplicate_action", "skip"),
                getattr(self, "dry_run", True),
            )
        )
