# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 07 테이블 카탈로그 검수/저장 처리기
# 역할: 스키마·credential·중복 action을 결정론적으로 검증한 뒤 드라이런 또는 MongoDB 저장을 실행합니다.
# 주요 입력: 페이로드 (payload) · 필수, MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 컬렉션 이름 (collection_name)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 테이블 카탈로그 필수 필드·비밀값·중복 정책을 결정론적으로 검증하고 dry-run 계획 또는 MongoDB 저장을 수행합니다.
# 유지보수 포인트: 연결 설정은 노드 입력→환경변수→기본값 순으로 해석하며, 오류는 숨기지 않고 trace/status에 남기고 연결은 반드시 닫습니다.
# =============================================================================

from __future__ import annotations

import builtins
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
QA_SNAPSHOT_CACHE_REGISTRY = "_metadata_driven_v5_qa_snapshot_cache_v1"


# 주요 함수: 결정론적 검증과 duplicate 정책을 적용하고 dry-run 계획 또는 실제 저장을 수행합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def review_and_write(payload_value: Any, review_response: Any = "", mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    dry_run = bool(_dict(payload.get("request")).get("dry_run", True))
    action = _duplicate_action(payload)
    deterministic_errors = _deterministic_errors(payload)
    llm_review = _json(review_response)
    review = _merge_review(llm_review, payload, deterministic_errors)
    ready = bool(review.get("ready_to_save"))
    next_payload = payload
    next_payload["review"] = review
    if not ready:
        needs_input = bool(_list(review.get("supplement_requests")))
        next_payload["write_result"] = {
            "success": False,
            "ready_to_save": False,
            "status": "needs_input" if needs_input else "error",
            "saved_count": 0,
            "message": "추가 정보가 필요해 저장하지 않았습니다." if needs_input else "필수 검증을 통과하지 못해 저장하지 않았습니다.",
            "errors": review.get("errors", []),
            "supplement_requests": deepcopy(_list(review.get("supplement_requests"))),
        }
    elif dry_run:
        next_payload["write_result"] = _dry_run_result(payload, action)
    else:
        next_payload["write_result"] = _write_to_mongodb(payload, action, mongo_uri, mongo_database, collection_name)
        if next_payload["write_result"].get("success") and int(next_payload["write_result"].get("saved_count") or 0) > 0:
            next_payload["write_result"]["metadata_qa_snapshot_invalidated"] = _invalidate_metadata_qa_snapshot_cache()
    return next_payload


# 함수 설명: `_invalidate_metadata_qa_snapshot_cache()`는 실제 저장 성공 후 같은 worker의 QA snapshot generation을 증가시킵니다.
def _invalidate_metadata_qa_snapshot_cache() -> bool:
    registry = getattr(builtins, QA_SNAPSHOT_CACHE_REGISTRY, None)
    if not isinstance(registry, dict):
        registry = {"generation": 0, "entries": {}}
        setattr(builtins, QA_SNAPSHOT_CACHE_REGISTRY, registry)
    try:
        registry["generation"] = max(0, int(registry.get("generation", 0))) + 1
    except Exception:
        registry["generation"] = 1
    registry["entries"] = {}
    return True


# 함수 설명: `_deterministic_errors()`는 저장 후보의 필수 필드·허용값·비밀값 위반을 중복 없는 오류 목록으로 만듭니다.
def _deterministic_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = _unique_errors(deepcopy(_list(payload.get("errors"))))
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


# 함수 설명: `_merge_review()`는 결정론적 검증 결과와 선택적 추가 검수 결과를 하나의 저장 판단으로 합칩니다.
def _merge_review(llm_review: dict[str, Any], payload: dict[str, Any], deterministic_errors: list[dict[str, Any]]) -> dict[str, Any]:
    refinement = deepcopy(_dict(payload.get("refinement")))
    missing = [str(item).strip() for item in _list(refinement.get("missing_information")) if str(item or "").strip()]
    upstream_supplements = list(missing)
    if bool(refinement.get("needs_more_input")) and not upstream_supplements:
        upstream_supplements.append("저장에 필요한 정보를 원문에 보완해 주세요.")
    assumptions = [str(item).strip() for item in _list(refinement.get("assumptions")) if str(item or "").strip()]
    if not llm_review:
        errors = list(deterministic_errors)
        return {"ready_to_save": bool(payload.get("items")) and not errors and not upstream_supplements, "errors": errors, "supplement_requests": upstream_supplements, "assumptions": assumptions, "refinement": refinement}
    merged = deepcopy(llm_review)
    merged_errors = _list(merged.get("errors")) + deterministic_errors
    supplements = _unique_text_items(_list(merged.get("supplement_requests")) + upstream_supplements)
    merged["errors"] = _unique_errors(merged_errors)
    merged["supplement_requests"] = supplements
    merged["ready_to_save"] = bool(merged.get("ready_to_save")) and not merged["errors"] and not supplements and bool(payload.get("items"))
    merged["assumptions"] = _unique_text_items(_list(merged.get("assumptions")) + assumptions)
    merged["refinement"] = refinement
    return merged


# 함수 설명: `_unique_text_items()`는 문자열 또는 질문 dict 형태의 보완 요청을 순서를 유지하며 중복 제거합니다.
def _unique_text_items(values: list[Any]) -> list[Any]:
    result = []
    seen = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) if isinstance(value, dict) else str(value).strip()
        if marker and marker not in seen:
            seen.add(marker)
            result.append(deepcopy(value))
    return result


# 함수 설명: `_dry_run_result()`는 실제 DB를 변경하지 않고 실행 예정 작업만 보여 주는 dry-run 결과를 만듭니다.
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


# 함수 설명: `_write_to_mongodb()`는 검증을 통과한 작업만 duplicate action에 맞춰 MongoDB에 저장하고 결과를 기록합니다.
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


# 함수 설명: `_match_map()`는 입력 조건과 일치하는 MAP을 찾아 비교·필터 결과로 반환합니다.
def _match_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for match in _list(payload.get("existing_matches")):
        match = _dict(match)
        key = str(match.get("existing_key") or match.get("new_key") or "").lower()
        existing = _dict(match.get("existing_item"))
        if key and existing:
            result[key] = existing
    return result


# 함수 설명: `_next_key()`는 create_new 정책에서 기존 key와 충돌하지 않는 다음 저장 key를 계산합니다.
def _next_key(collection: Any, key: str) -> str:
    base = f"{key}_copy"
    candidate = base
    index = 2
    while collection.find_one({"_id": f"table_catalog:{candidate}"}):
        candidate = f"{base}_{index}"
        index += 1
    return candidate


# 함수 설명: `_deep_merge()`는 중첩 dict를 재귀 병합하되 새 값이 지정된 필드만 기존 문서에 반영합니다.
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


# 함수 설명: `_secret_paths()`는 저장 후보 내부에서 password·token 등 비밀값으로 의심되는 필드 경로를 재귀 탐색합니다.
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


# 함수 설명: `_is_secret_key()`는 필드 이름이 credential·token·password 등 저장 금지 비밀 key인지 판정합니다.
def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key or "").lower())
    return any(pattern in normalized for pattern in SECRET_PATTERNS)


# 함수 설명: `_redact_raw_text()`는 등록 원문에 포함될 수 있는 credential 값을 응답·trace에서 마스킹합니다.
def _redact_raw_text(value: str, limit: int = 2000) -> str:
    text = str(value or "")
    pattern = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")
    return pattern.sub(r"\1\2***", text)[:limit]


# 함수 설명: `_duplicate_action()`는 요청에 지정된 skip/merge/replace/create_new 중복 처리 정책을 안전한 기본값과 함께 해석합니다.
def _duplicate_action(payload: dict[str, Any]) -> str:
    request = _dict(payload.get("request"))
    decision = _dict(payload.get("duplicate_decision"))
    action = str(request.get("duplicate_action") or decision.get("action") or "skip")
    return action if action in {"merge", "replace", "skip", "create_new"} else "skip"


# 함수 설명: `_resolve_mongo_config()`는 컴포넌트 입력→환경변수→기본값 순서로 MongoDB database와 collection 설정을 확정합니다.
def _resolve_mongo_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (mongo_uri or os.getenv("MONGODB_URI", ""), mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE), collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION))


# 함수 설명: `_unique_errors()`는 중복 오류 메시지를 최초 발생 순서대로 하나씩만 남깁니다.
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


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_json()`는 Message·dict·JSON 문자열에서 Markdown fence를 제거하고 JSON object를 안전하게 추출합니다.
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


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TableCatalogReviewWriter(Component):
    display_name = "07 테이블 카탈로그 검수/저장 처리기"
    description = "스키마·credential·중복 action을 결정론적으로 검증한 뒤 드라이런 또는 MongoDB 저장을 실행합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True), MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True), MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True), MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=review_and_write(getattr(self, "payload", None), "", getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", "")))
