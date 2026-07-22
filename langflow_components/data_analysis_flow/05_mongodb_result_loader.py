# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 MongoDB 이전 결과 로더
# 역할: 후속 질문의 reuse_strategy에 따라 MongoDB result store에서 필요한 이전 결과 또는 source alias만 복원합니다.
# 주요 입력: 페이로드 (payload) · 필수, MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스 (mongo_database), 결과 컬렉션 (collection_name)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 명시적 상위 결과 또는 후속 재사용 전략을 확인하고, 세션 검증과 MongoDB projection을 거쳐 필요한 행만 pandas source로 복원합니다.
# 유지보수 포인트: standalone Flow의 노드 입력으로 연결 설정을 받고, 오류는 숨기지 않고 trace/status에 남기며 연결은 반드시 닫습니다.
# =============================================================================

from __future__ import annotations

import re
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
PREVIOUS_RESULT_ALIAS = "previous_result"
ROW_REUSE_STRATEGIES = {"previous_result", "previous_source"}
SAFE_SOURCE_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


# 주요 함수: 저장된 이전 분석 결과를 찾아 후속 분석에서 재사용 가능한 source로 복원합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def load_previous_result(payload_value: Any, mongo_uri: str = "", mongo_database: str = "", collection_name: str = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    explicit_ref = _explicit_data_ref(payload)
    explicit_orchestration = bool(explicit_ref)
    reuse_strategy = _reuse_strategy(payload)
    mongo_uri, mongo_database, collection_name = _resolve_config(mongo_uri, mongo_database, collection_name)
    next_payload = payload

    if not explicit_orchestration and reuse_strategy not in ROW_REUSE_STRATEGIES:
        return _mark_skipped(
            next_payload,
            mongo_database,
            collection_name,
            "reuse_strategy_without_row_restore",
            f"reuse_strategy={reuse_strategy or 'none'}에는 이전 결과 행 복원이 필요하지 않습니다.",
            add_warning=False,
        )

    requested_aliases = _requested_source_aliases(next_payload) if reuse_strategy == "previous_source" else []
    if reuse_strategy == "previous_source" and not requested_aliases:
        return _mark_error(
            next_payload,
            mongo_database,
            collection_name,
            "",
            [{
                "type": "missing_previous_source_alias",
                "message": "previous_source 재사용 계획에 복원할 source_alias가 없습니다.",
            }],
        )

    ref = explicit_ref or _find_data_ref(payload)
    if not ref:
        return _mark_error(
            next_payload,
            mongo_database,
            collection_name,
            "",
            [{"type": "missing_data_ref", "message": "후속 분석에 필요한 이전 data_ref가 없습니다."}],
        )
    if not mongo_uri:
        return _mark_error(
            next_payload,
            mongo_database,
            collection_name,
            ref,
            [{"type": "missing_mongo_uri", "message": "후속 결과를 불러올 MongoDB 연결 URI가 비어 있습니다."}],
        )

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(mongo_uri, serverSelectionTimeoutMS=5000)
        projection = _mongo_projection(explicit_orchestration, reuse_strategy, requested_aliases)
        doc = client[mongo_database][collection_name].find_one({"_id": ref}, projection) or {}
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
        session_error = _session_error(next_payload, doc, explicit_orchestration)
        if session_error:
            return _mark_error(next_payload, mongo_database, collection_name, ref, [session_error])
        if explicit_orchestration:
            return _restore_explicit_upstream_result(
                next_payload,
                stored_payload,
                ref,
                mongo_database,
                collection_name,
            )
        if reuse_strategy == "previous_result":
            return _restore_previous_result(next_payload, stored_payload, ref, mongo_database, collection_name)
        return _restore_previous_sources(
            next_payload,
            stored_payload,
            requested_aliases,
            ref,
            mongo_database,
            collection_name,
        )
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


# 함수 설명: `_reuse_strategy()`는 정규화된 intent plan에서 이전 행 복원 전략을 읽습니다.
def _reuse_strategy(payload: dict[str, Any]) -> str:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    return str(plan.get("reuse_strategy") or "none").strip()


# 함수 설명: `_requested_source_aliases()`는 현재 pandas 계획에서 실제 참조한 이전 source alias만 추출합니다.
def _requested_source_aliases(payload: dict[str, Any]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    aliases: list[str] = []

    # 함수 설명: `append()`는 MongoDB field path로 사용 가능한 안전한 alias만 중복 없이 추가합니다.
    def append(value: Any) -> None:
        text = str(value or "").strip()
        if text and SAFE_SOURCE_ALIAS_PATTERN.fullmatch(text) and text not in aliases:
            aliases.append(text)

    # 함수 설명: `visit()`는 pandas 계획을 순회하며 source alias 계약 필드만 재귀적으로 수집합니다.
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key or "").strip().lower()
                if normalized_key in {"source_alias", "left_source_alias", "right_source_alias"}:
                    append(item)
                elif normalized_key == "source_aliases" and isinstance(item, list):
                    for alias in item:
                        append(alias)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(plan.get("pandas_execution_plan"))
    visit(plan.get("pandas_function_cases"))
    if aliases:
        return aliases

    # LLM이 alias를 생략했더라도 이전 source가 정확히 하나인 경우에만 모호하지 않은 계약으로 보완합니다.
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    previous_aliases = [
        str(item).strip()
        for item in current_data.get("source_aliases", [])
        if str(item or "").strip() and SAFE_SOURCE_ALIAS_PATTERN.fullmatch(str(item).strip())
    ] if isinstance(current_data.get("source_aliases"), list) else []
    previous_aliases = list(dict.fromkeys(previous_aliases))
    return previous_aliases if len(previous_aliases) == 1 else []


# 함수 설명: `_mongo_projection()`은 재사용 전략별로 MongoDB가 반환할 payload 경로를 최소 범위로 제한합니다.
def _mongo_projection(explicit_orchestration: bool, reuse_strategy: str, source_aliases: list[str]) -> dict[str, int]:
    projection = {
        "_id": 0,
        "session_id": 1,
        "expires_at": 1,
        "payload.storage_manifest": 1,
    }
    if explicit_orchestration or reuse_strategy == "previous_result":
        projection.update(
            {
                "payload.result_rows": 1,
                "payload.data": 1,
                "payload.analysis": 1,
            }
        )
    elif reuse_strategy == "previous_source":
        projection["payload.source_results"] = 1
        for alias in source_aliases:
            projection[f"payload.runtime_sources.{alias}"] = 1
    return projection


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


# 함수 설명: `_session_error()`는 일반 후속 결과와 명시적 상위 결과 모두 현재 세션과 일치할 때만 복원되도록 검증합니다.
def _session_error(
    payload: dict[str, Any],
    document: dict[str, Any],
    explicit_orchestration: bool,
) -> dict[str, Any] | None:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    current_session = str(request.get("session_id") or "").strip()
    stored_session = str(document.get("session_id") or "").strip()
    prefix = "upstream" if explicit_orchestration else "previous_result"
    label = "상위 결과" if explicit_orchestration else "이전 결과"
    if not current_session or not stored_session:
        return {
            "type": f"{prefix}_session_missing",
            "message": f"{label}와 현재 요청의 session_id를 모두 확인할 수 없어 복원을 차단했습니다.",
        }
    if current_session != stored_session:
        return {
            "type": f"{prefix}_session_mismatch",
            "message": f"현재 요청과 다른 세션에서 생성된 {label}이므로 복원을 차단했습니다.",
        }
    return None


# 주요 함수: `previous_result`는 저장된 최종 결과 행만 예약 alias로 복원하고 이전 원본 source는 payload에 넣지 않습니다.
def _restore_previous_result(
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
            [{"type": "previous_result_incomplete", "message": "이전 최종 결과가 저장 상한으로 잘려 있어 재분석에 사용할 수 없습니다."}],
        )

    result_rows = _stored_result_rows(stored_payload)
    data = _restore_data_from_stored_payload(stored_payload)
    columns = _string_list(data.get("columns")) or _columns_from_rows(result_rows)
    result_ref = _data_ref_object(
        ref_id,
        database,
        collection_name,
        "payload.result_rows",
        "analysis_result",
        "분석 결과 데이터",
        row_count=data.get("row_count") if data.get("row_count") is not None else len(result_rows),
        columns=columns,
        source_alias=PREVIOUS_RESULT_ALIAS,
    )
    payload["runtime_sources"] = {PREVIOUS_RESULT_ALIAS: deepcopy(result_rows)}
    payload["source_results"] = [
        {
            "dataset_key": PREVIOUS_RESULT_ALIAS,
            "source_alias": PREVIOUS_RESULT_ALIAS,
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
        }
    ]
    payload["data"] = data
    payload.setdefault("data", {})["data_ref"] = result_ref
    payload["data_refs"] = [result_ref]
    if isinstance(stored_payload.get("analysis"), dict):
        payload["analysis"] = deepcopy(stored_payload["analysis"])
    payload.pop("_runtime_rows_by_alias", None)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "ok",
        "mode": "previous_result",
        "database": database,
        "collection_name": collection_name,
        "data_ref": ref_id,
        "loaded_source_aliases": [PREVIOUS_RESULT_ALIAS],
        "projection_paths": ["payload.result_rows", "payload.data", "payload.analysis", "payload.storage_manifest"],
        "errors": [],
    }
    return payload


# 주요 함수: `previous_source`는 현재 pandas 계획이 명시한 source alias만 복원해 관계없는 과거 source 혼입을 막습니다.
def _restore_previous_sources(
    payload: dict[str, Any],
    stored_payload: dict[str, Any],
    source_aliases: list[str],
    ref_id: str,
    database: str,
    collection_name: str,
) -> dict[str, Any]:
    stored_sources = stored_payload.get("runtime_sources") if isinstance(stored_payload.get("runtime_sources"), dict) else {}
    source_summaries = _source_result_by_alias(stored_payload.get("source_results"))
    manifest = stored_payload.get("storage_manifest") if isinstance(stored_payload.get("storage_manifest"), dict) else {}
    source_manifest = manifest.get("runtime_sources") if isinstance(manifest.get("runtime_sources"), dict) else {}
    errors: list[dict[str, Any]] = []
    for alias in source_aliases:
        if alias not in stored_sources or not isinstance(stored_sources.get(alias), list):
            errors.append({"type": "previous_source_alias_not_found", "message": f"이전 결과 저장소에 source_alias={alias!r}가 없습니다."})
        elif isinstance(source_manifest.get(alias), dict) and source_manifest[alias].get("complete") is False:
            errors.append({"type": "previous_source_incomplete", "message": f"이전 source_alias={alias!r} 행이 저장 상한으로 잘려 있습니다."})
    if errors:
        return _mark_error(payload, database, collection_name, ref_id, errors)

    runtime_sources: dict[str, list[Any]] = {}
    source_results: list[dict[str, Any]] = []
    data_refs: list[dict[str, Any]] = []
    for alias in source_aliases:
        rows = stored_sources[alias]
        summary = deepcopy(source_summaries.get(alias, {}))
        columns = _string_list(summary.get("columns")) or _columns_from_rows(rows)
        source_ref = _data_ref_object(
            ref_id,
            database,
            collection_name,
            f"payload.runtime_sources.{alias}",
            "source_rows",
            f"사용 원본 데이터: {alias}",
            row_count=summary.get("row_count") if summary.get("row_count") is not None else len(rows),
            columns=columns,
            source_alias=alias,
            dataset_key=summary.get("dataset_key"),
            source_type=summary.get("source_type"),
        )
        runtime_sources[alias] = deepcopy(rows)
        summary.update(
            {
                "dataset_key": summary.get("dataset_key") or alias,
                "source_alias": alias,
                "source_type": summary.get("source_type") or "mongodb_result_store",
                "status": "ok",
                "success": True,
                "row_count": len(rows),
                "columns": columns,
                "data_ref": source_ref,
                "errors": [],
            }
        )
        source_results.append(summary)
        data_refs.append(source_ref)

    payload["runtime_sources"] = runtime_sources
    payload["source_results"] = source_results
    payload["data_refs"] = data_refs
    payload.pop("_runtime_rows_by_alias", None)
    payload.setdefault("trace", {}).setdefault("inspection", {})["result_loader"] = {
        "stage": "05_mongodb_result_loader",
        "status": "ok",
        "mode": "previous_source",
        "database": database,
        "collection_name": collection_name,
        "data_ref": ref_id,
        "loaded_source_aliases": source_aliases,
        "projection_paths": [f"payload.runtime_sources.{alias}" for alias in source_aliases],
        "errors": [],
    }
    return payload


# 함수 설명: `_stored_result_rows()`는 현재 저장 포맷과 legacy data.rows 포맷 모두에서 최종 결과 행을 읽습니다.
def _stored_result_rows(stored_payload: dict[str, Any]) -> list[Any]:
    if isinstance(stored_payload.get("result_rows"), list):
        return stored_payload["result_rows"]
    data = stored_payload.get("data") if isinstance(stored_payload.get("data"), dict) else {}
    return data.get("rows") if isinstance(data.get("rows"), list) else []


# 함수 설명: `_string_list()`는 입력 목록에서 비어 있지 않은 문자열을 중복 없이 유지합니다.
def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


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
    result_rows = _stored_result_rows(stored_payload)
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
    description = "reuse_strategy와 현재 pandas source alias에 따라 MongoDB의 이전 최종 결과 또는 필요한 원본만 복원합니다."
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
