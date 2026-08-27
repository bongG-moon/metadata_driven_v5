"""Manually send one question through GAIA and deliver its answer to CUBE.

This is a direct test tool, not a public FastAPI endpoint. Fill in the values
below, then run it in HCP with the same `.env` values as `app.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app import (
    ExternalApiError,
    GaiaResponseError,
    Settings,
    SettingsError,
    build_gaia_context,
    call_gaia,
    extract_final_answer,
    send_cube_message,
)


# ---------------------------------------------------------------------------
# Enter only the values for this one test here, then run:
#     python manual_gaia_cube_send.py
# Do not put GAIA/CUBE authentication keys here; keep them in `.env`.
# ---------------------------------------------------------------------------
MESSAGE = "PASTE_MESSAGE_HERE"
RECEIVER_ID = "PASTE_CUBE_RECEIVER_ID_HERE"
# A receiver ID is enough for a direct send. Fill this only when you need a channel.
CHANNEL_ID = ""

# Leave blank to use RECEIVER_ID as the GAIA user ID.
GAIA_USER_ID = ""

# Leave blank to start a new GAIA conversation for this test.
SESSION_ID = ""

# Optional: enter only completed turns that happened before ``MESSAGE``.
# The current MESSAGE is appended as the final user message automatically.
RECENT_CONVERSATION_HISTORY: list[dict[str, Any]] = []


@dataclass(frozen=True)
class ManualSendResult:
    """Values useful for confirming one manual GAIA-to-CUBE run."""

    answer: str
    session_id: str
    data: str
    metadata: str


def _required(value: str, label: str) -> str:
    """Reject missing manually entered values before making any HTTP call."""

    cleaned = value.strip()
    if not cleaned or cleaned.startswith("PASTE_"):
        raise ValueError(f"{label} is required.")
    return cleaned


def _optional_channel(value: str) -> str:
    """Allow a blank channel only when the CUBE setup explicitly supports it."""

    cleaned = value.strip()
    if cleaned.startswith("PASTE_"):
        raise ValueError("CHANNEL_ID must be filled in or intentionally left blank.")
    return cleaned


async def run_manual_send(
    *,
    settings: Settings,
    message: str,
    receiver_id: str,
    channel_id: str,
    gaia_user_id: str,
    session_id: str,
    client: httpx.AsyncClient,
    conversation_history: list[dict[str, Any]] | None = None,
) -> ManualSendResult:
    """Run exactly one direct GAIA call followed by one CUBE send."""

    message = _required(message, "message")
    receiver_id = _required(receiver_id, "receiver ID")
    gaia_user_id = _required(gaia_user_id, "GAIA user ID")
    session_id = _required(session_id, "session ID")
    data, metadata = build_gaia_context(
        message=message,
        user_id=gaia_user_id,
        channel_id=channel_id.strip(),
        session_id=session_id,
        conversation_history=conversation_history,
        cube_user_id=receiver_id,
    )

    # The callback server and this manual tool both place these values inside
    # GAIA's configured Chat Input ``tweaks`` object.
    gaia_response = await call_gaia(
        client,
        settings,
        gaia_user_id,
        session_id,
        message,
        data=data,
        metadata=metadata,
    )
    answer = extract_final_answer(gaia_response)
    returned_session_id = gaia_response.get("session_id")
    if isinstance(returned_session_id, str) and returned_session_id.strip():
        session_id = returned_session_id.strip()

    # An empty channel ID is allowed for the direct-send shape supplied by the user.
    await send_cube_message(client, settings, receiver_id, channel_id.strip(), answer)
    return ManualSendResult(
        answer=answer,
        session_id=session_id,
        data=data,
        metadata=metadata,
    )


async def _main() -> int:
    try:
        receiver_id = _required(RECEIVER_ID, "RECEIVER_ID")
        gaia_user_id = GAIA_USER_ID.strip() or receiver_id
        session_id = SESSION_ID.strip() or f"manual_{uuid.uuid4()}"
        channel_id = _optional_channel(CHANNEL_ID)
        settings = Settings.from_env()
        async with httpx.AsyncClient() as client:
            result = await run_manual_send(
                settings=settings,
                message=MESSAGE,
                receiver_id=receiver_id,
                channel_id=channel_id,
                gaia_user_id=gaia_user_id,
                session_id=session_id,
                client=client,
                conversation_history=RECENT_CONVERSATION_HISTORY,
            )
    except (SettingsError, ExternalApiError, GaiaResponseError, ValueError) as exc:
        print(f"Manual GAIA-CUBE test failed: {exc}")
        return 1

    print("CUBE send request was accepted.")
    print(f"GAIA session ID: {result.session_id}")
    print(f"GAIA data input: {result.data}")
    print(f"GAIA metadata input: {result.metadata}")
    print("GAIA answer sent to CUBE:")
    print(result.answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
