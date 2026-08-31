"""Runtime identity split tests for the Portal.

The production entry point deliberately uses server-side SSO identity only.
``app_local`` is a separate developer entry point that always supplies the
fixed local test identity, so browser-provided employee headers are never an
authentication mechanism in either mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _runtime_request(*, module_name: str, mode: str, headers: dict[str, str] | None = None) -> dict:
    """Run a request in a fresh Python process without HCP-only dependencies.

    ``test_app.py`` imports the Portal in its explicit ``test`` identity mode.
    A subprocess prevents a production/local import here from mutating that
    shared module object or its identity adapter.
    """

    request_headers = headers or {}
    script = f"""
import builtins
import json

original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'hcputil' or name.startswith('hcputil.'):
        raise AssertionError('hcputil SSO must not be imported for this isolated test')
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import

from fastapi.testclient import TestClient
runtime_module = __import__({module_name!r})
response = TestClient(runtime_module.application).get(
    '/api/portal', headers={request_headers!r}
)
print('__PORTAL_RUNTIME_RESULT__=' + json.dumps({{
    'status_code': response.status_code,
    'body': response.json(),
}}, ensure_ascii=False))
"""
    env = os.environ.copy()
    env["PTMORE_PORTAL_AUTH_MODE"] = mode
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "isolated runtime request failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    marker = "__PORTAL_RUNTIME_RESULT__="
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise AssertionError(f"runtime response marker is missing: {completed.stdout!r}")


def test_production_portal_rejects_browser_identity_headers_without_sso_session() -> None:
    """A forged employee header must not act as production authentication.

    No cookie is supplied, so the production adapter must reject the request
    before attempting to import/call the HCP-only SSO module.  This keeps the
    test executable on a developer PC and proves header-only identity is not
    trusted.
    """

    response = _runtime_request(
        module_name="app",
        mode="production",
        headers={
            "X-PTMORE-Employee-Id": "9999999",
            "X-PTMORE-Employee-Name": "Forged Browser User",
            "X-PTMORE-Is-Admin": "true",
        },
    )

    assert response["status_code"] == 401
    assert response["body"]["detail"]["code"] == "portal_identity_required"


def test_local_portal_always_uses_fixed_developer_identity() -> None:
    """The local entry point must not depend on cookies or browser ID headers."""

    response = _runtime_request(
        module_name="app_local",
        mode="local",
        headers={
            "X-PTMORE-Employee-Id": "9999999",
            "X-PTMORE-Employee-Name": "Forged Browser User",
        },
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "2011111"
    assert viewer["name"] == "문봉건"
    assert viewer["is_admin"] is True
    assert viewer["role"] == "관리자"
    assert any(
        admin["employee_id"] == "2011111" and admin["status"] == "활성"
        for admin in response["body"]["settings"]["admins"]
    )


def test_local_identity_does_not_change_when_browser_headers_change() -> None:
    """Local mode is deterministic and intentionally ignores identity headers."""

    first = _runtime_request(module_name="app_local", mode="local")
    second = _runtime_request(
        module_name="app_local",
        mode="local",
        headers={
            "X-PTMORE-Employee-Id": "0000000",
            "X-PTMORE-Employee-Name": "Another Forged User",
        },
    )

    assert first["status_code"] == second["status_code"] == 200
    assert first["body"]["viewer"] == second["body"]["viewer"]


def test_production_health_is_public_even_when_no_sso_session_exists() -> None:
    """Health monitoring must not need an interactive SSO session."""

    script = """
import builtins
import json
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'hcputil' or name.startswith('hcputil.'):
        raise AssertionError('hcputil SSO must not be imported for this isolated test')
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
from fastapi.testclient import TestClient
import app
response = TestClient(app.application).get('/health')
print('__PORTAL_HEALTH_RESULT__=' + json.dumps({
    'status_code': response.status_code,
    'body': response.json(),
}, ensure_ascii=False))
"""
    env = os.environ.copy()
    env["PTMORE_PORTAL_AUTH_MODE"] = "production"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    line = next(
        line
        for line in reversed(completed.stdout.splitlines())
        if line.startswith("__PORTAL_HEALTH_RESULT__=")
    )
    response = json.loads(line.split("=", 1)[1])
    assert response["status_code"] == 200
    assert response["body"]["status"] == "ok"


def test_production_sso_login_creates_a_session_for_the_verified_employee() -> None:
    """Production login must use ``SSO`` values, then reuse a signed session.

    The fake package is registered only inside a fresh subprocess.  This
    exercises the real lazy import and cookie/session flow without requiring
    the HCP-only package on a developer PC or exposing a real SSO cookie.
    """

    script = """
import json
import sys
import types

calls = []

class FakeSSO:
    def __init__(self, request):
        calls.append({"method": "init", "path": request.url.path})
        self.redirect_url = "https://sso.example.test/login"

    def check_day_cookie(self, cookie):
        calls.append({"method": "check_day_cookie", "cookie": cookie})
        return cookie == "day-cookie=valid"

    def get_sso_info(self, cookie):
        calls.append({"method": "get_sso_info", "cookie": cookie})
        return (
            "2099999",
            "SSO 테스트 사용자",
            "SSO Test User",
            "PKG 개발팀",
            "sso-user@example.test",
            "PKG-DEV",
        )

hcputil = types.ModuleType("hcputil")
hcputil.__path__ = []
auth = types.ModuleType("hcputil.auth")
auth.__path__ = []
sso = types.ModuleType("hcputil.auth.sso")
sso.SSO = FakeSSO
sys.modules.update({
    "hcputil": hcputil,
    "hcputil.auth": auth,
    "hcputil.auth.sso": sso,
})

from fastapi.testclient import TestClient
import app

client = TestClient(app.application)
login_response = client.get(
    "/login?ORIGIN=/api/portal",
    headers={"cookie": "day-cookie=valid"},
    follow_redirects=False,
)
portal_response = client.get(
    "/api/portal",
    headers={
        "X-PTMORE-Employee-Id": "9999999",
        "X-PTMORE-Employee-Name": "Forged Browser User",
    },
)
print("__PORTAL_SSO_LOGIN_RESULT__=" + json.dumps({
    "login_status": login_response.status_code,
    "login_location": login_response.headers.get("location"),
    "portal_status": portal_response.status_code,
    "portal_body": portal_response.json(),
    "calls": calls,
}, ensure_ascii=False))
"""
    env = os.environ.copy()
    env.update(
        {
            "PTMORE_PORTAL_AUTH_MODE": "production",
            "PTMORE_SSO_SESSION_SECRET": "test-only-session-secret",
            # TestClient uses HTTP by default; production remains HTTPS-only
            # unless the deployment explicitly changes this setting.
            "PTMORE_SSO_SESSION_HTTPS_ONLY": "false",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "isolated SSO login request failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    marker = "__PORTAL_SSO_LOGIN_RESULT__="
    line = next(
        line
        for line in reversed(completed.stdout.splitlines())
        if line.startswith(marker)
    )
    result = json.loads(line[len(marker) :])

    assert result["login_status"] == 307
    assert result["login_location"] == "/api/portal"
    assert result["portal_status"] == 200
    viewer = result["portal_body"]["viewer"]
    assert viewer["employee_id"] == "2099999"
    assert viewer["name"] == "SSO 테스트 사용자"
    assert any(call["method"] == "check_day_cookie" for call in result["calls"])
    assert any(call["method"] == "get_sso_info" for call in result["calls"])
