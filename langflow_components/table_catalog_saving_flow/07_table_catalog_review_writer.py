from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

TRUNCATED = ("...", "생략", "omitted", "truncated")
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_table_catalog_items"
COLLECTION_ENV = "MONGODB_TABLE_CATALOG_COLLECTION"
ALLOWED_SOURCE_CONFIG_KEYS = {
    "source_type", "db_key", "query_template", "api_url", "url", "endpoint", "endpoint_id", "method",
    "headers", "params", "query_params", "body", "payload", "response_path", "doc_id", "sheet_name", "token_source", "token_key",
}
SAFE_REFERENCE_KEYS = {"token_source", "token_key"}
SECRET_PATTERNS = ("password", "passwd", "token", "secret", "api_key", "apikey", "authorization", "credential", "access_key", "private_key", "cookie")


def review_and_write(payload_value: Any, review_response: Any = "", mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    dry_run = bool(_dict(payload.get("request")).get("dry_run", True))
    action = _duplicate_action(payload)
    deterministic_errors = _deterministic_errors(payload)
    llm_review = _json(review_response)
    review = _merge_review(llm_review, payload, deterministic_errors)
    ready = bool(review.get("ready_to_save"))
    next_payload = deepcopy(payload)
    next_payload["review"] = review
    if not ready:
        next_payload["write_result"] = {
            "success": False,
            "ready_to_save": False,
            "saved_count": 0,
            "message": "필수 검증을 통과하지 못해 저장하지 않았습니다.",
            "errors": review.get("errors", []),
        }
    elif dry_run:
        next_payload["write_result"] = _dry_run_result(payload, action)
    else:
        next_payload["write_result"] = _write_to_mongodb(payload, action, mongo_uri, mongo_database, collection_name)
    return next_payload


def _deterministic_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = []
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        item_key = str(item.get("dataset_key") or "")
        p = _dict(item.get("payload"))
        sc = _dict(p.get("source_config"))
        source_type = str(p.get("source_type") or sc.get("source_type") or "").lower()
        if not item_key:
            errors.append({"type": "missing_key", "message": "dataset_key가 없습니다."})
        if not source_type:
            errors.append({"type": "missing_source_type", "message": "payload.source_type이 없습니다.", "key": item_key})
        if source_type in {"oracle", "datalake"}:
            query = str(sc.get("query_template") or "")
            if not query:
                errors.append({"type": "missing_query_template", "message": "Oracle/Datalake dataset에는 query_template이 필요합니다.", "key": item_key})
            if any(marker in query.lower() for marker in TRUNCATED):
                errors.append({"type": "truncated_query", "message": "query_template이 축약되어 저장하지 않습니다.", "key": item_key})
        if source_type == "goodocs" and not sc.get("doc_id"):
            errors.append({"type": "missing_doc_id", "message": "Goodocs dataset에는 doc_id가 필요합니다.", "key": item_key})
        for field in sc:
            if str(field) not in ALLOWED_SOURCE_CONFIG_KEYS:
                errors.append({"type": "forbidden_source_config_key", "message": f"허용되지 않은 source_config 필드입니다: {field}", "key": item_key, "field": str(field)})
        for path in _secret_paths(item):
            errors.append({"type": "credential_field_forbidden", "message": f"credential/secret 필드는 저장할 수 없습니다: {path}", "key": item_key, "field": path})
    return _unique_errors(errors)


def _merge_review(llm_review: dict[str, Any], payload: dict[str, Any], deterministic_errors: list[dict[str, Any]]) -> dict[str, Any]:
    if not llm_review:
        errors = list(deterministic_errors)
        return {"ready_to_save": bool(payload.get("items")) and not errors, "errors": errors, "supplement_requests": []}
    merged = deepcopy(llm_review)
    merged_errors = _list(merged.get("errors")) + deterministic_errors
    supplements = _list(merged.get("supplement_requests"))
    merged["errors"] = _unique_errors(merged_errors)
    merged["supplement_requests"] = supplements
    merged["ready_to_save"] = bool(merged.get("ready_to_save")) and not merged["errors"] and not supplements and bool(payload.get("items"))
    return merged


def _dry_run_result(payload: dict[str, Any], action: str) -> dict[str, Any]:
    matched = _match_map(payload)
    operations = []
    for item in payload.get("items", []):
        key = str(_dict(item).get("dataset_key") or "")
        has_match = key.lower() in matched
        operation = "skipped" if has_match and action == "skip" else "create_new" if has_match and action == "create_new" else "merged" if has_match and action == "merge" else "replaced" if has_match else "inserted"
        operations.append({"key": key, "operation": operation})
    would_save = sum(1 for item in operations if item["operation"] != "skipped")
    return {"success": True, "ready_to_save": True, "dry_run": True, "saved_count": 0, "would_save_count": would_save, "skipped_count": len(operations) - would_save, "operation_by_key": operations, "message": "드라이런입니다. MongoDB에는 저장하지 않았습니다.", "keys": [item["key"] for item in operations]}


def _write_to_mongodb(payload: dict[str, Any], action: str, mongo_uri: str, mongo_database: str, collection_name: str) -> dict[str, Any]:
    mongo_uri, mongo_database, collection_name = _resolve_mongo_config(mongo_uri, mongo_database, collection_name)
    if not mongo_uri or not mongo_database or not collection_name:
        return {"success": False, "ready_to_save": False, "saved_count": 0, "message": "MongoDB 저장 정보가 부족해 저장하지 않았습니다.", "errors": [{"type": "missing_mongo_config", "message": "mongo_uri, mongo_database, collection_name are required"}]}
    client = None
    operations = []
    try:
        client = getattr(import_module("pymongo"), "MongoClient")(mongo_uri, serverSelectionTimeoutMS=5000)
        collection = client[mongo_database][collection_name]
        now = datetime.now(timezone.utc).isoformat()
        raw_text = _redact_raw_text(str(_dict(payload.get("request")).get("raw_text") or ""))
        matched = _match_map(payload)
        for source_item in payload.get("items", []):
            item = deepcopy(source_item)
            key = str(item.get("dataset_key") or "")
            existing = deepcopy(matched.get(key.lower()) or collection.find_one({"_id": f"table_catalog:{key}"}) or {})
            if existing and action == "skip":
                operations.append({"key": key, "operation": "skipped"})
                continue
            if existing and action == "create_new":
                key = _next_key(collection, key)
                item["dataset_key"] = key
                existing = {}
                operation = "created_new"
            elif existing and action == "merge":
                item = _deep_merge(existing, item)
                item["dataset_key"] = key
                operation = "merged"
            elif existing:
                operation = "replaced"
            else:
                operation = "inserted"
            doc = deepcopy(item)
            doc["_id"] = f"table_catalog:{doc.get('dataset_key')}"
            if existing.get("created_at") and not doc.get("created_at"):
                doc["created_at"] = existing["created_at"]
            doc["updated_at"] = now
            if raw_text:
                doc["registration_trace"] = {"raw_text": raw_text}
            collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            operations.append({"key": doc.get("dataset_key"), "operation": operation})
        saved_count = sum(1 for item in operations if item["operation"] != "skipped")
        skipped_count = len(operations) - saved_count
        return {"success": True, "ready_to_save": True, "status": "skipped" if not saved_count and skipped_count else "saved", "saved_count": saved_count, "skipped_count": skipped_count, "operation_by_key": operations, "database": mongo_database, "collection_name": collection_name, "message": "저장 처리를 완료했습니다.", "errors": []}
    except Exception as exc:
        saved_count = sum(1 for item in operations if item.get("operation") != "skipped")
        return {"success": False, "ready_to_save": False, "status": "partial_success" if saved_count else "error", "saved_count": saved_count, "partial_success": bool(saved_count), "operation_by_key": operations, "message": "MongoDB 저장 중 오류가 발생했습니다.", "errors": [{"type": "mongo_write_error", "message": str(exc)}]}
    finally:
        if client is not None:
            client.close()


def _match_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for match in _list(payload.get("existing_matches")):
        match = _dict(match)
        key = str(match.get("existing_key") or match.get("new_key") or "").lower()
        existing = _dict(match.get("existing_item"))
        if key and existing:
            result[key] = existing
    return result


def _next_key(collection: Any, key: str) -> str:
    base = f"{key}_copy"
    candidate = base
    index = 2
    while collection.find_one({"_id": f"table_catalog:{candidate}"}):
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _deep_merge(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    for key, value in incoming.items():
        if key == "_id":
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            lowered = key_text.lower()
            if lowered not in SAFE_REFERENCE_KEYS and _is_secret_key(lowered):
                paths.append(path)
            else:
                paths.extend(_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_secret_paths(item, f"{prefix}[{index}]"))
    return paths


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key or "").lower())
    return any(pattern in normalized for pattern in SECRET_PATTERNS)


def _redact_raw_text(value: str, limit: int = 2000) -> str:
    text = str(value or "")
    pattern = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")
    return pattern.sub(r"\1\2***", text)[:limit]


def _duplicate_action(payload: dict[str, Any]) -> str:
    request = _dict(payload.get("request"))
    decision = _dict(payload.get("duplicate_decision"))
    action = str(request.get("duplicate_action") or decision.get("action") or "skip")
    return action if action in {"merge", "replace", "skip", "create_new"} else "skip"


def _resolve_mongo_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (mongo_uri or os.getenv("MONGODB_URI", ""), mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE), collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION))


def _unique_errors(errors: list[Any]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for error in errors:
        item = error if isinstance(error, dict) else {"type": "review_error", "message": str(error)}
        marker = (str(item.get("type")), str(item.get("field")), str(item.get("key")), str(item.get("message")))
        if marker not in seen:
            seen.add(marker)
            result.append(deepcopy(item))
    return result


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = str(getattr(value, "text", value) or "")
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class TableCatalogReviewWriter(Component):
    display_name = "07 테이블 카탈로그 검수/저장 처리기"
    description = "스키마·credential·중복 action을 결정론적으로 검증한 뒤 드라이런 또는 MongoDB 저장을 실행합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True), MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True), MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        return Data(data=review_and_write(getattr(self, "payload", None), "", getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", "")))
