"""Standalone FastAPI artifact server package."""

from .app import application, create_app

__all__ = ["application", "create_app"]
