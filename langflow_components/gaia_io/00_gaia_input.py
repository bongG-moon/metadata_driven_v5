# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: GaiA Input Adapter
# 역할: 표준 Chat Input Message에 GaiA AgentBuilder의 A2A data와 metadata를 병합합니다.
# 주요 입력: 표준 Chat Input Message, data JSON, metadata JSON
# 주요 출력: GaiA 속성이 포함된 Message
# 유지보수 포인트: 잘못된 JSON은 실행을 중단하지 않고 빈 객체로 정규화합니다.
# =============================================================================

import json
import logging
from typing import Any

from lfx.base.io.chat import ChatComponent
from lfx.inputs.inputs import HandleInput
from lfx.io import MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

logger = logging.getLogger(__name__)


# Langflow 컴포넌트 클래스: 표준 Chat Input 뒤에서 GaiA 실행 문맥을 Message에 병합하는 어댑터입니다.
class GaiAInputAdapter(ChatComponent):
    display_name = "GaiA Input Adapter"
    description = (
        "표준 Chat Input이 생성한 Message에 GaiA A2A data와 metadata를 병합합니다. "
        "Playground에서는 빈 JSON을 사용하고, 운영 호출에서는 tweak으로 부가 문맥을 전달합니다."
    )
    documentation: str = "https://docs.langflow.org/chat-input-and-output"
    icon = "MessagesSquare"
    name = "GaiAInputAdapter"
    minimized = True

    inputs = [
        HandleInput(
            name="input_message",
            display_name="Chat Input Message",
            info="표준 Chat Input에서 받은 사용자 Message입니다.",
            input_types=["Message"],
            required=True,
        ),
        MultilineInput(
            name="data",
            display_name="data",
            value="{}",
            info='GAIA A2A data JSON from tweaks["GaiA Input Adapter"]["data"].',
            advanced=False,
        ),
        MultilineInput(
            name="metadata",
            display_name="metadata",
            value="{}",
            info='GAIA A2A metadata JSON from tweaks["GaiA Input Adapter"]["metadata"].',
            advanced=False,
        ),
    ]
    outputs = [Output(display_name="message", name="message", method="message_response", types=["Message"])]

    # 함수 설명: 문자열 JSON은 객체로 읽고, 비어 있거나 해석할 수 없는 값은 빈 객체로 처리합니다.
    def _json_or_raw(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            logger.warning("GAIA_DEBUG GaiA Input non-json value ignored type=%s", type(value).__name__)
            return {}

    # 함수 설명: data 또는 metadata가 한 번 더 감싸진 형식도 단일 dict로 정규화합니다.
    def _as_dict(self, value: Any, field_name: str) -> dict:
        parsed = self._json_or_raw(value)
        if isinstance(parsed, dict):
            nested = parsed.get(field_name)
            if isinstance(nested, dict) and len(parsed) == 1:
                return nested
            return parsed
        logger.warning("GAIA_DEBUG GaiA Input %s is not object: %s", field_name, type(parsed).__name__)
        return {}

    # 함수 설명: 디버그 상태를 한글이 보존되는 JSON 문자열로 안전하게 변환합니다.
    def _json_for_log(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    # 주요 메서드: 표준 Chat Input Message의 본문과 세션을 유지하면서 A2A 부가 정보를 병합합니다.
    def message_response(self) -> Message:
        source_message = getattr(self, "input_message", None)
        message = source_message if isinstance(source_message, Message) else Message(text=str(source_message or ""))
        explicit_data = self._as_dict(getattr(self, "data", "{}"), "data")
        explicit_metadata = self._as_dict(getattr(self, "metadata", "{}"), "metadata")
        current_data = getattr(message, "data", None)
        current_metadata = getattr(message, "metadata", None)
        data = {**(current_data if isinstance(current_data, dict) else {}), **explicit_data}
        metadata = {**(current_metadata if isinstance(current_metadata, dict) else {}), **explicit_metadata}

        message.data = data
        message.a2a_data = data
        message.metadata = metadata
        message.framework2_metadata = metadata
        message.a2a_metadata = metadata
        if metadata.get("session_id"):
            message.session_id = str(metadata.get("session_id"))

        status_payload = {
            "component": "GaiA Input Adapter",
            "text_length": len(str(getattr(message, "text", "") or "")),
            "data_keys": sorted(str(key) for key in data.keys()),
            "metadata_keys": sorted(str(key) for key in metadata.keys()),
        }
        debug_text = self._json_for_log(status_payload)
        logger.warning("GAIA_DEBUG GaiA Input Adapter payload=%s", debug_text)
        print(f"GAIA_DEBUG GaiA Input Adapter payload={debug_text}", flush=True)
        self.status = Data(data=status_payload)
        return message
