"""Local PTMORE Portal runner with a fixed developer identity.

This entry point is intentionally separate from ``app.py``.  It never imports
the HCP-only SSO module and always uses employee ``2011111`` / ``문봉건`` as
the fixed local administrator.
"""

from __future__ import annotations

import os

import uvicorn


# Set these before importing the shared application module.  They are fixed on
# purpose so a local browser request cannot impersonate another employee.
# The shared application grants this fixed local identity administrator access
# only while this ``local`` adapter is selected.
os.environ["PTMORE_PORTAL_AUTH_MODE"] = "local"

from app import application


# Both names are intentional: ``application`` matches the production command,
# while ``app`` supports normal Uvicorn import syntax.
app = application


if __name__ == "__main__":
    uvicorn.run("__main__:application", host="0.0.0.0", port=5000, reload=False)
