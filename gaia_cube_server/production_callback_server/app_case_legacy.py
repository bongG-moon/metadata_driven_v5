"""CASE 1: callback server using the former per-line Rich Notification renderer.

Run this file alone when evaluating the legacy presentation.  It uses the
same GAIA request, CUBE send payload, callback endpoint, session handling,
and fallback behaviour as ``app.py``; only ``content[0].body`` differs.
"""

from __future__ import annotations

import uvicorn

from app import create_application
from markdown_legacy_rich_notification import render_legacy_markdown_to_cube_body


# This case is selected in code, not through an environment variable.
application = create_application(body_renderer=render_legacy_markdown_to_cube_body)


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
