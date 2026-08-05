from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str) -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    return max(minimum, int(os.getenv(name, str(default))))


@dataclass(frozen=True)
class CubeSchedulerConfig:
    source_mongo_uri: str
    source_database: str = "cube_authoring"
    source_collection: str = "cube_schedules"
    runtime_mongo_uri: str = ""
    runtime_database: str = "cube_scheduler_runtime"
    cursor_collection: str = "cube_schedule_cursors"
    run_collection: str = "cube_runs"
    outbox_collection: str = "cube_outbox"
    inbound_collection: str = "cube_inbound_events"
    user_channel_collection: str = "cube_user_channels"
    cube_outbound_url: str = ""
    cube_bot_id: str = ""
    cube_bot_token: str = ""
    cube_callback_address: str = ""
    accepted_statuses: tuple[str, ...] = ("success", "ok")
    poll_seconds: int = 15
    lease_seconds: int = 120
    max_delivery_attempts: int = 3
    retry_base_seconds: int = 5
    source_limit: int = 10_000
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "CubeSchedulerConfig":
        env_file = os.getenv("CUBE_ENV_FILE") or str(Path(__file__).resolve().parents[1] / ".env")
        _load_dotenv(env_file)
        accepted = tuple(
            item.strip().lower()
            for item in os.getenv("CUBE_SUCCESS_STATUSES", "success,ok").split(",")
            if item.strip()
        )
        return cls(
            source_mongo_uri=os.getenv("CUBE_SCHEDULE_SOURCE_MONGODB_URI", "").strip(),
            source_database=os.getenv("CUBE_SCHEDULE_SOURCE_DATABASE", "cube_authoring").strip(),
            source_collection=os.getenv("CUBE_SCHEDULE_SOURCE_COLLECTION", "cube_schedules").strip(),
            runtime_mongo_uri=os.getenv("CUBE_RUNTIME_MONGODB_URI", "").strip(),
            runtime_database=os.getenv("CUBE_RUNTIME_DATABASE", "cube_scheduler_runtime").strip(),
            cursor_collection=os.getenv("CUBE_RUNTIME_CURSOR_COLLECTION", "cube_schedule_cursors").strip(),
            run_collection=os.getenv("CUBE_RUNTIME_RUN_COLLECTION", "cube_runs").strip(),
            outbox_collection=os.getenv("CUBE_RUNTIME_OUTBOX_COLLECTION", "cube_outbox").strip(),
            inbound_collection=os.getenv("CUBE_RUNTIME_INBOUND_COLLECTION", "cube_inbound_events").strip(),
            user_channel_collection=os.getenv("CUBE_RUNTIME_USER_CHANNEL_COLLECTION", "cube_user_channels").strip(),
            cube_outbound_url=os.getenv("CUBE_OUTBOUND_URL", "").strip(),
            cube_bot_id=os.getenv("CUBE_BOT_ID", "").strip(),
            cube_bot_token=os.getenv("CUBE_BOT_TOKEN", "").strip(),
            cube_callback_address=os.getenv("CUBE_CALLBACK_ADDRESS", "").strip(),
            accepted_statuses=accepted or ("success", "ok"),
            poll_seconds=_int_env("CUBE_SCHEDULER_POLL_SECONDS", 15),
            lease_seconds=_int_env("CUBE_SCHEDULER_LEASE_SECONDS", 120),
            max_delivery_attempts=_int_env("CUBE_MAX_DELIVERY_ATTEMPTS", 3),
            retry_base_seconds=_int_env("CUBE_RETRY_BASE_SECONDS", 5),
            source_limit=_int_env("CUBE_SCHEDULE_SOURCE_LIMIT", 10_000),
            connect_timeout_seconds=float(os.getenv("CUBE_CONNECT_TIMEOUT_SECONDS", "5")),
            read_timeout_seconds=float(os.getenv("CUBE_READ_TIMEOUT_SECONDS", "20")),
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name, value in {
            "CUBE_SCHEDULE_SOURCE_MONGODB_URI": self.source_mongo_uri,
            "CUBE_RUNTIME_MONGODB_URI": self.runtime_mongo_uri,
            "CUBE_OUTBOUND_URL": self.cube_outbound_url,
            "CUBE_BOT_ID": self.cube_bot_id,
            "CUBE_BOT_TOKEN": self.cube_bot_token,
        }.items():
            if not str(value or "").strip():
                errors.append(f"{name} is not configured")
        return errors
