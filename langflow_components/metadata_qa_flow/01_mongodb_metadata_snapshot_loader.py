# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 01 메타데이터 QA 통합 Snapshot 로더
# 역할: 한 MongoClient로 도메인·테이블 카탈로그·메인 필터 컬렉션을 읽어 하나의 짧은 TTL snapshot을 만듭니다.
# 주요 입력: 요청 페이로드, MongoDB 연결 정보, 컬렉션 이름 3종, 조회 제한 3종, 상태 필터, 캐시 TTL
# 주요 출력: 도메인 메타데이터, 테이블 카탈로그, 메인 필터
# 처리 흐름: 빈 질문을 먼저 차단하고, cache miss일 때만 한 연결에서 세 컬렉션을 순차 조회한 뒤 projection 결과를 공유합니다.
# 유지보수 포인트: cache는 프로세스 로컬이므로 실제 저장 성공 시 writer가 generation을 증가시켜 무효화하고, 다중 worker 간 차이는 TTL로 제한합니다.
# =============================================================================

from __future__ import annotations

import builtins
import hashlib
import os
import time
from copy import deepcopy
from importlib import import_module
from threading import RLock
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_DOMAIN_COLLECTION = "agent_v4_domain_items"
DEFAULT_TABLE_COLLECTION = "agent_v4_table_catalog_items"
DEFAULT_FILTER_COLLECTION = "agent_v4_main_flow_filters"
DEFAULT_CACHE_TTL_SECONDS = 15
CACHE_REGISTRY_NAME = "_metadata_driven_v5_qa_snapshot_cache_v1"
_CACHE_LOCK = RLock()


# 주요 함수: 동시 group output 요청도 한 번의 snapshot 조회만 수행하도록 프로세스 lock 안에서 로드합니다.
def load_metadata_snapshot(*args: Any, **kwargs: Any) -> dict[str, Any]:
    with _CACHE_LOCK:
        return _load_metadata_snapshot_unlocked(*args, **kwargs)


# 함수 설명: `_load_metadata_snapshot_unlocked()`는 한 MongoClient로 세 컬렉션을 읽고 cache 가능한 snapshot을 만듭니다.
def _load_metadata_snapshot_unlocked(
    request_payload_value: Any = None,
    mongo_uri: str = "",
    mongo_database: str = "",
    domain_collection_name: str = "",
    table_collection_name: str = "",
    filter_collection_name: str = "",
    domain_limit: Any = "1000",
    table_limit: Any = "1000",
    filter_limit: Any = "1000",
    status_filter: str = "active",
    cache_ttl_seconds: Any = None,
) -> dict[str, Any]:
    config = _resolve_config(
        mongo_uri,
        mongo_database,
        domain_collection_name,
        table_collection_name,
        filter_collection_name,
    )
    limits = {
        "domain_items": _limit(domain_limit, 1000),
        "table_catalog_items": _limit(table_limit, 1000),
        "main_flow_filters": _limit(filter_limit, 1000),
    }
    generation = _generation()
    if _has_empty_question(request_payload_value):
        error = {"type": "empty_question", "message": "질문이 비어 있어 MongoDB metadata snapshot 조회를 건너뛰었습니다."}
        return _empty_result("skipped", config, limits, status_filter, [error], generation)
    if not config["mongo_uri"]:
        error = {"type": "missing_mongo_uri", "message": "MONGODB_URI가 없어 metadata snapshot을 불러오지 않았습니다."}
        return _empty_result("skipped", config, limits, status_filter, [error], generation)

    ttl_seconds = _cache_ttl(cache_ttl_seconds)
    cache_key = _cache_key(config, limits, status_filter)
    cached = _cache_get(cache_key, ttl_seconds, generation)
    if cached is not None:
        return cached

    client = None
    items_by_kind: dict[str, list[dict[str, Any]]] = {
        "domain_items": [],
        "table_catalog_items": [],
        "main_flow_filters": [],
    }
    loads: dict[str, dict[str, Any]] = {}
    specs = (
        ("domain_items", config["domain_collection_name"], {"_id": 0, "section": 1, "key": 1, "status": 1, "payload": 1}),
        # v4 초기 문서는 dataset_key 대신 top-level key만 가진 경우가 있어 두 필드를 함께 읽습니다.
        ("table_catalog_items", config["table_collection_name"], {"_id": 0, "dataset_key": 1, "key": 1, "status": 1, "payload": 1}),
        ("main_flow_filters", config["filter_collection_name"], {"_id": 0, "filter_key": 1, "status": 1, "payload": 1}),
    )
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(config["mongo_uri"], serverSelectionTimeoutMS=5000)
        database = client[config["mongo_database"]]
        for metadata_kind, collection_name, projection in specs:
            try:
                item_limit = limits[metadata_kind]
                docs = list(
                    database[collection_name]
                    .find(_status_query(status_filter), projection)
                    .limit(item_limit + 1)
                )
                truncated = len(docs) > item_limit
                items = [deepcopy(doc) for doc in docs[:item_limit] if isinstance(doc, dict)]
                items_by_kind[metadata_kind] = items
                loads[metadata_kind] = _load_status(
                    "ok",
                    metadata_kind,
                    config["mongo_database"],
                    collection_name,
                    len(items),
                    status_filter,
                    [],
                    limit=item_limit,
                    truncated=truncated,
                )
            except Exception as exc:
                error = {"type": "mongo_load_error", "message": str(exc), "metadata_kind": metadata_kind}
                loads[metadata_kind] = _load_status(
                    "error",
                    metadata_kind,
                    config["mongo_database"],
                    collection_name,
                    0,
                    status_filter,
                    [error],
                    limit=limits[metadata_kind],
                )
    except Exception as exc:
        error = {"type": "mongo_snapshot_connection_error", "message": str(exc)}
        return _empty_result("error", config, limits, status_filter, [error], generation)
    finally:
        if client is not None:
            client.close()

    result = _snapshot_result(items_by_kind, loads, config, generation, cache_hit=False)
    # 한 컬렉션이라도 실패한 partial snapshot은 캐시하지 않아 다음 요청에서 즉시 재시도합니다.
    if result["metadata_snapshot"]["status"] == "ok":
        _cache_put(cache_key, result, generation)
    return result


# 함수 설명: `_resolve_config()`는 노드 입력→환경변수→v4 기본 컬렉션 순으로 실제 MongoDB 설정을 확정합니다.
def _resolve_config(
    mongo_uri: str,
    mongo_database: str,
    domain_collection_name: str,
    table_collection_name: str,
    filter_collection_name: str,
) -> dict[str, str]:
    return {
        "mongo_uri": mongo_uri or os.getenv("MONGODB_URI", ""),
        "mongo_database": mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        "domain_collection_name": domain_collection_name or os.getenv("MONGODB_DOMAIN_COLLECTION", DEFAULT_DOMAIN_COLLECTION),
        "table_collection_name": table_collection_name or os.getenv("MONGODB_TABLE_CATALOG_COLLECTION", DEFAULT_TABLE_COLLECTION),
        "filter_collection_name": filter_collection_name or os.getenv("MONGODB_MAIN_FLOW_FILTER_COLLECTION", DEFAULT_FILTER_COLLECTION),
    }


# 함수 설명: `_snapshot_result()`는 세 컬렉션 조회 결과와 공통 snapshot 상태를 하나의 payload로 묶습니다.
def _snapshot_result(
    items_by_kind: dict[str, list[dict[str, Any]]],
    loads: dict[str, dict[str, Any]],
    config: dict[str, str],
    generation: int,
    cache_hit: bool,
) -> dict[str, Any]:
    statuses = [str(load.get("status") or "error") for load in loads.values()]
    status = "ok" if statuses and all(item == "ok" for item in statuses) else "error" if not statuses or all(item == "error" for item in statuses) else "partial"
    errors = [deepcopy(error) for load in loads.values() for error in load.get("errors", []) if isinstance(error, dict)]
    return {
        **{kind: deepcopy(items_by_kind.get(kind, [])) for kind in ("domain_items", "table_catalog_items", "main_flow_filters")},
        "metadata_loads": deepcopy(loads),
        "metadata_snapshot": {
            "status": status,
            "database": config["mongo_database"],
            "collections": {
                "domain_items": config["domain_collection_name"],
                "table_catalog_items": config["table_collection_name"],
                "main_flow_filters": config["filter_collection_name"],
            },
            "count": sum(len(items_by_kind.get(kind, [])) for kind in items_by_kind),
            "cache_hit": cache_hit,
            "generation": generation,
            "errors": errors,
        },
    }


# 함수 설명: `_empty_result()`는 연결을 열지 않은 skip/error 결과도 정상 snapshot과 같은 shape로 만듭니다.
def _empty_result(
    status: str,
    config: dict[str, str],
    limits: dict[str, int],
    status_filter: str,
    errors: list[dict[str, Any]],
    generation: int,
) -> dict[str, Any]:
    collection_by_kind = {
        "domain_items": config["domain_collection_name"],
        "table_catalog_items": config["table_collection_name"],
        "main_flow_filters": config["filter_collection_name"],
    }
    loads = {
        kind: _load_status(
            status,
            kind,
            config["mongo_database"],
            collection,
            0,
            status_filter,
            deepcopy(errors),
            limit=limits[kind],
        )
        for kind, collection in collection_by_kind.items()
    }
    result = _snapshot_result({kind: [] for kind in collection_by_kind}, loads, config, generation, cache_hit=False)
    result["metadata_snapshot"]["status"] = status
    result["metadata_snapshot"]["limits"] = deepcopy(limits)
    return result


# 함수 설명: `_load_status()`는 Context Builder가 기존 loader와 동일하게 읽을 컬렉션별 metadata_load 계약을 만듭니다.
def _load_status(
    status: str,
    metadata_kind: str,
    database: str,
    collection_name: str,
    count: int,
    status_filter: str,
    errors: list[dict[str, Any]],
    limit: int | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    result = {
        "status": status,
        "metadata_kind": metadata_kind,
        "database": database,
        "collection_name": collection_name,
        "count": count,
        "status_filter": status_filter or "active",
        "cache_hit": False,
        "errors": deepcopy(errors),
    }
    if limit is not None:
        result["limit"] = int(limit)
        result["truncated"] = bool(truncated)
        if truncated:
            # limit+1 조회로 적어도 한 건이 더 있다는 사실을 보존해 "최소 N건"을 과소 표시하지 않습니다.
            result["total_count_lower_bound"] = int(count) + 1
    return result


# 함수 설명: `_output_payload()`는 통합 snapshot에서 한 output에 필요한 목록과 load 상태만 projection합니다.
def _output_payload(snapshot: dict[str, Any], metadata_kind: str) -> dict[str, Any]:
    load = deepcopy(snapshot.get("metadata_loads", {}).get(metadata_kind, {}))
    load["cache_hit"] = bool(snapshot.get("metadata_snapshot", {}).get("cache_hit"))
    return {
        metadata_kind: deepcopy(snapshot.get(metadata_kind, [])),
        "metadata_load": load,
        "metadata_snapshot": deepcopy(snapshot.get("metadata_snapshot", {})),
    }


# 함수 설명: `_has_empty_question()`은 요청 payload가 명시된 경우에만 빈 질문을 판정해 단독 loader 테스트 호환성을 유지합니다.
def _has_empty_question(value: Any) -> bool:
    if value is None:
        return False
    data = getattr(value, "data", value)
    return isinstance(data, dict) and isinstance(data.get("request"), dict) and not str(data["request"].get("question") or "").strip()


# 함수 설명: `_status_query()`는 active/all 설정을 MongoDB 조회 조건으로 변환합니다.
def _status_query(status_filter: str) -> dict[str, Any]:
    value = str(status_filter or "active").strip()
    return {} if not value or value.lower() == "all" else {"status": value}


# 함수 설명: `_limit()`은 각 컬렉션 조회 제한을 1 이상의 정수로 정규화합니다.
def _limit(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except Exception:
        return default


# 함수 설명: `_cache_ttl()`은 프로세스 캐시 TTL을 0~300초 범위로 제한합니다.
def _cache_ttl(value: Any) -> float:
    raw = value if value not in (None, "") else os.getenv("METADATA_QA_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS))
    try:
        return max(0.0, min(float(raw), 300.0))
    except Exception:
        return float(DEFAULT_CACHE_TTL_SECONDS)


# 함수 설명: `_registry()`는 standalone custom component들이 공유할 프로세스 로컬 cache registry를 준비합니다.
def _registry() -> dict[str, Any]:
    registry = getattr(builtins, CACHE_REGISTRY_NAME, None)
    if not isinstance(registry, dict) or not isinstance(registry.get("entries"), dict):
        registry = {"generation": 0, "entries": {}}
        setattr(builtins, CACHE_REGISTRY_NAME, registry)
    return registry


# 함수 설명: `_generation()`은 writer invalidation과 snapshot cache를 연결하는 현재 generation을 반환합니다.
def _generation() -> int:
    try:
        return max(0, int(_registry().get("generation", 0)))
    except Exception:
        return 0


# 함수 설명: `_cache_key()`는 URI 원문을 보관하지 않도록 해시한 뒤 snapshot 설정을 cache key로 만듭니다.
def _cache_key(config: dict[str, str], limits: dict[str, int], status_filter: str) -> tuple[Any, ...]:
    uri_hash = hashlib.sha256(config["mongo_uri"].encode("utf-8")).hexdigest()
    return (
        uri_hash,
        config["mongo_database"],
        config["domain_collection_name"],
        config["table_collection_name"],
        config["filter_collection_name"],
        limits["domain_items"],
        limits["table_catalog_items"],
        limits["main_flow_filters"],
        str(status_filter or "active"),
    )


# 함수 설명: `_cache_get()`은 현재 generation과 TTL이 모두 유효한 snapshot 복사본만 반환합니다.
def _cache_get(key: tuple[Any, ...], ttl_seconds: float, generation: int) -> dict[str, Any] | None:
    if ttl_seconds <= 0:
        return None
    with _CACHE_LOCK:
        entry = _registry()["entries"].get(key)
        if not isinstance(entry, dict) or entry.get("generation") != generation or time.monotonic() - float(entry.get("created_at", 0)) > ttl_seconds:
            _registry()["entries"].pop(key, None)
            return None
        result = deepcopy(entry.get("value"))
    if not isinstance(result, dict):
        return None
    result.setdefault("metadata_snapshot", {})["cache_hit"] = True
    for load in result.get("metadata_loads", {}).values():
        if isinstance(load, dict):
            load["cache_hit"] = True
    return result


# 함수 설명: `_cache_put()`은 조회 중 write invalidation이 없었던 성공 snapshot만 현재 generation에 저장합니다.
def _cache_put(key: tuple[Any, ...], result: dict[str, Any], generation: int) -> None:
    with _CACHE_LOCK:
        registry = _registry()
        if int(registry.get("generation", 0)) != generation:
            return
        entries = registry["entries"]
        if len(entries) >= 16:
            oldest_key = min(entries, key=lambda item: float(entries[item].get("created_at", 0)))
            entries.pop(oldest_key, None)
        entries[key] = {"generation": generation, "created_at": time.monotonic(), "value": deepcopy(result)}


# Langflow 컴포넌트 클래스: 세 group output은 동일 instance snapshot을 사용하고 각각 기존 Context Builder 입력 shape를 유지합니다.
class MetadataQaSnapshotLoader(Component):
    display_name = "01 메타데이터 QA 통합 Snapshot 로더"
    description = "한 MongoClient와 짧은 TTL cache로 도메인·테이블 카탈로그·메인 필터를 함께 불러옵니다."
    inputs = [
        DataInput(name="request_payload", display_name="요청 페이로드", required=False),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="domain_collection_name", display_name="도메인 컬렉션", required=False, value=DEFAULT_DOMAIN_COLLECTION, advanced=True),
        MessageTextInput(name="table_collection_name", display_name="테이블 카탈로그 컬렉션", required=False, value=DEFAULT_TABLE_COLLECTION, advanced=True),
        MessageTextInput(name="filter_collection_name", display_name="메인 필터 컬렉션", required=False, value=DEFAULT_FILTER_COLLECTION, advanced=True),
        MessageTextInput(name="domain_limit", display_name="도메인 조회 제한", required=False, value="1000", advanced=True),
        MessageTextInput(name="table_limit", display_name="테이블 조회 제한", required=False, value="1000", advanced=True),
        MessageTextInput(name="filter_limit", display_name="필터 조회 제한", required=False, value="1000", advanced=True),
        MessageTextInput(name="status_filter", display_name="상태 필터", required=False, value="active", advanced=True),
        MessageTextInput(name="cache_ttl_seconds", display_name="캐시 TTL(초)", required=False, value=str(DEFAULT_CACHE_TTL_SECONDS), advanced=True),
    ]
    outputs = [
        Output(name="domain_items", display_name="도메인 메타데이터", method="build_domain_items", types=["Data"], group_outputs=True),
        Output(name="table_catalog_items", display_name="테이블 카탈로그", method="build_table_items", types=["Data"], group_outputs=True),
        Output(name="main_flow_filters", display_name="메인 필터", method="build_filter_items", types=["Data"], group_outputs=True),
    ]

    # 주요 메서드: 한 component instance에서 세 output이 요청돼도 snapshot 조회는 한 번만 수행합니다.
    def _load_once(self) -> dict[str, Any]:
        cached = getattr(self, "_metadata_snapshot_result", None)
        if isinstance(cached, dict):
            return cached
        result = load_metadata_snapshot(
            getattr(self, "request_payload", None),
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", ""),
            getattr(self, "domain_collection_name", ""),
            getattr(self, "table_collection_name", ""),
            getattr(self, "filter_collection_name", ""),
            getattr(self, "domain_limit", "1000"),
            getattr(self, "table_limit", "1000"),
            getattr(self, "filter_limit", "1000"),
            getattr(self, "status_filter", "active"),
            getattr(self, "cache_ttl_seconds", None),
        )
        self._metadata_snapshot_result = result
        snapshot = result.get("metadata_snapshot", {}) if isinstance(result, dict) else {}
        self.status = f"{snapshot.get('status', 'unknown')} / {snapshot.get('count', 0)}건 / cache={snapshot.get('cache_hit', False)}"
        return result

    # Langflow 출력 함수: 통합 snapshot의 도메인 목록과 해당 load 상태를 반환합니다.
    def build_domain_items(self) -> Data:
        return Data(data=_output_payload(self._load_once(), "domain_items"))

    # Langflow 출력 함수: 통합 snapshot의 테이블 카탈로그 목록과 해당 load 상태를 반환합니다.
    def build_table_items(self) -> Data:
        return Data(data=_output_payload(self._load_once(), "table_catalog_items"))

    # Langflow 출력 함수: 통합 snapshot의 메인 필터 목록과 해당 load 상태를 반환합니다.
    def build_filter_items(self) -> Data:
        return Data(data=_output_payload(self._load_once(), "main_flow_filters"))
