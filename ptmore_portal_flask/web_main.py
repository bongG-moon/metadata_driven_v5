"""Flask application module for the PTMORE PKG Agent Portal.

The Portal's existing business rules remain in ``portal_core.py`` so the
Flask migration can preserve the tested API contracts, MongoDB stores,
metadata authoring, schedule ownership, and Phoenix dashboard behavior.
Only Flask handles HTTP routes, sessions, and the current user identity.
``index.py`` imports this module's ``app`` object to start the WebApp.

For the first local check, every request receives the fixed Flask session
identity requested by the operator: ``2069026 / 문봉건``.  Switching
``PTMORE_PORTAL_FLASK_AUTH_MODE`` to ``sso`` enables the commented HCP SSO
pattern once the production package is available.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, TypeVar
from urllib.parse import quote

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pydantic import BaseModel, ValidationError
from urllib3 import disable_warnings
from urllib3.exceptions import InsecureRequestWarning

import portal_core


os.environ["NLS_LANG"] = [
    "AMERICAN_AMERICA.KO16MSWIN949",
    "AMERICAN_AMERICA.WE8MSWIN1252",
    "AMERICAN_AMERICA.UTF8",
    "AMERICAN_AMERICA.AL32UTF8",
    "AMERICAN_AMERICA.KO16KSC5601",
    "KOREAN_KOREA.KO16MSWIN949",
    "KOREAN_KOREA.UTF8",
    "KOREAN_KOREA.AL32UTF8",
    ".UTF8",
][-1]

# The imports above intentionally match the existing Flask deployment base.
# HCP Secret/runtime environment variables are read directly with ``os.getenv``.
# Do not load a local ``.env`` file during WebApp module import.
# The Portal does not globally disable TLS warnings; API certificate policy is
# still governed by the existing Portal environment configuration.
_ = (requests, disable_warnings, InsecureRequestWarning, abort, url_for)

app = Flask(__name__, static_folder="static", static_url_path="/static", template_folder="templates")
app.config["SECRET_KEY"] = os.getenv("PTMORE_FLASK_SESSION_SECRET") or os.urandom(12)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
url_path = ""


_EMPLOYEE_ID_PATTERN = re.compile(r"^\d{7}$")
_MOCK_EMPLOYEE_ID = "2069026"
_MOCK_EMPLOYEE_NAME = "문봉건"
_PROFILE_IMAGE_TEMPLATE = "http://skynet.skhynix.com/portalWeb/uploadfile/pictures/{employee_id}.jpg"
_MODEL = TypeVar("_MODEL", bound=BaseModel)


def _auth_mode() -> str:
    """Choose mock mode until the HCP-only SSO package is deployed."""

    return str(os.getenv("PTMORE_PORTAL_FLASK_AUTH_MODE", "mock")).strip().lower() or "mock"


# The first local run should expose the same administrator-only screens as the
# original Portal preview.  This default exists only while the explicit Flask
# mock login is selected; an operator's configured administrator list always
# takes precedence, and SSO mode never receives this implicit default.
if _auth_mode() == "mock" and not os.getenv("PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON"):
    os.environ["PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON"] = (
        '[{"employee_id":"2069026","name":"문봉건"}]'
    )


def _mock_session_identity() -> None:
    """Populate exactly the temporary Flask session identity requested by the user."""

    session["emp_no"] = _MOCK_EMPLOYEE_ID
    session["emp_name"] = _MOCK_EMPLOYEE_NAME
    session["logFlag"] = True


def _current_flask_identity() -> portal_core.PortalIdentity | None:
    """Use only server-side Flask session values for Portal authorization."""

    employee_id = str(session.get("emp_no") or "").strip()
    employee_name = str(session.get("emp_name") or "").strip()
    if not _EMPLOYEE_ID_PATTERN.fullmatch(employee_id):
        return None
    return portal_core.PortalIdentity(
        employee_id=employee_id,
        name=employee_name or employee_id,
        source="flask_session",
    )


def _core_identity_from_flask_session(_: Any) -> portal_core.PortalIdentity | None:
    return _current_flask_identity()


# The Flask session is the only identity boundary in this application.  The
# copied Portal core is deliberately told to use it rather than LASTUSER,
# MongoDB employee-name lookup, request headers, or its legacy ASGI session.
portal_core._request_portal_identity = _core_identity_from_flask_session
# ``sso`` is the existing core's non-directory identity branch.  The Flask
# session above remains the actual source; this setting only prevents schedule
# saves from falling back to the old LASTUSER/MongoDB name-enrichment code.
portal_core._portal_auth_mode_override = "sso"


@dataclass
class _FlaskCoreRequest:
    """Small adapter for the unchanged Portal business-layer request contract."""

    headers: Any
    cookies: Any
    query_params: Any
    url: Any
    session: Any
    state: Any


def _core_request() -> _FlaskCoreRequest:
    return _FlaskCoreRequest(
        headers=request.headers,
        cookies=request.cookies,
        query_params=request.args,
        url=SimpleNamespace(netloc=request.host, path=request.path),
        session=session,
        state=SimpleNamespace(),
    )


def _call_core(handler: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Call an existing sync or async Portal handler from a Flask route."""

    result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _response_from_core(value: Any, *, status_code: int | None = None) -> Response:
    """Translate dicts and Starlette responses from the unchanged core layer."""

    if isinstance(value, Response):
        return value
    if hasattr(value, "body") and hasattr(value, "status_code") and hasattr(value, "headers"):
        return Response(
            response=bytes(value.body),
            status=int(value.status_code),
            headers=dict(value.headers),
        )
    response = jsonify(value)
    if status_code is not None:
        response.status_code = status_code
    return response


def _request_model(model_type: type[_MODEL]) -> _MODEL:
    payload = request.get_json(silent=True)
    if not isinstance(payload, Mapping):
        raise portal_core.HTTPException(
            status_code=422,
            detail={
                "code": "request_body_invalid",
                "message": "JSON 요청 본문을 입력해 주세요.",
            },
        )
    try:
        return model_type.model_validate(dict(payload))
    except ValidationError as exc:
        raise portal_core.HTTPException(
            status_code=422,
            detail={
                "code": "request_validation_error",
                "message": "입력값을 확인해 주세요.",
                "errors": exc.errors(include_url=False),
            },
        ) from exc


def _profile_image_url(employee_id: str) -> str:
    """Return the established intranet image URL for one validated employee ID."""

    safe_id = employee_id if _EMPLOYEE_ID_PATTERN.fullmatch(employee_id) else "0000000"
    return _PROFILE_IMAGE_TEMPLATE.format(employee_id=quote(safe_id, safe=""))


def _portal_payload() -> dict[str, Any]:
    payload = _call_core(portal_core.portal_data, _core_request())
    if not isinstance(payload, dict):
        return payload
    viewer = payload.get("viewer")
    if isinstance(viewer, dict):
        viewer["profile_image_url"] = _profile_image_url(str(viewer.get("employee_id") or ""))
    return payload


@app.before_request
def establish_mock_session() -> None:
    """Keep the requested local test identity stable on every browser request."""

    if _auth_mode() == "mock":
        _mock_session_identity()


# **********************
# Flask / HCP SSO login shape retained for the production switch.
# **********************
@app.route("/login")
@app.route("/login/<path:sub_path>")
def login(sub_path: str | None = None):
    if _auth_mode() != "sso":
        return redirect(_safe_local_path(sub_path))

    try:
        from hcputil.auth.sso import SSO
    except ImportError:
        return jsonify(
            detail={
                "code": "portal_sso_unavailable",
                "message": "HCP SSO 모듈을 불러올 수 없습니다. 운영 환경 설정을 확인해 주세요.",
            }
        ), 503

    sso = SSO(request)
    redirect_url = str(getattr(sso, "redirect_url", "") or "") + url_path
    if sub_path is not None:
        redirect_url = redirect_url + sub_path

    if session.get("logFlag") is not True:
        cookie = request.headers.get("cookie")
        if cookie is not None and sso.check_day_cookie(cookie) is True:
            values = sso.get_sso_info(cookie)
            session["emp_no"], session["emp_name"], session["emp_name_en"], session["dept"], session["email"], session["dept_cd"] = values
            session["logFlag"] = True
        return redirect(redirect_url)
    return redirect(redirect_url)


def _safe_local_path(sub_path: str | None = None) -> str:
    candidate = str(request.args.get("ORIGIN") or request.args.get("next") or sub_path or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return "/"
    return candidate


@app.route("/")
def main():
    if _auth_mode() == "sso" and session.get("logFlag") is not True:
        return redirect(url_for("login", ORIGIN=request.url))
    return render_template("sample.html")


@app.route("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
    return Response(status=204)


@app.route("/health")
def health():
    return _response_from_core(_call_core(portal_core.health))


@app.route("/api/portal")
def portal_data():
    return _response_from_core(_portal_payload())


@app.route("/api/schedules")
def list_schedules():
    return _response_from_core(_call_core(portal_core.list_schedules, _core_request()))


@app.route("/api/schedules/<schedule_id>")
def get_schedule(schedule_id: str):
    return _response_from_core(_call_core(portal_core.get_schedule, schedule_id, _core_request()))


@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    return _response_from_core(
        _call_core(portal_core.create_schedule, _request_model(portal_core.ScheduleCreateRequest), _core_request()),
        status_code=201,
    )


@app.route("/api/schedules/<schedule_id>", methods=["PATCH"])
def update_schedule(schedule_id: str):
    return _response_from_core(
        _call_core(portal_core.update_schedule, schedule_id, _request_model(portal_core.ScheduleUpdateRequest), _core_request())
    )


@app.route("/api/schedules/<schedule_id>/status", methods=["PATCH"])
def update_schedule_status(schedule_id: str):
    return _response_from_core(
        _call_core(portal_core.update_schedule_status, schedule_id, _request_model(portal_core.ScheduleStatusUpdateRequest), _core_request())
    )


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id: str):
    return _response_from_core(_call_core(portal_core.delete_schedule, schedule_id, _core_request()))


@app.route("/api/admin/settings")
def admin_settings():
    return _response_from_core(_call_core(portal_core.admin_settings, _core_request()))


@app.route("/api/settings/admins", methods=["POST"])
@app.route("/api/admin/settings/admins", methods=["POST"])
def create_portal_administrator():
    return _response_from_core(
        _call_core(
            portal_core.create_portal_administrator,
            _request_model(portal_core.PortalAdministratorCreateRequest),
            _core_request(),
        ),
        status_code=201,
    )


@app.route("/api/admin/settings/admins/<employee_id>", methods=["PATCH"])
def update_portal_administrator(employee_id: str):
    return _response_from_core(
        _call_core(
            portal_core.update_portal_administrator,
            employee_id,
            _request_model(portal_core.PortalAdministratorUpdateRequest),
            _core_request(),
        )
    )


@app.route("/api/settings/admins/<employee_id>", methods=["DELETE"])
@app.route("/api/admin/settings/admins/<employee_id>", methods=["DELETE"])
def delete_portal_administrator(employee_id: str):
    return _response_from_core(_call_core(portal_core.delete_portal_administrator, employee_id, _core_request()))


@app.route("/api/admin/settings", methods=["PUT"])
def update_admin_settings():
    return _response_from_core(
        _call_core(portal_core.update_admin_settings, _request_model(portal_core.PortalSettingsUpdateRequest), _core_request())
    )


@app.route("/api/metadata-authoring/status")
def metadata_authoring_status():
    return _response_from_core(_call_core(portal_core.metadata_authoring_status, _core_request()))


@app.route("/api/metadata/live")
def live_metadata():
    return _response_from_core(_call_core(portal_core.live_metadata, _core_request()))


@app.route("/api/metadata/live/<metadata_type>/<record_id>")
def live_metadata_detail(metadata_type: str, record_id: str):
    return _response_from_core(_call_core(portal_core.live_metadata_detail, metadata_type, record_id, _core_request()))


@app.route("/api/metadata-authoring/<metadata_type>/<record_id>/status", methods=["PATCH"])
def update_live_metadata_record_status(metadata_type: str, record_id: str):
    return _response_from_core(
        _call_core(
            portal_core.update_live_metadata_record_status,
            metadata_type,
            record_id,
            _request_model(portal_core.MetadataStatusUpdateRequest),
            _core_request(),
        )
    )


@app.route("/api/metadata-authoring", methods=["POST"])
def submit_metadata_authoring():
    return _response_from_core(
        _call_core(portal_core.submit_metadata_authoring, _request_model(portal_core.MetadataAuthoringRequest), _core_request())
    )


@app.route("/api/dashboard/usage")
def dashboard_usage_data():
    return _response_from_core(_call_core(portal_core.dashboard_usage_data, _core_request()))


@app.route("/api/dashboard/usage/refresh", methods=["POST"])
def dashboard_usage_full_refresh():
    return _response_from_core(_call_core(portal_core.dashboard_usage_full_refresh, _core_request()))


@app.route("/api/dashboard/usage/export.csv")
def dashboard_usage_export_csv():
    return _response_from_core(
        _call_core(
            portal_core.dashboard_usage_export_csv,
            _core_request(),
            start_date=request.args.get("start_date"),
            end_date=request.args.get("end_date"),
            scope=request.args.get("scope", "recent"),
        )
    )


@app.errorhandler(portal_core.HTTPException)
def portal_http_exception(error: portal_core.HTTPException):
    """Preserve the browser's existing FastAPI-style ``detail`` error contract."""

    return jsonify(detail=error.detail), int(error.status_code)


@app.errorhandler(ValidationError)
def pydantic_validation_exception(error: ValidationError):
    return jsonify(detail={"code": "request_validation_error", "message": "입력값을 확인해 주세요.", "errors": error.errors(include_url=False)}), 422


# ``index.py`` imports this module-level Flask object as ``application``.
# Keep this module to Flask setup and route functions; its execution block is
# intentionally located in the supplied ``index.py`` entry point.
