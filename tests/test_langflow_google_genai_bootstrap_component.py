from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


COMPONENT_PATH = Path(__file__).parents[1] / "tools" / "langflow_google_genai_bootstrap_component.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("langflow_google_genai_bootstrap_component", COMPONENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_allowlist_rejects_arbitrary_package_without_running_pip(monkeypatch):
    module = _load_module()
    pip = Mock()
    monkeypatch.setattr(module.subprocess, "run", pip)

    result = module.install_google_genai_provider("requests")

    assert result["status"] == "blocked"
    assert result["error_type"] == "package_not_allowlisted"
    pip.assert_not_called()


def test_existing_provider_is_reported_without_running_pip(monkeypatch):
    module = _load_module()
    pip = Mock()
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: SimpleNamespace())
    monkeypatch.setattr(module.subprocess, "run", pip)
    monkeypatch.setattr(module, "_package_version", lambda _: "1.2.3")

    result = module.install_google_genai_provider()

    assert result["status"] == "already_installed"
    assert result["version"] == "1.2.3"
    pip.assert_not_called()


def test_missing_provider_uses_active_python_and_requires_restart(monkeypatch):
    module = _load_module()
    completed = SimpleNamespace(returncode=0, stdout="installed", stderr="")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return completed

    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_package_version", lambda _: "1.2.3")

    calls_after = iter([None, SimpleNamespace()])
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: next(calls_after))
    result = module.install_google_genai_provider(timeout_seconds=9999)

    assert result["status"] == "installed"
    assert result["restart_required"] is True
    assert calls[0][0][:5] == [module.sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    assert calls[0][0][-1] == "langchain-google-genai"
    assert calls[0][1]["timeout"] == module.MAX_TIMEOUT_SECONDS


def test_failed_install_returns_compact_error(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="out", stderr="failed"),
    )

    result = module.install_google_genai_provider()

    assert result["status"] == "error"
    assert result["error_type"] == "provider_install_failed"
    assert result["restart_required"] is False
