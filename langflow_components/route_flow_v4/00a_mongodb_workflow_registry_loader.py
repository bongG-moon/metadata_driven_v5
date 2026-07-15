# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00A Workflow Registry 로더
# 역할: standalone 입력으로 지정한 MongoDB 또는 inline seed에서 활성 Workflow Skill을 읽고 질문 관련 후보만 제공합니다.
# 주요 입력: 사용자 질문, Registry 소스 모드, MongoDB 연결 정보, inline seed, 상태·조회·후보·바이트 제한
# 주요 출력: 계획 Prompt/Parser용 후보 Registry JSON Message, 조회·선정 상태 Data
# 처리 흐름: 소스 모드 확정 -> 문서 조회/검증 -> 안전 필드 정규화 -> 질문 관련도 계산 -> 최대 8개/바이트 제한 적용
# 유지보수 포인트: mongodb 모드에서 inline seed로 자동 fallback하지 않으며 전체 Mongo 문서나 연결 URI를 출력에 포함하지 않습니다.
# =============================================================================

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DropdownInput, MessageTextInput, MultilineInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

REGISTRY_CONTRACT_VERSION = "workflow.registry.v1"
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_workflow_skills"
DEFAULT_SOURCE = "mongodb"
MAX_CANDIDATES = 8
DEFAULT_MAX_ITEMS = 1000
DEFAULT_MAX_REGISTRY_BYTES = 65536
MAX_REGISTRY_BYTES = 262144
MAX_STEPS = 4
MAX_STEP_QUESTION_CHARS = 4000
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
STEP_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z가-힣_/.-]+")
MATCH_STOP_TOKENS = {
    "오늘",
    "어제",
    "현재",
    "관련",
    "데이터",
    "업무",
    "실행",
    "조회",
    "조회해줘",
    "알려줘",
    "보여줘",
    "해줘",
}


# 주요 함수: 선택된 명시적 소스에서 Workflow Skill을 읽고 사용자 질문과 관련된 후보 Registry를 만듭니다.
def load_workflow_registry_candidates(
    user_question: Any = "",
    registry_source: Any = DEFAULT_SOURCE,
    mongo_uri: Any = "",
    mongo_database: Any = DEFAULT_DATABASE,
    collection_name: Any = DEFAULT_COLLECTION,
    inline_seed_json: Any = "{}",
    status_filter: Any = "active",
    max_items: Any = DEFAULT_MAX_ITEMS,
    candidate_limit: Any = MAX_CANDIDATES,
    max_registry_bytes: Any = DEFAULT_MAX_REGISTRY_BYTES,
) -> dict[str, Any]:
    question = _text(user_question).strip()
    source = str(registry_source or DEFAULT_SOURCE).strip().lower()
    item_limit = _bounded_int(max_items, DEFAULT_MAX_ITEMS, 1, 5000)
    requested_candidate_limit = _bounded_int(candidate_limit, MAX_CANDIDATES, 1, MAX_CANDIDATES)
    byte_limit = _bounded_int(max_registry_bytes, DEFAULT_MAX_REGISTRY_BYTES, 4096, MAX_REGISTRY_BYTES)
    if source not in {"mongodb", "inline_seed"}:
        return _error_result(
            source,
            question,
            byte_limit,
            [_issue("invalid_registry_source", "Registry 소스는 mongodb 또는 inline_seed여야 합니다.")],
        )
    if not question:
        return _result(
            source=source,
            status="skipped",
            question=question,
            workflows=[],
            loaded_count=0,
            rejected_count=0,
            candidate_limit=requested_candidate_limit,
            byte_limit=byte_limit,
            errors=[_issue("empty_question", "사용자 질문이 비어 있어 Workflow 후보 조회를 건너뛰었습니다.")],
        )

    if source == "mongodb":
        documents, load_errors, truncated = _load_mongodb_documents(
            mongo_uri,
            mongo_database,
            collection_name,
            status_filter,
            item_limit,
        )
    else:
        documents, load_errors, truncated = _load_inline_documents(inline_seed_json, item_limit)

    if load_errors:
        return _error_result(source, question, byte_limit, load_errors)

    workflows, rejected = _safe_workflows(documents)
    ranked = _rank_workflows(question, workflows)
    candidates, byte_truncated = _bounded_candidates(ranked, requested_candidate_limit, byte_limit, source)
    errors: list[dict[str, Any]] = []
    if truncated:
        errors.append(
            _issue(
                "registry_item_limit_reached",
                "Workflow Registry 조회 제한에 도달했습니다. 필요한 경우 최대 조회 건수를 늘리세요.",
                max_items=item_limit,
            )
        )
    if rejected:
        errors.append(
            _issue(
                "invalid_workflow_documents_skipped",
                "계약에 맞지 않는 Workflow 문서를 후보에서 제외했습니다.",
                rejected_count=rejected,
            )
        )
    if byte_truncated:
        errors.append(
            _issue(
                "registry_byte_limit_reached",
                "후보 Registry 바이트 제한에 도달해 일부 후보를 제외했습니다.",
                max_registry_bytes=byte_limit,
            )
        )
    status = "ok" if candidates else "empty"
    return _result(
        source=source,
        status=status,
        question=question,
        workflows=candidates,
        loaded_count=len(documents),
        rejected_count=rejected,
        candidate_limit=requested_candidate_limit,
        byte_limit=byte_limit,
        errors=errors,
    )


# 함수 설명: `_load_mongodb_documents()`는 입력 포트로 받은 URI만 사용해 활성 workflow_skills 문서를 projection 조회합니다.
def _load_mongodb_documents(
    mongo_uri: Any,
    mongo_database: Any,
    collection_name: Any,
    status_filter: Any,
    item_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    uri = _secret_text(mongo_uri).strip()
    database_name = str(mongo_database or DEFAULT_DATABASE).strip()
    collection = str(collection_name or DEFAULT_COLLECTION).strip()
    status = str(status_filter or "active").strip()
    if not uri:
        return [], [_issue("missing_mongo_uri", "MongoDB 연결 URI가 비어 있습니다.")], False
    if not database_name or not collection:
        return [], [_issue("missing_mongo_target", "MongoDB 데이터베이스와 컬렉션 이름이 필요합니다.")], False

    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(uri, serverSelectionTimeoutMS=5000)
        query: dict[str, Any] = {"section": "workflow_skills"}
        if status:
            query["status"] = status
        projection = {"_id": 0, "section": 1, "key": 1, "status": 1, "payload": 1}
        cursor = client[database_name][collection].find(query, projection).limit(item_limit + 1)
        documents = [deepcopy(item) for item in list(cursor) if isinstance(item, dict)]
        return documents[:item_limit], [], len(documents) > item_limit
    except Exception as exc:
        safe_message = _redact_secret(str(exc), uri)
        return [], [_issue("workflow_registry_load_error", f"MongoDB Workflow Registry 조회에 실패했습니다: {safe_message}")], False
    finally:
        if client is not None:
            client.close()


# 함수 설명: `_load_inline_documents()`는 명시적으로 inline_seed 모드를 선택했을 때만 seed JSON을 Registry 문서 형태로 변환합니다.
def _load_inline_documents(value: Any, item_limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    text = _text(value).strip()
    if not text:
        return [], [], False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return [], [_issue("inline_registry_parse_error", f"Inline seed JSON 형식이 올바르지 않습니다: {exc}")], False
    source = parsed.get("workflows") if isinstance(parsed, dict) and "workflows" in parsed else parsed
    documents: list[dict[str, Any]] = []
    if isinstance(source, dict):
        for key, item in source.items():
            if isinstance(item, dict):
                workflow = deepcopy(item)
                workflow.setdefault("workflow_key", str(key))
                documents.append({"section": "workflow_skills", "key": str(key), "status": "active", "payload": workflow})
    elif isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            key = str(item.get("workflow_key") or item.get("key") or "").strip()
            documents.append({"section": "workflow_skills", "key": key, "status": "active", "payload": deepcopy(item)})
    else:
        return [], [_issue("invalid_inline_registry", "Inline seed Registry는 object 또는 array여야 합니다.")], False
    return documents[:item_limit], [], len(documents) > item_limit


# 함수 설명: `_safe_workflows()`는 MongoDB 저장 문서에서 계획·매칭에 필요한 허용 필드만 추출하고 잘못된 문서를 제외합니다.
def _safe_workflows(documents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    normalized: list[dict[str, Any]] = []
    rejected = 0
    for document in documents:
        workflow = _safe_workflow(document)
        if not workflow:
            rejected += 1
            continue
        normalized.append(workflow)
    key_counts: dict[str, int] = {}
    for workflow in normalized:
        key = str(workflow.get("workflow_key") or "")
        key_counts[key] = key_counts.get(key, 0) + 1
    duplicate_keys = {key for key, count in key_counts.items() if count > 1}
    workflows = [workflow for workflow in normalized if str(workflow.get("workflow_key") or "") not in duplicate_keys]
    rejected += sum(key_counts[key] for key in duplicate_keys)
    return workflows, rejected


# 함수 설명: `_safe_workflow()`는 한 저장 문서를 workflow.registry.v1 후보 항목으로 정규화합니다.
def _safe_workflow(document: Any) -> dict[str, Any]:
    source = document if isinstance(document, dict) else {}
    payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
    key = str(source.get("key") or payload.get("workflow_key") or payload.get("key") or "").strip()
    if not KEY_PATTERN.fullmatch(key):
        return {}
    raw_steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    if not 1 <= len(raw_steps) <= MAX_STEPS:
        return {}
    steps: list[dict[str, Any]] = []
    seen_step_ids: set[str] = set()
    for raw_step in raw_steps:
        step = _safe_step(raw_step)
        step_id = str(step.get("step_id") or "") if step else ""
        if not step or step_id in seen_step_ids:
            return {}
        seen_step_ids.add(step_id)
        steps.append(step)
    return {
        "workflow_key": key,
        "title": _clip(payload.get("display_name") or payload.get("title") or key, 200),
        "description": _clip(payload.get("description"), 1000),
        "aliases": _string_list(payload.get("aliases"), 20, 120),
        "intent_examples": _string_list(payload.get("intent_examples") or payload.get("examples"), 20, 300),
        "keywords": _string_list(payload.get("keywords"), 40, 100),
        "excluded_keywords": _string_list(payload.get("excluded_keywords"), 40, 100),
        "priority": _bounded_int(payload.get("priority"), 0, -1000, 1000),
        "steps": steps,
    }


# 함수 설명: `_safe_step()`은 실행 계획에 허용된 필드와 길이만 보존하고 기본값을 명시합니다.
def _safe_step(value: Any) -> dict[str, Any]:
    step = value if isinstance(value, dict) else {}
    step_id = str(step.get("step_id") or step.get("id") or "").strip()
    tool_name = str(step.get("tool_name") or step.get("tool") or "").strip()
    question = _clip(step.get("question"), MAX_STEP_QUESTION_CHARS)
    if not STEP_ID_PATTERN.fullmatch(step_id) or not TOOL_NAME_PATTERN.fullmatch(tool_name) or not question:
        return {}
    handoff = str(step.get("handoff") or "none").strip().lower()
    on_error = str(step.get("on_error") or "stop").strip().lower()
    if handoff not in {"none", "result_ref"} or on_error not in {"stop", "continue"}:
        return {}
    return {
        "step_id": step_id,
        "tool_name": tool_name,
        "question": question,
        "depends_on": _string_list(step.get("depends_on"), MAX_STEPS, 64),
        "handoff": handoff,
        "on_error": on_error,
    }


# 함수 설명: `_rank_workflows()`는 exact key/alias를 최우선으로 하고 등록 문구의 질문 관련도를 안정적으로 계산합니다.
def _rank_workflows(question: str, workflows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_question = _normalize_text(question)
    question_tokens = set(_tokens(question))
    ranked: list[tuple[float, int, str, dict[str, Any]]] = []
    for workflow in workflows:
        score = _workflow_score(normalized_question, question_tokens, workflow)
        if score <= 0:
            continue
        priority = _bounded_int(workflow.get("priority"), 0, -1000, 1000)
        ranked.append((score, priority, str(workflow.get("workflow_key") or ""), workflow))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [deepcopy(item[3]) for item in ranked]


# 함수 설명: `_workflow_score()`는 제외 키워드가 있는 후보를 제거하고 key·alias·예시·설명의 일치 점수를 계산합니다.
def _workflow_score(normalized_question: str, question_tokens: set[str], workflow: dict[str, Any]) -> float:
    excluded = [_normalize_text(item) for item in _string_list(workflow.get("excluded_keywords"), 40, 100)]
    if any(item and item in normalized_question for item in excluded):
        return 0.0
    key = _normalize_text(workflow.get("workflow_key"))
    aliases = [_normalize_text(item) for item in _string_list(workflow.get("aliases"), 20, 120)]
    title = _normalize_text(workflow.get("title"))
    if normalized_question == key:
        return 10000.0
    if normalized_question in aliases:
        return 9500.0

    score = 0.0
    if key and key in normalized_question:
        score += 700.0
    if title and title in normalized_question:
        score += 600.0
    score += 500.0 * sum(1 for alias in aliases if alias and alias in normalized_question)
    for keyword in _string_list(workflow.get("keywords"), 40, 100):
        normalized_keyword = _normalize_text(keyword)
        if normalized_keyword and normalized_keyword in normalized_question:
            score += 260.0
    score += 140.0 * _best_token_overlap(question_tokens, workflow.get("intent_examples"))
    score += 80.0 * _token_overlap(question_tokens, _tokens(workflow.get("description")))
    score += 60.0 * _token_overlap(question_tokens, _tokens(workflow.get("title")))
    return score


# 함수 설명: `_best_token_overlap()`은 여러 intent example 가운데 사용자 질문과 가장 가까운 토큰 겹침 비율을 반환합니다.
def _best_token_overlap(question_tokens: set[str], values: Any) -> float:
    return max(
        (_token_overlap(question_tokens, _tokens(value)) for value in _string_list(values, 20, 300)),
        default=0.0,
    )


# 함수 설명: `_token_overlap()`은 짧은 한국어·영문 질문에도 과도한 점수가 생기지 않도록 교집합을 질문 토큰 수로 나눕니다.
def _token_overlap(question_tokens: set[str], candidate_tokens: list[str]) -> float:
    if not question_tokens:
        return 0.0
    return len(question_tokens.intersection(candidate_tokens)) / len(question_tokens)


# 함수 설명: `_bounded_candidates()`는 순위 상위 후보를 최대 8개 및 직렬화 바이트 제한 안에서만 Registry에 포함합니다.
def _bounded_candidates(
    ranked: list[dict[str, Any]],
    candidate_limit: int,
    byte_limit: int,
    source: str,
) -> tuple[list[dict[str, Any]], bool]:
    candidates: list[dict[str, Any]] = []
    truncated = False
    for workflow in ranked[:candidate_limit]:
        trial = candidates + [workflow]
        registry = _registry_document(source, "ok", trial, 0, 0, candidate_limit, byte_limit, [])
        if len(json.dumps(registry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > byte_limit:
            truncated = True
            continue
        candidates.append(workflow)
    return candidates, truncated


# 함수 설명: `_result()`는 후보 Registry JSON과 비밀정보를 제외한 진단 상태를 함께 만듭니다.
def _result(
    *,
    source: str,
    status: str,
    question: str,
    workflows: list[dict[str, Any]],
    loaded_count: int,
    rejected_count: int,
    candidate_limit: int,
    byte_limit: int,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    bounded_workflows = deepcopy(workflows)
    bounded_errors = [deepcopy(item) for item in errors]
    while True:
        effective_status = "empty" if status == "ok" and not bounded_workflows else status
        registry = _registry_document(
            source,
            effective_status,
            bounded_workflows,
            loaded_count,
            rejected_count,
            candidate_limit,
            byte_limit,
            bounded_errors,
        )
        registry_json = json.dumps(registry, ensure_ascii=False, separators=(",", ":"))
        if len(registry_json.encode("utf-8")) <= byte_limit or not bounded_workflows:
            break
        bounded_workflows.pop()
        if not any(str(item.get("type")) == "registry_byte_limit_reached" for item in bounded_errors):
            bounded_errors.append(
                _issue(
                    "registry_byte_limit_reached",
                    "후보 Registry 바이트 제한에 도달해 일부 후보를 제외했습니다.",
                    max_registry_bytes=byte_limit,
                )
            )
    return {
        "status": effective_status,
        "source": source,
        "question_present": bool(question),
        "loaded_count": loaded_count,
        "rejected_count": rejected_count,
        "candidate_count": len(bounded_workflows),
        "candidate_keys": [str(item.get("workflow_key") or "") for item in bounded_workflows],
        "registry_bytes": len(registry_json.encode("utf-8")),
        "max_registry_bytes": byte_limit,
        "errors": deepcopy(bounded_errors),
        "workflow_registry_json": registry_json,
    }


# 함수 설명: `_error_result()`는 소스 조회 실패를 빈 Registry meta에 명시하고 다른 소스로 우회하지 않습니다.
def _error_result(source: str, question: str, byte_limit: int, errors: list[dict[str, Any]]) -> dict[str, Any]:
    return _result(
        source=source,
        status="error",
        question=question,
        workflows=[],
        loaded_count=0,
        rejected_count=0,
        candidate_limit=MAX_CANDIDATES,
        byte_limit=byte_limit,
        errors=errors,
    )


# 함수 설명: `_registry_document()`는 Prompt와 Parser가 함께 읽는 workflow.registry.v1 후보 문서를 만듭니다.
def _registry_document(
    source: str,
    status: str,
    workflows: list[dict[str, Any]],
    loaded_count: int,
    rejected_count: int,
    candidate_limit: int,
    byte_limit: int,
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "contract_version": REGISTRY_CONTRACT_VERSION,
        "meta": {
            "status": status,
            "source": source,
            "loaded_count": loaded_count,
            "rejected_count": rejected_count,
            "candidate_count": len(workflows),
            "candidate_limit": candidate_limit,
            "max_registry_bytes": byte_limit,
            "errors": deepcopy(errors),
        },
        "workflows": {str(item["workflow_key"]): deepcopy(item) for item in workflows},
    }


# 함수 설명: `_string_list()`는 저장 필드의 문자열 목록을 개수·글자 수 제한 안에서 중복 없이 정규화합니다.
def _string_list(value: Any, max_items: int, max_chars: int) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = _clip(item, max_chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


# 함수 설명: `_tokens()`는 질문과 등록 문구를 비교할 영문·숫자·한글 업무 토큰 목록을 만듭니다.
def _tokens(value: Any) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_PATTERN.findall(str(value or ""))
        if len(token.strip()) >= 2 and token.lower() not in MATCH_STOP_TOKENS
    ]


# 함수 설명: `_normalize_text()`는 exact/부분 일치 비교에서 공백·대소문자 차이를 제거합니다.
def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


# 함수 설명: `_bounded_int()`는 화면 문자열 숫자를 허용 범위 안의 정수로 변환합니다.
def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


# 함수 설명: `_clip()`은 저장 문서의 긴 문자열이 후보 payload를 과도하게 키우지 않도록 자릅니다.
def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


# 함수 설명: `_secret_text()`는 Langflow SecretStr와 일반 문자열을 URI 자체를 노출하지 않고 실행 문자열로 변환합니다.
def _secret_text(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "")


# 함수 설명: `_secret_fingerprint()`는 URI 원문을 component cache 상태에 남기지 않고 변경 여부만 비교할 digest를 만듭니다.
def _secret_fingerprint(value: Any) -> str:
    return hashlib.sha256(_secret_text(value).encode("utf-8")).hexdigest()


# 함수 설명: `_redact_secret()`은 외부 드라이버 예외가 입력 URI를 되풀이하더라도 상태 payload에서 제거합니다.
def _redact_secret(message: Any, secret: Any) -> str:
    text = str(message or "")
    secret_text = str(secret or "")
    return text.replace(secret_text, "<redacted>") if secret_text else text


# 함수 설명: `_text()`는 Langflow Message/Data 또는 일반 값을 JSON 파싱 가능한 문자열로 변환합니다.
def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if text is not None:
        return str(text)
    data = getattr(value, "data", value)
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False)
    return str(data or "")


# 함수 설명: `_issue()`는 Registry 조회·선정 경고와 오류를 표준 type/message dict로 만듭니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": _clip(message, 1000), **extra}


# Langflow 컴포넌트 클래스: standalone 화면에서 소스를 명시하고 질문 관련 Workflow 후보만 Prompt/Parser에 제공합니다.
class MongoDBWorkflowRegistryLoader(Component):
    display_name = "00A Workflow Registry 로더"
    description = "MongoDB 또는 명시적 inline seed에서 질문 관련 Workflow Skill을 최대 8개만 불러옵니다."
    name = "MongoDBWorkflowRegistryLoader"
    icon = "DatabaseZap"

    inputs = [
        MessageTextInput(
            name="user_question",
            display_name="사용자 질문",
            info="후보를 고를 현재 사용자 질문입니다.",
            value="",
            required=True,
        ),
        DropdownInput(
            name="registry_source",
            display_name="Registry 소스",
            info="mongodb 또는 inline_seed를 명시적으로 선택합니다. 실패 시 다른 소스로 자동 전환하지 않습니다.",
            options=["mongodb", "inline_seed"],
            value=DEFAULT_SOURCE,
            required=True,
        ),
        MessageTextInput(
            name="mongo_uri",
            display_name="MongoDB 연결 URI",
            info="Langflow Credential Global Variable MONGO_URL을 연결합니다.",
            value="",
            required=False,
        ),
        MessageTextInput(
            name="mongo_database",
            display_name="MongoDB 데이터베이스",
            info="Workflow Skill을 저장한 데이터베이스입니다.",
            value=DEFAULT_DATABASE,
            required=True,
        ),
        MessageTextInput(
            name="collection_name",
            display_name="Workflow Skill 컬렉션",
            info="section=workflow_skills 문서를 조회할 컬렉션입니다.",
            value=DEFAULT_COLLECTION,
            required=True,
        ),
        MultilineInput(
            name="inline_seed_json",
            display_name="Inline Seed Registry JSON 입력",
            info="Registry 소스를 inline_seed로 선택한 경우에만 사용합니다.",
            value="{}",
            required=False,
        ),
        MessageTextInput(
            name="status_filter",
            display_name="상태 필터",
            info="MongoDB에서 조회할 status 값입니다.",
            value="active",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="max_items",
            display_name="최대 조회 건수",
            info="후보 선정 전에 읽을 최대 Workflow 문서 수입니다.",
            value=str(DEFAULT_MAX_ITEMS),
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="candidate_limit",
            display_name="최대 후보 수",
            info="질문 관련 후보 수입니다. 최대 8개로 제한됩니다.",
            value=str(MAX_CANDIDATES),
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="max_registry_bytes",
            display_name="최대 후보 바이트",
            info="계획 모델과 Parser에 전달할 Registry JSON의 최대 UTF-8 바이트입니다.",
            value=str(DEFAULT_MAX_REGISTRY_BYTES),
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(
            name="workflow_registry_json",
            display_name="Workflow 후보 Registry JSON",
            method="build_registry_message",
            types=["Message"],
            group_outputs=True,
        ),
        Output(
            name="registry_status",
            display_name="Workflow Registry 상태",
            method="build_registry_status",
            types=["Data"],
            group_outputs=True,
        ),
    ]

    # 주요 메서드: 같은 입력의 group output만 조회 결과를 공유하고 질문·소스·설정이 바뀌면 다시 조회합니다.
    def _result_once(self) -> dict[str, Any]:
        values = (
            getattr(self, "user_question", ""),
            getattr(self, "registry_source", DEFAULT_SOURCE),
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", DEFAULT_DATABASE),
            getattr(self, "collection_name", DEFAULT_COLLECTION),
            getattr(self, "inline_seed_json", "{}"),
            getattr(self, "status_filter", "active"),
            getattr(self, "max_items", DEFAULT_MAX_ITEMS),
            getattr(self, "candidate_limit", MAX_CANDIDATES),
            getattr(self, "max_registry_bytes", DEFAULT_MAX_REGISTRY_BYTES),
        )
        cache_key = tuple(_secret_fingerprint(value) if index == 2 else _text(value) for index, value in enumerate(values))
        cached = getattr(self, "_workflow_registry_result", None)
        if isinstance(cached, dict) and getattr(self, "_workflow_registry_cache_key", None) == cache_key:
            return cached
        result = load_workflow_registry_candidates(
            *values,
        )
        self._workflow_registry_cache_key = cache_key
        self._workflow_registry_result = result
        self.status = f"{result.get('status', 'unknown')} / 후보 {result.get('candidate_count', 0)}건"
        return result

    # Langflow 출력 함수: Prompt와 Parser가 같은 후보 정의를 읽도록 compact JSON Message를 반환합니다.
    def build_registry_message(self) -> Message:
        return Message(text=str(self._result_once().get("workflow_registry_json") or "{}"))

    # Langflow 출력 함수: 운영자가 소스 상태·후보 key·제한 여부를 확인할 비밀정보 없는 Data를 반환합니다.
    def build_registry_status(self) -> Data:
        result = deepcopy(self._result_once())
        result.pop("workflow_registry_json", None)
        return Data(data=result)
