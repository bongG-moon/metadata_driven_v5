"""Local GaiA External API floating-chat integration test server.

This project deliberately keeps the GaiA external authentication key on the
server.  The browser talks only to this local FastAPI app, which adds the
required X-Gaia-* headers before proxying the GaiA External API request.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
load_dotenv(ROOT / ".env", override=False)

EMPLOYEE_ID_PATTERN = re.compile(r"^\d{7}$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


@dataclass(frozen=True)
class GaiaExternalSettings:
    """Runtime settings loaded from the local .env file."""

    agent_url: str
    api_key: str
    user_id: str
    fixed_session_id: str
    input_tweak_name: str
    timeout_seconds: float
    verify_ssl: bool


class ChatRequest(BaseModel):
    """One browser-originated user message for the external Agent."""

    message: str = Field(..., min_length=1, max_length=10_000)
    session_id: str = Field("", max_length=200)

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("메시지를 입력해 주세요.")
        return normalized

    @field_validator("session_id")
    @classmethod
    def _valid_session_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not SESSION_ID_PATTERN.fullmatch(normalized):
            raise ValueError("session_id에는 영문, 숫자, . _ : - 만 사용할 수 있습니다.")
        return normalized


def _env_bool(name: str, default: bool) -> bool:
    raw_value = str(os.getenv(name, "")).strip().lower()
    if not raw_value:
        return default
    return raw_value in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw_value = str(os.getenv(name, "")).strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name}은 숫자로 입력해 주세요.") from exc
    if value <= 0:
        raise ValueError(f"{name}은 0보다 커야 합니다.")
    return value


def _settings_errors() -> list[str]:
    errors: list[str] = []
    agent_url = str(os.getenv("GAIA_EXTERNAL_AGENT_URL", "")).strip()
    user_id = str(os.getenv("GAIA_TEST_USER_ID", "")).strip()
    api_key = str(os.getenv("GAIA_EXTERNAL_API_KEY", "")).strip()
    fixed_session_id = str(os.getenv("GAIA_TEST_SESSION_ID", "")).strip()
    input_tweak_name = str(os.getenv("GAIA_INPUT_TWEAK_NAME", "GaiA Input")).strip()

    parsed = urlparse(agent_url)
    if not agent_url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        errors.append("GAIA_EXTERNAL_AGENT_URL")
    if not api_key:
        errors.append("GAIA_EXTERNAL_API_KEY")
    if not EMPLOYEE_ID_PATTERN.fullmatch(user_id):
        errors.append("GAIA_TEST_USER_ID")
    if fixed_session_id and not SESSION_ID_PATTERN.fullmatch(fixed_session_id):
        errors.append("GAIA_TEST_SESSION_ID")
    if not input_tweak_name:
        errors.append("GAIA_INPUT_TWEAK_NAME")
    return errors


def load_settings() -> GaiaExternalSettings:
    """Read and validate settings without ever returning the API key to clients."""

    errors = _settings_errors()
    if errors:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "gaia_external_configuration_invalid",
                "message": ".env의 GaiA External 연결 정보를 확인해 주세요.",
                "invalid_or_missing": errors,
            },
        )
    return GaiaExternalSettings(
        agent_url=str(os.getenv("GAIA_EXTERNAL_AGENT_URL", "")).strip(),
        api_key=str(os.getenv("GAIA_EXTERNAL_API_KEY", "")).strip(),
        user_id=str(os.getenv("GAIA_TEST_USER_ID", "")).strip(),
        fixed_session_id=str(os.getenv("GAIA_TEST_SESSION_ID", "")).strip(),
        input_tweak_name=str(os.getenv("GAIA_INPUT_TWEAK_NAME", "GaiA Input")).strip(),
        timeout_seconds=_env_float("GAIA_REQUEST_TIMEOUT_SECONDS", 300.0),
        verify_ssl=_env_bool("GAIA_VERIFY_SSL", True),
    )


def _external_payload(message: str, session_id: str, settings: GaiaExternalSettings) -> dict:
    """Build the proven GaiA External API input body.

    This deliberately matches the working request supplied for this Agent:
    ``input_value``, ``session_id``, and the ``GaiA Input`` metadata tweak.
    It does not send an inferred A2A JSON-RPC envelope or extra ``data``
    field that could override this Flow's configured defaults.
    """

    return {
        "input_value": message,
        "session_id": session_id,
        "tweaks": {
            settings.input_tweak_name: {
                "metadata": json.dumps(
                    {"user_id": settings.user_id}, ensure_ascii=False, separators=(",", ":")
                )
            }
        },
    }


def _proxy_error_payload(response: httpx.Response, body: bytes) -> dict:
    """Produce a safe, readable upstream error without leaking local secrets."""

    detail = body.decode("utf-8", errors="replace").strip()
    try:
        decoded = json.loads(detail)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        detail = str(
            decoded.get("errorMessage")
            or decoded.get("message")
            or decoded.get("detail")
            or detail
        )
    return {
        "code": "gaia_external_request_failed",
        "message": detail or "GaiA External Gateway 호출에 실패했습니다.",
        "upstream_status": response.status_code,
    }


def create_app() -> FastAPI:
    """Create the local test application."""

    application = FastAPI(title="GaiA Floating Chat Local Test", version="1.0.0")
    application.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    @application.get("/", include_in_schema=False)
    async def home() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    @application.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "configured": not _settings_errors(),
            "configuration_errors": _settings_errors(),
        }

    @application.get("/api/config")
    async def browser_config() -> dict:
        """Return only display-safe configuration; the external key stays server-side."""

        errors = _settings_errors()
        if errors:
            return {
                "configured": False,
                "invalid_or_missing": errors,
                "message": ".env 저장 후 서버를 다시 실행해 주세요.",
            }
        settings = load_settings()
        return {
            "configured": True,
            "agent_url": settings.agent_url,
            "user_id": settings.user_id,
            "fixed_session_id": settings.fixed_session_id,
            "input_tweak_name": settings.input_tweak_name,
            "api_key_configured": True,
        }

    @application.post("/api/chat/completion")
    async def chat_completion(chat: ChatRequest):
        """Proxy one proven GaiA External API request with server-held credentials."""

        settings = load_settings()
        session_id = settings.fixed_session_id or chat.session_id
        payload = _external_payload(chat.message, session_id, settings)
        headers = {
            "Content-Type": "application/json",
            "X-Gaia-Auth-Key": settings.api_key,
            "X-Gaia-User-Id": settings.user_id,
        }
        timeout = httpx.Timeout(settings.timeout_seconds, connect=min(settings.timeout_seconds, 30.0))

        try:
            async with httpx.AsyncClient(timeout=timeout, verify=settings.verify_ssl) as client:
                upstream = await client.post(settings.agent_url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "gaia_external_connection_failed",
                    "message": "GaiA External Gateway에 연결하지 못했습니다.",
                    "error_type": type(exc).__name__,
                },
            ) from exc

        if upstream.status_code >= 400:
            # Preserve client-actionable upstream statuses such as 401/403.
            response_status = upstream.status_code if upstream.status_code < 500 else 502
            return JSONResponse(
                status_code=response_status,
                content={"detail": _proxy_error_payload(upstream, upstream.content)},
            )

        return {
            "response": _safe_json_or_text(upstream.content),
            "session_id": session_id,
            # This is intentionally safe to show in the local diagnostic UI.
            # It confirms the body shape but excludes the authentication key.
            "request_payload": payload,
        }

    return application


def _safe_json_or_text(body: bytes):
    """Decode a non-streaming upstream result for the local diagnostic UI."""

    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# Both names are intentional: "application" matches the production-style
# Uvicorn import path, while "app" supports standard Uvicorn usage.
application = create_app()
app = application


if __name__ == "__main__":
    uvicorn.run("app:application", host="0.0.0.0", port=8003, reload=False)
