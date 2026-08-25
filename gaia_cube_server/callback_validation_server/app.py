"""Temporary HCP server for validating the CUBE callback and reply path.

One CUBE message follows this intentionally short path:
    CUBE callback -> immediate ACK -> fixed Rich Notification reply to CUBE

GAIA is deliberately not configured or called here.  Deploy this app only
while verifying the registered CUBE callback URL, then switch HCP back to the
real production_callback_server application.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
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


LOGGER = logging.getLogger("cube_callback_validation")
CUBE_CALLBACK_PATH = "/api/v1/receiver"
HELLO_CHATBOT_SENTINEL = "!@#HelloChatBot#@!"
INTERACTION_KEYS = ("UserSelection", "SendBtn")
CUBE_REPLY_REQUEST_ID = "request_cond_change_main"
CUBE_BOT_FROMUSERNAME_COUNT = 5

# This is the only user-visible response sent by this validation server.
FIXED_CALLBACK_REPLY = "콜백 검증 성공: CUBE 메시지를 정상 수신하고 고정 응답을 보냈습니다."


class SettingsError(RuntimeError):
    """Raised when a required CUBE setting is missing or malformed."""


class CallbackValidationError(ValueError):
    """Raised when the incoming CUBE callback cannot be safely answered."""


class CubeDeliveryError(RuntimeError):
    """Raised when the fixed reply cannot be delivered to CUBE."""


@dataclass(frozen=True)
class Settings:
    """Only the CUBE values required to receive and answer one callback."""

    cube_send_url: str
    cube_bot_id: str
    cube_bot_token: str
    cube_bot_fromusername: tuple[str, ...]
    cube_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        """Read HCP environment variables and an optional local `.env` file."""

        load_dotenv(Path(__file__).with_name(".env"), override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value or value.startswith("PASTE_") or value.startswith("<"):
                raise SettingsError(f"{name} is required and must not be a placeholder.")
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

        def positive_seconds(name: str, default: float) -> float:
            raw_value = os.getenv(name, str(default)).strip()
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise SettingsError(f"{name} must be a number.") from exc
            if value <= 0:
                raise SettingsError(f"{name} must be greater than zero.")
            return value

        try:
            names = json.loads(required("CUBE_BOT_FROMUSERNAME_JSON"))
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
            cube_send_url=api_url("CUBE_SEND_URL"),
            cube_bot_id=required("CUBE_BOT_ID"),
            cube_bot_token=required("CUBE_BOT_TOKEN"),
            cube_bot_fromusername=bot_names,
            cube_timeout_seconds=positive_seconds("CUBE_TIMEOUT_SECONDS", 20),
        )


@dataclass(frozen=True)
class CubeCallbackEvent:
    """The callback identity needed to return a message to the same CUBE chat."""

    user_id: str
    channel_id: str


def _text(value: Any) -> str | None:
    """Return a non-empty string, otherwise None."""

    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _single_text(value: Any, label: str) -> str | None:
    """Read one ID from a CUBE string or an array of identical strings.

    CUBE examples use both scalar and array ``channelid`` fields. A callback
    with two different non-empty IDs is unsafe: replying to the first one
    could expose the fixed reply in the wrong chat, so reject it instead.
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
    # of silently treating only the first one as the user interaction.
    distinct_values = list(dict.fromkeys(values))
    return "\n".join(distinct_values) if distinct_values else None


def parse_cube_callback(payload: Mapping[str, Any]) -> CubeCallbackEvent | None:
    """Read a normal CUBE message, selection, or hello control callback."""

    envelope = payload.get("richnotificationmessage")
    if not isinstance(envelope, Mapping):
        raise CallbackValidationError("richnotificationmessage is required.")

    process = _mapping(envelope.get("process"))
    message = _text(process.get("processdata"))

    # CUBE may send this control request while connecting the bot.  It is not a
    # user question and must not create a visible reply.
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
    return CubeCallbackEvent(user_id=user_id, channel_id=channel_id)


def build_cube_rich_notification(
    settings: Settings, receiver_id: str, channel_id: str
) -> dict[str, Any]:
    """Build the known working CUBE Rich Notification payload.

    The populated ``process`` object is intentional: CUBE did not deliver
    messages when this object was empty in the supplied working example.
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
                    "body": {
                        "bodystyle": "none",
                        "row": [
                            {
                                "bgcolor": "#ffffff",
                                "border": "false",
                                "align": "",
                                "width": "",
                                "column": [
                                    {
                                        "bgcolor": "#ffffff",
                                        "border": "false",
                                        "align": "left",
                                        "valign": "middle",
                                        "width": "100%",
                                        "type": "label",
                                        "control": {
                                            "active": "true",
                                            "text": [FIXED_CALLBACK_REPLY],
                                            "color": "#000000",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
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


async def send_fixed_reply(
    client: httpx.AsyncClient,
    settings: Settings,
    receiver_id: str,
    channel_id: str,
) -> None:
    """Send the fixed validation answer to the callback's user and channel."""

    try:
        response = await client.post(
            settings.cube_send_url,
            headers={"Content-Type": "application/json"},
            json=build_cube_rich_notification(settings, receiver_id, channel_id),
            timeout=settings.cube_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CubeDeliveryError("CUBE validation reply request failed.") from exc


async def process_fixed_reply(
    event: CubeCallbackEvent,
    settings: Settings,
    client: httpx.AsyncClient,
) -> None:
    """Send the fixed reply after the callback has already received HTTP 200."""

    try:
        await send_fixed_reply(client, settings, event.user_id, event.channel_id)
    except CubeDeliveryError:
        LOGGER.warning("CUBE fixed validation reply delivery failed after ACK.")
        return

    LOGGER.info(
        "CUBE callback validation reply delivered: user=%s channel=%s",
        event.user_id,
        event.channel_id,
    )


def create_application(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create the HCP validation application; tests inject a mock transport."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings or Settings.from_env()
        app.state.http_client = httpx.AsyncClient(transport=transport)
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    app = FastAPI(
        title="CUBE Callback Validation Server",
        description="CUBE callback -> fixed CUBE reply (no GAIA call)",
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
        return {
            "status": "ok",
            "mode": "fixed_reply_validation",
            "callback_path": CUBE_CALLBACK_PATH,
        }

    @app.post(CUBE_CALLBACK_PATH)
    async def receive_cube_callback(
        payload: dict[str, Any], request: Request, background_tasks: BackgroundTasks
    ) -> JSONResponse:
        """ACK every valid CUBE message before sending the fixed reply."""

        try:
            event = parse_cube_callback(payload)
        except CallbackValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if event is None:
            return JSONResponse({"status": "ignored"})

        LOGGER.info(
            "CUBE callback validation received: user=%s channel=%s",
            event.user_id,
            event.channel_id,
        )
        active_settings: Settings = request.app.state.settings
        client: httpx.AsyncClient = request.app.state.http_client
        background_tasks.add_task(process_fixed_reply, event, active_settings, client)

        # Keep the ACK equivalent to the supplied working FastAPI callback:
        # HTTP 200 with a JSON null body. CUBE receives the fixed answer later
        # through the separate Rich Notification API request.
        return JSONResponse(content=None, status_code=200)

    return app


# HCP runs this exact ASGI object and fixed Uvicorn entrypoint.
application = create_application()


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
