"""CASE 2: callback server using the supplied production parser/builder shape.

Run this file alone when evaluating the current grouped-text, dynamic-table,
and CUBE-image-row presentation.  Only the Markdown-to-body renderer differs
from ``app_case_legacy.py``.
"""

from __future__ import annotations

import uvicorn

from app import create_application
from markdown_rich_notification import render_markdown_to_cube_body


# This case is selected in code, not through an environment variable.
application = create_application(body_renderer=render_markdown_to_cube_body)


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
