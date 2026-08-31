"""MongoDB-backed scheduler worker for the PTMORE GAIA-CUBE service.

This process is intentionally separate from :mod:`app`.  ``app.py`` only
handles an interactive CUBE callback and must acknowledge that request
quickly.  This worker polls the Portal's ``portal_schedules`` collection,
claims one due schedule atomically, runs GAIA, and sends the final result to
the schedule owner's personal CUBE DM.

Run it as a long-lived process next to ``app.py``::

    python scheduler_worker.py

The worker does *not* send the interactive "processing" notice.  Every
scheduled execution has a new GAIA session and uses
``platform=CUBE_SCHEDULING`` so it cannot share an ordinary CUBE chat's
conversation/session identity.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import math
import os
import re
import socket
import sys
import uuid
from calendar import monthrange
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

# Reuse the proven GAIA request shape, CUBE sender, and Markdown-to-Rich-
# Notification renderer.  The interactive callback flow in app.py is not
# modified by this worker.
from app import (
    ExternalApiError,
    GaiaRequestError,
    GaiaResponseError,
    Settings,
    SettingsError,
    _gaia_fallback_message,
    call_gaia,
    extract_final_answer,
    send_cube_message,
)


LOGGER = logging.getLogger("ptmore_scheduler_worker")
UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
SCHEDULE_PLATFORM = "CUBE_SCHEDULING"
SCHEDULE_SESSION_PREFIX = "cube_scheduling"
SCHEDULE_RESULT_PREFIX = "안녕하세요! PTMORE PKG Agent 스케쥴링 실행 결과입니다 😀."
SCHEDULE_CONFIGURATION_MESSAGE = (
    "스케줄 설정을 확인할 수 없습니다. Portal에서 반복 방식과 실행 시간을 확인해 주세요."
)

ACTIVE_SCHEDULE_STATUSES = ("active", "ACTIVE", "활성")


class ScheduleConfigurationError(ValueError):
    """Raised when an active schedule document cannot be run safely."""


def utc_now() -> datetime:
    """Return an aware UTC timestamp through a small injectable seam."""

    return datetime.now(UTC)


def _utc_iso(value: datetime) -> str:
    """Return the one canonical UTC ISO value used in MongoDB documents."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    """Read an ISO timestamp or BSON datetime and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ScheduleConfigurationError(
                f"{field_name} must be an ISO timestamp."
            ) from exc
    else:
        raise ScheduleConfigurationError(f"{field_name} is required.")

    # Portal writes UTC ISO values.  Treating a legacy naive value as UTC is
    # deterministic and avoids silently applying the worker host timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _required_text(document: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = document.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ScheduleConfigurationError(f"One of {', '.join(names)} is required.")


def _optional_text(document: Mapping[str, Any], *names: str) -> str | None:
    try:
        return _required_text(document, *names)
    except ScheduleConfigurationError:
        return None


def schedule_identifier(document: Mapping[str, Any]) -> str:
    """Return the Portal's human-readable ID, falling back to Mongo ``_id``."""

    return _required_text(document, "id", "schedule_id", "_id")


def schedule_owner_id(document: Mapping[str, Any]) -> str:
    """Read the schedule owner's employee number from compatible field names."""

    owner_id = _optional_text(
        document,
        "owner_id",
        "owner_employee_id",
        "user_id",
        "created_by",
        "created_by_employee_id",
        "employee_id",
    )
    if owner_id:
        if re.fullmatch(r"\d{5,12}", owner_id):
            return owner_id
        raise ScheduleConfigurationError("owner_id must be an employee number.")

    # ``owner`` is only a compatibility fallback: new Portal documents use
    # owner_id while old preview data used owner for an employee number. Do
    # not ever turn a display label such as "문봉건" into a CUBE recipient.
    legacy_owner = _optional_text(document, "owner")
    if legacy_owner and re.fullmatch(r"\d{5,12}", legacy_owner):
        return legacy_owner
    raise ScheduleConfigurationError("owner_id is required.")


def schedule_question(document: Mapping[str, Any]) -> str:
    """Read the GAIA question from a Portal schedule document."""

    return _required_text(document, "question", "query", "message")


def _schedule_timezone(document: Mapping[str, Any]) -> ZoneInfo:
    timezone_name = _optional_text(document, "timezone") or "Asia/Seoul"
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleConfigurationError("timezone is not a valid IANA timezone.") from exc


def _parse_clock(value: Any, *, field_name: str, fallback: time | None = None) -> time:
    """Parse ``HH:MM`` / ``HH:MM:SS`` without depending on browser formatting."""

    if value is None or not str(value).strip():
        if fallback is not None:
            return fallback.replace(tzinfo=None)
        raise ScheduleConfigurationError(f"{field_name} is required.")

    raw = str(value).strip()
    # The Portal persists 24-hour values.  The small Korean fallback makes a
    # legacy display value such as "오전 09:30" harmless during migration.
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
    if not match:
        raise ScheduleConfigurationError(f"{field_name} must be HH:MM.")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if "오후" in raw and 1 <= hour < 12:
        hour += 12
    if "오전" in raw and hour == 12:
        hour = 0
    try:
        return time(hour=hour, minute=minute, second=second)
    except ValueError as exc:
        raise ScheduleConfigurationError(f"{field_name} must be HH:MM.") from exc


def _repeat_kind(document: Mapping[str, Any]) -> str:
    raw_value = _optional_text(document, "repeat", "repeat_type", "schedule_type") or ""
    normalized = raw_value.lower().replace(" ", "")
    if document.get("interval_minutes") not in (None, "") or normalized in {
        "interval",
        "간격",
    }:
        return "interval"
    if re.search(r"\d+\s*분마다", raw_value):
        return "interval"
    if normalized in {"평일", "weekday", "weekdays", "businessdays"}:
        return "weekdays"
    if normalized in {"매일", "daily", "day"}:
        return "daily"
    if normalized in {"매주", "weekly", "week"}:
        return "weekly"
    if normalized in {"매월", "monthly", "month"}:
        return "monthly"
    if normalized in {"한번만", "한번", "once", "one-time", "onetime"}:
        return "once"
    raise ScheduleConfigurationError("repeat must be daily, weekdays, weekly, monthly, once, or interval.")


def _schedule_time(document: Mapping[str, Any], fallback: time | None) -> time:
    return _parse_clock(
        document.get("time")
        or document.get("run_time")
        or document.get("execution_time"),
        field_name="time",
        fallback=fallback,
    )


def _weekly_weekday(document: Mapping[str, Any], fallback: int) -> int:
    """Read Monday=0 ... Sunday=6 from common Portal representations."""

    value: Any = None
    for key in ("weekday", "day_of_week", "week_day"):
        if document.get(key) not in (None, ""):
            value = document[key]
            break
    if value is None:
        values = document.get("weekdays")
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values:
            value = values[0]
    if value is None:
        return fallback
    if isinstance(value, int) and 0 <= value <= 6:
        return value

    names = {
        "mon": 0,
        "monday": 0,
        "월": 0,
        "월요일": 0,
        "tue": 1,
        "tuesday": 1,
        "화": 1,
        "화요일": 1,
        "wed": 2,
        "wednesday": 2,
        "수": 2,
        "수요일": 2,
        "thu": 3,
        "thursday": 3,
        "목": 3,
        "목요일": 3,
        "fri": 4,
        "friday": 4,
        "금": 4,
        "금요일": 4,
        "sat": 5,
        "saturday": 5,
        "토": 5,
        "토요일": 5,
        "sun": 6,
        "sunday": 6,
        "일": 6,
        "일요일": 6,
    }
    text_value = str(value).strip().lower()
    if text_value.isdigit() and 0 <= int(text_value) <= 6:
        return int(text_value)
    if text_value in names:
        return names[text_value]
    raise ScheduleConfigurationError("weekday must be between 0 and 6, or a weekday name.")


def _monthly_day(document: Mapping[str, Any], fallback: int) -> int:
    value = document.get("day_of_month", document.get("monthly_day", fallback))
    try:
        day = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigurationError("day_of_month must be a number.") from exc
    if not 1 <= day <= 31:
        raise ScheduleConfigurationError("day_of_month must be between 1 and 31.")
    return day


def _interval_minutes(document: Mapping[str, Any]) -> int:
    value = document.get("interval_minutes")
    if value in (None, ""):
        repeat = _optional_text(document, "repeat", "repeat_type") or ""
        match = re.search(r"(\d+)\s*분마다", repeat)
        if match:
            value = match.group(1)
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigurationError("interval_minutes must be a positive integer.") from exc
    if minutes <= 0:
        raise ScheduleConfigurationError("interval_minutes must be a positive integer.")
    return minutes


def _next_weekday(candidate_date: date) -> date:
    while candidate_date.weekday() >= 5:
        candidate_date += timedelta(days=1)
    return candidate_date


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def calculate_next_run_at(
    document: Mapping[str, Any],
    *,
    now: datetime,
    scheduled_for: datetime | None = None,
) -> datetime | None:
    """Calculate the first occurrence *after* ``now`` in the schedule timezone.

    Portal stores ``next_run_at`` as an aware UTC ISO string.  The recurrence
    rule itself is interpreted in KST by default, including interval start/end
    windows.  ``None`` means a one-time schedule has no next run.
    """

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_utc = now.astimezone(UTC)
    if scheduled_for is not None:
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=UTC)
        reference_utc = max(now_utc, scheduled_for.astimezone(UTC))
    else:
        reference_utc = now_utc

    timezone_value = _schedule_timezone(document)
    reference = reference_utc.astimezone(timezone_value)
    scheduled_local = scheduled_for.astimezone(timezone_value) if scheduled_for else None
    fallback_time = scheduled_local.timetz().replace(tzinfo=None) if scheduled_local else None
    kind = _repeat_kind(document)

    if kind == "once":
        return None

    if kind == "interval":
        interval = timedelta(minutes=_interval_minutes(document))
        start_clock = _parse_clock(document.get("start_time"), field_name="start_time", fallback=time.min)
        # A blank end means the rest of the day.  Explicit ``00:00`` keeps its
        # literal meaning so a configuration error is visible rather than
        # silently creating an all-day schedule.
        end_clock = _parse_clock(
            document.get("end_time"),
            field_name="end_time",
            fallback=time(23, 59, 59),
        )
        # An overnight window is supported: e.g. 22:00 ~ 02:00.
        for offset in range(-1, 370):
            anchor_day = reference.date() + timedelta(days=offset)
            window_start = datetime.combine(anchor_day, start_clock, tzinfo=timezone_value)
            window_end_day = anchor_day + timedelta(days=1) if end_clock < start_clock else anchor_day
            window_end = datetime.combine(window_end_day, end_clock, tzinfo=timezone_value)
            if reference < window_start:
                candidate = window_start
            else:
                elapsed_seconds = (reference - window_start).total_seconds()
                steps = int(elapsed_seconds // interval.total_seconds()) + 1
                candidate = window_start + interval * steps
            if candidate <= window_end:
                return candidate.astimezone(UTC)
        raise ScheduleConfigurationError("Could not calculate the next interval execution.")

    run_clock = _schedule_time(document, fallback_time)
    if kind == "daily":
        candidate = datetime.combine(reference.date(), run_clock, tzinfo=timezone_value)
        if candidate <= reference:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    if kind == "weekdays":
        candidate_day = _next_weekday(reference.date())
        candidate = datetime.combine(candidate_day, run_clock, tzinfo=timezone_value)
        if candidate <= reference:
            candidate_day = _next_weekday(candidate_day + timedelta(days=1))
            candidate = datetime.combine(candidate_day, run_clock, tzinfo=timezone_value)
        return candidate.astimezone(UTC)

    if kind == "weekly":
        fallback_weekday = scheduled_local.weekday() if scheduled_local else 0
        target_weekday = _weekly_weekday(document, fallback_weekday)
        offset = (target_weekday - reference.weekday()) % 7
        candidate_day = reference.date() + timedelta(days=offset)
        candidate = datetime.combine(candidate_day, run_clock, tzinfo=timezone_value)
        if candidate <= reference:
            candidate += timedelta(days=7)
        return candidate.astimezone(UTC)

    if kind == "monthly":
        fallback_day = scheduled_local.day if scheduled_local else 1
        target_day = _monthly_day(document, fallback_day)
        year, month = reference.year, reference.month
        for _ in range(24):
            actual_day = min(target_day, monthrange(year, month)[1])
            candidate = datetime.combine(
                date(year, month, actual_day), run_clock, tzinfo=timezone_value
            )
            if candidate > reference:
                return candidate.astimezone(UTC)
            year, month = _next_month(year, month)
        raise ScheduleConfigurationError("Could not calculate the next monthly execution.")

    # ``_repeat_kind`` currently makes this branch unreachable. Keep a safe
    # guard in case a future recurrence is added without a calculator.
    raise ScheduleConfigurationError("Unsupported repeat rule.")


def build_scheduling_gaia_context(
    *,
    question: str,
    owner_id: str,
    session_id: str,
    schedule_id: str,
) -> tuple[str, str]:
    """Build GAIA's GaiA Input ``data`` and ``metadata`` tweak strings.

    A scheduled run intentionally has only the current user question in
    history.  It must never inherit interactive CUBE chat context.
    """

    data = {
        "conversation_history": [
            {"role": "user", "content": question, "files": []},
        ]
    }
    metadata = {
        "platform": SCHEDULE_PLATFORM,
        "user_id": owner_id,
        "session_id": session_id,
        "cube_user_id": owner_id,
        "schedule_id": schedule_id,
    }
    try:
        return (
            json.dumps(data, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
        )
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigurationError("The scheduled GAIA context is not JSON serializable.") from exc


def build_scheduled_result_message(question: str, answer: str) -> str:
    """Return the exact visible contract for every scheduled CUBE result."""

    return f"{SCHEDULE_RESULT_PREFIX}\n실행 질문 : {question}\n\n{answer}"


def _safe_error_category(error: BaseException, *, phase: str) -> str:
    """Classify errors for MongoDB without storing credentials or raw details."""

    if phase == "cube":
        return "cube_delivery"
    if isinstance(error, ScheduleConfigurationError):
        return "invalid_schedule"
    if isinstance(error, GaiaRequestError):
        allowed = {
            "forbidden",
            "timeout",
            "http_error",
            "connection",
            "invalid_json",
            "unexpected_body",
        }
        return f"gaia_{error.reason}" if error.reason in allowed else "gaia_request"
    if isinstance(error, GaiaResponseError):
        return "gaia_response"
    if isinstance(error, ExternalApiError):
        return "external_api"
    return "unexpected"


@dataclass(frozen=True)
class SchedulerSettings:
    """Settings needed only by the independent scheduler process."""

    mongodb_uri: str
    mongodb_database: str
    schedule_collection: str
    schedule_run_collection: str
    poll_seconds: float
    lease_seconds: float
    batch_size: int
    worker_id: str
    mongodb_server_selection_timeout_ms: int

    @classmethod
    def from_env(cls) -> "SchedulerSettings":
        load_dotenv(Path(__file__).with_name(".env"), override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value or value.startswith("PASTE_") or value.startswith("<"):
                raise SettingsError(f"{name} is required and must not be a placeholder.")
            return value

        def positive_float(name: str, default: float) -> float:
            raw_value = os.getenv(name, str(default)).strip()
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise SettingsError(f"{name} must be a number.") from exc
            if value <= 0:
                raise SettingsError(f"{name} must be greater than zero.")
            return value

        def positive_int(name: str, default: int, maximum: int | None = None) -> int:
            raw_value = os.getenv(name, str(default)).strip()
            try:
                value = int(raw_value)
            except ValueError as exc:
                raise SettingsError(f"{name} must be an integer.") from exc
            if value <= 0 or (maximum is not None and value > maximum):
                limit = f" between 1 and {maximum}" if maximum is not None else " greater than zero"
                raise SettingsError(f"{name} must be{limit}.")
            return value

        return cls(
            mongodb_uri=required("MONGODB_URI"),
            mongodb_database=required("MONGODB_DATABASE"),
            schedule_collection=required("PTMORE_SCHEDULE_COLLECTION"),
            schedule_run_collection=required("PTMORE_SCHEDULE_RUN_COLLECTION"),
            poll_seconds=positive_float("PTMORE_SCHEDULER_POLL_SECONDS", 30),
            lease_seconds=positive_float("PTMORE_SCHEDULER_LEASE_SECONDS", 900),
            batch_size=positive_int("PTMORE_SCHEDULER_BATCH_SIZE", 20, maximum=200),
            worker_id=os.getenv(
                "PTMORE_SCHEDULER_WORKER_ID",
                f"{socket.gethostname()}-{os.getpid()}",
            ).strip()
            or f"{socket.gethostname()}-{os.getpid()}",
            mongodb_server_selection_timeout_ms=positive_int(
                "PTMORE_SCHEDULER_MONGODB_TIMEOUT_MS", 5000
            ),
        )

    def validate_lease_duration(self, gaia_settings: Settings) -> None:
        """Reject a lease that can expire during a normal GAIA+CUBE call."""

        # One final CUBE delivery is needed after GAIA. Keep a one-minute
        # buffer for network variance and application overhead. A too-short
        # lease could allow another replica to acquire and send the same run.
        minimum_seconds = max(
            60.0,
            gaia_settings.gaia_timeout_seconds
            + gaia_settings.cube_timeout_seconds
            + 60.0,
        )
        if self.lease_seconds < minimum_seconds:
            raise SettingsError(
                "PTMORE_SCHEDULER_LEASE_SECONDS must be at least "
                f"{math.ceil(minimum_seconds)} seconds for the configured "
                "GAIA/CUBE timeouts."
            )


@dataclass(frozen=True)
class ClaimedSchedule:
    """One MongoDB document that this worker holds under a finite lease."""

    document: Mapping[str, Any]
    claim_token: str
    claimed_at: datetime
    scheduled_for: datetime | None
    # Keep the source value for a malformed timestamp run record. This lets a
    # bad active document be auditable and deactivated instead of being leased
    # repeatedly forever.
    scheduled_for_value: str = ""


class MongoScheduleRepository:
    """Small synchronous MongoDB adapter with atomic schedule claims.

    PyMongo's ``find_one_and_update`` runs one MongoDB command.  A competing
    worker must match the same unclaimed due document, so only one replica can
    acquire a given schedule lease at a time.
    """

    def __init__(self, schedules: Collection, runs: Collection) -> None:
        self.schedules = schedules
        self.runs = runs

    @staticmethod
    def _active_filter() -> dict[str, Any]:
        """Match active schedules while letting explicit inactive win."""

        return {
            "$or": [
                {"status": {"$in": list(ACTIVE_SCHEDULE_STATUSES)}},
                # Accept a legacy boolean only when an explicit
                # inactive/paused status is not present.
                {"status": {"$exists": False}, "is_active": True},
                {"status": None, "is_active": True},
            ]
        }

    @classmethod
    def _due_filter(cls, now: datetime) -> dict[str, Any]:
        now_iso = _utc_iso(now)
        now_z = now_iso.replace("+00:00", "Z")
        return {
            "$and": [
                cls._active_filter(),
                # The Portal contract writes +00:00 ISO text. Accept legacy
                # Z suffixes too, including an execution exactly on the
                # scheduled second.
                {
                    "$or": [
                        {"next_run_at": {"$lte": now_iso}},
                        {"next_run_at": {"$lte": now_z}},
                    ]
                },
                {
                    "$or": [
                        {"scheduler_claim_until": {"$exists": False}},
                        {"scheduler_claim_until": None},
                        {"scheduler_claim_until": {"$lte": now_iso}},
                        {"scheduler_claim_until": {"$lte": now_z}},
                    ]
                },
            ]
        }

    def ensure_indexes(self) -> None:
        """Try to add operational indexes without requiring DDL permission."""

        try:
            self.schedules.create_index(
                [("status", ASCENDING), ("next_run_at", ASCENDING), ("scheduler_claim_until", ASCENDING)],
                name="ptmore_scheduler_due_lookup",
            )
        except PyMongoError:
            LOGGER.warning(
                "Could not create the schedule due index; continuing with existing MongoDB indexes."
            )
        try:
            self.runs.create_index(
                [("schedule_id", ASCENDING), ("scheduled_for", ASCENDING)],
                name="ptmore_scheduler_run_lookup",
            )
        except PyMongoError:
            LOGGER.warning(
                "Could not create the schedule run index; continuing with existing MongoDB indexes."
            )

    def due_candidates(self, now: datetime, limit: int) -> list[Mapping[str, Any]]:
        cursor = self.schedules.find(self._due_filter(now)).sort(
            [("next_run_at", ASCENDING)]
        ).limit(limit)
        return [item for item in cursor if isinstance(item, Mapping)]

    def claim(self, document: Mapping[str, Any], *, now: datetime, lease_seconds: float) -> ClaimedSchedule | None:
        if "_id" not in document:
            raise ScheduleConfigurationError("MongoDB schedule document has no _id.")
        token = str(uuid.uuid4())
        lease_until = now + timedelta(seconds=lease_seconds)
        claim_filter = {
            "$and": [
                {"_id": document["_id"]},
                self._due_filter(now),
            ]
        }
        claimed = self.schedules.find_one_and_update(
            claim_filter,
            {
                "$set": {
                    "scheduler_claim_token": token,
                    "scheduler_claimed_at": _utc_iso(now),
                    "scheduler_claim_until": _utc_iso(lease_until),
                    "updated_at": _utc_iso(now),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if not isinstance(claimed, Mapping):
            return None
        scheduled_for_value = str(claimed.get("next_run_at") or "")
        try:
            scheduled_for = _parse_datetime(
                claimed.get("next_run_at"), field_name="next_run_at"
            )
        except ScheduleConfigurationError:
            # Do not abandon a newly-acquired lease for a malformed timestamp.
            # execute_claimed_schedule will write the failed run, deactivate
            # the document, and release the same lease token safely.
            scheduled_for = None
        return ClaimedSchedule(
            document=claimed,
            claim_token=token,
            claimed_at=now,
            scheduled_for=scheduled_for,
            scheduled_for_value=scheduled_for_value,
        )

    @staticmethod
    def _unexpired_lease_filter(now: datetime) -> dict[str, Any]:
        """Parse canonical ``+00:00`` and legacy ``Z`` Mongo timestamps.

        A lexical string comparison would consider an equal legacy ``Z`` time
        newer than a ``+00:00`` value. MongoDB therefore converts the field to
        a Date before comparing it with the current UTC instant.
        """

        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return {
            "$expr": {
                "$gt": [
                    {
                        "$convert": {
                            "input": "$scheduler_claim_until",
                            "to": "date",
                            "onError": epoch,
                            "onNull": epoch,
                        }
                    },
                    now.astimezone(UTC),
                ]
            }
        }

    def claim_is_current(
        self,
        claimed: ClaimedSchedule,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return true only while this worker still owns an active schedule.

        Portal edits/pause actions invalidate the lease token. Checking just
        before CUBE delivery prevents a slow GAIA response from being sent
        after the user has stopped or changed the schedule.
        """

        if "_id" not in claimed.document:
            return False
        checked_at = (now or utc_now()).astimezone(UTC)
        current = self.schedules.find_one(
            {
                "$and": [
                    {"_id": claimed.document["_id"]},
                    {"scheduler_claim_token": claimed.claim_token},
                    self._active_filter(),
                    self._unexpired_lease_filter(checked_at),
                ]
            },
            {"_id": 1},
        )
        return isinstance(current, Mapping)

    def start_run(
        self,
        claimed: ClaimedSchedule,
        *,
        run_id: str,
        schedule_id: str,
        owner_id: str | None,
        question: str | None,
        session_id: str,
        started_at: datetime,
    ) -> None:
        """Persist only operational fields needed for audit and retry diagnosis."""

        self.runs.insert_one(
            {
                "run_id": run_id,
                "schedule_id": schedule_id,
                "schedule_object_id": str(claimed.document.get("_id", "")),
                "owner_id": owner_id or "",
                "question": question or "",
                "platform": SCHEDULE_PLATFORM,
                "session_id": session_id,
                "status": "running",
                "error_category": None,
                "scheduled_for": (
                    _utc_iso(claimed.scheduled_for)
                    if claimed.scheduled_for is not None
                    else claimed.scheduled_for_value
                ),
                "started_at": _utc_iso(started_at),
                "completed_at": None,
                "worker_id": "",
            }
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error_category: str | None,
        completed_at: datetime,
        delivery_status: str,
        worker_id: str,
    ) -> None:
        self.runs.update_one(
            {"run_id": run_id},
            {
                "$set": {
                    "status": status,
                    "error_category": error_category,
                    "completed_at": _utc_iso(completed_at),
                    "delivery_status": delivery_status,
                    "worker_id": worker_id,
                }
            },
        )

    def complete_claim(
        self,
        claimed: ClaimedSchedule,
        *,
        next_run_at: datetime | None,
        completed_at: datetime,
        status: str,
        error_category: str | None,
        deactivate: bool = False,
    ) -> None:
        fields: dict[str, Any] = {
            "next_run_at": _utc_iso(next_run_at) if next_run_at is not None else None,
            "last_run_at": _utc_iso(completed_at),
            "last_run_status": status,
            "last_run_error_category": error_category,
            "updated_at": _utc_iso(completed_at),
        }
        if deactivate:
            # Both values are retained for old/new Portal readers.  Neither
            # belongs to the active query, so a one-time schedule cannot run
            # again after a successful or failed attempt.
            fields.update({"status": "inactive", "is_active": False})
        self.schedules.update_one(
            {
                "_id": claimed.document["_id"],
                "scheduler_claim_token": claimed.claim_token,
            },
            {
                "$set": fields,
                "$unset": {
                    "scheduler_claim_token": "",
                    "scheduler_claimed_at": "",
                    "scheduler_claim_until": "",
                },
            },
        )


class SchedulerWorker:
    """Run due schedules once or continuously with injected test seams."""

    def __init__(
        self,
        *,
        gaia_settings: Settings,
        scheduler_settings: SchedulerSettings,
        repository: MongoScheduleRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.gaia_settings = gaia_settings
        self.scheduler_settings = scheduler_settings
        self.repository = repository
        self.clock = clock

    def _claim_allows_delivery(self, claimed: ClaimedSchedule) -> bool:
        """Fail closed when a Portal edit invalidated or cannot verify a lease."""

        try:
            return self.repository.claim_is_current(
                claimed,
                now=self.clock().astimezone(UTC),
            )
        except PyMongoError:
            LOGGER.exception("Could not verify the schedule lease before CUBE delivery.")
            return False

    async def run_once(self, client: httpx.AsyncClient) -> int:
        """Claim and execute at most one batch of currently due schedules."""

        now = self.clock().astimezone(UTC)
        candidates = self.repository.due_candidates(now, self.scheduler_settings.batch_size)
        executed = 0
        for candidate in candidates:
            try:
                claimed = self.repository.claim(
                    candidate,
                    now=self.clock().astimezone(UTC),
                    lease_seconds=self.scheduler_settings.lease_seconds,
                )
            except (PyMongoError, ScheduleConfigurationError):
                LOGGER.exception("Could not claim a due schedule safely.")
                continue
            if claimed is None:
                # A second worker claimed it after our list query.
                continue
            await self.execute_claimed_schedule(claimed, client)
            executed += 1
        return executed

    async def execute_claimed_schedule(
        self,
        claimed: ClaimedSchedule,
        client: httpx.AsyncClient,
    ) -> None:
        """Execute one finite lease and persist its final outcome."""

        started_at = self.clock().astimezone(UTC)
        run_id = str(uuid.uuid4())
        schedule_id = _optional_text(claimed.document, "id", "schedule_id", "_id") or "unknown"
        owner_id: str | None = None
        question: str | None = None
        validation_error: ScheduleConfigurationError | None = None
        try:
            owner_id = schedule_owner_id(claimed.document)
        except ScheduleConfigurationError as exc:
            validation_error = exc
        try:
            question = schedule_question(claimed.document)
        except ScheduleConfigurationError as exc:
            validation_error = validation_error or exc

        # The owner employee ID is deliberately part of a fresh random
        # session identity. It is never the interactive CUBE session ID.
        session_id = f"{SCHEDULE_SESSION_PREFIX}_{owner_id or 'unknown'}_{uuid.uuid4()}"
        status = "failed"
        error_category: str | None = None
        delivery_status = "not_sent"
        deactivate = False
        claim_still_current = True

        try:
            # A run document is inserted before validation so malformed active
            # schedules remain visible to operators instead of failing silently.
            self.repository.start_run(
                claimed,
                run_id=run_id,
                schedule_id=schedule_id,
                owner_id=owner_id,
                question=question,
                session_id=session_id,
                started_at=started_at,
            )
            if claimed.scheduled_for is None:
                raise ScheduleConfigurationError("next_run_at must be an ISO timestamp.")
            if validation_error is not None:
                raise validation_error
            assert owner_id is not None
            assert question is not None
            # Validate recurrence before invoking GAIA. A broken schedule is
            # deactivated below so it cannot flood its owner every poll.
            repeat_kind = _repeat_kind(claimed.document)
            data, metadata = build_scheduling_gaia_context(
                question=question,
                owner_id=owner_id,
                session_id=session_id,
                schedule_id=schedule_id,
            )
            gaia_response = await call_gaia(
                client,
                self.gaia_settings,
                owner_id,
                session_id,
                question,
                data=data,
                metadata=metadata,
            )
            answer = extract_final_answer(gaia_response)
            if not self._claim_allows_delivery(claimed):
                # Portal pause/edit/delete invalidates the token. Do not send
                # a response for a schedule the user no longer requested.
                claim_still_current = False
                status = "cancelled"
                error_category = "schedule_cancelled"
                delivery_status = "skipped_cancelled"
                LOGGER.info("Scheduled output skipped after Portal change: schedule=%s", schedule_id)
            else:
                await send_cube_message(
                    client,
                    self.gaia_settings,
                    owner_id,
                    "",  # personal DM only; no CUBE channel is used for a schedule
                    build_scheduled_result_message(question, answer),
                )
                status = "success"
                delivery_status = "answer_sent"
                deactivate = repeat_kind == "once"
        except ScheduleConfigurationError as exc:
            error_category = _safe_error_category(exc, phase="schedule")
            deactivate = True
            # When only recurrence/timing data is malformed, owner and
            # question remain usable. Send one clear, prefixed DM before the
            # schedule is deactivated. Invalid recipient/question documents
            # never send anything.
            if owner_id and question:
                if not self._claim_allows_delivery(claimed):
                    claim_still_current = False
                    status = "cancelled"
                    error_category = "schedule_cancelled"
                    delivery_status = "skipped_cancelled"
                else:
                    try:
                        await send_cube_message(
                            client,
                            self.gaia_settings,
                            owner_id,
                            "",
                            build_scheduled_result_message(
                                question,
                                SCHEDULE_CONFIGURATION_MESSAGE,
                            ),
                        )
                        delivery_status = "configuration_notice_sent"
                    except ExternalApiError:
                        # The configuration error is still the root cause;
                        # keep its safe category in the run history.
                        delivery_status = "configuration_notice_delivery_failed"
            LOGGER.warning("Invalid scheduler document was deactivated: schedule=%s", schedule_id)
        except (GaiaRequestError, GaiaResponseError) as exc:
            error_category = _safe_error_category(exc, phase="gaia")
            # A GAIA failure still produces one user-visible CUBE response with
            # the exact scheduler prefix. No temporary processing notice is sent.
            if owner_id and question:
                if not self._claim_allows_delivery(claimed):
                    claim_still_current = False
                    status = "cancelled"
                    error_category = "schedule_cancelled"
                    delivery_status = "skipped_cancelled"
                    LOGGER.info("Scheduled fallback skipped after Portal change: schedule=%s", schedule_id)
                else:
                    try:
                        await send_cube_message(
                            client,
                            self.gaia_settings,
                            owner_id,
                            "",
                            build_scheduled_result_message(
                                question,
                                _gaia_fallback_message(self.gaia_settings, exc),
                            ),
                        )
                        delivery_status = "fallback_sent"
                    except ExternalApiError:
                        delivery_status = "fallback_delivery_failed"
                        error_category = "cube_delivery"
            LOGGER.warning("Scheduled GAIA execution failed: schedule=%s category=%s", schedule_id, error_category)
        except ExternalApiError as exc:
            error_category = _safe_error_category(exc, phase="cube")
            delivery_status = "answer_delivery_failed"
            LOGGER.warning("Scheduled CUBE delivery failed: schedule=%s", schedule_id)
        except PyMongoError:
            # The lease will eventually expire if the run cannot be persisted.
            # Do not expose database details to CUBE users.
            error_category = "persistence"
            LOGGER.exception("Scheduler MongoDB operation failed: schedule=%s", schedule_id)
        except Exception:
            error_category = "unexpected"
            LOGGER.exception("Unexpected scheduler failure: schedule=%s", schedule_id)
        finally:
            completed_at = self.clock().astimezone(UTC)
            try:
                if not claim_still_current:
                    # Portal changed/deleted this document and invalidated the
                    # token. Preserve the Portal's new next_run/status values.
                    next_run_at = None
                elif error_category == "invalid_schedule":
                    next_run_at = None
                else:
                    next_run_at = calculate_next_run_at(
                        claimed.document,
                        now=completed_at,
                        scheduled_for=claimed.scheduled_for,
                    )
                    deactivate = deactivate or next_run_at is None
            except ScheduleConfigurationError:
                # Recurrence parsing can fail after an otherwise valid GAIA
                # request. Deactivate to avoid continuously replaying it.
                next_run_at = None
                deactivate = True
                status = "failed"
                error_category = "invalid_schedule"

            try:
                self.repository.finish_run(
                    run_id,
                    status=status,
                    error_category=error_category,
                    completed_at=completed_at,
                    delivery_status=delivery_status,
                    worker_id=self.scheduler_settings.worker_id,
                )
                if claim_still_current:
                    self.repository.complete_claim(
                        claimed,
                        next_run_at=next_run_at,
                        completed_at=completed_at,
                        status=status,
                        error_category=error_category,
                        deactivate=deactivate,
                    )
            except PyMongoError:
                LOGGER.exception("Could not finalize scheduled run: schedule=%s", schedule_id)

    async def run_forever(self) -> None:
        """Keep polling until the process receives Ctrl+C / SIGTERM."""

        self.repository.ensure_indexes()
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    executed = await self.run_once(client)
                    if executed:
                        LOGGER.info("Completed %d scheduled execution(s).", executed)
                except PyMongoError:
                    LOGGER.exception("MongoDB polling failed; the worker will retry.")
                await asyncio.sleep(self.scheduler_settings.poll_seconds)


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("PTMORE_SCHEDULER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(
        description="Run due PTMORE schedules from MongoDB through GAIA and CUBE."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim and process the current due batch once, then exit",
    )
    args = parser.parse_args(argv)
    try:
        gaia_settings = Settings.from_env()
        scheduler_settings = SchedulerSettings.from_env()
        scheduler_settings.validate_lease_duration(gaia_settings)
        mongo_client = MongoClient(
            scheduler_settings.mongodb_uri,
            serverSelectionTimeoutMS=scheduler_settings.mongodb_server_selection_timeout_ms,
        )
        database = mongo_client[scheduler_settings.mongodb_database]
        repository = MongoScheduleRepository(
            database[scheduler_settings.schedule_collection],
            database[scheduler_settings.schedule_run_collection],
        )
        # Fail early on a configuration/network mistake instead of appearing
        # healthy while no schedules can be read.
        mongo_client.admin.command("ping")
        worker = SchedulerWorker(
            gaia_settings=gaia_settings,
            scheduler_settings=scheduler_settings,
            repository=repository,
        )
        LOGGER.info(
            "Scheduler worker started: database=%s schedules=%s runs=%s worker=%s",
            scheduler_settings.mongodb_database,
            scheduler_settings.schedule_collection,
            scheduler_settings.schedule_run_collection,
            scheduler_settings.worker_id,
        )
        try:
            if args.once:
                repository.ensure_indexes()
                async with httpx.AsyncClient() as client:
                    executed = await worker.run_once(client)
                LOGGER.info("One-time scheduler scan completed: executed=%d", executed)
            else:
                await worker.run_forever()
        finally:
            mongo_client.close()
    except (SettingsError, PyMongoError, ValueError) as exc:
        # This is an operator-facing stderr message only. It deliberately does
        # not echo URI credentials or a remote response body.
        LOGGER.error("Scheduler worker could not start: %s", type(exc).__name__)
        print(f"Scheduler worker startup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
