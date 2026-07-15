# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00 Workflow Skill 기존 항목 로더
# 역할: MongoDB에 저장된 활성 Workflow Skill을 중복 판정용 최소 필드로 조회합니다.
# 주요 입력: MongoDB URI, 데이터베이스, 컬렉션, 조회 제한
# 주요 출력: 기존 항목(existing_items)
# 처리 흐름: standalone 노드 입력값으로 MongoDB에 연결하고 identity·실행 단계에 필요한 필드만 projection합니다.
# 유지보수 포인트: URI·DB·컬렉션 입력은 캔버스에서 보여야 하며 registration trace는 LLM이나 후속 노드로 전달하지 않습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_workflow_skills"


# 주요 함수: MongoDB에서 활성 Workflow Skill 문서를 제한된 projection으로 조회합니다.
def load_existing_items(mongo_uri: str = "", mongo_database: str = "", collection_name: str = "", limit: str = "500") -> dict[str, Any]:
    uri = str(mongo_uri or "").strip()
    database = str(mongo_database or DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    collection = str(collection_name or DEFAULT_COLLECTION).strip() or DEFAULT_COLLECTION
    load_limit = _int(limit, 500)
    if load_limit == 0:
        return _result("skipped", [], database, collection, [])
    if not uri:
        return _result("skipped", [], database, collection, [{"type": "missing_mongo_uri", "message": "MongoDB 연결 URI가 없어 기존 Workflow Skill 조회를 건너뛰었습니다."}])
    client = None
    try:
        client = getattr(import_module("pymongo"), "MongoClient")(uri, serverSelectionTimeoutMS=5000)
        projection = {
            "_id": 1,
            "section": 1,
            "key": 1,
            "status": 1,
            "payload.display_name": 1,
            "payload.description": 1,
            "payload.aliases": 1,
            "payload.intent_examples": 1,
            "payload.keywords": 1,
            "payload.excluded_keywords": 1,
            "payload.priority": 1,
            "payload.steps": 1,
            "updated_at": 1,
        }
        docs = list(
            client[database][collection]
            .find({"section": "workflow_skills", "status": {"$ne": "inactive"}}, projection)
            .limit(load_limit)
        )
        return _result("ok", docs, database, collection, [])
    except Exception as exc:
        return _result("error", [], database, collection, [{"type": "mongo_load_error", "message": str(exc)}])
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_result()`는 조회 결과와 진단 정보를 후속 노드가 재사용할 표준 계약으로 묶습니다.
def _result(status: str, items: list[dict[str, Any]], database: str, collection: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    safe_items = [deepcopy(item) for item in items if isinstance(item, dict)]
    return {
        "existing_items": safe_items,
        "metadata_load": {
            "status": status,
            "metadata_type": "workflow_skill",
            "database": database,
            "collection_name": collection,
            "count": len(safe_items),
            "errors": deepcopy(errors),
        },
    }


# 함수 설명: `_int()`는 조회 제한 입력을 0 이상의 정수로 변환하고 잘못된 값에는 기본값을 사용합니다.
def _int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return default


# Langflow 컴포넌트 클래스: standalone MongoDB 설정과 기존 Workflow Skill 조회 출력을 노출합니다.
class WorkflowSkillExistingItemsLoader(Component):
    display_name = "00 Workflow Skill 기존 항목 로더"
    description = "MongoDB에서 기존 Workflow Skill을 최소 projection으로 조회합니다."
    inputs = [
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, value=""),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE),
        MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION),
        MessageTextInput(name="limit", display_name="조회 제한", required=False, value="500", advanced=True),
    ]
    outputs = [Output(name="existing_items", display_name="기존 항목", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 현재 standalone MongoDB 입력으로 기존 Workflow Skill 목록을 반환합니다.
    def build_payload(self) -> Data:
        return Data(
            data=load_existing_items(
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", DEFAULT_DATABASE),
                getattr(self, "collection_name", DEFAULT_COLLECTION),
                getattr(self, "limit", "500"),
            )
        )
