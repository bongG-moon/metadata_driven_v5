from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("ARTIFACT_LISTEN_HOST") or os.getenv("DATA_REF_DOWNLOAD_HOST") or "127.0.0.1"
    port = int(os.getenv("ARTIFACT_LISTEN_PORT") or os.getenv("DATA_REF_DOWNLOAD_PORT") or "8765")
    uvicorn.run(
        "artifact_server.app:app",
        host=host,
        port=port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
