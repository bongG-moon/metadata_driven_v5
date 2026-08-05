from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .config import CubeSchedulerConfig
from .models import ScheduleDefinition, ScheduledQuery, public_schedule
from .scheduling import definition_hash, next_run_after


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ScheduleSource:
    def __init__(self, config: CubeSchedulerConfig, client: Any | None = None) -> None:
        self.config = config
        if client is None:
            from pymongo import MongoClient

            client = MongoClient(
                config.source_mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10_000,
            )
        self.client = client
        self.collection = client[config.source_database][config.source_collection]

    def ping(self) -> None:
        self.client.admin.command("ping")

    def read_all(self) -> list[dict[str, Any]]:
        cursor = self.collection.find({}, {"_id": 0}).sort("schedule_id", ASCENDING).limit(
            self.config.source_limit + 1
        )
        documents = [public_schedule(item) for item in cursor]
        if len(documents) > self.config.source_limit:
            raise RuntimeError(
                "schedule source exceeds CUBE_SCHEDULE_SOURCE_LIMIT; "
                "reconciliation was stopped to avoid marking unread schedules as missing"
            )
        return documents

    def close(self) -> None:
        self.client.close()


class RuntimeStore:
    def __init__(self, config: CubeSchedulerConfig, client: Any | None = None) -> None:
        self.config = config
        if client is None:
            from pymongo import MongoClient

            client = MongoClient(
                config.runtime_mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10_000,
            )
        self.client = client
        database = client[config.runtime_database]
        self.cursors = database[config.cursor_collection]
        self.runs = database[config.run_collection]
        self.outbox = database[config.outbox_collection]
        self.inbound = database[config.inbound_collection]
        self.user_channels = database[config.user_channel_collection]

    def ensure_indexes(self) -> None:
        self.cursors.create_index([("status", ASCENDING), ("next_run_at", ASCENDING)])
        self.cursors.create_index("last_seen_sync_id")
        self.runs.create_index([("schedule_id", ASCENDING), ("scheduled_for", ASCENDING)])
        self.outbox.create_index([("status", ASCENDING), ("next_attempt_at", ASCENDING)])
        self.inbound.create_index("expires_at", expireAfterSeconds=0)
        self.user_channels.create_index("employee_id", unique=True)

    def ping(self) -> None:
        self.client.admin.command("ping")

    def close(self) -> None:
        self.client.close()

    def reconcile(self, documents: list[dict[str, Any]], now: datetime | None = None) -> dict[str, int]:
        timestamp = now or utc_now()
        sync_id = uuid.uuid4().hex
        counts = {"active": 0, "disabled": 0, "invalid": 0, "missing": 0}
        for raw in documents:
            try:
                definition = ScheduleDefinition.model_validate(raw)
            except ValidationError as exc:
                self._record_invalid(raw, exc, sync_id, timestamp)
                counts["invalid"] += 1
                continue
            channel_id = definition.channel_id or self.lookup_channel(definition.employee_id)
            if channel_id and channel_id != definition.channel_id:
                definition = definition.model_copy(update={"channel_id": channel_id})
            if not definition.enabled:
                status = "disabled"
            elif not definition.channel_id:
                status = "invalid_definition"
            else:
                status = "active"
            digest = definition_hash(definition)
            existing = self.cursors.find_one({"_id": definition.schedule_id}) or {}
            definition_document = definition.model_dump(mode="json")
            update: dict[str, Any] = {
                "schedule_id": definition.schedule_id,
                "source_version": definition.version,
                "definition_hash": digest,
                "definition": definition_document,
                "status": status,
                "validation_errors": [] if status != "invalid_definition" else ["channel_id is not available"],
                "last_seen_sync_id": sync_id,
                "source_updated_at": definition.updated_at,
                "updated_at": timestamp,
            }
            changed = existing.get("definition_hash") != digest or existing.get("status") != status
            if status == "active" and (changed or not existing.get("next_run_at")):
                update["next_run_at"] = next_run_after(
                    definition.schedule,
                    timestamp,
                    anchor_utc=definition.updated_at,
                )
            elif status != "active":
                update["next_run_at"] = None
                update["lease_owner"] = ""
                update["lease_until"] = None
            self.cursors.update_one(
                {"_id": definition.schedule_id},
                {
                    "$set": update,
                    "$setOnInsert": {"created_at": timestamp},
                },
                upsert=True,
            )
            counts["active" if status == "active" else "disabled" if status == "disabled" else "invalid"] += 1
        missing_result = self.cursors.update_many(
            {"last_seen_sync_id": {"$ne": sync_id}, "status": {"$ne": "missing"}},
            {
                "$set": {
                    "status": "missing",
                    "next_run_at": None,
                    "lease_owner": "",
                    "lease_until": None,
                    "updated_at": timestamp,
                }
            },
        )
        counts["missing"] = int(missing_result.modified_count)
        return counts

    def _record_invalid(
        self,
        raw: dict[str, Any],
        error: ValidationError,
        sync_id: str,
        timestamp: datetime,
    ) -> None:
        raw_id = str(raw.get("schedule_id") or "").strip()
        if not raw_id:
            encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            raw_id = "invalid:" + hashlib.sha256(encoded).hexdigest()[:24]
        self.cursors.update_one(
            {"_id": raw_id},
            {
                "$set": {
                    "schedule_id": raw_id,
                    "status": "invalid_definition",
                    "definition": public_schedule(raw),
                    "validation_errors": error.errors(include_url=False),
                    "next_run_at": None,
                    "last_seen_sync_id": sync_id,
                    "updated_at": timestamp,
                },
                "$setOnInsert": {"created_at": timestamp},
            },
            upsert=True,
        )

    def lookup_channel(self, employee_id: str) -> str:
        document = self.user_channels.find_one({"employee_id": employee_id}, {"channel_id": 1}) or {}
        return str(document.get("channel_id") or "").strip()

    def claim_due(self, worker_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        timestamp = now or utc_now()
        return self.cursors.find_one_and_update(
            {
                "status": "active",
                "next_run_at": {"$lte": timestamp},
                "$or": [
                    {"lease_until": {"$exists": False}},
                    {"lease_until": None},
                    {"lease_until": {"$lte": timestamp}},
                ],
            },
            {
                "$set": {
                    "lease_owner": worker_id,
                    "lease_until": timestamp + timedelta(seconds=self.config.lease_seconds),
                    "updated_at": timestamp,
                }
            },
            sort=[("next_run_at", ASCENDING), ("schedule_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def enqueue_claimed(self, cursor: dict[str, Any], worker_id: str, now: datetime | None = None) -> str:
        timestamp = now or utc_now()
        definition = ScheduleDefinition.model_validate(cursor["definition"])
        scheduled_for = cursor["next_run_at"]
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
        dedupe_key = f"{definition.schedule_id}:{scheduled_for.astimezone(timezone.utc).isoformat()}"
        run_id = "run:" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]
        query = ScheduledQuery(
            employee_id=definition.employee_id,
            channel_id=definition.channel_id,
            question=definition.question,
            schedule_id=definition.schedule_id,
            run_id=run_id,
            dedupe_key=dedupe_key,
        )
        self.runs.update_one(
            {"_id": run_id},
            {
                "$setOnInsert": {
                    "run_id": run_id,
                    "schedule_id": definition.schedule_id,
                    "scheduled_for": scheduled_for,
                    "dedupe_key": dedupe_key,
                    "status": "queued",
                    "created_at": timestamp,
                }
            },
            upsert=True,
        )
        self.outbox.update_one(
            {"_id": dedupe_key},
            {
                "$setOnInsert": {
                    "dedupe_key": dedupe_key,
                    "run_id": run_id,
                    "schedule_id": definition.schedule_id,
                    "payload": query.model_dump(mode="json"),
                    "status": "pending",
                    "attempts": 0,
                    "next_attempt_at": timestamp,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            },
            upsert=True,
        )
        next_run = next_run_after(
            definition.schedule,
            max(timestamp, scheduled_for),
            anchor_utc=definition.updated_at,
        )
        self.cursors.update_one(
            {
                "_id": definition.schedule_id,
                "lease_owner": worker_id,
                "next_run_at": scheduled_for,
            },
            {
                "$set": {
                    "last_scheduled_for": scheduled_for,
                    "next_run_at": next_run,
                    "lease_owner": "",
                    "lease_until": None,
                    "updated_at": timestamp,
                }
            },
        )
        return run_id

    def release_claim(self, schedule_id: str, worker_id: str, error: str) -> None:
        self.cursors.update_one(
            {"_id": schedule_id, "lease_owner": worker_id},
            {
                "$set": {
                    "lease_owner": "",
                    "lease_until": None,
                    "last_error": str(error)[:2000],
                    "updated_at": utc_now(),
                }
            },
        )

    def claim_outbox(self, worker_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        timestamp = now or utc_now()
        return self.outbox.find_one_and_update(
            {
                "$or": [
                    {
                        "status": {"$in": ["pending", "retry"]},
                        "next_attempt_at": {"$lte": timestamp},
                        "$or": [
                            {"lease_until": {"$exists": False}},
                            {"lease_until": None},
                            {"lease_until": {"$lte": timestamp}},
                        ],
                    },
                    {"status": "sending", "lease_until": {"$lte": timestamp}},
                ],
            },
            {
                "$set": {
                    "status": "sending",
                    "lease_owner": worker_id,
                    "lease_until": timestamp + timedelta(seconds=self.config.lease_seconds),
                    "updated_at": timestamp,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("next_attempt_at", ASCENDING), ("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def complete_outbox(self, document: dict[str, Any], response: dict[str, Any] | None) -> None:
        timestamp = utc_now()
        self.outbox.update_one(
            {"_id": document["_id"], "lease_owner": document.get("lease_owner")},
            {
                "$set": {
                    "status": "sent",
                    "sent_at": timestamp,
                    "response": response or {},
                    "lease_owner": "",
                    "lease_until": None,
                    "updated_at": timestamp,
                }
            },
        )
        self.runs.update_one(
            {"_id": document["run_id"]},
            {"$set": {"status": "accepted", "finished_at": timestamp, "updated_at": timestamp}},
        )

    def fail_outbox(self, document: dict[str, Any], message: str, retryable: bool) -> None:
        timestamp = utc_now()
        attempts = int(document.get("attempts") or 0)
        retry = retryable and attempts < self.config.max_delivery_attempts
        delay = min(300, self.config.retry_base_seconds * (2 ** max(0, attempts - 1)))
        status = "retry" if retry else "dead_letter"
        self.outbox.update_one(
            {"_id": document["_id"], "lease_owner": document.get("lease_owner")},
            {
                "$set": {
                    "status": status,
                    "last_error": str(message)[:2000],
                    "next_attempt_at": timestamp + timedelta(seconds=delay) if retry else None,
                    "lease_owner": "",
                    "lease_until": None,
                    "updated_at": timestamp,
                }
            },
        )
        self.runs.update_one(
            {"_id": document["run_id"]},
            {
                "$set": {
                    "status": status,
                    "last_error": str(message)[:2000],
                    "finished_at": None if retry else timestamp,
                    "updated_at": timestamp,
                }
            },
        )

    def record_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = payload.get("richnotificationmessage") if isinstance(payload, dict) else None
        message = message if isinstance(message, dict) else {}
        header = message.get("header") if isinstance(message.get("header"), dict) else {}
        process = message.get("process") if isinstance(message.get("process"), dict) else {}
        sender = header.get("from") if isinstance(header.get("from"), dict) else {}
        receiver = header.get("to") if isinstance(header.get("to"), dict) else {}
        employee_id = str(sender.get("uniquename") or "").strip()
        channels = receiver.get("channelid") if isinstance(receiver.get("channelid"), list) else []
        channel_id = str(channels[0] if channels else "").strip()
        process_data = str(process.get("processdata") or "")
        message_id = str(process.get("messageid") or process.get("requestid") or "").strip()
        if not message_id:
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            message_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        timestamp = utc_now()
        try:
            self.inbound.insert_one(
                {
                    "_id": message_id,
                    "message_id": message_id,
                    "employee_id": employee_id,
                    "channel_id": channel_id,
                    "process_data": process_data[:8000],
                    "created_at": timestamp,
                    "expires_at": timestamp + timedelta(days=30),
                }
            )
            duplicate = False
        except DuplicateKeyError:
            duplicate = True
        if employee_id and channel_id:
            self.user_channels.update_one(
                {"employee_id": employee_id},
                {"$set": {"channel_id": channel_id, "last_seen_at": timestamp}},
                upsert=True,
            )
        return {
            "message_id": message_id,
            "employee_id": employee_id,
            "channel_id": channel_id,
            "process_data": process_data,
            "duplicate": duplicate,
        }

    def list_runs(self, schedule_id: str, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self.runs.find({"schedule_id": schedule_id}, {"_id": 0}).sort(
            "scheduled_for", -1
        ).limit(max(1, min(limit, 500)))
        return list(cursor)
