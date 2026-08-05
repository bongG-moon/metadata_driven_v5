# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: CUBE 스케줄 MongoDB Writer
# 역할: 검증된 cube.schedule.v1 문서를 외부 authoring MongoDB에 versioned upsert합니다.
# 주요 입력: 스케줄 페이로드, MongoDB URI/database/collection, dry_run
# 주요 출력: 저장 결과(write_result)
# 유지보수 포인트: 실행 cursor·next_run_at·outbox는 source collection에 기록하지 않습니다.
# =============================================================================

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, MessageTextInput, Output
from lfx.schema.data import Data


DEFAULT_DATABASE = "cube_authoring"
DEFAULT_COLLECTION = "cube_schedules"


# 함수 설명: 검증된 source 문서를 dry-run으로 검토하거나 authoring MongoDB에 versioned upsert합니다.
def write_schedule(
    payload_value: Any,
    mongo_uri: Any = "",
    mongo_database: Any = DEFAULT_DATABASE,
    collection_name: Any = DEFAULT_COLLECTION,
    dry_run: Any = True,
    mongo_client_cls: Any = None,
) -> dict[str, Any]:
    payload = _payload(payload_value)
    result = deepcopy(payload)
    document = deepcopy(payload.get("schedule_document")) if isinstance(payload.get("schedule_document"), dict) else {}
    database = str(mongo_database or os.getenv("CUBE_SCHEDULE_SOURCE_DATABASE") or DEFAULT_DATABASE).strip()
    collection = str(collection_name or os.getenv("CUBE_SCHEDULE_SOURCE_COLLECTION") or DEFAULT_COLLECTION).strip()
    uri = str(mongo_uri or os.getenv("CUBE_SCHEDULE_SOURCE_MONGODB_URI") or "").strip()
    result["database"] = database
    result["collection_name"] = collection
    if not payload.get("ready_to_save") or not document:
        result.update({"status": "error", "success": False, "saved_count": 0, "message": "스케줄 검증 오류로 저장하지 않았습니다."})
        return result
    if _bool(dry_run, True):
        document["version"] = 1
        result.update({"status": "dry_run", "success": True, "saved_count": 0, "would_save_count": 1, "schedule_document": document, "message": f"드라이런 완료: {document.get('schedule_id')} 스케줄을 저장할 수 있습니다."})
        return result
    if not uri:
        result.setdefault("errors", []).append({"type": "missing_mongo_uri", "message": "MongoDB 연결 URI가 필요합니다."})
        result.update({"status": "error", "success": False, "saved_count": 0, "message": "MongoDB 연결 URI가 없어 저장하지 않았습니다."})
        return result
    client = None
    try:
        client_cls = mongo_client_cls or getattr(import_module("pymongo"), "MongoClient")
        client = client_cls(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000)
        target = client[database][collection]
        target.create_index("schedule_id", unique=True)
        schedule_id = str(document["schedule_id"])
        existing = target.find_one({"schedule_id": schedule_id}, {"version": 1, "created_at": 1}) or {}
        now = datetime.now(timezone.utc)
        version = max(0, int(existing.get("version") or 0)) + 1
        document.update({"version": version, "updated_at": now})
        if existing.get("created_at"):
            created_at = existing["created_at"]
        else:
            created_at = now
        target.update_one(
            {"schedule_id": schedule_id},
            {"$set": document, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )
        public_document = deepcopy(document)
        public_document["updated_at"] = now.isoformat()
        public_document["created_at"] = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        result.update({"status": "saved", "success": True, "saved_count": 1, "would_save_count": 0, "schedule_document": public_document, "message": f"CUBE 스케줄을 저장했습니다: {schedule_id} (version {version})"})
        return result
    except Exception as exc:
        result.setdefault("errors", []).append({"type": "mongo_write_error", "message": str(exc)})
        result.update({"status": "error", "success": False, "saved_count": 0, "message": f"CUBE 스케줄 저장 실패: {exc}"})
        return result
    finally:
        if client is not None:
            client.close()


# 함수 설명: Langflow Data 또는 일반 dict 입력을 외부 변경과 분리된 payload 복사본으로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: Writer의 dry_run 입력을 다양한 문자열 표현까지 포함해 안전한 boolean으로 해석합니다.
def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


# Langflow 컴포넌트 클래스: source authoring DB만 쓰고 Scheduler runtime 상태는 건드리지 않는 standalone Writer입니다.
class CubeScheduleMongoDBWriter(Component):
    display_name = "02 CUBE 스케줄 MongoDB Writer"
    description = "검증된 스케줄을 외부 authoring MongoDB source collection에 versioned upsert합니다."
    inputs = [
        DataInput(name="schedule_payload", display_name="스케줄 페이로드", required=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, value=""),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE),
        MessageTextInput(name="collection_name", display_name="스케줄 컬렉션", required=False, value=DEFAULT_COLLECTION),
        BoolInput(name="dry_run", display_name="드라이런", value=True),
    ]
    outputs = [Output(name="write_result", display_name="저장 결과", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 화면에 노출된 Mongo 설정과 dry-run 값을 적용한 단일 저장 결과를 Data로 반환합니다.
    def build_payload(self) -> Data:
        return Data(data=write_schedule(getattr(self, "schedule_payload", None), getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", DEFAULT_DATABASE), getattr(self, "collection_name", DEFAULT_COLLECTION), getattr(self, "dry_run", True)))
