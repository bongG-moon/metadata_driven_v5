"""Runtime configuration resolver for the Flask Portal.

The HCP WebApp does not need ``python-dotenv`` or a local ``.env`` file.
Deployments can place a private ``portal_runtime_config.py`` beside this file
and define ordinary uppercase Python variables there.  HCP Secret/process
environment variables still take priority when they are available.

Private configuration is intentionally never logged or returned by an API.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any


_PRIVATE_CONFIG_MODULE = "portal_runtime_config"


class RuntimeSettingsError(RuntimeError):
    """Raised when the private Python configuration file is not valid."""


def _string_value(value: Any) -> str:
    """Convert native Python config values into existing string settings."""

    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


@lru_cache(maxsize=1)
def _private_config_values() -> dict[str, Any]:
    """Load the optional deployment-only module once per application process."""

    try:
        module = importlib.import_module(_PRIVATE_CONFIG_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _PRIVATE_CONFIG_MODULE:
            return {}
        raise RuntimeSettingsError(
            "portal_runtime_config.py 안에서 필요한 모듈을 불러오지 못했습니다."
        ) from exc
    except Exception as exc:
        raise RuntimeSettingsError(
            "portal_runtime_config.py를 읽지 못했습니다. Python 문법과 값 형식을 확인해 주세요."
        ) from exc

    values: dict[str, Any] = {}
    mapping = getattr(module, "SETTINGS", None)
    if mapping is not None:
        if not isinstance(mapping, Mapping):
            raise RuntimeSettingsError(
                "portal_runtime_config.py의 SETTINGS는 dict 형태여야 합니다."
            )
        values.update({str(key): value for key, value in mapping.items()})

    # Direct uppercase variables are the recommended, easy-to-edit form.
    # They intentionally override duplicate SETTINGS entries.
    for name, value in vars(module).items():
        if name.isupper() and not name.startswith("_"):
            values[name] = value
    return values


def get_setting(name: str, default: str = "") -> str:
    """Return one setting: process environment, private module, then default."""

    environment_value = os.getenv(name)
    if environment_value is not None and str(environment_value).strip():
        return str(environment_value).strip()

    configured_value = _string_value(_private_config_values().get(name))
    if configured_value:
        return configured_value
    return default


def settings_mapping(names: Iterable[str]) -> dict[str, str]:
    """Build a string mapping for existing ``from_env`` compatibility APIs."""

    return {str(name): get_setting(str(name)) for name in names}


def reset_runtime_settings_cache() -> None:
    """Test hook; production config changes take effect after process restart."""

    _private_config_values.cache_clear()
