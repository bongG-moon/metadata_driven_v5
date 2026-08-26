"""HCP-only GAIA-CUBE callback server.

One request follows one simple path:
    CUBE callback -> immediate ACK -> GAIA API -> CUBE Rich Notification

The server keeps the current in-memory GAIA session ID and a short successful
conversation history for each user and CUBE channel. GAIA and CUBE delivery
run in FastAPI's in-process background task after the ACK. It does not add
external workers, databases, schedulers, retry queues, or a second callback
route.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from markdown_rich_notification import render_markdown_to_cube_body


LOGGER = logging.getLogger("gaia_cube_callback")
CUBE_CALLBACK_PATH = "/api/v1/receiver"
HELLO_CHATBOT_SENTINEL = "!@#HelloChatBot#@!"
INTERACTION_KEYS = ("UserSelection", "SendBtn")
CUBE_REPLY_REQUEST_ID = "request_cond_change_main"
CUBE_BOT_FROMUSERNAME_COUNT = 5
# Keep only the latest three question/answer pairs in the callback server.
MAX_CONVERSATION_HISTORY_MESSAGES = 6
CUBE_SESSION_ID_NAMESPACE = "gaia-cube-session-v1"
CubeBodyRenderer = Callable[[str], dict[str, Any]]


class SettingsError(RuntimeError):
    """Raised when required HCP configuration is missing or malformed."""


class CallbackValidationError(ValueError):
    """Raised when a callback has no safe user, channel, or message."""


class GaiaResponseError(RuntimeError):
    """Raised when GAIA has no usable final answer."""


class ExternalApiError(RuntimeError):
    """Raised when GAIA or CUBE cannot complete an HTTP request."""


class GaiaRequestError(ExternalApiError):
    """A GAIA failure with a safe category for the CUBE fallback message."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"GAIA request failed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Settings:
    """Only values needed by the HCP callback server.

    ``GAIA_API_URL`` is the complete GAIA Agent endpoint. The server never
    appends a service ID or reconstructs the URL.
    """

    gaia_api_url: str
    gaia_auth_key: str
    cube_send_url: str
    cube_bot_id: str
    cube_bot_token: str
    cube_bot_fromusername: tuple[str, ...]
    gaia_timeout_seconds: float
    cube_timeout_seconds: float
    user_error_message: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Load HCP environment variables and an optional HCP `.env` file."""

        load_dotenv(Path(__file__).with_name(".env"), override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value or value.startswith("PASTE_") or value.startswith("<"):
                raise SettingsError(f"{name} is required and must not be a placeholder.")
            return value

        def positive_seconds(name: str, default: float) -> float:
            raw_value = os.getenv(name, str(default)).strip()
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise SettingsError(f"{name} must be a number.") from exc
            if value <= 0:
                raise SettingsError(f"{name} must be greater than zero.")
            return value

        def api_url(name: str) -> str:
            value = required(name)
            parsed = urlparse(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path in {"", "/"}
            ):
                raise SettingsError(
                    f"{name} must be a complete http(s) API URL including its path."
                )
            return value

        raw_names = required("CUBE_BOT_FROMUSERNAME_JSON")
        try:
            names = json.loads(raw_names)
        except json.JSONDecodeError as exc:
            raise SettingsError(
                "CUBE_BOT_FROMUSERNAME_JSON must be a JSON array with five bot names."
            ) from exc
        if (
            not isinstance(names, list)
            or len(names) != CUBE_BOT_FROMUSERNAME_COUNT
            or not all(isinstance(name, str) and name.strip() for name in names)
        ):
            raise SettingsError(
                "CUBE_BOT_FROMUSERNAME_JSON must contain exactly five non-empty strings."
            )
        bot_names = tuple(name.strip() for name in names)
        if any(
            name.startswith("PASTE_")
            or "YOUR_" in name.upper()
            or "<" in name
            or ">" in name
            for name in bot_names
        ):
            raise SettingsError(
                "CUBE_BOT_FROMUSERNAME_JSON must contain real bot display names."
            )

        return cls(
            gaia_api_url=api_url("GAIA_API_URL"),
            gaia_auth_key=required("GAIA_AUTH_KEY"),
            cube_send_url=api_url("CUBE_SEND_URL"),
            cube_bot_id=required("CUBE_BOT_ID"),
            cube_bot_token=required("CUBE_BOT_TOKEN"),
            cube_bot_fromusername=bot_names,
            gaia_timeout_seconds=positive_seconds("GAIA_TIMEOUT_SECONDS", 10),
            cube_timeout_seconds=positive_seconds("CUBE_TIMEOUT_SECONDS", 20),
            user_error_message=os.getenv(
                "USER_ERROR_MESSAGE",
                "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ).strip()
            or "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )


@dataclass(frozen=True)
class CubeCallbackEvent:
    """The three values required for one GAIA request and CUBE reply."""

    user_id: str
    channel_id: str
    message: str


class InMemorySessionStore:
    """Reuse one GAIA session and a short visible history per CUBE chat."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], str] = {}
        self._histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_with_history(
        self, user_id: str, channel_id: str
    ) -> tuple[str, list[dict[str, Any]]]:
        key = (user_id, channel_id)
        async with self._lock:
            # A deterministic ID lets the GAIA server/Phoenix associate this
            # CUBE chat with the same session even after this HCP app restarts.
            session_id = self._sessions.setdefault(
                key,
                f"gc_{uuid.uuid5(uuid.NAMESPACE_URL, f'{CUBE_SESSION_ID_NAMESPACE}:{user_id}:{channel_id}')}",
            )
            # The stored values are deliberately simple JSON-safe chat turns.
            # Return a copy so a request cannot mutate another request's history.
            history = [
                {
                    "role": turn["role"],
                    "content": turn["content"],
                    "files": list(turn["files"]),
                }
                for turn in self._histories.get(key, [])
            ]
            return session_id, history

    async def save(self, user_id: str, channel_id: str, session_id: str) -> None:
        async with self._lock:
            self._sessions[(user_id, channel_id)] = session_id

    async def append_completed_turn(
        self,
        user_id: str,
        channel_id: str,
        question: str,
        answer: str,
    ) -> None:
        """Remember only an answer that was successfully sent to CUBE."""

        key = (user_id, channel_id)
        async with self._lock:
            history = self._histories.setdefault(key, [])
            history.extend(
                [
                    {"role": "user", "content": question, "files": []},
                    {"role": "assistant", "content": answer, "files": []},
                ]
            )
            if len(history) > MAX_CONVERSATION_HISTORY_MESSAGES:
                del history[:-MAX_CONVERSATION_HISTORY_MESSAGES]


def build_gaia_context(
    *,
    message: str,
    user_id: str,
    channel_id: str,
    session_id: str,
    conversation_history: list[dict[str, Any]] | None = None,
    cube_user_id: str | None = None,
) -> tuple[str, str]:
    """Build the JSON-string ``data`` and ``metadata`` Flow inputs.

    These values describe the real CUBE callback context. GAIA-internal UI
    fields such as ``super_agent_id`` are intentionally not fabricated here.
    """

    history = [dict(turn) for turn in (conversation_history or [])]
    history.append({"role": "user", "content": message, "files": []})
    metadata = {
        "platform": "CUBE",
        "user_id": user_id,
        "session_id": session_id,
        "cube_user_id": cube_user_id or user_id,
    }
    if channel_id:
        metadata["cube_channel_id"] = channel_id
    try:
        return (
            json.dumps({"conversation_history": history}, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("CUBE conversation context must be JSON-serializable.") from exc


def _text(value: Any) -> str | None:
    """Return a non-empty string, otherwise None."""

    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _single_text(value: Any, label: str) -> str | None:
    """Read one ID from a CUBE string or an array of identical strings.

    CUBE examples use both scalar and array ``channelid`` fields. A callback
    with two different non-empty IDs is unsafe: replying to the first one
    could expose the answer in the wrong chat, so reject it instead.
    """

    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, list):
        return None

    values = [text for item in value if (text := _text(item))]
    if len(set(values)) > 1:
        raise CallbackValidationError(
            f"CUBE {label} must contain exactly one unique value."
        )
    return values[0] if values else None


def _matching_values(*values: str | None, label: str) -> str | None:
    """Use any documented CUBE field, but reject conflicting IDs."""

    present_values = [value for value in values if value]
    if len(set(present_values)) > 1:
        raise CallbackValidationError(f"CUBE {label} values do not match.")
    return present_values[0] if present_values else None


def _selection_values(value: Any) -> list[str]:
    """Keep only non-empty text selection values; never stringify objects."""

    if isinstance(value, str):
        return [text] if (text := _text(value)) else []
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _resultdata_message(envelope: Mapping[str, Any]) -> str | None:
    """Read a Rich Message selection from ``result.resultdata[].value``.

    The working CUBE callback example uses this shape after a button or radio
    selection.  It is only a fallback: normal free-text ``processdata`` and
    the documented process keys keep priority.
    """

    resultdata = _mapping(envelope.get("result")).get("resultdata")
    if isinstance(resultdata, Mapping):
        resultdata = [resultdata]
    if not isinstance(resultdata, list):
        return None

    values: list[str] = []
    for item in resultdata:
        values.extend(_selection_values(_mapping(item).get("value")))

    # A radio button normally produces one value. If a future Rich Message
    # returns multiple checkbox values, preserve every distinct value instead
    # of silently sending only the first one to GAIA.
    distinct_values = list(dict.fromkeys(values))
    return "\n".join(distinct_values) if distinct_values else None


def parse_cube_callback(payload: Mapping[str, Any]) -> CubeCallbackEvent | None:
    """Read a CUBE callback. None means the CUBE hello control event."""

    envelope = payload.get("richnotificationmessage")
    if not isinstance(envelope, Mapping):
        raise CallbackValidationError("richnotificationmessage is required.")

    process = _mapping(envelope.get("process"))
    message = _text(process.get("processdata"))

    if message == HELLO_CHATBOT_SENTINEL:
        return None

    header = _mapping(envelope.get("header"))
    header_from = _mapping(header.get("from"))
    header_to = _mapping(header.get("to"))
    user_id = _matching_values(
        _single_text(header_from.get("uniquename"), "user ID"),
        _single_text(process.get("userId"), "user ID"),
        label="user ID",
    )
    channel_id = _matching_values(
        _single_text(header_from.get("channelid"), "channel ID"),
        _single_text(header_to.get("channelid"), "channel ID"),
        _single_text(process.get("channelId"), "channel ID"),
        label="channel ID",
    )
    if message is None:
        message = next(
            (_text(process.get(key)) for key in INTERACTION_KEYS if _text(process.get(key))),
            None,
        )
    if message is None:
        message = _resultdata_message(envelope)
    if not user_id or not channel_id or not message:
        raise CallbackValidationError(
            "A CUBE user ID, channel ID, and processdata or selection value are required."
        )
    return CubeCallbackEvent(user_id=user_id, channel_id=channel_id, message=message)


def _at(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def extract_final_answer(payload: Mapping[str, Any]) -> str:
    """Read the preferred GAIA answer from the last Langflow Chat Output."""

    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise GaiaResponseError("GAIA response has no outputs list.")

    for outer in reversed(outputs):
        inner_outputs = _mapping(outer).get("outputs")
        if not isinstance(inner_outputs, list):
            continue
        for component in reversed(inner_outputs):
            component = _mapping(component)
            is_chat_output = (
                component.get("component_display_name") == "Chat Output"
                or str(component.get("component_id", "")).startswith("ChatOutput-")
            )
            if not is_chat_output:
                continue

            result = _mapping(component.get("results"))
            if _at(result, "message", "data", "error") is True:
                raise GaiaResponseError("The final GAIA Chat Output is an error.")
            answer = _text(_at(result, "gaia_response", "data", "answer"))
            if answer:
                return answer
            answer = _text(_at(result, "message", "data", "text"))
            if answer:
                return answer
            raise GaiaResponseError("The final GAIA Chat Output has no answer text.")

    raise GaiaResponseError("GAIA response has no Chat Output.")


def _returned_session_id(payload: Mapping[str, Any]) -> str | None:
    return _text(payload.get("session_id"))


def render_gaia_answer_to_cube_body(message_text: str) -> dict[str, Any]:
    """Turn GAIA Markdown into CUBE rows using the supplied production shape.

    This adapter owns only ``richnotification.content[0].body``. The existing
    bot header, CUBE ``process`` object, and outbound HTTP request remain
    unchanged.
    """

    return render_markdown_to_cube_body(message_text)


def build_cube_rich_notification(
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
    *,
    body_renderer: CubeBodyRenderer = render_gaia_answer_to_cube_body,
) -> dict[str, Any]:
    """Build the CUBE payload with one explicitly selected body renderer.

    The default remains the current production parser.  Case-specific app
    files pass their renderer directly in code; there is no renderer setting
    in `.env` and no change to the CUBE header/process contract.
    """

    return {
        "richnotification": {
            "header": {
                "from": settings.cube_bot_id,
                "token": settings.cube_bot_token,
                "fromusername": list(settings.cube_bot_fromusername),
                "to": {
                    "uniquename": [receiver_id],
                    "channelid": [channel_id],
                },
            },
            "content": [
                {
                    "header": {},
                    "body": body_renderer(message_text),
                    # CUBE did not deliver messages when this object was empty.
                    "process": {
                        "callbacktype": "url",
                        "callbackaddress": "",
                        "processdata": "",
                        "processtype": "",
                        "summary": ["", "", "", "", ""],
                        "session": {"sessionid": "", "sequence": "1"},
                        "mandatory": [],
                        "requestid": [CUBE_REPLY_REQUEST_ID],
                    },
                }
            ],
            "result": "",
        }
    }


async def call_gaia(
    client: httpx.AsyncClient,
    settings: Settings,
    user_id: str,
    session_id: str,
    message: str,
    *,
    data: str | None = None,
    metadata: str | None = None,
) -> dict[str, Any]:
    """Call the exact GAIA_API_URL supplied in the environment.

    ``data`` and ``metadata`` are JSON strings for Flow inputs exposed by the
    GAIA Agent. The callback path fills both with real CUBE context; callers
    that omit them retain the basic three-field GAIA request shape.
    """

    request_payload: dict[str, str] = {
        "input_value": message,
        "user_id": user_id,
        "session_id": session_id,
    }
    if data is not None:
        request_payload["data"] = data
    if metadata is not None:
        request_payload["metadata"] = metadata

    try:
        response = await client.post(
            settings.gaia_api_url,
            headers={
                "Content-Type": "application/json",
                "X-Gaia-Auth-Key": settings.gaia_auth_key,
                "X-Gaia-User-Id": user_id,
            },
            json=request_payload,
            timeout=settings.gaia_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
    except httpx.TimeoutException as exc:
        raise GaiaRequestError("timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise GaiaRequestError("http_error") from exc
    except httpx.RequestError as exc:
        raise GaiaRequestError("connection") from exc
    except ValueError as exc:
        raise GaiaRequestError("invalid_json") from exc
    if not isinstance(body, dict):
        raise GaiaRequestError("unexpected_body")
    return body


def _gaia_fallback_message(settings: Settings, error: Exception) -> str:
    """Return a useful but non-sensitive explanation for the CUBE user.

    Never expose HTTP status details, internal addresses, exception text, or
    credentials.  The configured user message remains the final guidance.
    """

    if isinstance(error, GaiaRequestError):
        causes = {
            "timeout": "주의: GAIA 응답 시간이 초과되었습니다.",
            "http_error": "오류: GAIA API가 요청을 정상 처리하지 못했습니다.",
            "connection": "오류: GAIA API 연결에 실패했습니다.",
            "invalid_json": "오류: GAIA API 응답 형식을 읽지 못했습니다.",
            "unexpected_body": "오류: GAIA API 응답 형식이 예상과 다릅니다.",
        }
        cause = causes.get(error.reason, "오류: GAIA API 요청 중 오류가 발생했습니다.")
    elif isinstance(error, GaiaResponseError):
        detail = str(error)
        if "is an error" in detail:
            cause = "오류: GAIA/Langflow가 처리 오류를 반환했습니다."
        elif "no answer" in detail or "no Chat Output" in detail:
            cause = "오류: GAIA/Langflow 응답에서 최종 답변을 찾지 못했습니다."
        else:
            cause = "오류: GAIA/Langflow 응답 형식이 예상과 다릅니다."
    else:
        cause = "오류: GAIA 요청 처리 중 오류가 발생했습니다."

    return f"{cause}\n{settings.user_error_message}"


async def send_cube_message(
    client: httpx.AsyncClient,
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
    *,
    body_renderer: CubeBodyRenderer = render_gaia_answer_to_cube_body,
) -> None:
    """Send one GAIA answer (or the safe fallback) to CUBE."""

    try:
        response = await client.post(
            settings.cube_send_url,
            headers={"Content-Type": "application/json"},
            json=build_cube_rich_notification(
                settings,
                receiver_id,
                channel_id,
                message_text,
                body_renderer=body_renderer,
            ),
            timeout=settings.cube_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalApiError("CUBE message request failed.") from exc


async def process_gaia_and_send(
    event: CubeCallbackEvent,
    settings: Settings,
    sessions: InMemorySessionStore,
    client: httpx.AsyncClient,
    *,
    body_renderer: CubeBodyRenderer = render_gaia_answer_to_cube_body,
) -> None:
    """Finish an accepted CUBE request after its HTTP ACK has been returned.

    CUBE must not wait for Langflow/GAIA execution. Delivery failures cannot
    change the already-returned ACK, so they are logged and use the existing
    visible CUBE fallback when possible.
    """

    session_id, history = await sessions.get_or_create_with_history(
        event.user_id, event.channel_id
    )
    data, metadata = build_gaia_context(
        message=event.message,
        user_id=event.user_id,
        channel_id=event.channel_id,
        session_id=session_id,
        conversation_history=history,
    )
    LOGGER.info(
        "GAIA CUBE context prepared: user=%s channel=%s session=%s history_messages=%d",
        event.user_id,
        event.channel_id,
        session_id,
        len(history),
    )
    try:
        gaia_response = await call_gaia(
            client,
            settings,
            event.user_id,
            session_id,
            event.message,
            data=data,
            metadata=metadata,
        )
        answer = extract_final_answer(gaia_response)
        if returned_session_id := _returned_session_id(gaia_response):
            LOGGER.info(
                "GAIA session observed: sent=%s returned=%s same=%s",
                session_id,
                returned_session_id,
                returned_session_id == session_id,
            )
            if returned_session_id == session_id:
                await sessions.save(event.user_id, event.channel_id, returned_session_id)
            else:
                # Do not replace the deterministic CUBE identity with an
                # unverified GAIA-generated value. The log above tells the
                # operator that GAIA did not echo the supplied session ID.
                LOGGER.warning(
                    "GAIA returned a different session ID; keeping the stable CUBE session."
                )
        else:
            LOGGER.info("GAIA session observed: sent=%s returned=<missing>", session_id)
    except (ExternalApiError, GaiaResponseError) as exc:
        LOGGER.warning("GAIA processing failed: %s", type(exc).__name__)
        try:
            await send_cube_message(
                client,
                settings,
                event.user_id,
                event.channel_id,
                _gaia_fallback_message(settings, exc),
                body_renderer=body_renderer,
            )
        except ExternalApiError as fallback_exc:
            LOGGER.warning(
                "CUBE fallback delivery failed after ACK: %s",
                type(fallback_exc).__name__,
            )
        return

    try:
        await send_cube_message(
            client,
            settings,
            event.user_id,
            event.channel_id,
            answer,
            body_renderer=body_renderer,
        )
    except ExternalApiError:
        LOGGER.warning("CUBE answer delivery failed after ACK.")
        return

    await sessions.append_completed_turn(
        event.user_id,
        event.channel_id,
        event.message,
        answer,
    )

    LOGGER.info(
        "GAIA answer delivered to CUBE: user=%s channel=%s",
        event.user_id,
        event.channel_id,
    )


def create_application(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    body_renderer: CubeBodyRenderer = render_gaia_answer_to_cube_body,
) -> FastAPI:
    """Create one HCP app for a fixed renderer case.

    The chosen renderer is a Python function supplied by the case's app file,
    not an environment setting.  Both cases otherwise use the same callback,
    GAIA, session, CUBE send, and fallback path.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or Settings.from_env()
        app.state.sessions = InMemorySessionStore()
        app.state.http_client = httpx.AsyncClient(transport=transport)
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    app = FastAPI(
        title="happy engr",
        description="CUBE callback → GAIA → CUBE response",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/hello")
    async def say_hello() -> Response:
        return Response(content="hello world!", media_type="text/plain")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "callback_path": CUBE_CALLBACK_PATH}

    @app.post(CUBE_CALLBACK_PATH)
    async def receive_cube_callback(
        payload: dict[str, Any], request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """Validate the callback, ACK it immediately, then run GAIA in-process."""

        try:
            event = parse_cube_callback(payload)
        except CallbackValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if event is None:
            return JSONResponse({"status": "ignored"})

        active_settings: Settings = request.app.state.settings
        sessions: InMemorySessionStore = request.app.state.sessions
        client: httpx.AsyncClient = request.app.state.http_client
        background_tasks.add_task(
            process_gaia_and_send,
            event,
            active_settings,
            sessions,
            client,
            body_renderer=body_renderer,
        )

        # Match the supplied working FastAPI pattern: a valid callback receives
        # HTTP 200 with a JSON null body. The user-visible answer is sent later
        # through CUBE Rich Notification, not in this callback response.
        return JSONResponse(content=None, status_code=200)

    return app


# HCP runs this exact ASGI object and fixed Uvicorn entrypoint.
application = create_application()


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
