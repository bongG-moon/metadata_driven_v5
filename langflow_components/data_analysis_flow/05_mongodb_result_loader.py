# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 MongoDB 이전 결과 로더
# 역할: payload/state 안의 data_ref를 자동으로 찾아 MongoDB result store의 이전 분석 결과를 복원합니다.
# 주요 입력: 페이로드 (payload) · 필수, MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 결과 컬렉션 (collection_name)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 이전 상태의 data_ref를 따라 저장된 분석 결과를 복원하고 source alias·columns·rows를 후속 분석용으로 재구성합니다.
# 유지보수 포인트: standalone Flow의 노드 입력으로 연결 설정을 받고, 오류는 숨기지 않고 trace/status에 남기며 연결은 반드시 닫습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_result_store"
RESULT_PREVIEW_LIMIT = 50
UPSTREAM_SOURCE_ALIAS = "upstream_result"


# 주요 함수: 저장된 이전 분석 결과를 찾아 후속 분석에서 재사용 가능한 source로 복원합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def load_previous_result(payload_value: Any, mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    explicit_ref = _explicit_data_ref(payload)
    ref = explicit_ref or _find_data_ref(payload)
    explicit_orchestration = bool(explicit_ref)
    mongo_uri, mongo_database, collection_name = _resolve_config(mongo_uri, mongo_database, collection_name)
    next_payload = payload
    if not ref:
        return _mark_skipped(next_payload, mongo_database, collection_name, "missing_data_ref", "data_ref가 없어 이전 결과를 불러오지 않았습니다.", add_warning=False)
    if not mongo_uri:
        if explicit_orchestration:
            return _mark_error(
                next_payload,
                mongo_database,
                collection_name,
                ref,
                [{"type": "missing_mongo_uri", "message": "명시적 상위 결과를 불러올 MongoDB 연결 URI가 비어 있습니다."}],
            )
        return _mark_skipped(next_payload, mongo_database, collection_name, "missing_mongo_uri", "MongoDB 연결 URI 노드 입력이 비어 있어 이전 결과를 불러오지 않았습니다.", ref)

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(mongo_uri, serverSelectionTimeoutMS=5000)
        doc = client[mongo_database][collection_name].find_one({"_id": ref}, {"_id": 0}) or {}
        if not doc:
            if explicit_orchestration:
                return _mark_error(
                    next_payload,
                    mongo_database,
                    collection_name,
                    ref,
                    [{"type": "upstream_result_not_found", "message": "상위 Flow result_ref에 해당하는 결과가 없습니다."}],
                )
            return _mark_skipped(next_payload, mongo_database, collection_name, "result_not_found", "data_ref에 해당하는 이전 결과가 없습니다.", ref)
        stored_payload = doc.get("payload", {}) if isinstance(doc.get("payload"), dict) else {}
        if explicit_orchestration:
            session_error = _explicit_session_error(next_payload, doc)
            if session_error:
                return _mark_error(next_payload, mongo_database, collection_name, ref, [session_error])
            return _restore_explicit_upstream_result(
                next_payload,
                stored_payload,
                ref,
                mongo_database,
                collection_name,
            )
        for key in ("source_results", "runtime_sources", "analysis"):
            if key in stored_payload:
                next_payload[key] = deepcopy(stored_payload[key])
        next_payload["data"] = _restore_data_from_stored_payload(stored_payload)
        data_refs = _build_data_refs(stored_payload, ref, mongo_database, collection_name)
        next_payload.setdefault("data", {})["data_ref"] = data_refs[0] if data_refs else _data_ref_object(ref, mongo_database, collection_name, "payload.result_rows", "analysis_result", "분석 결과 데이터")
        next_payload["data_refs"] = data_refs
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
            "stage": "05_mongodb_result_loader",
            "status": "ok",
            "database": mongo_database,
            "collection_name": collection_name,
            "data_ref": ref,
            "data_refs": data_refs,
            "errors": [],
        }
        return next_payload
    except Exception as exc:
        return _mark_error(next_payload, mongo_database, collection_name, ref, [{"type": "mongo_load_error", "message": str(exc)}])
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


# 함수 설명: `_find_data_ref()`는 입력 조건과 일치하는 데이터·참조을 찾아 비교·필터 결과로 반환합니다.
def _find_data_ref(payload: dict[str, Any]) -> str:
    explicit_ref = _explicit_data_ref(payload)
    if explicit_ref:
        return explicit_ref
    data = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
    state = payload.get("state", {}) if isinstance(payload.get("state"), dict) else {}
    current_data = state.get("current_data", {}) if isinstance(state.get("current_data"), dict) else {}
    for candidate in (data.get("data_ref"), current_data.get("data_ref"), state.get("data_ref")):
        ref = _ref_id(candidate)
        if ref:
            return ref
    return ""


# 함수 설명: `_explicit_data_ref()`는 Workflow Orchestrator가 별도 orchestration 영역에 명시한 상위 결과 참조만 추출합니다.
def _explicit_data_ref(payload: dict[str, Any]) -> str:
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    return _ref_id(orchestration.get("upstream_result_ref"))


# 함수 설명: `_explicit_session_error()`는 다른 실행 세션의 result_ref가 현재 Flow로 전달되는 것을 차단합니다.
def _explicit_session_error(payload: dict[str, Any], document: dict[str, Any]) -> dict[str, Any] | None:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    current_session = str(request.get("session_id") or "").strip()
    stored_session = str(document.get("session_id") or "").strip()
    if not current_session or not stored_session:
        return {
            "type": "upstream_session_missing",
            "message": "상위 결과와 현재 요청의 session_id를 모두 확인할 수 없어 연계 실행을 차단했습니다.",
        }
    if current_session != stored_session:
        return {
            "type": "upstream_session_mismatch",
            "message": "현재 요청과 다른 세션에서 생성된 상위 결과이므로 연계 실행을 차단했습니다.",
        }
    return None


# 주요 함수: 명시적 상위 결과는 현재 분석의 결과로 덮어쓰지 않고 예약 runtime source로 완전하게 복원합니다.
def _restore_explicit_upstream_result(
    payload: dict[str, Any],
    stored_payload: dict[str, Any],
    ref_id: str,
    database: str,
    collection_name: str,
) -> dict[str, Any]:
    manifest = stored_payload.get("storage_manifest") if isinstance(stored_payload.get("storage_manifest"), dict) else {}
    result_manifest = manifest.get("result_rows") if isinstance(manifest.get("result_rows"), dict) else {}
    if result_manifest.get("complete") is False:
        return _mark_error(
            payload,
            database,
            collection_name,
            ref_id,
            [{"type": "upstream_result_incomplete", "message": "상위 결과가 저장 상한으로 잘려 있어 연계 실행에 사용할 수 없습니다."}],
        )

    result_rows = stored_payload.get("result_rows") if isinstance(stored_payload.get("result_rows"), list) else []
    if not result_rows:
        return _mark_error(
            payload,
            database,
            collection_name,
            ref_id,
            [{"type": "upstream_result_empty", "message": "상위 Flow 결과에 다음 조회로 전달할 행이 없습니다."}],
        )

    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    if UPSTREAM_SOURCE_ALIAS in runtime_sources:
        return _mark_error(
            payload,
            database,
            collection_name,
            ref_id,
            [{"type": "upstream_alias_collision", "message": f"예약 source alias가 이미 사용 중입니다: {UPSTREAM_SOURCE_ALIAS}"}],
        )

    columns = _columns_from_rows(result_rows)
    result_ref = _data_ref_object(
        ref_id,
        database,
        collection_name,
        "payload.result_rows",
        "upstream_result",
        "상위 Flow 분석 결과",
        row_count=len(result_rows),
        columns=columns,
        source_alias=UPSTREAM_SOURCE_ALIAS,
    )
    runtime_sources[UPSTREAM_SOURCE_ALIAS] = deepcopy(result_rows)
    payload["runtime_sources"] = runtime_sources
    payload["source_results"] = _merge_source_result_by_alias(
        payload.get("source_results"),
        {
            "dataset_key": UPSTREAM_SOURCE_ALIAS,
            "source_alias": UPSTREAM_SOURCE_ALIAS,
            "source_type": "mongodb_result_store",
            "status": "ok",
            "success": True,
            "row_count": len(result_rows),
            "columns": columns,
            "data_ref": result_ref,
            "source_execution": {
                "adapter": "mongodb_result_store",
                "used_dummy_data": False,
                "source_configured": True,
            },
            "errors": [],
        },
    )
    payload["data_refs"] = _merge_data_refs(payload.get("data_refs"), [result_ref])
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    orchestration.update(
        {
            "explicit": True,
            "status": "ok",
            "upstream_result_ref": ref_id,
            "source_alias": UPSTREAM_SOURCE_ALIAS,
            "row_count": len(result_rows),
            "columns": columns,
        }
    )
    payload["orchestration"] = orchestration
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "ok",
        "mode": "explicit_orchestration",
        "database": database,
        "collection_name": collection_name,
        "data_ref": ref_id,
        "source_alias": UPSTREAM_SOURCE_ALIAS,
        "row_count": len(result_rows),
        "columns": columns,
        "errors": [],
    }
    return payload


# 함수 설명: `_merge_source_result_by_alias()`는 기존 결과를 보존하면서 예약 upstream alias 항목만 안전하게 추가·교체합니다.
def _merge_source_result_by_alias(existing: Any, addition: dict[str, Any]) -> list[dict[str, Any]]:
    result = [deepcopy(item) for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    addition_alias = str(addition.get("source_alias") or addition.get("dataset_key") or "").strip()
    for index, item in enumerate(result):
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias == addition_alias:
            result[index] = deepcopy(addition)
            return result
    result.append(deepcopy(addition))
    return result


# 함수 설명: `_merge_data_refs()`는 같은 ref/path 조합을 중복하지 않고 상위 결과 참조를 기존 참조 목록에 합칩니다.
def _merge_data_refs(existing: Any, additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    values = [*(existing if isinstance(existing, list) else []), *additions]
    for item in values:
        if not isinstance(item, dict):
            continue
        marker = (str(item.get("ref_id") or ""), str(item.get("path") or ""))
        if not marker[0] or marker in seen:
            continue
        seen.add(marker)
        result.append(deepcopy(item))
    return result


# 함수 설명: `_ref_id()`는 여러 data_ref 표현에서 실제 MongoDB 결과 참조 ID를 추출합니다.
def _ref_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("ref_id") or value.get("data_ref") or value.get("_id") or "").strip()
    return str(value or "").strip()


# 함수 설명: `_build_data_refs()`는 데이터·참조 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
def _build_data_refs(stored_payload: dict[str, Any], ref_id: str, database: str, collection_name: str) -> list[dict[str, Any]]:
    data = _restore_data_from_stored_payload(stored_payload)
    refs = [
        _data_ref_object(
            ref_id,
            database,
            collection_name,
            "payload.result_rows",
            "analysis_result",
            "분석 결과 데이터",
            row_count=data.get("row_count"),
            columns=data.get("columns"),
        )
    ]
    runtime_sources = stored_payload.get("runtime_sources") if isinstance(stored_payload.get("runtime_sources"), dict) else {}
    source_result_by_alias = _source_result_by_alias(stored_payload.get("source_results"))
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
def _data_ref_object(ref_id: str, database: str, collection_name: str, path: str, role: str, label: str, **extra: Any) -> dict[str, Any]:
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
        if value not in (None, "", [], {}):
            result[key] = deepcopy(value)
    return result


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


# 함수 설명: `_restore_data_from_stored_payload()`는 저장 payload의 rows·columns·source alias를 후속 분석용 data/runtime_sources로
#        복원합니다.
def _restore_data_from_stored_payload(stored_payload: dict[str, Any]) -> dict[str, Any]:
    data = deepcopy(stored_payload.get("data")) if isinstance(stored_payload.get("data"), dict) else {}
    result_rows = stored_payload.get("result_rows") if isinstance(stored_payload.get("result_rows"), list) else []
    if result_rows and "rows" not in data:
        data["rows"] = deepcopy(result_rows[:RESULT_PREVIEW_LIMIT])
    if result_rows and "row_count" not in data:
        data["row_count"] = len(result_rows)
    if result_rows and not data.get("columns"):
        data["columns"] = _columns_from_rows(result_rows)
    return data


# 함수 설명: `_mark_skipped()`는 현재 작업 payload에 실행 생략 상태와 구체적인 사유를 기록합니다.
def _mark_skipped(
    payload: dict[str, Any],
    database: str,
    collection_name: str,
    error_type: str,
    message: str,
    data_ref: str = "",
    add_warning: bool = True,
) -> dict[str, Any]:
    if add_warning:
        payload.setdefault("trace", {}).setdefault("warnings", []).append({"type": error_type, "message": message})
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "skipped",
        "database": database,
        "collection_name": collection_name,
        "data_ref": data_ref,
        "errors": [{"type": error_type, "message": message}],
    }
    return payload


# 함수 설명: `_mark_error()`는 현재 작업 payload에 오류 상태와 정규화된 오류 정보를 기록합니다.
def _mark_error(payload: dict[str, Any], database: str, collection_name: str, data_ref: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    payload.setdefault("trace", {}).setdefault("errors", []).extend(errors)
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    if _ref_id(orchestration.get("upstream_result_ref")):
        orchestration["status"] = "error"
        orchestration["errors"] = deepcopy(errors)
        payload["orchestration"] = orchestration
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "error",
        "database": database,
        "collection_name": collection_name,
        "data_ref": data_ref,
        "errors": errors,
    }
    return payload


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MongoDBResultLoader(Component):
    display_name = "05 MongoDB 이전 결과 로더"
    description = "payload/state 안의 data_ref를 자동으로 찾아 MongoDB result store의 이전 분석 결과를 복원합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=False),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=False),
        MessageTextInput(name="collection_name", display_name="결과 컬렉션", required=False, value=DEFAULT_COLLECTION, advanced=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=load_previous_result(
                getattr(self, "payload", None),
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", ""),
                getattr(self, "collection_name", ""),
            )
        )
