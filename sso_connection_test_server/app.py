"""Minimal HCP SSO compatibility probe using Flask behind Uvicorn.

This server deliberately does not use Portal sessions, MongoDB, GAIA, or CUBE.
It only answers one question: can ``hcputil.auth.sso.SSO`` read the HCP SSO
cookie when it receives the same Flask ``request`` object used by the working
HCP examples?

Open ``/`` in an HCP browser.  A user without a valid SSO cookie is redirected
to the HCP SSO page.  After login, the endpoint returns only the employee ID
and Korean name as JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import uvicorn
from a2wsgi import WSGIMiddleware
from flask import Flask, jsonify, redirect, request


LOGGER = logging.getLogger("hcp_sso_probe")


@dataclass(frozen=True)
class SsoProbeError(RuntimeError):
    """A safe, user-facing HCP SSO probe failure."""

    code: str
    message: str
    error_type: str = ""
    missing_attribute: str = ""


def _error_response(error: SsoProbeError) -> tuple[Any, int]:
    """Return a safe diagnostic without exposing cookies or internal details."""

    detail: dict[str, str] = {"code": error.code, "message": error.message}
    if error.error_type:
        detail["error_type"] = error.error_type
    if error.missing_attribute:
        detail["missing_attribute"] = error.missing_attribute
    return jsonify({"detail": detail}), 503


def _new_sso() -> Any:
    """Construct the HCP helper with Flask's real request object."""

    try:
        from hcputil.auth.sso import SSO
    except ImportError as exc:
        LOGGER.error("HCP SSO module import failed: type=%s", type(exc).__name__)
        raise SsoProbeError(
            code="sso_module_unavailable",
            message="운영 Python 환경에서 hcputil.auth.sso 모듈을 찾을 수 없습니다.",
            error_type=type(exc).__name__,
        ) from exc

    try:
        # This intentionally matches the known working Flask sample:
        # ``sso = SSO(request)``.
        return SSO(request)
    except AttributeError as exc:
        # ``AttributeError.name`` is only the missing attribute identifier.
        # It is safe to expose in this isolated diagnostic server, while raw
        # exception text and cookies remain server-side only.
        missing_attribute = str(getattr(exc, "name", "") or "").strip()
        LOGGER.exception(
            "HCP SSO initialization failed: type=%s missing_attribute=%s",
            type(exc).__name__,
            missing_attribute or "unknown",
        )
        raise SsoProbeError(
            code="sso_initialization_failed",
            message="HCP SSO를 Flask 요청으로 초기화하지 못했습니다.",
            error_type=type(exc).__name__,
            missing_attribute=missing_attribute,
        ) from exc
    except Exception as exc:
        LOGGER.error("HCP SSO initialization failed: type=%s", type(exc).__name__)
        raise SsoProbeError(
            code="sso_initialization_failed",
            message="HCP SSO를 Flask 요청으로 초기화하지 못했습니다.",
            error_type=type(exc).__name__,
        ) from exc


def _employee_from_cookie(sso: Any, cookie: str | None) -> dict[str, str] | None:
    """Return just employee number/name, or None when SSO login is needed."""

    if not cookie:
        return None

    try:
        if sso.check_day_cookie(cookie) is not True:
            return None
        values = sso.get_sso_info(cookie)
    except Exception as exc:
        LOGGER.error("HCP SSO cookie lookup failed: type=%s", type(exc).__name__)
        raise SsoProbeError(
            code="sso_cookie_lookup_failed",
            message="HCP SSO 쿠키에서 사용자 정보를 읽지 못했습니다.",
            error_type=type(exc).__name__,
        ) from exc

    if not isinstance(values, (list, tuple)) or len(values) < 2:
        raise SsoProbeError(
            code="sso_identity_invalid",
            message="HCP SSO가 사번과 이름 형식의 사용자 정보를 반환하지 않았습니다.",
        )

    employee_id = str(values[0] or "").strip()
    employee_name = str(values[1] or "").strip()
    if not employee_id:
        raise SsoProbeError(
            code="sso_employee_id_missing",
            message="HCP SSO가 사용자 사번을 반환하지 않았습니다.",
        )

    return {
        "employee_id": employee_id,
        "employee_name": employee_name or employee_id,
    }


def create_flask_app() -> Flask:
    """Create the Flask app that receives the real Flask request object."""

    flask_app = Flask(__name__)

    @flask_app.route("/health", methods=["GET"])
    def health():
        """Public process health check; it never imports or calls HCP SSO."""

        return jsonify({"status": "ok"})

    @flask_app.route("/", methods=["GET"])
    @flask_app.route("/whoami", methods=["GET"])
    def whoami():
        """Redirect to HCP SSO when needed, otherwise show employee identity."""

        try:
            sso = _new_sso()
            employee = _employee_from_cookie(sso, request.headers.get("cookie"))
        except SsoProbeError as exc:
            return _error_response(exc)

        if employee is not None:
            return jsonify({"authenticated": True, **employee})

        redirect_url = str(getattr(sso, "redirect_url", "") or "").strip()
        if not redirect_url:
            return _error_response(
                SsoProbeError(
                    code="sso_redirect_missing",
                    message="HCP SSO 로그인 주소를 확인할 수 없습니다.",
                )
            )
        return redirect(redirect_url, code=307)

    return flask_app


def create_application() -> WSGIMiddleware:
    """Expose Flask as ASGI so the established Uvicorn command stays valid."""

    return WSGIMiddleware(create_flask_app())


# Both names are intentional: "application" matches the existing production
# command, while "app" supports normal Uvicorn import syntax.
application = create_application()
app = application


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
