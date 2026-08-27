# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: GaiA Input
# 역할: GaiA A2A 요청의 질문, data, metadata를 하나의 Message로 전달하는 운영용
#       시작점입니다. metadata의 session_id는 Message.metadata와 Message.session_id에
#       함께 보존되어 다음 세션 ID 추출 컴포넌트가 사용할 수 있습니다.
# 주요 입력: input_value(질문), data(JSON), metadata(JSON)
# 주요 출력: GaiA 문맥이 포함된 Message
# 유지보수 포인트: 리스트형 data는 Langflow Message.data 제약 때문에 JSON 문자열로
#       보존하며, Router에서는 필요한 conversation_history만 다시 복원합니다.
# =============================================================================
"""GaiA A2A ingress component for the Router Flow.

This is the actual Flow entry point in the GAIA production route. It is not an
adapter after a native Chat Input: the external API supplies ``input_value``
as the user's question and supplies ``data``/``metadata`` through tweaks for
this component. The downstream session extractor deliberately receives this
Message and emits a session-only Message for Router 00's MessageTextInput.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from lfx.base.io.chat import ChatComponent
from lfx.io import MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message


logger = logging.getLogger(__name__)


class GaiAInput(ChatComponent):
    display_name = "GaiA Input"
    description = (
        "사용자의 질문과 실행에 필요한 추가 정보를 받아 플로우를 시작합니다. "
        "input_value에는 질문, data에는 이전 대화/첨부파일, metadata에는 세션/사용자 정보를 넣습니다."
    )
    documentation: str = "https://docs.langflow.org/chat-input-and-output"
    icon = "MessagesSquare"
    # Langflow 1.11은 외부 /run의 top-level input_value를 ``ChatInput``
    # runtime type에만 자동 주입합니다. display_name은 GaiA Input으로 유지하되,
    # 이 custom ingress가 질문을 받도록 internal runtime type은 ChatInput입니다.
    name = "ChatInput"
    minimized = True

    inputs = [
        MultilineInput(
            name="input_value",
            display_name="input_value",
            value="",
            info="User message from Langflow /run input_value.",
            input_types=[],
        ),
        MultilineInput(
            name="data",
            display_name="data",
            value="{}",
            info='GAIA A2A data JSON from tweaks["GaiA Input"]["data"].',
            advanced=False,
        ),
        MultilineInput(
            name="metadata",
            display_name="metadata",
            value="{}",
            info='GAIA A2A metadata JSON from tweaks["GaiA Input"]["metadata"].',
            advanced=False,
        ),
    ]
    outputs = [Output(display_name="message", name="message", method="message_response", types=["Message"])]

    def _json_or_raw(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            # GaiA A2A conversation_history의 문자열 값에 실제 개행/제어 문자가
            # 포함될 수 있으므로 strict=False를 한 번 더 시도합니다.
            try:
                return json.loads(text, strict=False)
            except Exception:
                logger.warning("GAIA_DEBUG GaiA Input non-json value ignored type=%s", type(value).__name__)
                return {}

    def _as_dict(self, value: Any, field_name: str) -> dict[str, Any]:
        parsed = self._json_or_raw(value)
        if isinstance(parsed, dict):
            nested = parsed.get(field_name)
            if isinstance(nested, dict) and len(parsed) == 1:
                return nested
            return parsed
        logger.warning("GAIA_DEBUG GaiA Input %s is not object: %s", field_name, type(parsed).__name__)
        return {}

    def _json_for_log(self, value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            return str(value)

    def message_response(self) -> Message:
        raw_data = getattr(self, "data", "{}")
        raw_metadata = getattr(self, "metadata", "{}")
        raw_input = getattr(self, "input_value", "")
        debug_input = {
            "raw_data_type": type(raw_data).__name__,
            "raw_data_value": str(raw_data)[:500] if raw_data is not None else "None",
            "raw_metadata_type": type(raw_metadata).__name__,
            "raw_metadata_value": str(raw_metadata)[:300] if raw_metadata is not None else "None",
            "raw_input_type": type(raw_input).__name__,
            "raw_input_value": str(raw_input)[:200] if raw_input is not None else "None",
        }
        print(f"GAIA_DEBUG GaiA Input RAW={json.dumps(debug_input, ensure_ascii=False, default=str)}", flush=True)
        data = self._as_dict(raw_data, "data")
        metadata = self._as_dict(raw_metadata, "metadata")
        text = str(raw_input or "")

        # Langflow 1.11 Message.data는 mapping만 안정적으로 직렬화합니다. 목록
        # 값은 JSON 문자열로 보존하고 Router 00이 conversation_history만 복원합니다.
        safe_data: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, list):
                try:
                    safe_data[str(key)] = json.dumps(value, ensure_ascii=False, default=str)
                except Exception:
                    safe_data[str(key)] = "[]"
            else:
                safe_data[str(key)] = value

        message = Message(
            text=text,
            data=safe_data,
            metadata=metadata,
            session_id=str(metadata.get("session_id")) if metadata.get("session_id") else None,
        )

        status_payload = {
            "component": "GaiA Input",
            "text_length": len(text),
            "data_keys": sorted(str(key) for key in data.keys()),
            "metadata_keys": sorted(str(key) for key in metadata.keys()),
            "session_id_present": bool(metadata.get("session_id")),
        }
        debug_text = self._json_for_log(status_payload)
        logger.warning("GAIA_DEBUG GaiA Input payload=%s", debug_text)
        print(f"GAIA_DEBUG GaiA Input payload={debug_text}", flush=True)
        self.status = Data(data=status_payload)
        return message
