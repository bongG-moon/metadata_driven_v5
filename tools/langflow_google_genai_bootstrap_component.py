# -*- coding: utf-8 -*-
"""Langflow Desktop bootstrap component for the Google Gemini provider.

This component is intentionally narrow: it can install only the provider
package required by the exported Gemini Language Model nodes.  It does not
accept shell commands, package indexes, or arbitrary package names.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import subprocess
import sys
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DropdownInput, IntInput, Output
from lfx.schema.data import Data


ALLOWED_PROVIDER_PACKAGES: dict[str, str] = {
    "langchain-google-genai": "langchain_google_genai",
}
DEFAULT_PROVIDER_PACKAGE = "langchain-google-genai"
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 900
OUTPUT_TEXT_LIMIT = 2000


def _bounded_timeout(value: Any) -> int:
    """Return a safe pip timeout without allowing an unbounded subprocess."""

    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(30, min(timeout, MAX_TIMEOUT_SECONDS))


def _package_version(package_name: str) -> str:
    """Return the installed distribution version, or an empty string."""

    try:
        return importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        return ""
    except Exception:
        return ""


def _text(value: Any, limit: int = OUTPUT_TEXT_LIMIT) -> str:
    """Normalize subprocess text and keep the returned status compact."""

    return str(value or "").strip()[-limit:]


def install_google_genai_provider(
    provider_package: Any = DEFAULT_PROVIDER_PACKAGE,
    install_if_missing: Any = True,
    timeout_seconds: Any = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Check or install the allowlisted Gemini provider in the active runtime."""

    package_name = str(provider_package or "").strip()
    import_name = ALLOWED_PROVIDER_PACKAGES.get(package_name)
    if not import_name:
        return {
            "status": "blocked",
            "error_type": "package_not_allowlisted",
            "package": package_name,
            "allowed_packages": sorted(ALLOWED_PROVIDER_PACKAGES),
            "interpreter": sys.executable,
            "message": "허용된 Provider 패키지만 설치할 수 있습니다.",
        }

    module_present = importlib.util.find_spec(import_name) is not None
    if module_present:
        return {
            "status": "already_installed",
            "package": package_name,
            "import_name": import_name,
            "version": _package_version(package_name),
            "interpreter": sys.executable,
            "installed_now": False,
            "restart_required": False,
            "message": "Provider 패키지가 이미 설치되어 있습니다.",
        }

    if not bool(install_if_missing):
        return {
            "status": "missing",
            "error_type": "provider_package_missing",
            "package": package_name,
            "import_name": import_name,
            "interpreter": sys.executable,
            "installed_now": False,
            "restart_required": False,
            "message": "Provider 패키지가 없고 자동 설치가 비활성화되어 있습니다.",
        }

    timeout = _bounded_timeout(timeout_seconds)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        package_name,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "error",
            "error_type": "provider_install_timeout",
            "package": package_name,
            "import_name": import_name,
            "interpreter": sys.executable,
            "timeout_seconds": timeout,
            "installed_now": False,
            "restart_required": False,
            "stderr": _text(getattr(exc, "stderr", "")),
            "message": "Provider 설치가 제한 시간 내에 완료되지 않았습니다.",
        }
    except OSError as exc:
        return {
            "status": "error",
            "error_type": "provider_install_process_error",
            "package": package_name,
            "import_name": import_name,
            "interpreter": sys.executable,
            "installed_now": False,
            "restart_required": False,
            "message": f"pip 프로세스를 시작하지 못했습니다: {exc}",
        }

    if completed.returncode != 0:
        return {
            "status": "error",
            "error_type": "provider_install_failed",
            "package": package_name,
            "import_name": import_name,
            "interpreter": sys.executable,
            "returncode": completed.returncode,
            "installed_now": False,
            "restart_required": False,
            "stdout": _text(completed.stdout),
            "stderr": _text(completed.stderr),
            "message": "Provider 패키지 설치에 실패했습니다.",
        }

    importlib.invalidate_caches()
    module_present_after = importlib.util.find_spec(import_name) is not None
    if not module_present_after:
        return {
            "status": "error",
            "error_type": "provider_import_unavailable_after_install",
            "package": package_name,
            "import_name": import_name,
            "interpreter": sys.executable,
            "returncode": completed.returncode,
            "installed_now": False,
            "restart_required": False,
            "stdout": _text(completed.stdout),
            "stderr": _text(completed.stderr),
            "message": "pip은 성공했지만 Provider 모듈을 현재 프로세스에서 찾지 못했습니다.",
        }

    return {
        "status": "installed",
        "package": package_name,
        "import_name": import_name,
        "version": _package_version(package_name),
        "interpreter": sys.executable,
        "returncode": completed.returncode,
        "installed_now": True,
        "restart_required": True,
        "stdout": _text(completed.stdout),
        "stderr": _text(completed.stderr),
        "message": "Provider 설치가 완료되었습니다. Langflow Desktop을 재시작한 뒤 Flow를 다시 여세요.",
    }


class LangflowGoogleGenAIProviderBootstrap(Component):
    """Install and verify the Gemini Provider package in a Desktop runtime."""

    display_name = "Google Gemini Provider Bootstrap"
    description = (
        "Langflow Desktop의 현재 Python 환경에 허용된 Gemini Provider 패키지만 설치하고 상태를 반환합니다. "
        "설치 후 Desktop 재시작이 필요합니다."
    )
    documentation = "https://python.langchain.com/docs/integrations/chat/google_generative_ai/"
    icon = "PackageCheck"
    name = "LangflowGoogleGenAIProviderBootstrap"

    inputs = [
        DropdownInput(
            name="provider_package",
            display_name="Provider package",
            options=sorted(ALLOWED_PROVIDER_PACKAGES),
            value=DEFAULT_PROVIDER_PACKAGE,
            required=True,
        ),
        BoolInput(
            name="install_if_missing",
            display_name="Install if missing",
            value=True,
            info="누락 시 현재 Desktop Python에 설치합니다.",
        ),
        IntInput(
            name="timeout_seconds",
            display_name="Install timeout (seconds)",
            value=DEFAULT_TIMEOUT_SECONDS,
            advanced=True,
            info=f"30~{MAX_TIMEOUT_SECONDS}초 범위로 제한됩니다.",
        ),
    ]

    outputs = [
        Output(
            name="status",
            display_name="Provider status",
            method="install_provider",
            types=["Data"],
        )
    ]

    def install_provider(self) -> Data:
        """Install or verify the allowlisted provider and return a compact status."""

        return Data(
            data=install_google_genai_provider(
                provider_package=getattr(self, "provider_package", DEFAULT_PROVIDER_PACKAGE),
                install_if_missing=getattr(self, "install_if_missing", True),
                timeout_seconds=getattr(self, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            )
        )
