from __future__ import annotations

from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, MessageTextInput, Output
from lfx.schema.data import Data

ALLOWED_DUPLICATE_ACTIONS = {"merge", "replace", "skip", "create_new"}


def build_request(raw_text: Any, duplicate_action: str = "skip", dry_run: Any = True) -> dict[str, Any]:
    action = _duplicate_action(duplicate_action)
    return {
        "metadata_type": "main_flow_filter",
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


def _duplicate_action(value: Any) -> str:
    action = str(value or "skip").strip().lower()
    return action if action in ALLOWED_DUPLICATE_ACTIONS else "skip"


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


class MainFlowFilterSavingRequestLoader(Component):
    display_name = "00 메인 플로우 필터 등록 요청 로더"
    description = "자연어 메인 플로우 필터 등록 요청을 시작합니다. 기본값은 드라이런입니다."
    inputs = [MessageTextInput(name="raw_text", display_name="원문 텍스트", required=True, tool_mode=True), DropdownInput(name="duplicate_action", display_name="중복 처리 방식", options=["skip", "merge", "replace", "create_new"], value="skip"), BoolInput(name="dry_run", display_name="드라이런", value=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=build_request(getattr(self, "raw_text", ""), getattr(self, "duplicate_action", "skip"), getattr(self, "dry_run", True)))
