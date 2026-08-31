"""Self-contained GAIA and CUBE runtime used by ``scheduler_worker.py``.

This module deliberately has no dependency on the callback server deployment.
It preserves the GAIA external request contract and CUBE Rich Notification
payload used by the production callback application, while leaving callback
ACK/interactive-session behavior out of this Worker package.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from markdown_rich_notification import render_markdown_to_cube_body


GAIA_INPUT_TWEAK_NAME = "GaiA Input"
CUBE_REPLY_REQUEST_ID = "request_cond_change_main"
CUBE_BOT_FROMUSERNAME_COUNT = 5
GAIA_PERMISSION_REQUEST_URL = (
    "http://aimarket.skhynix.com/apps/gaia-market-web/market/agent"
)
GAIA_FORBIDDEN_MESSAGE = (
    "권한이 없는 사용자입니다. 아래 링크를 통해 PTMORE PKG Agent 권한 신청 부탁드립니다."
    f"\n[{GAIA_PERMISSION_REQUEST_URL}]({GAIA_PERMISSION_REQUEST_URL})"
)


class SettingsError(RuntimeError):
    """Raised when required Worker configuration is missing or malformed."""


class GaiaResponseError(RuntimeError):
    """Raised when GAIA has no usable final Chat Output answer."""


class ExternalApiError(RuntimeError):
    """Raised when GAIA or CUBE cannot complete a network request."""


class GaiaRequestError(ExternalApiError):
    """A GAIA failure with a non-sensitive category for the CUBE fallback."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"GAIA request failed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class Settings:
    """GAIA/CUBE configuration shared by a standalone Worker process."""

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


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _at(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def extract_final_answer(payload: Mapping[str, Any]) -> str:
    """Read GAIA's preferred normalized answer, then Chat Output text fallback."""

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


def build_cube_rich_notification(
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
) -> dict[str, Any]:
    """Build the same CUBE payload shape used by the callback server."""

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
                    "body": render_markdown_to_cube_body(message_text),
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
    data: str,
    metadata: str,
) -> dict[str, Any]:
    """Call the complete GAIA external URL with the configured GaiA Input tweak."""

    request_payload: dict[str, Any] = {
        "input_value": message,
        "user_id": user_id,
        "session_id": session_id,
        "tweaks": {
            GAIA_INPUT_TWEAK_NAME: {
                "data": data,
                "metadata": metadata,
            }
        },
    }
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
        if exc.response.status_code == 403:
            raise GaiaRequestError("forbidden") from exc
        raise GaiaRequestError("http_error") from exc
    except httpx.RequestError as exc:
        raise GaiaRequestError("connection") from exc
    except ValueError as exc:
        raise GaiaRequestError("invalid_json") from exc
    if not isinstance(body, dict):
        raise GaiaRequestError("unexpected_body")
    return body


def gaia_fallback_message(settings: Settings, error: Exception) -> str:
    """Create the same non-sensitive GAIA error explanation used in callback."""

    if isinstance(error, GaiaRequestError):
        causes = {
            "forbidden": GAIA_FORBIDDEN_MESSAGE,
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

    if isinstance(error, GaiaRequestError) and error.reason == "forbidden":
        return cause
    return f"{cause}\n{settings.user_error_message}"


async def send_cube_message(
    client: httpx.AsyncClient,
    settings: Settings,
    receiver_id: str,
    channel_id: str,
    message_text: str,
) -> None:
    """Send one final Rich Notification through the CUBE send API."""

    try:
        response = await client.post(
            settings.cube_send_url,
            headers={"Content-Type": "application/json"},
            json=build_cube_rich_notification(
                settings,
                receiver_id,
                channel_id,
                message_text,
            ),
            timeout=settings.cube_timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExternalApiError("CUBE message request failed.") from exc
