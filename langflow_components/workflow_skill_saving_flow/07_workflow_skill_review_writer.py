# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 07 Workflow Skill 검수/저장 처리기
# 역할: 후보 스키마와 중복 정책을 재검증한 뒤 dry-run 계획 또는 MongoDB 저장을 수행합니다.
# 주요 입력: 페이로드, MongoDB URI·데이터베이스·컬렉션
# 주요 출력: 페이로드 출력(payload_out)
# 처리 흐름: 상위 오류 확인 -> 저장 스키마 검증 -> 유사 대상 확정 -> dry-run 또는 단건 MongoDB write
# 유지보수 포인트: replace는 유사 1건이면 canonical 문서를 교체하고, 없으면 신규 저장하며, 복수이면 반드시 차단합니다.
# =============================================================================

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

SECTION = "workflow_skills"
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_workflow_skills"
ALLOWED_ACTIONS = {"skip", "merge", "replace", "create_new"}
ALLOWED_TOOLS = {
    "run_data_analysis",
    "run_metadata_qa",
    "run_visualization",
    "save_domain_metadata",
    "save_table_catalog_metadata",
    "save_main_flow_filter_metadata",
}
MAX_STEP_QUESTION_CHARS = 4000
MAX_WORKFLOW_PAYLOAD_BYTES = 32768
WORKFLOW_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SECRET_PATTERNS = ("password", "passwd", "token", "secret", "api_key", "apikey", "authorization", "credential", "access_key", "private_key", "cookie")


# 주요 함수: 상위 검증과 duplicate 정책을 적용해 dry-run 또는 실제 MongoDB 저장 결과를 만듭니다.
def review_and_write(
    payload_value: Any,
    review_response: Any = "",
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
) -> dict[str, Any]:
    del review_response
    payload = _payload(payload_value)
    action = _duplicate_action(payload)
    dry_run = bool(_dict(payload.get("request")).get("dry_run", True))
    review = _deterministic_review(payload, action)
    payload["review"] = review
    if not review["ready_to_save"]:
        needs_input = bool(review["supplement_requests"])
        payload["write_result"] = {
            "success": False,
            "ready_to_save": False,
            "status": "needs_input" if needs_input else "error",
            "dry_run": dry_run,
            "saved_count": 0,
            "would_save_count": 0,
            "skipped_count": 0,
            "operation_by_key": [],
            "message": "추가 정보가 필요해 저장하지 않았습니다." if needs_input else "Workflow Skill 검증을 통과하지 못해 저장하지 않았습니다.",
            "errors": deepcopy(review["errors"]),
            "supplement_requests": deepcopy(review["supplement_requests"]),
        }
    elif dry_run:
        payload["write_result"] = _dry_run_result(payload, action)
    else:
        payload["write_result"] = _write_to_mongodb(payload, action, mongo_uri, mongo_database, collection_name)
    return payload


# 함수 설명: `_deterministic_review()`는 상위 오류·필수 스키마·비밀 필드·중복 조회 상태를 저장 차단 조건으로 검사합니다.
def _deterministic_review(payload: dict[str, Any], action: str) -> dict[str, Any]:
    refinement = deepcopy(_dict(payload.get("refinement")))
    errors = _unique_errors(_list(payload.get("errors")))
    missing = [str(item).strip() for item in _list(refinement.get("missing_information")) if str(item or "").strip()]
    if bool(refinement.get("needs_more_input")) and not missing:
        missing.append("Workflow 실행 순서를 확정할 수 있도록 원문을 보완해 주세요.")
    items = [_dict(item) for item in _list(payload.get("items")) if isinstance(item, dict)]
    if len(items) != 1:
        errors.append({"type": "workflow_item_count_error", "message": "한 번에 Workflow Skill 한 건만 저장할 수 있습니다."})
    for item in items:
        errors.extend(_validate_item(item))
        for path in _secret_paths(item):
            errors.append({"type": "credential_field_forbidden", "message": f"credential/secret 필드는 저장할 수 없습니다: {path}", "field": path, "key": item.get("key", "")})
    lookup = _dict(_dict(payload.get("trace")).get("duplicate_lookup"))
    if action != "create_new" and lookup.get("status") in {"error", "skipped"} and int(lookup.get("combined_count") or 0) == 0:
        errors.append(
            {
                "type": "identity_lookup_unavailable",
                "message": "기존 Workflow Skill 조회를 완료하지 못해 중복 여부를 확정할 수 없습니다.",
                "lookup_errors": deepcopy(_list(lookup.get("errors"))),
            }
        )
    resolution = _match_resolution(payload)
    if resolution["status"] == "ambiguous" and action != "create_new":
        errors.append(
            {
                "type": "ambiguous_replace_target" if action == "replace" else "ambiguous_identity_target",
                "message": "유사한 기존 Workflow Skill이 여러 건이라 저장 대상을 하나로 확정할 수 없습니다.",
                "existing_candidate_keys": deepcopy(resolution.get("candidate_keys", [])),
            }
        )
    errors = _unique_errors(errors)
    ready = len(items) == 1 and not errors and not missing
    return {
        "ready_to_save": ready,
        "item_reviews": [{"key": str(item.get("key") or ""), "ready_to_save": ready, "warnings": [], "errors": deepcopy(errors)} for item in items],
        "errors": errors,
        "supplement_requests": missing,
        "assumptions": [str(item).strip() for item in _list(refinement.get("assumptions")) if str(item or "").strip()],
        "refinement": refinement,
    }


# 함수 설명: `_validate_item()`은 Writer 경계에서 Workflow Skill 핵심 스키마를 한 번 더 검사합니다.
def _validate_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    errors = []
    key = str(item.get("key") or "")
    payload = _dict(item.get("payload"))
    steps = [_dict(step) for step in _list(payload.get("steps"))]
    if item.get("section") != SECTION:
        errors.append({"type": "invalid_section", "message": f"section은 {SECTION}이어야 합니다.", "key": key})
    if not WORKFLOW_KEY_PATTERN.fullmatch(key):
        errors.append({"type": "invalid_workflow_key", "message": "유효한 workflow key가 없습니다.", "key": key})
    if not str(payload.get("display_name") or "").strip() or not str(payload.get("description") or "").strip():
        errors.append({"type": "missing_workflow_description", "message": "display_name과 description이 모두 필요합니다.", "key": key})
    if not 1 <= len(steps) <= 4:
        errors.append({"type": "invalid_step_count", "message": "실행 단계는 1~4개여야 합니다.", "key": key})
    seen = set()
    by_id = {}
    for index, step in enumerate(steps):
        step_id = str(step.get("step_id") or "")
        tool_name = str(step.get("tool_name") or "")
        dependencies = [str(value) for value in _list(step.get("depends_on"))]
        if not STEP_ID_PATTERN.fullmatch(step_id) or step_id in seen:
            errors.append({"type": "invalid_or_duplicate_step_id", "message": "step_id 형식이 잘못되었거나 중복되었습니다.", "key": key, "step_id": step_id})
        if tool_name not in ALLOWED_TOOLS:
            errors.append({"type": "unsupported_tool", "message": f"지원하지 않는 Tool입니다: {tool_name}", "key": key, "step_id": step_id})
        question = str(step.get("question") or "").strip()
        if not question:
            errors.append({"type": "missing_step_question", "message": "단계 question이 비어 있습니다.", "key": key, "step_id": step_id})
        elif len(question) > MAX_STEP_QUESTION_CHARS:
            errors.append({"type": "step_question_too_long", "message": f"단계 question은 {MAX_STEP_QUESTION_CHARS}자를 초과할 수 없습니다.", "key": key, "step_id": step_id})
        if any(dependency not in seen for dependency in dependencies):
            errors.append({"type": "dependency_not_prior", "message": "depends_on은 앞에서 정의된 step_id만 사용할 수 있습니다.", "key": key, "step_id": step_id})
        handoff = str(step.get("handoff") or "")
        if handoff not in {"none", "result_ref"}:
            errors.append({"type": "invalid_handoff", "message": "handoff는 none 또는 result_ref여야 합니다.", "key": key, "step_id": step_id})
        if handoff == "result_ref":
            source_tool = str(_dict(by_id.get(dependencies[0] if len(dependencies) == 1 else "")).get("tool_name") or "")
            if (
                len(dependencies) != 1
                or source_tool != "run_data_analysis"
                or tool_name not in {"run_data_analysis", "run_visualization"}
            ):
                errors.append({"type": "invalid_result_ref_contract", "message": "result_ref는 단일 run_data_analysis 결과를 다음 run_data_analysis 또는 run_visualization에 전달할 때만 사용할 수 있습니다.", "key": key, "step_id": step_id})
        if str(step.get("on_error") or "") not in {"stop", "continue"}:
            errors.append({"type": "invalid_on_error", "message": "on_error는 stop 또는 continue여야 합니다.", "key": key, "step_id": step_id})
        if index == 0 and (dependencies or handoff != "none"):
            errors.append({"type": "invalid_first_step", "message": "첫 단계에는 dependency나 result_ref handoff를 지정할 수 없습니다.", "key": key, "step_id": step_id})
        seen.add(step_id)
        by_id[step_id] = step
    payload_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if payload_bytes > MAX_WORKFLOW_PAYLOAD_BYTES:
        errors.append({"type": "workflow_payload_too_large", "message": f"Workflow Skill payload는 UTF-8 {MAX_WORKFLOW_PAYLOAD_BYTES}바이트를 초과할 수 없습니다.", "key": key, "payload_bytes": payload_bytes})
    return errors


# 함수 설명: `_dry_run_result()`는 중복 정책에 따라 실제 저장 시 수행될 한 건의 operation을 계산합니다.
def _dry_run_result(payload: dict[str, Any], action: str) -> dict[str, Any]:
    item = _dict(_list(payload.get("items"))[0])
    requested_key = str(item.get("key") or "")
    resolution = _match_resolution(payload)
    existing = _dict(resolution.get("existing_item"))
    target_key = str(existing.get("key") or requested_key) if existing and action != "create_new" else requested_key
    operation = _operation(action, bool(existing))
    record = _operation_record(requested_key, target_key, operation, existing, str(resolution.get("match_type") or ""))
    would_save = 0 if operation == "skipped" else 1
    return {
        "success": True,
        "ready_to_save": True,
        "status": "dry_run",
        "dry_run": True,
        "saved_count": 0,
        "would_save_count": would_save,
        "skipped_count": 1 - would_save,
        "operation_by_key": [record],
        "message": "드라이런입니다. MongoDB에는 저장하지 않았습니다.",
        "keys": [target_key],
        "errors": [],
    }


# 함수 설명: `_write_to_mongodb()`는 preflight로 canonical 대상을 재확인한 뒤 단건 Workflow 문서를 저장합니다.
def _write_to_mongodb(payload: dict[str, Any], action: str, mongo_uri: str, mongo_database: str, collection_name: str) -> dict[str, Any]:
    uri = str(mongo_uri or "").strip()
    database = str(mongo_database or DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    collection_name = str(collection_name or DEFAULT_COLLECTION).strip() or DEFAULT_COLLECTION
    if not uri:
        return {"success": False, "ready_to_save": False, "status": "error", "saved_count": 0, "message": "MongoDB 연결 URI가 없어 저장하지 않았습니다.", "errors": [{"type": "missing_mongo_config", "message": "mongo_uri가 필요합니다."}]}
    client = None
    try:
        client = getattr(import_module("pymongo"), "MongoClient")(uri, serverSelectionTimeoutMS=5000)
        collection = client[database][collection_name]
        item = deepcopy(_dict(_list(payload.get("items"))[0]))
        requested_key = str(item.get("key") or "")
        resolution = _match_resolution(payload)
        if resolution["status"] == "ambiguous" and action != "create_new":
            return _write_error("ambiguous_replace_target", "유사 대상이 여러 건이라 저장하지 않았습니다.", database, collection_name, resolution.get("candidate_keys", []))
        existing = _dict(resolution.get("existing_item"))
        if existing and action != "create_new":
            target_key = str(existing.get("key") or requested_key)
            target_id = str(existing.get("_id") or f"workflow:{target_key}")
            current = _dict(collection.find_one({"_id": target_id}))
            if not current:
                return _write_error("target_disappeared", "유사 대상으로 확정한 기존 Workflow Skill이 저장 직전에 사라졌습니다.", database, collection_name, [target_key])
            existing = current
        else:
            exact = _dict(collection.find_one({"_id": f"workflow:{requested_key}"}))
            existing = exact if exact and action != "create_new" else {}
            target_key = str(existing.get("key") or requested_key)
            target_id = str(existing.get("_id") or f"workflow:{target_key}")
        if action == "create_new":
            target_key = _next_key(collection, requested_key)
            target_id = f"workflow:{target_key}"
            existing = {}
        operation = _operation(action, bool(existing))
        if operation == "skipped":
            record = _operation_record(requested_key, target_key, operation, existing, str(resolution.get("match_type") or ""))
            return {"success": True, "ready_to_save": True, "status": "skipped", "saved_count": 0, "skipped_count": 1, "operation_by_key": [record], "database": database, "collection_name": collection_name, "message": "유사한 기존 Workflow Skill을 유지했습니다.", "errors": []}
        if operation == "merged":
            document = _deep_merge(existing, item)
        else:
            document = deepcopy(item)
        document = {
            "_id": target_id,
            "section": SECTION,
            "key": target_key,
            "status": str(document.get("status") or "active"),
            "payload": deepcopy(_dict(document.get("payload"))),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "registration_trace": {"raw_text": _redact_raw_text(str(_dict(payload.get("request")).get("raw_text") or ""))},
        }
        write_result = collection.replace_one({"_id": target_id}, document, upsert=not bool(existing))
        if existing and getattr(write_result, "matched_count", 1) != 1:
            raise RuntimeError(f"target document was not matched: {target_id}")
        record = _operation_record(requested_key, target_key, operation, existing, str(resolution.get("match_type") or ""))
        return {"success": True, "ready_to_save": True, "status": "saved", "saved_count": 1, "skipped_count": 0, "operation_by_key": [record], "database": database, "collection_name": collection_name, "message": "Workflow Skill 저장을 완료했습니다.", "errors": []}
    except Exception as exc:
        return {"success": False, "ready_to_save": False, "status": "error", "saved_count": 0, "database": database, "collection_name": collection_name, "message": "MongoDB 저장 중 오류가 발생했습니다.", "errors": [{"type": "mongo_write_error", "message": str(exc)}]}
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_match_resolution()`은 similarity 결과를 저장 가능한 유일 대상·미일치·모호함 중 하나로 확정합니다.
def _match_resolution(payload: dict[str, Any]) -> dict[str, Any]:
    matches = [_dict(match) for match in _list(payload.get("existing_matches"))]
    if any(match.get("identity_resolution") == "ambiguous" for match in matches):
        candidates = []
        for match in matches:
            candidates.extend(_list(match.get("existing_candidate_keys")))
        return {"status": "ambiguous", "candidate_keys": list(dict.fromkeys(str(value) for value in candidates if str(value).strip()))}
    resolved = [match for match in matches if isinstance(match.get("existing_item"), dict)]
    if len(resolved) > 1:
        return {"status": "ambiguous", "candidate_keys": [str(match.get("existing_key") or "") for match in resolved]}
    if not resolved:
        return {"status": "not_found", "candidate_keys": []}
    match = resolved[0]
    return {"status": "unique", "existing_item": deepcopy(_dict(match.get("existing_item"))), "match_type": str(match.get("match_type") or "")}


# 함수 설명: `_operation()`은 중복 action과 기존 문서 존재 여부를 실제 저장 operation 이름으로 변환합니다.
def _operation(action: str, has_existing: bool) -> str:
    if action == "create_new":
        return "created_new"
    if has_existing and action == "skip":
        return "skipped"
    if has_existing and action == "merge":
        return "merged"
    if has_existing and action == "replace":
        return "replaced"
    return "inserted"


# 함수 설명: `_operation_record()`는 요청 key와 실제 canonical target을 저장 결과에 함께 기록합니다.
def _operation_record(requested_key: str, target_key: str, operation: str, existing: dict[str, Any], match_type: str) -> dict[str, Any]:
    record = {"key": target_key, "operation": operation, "target_key": target_key, "target_id": str(existing.get("_id") or f"workflow:{target_key}")}
    if requested_key != target_key:
        record["requested_key"] = requested_key
    if match_type:
        record["match_type"] = match_type
    return record


# 함수 설명: `_next_key()`는 create_new 정책에서 충돌하지 않는 `_copy` suffix key를 계산합니다.
def _next_key(collection: Any, key: str) -> str:
    if not collection.find_one({"_id": f"workflow:{key}"}):
        return key
    base = f"{key}_copy"
    candidate = base
    index = 2
    while collection.find_one({"_id": f"workflow:{candidate}"}):
        candidate = f"{base}_{index}"
        index += 1
    return candidate


# 함수 설명: `_deep_merge()`는 새 Workflow 필드만 기존 문서에 재귀 병합하고 내부 ID는 유지합니다.
def _deep_merge(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    for key, value in incoming.items():
        if key in {"_id", "updated_at", "registration_trace"}:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            combined = deepcopy(merged[key])
            for item in value:
                if item not in combined:
                    combined.append(deepcopy(item))
            merged[key] = combined
        else:
            merged[key] = deepcopy(value)
    return merged


# 함수 설명: `_write_error()`는 preflight 저장 차단 오류를 공통 write_result 계약으로 만듭니다.
def _write_error(error_type: str, message: str, database: str, collection: str, candidate_keys: Any) -> dict[str, Any]:
    return {"success": False, "ready_to_save": False, "status": "error", "saved_count": 0, "database": database, "collection_name": collection, "message": message, "errors": [{"type": error_type, "message": message, "existing_candidate_keys": deepcopy(_list(candidate_keys))}]}


# 함수 설명: `_secret_paths()`는 Workflow 후보 전체에서 credential로 의심되는 field 경로를 재귀 탐색합니다.
def _secret_paths(value: Any, prefix: str = "") -> list[str]:
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower())
            if any(pattern in normalized for pattern in SECRET_PATTERNS):
                paths.append(path)
            else:
                paths.extend(_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_secret_paths(item, f"{prefix}[{index}]"))
    return paths


# 함수 설명: `_redact_raw_text()`는 등록 원문에 포함될 수 있는 credential 값을 trace에 남기기 전에 마스킹합니다.
def _redact_raw_text(value: str, limit: int = 2000) -> str:
    pattern = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")
    return pattern.sub(r"\1\2***", str(value or ""))[:limit]


# 함수 설명: `_duplicate_action()`은 요청에 지정된 중복 정책을 허용 목록으로 제한합니다.
def _duplicate_action(payload: dict[str, Any]) -> str:
    action = str(_dict(payload.get("request")).get("duplicate_action") or "skip").strip().lower()
    return action if action in ALLOWED_ACTIONS else "skip"


# 함수 설명: `_unique_errors()`는 동일한 오류를 발생 순서대로 한 건씩만 유지합니다.
def _unique_errors(values: list[Any]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for value in values:
        item = value if isinstance(value, dict) else {"type": "review_error", "message": str(value)}
        marker = (item.get("type"), item.get("key"), item.get("step_id"), item.get("field"), item.get("message"))
        if marker not in seen:
            seen.add(marker)
            result.append(deepcopy(item))
    return result


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 변경에 안전한 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 값이 dict일 때만 반환하고 아니면 빈 dict를 사용합니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 값이 list일 때만 반환하고 아니면 빈 목록을 사용합니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# Langflow 컴포넌트 클래스: 결정론적 검수와 standalone MongoDB 저장을 하나의 Writer 포트로 제공합니다.
class WorkflowSkillReviewWriter(Component):
    display_name = "07 Workflow Skill 검수/저장 처리기"
    description = "Workflow 계약과 유사 대상을 재검증한 뒤 dry-run 또는 MongoDB 저장을 수행합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, value=""),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE),
        MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 현재 검수 결과와 저장 결과가 포함된 페이로드를 반환합니다.
    def build_payload(self) -> Data:
        return Data(
            data=review_and_write(
                getattr(self, "payload", None),
                "",
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", DEFAULT_DATABASE),
                getattr(self, "collection_name", DEFAULT_COLLECTION),
            )
        )
