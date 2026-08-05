from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEDULE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}")


class ScheduleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["interval", "cron"]
    minutes: int | None = Field(default=None, ge=5, le=525_600)
    expression: str = ""
    timezone: str = "Asia/Seoul"
    start_at: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        text = str(value or "").strip() or "Asia/Seoul"
        try:
            ZoneInfo(text)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {text}") from exc
        return text

    @model_validator(mode="after")
    def validate_kind(self) -> "ScheduleSpec":
        if self.type == "interval" and self.minutes is None:
            raise ValueError("interval schedule requires minutes")
        if self.type == "cron" and not self.expression.strip():
            raise ValueError("cron schedule requires expression")
        return self


class ScheduleDefinition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = "cube.schedule.v1"
    schedule_id: str
    version: int = Field(default=1, ge=1)
    employee_id: str
    channel_id: str = ""
    question: str = Field(min_length=1, max_length=8_000)
    schedule: ScheduleSpec
    enabled: bool = True
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("schedule_id")
    @classmethod
    def valid_schedule_id(cls, value: str) -> str:
        text = str(value or "").strip()
        if not SCHEDULE_ID_PATTERN.fullmatch(text):
            raise ValueError("schedule_id format is invalid")
        return text

    @field_validator("employee_id")
    @classmethod
    def nonempty_employee_id(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("employee_id is required")
        return text

    @field_validator("channel_id", "question")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("schema_version")
    @classmethod
    def valid_schema(cls, value: str) -> str:
        if value != "cube.schedule.v1":
            raise ValueError("schema_version must be cube.schedule.v1")
        return value


class ScheduledQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scheduled_query"] = "scheduled_query"
    employee_id: str
    channel_id: str
    question: str
    schedule_id: str
    run_id: str
    dedupe_key: str


def public_schedule(document: dict[str, Any]) -> dict[str, Any]:
    value = dict(document)
    value.pop("_id", None)
    return value
