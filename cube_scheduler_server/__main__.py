from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "cube_scheduler_server.app:app",
        host=os.getenv("CUBE_SERVER_HOST", "127.0.0.1"),
        port=int(os.getenv("CUBE_SERVER_PORT", "8770")),
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
