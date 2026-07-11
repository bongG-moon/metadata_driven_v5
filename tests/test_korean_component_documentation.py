from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "tools" / "validate_korean_component_documentation.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_korean_component_documentation", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_korean_component_comments_and_generated_json_are_utf8_safe() -> None:
    result = _load_validator().audit()
    assert result["status"] == "ok", "\n".join(result["errors"])
