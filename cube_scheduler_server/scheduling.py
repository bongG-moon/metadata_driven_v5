from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import ScheduleDefinition, ScheduleSpec


def definition_hash(definition: ScheduleDefinition) -> str:
    payload = definition.model_dump(mode="json", exclude={"updated_at"})
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def next_run_after(
    schedule: ScheduleSpec,
    after_utc: datetime,
    *,
    anchor_utc: datetime | None = None,
) -> datetime:
    after = _utc(after_utc)
    if schedule.type == "interval":
        interval = timedelta(minutes=int(schedule.minutes or 5))
        anchor = _utc(schedule.start_at or anchor_utc or after)
        if anchor > after:
            return anchor
        elapsed = after - anchor
        steps = elapsed // interval + 1
        return anchor + (steps * interval)
    return _next_cron(schedule.expression, schedule.timezone, after)


def _next_cron(expression: str, timezone_name: str, after_utc: datetime) -> datetime:
    fields = str(expression or "").split()
    if len(fields) != 5:
        raise ValueError("cron expression must contain five fields")
    minute, hour, day, month, weekday = (
        _parse_field(fields[0], 0, 59),
        _parse_field(fields[1], 0, 23),
        _parse_field(fields[2], 1, 31),
        _parse_field(fields[3], 1, 12),
        _parse_field(fields[4], 0, 7, normalize_sunday=True),
    )
    timezone_value = ZoneInfo(timezone_name)
    candidate = after_utc.astimezone(timezone_value).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day_is_wildcard = fields[2] == "*"
    weekday_is_wildcard = fields[4] == "*"
    for _ in range(60 * 24 * 366 * 2):
        cron_weekday = (candidate.weekday() + 1) % 7
        day_match = candidate.day in day
        weekday_match = cron_weekday in weekday
        if day_is_wildcard:
            date_match = weekday_match
        elif weekday_is_wildcard:
            date_match = day_match
        else:
            date_match = day_match or weekday_match
        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.month in month
            and date_match
        ):
            return candidate.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression did not produce a run within two years")


def _parse_field(
    value: str,
    minimum: int,
    maximum: int,
    *,
    normalize_sunday: bool = False,
) -> set[int]:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty cron field")
    result: set[int] = set()
    for part in text.split(","):
        match = re.fullmatch(r"(\*|\d+|\d+-\d+)(?:/(\d+))?", part)
        if not match:
            raise ValueError(f"invalid cron field: {value}")
        base, step_text = match.groups()
        step = int(step_text or "1")
        if step <= 0:
            raise ValueError("cron step must be positive")
        if base == "*":
            values: Iterable[int] = range(minimum, maximum + 1, step)
        elif "-" in base:
            start, end = (int(item) for item in base.split("-", 1))
            if start > end:
                raise ValueError("cron range start exceeds end")
            values = range(start, end + 1, step)
        else:
            values = (int(base),)
        for item in values:
            if item < minimum or item > maximum:
                raise ValueError(f"cron value {item} is outside {minimum}..{maximum}")
            result.add(0 if normalize_sunday and item == 7 else item)
    return result


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
