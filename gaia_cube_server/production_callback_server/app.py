"""GAIA-CUBE production callback server.

This module intentionally implements only the current basic synchronous flow:

CUBE callback -> GAIA external Agent API -> CUBE Rich Notification -> callback result

It does not add MongoDB, workers, outbox, retry queues, or a scheduler.  The
only in-process state is the user+channel -> GAIA session_id mapping, so a
server restart starts new GAIA sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


LOGGER = logging.getLogger("gaia_cube_callback.production")
HELLO_CHATBOT_SENTINEL = "!@#HelloChatBot#@!"
INTERACTION_KEYS = ("UserSelection", "SendBtn")


class SettingsError(RuntimeError):
    """Raised when the production .env configuration is incomplete."""


class CallbackValidationError(ValueError):
    """Raised when a CUBE callback cannot be safely understood."""


class GaiaResponseError(RuntimeError):
    """Raised when GAIA returns no usable final Chat Output answer."""


class ExternalApiError(RuntimeError):
    """A safe error used for a failed GAIA or CUBE HTTP request."""


@dataclass(frozen=True)
class Settings:
    """Values needed to run the real callback server.

    Secrets are read only from environment variables or this folder's .env
    file. They are never returned by an endpoint or written to logs.
    """

    gaia_base_url: str
    gaia_auth_key: str
    default_gaia_service_id: str
    channel_service_map: dict[str, str]
    cube_send_url: str
    cube_bot_id: str
    cube_bot_token: str
    gaia_timeout_seconds: float
    cube_timeout_seconds: float
    callback_success_status: str
    callback_ignored_status: str
    user_error_message: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path(__file__).with_name(".env"), override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value or value.startswith("PASTE_"):
                raise SettingsError(
                    f"{name} is required. Copy .env.example to .env and enter its value."
                )
            return value

        def optional(name: str, default: str) -> str:
            value = os.getenv(name, "").strip()
            return value or default

        raw_channel_map = os.getenv("CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON", "").strip()
        channel_service_map: dict[str, str] = {}
        if raw_channel_map:
            try:
                parsed = json.loads(raw_channel_map)
            except json.JSONDecodeError as exc:
                raise SettingsError(
                    "CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON must be a JSON object."
                ) from exc
            if not isinstance(parsed, Mapping):
                raise SettingsError(
                    "CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON must be a JSON object."
                )
            for channel_id, service_id in parsed.items():
                channel = str(channel_id).strip()
                service = str(service_id).strip()
                if not channel or not service:
                    raise SettingsError(
                        "CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON cannot contain empty keys or values."
                    )
                channel_service_map[channel] = service

        default_service_id = optional("GAIA_SERVICE_ID", "")
        if not default_service_id and not channel_service_map:
            raise SettingsError(
                "Set GAIA_SERVICE_ID or CUBE_CHANNEL_GAIA_SERVICE_MAP_JSON."
            )

        def positive_float(name: str, default: float) -> float:
            raw_value = optional(name, str(default))
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise SettingsError(f"{name} must be a number.") from exc
            if value <= 0:
                raise SettingsError(f"{name} must be greater than zero.")
            return value

        return cls(
            gaia_base_url=optional("GAIA_BASE_URL", "http://gaia.example.internal").rstrip("/"),
            gaia_auth_key=required("GAIA_AUTH_KEY"),
            default_gaia_service_id=default_service_id,
            channel_service_map=channel_service_map,
            cube_send_url=required("CUBE_SEND_URL"),
            cube_bot_id=required("CUBE_BOT_ID"),
            cube_bot_token=required("CUBE_BOT_TOKEN"),
            gaia_timeout_seconds=positive_float("GAIA_TIMEOUT_SECONDS", 10),
            cube_timeout_seconds=positive_float("CUBE_TIMEOUT_SECONDS", 20),
            callback_success_status=optional("CUBE_CALLBACK_SUCCESS_STATUS", "success"),
            callback_ignored_status=optional("CUBE_CALLBACK_IGNORED_STATUS", "ignored"),
            user_error_message=optional(
                "USER_ERROR_MESSAGE",
                "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            ),
        )


@dataclass(frozen=True)
class CubeCallbackEvent:
    user_id: str
    channel_id: str
    message: str
    event_type: str


class InMemorySessionStore:
    """Maps a CUBE user+channel to one opaque GAIA session_id while running."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, *, user_id: str, channel_id: str) -> str:
        key = (user_id, channel_id)
        async with self._lock:
            session_id = self._sessions.get(key)
            if session_id is None:
                session_id = f"gc_{uuid.uuid4()}"
                self._sessions[key] = session_id
            return session_id


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            text = _nonempty_text(item)
            if text is not None:
                return text
        return None
    return _nonempty_text(value)


def _same_or_missing(
    *,
    primary: str | None,
    secondary: str | None,
    label: str,
) -> str | None:
    if primary is not None and secondary is not None and primary != secondary:
        raise CallbackValidationError(f"CUBE {label} values do not match.")
    return primary or secondary


def parse_cube_callback(payload: Mapping[str, Any]) -> CubeCallbackEvent | None:
    """Normalize supplied CUBE callback shapes.

    Returns None for the HelloChatBot control event.  It never fabricates a
    missing user or channel value.
    """

    envelope = payload.get("richnotificationmessage")
    if not isinstance(envelope, Mapping):
        raise CallbackValidationError("richnotificationmessage is required.")

    header = _mapping(envelope.get("header"))
    process = _mapping(envelope.get("process"))
    header_from = _mapping(header.get("from"))
    header_to = _mapping(header.get("to"))

    header_user_id = _nonempty_text(header_from.get("uniquename"))
    process_user_id = _nonempty_text(process.get("userId"))
    user_id = _same_or_missing(
        primary=header_user_id,
        secondary=process_user_id,
        label="user ID",
    )

    header_channel_id = _first_text(header_to.get("channelid"))
    process_channel_id = _nonempty_text(process.get("channelId"))
    channel_id = _same_or_missing(
        primary=header_channel_id,
        secondary=process_channel_id,
        label="channel ID",
    )

    processdata = _nonempty_text(process.get("processdata"))
    if processdata == HELLO_CHATBOT_SENTINEL:
        return None

    if user_id is None or channel_id is None:
        raise CallbackValidationError("A CUBE user ID and channel ID are required.")

    if processdata is not None:
        return CubeCallbackEvent(
            user_id=user_id,
            channel_id=channel_id,
            message=processdata,
            event_type="text_message",
        )

    for key in INTERACTION_KEYS:
        selected_value = _nonempty_text(process.get(key))
        if selected_value is not None:
            return CubeCallbackEvent(
                user_id=user_id,
                channel_id=channel_id,
                message=selected_value,
                event_type="rich_interaction",
            )

    raise CallbackValidationError("processdata or a supported interaction value is required.")


def resolve_gaia_service_id(settings: Settings, channel_id: str) -> str:
    """Resolve the fixed GAIA Agent assigned to the CUBE channel."""

    service_id = settings.channel_service_map.get(channel_id)
    if service_id:
        return service_id
    if settings.default_gaia_service_id:
        return settings.default_gaia_service_id
    raise CallbackValidationError("This CUBE channel has no configured GAIA Agent.")


def _get(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _is_chat_output(component: Mapping[str, Any]) -> bool:
    return component.get("component_display_name") == "Chat Output" or str(
        component.get("component_id") or ""
    ).startswith("ChatOutput-")


def extract_final_answer(payload: Mapping[str, Any]) -> str:
    """Extract text from the last valid Langflow Chat Output in a GAIA result."""

    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise GaiaResponseError("GAIA response has no outputs list.")

    selected: Mapping[str, Any] | None = None
    for outer in reversed(outputs):
        if not isinstance(outer, Mapping):
            continue
        inner_outputs = outer.get("outputs")
        if not isinstance(inner_outputs, list):
            continue
        for component in reversed(inner_outputs):
            if isinstance(component, Mapping) and _is_chat_output(component):
                selected = component
                break
        if selected is not None:
            break

    if selected is None:
        raise GaiaResponseError("GAIA response has no Chat Output.")

    if _get(selected, "results", "message", "data", "error") is True:
        raise GaiaResponseError("The final GAIA Chat Output is an error.")

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

    raise GaiaResponseError("The final GAIA Chat Output has no answer text.")


def build_cube_rich_notification(
    *,
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
) -> dict[str, Any]:
    """Build the supplied CUBE Rich Notification label payload."""

    return {
        "richnotification": {
            "header": {
                "from": settings.cube_bot_id,
                "token": settings.cube_bot_token,
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


async def call_gaia(
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    service_id: str,
    user_id: str,
    session_id: str,
    message: str,
) -> dict[str, Any]:
    url = f"{settings.gaia_base_url}/v2/agents/{quote(service_id, safe='')}/external"
    try:
        response = await client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-Gaia-Auth-Key": settings.gaia_auth_key,
                "X-Gaia-User-Id": user_id,
            },
            json={
                "message": message,
                "user_id": user_id,
                "session_id": session_id,
            },
            timeout=settings.gaia_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise ExternalApiError("GAIA request failed.") from exc

    if response.is_error:
        raise ExternalApiError(f"GAIA returned HTTP {response.status_code}.")

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ExternalApiError("GAIA returned invalid JSON.") from exc
    if not isinstance(body, dict):
        raise ExternalApiError("GAIA returned an unexpected JSON body.")
    return body


async def send_cube_message(
    *,
    client: httpx.AsyncClient,
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
) -> None:
    payload = build_cube_rich_notification(
        settings=settings,
        receiver_id=receiver_id,
        channel_id=channel_id,
        message_text=message_text,
    )
    try:
        response = await client.post(
            settings.cube_send_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=settings.cube_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise ExternalApiError("CUBE message request failed.") from exc
    if response.is_error:
        raise ExternalApiError(f"CUBE returned HTTP {response.status_code}.")


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the application.

    Tests can pass Settings and a MockTransport. Normal Uvicorn startup loads
    this folder's .env during startup and fails clearly when required values
    are missing.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or Settings.from_env()
        app.state.settings = active_settings
        app.state.sessions = InMemorySessionStore()
        app.state.http_client = httpx.AsyncClient(transport=transport)
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    app = FastAPI(
        title="GAIA-CUBE Production Callback Server",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        active_settings: Settings = request.app.state.settings
        return {
            "status": "ok",
            "mode": "production",
            "configured_default_agent": "yes"
            if active_settings.default_gaia_service_id
            else "no",
        }

    @app.post("/api/qna")
    async def receive_cube_callback(
        payload: dict[str, Any], request: Request
    ) -> JSONResponse:
        active_settings: Settings = request.app.state.settings
        sessions: InMemorySessionStore = request.app.state.sessions
        client: httpx.AsyncClient = request.app.state.http_client

        try:
            event = parse_cube_callback(payload)
        except CallbackValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if event is None:
            return JSONResponse(
                status_code=200,
                content={"status": active_settings.callback_ignored_status},
            )

        try:
            service_id = resolve_gaia_service_id(active_settings, event.channel_id)
            session_id = await sessions.get_or_create(
                user_id=event.user_id,
                channel_id=event.channel_id,
            )
            gaia_body = await call_gaia(
                client=client,
                settings=active_settings,
                service_id=service_id,
                user_id=event.user_id,
                session_id=session_id,
                message=event.message,
            )
            answer = extract_final_answer(gaia_body)
        except (CallbackValidationError, ExternalApiError, GaiaResponseError) as exc:
            LOGGER.warning("GAIA callback processing failed: %s", type(exc).__name__)
            try:
                await send_cube_message(
                    client=client,
                    settings=active_settings,
                    receiver_id=event.user_id,
                    channel_id=event.channel_id,
                    message_text=active_settings.user_error_message,
                )
            except ExternalApiError:
                LOGGER.warning("CUBE error message delivery also failed.")
            return JSONResponse(
                status_code=502,
                content={
                    "status": "error",
                    "message": "Unable to process the CUBE request.",
                },
            )

        try:
            await send_cube_message(
                client=client,
                settings=active_settings,
                receiver_id=event.user_id,
                channel_id=event.channel_id,
                message_text=answer,
            )
        except ExternalApiError as exc:
            LOGGER.warning("CUBE answer delivery failed: %s", type(exc).__name__)
            return JSONResponse(
                status_code=502,
                content={
                    "status": "error",
                    "message": "Unable to deliver the CUBE message.",
                },
            )

        return JSONResponse(
            status_code=200,
            content={
                "status": active_settings.callback_success_status,
                "message": "GAIA answer was sent to CUBE.",
            },
        )

    return app


app = create_app()
