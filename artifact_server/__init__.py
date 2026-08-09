"""FastAPI application package for metadata_driven_v5 data and HTML artifacts.

The package deliberately avoids importing ``artifact_server.app`` at module
import time.  Download helpers are also imported by the compatibility launcher,
and eager application loading would create a circular import.
"""

from __future__ import annotations

from typing import Any


__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    """Lazily expose the FastAPI application without coupling helper modules."""
    if name in {"app", "create_app"}:
        from .app import app, create_app

        return {"app": app, "create_app": create_app}[name]
    raise AttributeError(name)
