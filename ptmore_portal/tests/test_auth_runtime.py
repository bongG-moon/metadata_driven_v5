"""Runtime identity adapter tests for the Portal.

The normal production adapter reads only the internal ``LASTUSER`` browser
cookie.  The legacy HCP SSO adapter remains explicit, while ``app_local``
always supplies the fixed local test identity.  Browser-provided employee
headers must never replace either production identity source.
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

    encoded_headers = json.dumps(headers or {}, ensure_ascii=True)
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
    '/api/portal', headers=json.loads({encoded_headers!r})
)
print('__PORTAL_RUNTIME_RESULT__=' + json.dumps({{
    'status_code': response.status_code,
    'body': response.json(),
}}, ensure_ascii=True))
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


def _lastuser_runtime_request(
    *,
    mode: str = "lastuser",
    cookie: str = "",
    headers: dict[str, str] | None = None,
    directory_names: dict[str, str] | None = None,
    lookup_error: bool = False,
) -> dict:
    """Run the LASTUSER adapter with an isolated, in-memory name directory.

    The real adapter obtains the Korean name from MongoDB.  This subprocess
    injects the same narrow directory boundary without connecting to a real
    deployment database or importing the HCP-only SSO package.
    """

    request_headers = dict(headers or {})
    if cookie:
        request_headers["cookie"] = cookie
    encoded_headers = json.dumps(request_headers, ensure_ascii=True)
    encoded_names = json.dumps(directory_names or {}, ensure_ascii=True)
    script = f"""
import builtins
import json

original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'hcputil' or name.startswith('hcputil.'):
        raise AssertionError('hcputil SSO must not be imported for LASTUSER authentication')
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import

from fastapi.testclient import TestClient
import app

directory_names = json.loads({encoded_names!r})
request_headers = json.loads({encoded_headers!r})

class FakeDirectory:
    def __init__(self):
        self.calls = []

    def resolve_name(self, employee_id):
        self.calls.append(employee_id)
        if {lookup_error!r}:
            raise app.EmployeeDirectoryStoreError('directory temporarily unavailable')
        return directory_names.get(employee_id)

    def close(self):
        return None

class FakeSettingsStore:
    persistent = False

    def read(self):
        return app._default_portal_settings()

    def update(self, update, actor):
        raise AssertionError('this identity test must not mutate settings')

    def record_audit(self, action, actor, details):
        return None

class FakeRunReader:
    def list_recent_runs(self, limit):
        return []

    def close(self):
        return None

directory = FakeDirectory()
app._employee_directory_store_factory = lambda: directory
app._portal_settings_store_factory = lambda: FakeSettingsStore()
app._portal_schedule_run_reader_factory = lambda: FakeRunReader()

response = TestClient(app.application).get('/api/portal', headers=request_headers)
print('__PORTAL_LASTUSER_RESULT__=' + json.dumps({{
    'status_code': response.status_code,
    'body': response.json(),
    'directory_calls': directory.calls,
}}, ensure_ascii=True))
"""
    env = os.environ.copy()
    env.update(
        {
            "PTMORE_PORTAL_AUTH_MODE": mode,
            "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON": json.dumps(
                [{"employee_id": "2069026", "name": "Portal Administrator"}],
                ensure_ascii=True,
            ),
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
        "isolated LASTUSER request failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    marker = "__PORTAL_LASTUSER_RESULT__="
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise AssertionError(f"LASTUSER runtime response marker is missing: {completed.stdout!r}")


def test_production_portal_uses_lastuser_cookie_and_ignores_browser_identity_headers() -> None:
    """A valid LASTUSER cookie wins over forged browser identity headers."""

    response = _lastuser_runtime_request(
        mode="production",
        cookie="LASTUSER=2069026",
        headers={
            "X-PTMORE-Employee-Id": "9999999",
            "X-PTMORE-Employee-Name": "Forged Browser User",
            "X-PTMORE-Is-Admin": "true",
        },
        directory_names={"2069026": "문봉건"},
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "2069026"
    assert viewer["name"] == "문봉건"
    assert viewer["is_admin"] is True
    assert response["directory_calls"] == ["2069026"]


def test_lastuser_adapter_uses_unknown_identity_when_cookie_is_missing() -> None:
    """No cookie must not allow forged headers to select a Portal account."""

    response = _lastuser_runtime_request(
        headers={
            "X-PTMORE-Employee-Id": "2069026",
            "X-PTMORE-Employee-Name": "Forged Administrator",
        },
        directory_names={"2069026": "문봉건"},
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "0000000"
    assert viewer["name"] == "아무개"
    assert viewer["is_admin"] is False
    assert response["directory_calls"] == []


def test_lastuser_adapter_rejects_invalid_employee_number_format() -> None:
    """Only a seven-digit LASTUSER value is accepted as an employee number."""

    response = _lastuser_runtime_request(
        cookie="LASTUSER=not-a-seven-digit-number",
        directory_names={"2069026": "문봉건"},
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "0000000"
    assert viewer["name"] == "아무개"
    assert response["directory_calls"] == []


def test_lastuser_adapter_keeps_valid_employee_number_and_blank_name_when_directory_is_unavailable() -> None:
    """A missing name mapping must not replace a valid cookie identity."""

    response = _lastuser_runtime_request(
        cookie="LASTUSER=2071044",
        lookup_error=True,
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "2071044"
    assert viewer["name"] == ""
    assert viewer["is_admin"] is False
    assert response["directory_calls"] == ["2071044"]


def test_lastuser_adapter_keeps_valid_employee_number_and_blank_name_when_mapping_is_absent() -> None:
    """An empty MongoDB result is displayed as an empty name, not a placeholder."""

    response = _lastuser_runtime_request(
        cookie="LASTUSER=2071044",
        directory_names={},
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "2071044"
    assert viewer["name"] == ""
    assert viewer["is_admin"] is False
    assert response["directory_calls"] == ["2071044"]


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


def test_test_adapter_keeps_explicit_fixture_headers_after_lastuser_is_added() -> None:
    """The automated test adapter remains separate from browser cookies."""

    response = _runtime_request(
        module_name="app",
        mode="test",
        headers={
            "X-PTMORE-Employee-Id": "2071044",
            "X-PTMORE-Employee-Name": "Fixture User",
            "cookie": "LASTUSER=2069026",
        },
    )

    assert response["status_code"] == 200
    viewer = response["body"]["viewer"]
    assert viewer["employee_id"] == "2071044"
    assert viewer["name"] == "Fixture User"
    assert viewer["is_admin"] is False


def test_production_health_is_public_even_when_no_sso_session_exists() -> None:
    """Health monitoring must not need an interactive Portal identity."""

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


def test_legacy_sso_login_creates_a_session_for_the_verified_employee() -> None:
    """The explicit legacy ``sso`` mode keeps its signed-session behavior.

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
            "PTMORE_PORTAL_AUTH_MODE": "sso",
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
