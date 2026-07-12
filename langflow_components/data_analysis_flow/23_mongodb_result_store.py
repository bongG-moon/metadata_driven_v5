# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 23 MongoDB 결과 저장소
# 역할: pandas 분석 결과와 런타임 조회 결과를 MongoDB result store에 저장하고 data_ref를 페이로드에 남깁니다.
# 주요 입력: 페이로드 (payload) · 필수, MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 결과 컬렉션 (collection_name),
#        데이터 보관 시간(시간) (ttl_hours), 저장 결과/소스 행 상한, 결과 문서 바이트 상한
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 후속 질문에 필요한 분석 결과를 상한 안에서 저장하고, 불완전 저장이 필요하면 정상 data_ref를 만들지 않는 fail-closed 정책을 적용합니다.
# 유지보수 포인트: standalone Flow의 노드 입력으로 연결 설정을 받고, 오류는 숨기지 않고 trace/status에 남기며 연결은 반드시 닫습니다.
# =============================================================================

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_result_store"
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 24 * 7
TTL_INDEX_NAME = "agent_v4_result_store_expires_at_ttl"
DEFAULT_MAX_RESULT_ROWS = 20000
DEFAULT_MAX_SOURCE_ROWS_PER_ALIAS = 10000
DEFAULT_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 14 * 1024 * 1024


# 주요 함수: 후속 질문 재사용에 필요한 결과를 MongoDB에 저장하고 data_ref를 발급합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def store_result(
    payload_value: Any,
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
    ttl_hours: Any = "",
    max_result_rows: Any = "",
    max_source_rows_per_alias: Any = "",
    max_document_bytes: Any = "",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    mongo_uri, mongo_database, collection_name = _resolve_config(mongo_uri, mongo_database, collection_name)
    next_payload = payload
    if _execution_blocked(next_payload):
        return _mark_execution_blocked(next_payload, mongo_database, collection_name)
    if not mongo_uri:
        return _mark_skipped(next_payload, mongo_database, collection_name, "MongoDB 연결 URI 노드 입력이 비어 있어 분석 결과를 result store에 저장하지 않았습니다.")

    client = None
    data_ref = _build_data_ref(next_payload)
    ttl_hours_value = _effective_ttl_hours(ttl_hours)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours_value)
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(mongo_uri, serverSelectionTimeoutMS=5000)
        collection = client[mongo_database][collection_name]
        ttl_index_error = _ensure_ttl_index(collection)
        max_result_rows_value = _bounded_positive_int(
            max_result_rows,
            DEFAULT_MAX_RESULT_ROWS,
            upper=1_000_000,
        )
        max_source_rows_value = _bounded_positive_int(
            max_source_rows_per_alias,
            DEFAULT_MAX_SOURCE_ROWS_PER_ALIAS,
            upper=1_000_000,
        )
        max_document_bytes_value = _bounded_positive_int(
            max_document_bytes,
            DEFAULT_MAX_DOCUMENT_BYTES,
            lower=1024,
            upper=MAX_DOCUMENT_BYTES,
        )
        stored_payload, storage_manifest = _compact_store_payload(
            next_payload,
            max_result_rows=max_result_rows_value,
            max_source_rows_per_alias=max_source_rows_value,
            max_document_bytes=max_document_bytes_value,
        )
        doc = {
            "_id": data_ref,
            "data_ref": data_ref,
            "session_id": str(next_payload.get("request", {}).get("session_id") or ""),
            "question": str(next_payload.get("request", {}).get("question") or ""),
            "created_at": _to_iso(now),
            "expires_at": expires_at,
            "expires_at_iso": _to_iso(expires_at),
            "ttl_hours": ttl_hours_value,
            "payload": stored_payload,
        }
        storage_manifest["estimated_document_bytes"] = _json_size(doc)
        stored_payload["storage_manifest"] = storage_manifest
        if _json_size(doc) > max_document_bytes_value:
            return _mark_error(
                next_payload,
                mongo_database,
                collection_name,
                data_ref,
                [{"type": "result_store_document_too_large", "message": "안전 압축 후에도 결과 문서가 설정된 바이트 상한을 초과했습니다."}],
            )
        if storage_manifest.get("compacted"):
            return _mark_followup_unavailable(
                next_payload,
                mongo_database,
                collection_name,
                data_ref,
                storage_manifest,
            )
        collection.replace_one({"_id": data_ref}, doc, upsert=True)
        data_refs = _build_data_refs(next_payload, data_ref, mongo_database, collection_name, storage_manifest)
        result_ref = data_refs[0] if data_refs else _data_ref_object(data_ref, mongo_database, collection_name, "payload.result_rows", "analysis_result", "분석 결과")
        next_payload.setdefault("data", {})["data_ref"] = result_ref
        next_payload["data_refs"] = data_refs
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
            "stage": "23_mongodb_result_store",
            "status": "ok",
            "database": mongo_database,
            "collection_name": collection_name,
            "data_ref": data_ref,
            "data_refs": data_refs,
            "ttl_hours": ttl_hours_value,
            "expires_at": _to_iso(expires_at),
            "storage_manifest": deepcopy(storage_manifest),
            "errors": [],
        }
        if ttl_index_error:
            next_payload["trace"]["inspection"]["result_store"]["ttl_index_warning"] = ttl_index_error
        return next_payload
    except Exception as exc:
        return _mark_error(next_payload, mongo_database, collection_name, data_ref, [{"type": "mongo_write_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_resolve_config()`는 standalone 노드 입력과 코드 기본값만으로 실제 실행 설정을 확정합니다.
def _resolve_config(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> tuple[str, str, str]:
    return (
        str(mongo_uri or "").strip(),
        str(mongo_database or DEFAULT_DATABASE).strip(),
        str(collection_name or DEFAULT_COLLECTION).strip(),
    )


# 함수 설명: `_effective_ttl_hours()`는 요청값과 기본값을 해석해 결과 문서에 적용할 유효 TTL 시간을 결정합니다.
def _effective_ttl_hours(value: Any) -> int:
    text = str(value or "").strip()
    try:
        parsed = int(float(text)) if text else DEFAULT_TTL_HOURS
    except Exception:
        parsed = DEFAULT_TTL_HOURS
    return max(1, min(parsed, MAX_TTL_HOURS))


# 함수 설명: `_ensure_ttl_index()`는 TTL·index이 실행·저장 계약을 만족하는지 검사하고 위반 내용을 명시적으로 반환합니다.
def _ensure_ttl_index(collection: Any) -> str:
    create_index = getattr(collection, "create_index", None)
    if not callable(create_index):
        return ""
    try:
        create_index([("expires_at", 1)], expireAfterSeconds=0, name=TTL_INDEX_NAME)
    except Exception as exc:
        return str(exc)
    return ""


# 함수 설명: `_to_iso()`는 datetime 또는 문자열 시간을 UTC ISO 형식으로 변환합니다.
def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# 함수 설명: `_json_ready()`는 datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.
def _json_ready(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return None if value != value or value in (float("inf"), -float("inf")) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_ready(item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_ready(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item_value) for item_value in value]
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


# 함수 설명: `_bounded_positive_int()`는 standalone 노드 입력값을 안전한 양의 정수 범위로 제한합니다.
def _bounded_positive_int(value: Any, default: int, lower: int = 1, upper: int = 1_000_000) -> int:
    try:
        parsed = int(float(str(value).strip())) if str(value or "").strip() else default
    except Exception:
        parsed = default
    return max(lower, min(parsed, upper))


# 주요 함수: 저장 행 수와 추정 JSON 바이트를 함께 제한하고, 잘린 여부를 명시하는 호환 payload를 만듭니다.
def _compact_store_payload(
    payload: dict[str, Any],
    max_result_rows: int,
    max_source_rows_per_alias: int,
    max_document_bytes: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    original_result_rows = _result_rows_for_store(payload)
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    stored_result_rows = _json_ready(original_result_rows[:max_result_rows])
    stored_runtime_sources = {
        str(alias): _json_ready(rows[:max_source_rows_per_alias])
        for alias, rows in runtime_sources.items()
        if isinstance(rows, list)
    }
    original_source_counts = {
        str(alias): len(rows)
        for alias, rows in runtime_sources.items()
        if isinstance(rows, list)
    }
    stored_payload = {
        "request": _json_ready(payload.get("request", {})),
        "metadata_refs": _json_ready(payload.get("metadata_refs", [])),
        "intent_plan": _json_ready(payload.get("intent_plan", {})),
        "source_results": _json_ready(payload.get("source_results", [])),
        "runtime_sources": stored_runtime_sources,
        "result_rows": stored_result_rows,
        "analysis": _json_ready(_compact_analysis_for_store(payload.get("analysis", {}))),
        "data": _json_ready(_compact_data_for_store(payload.get("data", {}))),
    }
    manifest = {
        "version": 1,
        "policy": "bounded_compact",
        "max_document_bytes": max_document_bytes,
        "max_result_rows": max_result_rows,
        "max_source_rows_per_alias": max_source_rows_per_alias,
        "result_rows": {},
        "runtime_sources": {},
        "compacted": False,
    }
    stored_payload["storage_manifest"] = manifest
    _refresh_storage_manifest(manifest, original_result_rows, original_source_counts, stored_payload)

    # MongoDB envelope와 BSON 오버헤드를 위해 설정값의 약 10%를 여유로 둡니다.
    target_payload_bytes = max(512, max_document_bytes - min(64 * 1024, max_document_bytes // 10))
    while _json_size(stored_payload) > target_payload_bytes:
        candidate = _largest_stored_row_group(stored_payload)
        if candidate is None:
            break
        kind, alias = candidate
        rows = stored_payload["result_rows"] if kind == "result_rows" else stored_payload["runtime_sources"].get(alias, [])
        next_size = len(rows) // 2 if len(rows) > 1 else 0
        if kind == "result_rows":
            stored_payload["result_rows"] = rows[:next_size]
        else:
            stored_payload["runtime_sources"][alias] = rows[:next_size]
        _refresh_storage_manifest(manifest, original_result_rows, original_source_counts, stored_payload)

    manifest["estimated_payload_bytes"] = _json_size(stored_payload)
    return stored_payload, manifest


# 함수 설명: `_largest_stored_row_group()`는 바이트 상한을 넘길 때 가장 큰 행 묶음부터 줄이도록 대상을 고릅니다.
def _largest_stored_row_group(stored_payload: dict[str, Any]) -> tuple[str, str] | None:
    candidates: list[tuple[int, str, str]] = []
    result_rows = stored_payload.get("result_rows") if isinstance(stored_payload.get("result_rows"), list) else []
    if result_rows:
        candidates.append((_json_size(result_rows), "result_rows", ""))
    runtime_sources = stored_payload.get("runtime_sources") if isinstance(stored_payload.get("runtime_sources"), dict) else {}
    for alias, rows in runtime_sources.items():
        if isinstance(rows, list) and rows:
            candidates.append((_json_size(rows), "runtime_sources", str(alias)))
    if not candidates:
        return None
    _, kind, alias = max(candidates, key=lambda item: item[0])
    return kind, alias


# 함수 설명: `_refresh_storage_manifest()`는 원본·저장 행 수와 완전성 표시를 현재 압축 결과에 맞게 갱신합니다.
def _refresh_storage_manifest(
    manifest: dict[str, Any],
    original_result_rows: list[Any],
    original_source_counts: dict[str, int],
    stored_payload: dict[str, Any],
) -> None:
    stored_result_rows = stored_payload.get("result_rows") if isinstance(stored_payload.get("result_rows"), list) else []
    result_complete = len(stored_result_rows) == len(original_result_rows)
    manifest["result_rows"] = {
        "original_count": len(original_result_rows),
        "stored_count": len(stored_result_rows),
        "complete": result_complete,
    }
    runtime_sources = stored_payload.get("runtime_sources") if isinstance(stored_payload.get("runtime_sources"), dict) else {}
    source_manifest = {}
    for alias, original_count in original_source_counts.items():
        stored_rows = runtime_sources.get(alias) if isinstance(runtime_sources.get(alias), list) else []
        source_manifest[alias] = {
            "original_count": original_count,
            "stored_count": len(stored_rows),
            "complete": len(stored_rows) == original_count,
        }
    manifest["runtime_sources"] = source_manifest
    manifest["compacted"] = not result_complete or any(not item["complete"] for item in source_manifest.values())


# 함수 설명: `_json_size()`는 BSON 상한보다 보수적인 UTF-8 JSON 직렬화 크기를 계산합니다.
def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


# 함수 설명: `_result_rows_for_store()`는 행 목록·대상·store에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _result_rows_for_store(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("_full_result_rows")
    if rows is None:
        rows = payload.get("_runtime_result_rows")
    if rows is None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("rows", [])
    return rows if isinstance(rows, list) else []


# 함수 설명: `_compact_analysis_for_store()`는 분석·대상·store에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_analysis_for_store(value: Any) -> dict[str, Any]:
    analysis = value if isinstance(value, dict) else {}
    keep_keys = (
        "status",
        "row_count",
        "columns",
        "analysis_code",
        "llm_generated_code",
        "pandas_filter_preamble",
        "used_helpers",
        "step_outputs",
        "function_case_results",
        "error",
    )
    return {key: deepcopy(analysis[key]) for key in keep_keys if key in analysis and key != "rows"}


# 함수 설명: `_compact_data_for_store()`는 데이터·대상·store에서 후속 단계에 필요한 정보만 남겨 payload와 token 크기를 줄입니다.
def _compact_data_for_store(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    keep_keys = ("columns", "row_count", "data_ref")
    return {key: deepcopy(data[key]) for key in keep_keys if key in data and key != "rows"}


# 함수 설명: `_build_data_ref()`는 데이터·참조 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def _build_data_ref(payload: dict[str, Any]) -> str:
    existing = payload.get("data", {}).get("data_ref") if isinstance(payload.get("data"), dict) else ""
    if isinstance(existing, dict):
        existing = existing.get("ref_id") or existing.get("data_ref") or existing.get("_id") or ""
    if existing:
        return str(existing)
    session_id = str(payload.get("request", {}).get("session_id") or "session")
    return f"result:{session_id}:{uuid.uuid4().hex}"


# 함수 설명: `_build_data_refs()`는 데이터·참조 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def _build_data_refs(
    payload: dict[str, Any],
    ref_id: str,
    database: str,
    collection_name: str,
    storage_manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    result_ref = _data_ref_object(
        ref_id,
        database,
        collection_name,
        "payload.result_rows",
        "analysis_result",
        "분석 결과 데이터",
        row_count=data.get("row_count"),
        columns=data.get("columns"),
    )
    refs = [result_ref]

    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    source_result_by_alias = _source_result_by_alias(payload.get("source_results"))
    for alias, rows in runtime_sources.items():
        if not isinstance(rows, list):
            continue
        source_result = source_result_by_alias.get(str(alias), {})
        refs.append(
            _data_ref_object(
                ref_id,
                database,
                collection_name,
                f"payload.runtime_sources.{alias}",
                "source_rows",
                f"사용 원본 데이터: {alias}",
                row_count=source_result.get("row_count") or len(rows),
                columns=_columns_from_rows(rows),
                source_alias=str(alias),
                dataset_key=source_result.get("dataset_key"),
                source_type=source_result.get("source_type"),
            )
        )
    return refs


# 함수 설명: `_data_ref_object()`는 문자열 또는 dict 참조를 ref_id 중심의 표준 data_ref 객체로 바꿉니다.
def _data_ref_object(
    ref_id: str,
    database: str,
    collection_name: str,
    path: str,
    role: str,
    label: str,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "store": "mongodb",
        "ref_id": ref_id,
        "database": database,
        "collection_name": collection_name,
        "path": path,
        "role": role,
        "label": label,
    }
    for key, value in extra.items():
        if _has_value(value):
            result[key] = _json_ready(value)
    return result


# 함수 설명: `_has_value()`는 입력값이 값 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


# 함수 설명: `_source_result_by_alias()`는 결과·BY·alias 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_result_by_alias(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(value, list):
        return result
    for item in value:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias:
            result[alias] = item
    return result


# 함수 설명: `_columns_from_rows()`는 행 목록의 key 등장 순서를 유지하면서 결과 테이블의 컬럼 목록을 계산합니다.
def _columns_from_rows(rows: list[Any]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


# 함수 설명: `_mark_skipped()`는 현재 작업 payload에 실행 생략 상태와 구체적인 사유를 기록합니다.
def _mark_skipped(payload: dict[str, Any], database: str, collection_name: str, message: str) -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("warnings", []).append({"type": "missing_mongo_uri", "message": message})
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
        "stage": "23_mongodb_result_store",
        "status": "skipped",
        "database": database,
        "collection_name": collection_name,
        "data_ref": "",
        "errors": [{"type": "missing_mongo_uri", "message": message}],
    }
    return payload


# 함수 설명: `_execution_blocked()`는 필수 source 조회 실패로 후속 결과 저장이 금지됐는지 확인합니다.
def _execution_blocked(payload: dict[str, Any]) -> bool:
    gate = payload.get("execution_gate") if isinstance(payload.get("execution_gate"), dict) else {}
    return str(gate.get("status") or "").strip().lower() == "blocked"


# 함수 설명: `_mark_execution_blocked()`는 MongoDB 연결·index·write 없이 저장 생략 상태만 trace에 기록합니다.
def _mark_execution_blocked(payload: dict[str, Any], database: str, collection_name: str) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data.pop("data_ref", None)
    payload.pop("data_refs", None)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
        "stage": "23_mongodb_result_store",
        "status": "skipped",
        "reason": "required_source_retrieval_failed",
        "database": database,
        "collection_name": collection_name,
        "data_ref": "",
        "errors": [],
    }
    return payload


# 함수 설명: `_mark_error()`는 현재 작업 payload에 오류 상태와 정규화된 오류 정보를 기록합니다.
def _mark_error(payload: dict[str, Any], database: str, collection_name: str, data_ref: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("errors", []).extend(errors)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
        "stage": "23_mongodb_result_store",
        "status": "error",
        "database": database,
        "collection_name": collection_name,
        "data_ref": data_ref,
        "errors": errors,
    }
    return payload


# 함수 설명: `_mark_followup_unavailable()`는 불완전 저장본을 정상 ref로 노출하지 않고 현재 답변만 유지하도록 fail-closed 처리합니다.
def _mark_followup_unavailable(
    payload: dict[str, Any],
    database: str,
    collection_name: str,
    data_ref: str,
    storage_manifest: dict[str, Any],
) -> dict[str, Any]:
    warning = {
        "type": "result_store_limit_exceeded",
        "message": "결과 저장 상한을 초과해 불완전한 data_ref를 만들지 않았습니다. 현재 답변은 정상이며 이 결과를 이용한 후속 재사용은 불가합니다.",
    }
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data.pop("data_ref", None)
    payload.pop("data_refs", None)
    payload.setdefault("trace", {}).setdefault("warnings", []).append(warning)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_store"] = {
        "stage": "23_mongodb_result_store",
        "status": "followup_unavailable",
        "database": database,
        "collection_name": collection_name,
        "data_ref": "",
        "attempted_data_ref": data_ref,
        "storage_manifest": deepcopy(storage_manifest),
        "warnings": [warning],
        "errors": [],
    }
    return payload


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MongoDBResultStore(Component):
    display_name = "23 MongoDB 결과 저장소"
    description = "pandas 분석 결과와 런타임 조회 결과를 MongoDB result store에 저장하고 data_ref를 페이로드에 남깁니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=False),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=False),
        MessageTextInput(name="collection_name", display_name="결과 컬렉션", required=False, value=DEFAULT_COLLECTION, advanced=False),
        MessageTextInput(name="ttl_hours", display_name="데이터 보관 시간(시간)", value=str(DEFAULT_TTL_HOURS), required=False, advanced=True),
        MessageTextInput(name="max_result_rows", display_name="저장 결과 최대 행 수", value=str(DEFAULT_MAX_RESULT_ROWS), required=False, advanced=True),
        MessageTextInput(
            name="max_source_rows_per_alias",
            display_name="소스별 저장 최대 행 수",
            value=str(DEFAULT_MAX_SOURCE_ROWS_PER_ALIAS),
            required=False,
            advanced=True,
        ),
        MessageTextInput(name="max_document_bytes", display_name="결과 문서 최대 바이트", value=str(DEFAULT_MAX_DOCUMENT_BYTES), required=False, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=store_result(
                getattr(self, "payload", None),
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", ""),
                getattr(self, "collection_name", ""),
                getattr(self, "ttl_hours", ""),
                getattr(self, "max_result_rows", ""),
                getattr(self, "max_source_rows_per_alias", ""),
                getattr(self, "max_document_bytes", ""),
            )
        )
