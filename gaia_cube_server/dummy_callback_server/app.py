"""CUBE callback -> dummy GAIA -> simulated CUBE sender.

This application deliberately makes no external HTTP request.  It is a local
test double for the basic synchronous integration flow that will later be
implemented with real GAIA and CUBE credentials.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from threading import RLock
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field


HELLO_HANDSHAKE = "!@#HelloChatBot#@!"
DEFAULT_CHANNEL_TO_GAIA_SERVICE = {
    "CHANNEL_ID_EXAMPLE": "DUMMY_GAIA_PRODUCTION_AGENT",
    "500008005": "DUMMY_GAIA_PRODUCTION_AGENT",
    "CHANNEL_QUALITY_EXAMPLE": "DUMMY_GAIA_QUALITY_AGENT",
}
PROCESS_IDENTITY_KEYS = {"processdata", "userId", "channelId"}


class GaiaResponseError(RuntimeError):
    """Raised when a GAIA response does not contain a usable final answer."""


@dataclass(frozen=True)
class DummySettings:
    """Non-secret settings used only by the dummy application."""

    channel_to_gaia_service: dict[str, str]
    bot_id: str
    bot_token: str


def _load_channel_map() -> dict[str, str]:
    raw_value = os.getenv("DUMMY_CHANNEL_GAIA_MAP")
    if not raw_value:
        return dict(DEFAULT_CHANNEL_TO_GAIA_SERVICE)

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("DUMMY_CHANNEL_GAIA_MAP must be a JSON object") from exc

    if not isinstance(parsed, dict) or not all(
        isinstance(channel_id, str)
        and channel_id.strip()
        and isinstance(service_id, str)
        and service_id.strip()
        for channel_id, service_id in parsed.items()
    ):
        raise RuntimeError(
            "DUMMY_CHANNEL_GAIA_MAP must map non-empty channel IDs to non-empty service IDs"
        )
    return {channel_id.strip(): service_id.strip() for channel_id, service_id in parsed.items()}


def load_settings() -> DummySettings:
    return DummySettings(
        channel_to_gaia_service=_load_channel_map(),
        # The dummy server must never need, accept, or expose a real CUBE token.
        bot_id="DUMMY_CUBE_BOT_ID",
        bot_token="DUMMY_CUBE_BOT_TOKEN",
    )


SETTINGS = load_settings()


@dataclass(frozen=True)
class CallbackMessage:
    """Normalized values extracted from the CUBE callback body."""

    user_id: str
    channel_id: str
    process_data: str
    selections: dict[str, str]
    input_kind: str

    @property
    def gaia_message(self) -> str:
        """Convert text or Rich Message selections into the dummy GAIA input."""
        selection_text = "\n".join(
            f"- {key}: {value}" for key, value in self.selections.items()
        )
        if self.process_data and selection_text:
            return f"{self.process_data}\n\n[CUBE 선택값]\n{selection_text}"
        if self.process_data:
            return self.process_data
        return f"[CUBE 선택값]\n{selection_text}"


@dataclass
class SessionRecord:
    user_id: str
    channel_id: str
    gaia_service_id: str
    gaia_session_id: str
    request_count: int = 0
    last_input_kind: str = ""


@dataclass
class DummyState:
    """In-memory state. It is intentionally cleared when the process restarts."""

    sessions: dict[tuple[str, str], SessionRecord] = field(default_factory=dict)
    outgoing_messages: list[dict[str, Any]] = field(default_factory=list)
    gaia_runs: list[dict[str, Any]] = field(default_factory=list)
    lock: RLock = field(default_factory=RLock, repr=False)

    def reset(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.outgoing_messages.clear()
            self.gaia_runs.clear()

    def get_or_create_session(
        self, *, user_id: str, channel_id: str, gaia_service_id: str, input_kind: str
    ) -> SessionRecord:
        key = (user_id, channel_id)
        with self.lock:
            record = self.sessions.get(key)
            if record is None:
                record = SessionRecord(
                    user_id=user_id,
                    channel_id=channel_id,
                    gaia_service_id=gaia_service_id,
                    # CUBE IDs are not placed directly into the GAIA session ID.
                    gaia_session_id=f"gc_{uuid4()}",
                )
                self.sessions[key] = record
            record.request_count += 1
            record.last_input_kind = input_kind
            return record

    def record_gaia_run(self, run: dict[str, Any]) -> None:
        with self.lock:
            self.gaia_runs.append(run)

    def record_outgoing_message(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.outgoing_messages.append(payload)

    def snapshot_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            return [asdict(record) for record in self.sessions.values()]

    def snapshot_outgoing_messages(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.outgoing_messages)

    def snapshot_gaia_runs(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.gaia_runs)


STATE = DummyState()


class CallbackAck(BaseModel):
    status: str
    message: str
    targetUser: str | None = None


class TestResetResponse(BaseModel):
    status: str = "reset"


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be a string",
        )
    normalized = value.strip()
    return normalized or None


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must be an object",
        )
    return value


def _header_channel_id(header: Mapping[str, Any]) -> str | None:
    destination = header.get("to")
    if destination is None:
        return None
    destination_mapping = _mapping(destination, "richnotificationmessage.header.to")
    channel_ids = destination_mapping.get("channelid")
    if channel_ids is None:
        return None
    if not isinstance(channel_ids, list):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="richnotificationmessage.header.to.channelid must be an array",
        )
    if not channel_ids:
        return None
    return _optional_text(
        channel_ids[0], "richnotificationmessage.header.to.channelid[0]"
    )


def _header_user_id(header: Mapping[str, Any]) -> str | None:
    sender = header.get("from")
    if sender is None:
        return None
    sender_mapping = _mapping(sender, "richnotificationmessage.header.from")
    return _optional_text(
        sender_mapping.get("uniquename"),
        "richnotificationmessage.header.from.uniquename",
    )


def _effective_identity(
    *, header_value: str | None, process_value: str | None, label: str
) -> str:
    if header_value and process_value and header_value != process_value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"CUBE header and process {label} do not match",
        )
    value = header_value or process_value
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"CUBE {label} is required",
        )
    return value


def _selection_values(process: Mapping[str, Any]) -> dict[str, str]:
    """Keep user-selected Rich Message values without assuming a process ID name."""
    values: dict[str, str] = {}
    for key, raw_value in process.items():
        if key in PROCESS_IDENTITY_KEYS or raw_value in (None, ""):
            continue
        if isinstance(raw_value, (str, int, float, bool)):
            values[str(key)] = str(raw_value)
            continue
        # The provided samples use scalar values. Keep other JSON values readable
        # during local testing instead of silently dropping them.
        values[str(key)] = json.dumps(raw_value, ensure_ascii=False, sort_keys=True)
    return values


def parse_cube_callback(payload: Any) -> CallbackMessage:
    """Validate and normalize the callback shapes shown in the supplied CUBE guide."""
    root = _mapping(payload, "payload")
    rich_message = _mapping(
        root.get("richnotificationmessage"), "richnotificationmessage"
    )
    header_raw = rich_message.get("header")
    header = (
        _mapping(header_raw, "richnotificationmessage.header")
        if header_raw is not None
        else {}
    )
    process = _mapping(
        rich_message.get("process"), "richnotificationmessage.process"
    )

    header_user_id = _header_user_id(header)
    header_channel_id = _header_channel_id(header)
    process_user_id = _optional_text(
        process.get("userId"), "richnotificationmessage.process.userId"
    )
    process_channel_id = _optional_text(
        process.get("channelId"), "richnotificationmessage.process.channelId"
    )
    user_id = _effective_identity(
        header_value=header_user_id,
        process_value=process_user_id,
        label="user ID",
    )
    channel_id = _effective_identity(
        header_value=header_channel_id,
        process_value=process_channel_id,
        label="channel ID",
    )

    process_data = process.get("processdata", "")
    if not isinstance(process_data, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="richnotificationmessage.process.processdata must be a string",
        )
    normalized_process_data = process_data.strip()
    selections = _selection_values(process)

    if normalized_process_data == HELLO_HANDSHAKE:
        input_kind = "hello_handshake"
    elif selections:
        input_kind = "rich_interaction"
    elif normalized_process_data:
        input_kind = "text_message"
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A text message or Rich Message selection is required",
        )

    return CallbackMessage(
        user_id=user_id,
        channel_id=channel_id,
        process_data=normalized_process_data,
        selections=selections,
        input_kind=input_kind,
    )


def _get(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def extract_final_answer(payload: Mapping[str, Any]) -> str:
    """Extract the last valid Chat Output using the supplied GAIA guide contract."""
    component_outputs: list[Mapping[str, Any]] = []
    outer_outputs = payload.get("outputs")
    if isinstance(outer_outputs, list):
        for outer in reversed(outer_outputs):
            if not isinstance(outer, Mapping):
                continue
            inner_outputs = outer.get("outputs")
            if not isinstance(inner_outputs, list):
                continue
            component_outputs.extend(
                item for item in reversed(inner_outputs) if isinstance(item, Mapping)
            )

    if not component_outputs:
        raise GaiaResponseError("GAIA response has no component outputs")

    selected = next(
        (
            item
            for item in component_outputs
            if item.get("component_display_name") == "Chat Output"
            or str(item.get("component_id") or "").startswith("ChatOutput-")
        ),
        None,
    )
    if selected is None:
        raise GaiaResponseError("GAIA response has no Chat Output")

    if _get(selected, "results", "message", "data", "error") is True:
        raise GaiaResponseError("final GAIA Chat Output is an error")

    candidate_paths = (
        ("results", "gaia_response", "data", "answer"),
        ("results", "message", "data", "gaia_response", "answer"),
        ("results", "message", "data", "text"),
        ("outputs", "gaia_response", "message", "answer"),
        ("outputs", "message", "message"),
        ("artifacts", "message"),
    )
    for path in candidate_paths:
        answer = _nonempty_text(_get(selected, *path))
        if answer is not None:
            return answer

    messages = selected.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, Mapping):
                continue
            sender = str(message.get("sender") or "").strip().lower()
            answer = _nonempty_text(message.get("message"))
            if answer is not None and sender in {"machine", "ai", "assistant"}:
                return answer

    raise GaiaResponseError("final GAIA Chat Output has no answer text")


class DummyGaiaClient:
    """Makes a representative GAIA response locally and extracts its final answer."""

    def run(
        self, *, service_id: str, user_id: str, session_id: str, message: str
    ) -> tuple[str, dict[str, Any]]:
        final_answer = (
            f"[더미 GAIA | {service_id}]\n"
            f"'{message}' 요청을 받았습니다. "
            "이 응답은 외부 GAIA를 호출하지 않고 만든 테스트용 최종 답변입니다."
        )
        response = {
            "session_id": session_id,
            "outputs": [
                {
                    "inputs": {},
                    "outputs": [
                        {
                            "component_display_name": "Chat Output",
                            "component_id": "ChatOutput-EarlierDummy",
                            "results": {
                                "message": {
                                    "data": {
                                        "text": "이전 Chat Output이며 발송하면 안 됩니다.",
                                        "error": False,
                                    }
                                }
                            },
                        },
                        {
                            "component_display_name": "Chat Output",
                            "component_id": "ChatOutput-FinalDummy",
                            "results": {
                                "gaia_response": {"data": {"answer": final_answer}},
                                "message": {
                                    "data": {
                                        "text": final_answer,
                                        "error": False,
                                    }
                                },
                            },
                            "messages": [
                                {
                                    "message": final_answer,
                                    "sender": "Machine",
                                    "sender_name": "AI",
                                    "session_id": session_id,
                                }
                            ],
                        },
                    ],
                }
            ],
            "dummy_request": {
                "service_id": service_id,
                "user_id": user_id,
                "session_id": session_id,
                "message": message,
            },
        }
        return extract_final_answer(response), response


class SimulatedCubeTransport:
    """Builds and captures the Rich Notification request instead of sending it."""

    def __init__(self, settings: DummySettings, state: DummyState) -> None:
        self._settings = settings
        self._state = state

    def send_text(self, *, receiver_id: str, channel_id: str, message_text: str) -> dict[str, Any]:
        payload = {
            "richnotification": {
                "header": {
                    "from": self._settings.bot_id,
                    "token": self._settings.bot_token,
                    "to": {
                        "uniquename": [receiver_id],
                        "channelid": [channel_id],
                    },
                },
                "content": [
                    {
                        "header": {},
                        "body": {
                            "bodystyle": "none",
                            "row": [
                                {
                                    "bgcolor": "#ffffff",
                                    "border": False,
                                    "align": "",
                                    "width": "",
                                    "column": [
                                        {
                                            "bgcolor": "#ffffff",
                                            "border": False,
                                            "align": "",
                                            "valign": "middle",
                                            "width": "100%",
                                            "type": "label",
                                            "control": {
                                                "active": True,
                                                "text": [message_text],
                                                "color": "#000000",
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        "process": {},
                    }
                ],
            }
        }
        self._state.record_outgoing_message(payload)
        return payload


DUMMY_GAIA = DummyGaiaClient()
DUMMY_CUBE = SimulatedCubeTransport(SETTINGS, STATE)

app = FastAPI(
    title="GAIA-CUBE Dummy Callback Server",
    version="0.1.0",
    description=(
        "외부 CUBE·GAIA 호출 없이 callback → GAIA → Rich Notification 흐름을 검증하는 서버"
    ),
)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "GAIA-CUBE Dummy Callback Server",
        "callback": "POST /api/qna",
        "docs": "GET /docs",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "dummy"}


@app.post("/api/qna", response_model=CallbackAck)
def receive_cube_callback(payload: dict[str, Any]) -> CallbackAck:
    """Receive a CUBE callback and run the complete dummy synchronous flow."""
    callback = parse_cube_callback(payload)
    if callback.input_kind == "hello_handshake":
        return CallbackAck(
            status="ignored",
            message="CUBE hello handshake ignored",
            targetUser=callback.user_id,
        )

    service_id = SETTINGS.channel_to_gaia_service.get(callback.channel_id)
    if service_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "No dummy GAIA service is configured for this channel. "
                "Set DUMMY_CHANNEL_GAIA_MAP or use one of the documented test channels."
            ),
        )

    session = STATE.get_or_create_session(
        user_id=callback.user_id,
        channel_id=callback.channel_id,
        gaia_service_id=service_id,
        input_kind=callback.input_kind,
    )
    answer, raw_gaia_response = DUMMY_GAIA.run(
        service_id=service_id,
        user_id=callback.user_id,
        session_id=session.gaia_session_id,
        message=callback.gaia_message,
    )
    STATE.record_gaia_run(
        {
            "service_id": service_id,
            "user_id": callback.user_id,
            "channel_id": callback.channel_id,
            "session_id": session.gaia_session_id,
            "input_kind": callback.input_kind,
            "gaia_message": callback.gaia_message,
            "extracted_answer": answer,
            "raw_response": raw_gaia_response,
        }
    )
    DUMMY_CUBE.send_text(
        receiver_id=callback.user_id,
        channel_id=callback.channel_id,
        message_text=answer,
    )

    return CallbackAck(
        status="success",
        message="Dummy CUBE Rich Notification was captured.",
        targetUser=callback.user_id,
    )


@app.get("/api/test/config")
def test_config() -> dict[str, Any]:
    """Show only dummy configuration; this endpoint does not exist in production."""
    return {
        "mode": "dummy",
        "channel_to_gaia_service": SETTINGS.channel_to_gaia_service,
        "bot_id": SETTINGS.bot_id,
    }


@app.get("/api/test/sessions")
def test_sessions() -> dict[str, Any]:
    return {"sessions": STATE.snapshot_sessions()}


@app.get("/api/test/gaia-runs")
def test_gaia_runs() -> dict[str, Any]:
    return {"gaia_runs": STATE.snapshot_gaia_runs()}


@app.get("/api/test/outgoing-messages")
def test_outgoing_messages() -> dict[str, Any]:
    return {"outgoing_messages": STATE.snapshot_outgoing_messages()}


@app.post("/api/test/reset", response_model=TestResetResponse)
def test_reset() -> TestResetResponse:
    STATE.reset()
    return TestResetResponse()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8001, reload=True)
