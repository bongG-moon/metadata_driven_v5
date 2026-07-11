# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 07 도메인 검수/저장 처리기
# 역할: 스키마·credential·중복 action을 결정론적으로 검증한 뒤 드라이런 또는 MongoDB 저장을 실행합니다.
# 주요 입력: 페이로드 (payload) · 필수, MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 컬렉션 이름 (collection_name)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 도메인 필수 필드·비밀값·중복 정책을 결정론적으로 검증하고 dry-run 계획 또는 MongoDB 저장을 수행합니다.
# 유지보수 포인트: 연결 설정은 노드 입력→환경변수→기본값 순으로 해석하며, 오류는 숨기지 않고 trace/status에 남기고 연결은 반드시 닫습니다.
# =============================================================================

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

ALLOWED_SECTIONS = {"process_groups", "product_terms", "quantity_terms", "metric_terms", "analysis_recipes", "status_terms", "product_key_columns", "pandas_function_cases"}
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_domain_items"
COLLECTION_ENV = "MONGODB_DOMAIN_COLLECTION"
SAFE_REFERENCE_KEYS = {"token_source", "token_key"}
SECRET_PATTERNS = ("password", "passwd", "token", "secret", "api_key", "apikey", "authorization", "credential", "access_key", "private_key", "cookie")


# 주요 함수: 결정론적 검증과 duplicate 정책을 적용하고 dry-run 계획 또는 실제 저장을 수행합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def review_and_write(payload_value: Any, review_response: Any = "", mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    dry_run = bool(_dict(payload.get("request")).get("dry_run", True))
    action = _duplicate_action(payload)
    deterministic_review = _deterministic_review(payload)
    lookup_errors = _identity_lookup_errors(payload, action)
    deterministic_review["errors"] = _unique_errors(_list(deterministic_review.get("errors")) + lookup_errors)
    deterministic_review["ready_to_save"] = bool(deterministic_review.get("ready_to_save")) and not deterministic_review["errors"]
    if lookup_errors:
        for item_review in _list(deterministic_review.get("item_reviews")):
            if isinstance(item_review, dict):
                item_review["ready_to_save"] = False
                item_review["errors"] = _unique_errors(_list(item_review.get("errors")) + lookup_errors)
    llm_review = _json(review_response)
    review = _merge_review(llm_review, deterministic_review)
    ready = bool(review.get("ready_to_save"))
    next_payload = deepcopy(payload)
    next_payload["review"] = review
    if not ready:
        next_payload["write_result"] = {"success": False, "ready_to_save": False, "saved_count": 0, "message": "필수 검증을 통과하지 못해 저장하지 않았습니다.", "errors": review.get("errors", [])}
    elif dry_run:
        next_payload["write_result"] = _dry_run_result(payload, action)
    else:
        next_payload["write_result"] = _write_to_mongodb(payload, action, mongo_uri, mongo_database, collection_name)
    return next_payload


def _deterministic_review(payload: dict[str, Any]) -> dict[str, Any]:
    errors = []
    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        key = _key(item)
        if not item.get("section") or item.get("section") not in ALLOWED_SECTIONS:
            errors.append({"type": "unsupported_section", "message": f"지원하지 않는 section입니다: {item.get('section')}", "key": key})
        if not item.get("key"):
            errors.append({"type": "missing_key", "message": "key가 없습니다."})
        if not item.get("payload"):
            errors.append({"type": "missing_payload", "message": "payload가 비어 있습니다.", "key": key})
        p = _dict(item.get("payload"))
        if "source_config" in p or "query_template" in p:
            errors.append({"type": "domain_source_config_forbidden", "message": "domain에는 source/query config를 저장하지 않습니다.", "key": key})
        for path in _secret_paths(item):
            errors.append({"type": "credential_field_forbidden", "message": f"credential/secret 필드는 저장할 수 없습니다: {path}", "key": key, "field": path})
    errors = _unique_errors(errors)
    return {"ready_to_save": bool(payload.get("items")) and not errors, "item_reviews": [{"key": _key(item), "ready_to_save": not errors, "warnings": [], "errors": errors} for item in payload.get("items", []) if isinstance(item, dict)], "errors": errors, "supplement_requests": []}


def _identity_lookup_errors(payload: dict[str, Any], action: str) -> list[dict[str, Any]]:
    if action == "create_new":
        return []
    trace = _dict(payload.get("trace"))
    lookup = _dict(trace.get("duplicate_lookup"))
    if lookup.get("status") != "error":
        return []
    return [
        {
            "type": "identity_lookup_unavailable",
            "message": "기존 도메인 identity 조회에 실패해 중복 여부를 확정할 수 없으므로 저장하지 않았습니다.",
            "lookup_errors": deepcopy(_list(lookup.get("errors"))),
        }
    ]


def _merge_review(llm_review: dict[str, Any], deterministic_review: dict[str, Any]) -> dict[str, Any]:
    if not llm_review:
        return deepcopy(deterministic_review)
    merged = deepcopy(llm_review)
    errors = _unique_errors(_list(merged.get("errors")) + _list(deterministic_review.get("errors")))
    supplements = _list(merged.get("supplement_requests"))
    merged["errors"] = errors
    merged["supplement_requests"] = supplements
    merged["ready_to_save"] = bool(merged.get("ready_to_save")) and bool(deterministic_review.get("ready_to_save")) and not errors and not supplements
    merged.setdefault("deterministic_review", deterministic_review)
    return merged


def _dry_run_result(payload: dict[str, Any], action: str) -> dict[str, Any]:
    matched = _match_groups(payload)
    operations = []
    errors = []
    resolved_targets = set()
    for item in payload.get("items", []):
        item = _dict(item)
        requested_key = _key(item)
        resolution = _resolve_match(matched.get(requested_key.lower(), []))
        if resolution["status"] == "ambiguous" and action != "create_new":
            errors.append(_resolution_error("ambiguous_replace_target" if action == "replace" else "ambiguous_identity_target", requested_key, resolution))
            continue
        existing = _dict(resolution.get("existing_item"))
        target_key = _canonical_key(existing) if existing and action in {"skip", "merge", "replace"} else requested_key
        if target_key.lower() in resolved_targets and action in {"merge", "replace"}:
            errors.append({"type": "duplicate_canonical_target", "message": f"여러 후보가 같은 기존 항목을 대상으로 지정했습니다: {target_key}", "key": requested_key, "target_key": target_key})
            continue
        resolved_targets.add(target_key.lower())
        has_match = bool(existing)
        operation = "skipped" if has_match and action == "skip" else "created_new" if action == "create_new" else "merged" if has_match and action == "merge" else "replaced" if has_match and action == "replace" else "inserted"
        operations.append(_operation_record(requested_key, target_key, operation, existing))
    if errors:
        return {
            "success": False,
            "ready_to_save": False,
            "dry_run": True,
            "saved_count": 0,
            "would_save_count": 0,
            "skipped_count": 0,
            "operation_by_key": operations,
            "message": "교체/병합 대상을 안전하게 확정하지 못해 저장 계획을 중단했습니다.",
            "keys": [item["key"] for item in operations],
            "errors": errors,
        }
    would_save = sum(1 for item in operations if item["operation"] != "skipped")
    return {"success": True, "ready_to_save": True, "dry_run": True, "saved_count": 0, "would_save_count": would_save, "skipped_count": len(operations) - would_save, "operation_by_key": operations, "message": "드라이런입니다. MongoDB에는 저장하지 않았습니다.", "keys": [item["key"] for item in operations], "errors": []}


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
        matched = _match_groups(payload)
        plans = []
        preflight_errors = []
        resolved_targets = set()
        for source_item in payload.get("items", []):
            item = deepcopy(source_item)
            section = str(item.get("section") or "")
            key = str(item.get("key") or "")
            logical_key = f"{section}:{key}"
            resolution = _resolve_match(matched.get(logical_key.lower(), []))
            if resolution["status"] == "ambiguous" and action != "create_new":
                preflight_errors.append(_resolution_error("ambiguous_replace_target" if action == "replace" else "ambiguous_identity_target", logical_key, resolution))
                continue
            matched_existing = _dict(resolution.get("existing_item"))
            exact_existing = _dict(collection.find_one({"_id": f"domain:{section}:{key}"}))
            existing = deepcopy(exact_existing or matched_existing)
            if existing and action in {"skip", "merge", "replace"}:
                target_section, target_key = _canonical_identity(existing, section, key)
                target_id = str(existing.get("_id") or f"domain:{target_section}:{target_key}")
                current_existing = _dict(collection.find_one({"_id": target_id}))
                if current_existing:
                    existing = deepcopy(current_existing)
                elif action == "replace":
                    existing = {}
            else:
                target_section, target_key = section, key
                target_id = f"domain:{target_section}:{target_key}"
            canonical_logical_key = f"{target_section}:{target_key}"
            if canonical_logical_key.lower() in resolved_targets and action in {"merge", "replace"}:
                preflight_errors.append({"type": "duplicate_canonical_target", "message": f"여러 후보가 같은 기존 항목을 대상으로 지정했습니다: {canonical_logical_key}", "key": logical_key, "target_key": canonical_logical_key})
                continue
            resolved_targets.add(canonical_logical_key.lower())
            plans.append({"item": item, "section": section, "key": key, "logical_key": logical_key, "existing": existing, "target_section": target_section, "target_key": target_key, "target_id": target_id, "match_type": resolution.get("match_type", "")})
        if preflight_errors:
            return {"success": False, "ready_to_save": False, "status": "error", "saved_count": 0, "skipped_count": 0, "operation_by_key": [], "database": mongo_database, "collection_name": collection_name, "message": "교체/병합 대상을 안전하게 확정하지 못해 저장하지 않았습니다.", "errors": preflight_errors}

        for plan in plans:
            item = plan["item"]
            section = plan["section"]
            key = plan["key"]
            logical_key = plan["logical_key"]
            existing = plan["existing"]
            target_section = plan["target_section"]
            target_key = plan["target_key"]
            target_id = plan["target_id"]
            if existing and action == "skip":
                operations.append(_operation_record(logical_key, f"{target_section}:{target_key}", "skipped", existing, plan["match_type"]))
                continue
            if action == "create_new" and collection.find_one({"_id": f"domain:{section}:{key}"}):
                key = _next_key(collection, section, key)
                item["key"] = key
                existing = {}
                operation = "created_new"
            elif existing and action == "merge":
                item = _deep_merge(existing, item)
                item["section"] = target_section
                item["key"] = target_key
                operation = "merged"
            elif existing and action == "replace":
                item.pop("_id", None)
                item["section"] = target_section
                item["key"] = target_key
                operation = "replaced"
            elif action == "create_new":
                existing = {}
                operation = "created_new"
            else:
                operation = "inserted"
            doc = deepcopy(item)
            doc["_id"] = f"domain:{doc.get('section')}:{doc.get('key')}"
            if existing.get("created_at") and not doc.get("created_at"):
                doc["created_at"] = existing["created_at"]
            doc["updated_at"] = now
            if raw_text:
                doc["registration_trace"] = {"raw_text": raw_text}
            write_result = collection.replace_one({"_id": doc["_id"]}, doc, upsert=operation != "replaced")
            if operation == "replaced" and getattr(write_result, "matched_count", 1) != 1:
                raise RuntimeError(f"replace target disappeared before write: {doc['_id']}")
            operations.append(_operation_record(logical_key, f"{doc.get('section')}:{doc.get('key')}", operation, existing, plan["match_type"]))
        saved_count = sum(1 for item in operations if item["operation"] != "skipped")
        skipped_count = len(operations) - saved_count
        return {"success": True, "ready_to_save": True, "status": "skipped" if not saved_count and skipped_count else "saved", "saved_count": saved_count, "skipped_count": skipped_count, "operation_by_key": operations, "database": mongo_database, "collection_name": collection_name, "message": "저장 처리를 완료했습니다.", "errors": []}
    except Exception as exc:
        saved_count = sum(1 for item in operations if item.get("operation") != "skipped")
        return {"success": False, "ready_to_save": False, "status": "partial_success" if saved_count else "error", "saved_count": saved_count, "partial_success": bool(saved_count), "operation_by_key": operations, "message": "MongoDB 저장 중 오류가 발생했습니다.", "errors": [{"type": "mongo_write_error", "message": str(exc)}]}
    finally:
        if client is not None:
            client.close()


def _match_groups(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for match in _list(payload.get("existing_matches")):
        match = _dict(match)
        key = str(match.get("new_key") or "").lower()
        if key:
            result.setdefault(key, []).append(deepcopy(match))
    return result


def _resolve_match(matches: list[dict[str, Any]]) -> dict[str, Any]:
    if any(_dict(match).get("identity_resolution") == "ambiguous" or _dict(match).get("match_type") == "ambiguous_identity" for match in matches):
        candidates = []
        for match in matches:
            candidates.extend(_list(_dict(match).get("existing_candidate_keys")))
        return {"status": "ambiguous", "candidate_keys": list(dict.fromkeys(str(key) for key in candidates if str(key).strip()))}
    resolved = [match for match in matches if _dict(match).get("existing_item")]
    if len(resolved) > 1:
        return {"status": "ambiguous", "candidate_keys": [str(_dict(match).get("existing_key") or "") for match in resolved]}
    if not resolved:
        return {"status": "not_found", "candidate_keys": []}
    match = _dict(resolved[0])
    return {"status": "unique", "existing_item": deepcopy(_dict(match.get("existing_item"))), "match_type": str(match.get("match_type") or ""), "existing_key": str(match.get("existing_key") or "")}


def _resolution_error(error_type: str, requested_key: str, resolution: dict[str, Any]) -> dict[str, Any]:
    message = "기존 도메인 항목 후보가 여러 건이라 대상을 하나로 확정할 수 없습니다."
    return {"type": error_type, "message": message, "key": requested_key, "existing_candidate_keys": deepcopy(resolution.get("candidate_keys", []))}


def _canonical_identity(existing: dict[str, Any], fallback_section: str, fallback_key: str) -> tuple[str, str]:
    section = str(existing.get("section") or fallback_section).strip()
    key = str(existing.get("key") or fallback_key).strip()
    return section, key


def _canonical_key(existing: dict[str, Any]) -> str:
    section, key = _canonical_identity(existing, "", "")
    return f"{section}:{key}" if section and key else key


def _operation_record(requested_key: str, target_key: str, operation: str, existing: dict[str, Any] | None = None, match_type: str = "") -> dict[str, Any]:
    existing = _dict(existing)
    target_section, target_item_key = _split_logical_key(target_key)
    existing_target_id = str(existing.get("_id") or "") if _canonical_key(existing) == target_key else ""
    record = {
        "key": target_key,
        "operation": operation,
        "target_key": target_key,
        "target_id": existing_target_id or (f"domain:{target_section}:{target_item_key}" if target_section and target_item_key else ""),
    }
    if requested_key != target_key:
        record["requested_key"] = requested_key
    if match_type:
        record["match_type"] = match_type
    return record


def _split_logical_key(value: str) -> tuple[str, str]:
    section, separator, key = str(value or "").partition(":")
    return (section, key) if separator else ("", section)


def _next_key(collection: Any, section: str, key: str) -> str:
    base = f"{key}_copy"
    candidate = base
    index = 2
    while collection.find_one({"_id": f"domain:{section}:{candidate}"}):
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
    pattern = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")
    return pattern.sub(r"\1\2***", str(value or ""))[:limit]


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


def _key(item: dict[str, Any]) -> str:
    return f"{item.get('section', '')}:{item.get('key', '')}" if item.get("section") else str(item.get("key", ""))


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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class DomainReviewWriter(Component):
    display_name = "07 도메인 검수/저장 처리기"
    description = "스키마·credential·중복 action을 결정론적으로 검증한 뒤 드라이런 또는 MongoDB 저장을 실행합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True), MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True), MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=review_and_write(getattr(self, "payload", None), "", getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", "")))
