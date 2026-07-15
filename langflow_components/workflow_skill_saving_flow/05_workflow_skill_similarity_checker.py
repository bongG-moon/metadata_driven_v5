# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 Workflow Skill 유사 항목 조회기
# 역할: 신규 Workflow Skill과 기존 문서의 key·별칭·표시 이름 identity가 겹치는 대상을 찾습니다.
# 주요 입력: 페이로드, 기존 항목, MongoDB URI·데이터베이스·컬렉션
# 주요 출력: 페이로드 출력(payload_out)
# 처리 흐름: 전달된 기존 문서와 후보별 MongoDB 조회 결과를 합친 뒤 유일·없음·복수 유사 대상을 판정합니다.
# 유지보수 포인트: 키워드는 라우팅 신호일 뿐 저장 identity로 쓰지 않으며, 복수 유사 대상은 자동 선택하지 않습니다.
# =============================================================================

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_workflow_skills"


# 주요 함수: 후보별 Workflow identity를 비교해 유일·복수·미일치 결과를 페이로드에 기록합니다.
def check_similarity(
    payload_value: Any,
    existing_items_value: Any = None,
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    items = [_dict(item) for item in _list(payload.get("items")) if isinstance(item, dict)]
    existing_by_id = {_doc_id(item): item for item in _items(existing_items_value) if _doc_id(item)}
    loaded, lookup = _load_candidates(items, mongo_uri, mongo_database, collection_name)
    for item in loaded:
        doc_id = _doc_id(item)
        if doc_id:
            existing_by_id[doc_id] = item
    matches = []
    warnings = []
    for item in items:
        similar = _identity_matches(item, list(existing_by_id.values()))
        new_key = str(item.get("key") or "")
        if len(similar) == 1:
            existing, evidence = similar[0]
            matches.append(
                {
                    "new_key": new_key,
                    "existing_key": str(existing.get("key") or ""),
                    "match_type": "same_key" if _identity_token(new_key) == _identity_token(existing.get("key")) else "identity_overlap",
                    "identity_resolution": "unique",
                    "similarity_level": "exact" if _identity_token(new_key) == _identity_token(existing.get("key")) else "high",
                    "shared_identity_tokens": evidence,
                    "existing_item": deepcopy(existing),
                }
            )
            warnings.append(
                {
                    "severity": "warning",
                    "message": "유사한 기존 Workflow Skill이 있어 선택한 중복 처리 방식을 적용합니다.",
                    "new_item_key": new_key,
                    "existing_item_key": str(existing.get("key") or ""),
                }
            )
        elif len(similar) > 1:
            candidate_keys = [str(existing.get("key") or "") for existing, _ in similar]
            matches.append(
                {
                    "new_key": new_key,
                    "existing_key": "",
                    "match_type": "ambiguous_identity",
                    "identity_resolution": "ambiguous",
                    "similarity_level": "ambiguous",
                    "existing_candidate_keys": candidate_keys,
                }
            )
            warnings.append(
                {
                    "severity": "blocker",
                    "message": "유사한 Workflow Skill이 여러 건이라 자동으로 저장 대상을 고를 수 없습니다.",
                    "new_item_key": new_key,
                    "existing_candidate_keys": candidate_keys,
                }
            )
    payload.pop("existing_items", None)
    payload["existing_matches"] = matches
    payload["conflict_warnings"] = warnings
    payload.setdefault("trace", {})["duplicate_lookup"] = {
        **lookup,
        "provided_count": len(_items(existing_items_value)),
        "combined_count": len(existing_by_id),
        "matched_count": sum(1 for match in matches if match.get("identity_resolution") == "unique"),
        "ambiguous_count": sum(1 for match in matches if match.get("identity_resolution") == "ambiguous"),
        "policy": "normalized exact key, alias, or display_name; unique match only",
    }
    return payload


# 함수 설명: `_identity_matches()`는 후보와 key·alias·display_name이 실제로 겹치는 기존 문서를 모두 찾습니다.
def _identity_matches(item: dict[str, Any], existing_items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    candidate = _identity_parts(item)
    if not candidate:
        return []
    result = []
    for existing in existing_items:
        if str(existing.get("status") or "active").lower() == "inactive":
            continue
        existing_parts = _identity_parts(existing)
        overlap = sorted(candidate.intersection(existing_parts))
        if overlap:
            result.append((deepcopy(existing), overlap))
    return result


# 함수 설명: `_identity_parts()`는 Workflow의 canonical key·별칭·표시 이름을 비교용 token 집합으로 만듭니다.
def _identity_parts(item: dict[str, Any]) -> set[str]:
    payload = _dict(item.get("payload"))
    values = [item.get("key"), payload.get("display_name"), *_list(payload.get("aliases"))]
    return {token for token in (_identity_token(value) for value in values) if token}


# 함수 설명: `_identity_token()`은 NFKC·대소문자·구분자 차이를 제거한 Workflow identity token을 만듭니다.
def _identity_token(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[\s_./-]+", "", text)


# 함수 설명: `_load_candidates()`는 후보 identity와 겹칠 수 있는 활성 MongoDB 문서만 제한적으로 조회합니다.
def _load_candidates(
    items: list[dict[str, Any]],
    mongo_uri: str,
    mongo_database: str,
    collection_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    uri = str(mongo_uri or "").strip()
    database = str(mongo_database or DEFAULT_DATABASE).strip() or DEFAULT_DATABASE
    collection = str(collection_name or DEFAULT_COLLECTION).strip() or DEFAULT_COLLECTION
    if not items:
        return [], {"status": "skipped", "database": database, "collection_name": collection, "count": 0, "errors": []}
    if not uri:
        return [], {
            "status": "skipped",
            "database": database,
            "collection_name": collection,
            "count": 0,
            "errors": [{"type": "missing_mongo_uri", "message": "MongoDB 연결 URI가 없어 Workflow Skill 유사 항목 추가 조회를 건너뛰었습니다."}],
        }
    client = None
    try:
        client = getattr(import_module("pymongo"), "MongoClient")(uri, serverSelectionTimeoutMS=5000)
        target = client[database][collection]
        values = []
        ids = []
        for item in items:
            ids.append(f"workflow:{str(item.get('key') or '')}")
            item_payload = _dict(item.get("payload"))
            values.extend([item.get("key"), item_payload.get("display_name"), *_list(item_payload.get("aliases"))])
        values = list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))
        regex_values = [re.compile(f"^{re.escape(value)}$", re.IGNORECASE) for value in values]
        query = {
            "status": {"$ne": "inactive"},
            "$or": [
                {"_id": {"$in": list(dict.fromkeys(ids))}},
                {"key": {"$in": regex_values}},
                {"payload.display_name": {"$in": regex_values}},
                {"payload.aliases": {"$in": regex_values}},
            ],
        }
        projection = {"registration_trace": 0}
        docs = [deepcopy(doc) for doc in target.find(query, projection).limit(100) if isinstance(doc, dict)]
        return docs, {"status": "ok", "database": database, "collection_name": collection, "count": len(docs), "errors": []}
    except Exception as exc:
        return [], {
            "status": "error",
            "database": database,
            "collection_name": collection,
            "count": 0,
            "errors": [{"type": "mongo_duplicate_lookup_error", "message": str(exc)}],
        }
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_doc_id()`는 문서의 저장 ID가 없으면 workflow:key 계약으로 canonical ID를 계산합니다.
def _doc_id(item: dict[str, Any]) -> str:
    return str(item.get("_id") or (f"workflow:{item.get('key')}" if item.get("key") else ""))


# 함수 설명: `_items()`는 Langflow Data·dict·list에서 기존 Workflow 문서 목록만 안전하게 추출합니다.
def _items(value: Any) -> list[dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        data = data.get("existing_items") or data.get("items") or []
    return [deepcopy(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 후속 변경에 안전한 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 값이 dict일 때만 반환하고 아니면 빈 dict를 사용합니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 값이 list일 때만 반환하고 아니면 빈 목록을 사용합니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# Langflow 컴포넌트 클래스: 기존 Workflow Skill identity를 조회하고 중복 판정 결과를 다음 Writer에 전달합니다.
class WorkflowSkillSimilarityChecker(Component):
    display_name = "05 Workflow Skill 유사 항목 조회기"
    description = "key·별칭·표시 이름으로 유사 Workflow Skill을 찾고 복수 대상을 저장 차단 상태로 표시합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        DataInput(name="existing_items", display_name="기존 항목", required=False),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, value=""),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE),
        MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: 현재 후보와 기존 저장 문서를 비교한 페이로드를 반환합니다.
    def build_payload(self) -> Data:
        return Data(
            data=check_similarity(
                getattr(self, "payload", None),
                getattr(self, "existing_items", None),
                getattr(self, "mongo_uri", ""),
                getattr(self, "mongo_database", DEFAULT_DATABASE),
                getattr(self, "collection_name", DEFAULT_COLLECTION),
            )
        )
