from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from typing import Any


DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTIONS = {
    "domain": "agent_v4_domain_items",
    "table_catalog": "agent_v4_table_catalog_items",
    "main_flow_filter": "agent_v4_main_flow_filters",
}


def load_metadata_items(
    metadata_type: str,
    mongo_uri: str,
    mongo_database: str = DEFAULT_DATABASE,
    collection_name: str = "",
    status: str = "all",
) -> dict[str, Any]:
    """Standalone web 배포에서 MongoDB metadata 컬렉션을 직접 조회합니다."""
    kind = normalize_metadata_type(metadata_type)
    uri = str(mongo_uri or "").strip()
    if not uri:
        return {"ok": False, "items": [], "message": "MONGODB_URI가 설정되어 있지 않습니다."}
    database = str(mongo_database or DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    collection = str(collection_name or DEFAULT_COLLECTIONS[kind]).strip() or DEFAULT_COLLECTIONS[kind]
    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(uri, serverSelectionTimeoutMS=5000)
        query = {} if status == "all" else {"status": status}
        cursor = client[database][collection].find(query, {"_id": 0})
        sort_fields = sort_spec_for(kind)
        try:
            cursor = cursor.sort(sort_fields)
        except Exception:
            pass
        items = [normalize_metadata_document(kind, doc) for doc in cursor if isinstance(doc, dict)]
        return {"ok": True, "items": items, "database": database, "collection_name": collection, "message": ""}
    except Exception as exc:
        return {"ok": False, "items": [], "database": database, "collection_name": collection, "message": str(exc)}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def mark_metadata_deleted(
    metadata_type: str,
    mongo_uri: str,
    mongo_database: str = DEFAULT_DATABASE,
    collection_name: str = "",
    item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark one metadata document as deleted without removing it from MongoDB."""
    kind = normalize_metadata_type(metadata_type)
    uri = str(mongo_uri or "").strip()
    if not uri:
        return {"ok": False, "message": "MONGODB_URI is empty.", "matched_count": 0, "modified_count": 0}
    query = delete_query_for(kind, item or {})
    if not query:
        return {"ok": False, "message": "Cannot identify metadata item to delete.", "matched_count": 0, "modified_count": 0}

    database = str(mongo_database or DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    collection = str(collection_name or DEFAULT_COLLECTIONS[kind]).strip() or DEFAULT_COLLECTIONS[kind]
    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(uri, serverSelectionTimeoutMS=5000)
        update = {
            "$set": {
                "status": "deleted",
                "deleted_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        result = client[database][collection].update_one(query, update)
        matched_count = int(getattr(result, "matched_count", 0) or 0)
        modified_count = int(getattr(result, "modified_count", 0) or 0)
        ok = matched_count > 0
        message = "Metadata item marked as deleted." if ok else "No matching metadata item found."
        return {
            "ok": ok,
            "message": message,
            "database": database,
            "collection_name": collection,
            "query": query,
            "matched_count": matched_count,
            "modified_count": modified_count,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc), "database": database, "collection_name": collection, "query": query, "matched_count": 0, "modified_count": 0}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def delete_query_for(metadata_type: str, item: dict[str, Any]) -> dict[str, Any]:
    kind = normalize_metadata_type(metadata_type)
    if kind == "domain":
        section = str(item.get("section") or item.get("gbn") or "").strip()
        key = str(item.get("key") or "").strip()
        return {"$or": [{"section": section, "key": key}, {"gbn": section, "key": key}]} if section and key else {}
    if kind == "table_catalog":
        dataset_key = str(item.get("dataset_key") or item.get("key") or "").strip()
        return {"$or": [{"dataset_key": dataset_key}, {"key": dataset_key}]} if dataset_key else {}
    filter_key = str(item.get("filter_key") or item.get("key") or "").strip()
    return {"$or": [{"filter_key": filter_key}, {"key": filter_key}]} if filter_key else {}


def normalize_metadata_document(metadata_type: str, document: dict[str, Any]) -> dict[str, Any]:
    kind = normalize_metadata_type(metadata_type)
    doc = deepcopy(document)
    payload = doc.get("payload") if isinstance(doc.get("payload"), dict) else {}
    if kind == "domain":
        doc.setdefault("type", "domain")
        doc.setdefault("section", doc.get("gbn") or "")
        doc.setdefault("gbn", doc.get("section") or "")
        doc.setdefault("key", "")
        doc.setdefault("status", "active")
        doc.setdefault("display_name", payload.get("display_name") or doc.get("key") or "")
        if "aliases" not in doc and isinstance(payload.get("aliases"), list):
            doc["aliases"] = list(payload["aliases"])
        doc["registration_trace"] = normalize_registration_trace(doc)
        doc["payload"] = payload
        return doc
    if kind == "table_catalog":
        source_config = payload.get("source_config") if isinstance(payload.get("source_config"), dict) else {}
        doc.setdefault("type", "table_catalog")
        doc.setdefault("dataset_key", doc.get("key") or "")
        doc.setdefault("key", doc.get("dataset_key") or "")
        doc.setdefault("status", "active")
        doc.setdefault("display_name", payload.get("display_name") or doc.get("dataset_key") or "")
        doc.setdefault("dataset_family", payload.get("dataset_family") or "")
        doc.setdefault("source_type", payload.get("source_type") or source_config.get("source_type") or "")
        doc["registration_trace"] = normalize_registration_trace(doc)
        doc["payload"] = payload
        return doc
    doc.setdefault("type", "main_flow_filter")
    doc.setdefault("filter_key", doc.get("key") or "")
    doc.setdefault("key", doc.get("filter_key") or "")
    doc.setdefault("status", "active")
    doc.setdefault("display_name", payload.get("display_name") or payload.get("description") or doc.get("filter_key") or "")
    doc.setdefault("column_candidates", payload.get("column_candidates") if isinstance(payload.get("column_candidates"), list) else [])
    doc.setdefault("semantic_role", payload.get("semantic_role") or payload.get("value_type") or "")
    doc["registration_trace"] = normalize_registration_trace(doc)
    doc["payload"] = payload
    return doc


def collection_name_for(metadata_type: str, settings: Any) -> str:
    kind = normalize_metadata_type(metadata_type)
    attr = {
        "domain": "domain_collection",
        "table_catalog": "table_catalog_collection",
        "main_flow_filter": "main_flow_filter_collection",
    }[kind]
    return str(getattr(settings, attr, "") or DEFAULT_COLLECTIONS[kind])


def sort_spec_for(metadata_type: str) -> list[tuple[str, int]]:
    kind = normalize_metadata_type(metadata_type)
    if kind == "domain":
        return [("section", 1), ("key", 1)]
    if kind == "table_catalog":
        return [("dataset_key", 1)]
    return [("filter_key", 1)]


def normalize_metadata_type(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"domain", "domains"}:
        return "domain"
    if text in {"table", "table_catalog", "catalog", "data_catalog"}:
        return "table_catalog"
    return "main_flow_filter"


def normalize_registration_trace(document: dict[str, Any]) -> dict[str, Any]:
    trace = document.get("registration_trace") if isinstance(document.get("registration_trace"), dict) else {}
    if not trace and isinstance(document.get("authoring_trace"), dict):
        trace = document["authoring_trace"]
    if not trace and isinstance(document.get("trace"), dict):
        trace = document["trace"]
    result = {
        "raw_text": trace.get("raw_text") or document.get("raw_text") or document.get("source_text") or "",
        "refined_text": trace.get("refined_text") or document.get("refined_text") or "",
        "reviewed_at": trace.get("reviewed_at") or document.get("reviewed_at") or "",
    }
    return {key: value for key, value in result.items() if str(value or "").strip()}


normalize_authoring_trace = normalize_registration_trace
