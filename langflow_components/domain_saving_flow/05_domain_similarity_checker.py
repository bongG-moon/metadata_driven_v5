# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05 도메인 동일 Key 조회기
# 역할: 후보 생성 후 같은 section의 exact key와 유일한 key/alias/display_name identity를 조회해 canonical 대상을 결정합니다.
# 주요 입력: 페이로드 (payload) · 필수, 기존 항목(호환용) (existing_items), MongoDB 연결 URI (mongo_uri), MongoDB 데이터베이스
#        (mongo_database), 컬렉션 이름 (collection_name)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 도메인 후보와 기존 문서의 canonical key 또는 허용된 identity 충돌을 찾아 저장 정책 결정에 필요한 match 정보를 만듭니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

import os
import re
import unicodedata
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_domain_items"
COLLECTION_ENV = "MONGODB_DOMAIN_COLLECTION"
GENERIC_IDENTITY_TOKENS = {
    "domain",
    "group",
    "metadata",
    "process",
    "process group",
    "processgroup",
    "공정",
    "공정 그룹",
    "공정그룹",
    "그룹",
    "도메인",
    "메타데이터",
}


# 주요 함수: 신규 후보와 기존 문서의 정확 key 또는 identity 충돌을 판정합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def check_similarity(
    payload_value: Any,
    existing_items_value: Any = None,
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    items = [item for item in payload.get("items", []) if isinstance(item, dict)]
    existing = _items(existing_items_value) or _items(payload.get("existing_items"))
    existing_by_id = {_doc_id(item): item for item in existing if _doc_id(item)}
    missing_items = _missing_candidates(items, existing_by_id)
    load = {
        "status": "provided",
        "count": len(existing_by_id),
        "provided_count": len(existing_by_id),
        "queried_candidate_count": 0,
        "loaded_count": 0,
        "errors": [],
    }
    if missing_items:
        loaded, load = _load_candidates(missing_items, mongo_uri, mongo_database, collection_name)
        loaded_count = 0
        for item in loaded:
            doc_id = _doc_id(item)
            if doc_id:
                existing_by_id[doc_id] = item
                loaded_count += 1
        load["provided_count"] = len(existing)
        load["queried_candidate_count"] = len(missing_items)
        load["loaded_count"] = loaded_count
        load["count"] = len(existing_by_id)
    matches = []
    ambiguous_count = 0
    for item in items:
        doc_id = _doc_id(item)
        if doc_id and doc_id in existing_by_id:
            key = _key(item)
            exact_existing = existing_by_id[doc_id]
            competing_matches = _identity_matches(item, [existing for existing_id, existing in existing_by_id.items() if existing_id != doc_id])
            if competing_matches:
                ambiguous_count += 1
                matches.append(
                    {
                        "new_key": key,
                        "existing_key": "",
                        "match_type": "ambiguous_identity",
                        "similarity_level": "ambiguous",
                        "identity_resolution": "ambiguous",
                        "recommended_action": "review",
                        "reason": "동일 key 항목 외에도 같은 식별자를 공유하는 기존 항목이 있습니다.",
                        "existing_candidate_keys": [key, *[_key(existing_item) for existing_item, _tokens in competing_matches]],
                    }
                )
                continue
            matches.append(
                {
                    "new_key": key,
                    "existing_key": key,
                    "match_type": "same_key",
                    "similarity_level": "exact",
                    "identity_resolution": "unique",
                    "recommended_action": "merge",
                    "reason": "같은 section/key가 이미 존재합니다.",
                    "existing_item": deepcopy(exact_existing),
                }
            )
            continue

        identity_matches = _identity_matches(item, list(existing_by_id.values()))
        if len(identity_matches) == 1:
            existing_item, shared_tokens = identity_matches[0]
            matches.append(
                {
                    "new_key": _key(item),
                    "existing_key": _key(existing_item),
                    "match_type": "identity_overlap",
                    "similarity_level": "high",
                    "identity_resolution": "unique",
                    "recommended_action": "merge",
                    "reason": f"같은 section에서 key/alias/display_name 식별자가 겹칩니다: {', '.join(shared_tokens)}",
                    "shared_identity_tokens": shared_tokens,
                    "existing_item": deepcopy(existing_item),
                }
            )
        elif len(identity_matches) > 1:
            ambiguous_count += 1
            matches.append(
                {
                    "new_key": _key(item),
                    "existing_key": "",
                    "match_type": "ambiguous_identity",
                    "similarity_level": "ambiguous",
                    "identity_resolution": "ambiguous",
                    "recommended_action": "review",
                    "reason": "동일한 key/alias/display_name 식별자와 겹치는 기존 항목이 여러 건입니다.",
                    "existing_candidate_keys": [_key(existing_item) for existing_item, _tokens in identity_matches],
                }
            )
    next_payload = payload
    next_payload.pop("existing_items", None)
    next_payload["existing_matches"] = matches
    next_payload["conflict_warnings"] = [_conflict_warning(match) for match in matches]
    next_payload.setdefault("trace", {})["duplicate_lookup"] = load
    next_payload["trace"]["identity_resolution"] = {
        "matched_count": sum(1 for match in matches if match.get("identity_resolution") == "unique"),
        "ambiguous_count": ambiguous_count,
        "policy": "same section plus exact normalized key/alias/display_name overlap; unique matches only",
    }
    return next_payload


# 함수 설명: `_identity_matches()`는 같은 section 안에서 신규 항목의 key·alias·display name과 겹치는 기존 canonical 문서를 찾습니다.
def _identity_matches(item: dict[str, Any], existing_items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    section = str(item.get("section") or "").strip().casefold()
    candidate_identity = _identity_parts(item)
    if not section or not candidate_identity["all"]:
        return []
    result = []
    for existing in existing_items:
        if str(existing.get("section") or "").strip().casefold() != section:
            continue
        existing_identity = _identity_parts(existing)
        evidence = set()
        if existing_identity["key"] and existing_identity["key"] in candidate_identity["all"]:
            evidence.add(existing_identity["key"])
        if candidate_identity["key"] and candidate_identity["key"] in existing_identity["all"]:
            evidence.add(candidate_identity["key"])
        evidence.update(candidate_identity["aliases"].intersection(existing_identity["aliases"]))
        if candidate_identity["display"] and candidate_identity["display"] == existing_identity["display"]:
            evidence.add(candidate_identity["display"])
        if evidence:
            result.append((existing, sorted(evidence)))
    return result


# 함수 설명: `_identity_parts()`는 도메인 항목의 key·별칭·표시명을 identity 비교용 원문 조각으로 모읍니다.
def _identity_parts(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
    key = _identity_token(item.get("key"), allow_compact=True)
    alias_tokens = {_identity_token(alias, allow_compact=True) for alias in aliases}
    display = _identity_token(payload.get("display_name"), allow_compact=False)
    alias_tokens = {token for token in alias_tokens if token}
    all_tokens = {token for token in {key, display, *alias_tokens} if token}
    return {"key": key, "aliases": alias_tokens, "display": display, "all": all_tokens}


# 함수 설명: `_identity_token()`는 NFKC·대소문자·구분자 정규화를 적용해 identity 비교 token을 만듭니다.
def _identity_token(value: Any, allow_compact: bool) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    strict = " ".join(text.split())
    if not strict:
        return ""
    token = strict
    if allow_compact and re.fullmatch(r"[a-z0-9][a-z0-9 /_.-]*", strict):
        token = re.sub(r"[ /_.-]+", "", strict)
    generic_tokens = {_normalize_generic(item) for item in GENERIC_IDENTITY_TOKENS}
    return token if len(token) >= 2 and token not in generic_tokens else ""


# 함수 설명: `_normalize_generic()`는 generic의 표기·자료형 차이를 비교와 저장에 사용할 표준 형태로 정규화합니다.
def _normalize_generic(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().casefold().split())


# 함수 설명: `_conflict_warning()`는 한 후보가 여러 기존 문서와 겹칠 때 ambiguous 저장 차단 경고를 구성합니다.
def _conflict_warning(match: dict[str, Any]) -> dict[str, Any]:
    if match.get("identity_resolution") == "ambiguous":
        return {
            "severity": "blocker",
            "message": "교체/병합 대상을 하나로 확정할 수 없습니다.",
            "new_item_key": match.get("new_key", ""),
            "existing_candidate_keys": deepcopy(match.get("existing_candidate_keys", [])),
        }
    return {
        "severity": "warning",
        "message": "동일한 기존 도메인 항목이 있어 선택한 중복 처리 방식을 적용합니다.",
        "new_item_key": match.get("new_key", ""),
        "existing_item_key": match.get("existing_key", ""),
        "match_type": match.get("match_type", ""),
    }


# 함수 설명: `_missing_candidates()`는 현재 전달된 기존 문서만으로 비교할 수 없어 추가 조회가 필요한 후보를 찾습니다.
def _missing_candidates(items: list[dict[str, Any]], existing_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    missing = []
    seen = set()
    for item in items:
        doc_id = _doc_id(item)
        if doc_id and doc_id not in existing_by_id and doc_id not in seen:
            missing.append(item)
            seen.add(doc_id)
    return missing


# 함수 설명: `_load_candidates()`는 후보 key에 해당하는 기존 MongoDB 문서만 추가 조회해 비교 범위를 최소화합니다.
def _load_candidates(items: list[dict[str, Any]], mongo_uri: str, mongo_database: str, collection_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    uri, database, collection = _resolve_mongo_config(mongo_uri, mongo_database, collection_name)
    if not uri:
        return [], {"status": "skipped", "database": database, "collection_name": collection, "count": 0, "errors": [{"type": "missing_mongo_uri", "message": "MONGODB_URI가 없어 동일 key/identity 추가 조회를 건너뛰었습니다."}]}
    client = None
    try:
        client = getattr(import_module("pymongo"), "MongoClient")(uri, serverSelectionTimeoutMS=5000)
        target = client[database][collection]
        docs = []
        seen_ids = set()
        identity_query_count = 0
        for doc_id in dict.fromkeys(_doc_id(item) for item in items if _doc_id(item)):
            doc = target.find_one({"_id": doc_id})
            if isinstance(doc, dict):
                docs.append(deepcopy(doc))
                seen_ids.add(_doc_id(doc))
        for item in items:
            query = _identity_query(item)
            if not query:
                continue
            identity_query_count += 1
            for doc in target.find(query).limit(50):
                doc_id = _doc_id(doc)
                is_identity_match = isinstance(doc, dict) and bool(_identity_matches(item, [doc]))
                if is_identity_match and doc_id and doc_id not in seen_ids:
                    docs.append(deepcopy(doc))
                    seen_ids.add(doc_id)
        return docs, {"status": "ok", "database": database, "collection_name": collection, "count": len(docs), "identity_query_count": identity_query_count, "errors": []}
    except Exception as exc:
        return [], {"status": "error", "database": database, "collection_name": collection, "count": 0, "errors": [{"type": "mongo_duplicate_lookup_error", "message": str(exc)}]}
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_resolve_mongo_config()`는 컴포넌트 입력→환경변수→기본값 순서로 MongoDB database와 collection 설정을 확정합니다.
def _resolve_mongo_config(mongo_uri: str, mongo_database: str, collection_name: str) -> tuple[str, str, str]:
    return (
        mongo_uri or os.getenv("MONGODB_URI", ""),
        mongo_database or os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE),
        collection_name or os.getenv(COLLECTION_ENV, DEFAULT_COLLECTION),
    )


# 함수 설명: `_identity_query()`는 후보 identity와 겹칠 수 있는 기존 도메인 문서만 조회하는 MongoDB 조건을 만듭니다.
def _identity_query(item: dict[str, Any]) -> dict[str, Any]:
    section = str(item.get("section") or "").strip()
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    aliases = payload.get("aliases") if isinstance(payload.get("aliases"), list) else []
    values = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in [item.get("key"), payload.get("display_name"), *aliases]
            if str(value or "").strip()
        )
    )
    if not section or not values:
        return {}
    return {
        "section": section,
        "$or": [
            {"key": {"$in": values}},
            {"payload.aliases": {"$in": values}},
            {"payload.display_name": {"$in": values}},
        ],
    }


# 함수 설명: `_doc_id()`는 메타데이터 항목의 section/key 계약으로 canonical MongoDB 문서 ID를 계산합니다.
def _doc_id(item: dict[str, Any]) -> str:
    section = str(item.get("section") or "").strip()
    key = str(item.get("key") or "").strip()
    return str(item.get("_id") or (f"domain:{section}:{key}" if section and key else ""))


# 함수 설명: `_key()`는 메타데이터 항목에서 비교·표시에 사용할 논리 key를 안전하게 꺼냅니다.
def _key(item: dict[str, Any]) -> str:
    section = str(item.get("section") or "").strip()
    key = str(item.get("key") or "").strip()
    return f"{section}:{key}" if section and key else key


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_items()`는 Langflow 값이나 payload에서 저장·비교 대상 items 목록만 안전하게 꺼냅니다.
def _items(value: Any) -> list[dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("existing_items") or data.get("items") or []
    else:
        raw = []
    return [deepcopy(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class DomainSimilarityChecker(Component):
    display_name = "05 도메인 동일 Key 조회기"
    description = "후보 생성 후 같은 section의 exact key와 유일한 key/alias/display_name identity를 조회해 canonical 대상을 결정합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        DataInput(name="existing_items", display_name="기존 항목(호환용)", required=False),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=True),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE, advanced=True),
        MessageTextInput(name="collection_name", display_name="컬렉션 이름", required=False, value=DEFAULT_COLLECTION, advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=check_similarity(getattr(self, "payload", None), getattr(self, "existing_items", None), getattr(self, "mongo_uri", ""), getattr(self, "mongo_database", ""), getattr(self, "collection_name", "")))
