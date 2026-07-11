from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from typing import Any


DEFAULT_DATABASE = "datagov"
DEFAULT_RESULT_COLLECTION = "agent_v4_result_store"


def load_data_ref_rows(
    data_ref: dict[str, Any],
    mongo_uri: str,
    default_database: str = DEFAULT_DATABASE,
    default_collection: str = DEFAULT_RESULT_COLLECTION,
    limit: int | None = None,
) -> dict[str, Any]:
    """MongoDB result store에 저장된 data_ref rows를 웹 표시용으로 읽어옵니다."""
    if not isinstance(data_ref, dict):
        raise ValueError("data_ref must be a dict.")
    ref_id = str(data_ref.get("ref_id") or "").strip()
    if not ref_id:
        raise ValueError("data_ref.ref_id is empty.")
    uri = str(mongo_uri or "").strip()
    if not uri:
        raise ValueError("Mongo URI is empty.")

    database = data_ref_database(data_ref, default_database)
    collection_name = data_ref_collection(data_ref, default_collection)
    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(uri, serverSelectionTimeoutMS=5000)
        collection = client[database][collection_name]
        document = _find_data_ref_document(collection, ref_id)
        if not isinstance(document, dict):
            return {
                "ok": False,
                "ref_id": ref_id,
                "rows": [],
                "columns": [],
                "row_count": 0,
                "database": database,
                "collection_name": collection_name,
                "message": "data_ref not found.",
            }
        loaded = rows_from_data_ref_document(document, limit=limit, path=data_ref_path(data_ref))
        loaded.update({"ref_id": ref_id, "database": database, "collection_name": collection_name})
        return loaded
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def rows_from_data_ref_document(document: dict[str, Any], limit: int | None = None, path: str = "") -> dict[str, Any]:
    """result store document에서 rows/columns/row_count를 표준 형태로 추출합니다."""
    if not isinstance(document, dict):
        return {"ok": False, "rows": [], "columns": [], "row_count": 0, "message": "document is not a dict."}
    expires_at = data_ref_document_expires_at(document)
    if expires_at and expires_at <= datetime.now(timezone.utc):
        return {
            "ok": False,
            "expired": True,
            "rows": [],
            "columns": [],
            "row_count": 0,
            "expires_at": _to_iso(expires_at),
            "message": "data_ref expired.",
        }
    selected = _value_at_path(document, path) if path else None
    rows = _row_list(selected)
    if not rows and isinstance(selected, dict):
        rows = _row_list(selected.get("rows")) or _row_list(selected.get("data"))
    if not rows:
        rows = _row_list(document.get("rows"))
    if not rows:
        rows = _row_list(document.get("data"))
    if not rows:
        rows = _row_list((document.get("data") or {}).get("rows") if isinstance(document.get("data"), dict) else None)
    if not rows:
        rows = _row_list((document.get("result") or {}).get("rows") if isinstance(document.get("result"), dict) else None)
    if not rows:
        payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
        payload_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = _row_list(payload_data.get("rows")) or _row_list(payload_data.get("data"))

    selected_dict = selected if isinstance(selected, dict) else {}
    row_count = _positive_int(selected_dict.get("row_count"), _positive_int(document.get("row_count"), len(rows)))
    columns = _string_list(selected_dict.get("columns")) or _string_list(document.get("columns")) or _rows_columns(rows)
    visible_rows = deepcopy(rows)
    if isinstance(limit, int) and limit >= 0:
        visible_rows = visible_rows[:limit]
    return {
        "ok": True,
        "rows": visible_rows,
        "columns": columns,
        "row_count": row_count,
        "path": document.get("path", ""),
        "expires_at": _to_iso(expires_at) if expires_at else "",
        "metadata": deepcopy(document.get("metadata")) if isinstance(document.get("metadata"), dict) else {},
    }


def data_ref_document_expired(document: dict[str, Any]) -> bool:
    expires_at = data_ref_document_expires_at(document)
    return bool(expires_at and expires_at <= datetime.now(timezone.utc))


def data_ref_document_expires_at(document: dict[str, Any]) -> datetime | None:
    if not isinstance(document, dict):
        return None
    for key in ("expires_at", "expires_at_iso"):
        parsed = _parse_datetime(document.get(key))
        if parsed:
            return parsed
    return None


def data_ref_database(data_ref: dict[str, Any], default_database: str = DEFAULT_DATABASE) -> str:
    for key in ("database", "db_name", "mongo_database"):
        value = str(data_ref.get(key) or "").strip()
        if value:
            return value
    return str(default_database or DEFAULT_DATABASE)


def data_ref_collection(data_ref: dict[str, Any], default_collection: str = DEFAULT_RESULT_COLLECTION) -> str:
    for key in ("collection_name", "collection", "result_collection_name"):
        value = str(data_ref.get(key) or "").strip()
        if value:
            return value
    return str(default_collection or DEFAULT_RESULT_COLLECTION)


def data_ref_path(data_ref: dict[str, Any]) -> str:
    return str(data_ref.get("path") or data_ref.get("row_path") or "").strip()


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
            continue
        return None
    return current


def _find_data_ref_document(collection: Any, ref_id: str) -> dict[str, Any] | None:
    for query in ({"ref_id": ref_id}, {"_id": ref_id}, {"data_ref_id": ref_id}):
        try:
            document = collection.find_one(query, {"_id": 0})
        except TypeError:
            document = collection.find_one(query)
        if isinstance(document, dict):
            return document
    return None


def _row_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(0, parsed)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
