"""MongoDB persistence for generated HTML reports.

Each report is one normal MongoDB document in the configured collection.  The
document contains both the HTML text and the metadata needed to locate, expire,
authorize, and download that report.  The public browser URL remains a FastAPI
URL; this module never exposes a MongoDB URI to a browser.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError


class ReportStoreError(Exception):
    """Storage failure that can be returned safely by the FastAPI layer."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = str(message)


def storage_descriptor(config: Any) -> dict[str, str]:
    """Return non-secret storage metadata for API responses and diagnostics."""
    return {
        "backend": "mongodb_collection",
        "database": str(config.report_database),
        "collection": str(config.report_collection),
    }


def ensure_report_store(config: Any) -> None:
    """Check the configured MongoDB report collection and create its indexes."""
    client, _, collection = _open_store(config)
    try:
        _ensure_indexes(collection)
    finally:
        client.close()


def report_store_readiness(config: Any) -> dict[str, Any]:
    """Check whether the MongoDB-backed report collection is usable."""
    try:
        client, _, collection = _open_store(config)
        try:
            _ensure_indexes(collection)
        finally:
            client.close()
    except ReportStoreError as exc:
        return {
            "ok": False,
            "storage": storage_descriptor(config),
            "error": exc.message,
        }
    return {"ok": True, "storage": storage_descriptor(config), "error": ""}


def save_report(
    config: Any,
    metadata: dict[str, Any],
    html_bytes: bytes,
) -> dict[str, Any]:
    """Save HTML and metadata together as one MongoDB document."""
    try:
        html = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportStoreError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "report HTML is not valid UTF-8",
        ) from exc

    client, _, collection = _open_store(config)
    try:
        _ensure_indexes(collection)
        _cleanup_expired_unlocked(collection)
        _enforce_storage_limit(collection, config, len(html_bytes))
        stored = {
            **metadata,
            "storage_backend": "mongodb_collection",
            "html": html,
        }
        try:
            collection.insert_one(stored)
        except DuplicateKeyError as exc:
            raise ReportStoreError(HTTPStatus.CONFLICT, "report_id collision") from exc
        except Exception as exc:  # noqa: BLE001
            raise ReportStoreError(
                HTTPStatus.INSUFFICIENT_STORAGE,
                "failed to store report in MongoDB",
            ) from exc
        return stored
    finally:
        client.close()


def get_active_report_metadata(config: Any, report_id: str) -> dict[str, Any]:
    """Load one report document and enforce expiry before content is read."""
    client, _, collection = _open_store(config)
    try:
        try:
            metadata = collection.find_one({"report_id": report_id})
        except Exception as exc:  # noqa: BLE001
            raise ReportStoreError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "failed to read report from MongoDB",
            ) from exc
        if not isinstance(metadata, dict):
            raise ReportStoreError(HTTPStatus.NOT_FOUND, "report not found")
        expires_at = _as_utc_datetime(metadata.get("expires_at"))
        if expires_at is None:
            _delete_report_document(collection, metadata)
            raise ReportStoreError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "report metadata is invalid",
            )
        if expires_at <= datetime.now(timezone.utc):
            _delete_report_document(collection, metadata)
            raise ReportStoreError(HTTPStatus.GONE, "report expired")
        return metadata
    finally:
        client.close()


def read_report_html(
    config: Any,
    metadata: dict[str, Any],
) -> bytes:
    """Read HTML from the already-authorized report document."""
    html = metadata.get("html")
    if not isinstance(html, str):
        raise ReportStoreError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "report HTML is invalid",
        )
    html_bytes = html.encode("utf-8")
    if len(html_bytes) > int(config.max_report_html_bytes):
        raise ReportStoreError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "stored report exceeds configured size limit",
        )
    return html_bytes


def delete_report(config: Any, report_id: str) -> dict[str, Any] | None:
    """Delete the one MongoDB document that holds a report."""
    client, _, collection = _open_store(config)
    try:
        try:
            metadata = collection.find_one_and_delete({"report_id": report_id})
        except Exception as exc:  # noqa: BLE001
            raise ReportStoreError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "failed to delete report from MongoDB",
            ) from exc
        return metadata if isinstance(metadata, dict) else None
    finally:
        client.close()


def cleanup_expired_reports(config: Any) -> int:
    """Delete expired report documents."""
    if not str(getattr(config, "report_mongo_uri", "") or "").strip():
        return 0
    client, _, collection = _open_store(config)
    try:
        _ensure_indexes(collection)
        return _cleanup_expired_unlocked(collection)
    finally:
        client.close()


def _open_store(config: Any) -> tuple[Any, Any, Any]:
    uri = str(getattr(config, "report_mongo_uri", "") or "").strip()
    if not uri:
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "MongoDB report storage is not configured.",
        )
    database_name = str(getattr(config, "report_database", "") or "").strip()
    collection_name = str(getattr(config, "report_collection", "") or "").strip()
    if not database_name or not collection_name:
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "MongoDB report storage configuration is incomplete.",
        )

    client: Any = None
    try:
        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
        client.admin.command("ping")
        database = client[database_name]
        return client, database, database[collection_name]
    except ReportStoreError:
        if client is not None:
            client.close()
        raise
    except Exception as exc:  # noqa: BLE001
        if client is not None:
            client.close()
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            f"MongoDB report storage is unavailable: {type(exc).__name__}",
        ) from exc


def _ensure_indexes(collection: Any) -> None:
    try:
        collection.create_index(
            [("report_id", ASCENDING)],
            unique=True,
            name="report_id_unique",
        )
        collection.create_index(
            [("expires_at", ASCENDING)],
            name="expires_at_asc",
        )
        collection.create_index(
            [("created_at", DESCENDING)],
            name="created_at_desc",
        )
    except PyMongoError as exc:
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "failed to prepare MongoDB report indexes",
        ) from exc


def _cleanup_expired_unlocked(collection: Any) -> int:
    now = datetime.now(timezone.utc)
    deleted = 0
    try:
        expired_reports = list(collection.find({"expires_at": {"$lte": now}}))
    except Exception as exc:  # noqa: BLE001
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "failed to query expired reports",
        ) from exc
    for metadata in expired_reports:
        if not isinstance(metadata, dict):
            continue
        if _delete_report_document(collection, metadata):
            deleted += 1
    return deleted


def _delete_report_document(collection: Any, metadata: dict[str, Any]) -> bool:
    identifier = metadata.get("_id")
    query = {"_id": identifier} if identifier is not None else {
        "report_id": metadata.get("report_id")
    }
    try:
        result = collection.delete_one(query)
    except Exception as exc:  # noqa: BLE001
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "failed to delete expired report",
        ) from exc
    return int(getattr(result, "deleted_count", 0) or 0) > 0


def _enforce_storage_limit(
    collection: Any,
    config: Any,
    required_bytes: int,
) -> None:
    maximum = int(config.max_report_storage_bytes)
    if required_bytes > maximum:
        raise ReportStoreError(
            HTTPStatus.INSUFFICIENT_STORAGE,
            "report is larger than the total storage limit",
        )
    try:
        current_size = sum(
            max(0, int(document.get("html_bytes") or 0))
            for document in collection.find({}, {"html_bytes": 1})
            if isinstance(document, dict)
        )
    except Exception as exc:  # noqa: BLE001
        raise ReportStoreError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "failed to measure MongoDB report storage",
        ) from exc
    if current_size + required_bytes > maximum:
        raise ReportStoreError(
            HTTPStatus.INSUFFICIENT_STORAGE,
            "report storage limit exceeded",
        )


def _as_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
