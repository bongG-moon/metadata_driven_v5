"""Focused, offline contract tests for the Flask Portal migration.

The Flask layer must be usable before a real MongoDB, Phoenix, or HCP SSO
runtime is available. These tests deliberately replace those boundaries with
small in-memory doubles and exercise only Flask's test client.
"""

from __future__ import annotations

import builtins
import copy
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Mapping

import pytest


FLASK_ROOT = Path(__file__).resolve().parents[1]
if str(FLASK_ROOT) not in sys.path:
    sys.path.insert(0, str(FLASK_ROOT))


def _load_flask_portal_module():
    """Load the canonical Flask WebApp module used by ``index.py``."""

    module_name = "web_main"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    return importlib.import_module(module_name)


flask_portal = _load_flask_portal_module()
portal_core = flask_portal.portal_core
application = flask_portal.app


def _load_flask_index_module():
    """Import the HCP WebApp entry module without executing its server block."""

    return importlib.import_module("index")


def _load_runtime_settings_module():
    """Load the tracked runtime-setting resolver on demand."""

    return importlib.import_module("runtime_settings")


def _install_private_runtime_settings(
    monkeypatch,
    settings: Mapping[str, Any] | None = None,
    direct_values: Mapping[str, Any] | None = None,
):
    """Inject the deployment-only private setting module for one test."""

    private_config = ModuleType("portal_runtime_config")
    if settings is not None:
        private_config.SETTINGS = dict(settings)
    for name, value in (direct_values or {}).items():
        setattr(private_config, str(name), value)
    monkeypatch.setitem(sys.modules, "portal_runtime_config", private_config)

    runtime_settings = _load_runtime_settings_module()
    runtime_settings.reset_runtime_settings_cache()
    return runtime_settings


class FakePortalSettingsStore:
    """Settings-only double: no real MongoDB client is ever constructed."""

    persistent = True

    def __init__(self) -> None:
        self.settings = portal_core._default_portal_settings()
        self.audit_records: list[dict[str, Any]] = []

    def read(self) -> dict[str, Any]:
        return copy.deepcopy(self.settings)

    def update(self, update: Mapping[str, Any], actor: Any) -> dict[str, Any]:
        self.settings.update(copy.deepcopy(dict(update)))
        self.record_audit("settings_updated", actor, {"update": dict(update)})
        return self.read()

    def record_audit(self, action: str, actor: Any, details: Mapping[str, Any]) -> None:
        self.audit_records.append(
            {
                "action": action,
                "actor": actor.as_audit_actor(),
                "details": copy.deepcopy(dict(details)),
            }
        )


class FakeScheduleRunReader:
    """Prevents the dashboard bootstrap panel from opening MongoDB."""

    persistent = False

    def list_recent_runs(self, limit: int) -> list[dict[str, Any]]:
        assert limit > 0
        return []

    def close(self) -> None:
        return None


class FakeScheduleStore:
    """Minimal CRUD source-store double for the Flask schedule routes."""

    persistent = True

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.closed = 0

    def list_schedules(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(document) for document in self.documents.values()]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        document = self.documents.get(schedule_id)
        return copy.deepcopy(document) if document is not None else None

    def create_schedule(self, document: Mapping[str, Any]) -> dict[str, Any]:
        created = copy.deepcopy(dict(document))
        self.documents[str(created["_id"])] = created
        return copy.deepcopy(created)

    def update_schedule(
        self, schedule_id: str, update: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if schedule_id not in self.documents:
            return None
        self.documents[schedule_id].update(copy.deepcopy(dict(update)))
        return copy.deepcopy(self.documents[schedule_id])

    def delete_schedule(self, schedule_id: str) -> bool:
        return self.documents.pop(schedule_id, None) is not None

    def close(self) -> None:
        self.closed += 1


def _unexpected_employee_directory_store() -> Any:
    """Flask session identity must never trigger the old MongoDB name lookup."""

    raise AssertionError("Flask 스케줄 저장에서 직원 이름 MongoDB 조회를 하면 안 됩니다.")


@pytest.fixture(autouse=True)
def isolated_flask_portal_runtime(monkeypatch):
    """Keep every migration test independent from local .env configuration."""

    for name in (
        "MONGODB_URI",
        "MONGODB_DATABASE",
        "PTMORE_PORTAL_AUTH_MODE",
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        "PTMORE_PHOENIX_ENDPOINT",
        "PTMORE_PHOENIX_API_KEY",
        "PTMORE_PHOENIX_PROJECTS_JSON",
        "PTMORE_PHOENIX_PROJECT_ID",
        "PTMORE_METADATA_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    # This is the Flask-local identity requested for the first deployment
    # check. It is intentionally established server-side in session, rather
    # than taken from any browser request header or cookie.
    monkeypatch.setenv("PTMORE_PORTAL_FLASK_AUTH_MODE", "mock")
    monkeypatch.setenv(
        "PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON",
        json.dumps([{"employee_id": "2069026", "name": "문봉건"}]),
    )

    store = FakePortalSettingsStore()
    monkeypatch.setattr(portal_core, "_portal_settings_store_factory", lambda: store)
    monkeypatch.setattr(
        portal_core,
        "_portal_schedule_run_reader_factory",
        lambda: FakeScheduleRunReader(),
    )
    yield store


@pytest.fixture
def client():
    application.config.update(TESTING=True)
    with application.test_client() as test_client:
        yield test_client


def test_index_renders_existing_portal_ui_and_employee_photo_marker(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "PTMORE PKG Agent Portal" in page
    assert 'id="my_face"' in page
    assert "http://skynet.skhynix.com/portalWeb/uploadfile/pictures/2069026.jpg" in page
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_index_exports_the_exact_web_main_flask_application() -> None:
    index = _load_flask_index_module()

    assert index.application is flask_portal.app
    assert index.application is application


def test_flask_entrypoint_imports_and_renders_without_python_dotenv(
    monkeypatch,
) -> None:
    """HCP Flask deployment must not require an optional local .env loader.

    Import the two actual WebApp entry modules from scratch while blocking every
    ``dotenv`` import.  This catches a regression where either ``web_main`` or
    its reused Portal business layer adds ``from dotenv import ...`` again.
    The page request is deliberately limited to ``/`` so no MongoDB, Phoenix,
    or SSO service can be contacted.
    """

    original_import = builtins.__import__
    module_names = ("index", "web_main", "portal_core")
    previous_modules = {name: sys.modules.pop(name, None) for name in module_names}

    def block_dotenv_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "dotenv" or name.startswith("dotenv."):
            raise ModuleNotFoundError("No module named 'dotenv'", name="dotenv")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", block_dotenv_import)
    try:
        fresh_web_main = importlib.import_module("web_main")
        fresh_index = importlib.import_module("index")

        fresh_web_main.app.config.update(TESTING=True)
        with fresh_web_main.app.test_client() as fresh_client:
            response = fresh_client.get("/")

        assert fresh_index.application is fresh_web_main.app
        assert response.status_code == 200
        assert "PTMORE PKG Agent Portal" in response.get_data(as_text=True)
    finally:
        for name in module_names:
            sys.modules.pop(name, None)
        for name, module in previous_modules.items():
            if module is not None:
                sys.modules[name] = module


def test_runtime_settings_use_private_python_value_when_environment_is_missing(
    monkeypatch,
) -> None:
    """A deployment may supply safe non-secret defaults in private Python config."""

    runtime_settings = _install_private_runtime_settings(
        monkeypatch,
        {"PTMORE_TEST_RUNTIME_SETTING": "private-default"},
    )
    setting_name = "PTMORE_TEST_RUNTIME_SETTING"
    monkeypatch.delenv(setting_name, raising=False)

    assert runtime_settings.get_setting(setting_name, "fallback") == "private-default"


def test_runtime_settings_prioritize_nonempty_environment_over_private_python_value(
    monkeypatch,
) -> None:
    """HCP Secret/process settings must override a private Python safe default."""

    runtime_settings = _install_private_runtime_settings(
        monkeypatch,
        {"PTMORE_TEST_RUNTIME_SETTING": "private-default"},
    )
    setting_name = "PTMORE_TEST_RUNTIME_SETTING"
    monkeypatch.setenv(setting_name, "hcp-secret-value")

    assert runtime_settings.get_setting(setting_name, "fallback") == "hcp-secret-value"

    monkeypatch.setenv(setting_name, "")
    assert runtime_settings.get_setting(setting_name, "fallback") == "private-default"


def test_runtime_settings_direct_variable_overrides_duplicate_settings_mapping(
    monkeypatch,
) -> None:
    """The easy-to-edit uppercase Python variable wins over ``SETTINGS``."""

    setting_name = "PTMORE_TEST_RUNTIME_SETTING"
    runtime_settings = _install_private_runtime_settings(
        monkeypatch,
        {setting_name: "mapping-value"},
        {setting_name: "direct-variable-value"},
    )
    monkeypatch.delenv(setting_name, raising=False)

    assert runtime_settings.get_setting(setting_name, "fallback") == "direct-variable-value"


def test_runtime_settings_serialize_collection_values_and_fall_back_for_empty_values(
    monkeypatch,
) -> None:
    """List/dict settings remain usable while empty or absent values use defaults."""

    runtime_settings = _install_private_runtime_settings(
        monkeypatch,
        {
            "PTMORE_TEST_RUNTIME_COLLECTION": {
                "projects": ["agent-a", "agent-b"]
            },
            "PTMORE_TEST_RUNTIME_EMPTY": "",
        },
    )
    collection_name = "PTMORE_TEST_RUNTIME_COLLECTION"
    empty_name = "PTMORE_TEST_RUNTIME_EMPTY"
    monkeypatch.delenv(collection_name, raising=False)
    monkeypatch.delenv(empty_name, raising=False)

    assert json.loads(runtime_settings.get_setting(collection_name)) == {
        "projects": ["agent-a", "agent-b"]
    }
    assert runtime_settings.get_setting(empty_name, "fallback") == "fallback"
    assert runtime_settings.get_setting("PTMORE_TEST_RUNTIME_MISSING", "fallback") == "fallback"


def test_runtime_settings_fall_back_when_private_module_or_mapping_is_absent(
    monkeypatch,
) -> None:
    """The tracked app must still boot without a deployment-only config file."""

    runtime_settings = _load_runtime_settings_module()
    monkeypatch.delitem(sys.modules, "portal_runtime_config", raising=False)
    runtime_settings.reset_runtime_settings_cache()

    assert runtime_settings.get_setting("PTMORE_TEST_RUNTIME_MISSING", "fallback") == "fallback"

    runtime_settings = _install_private_runtime_settings(monkeypatch)
    assert runtime_settings.get_setting("PTMORE_TEST_RUNTIME_MISSING", "fallback") == "fallback"


def test_private_python_settings_configure_phoenix_and_usage_archive_factories(
    monkeypatch,
) -> None:
    """Phoenix/archive factories accept the same private Python settings path."""

    configured_values = {
        "MONGODB_URI": "mongodb://portal-user:secret@mongo.internal:27017/?authSource=admin",
        "MONGODB_DATABASE": "ptmore_portal",
        "PTMORE_USAGE_HISTORY_COLLECTION": "portal_usage_history",
        "PTMORE_PHOENIX_ENDPOINT": "https://phoenix.internal.example",
        "PTMORE_PHOENIX_API_KEY": "private-phoenix-key",
        "PTMORE_PHOENIX_PROJECTS_JSON": ["pkg-agent", "pkg-agent-scheduling"],
    }
    for name in configured_values:
        monkeypatch.delenv(name, raising=False)
    runtime_settings = _install_private_runtime_settings(monkeypatch, configured_values)
    monkeypatch.setattr(portal_core, "_phoenix_usage_config_factory", None)

    archive_config = portal_core._usage_history_archive_config_from_env()
    phoenix_config = portal_core._phoenix_usage_config_from_env()

    assert archive_config.uri == configured_values["MONGODB_URI"]
    assert archive_config.database == configured_values["MONGODB_DATABASE"]
    assert archive_config.collection == configured_values["PTMORE_USAGE_HISTORY_COLLECTION"]
    assert phoenix_config.endpoint == configured_values["PTMORE_PHOENIX_ENDPOINT"]
    assert phoenix_config.api_key == configured_values["PTMORE_PHOENIX_API_KEY"]
    assert phoenix_config.projects == ("pkg-agent", "pkg-agent-scheduling")
    assert phoenix_config.is_configured is True

    # This assertion documents that no factory call above touches either
    # Phoenix or MongoDB; configuration parsing is intentionally offline.
    assert runtime_settings.get_setting("PTMORE_PHOENIX_API_KEY") == "private-phoenix-key"


def test_metadata_authoring_requires_type_specific_urls_and_uses_one_auth_method(
    monkeypatch,
) -> None:
    """Legacy common URL/header variants must not affect Flow requests.

    Each authoring type owns one Flow endpoint.  The Portal keeps the familiar
    editable ``header + key`` authentication pair, but it must not silently
    append a bearer token or arbitrary extra headers from old deployments.
    """

    configured_values = {
        # This retired setting must never become a fallback endpoint.
        "PTMORE_METADATA_API_URL": "https://legacy.example/common-flow",
        "PTMORE_METADATA_TABLE_CATALOG_API_URL": "https://flow.example/table",
        "PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL": "https://flow.example/filter",
        # Deliberately leave domain empty to prove the common URL is ignored.
        "PTMORE_METADATA_DOMAIN_API_URL": "",
        "PTMORE_METADATA_API_AUTH_HEADER": "X-Gaia-Auth-Key",
        "PTMORE_METADATA_API_AUTH_KEY": "metadata-key",
        # Retired configuration values must have no effect even when an old
        # private config file still contains them.
        "PTMORE_METADATA_API_BEARER_TOKEN": "legacy-bearer-token",
        "PTMORE_METADATA_API_EXTRA_HEADERS_JSON": {"X-Legacy-Header": "legacy"},
        "PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON": {
            "domain": {"request_loader": "legacy-component-name"}
        },
    }
    for name in configured_values:
        monkeypatch.delenv(name, raising=False)
    _install_private_runtime_settings(monkeypatch, configured_values)

    settings = portal_core._metadata_settings_from_env()

    assert settings.endpoint_for("table_catalog") == "https://flow.example/table"
    assert settings.endpoint_for("main_flow_filters") == "https://flow.example/filter"
    assert settings.endpoint_for("domain") == ""
    assert settings.endpoint_source_for("table_catalog") == "type_specific_url"
    assert settings.endpoint_source_for("domain") == "not_configured"

    headers = portal_core._metadata_api_headers(
        settings,
        gaia_api_caller_employee_id="2069026",
    )
    assert headers == {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "X-Gaia-Auth-Key": "metadata-key",
        "X-Gaia-User-Id": "2069026",
    }


def test_metadata_authoring_status_does_not_expose_retired_auth_or_component_settings(
    monkeypatch,
) -> None:
    """The status API must match the simplified user-editable configuration."""

    configured_values = {
        "PTMORE_METADATA_TABLE_CATALOG_API_URL": "https://flow.example/table",
        "PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL": "https://flow.example/filter",
        "PTMORE_METADATA_DOMAIN_API_URL": "https://flow.example/domain",
        "PTMORE_METADATA_API_AUTH_HEADER": "X-Gaia-Auth-Key",
        "PTMORE_METADATA_API_AUTH_KEY": "metadata-key",
        "PTMORE_METADATA_API_BEARER_TOKEN": "legacy-bearer-token",
        "PTMORE_METADATA_API_EXTRA_HEADERS_JSON": {"X-Legacy-Header": "legacy"},
        "PTMORE_METADATA_FLOW_COMPONENT_MAP_JSON": {
            "domain": {"request_loader": "legacy-component-name"}
        },
    }
    for name in configured_values:
        monkeypatch.delenv(name, raising=False)
    _install_private_runtime_settings(monkeypatch, configured_values)

    status_payload = portal_core._metadata_api_status(
        portal_core._metadata_settings_from_env()
    )

    api_status = status_payload["api"]
    assert status_payload["all_metadata_types_ready"] is True
    assert api_status["auth_key_configured"] is True
    assert "bearer_token_configured" not in api_status
    assert "component_map_configured" not in api_status
    assert "api_terminal_configured" not in api_status


def test_metadata_authoring_never_forwards_portal_mongodb_settings_as_flow_tweaks(
    monkeypatch,
) -> None:
    """Flow requests must remain independent of Portal MongoDB deployment data.

    Older private configuration files can still contain the retired mapping and
    switch.  They must not add credentials, database names, or collection names
    to a Langflow request body; each Flow owns its own MongoDB runtime setup.
    """

    configured_values = {
        "MONGODB_URI": "mongodb://portal-user:portal-secret@mongo.internal:27017/?authSource=admin",
        "MONGODB_DATABASE": "portal_runtime_database",
        "MONGODB_COLLECTION_PREFIX": "portal_",
        "PTMORE_METADATA_TABLE_CATALOG_API_URL": "https://flow.example/table",
        "PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL": "https://flow.example/filter",
        "PTMORE_METADATA_DOMAIN_API_URL": "https://flow.example/domain",
        # Retired values deliberately contain unmistakable names.  This makes
        # it impossible for a Flow payload to accidentally use them unnoticed.
        "PTMORE_METADATA_MONGODB_COLLECTION_MAP_JSON": {
            "domain": "legacy_domain_collection",
            "table_catalog": "legacy_catalog_collection",
            "main_flow_filters": "legacy_filter_collection",
        },
        "PTMORE_METADATA_SEND_MONGODB_TWEAKS": True,
    }
    for name in configured_values:
        monkeypatch.delenv(name, raising=False)
    _install_private_runtime_settings(monkeypatch, configured_values)

    settings = portal_core._metadata_settings_from_env()
    payload = portal_core._metadata_api_payload(
        settings,
        metadata_type="domain",
        raw_text="DA 공정 그룹을 등록합니다.",
        duplicate_action="skip",
        dry_run=True,
    )
    serialized_payload = json.dumps(payload, ensure_ascii=False)

    assert payload["tweaks"]
    assert "duplicate_action" in serialized_payload
    assert "dry_run" in serialized_payload
    assert "mongo_uri" not in serialized_payload
    assert "mongo_database" not in serialized_payload
    assert "collection_name" not in serialized_payload
    assert "table_collection_name" not in serialized_payload
    assert "filter_collection_name" not in serialized_payload
    assert "domain_collection_name" not in serialized_payload
    assert "portal-secret" not in serialized_payload
    assert "portal_runtime_database" not in serialized_payload
    assert "legacy_domain_collection" not in serialized_payload
    assert "legacy_catalog_collection" not in serialized_payload
    assert "legacy_filter_collection" not in serialized_payload


def test_metadata_authoring_status_hides_retired_portal_to_flow_mongodb_tweaks(
    monkeypatch,
) -> None:
    """Status may show read-only sources, but never Flow MongoDB tweak state."""

    configured_values = {
        "MONGODB_URI": "mongodb://portal-user:portal-secret@mongo.internal:27017/?authSource=admin",
        "MONGODB_DATABASE": "portal_runtime_database",
        "PTMORE_METADATA_TABLE_CATALOG_API_URL": "https://flow.example/table",
        "PTMORE_METADATA_MAIN_FLOW_FILTER_API_URL": "https://flow.example/filter",
        "PTMORE_METADATA_DOMAIN_API_URL": "https://flow.example/domain",
        "PTMORE_METADATA_MONGODB_COLLECTION_MAP_JSON": {
            "domain": "legacy_domain_collection"
        },
        "PTMORE_METADATA_SEND_MONGODB_TWEAKS": True,
    }
    for name in configured_values:
        monkeypatch.delenv(name, raising=False)
    _install_private_runtime_settings(monkeypatch, configured_values)

    status_payload = portal_core._metadata_api_status(
        portal_core._metadata_settings_from_env()
    )

    for metadata_type_status in status_payload["metadata_types"].values():
        assert "writer_tweak_configured" not in metadata_type_status
        assert "writer_tweak_will_be_sent" not in metadata_type_status
        assert "snapshot_tweak_configured" not in metadata_type_status
        assert "snapshot_tweak_will_be_sent" not in metadata_type_status

    flow_mongodb_status = status_payload.get("flow_metadata_mongodb", {})
    assert "tweaks_enabled" not in flow_mongodb_status
    assert "writer_tweaks_configured" not in flow_mongodb_status


def test_flask_auth_mode_defaults_to_sso_without_private_or_process_setting(
    monkeypatch,
) -> None:
    """A missing setting must never silently establish the mock administrator."""

    runtime_settings = _load_runtime_settings_module()
    monkeypatch.delenv("PTMORE_PORTAL_FLASK_AUTH_MODE", raising=False)
    monkeypatch.delenv("PTMORE_PORTAL_BOOTSTRAP_ADMINS_JSON", raising=False)
    monkeypatch.delitem(sys.modules, "portal_runtime_config", raising=False)
    runtime_settings.reset_runtime_settings_cache()

    assert flask_portal._auth_mode() == "sso"

    application.config.update(TESTING=True)
    with application.test_client() as unauthenticated_client:
        response = unauthenticated_client.get("/")
        with unauthenticated_client.session_transaction() as current_session:
            assert "emp_no" not in current_session
            assert "emp_name" not in current_session

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_chrome_devtools_probe_is_acknowledged_without_a_404(client) -> None:
    response = client.get("/.well-known/appspecific/com.chrome.devtools.json")

    assert response.status_code == 204
    assert response.data == b""


def test_mock_flask_session_is_created_with_requested_employee_identity(client) -> None:
    assert client.get("/").status_code == 200

    with client.session_transaction() as current_session:
        assert current_session["emp_no"] == "2069026"
        assert current_session["emp_name"] == "문봉건"
        assert current_session["logFlag"] is True


def test_portal_api_uses_server_side_flask_session_identity_and_profile_url(client) -> None:
    response = client.get("/api/portal")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["viewer"] == {
        "employee_id": "2069026",
        "name": "문봉건",
        "role": "관리자",
        "is_admin": True,
        "profile_image_url": (
            "http://skynet.skhynix.com/portalWeb/uploadfile/pictures/2069026.jpg"
        ),
    }
    assert payload["schedules"] == []
    assert payload["usage_history"] == []
    assert payload["settings"]["admins"][0]["employee_id"] == "2069026"


def test_schedule_post_and_get_use_flask_session_owner_without_mongodb(
    client, monkeypatch
) -> None:
    store = FakeScheduleStore()
    monkeypatch.setattr(portal_core, "_portal_schedule_store_factory", lambda: store)
    monkeypatch.setattr(
        portal_core,
        "_employee_directory_store_factory",
        _unexpected_employee_directory_store,
    )

    created_response = client.post(
        "/api/schedules",
        json={
            "title": "DA 공정 실시간 생산 분석",
            "question": "DA 공정 실시간 생산 분석을 진행해줘.",
            "repeat": "매일",
            "time": "09:30",
        },
    )

    assert created_response.status_code == 201
    created = created_response.get_json()["schedule"]
    assert created["owner_id"] == "2069026"
    assert created["owner_name"] == "문봉건"
    assert created["target"] == "개인 DM"
    assert store.documents[created["id"]]["owner_id"] == "2069026"
    assert store.documents[created["id"]]["owner_name"] == "문봉건"

    listed_response = client.get("/api/schedules")

    assert listed_response.status_code == 200
    assert listed_response.get_json()["schedules"] == [created]
    assert store.closed == 2
