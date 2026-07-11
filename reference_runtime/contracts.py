"""Shared contracts for authoring and retrieval runtime.

The contracts are intentionally small and dictionary-based so Langflow
standalone components can copy the same shape without project-local imports.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


ALLOWED_METADATA_TYPES = {"domain", "table_catalog", "main_flow_filter"}
ALLOWED_DOMAIN_SECTIONS = {
    "process_groups",
    "product_terms",
    "quantity_terms",
    "metric_terms",
    "analysis_recipes",
    "status_terms",
    "product_key_columns",
    "pandas_function_cases",
}
ALLOWED_SOURCE_TYPES = {"dummy", "oracle", "h_api", "datalake", "goodocs"}
ALLOWED_DUPLICATE_ACTIONS = {"merge", "replace", "skip", "create_new"}

SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "mongodb://",
)

TRUNCATED_QUERY_MARKERS = ("...", "생략", "omitted", "truncated")


def ensure_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def compact_preview(value: str, limit: int = 500) -> str:
    text = value or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def contains_secret(value: Any) -> bool:
    """Return true when a nested value appears to contain credentials."""

    text = ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        text = " ".join(str(k) + " " + str(v) for k, v in value.items())
    elif isinstance(value, list):
        text = " ".join(str(v) for v in value)
    else:
        text = str(value)
    lower = text.lower()
    return any(marker in lower for marker in SECRET_MARKERS)


def query_is_truncated(query: Any) -> bool:
    if not isinstance(query, str):
        return False
    lower = query.lower()
    return any(marker in lower for marker in TRUNCATED_QUERY_MARKERS)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge patch into base without deleting existing data with empty values."""

    merged = deepcopy(base)
    for key, value in patch.items():
        if value in (None, "", [], {}):
            continue
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        elif isinstance(existing, list) and isinstance(value, list):
            seen = {repr(item) for item in existing}
            combined = list(existing)
            for item in value:
                if repr(item) not in seen:
                    combined.append(item)
                    seen.add(repr(item))
            merged[key] = combined
        else:
            merged[key] = deepcopy(value)
    return merged


def metadata_item_key(metadata_type: str, item: dict[str, Any]) -> str:
    if metadata_type == "domain":
        section = item.get("section") or item.get("gbn") or ""
        key = item.get("key") or ""
        return f"{section}:{key}" if section and key else key
    if metadata_type == "table_catalog":
        return item.get("dataset_key") or item.get("key") or ""
    if metadata_type == "main_flow_filter":
        return item.get("filter_key") or item.get("key") or ""
    return item.get("key") or ""


def make_error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    error = {"type": error_type, "message": message}
    error.update(extra)
    return error
