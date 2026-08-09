# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03B Continuation 인식 의도 라우터
# 역할: 최초 요청에서만 의도 LLM을 호출하고, 재개 요청은 저장된 Typed IR을 검증해 복원합니다.
# 주요 입력: 요청 payload, 의도 프롬프트, 언어 모델, MongoDB 결과 저장소 설정
# 주요 출력: 최초 LLM JSON 또는 저장소에서 복원한 의도 JSON Message
# 처리 흐름: continuation 유무를 먼저 판정하고 재개이면 모델 객체를 호출하지 않은 채 저장 계약을 검증합니다.
# 유지보수 포인트: 공개 continuation 계약은 조회 키일 뿐이며 전체 계획은 반드시 서버 저장소에서 다시 읽습니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib import import_module
import json
import re
from typing import Any, Callable

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, ModelInput, Output, SecretStrInput
from lfx.schema.message import Message

CONTRACT_VERSION = "analysis.dependent_retrieval.v1"
MAX_STAGES = 2
DEFAULT_DATABASE = "datagov"
DEFAULT_COLLECTION = "agent_v4_result_store"


# 주요 함수: 일반 요청은 모델에 전달하고 continuation 요청은 저장된 계획만 복원합니다.
def route_intent_response(
    payload_value: Any,
    intent_prompt: Any = "",
    model_invoker: Callable[[str], Any] | None = None,
    stored_plan_loader: Callable[[str], Any] | None = None,
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
    metadata_candidates_value: Any = None,
) -> tuple[str, dict[str, Any]]:
    payload = _payload(payload_value)
    request_error = _blocked_request_error(payload)
    if request_error:
        envelope = {
            "intent_plan": {
                "analysis_kind": "invalid_request",
                "request_scope": "new_analysis",
                "reference_mode": "none",
                "retrieval_jobs": [],
                "pandas_execution_plan": [],
                "output_contract": {
                    "result_mode": "detail",
                    "required_columns": [],
                    "grain_columns": [],
                    "metric_columns": [],
                    "result_columns": [],
                    "strict_result_columns": True,
                },
                "validation_errors": [deepcopy(request_error)],
            },
            "trace": {
                "decision_reason": ["공개 continuation 입력 계약 검증 실패로 실행을 차단했습니다."],
                "errors": [deepcopy(request_error)],
            },
        }
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), {
            "mode": "request_blocked",
            "model_called": False,
            "intent_llm_skipped": True,
            "error": deepcopy(request_error),
        }
    continuation = _continuation_request(payload)
    if continuation:
        upstream_ref = _upstream_result_ref(payload)
        if not upstream_ref:
            raise ValueError("continuation 재개에는 upstream_result_ref가 필요합니다.")
        loader = stored_plan_loader or (
            lambda ref: _load_stored_document(ref, mongo_uri, mongo_database, collection_name)
        )
        document = loader(upstream_ref)
        envelope, trace = _validated_resume_envelope(payload, continuation, document)
        trace["upstream_result_ref"] = upstream_ref
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), trace

    catalog_error = _table_catalog_metadata_error(metadata_candidates_value)
    if catalog_error:
        envelope = _metadata_blocked_envelope(catalog_error)
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), {
            "mode": "metadata_blocked",
            "model_called": False,
            "intent_llm_skipped": True,
            "error": deepcopy(catalog_error),
        }

    prompt = _text(intent_prompt)
    if not prompt:
        raise ValueError("의도 분석 prompt가 비어 있습니다.")
    if model_invoker is None:
        raise RuntimeError("의도 분석 Language Model이 연결되지 않았습니다.")
    response = model_invoker(prompt)
    return _text(response), {
        "mode": "initial_intent",
        "model_called": True,
        "intent_llm_skipped": False,
        "prompt_chars": len(prompt),
    }


# 함수 설명: 최초 요청에서 Table Catalog가 실제로 등록·로드되었는지 확인해 추측 분석을 차단합니다.
def _table_catalog_metadata_error(value: Any) -> dict[str, Any]:
    """Require a registered Table Catalog before the initial intent model call."""

    # Backward-compatible direct callers can omit this optional input. The
    # generated continuation Flow always wires 01E candidate output here.
    if value is None:
        return {}
    envelope = _payload(value)
    candidates = envelope.get("metadata_candidates")
    if not isinstance(candidates, dict):
        candidates = envelope
    load = envelope.get("metadata_load") if isinstance(envelope.get("metadata_load"), dict) else {}
    if not load and isinstance(candidates.get("metadata_load"), dict):
        load = candidates.get("metadata_load")
    loads = load.get("loads") if isinstance(load.get("loads"), dict) else {}
    table_load = loads.get("table_catalog_items") if isinstance(loads.get("table_catalog_items"), dict) else {}
    table_status = str(table_load.get("status") or "").strip().lower()
    overall_status = str(load.get("status") or "").strip().lower()
    table_items = candidates.get("table_catalog_items") if isinstance(candidates.get("table_catalog_items"), list) else []
    dataset_keys = [
        str(item.get("dataset_key") or _dict(item.get("payload")).get("dataset_key") or "").strip()
        for item in table_items
        if isinstance(item, dict)
    ]
    dataset_keys = [item for item in dict.fromkeys(dataset_keys) if item]
    failed_loads = _metadata_load_failures(loads)
    if failed_loads or overall_status in {"error", "failed", "failure", "invalid"}:
        return _metadata_load_error(
            failed_loads or [{"metadata_kind": "metadata", "status": overall_status or "error"}],
            registered_dataset_count=len(dataset_keys),
        )
    if not dataset_keys:
        return {
            "type": "table_catalog_metadata_unavailable",
            "reason": "no_active_table_catalog",
            "message": "MongoDB 메타데이터 연결은 되었지만 활성 Table Catalog에 등록된 데이터셋이 없습니다. 데이터셋 등록 상태와 collection의 status=active 설정을 확인해 주세요.",
            "table_catalog_load_status": table_status or overall_status or "not_available",
            "registered_dataset_count": 0,
        }
    return {}


# 함수 설명: continuation 메타데이터 로더의 실패 항목을 안전한 짧은 진단 목록으로 만듭니다.
def _metadata_load_failures(loads: dict[str, Any]) -> list[dict[str, str]]:
    """Keep a bounded, credential-safe detail for failed continuation metadata loads."""

    failed: list[dict[str, str]] = []
    for metadata_kind, raw_load in loads.items():
        if not isinstance(raw_load, dict):
            continue
        status = str(raw_load.get("status") or "").strip().lower()
        if status not in {"error", "failed", "failure", "invalid", "skipped"}:
            continue
        raw_errors = raw_load.get("errors") if isinstance(raw_load.get("errors"), list) else []
        first_error = next((item for item in raw_errors if isinstance(item, dict)), {})
        error_type = str(first_error.get("type") or status or "metadata_load_error").strip()
        detail = _safe_metadata_error_detail(first_error.get("message") or raw_load.get("message") or "")
        failed.append(
            {
                "metadata_kind": str(metadata_kind or "metadata"),
                "status": status,
                "error_type": error_type,
                "detail": detail,
            }
        )
    return failed


# 함수 설명: 메타데이터 조회 실패를 사용자 조치가 가능한 단일 차단 오류로 구성합니다.
def _metadata_load_error(
    failed_loads: list[dict[str, str]],
    *,
    registered_dataset_count: int,
) -> dict[str, Any]:
    first = _primary_metadata_failure(failed_loads)
    kind = str(first.get("metadata_kind") or "metadata")
    error_type = str(first.get("error_type") or first.get("status") or "metadata_load_error")
    detail = str(first.get("detail") or "상세 오류 정보가 없습니다.")
    return {
        "type": "table_catalog_metadata_unavailable",
        "reason": "metadata_connection_or_loader_failed",
        "message": (
            "메타데이터 연결 정보를 확인해 주세요. 분석에 필요한 메타데이터를 읽지 못해 분석을 시작하지 않았습니다. "
            f"상세 사유: {kind} 조회 실패 ({error_type}) - {detail}"
        ),
        "table_catalog_load_status": str(first.get("status") or "error"),
        "registered_dataset_count": registered_dataset_count,
        "metadata_failures": deepcopy(failed_loads[:3]),
    }


# 함수 설명: 실행 데이터셋을 권한화하는 Table Catalog 오류를 대표 원인으로 선택합니다.
def _primary_metadata_failure(failed_loads: list[dict[str, str]]) -> dict[str, str]:
    """Prefer Table Catalog because it is the executable dataset authority."""

    return next(
        (item for item in failed_loads if str(item.get("metadata_kind") or "") == "table_catalog_items"),
        failed_loads[0] if failed_loads else {},
    )


# 함수 설명: 오류 원문에서 MongoDB 인증 정보를 제거하고 표시 가능한 길이로 자릅니다.
def _safe_metadata_error_detail(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"mongodb(?:\+srv)?://[^\s@/]+@", "mongodb://***@", text, flags=re.IGNORECASE)
    return text[:500] if text else "상세 오류 정보가 없습니다."


# 함수 설명: 메타데이터가 없을 때 LLM 계획을 만들지 않고 공통 최소 차단 계약을 반환합니다.
def _metadata_blocked_envelope(error: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent_plan": {
            "analysis_kind": "metadata_catalog_unavailable",
            "request_scope": "new_analysis",
            "reference_mode": "none",
            "reuse_strategy": "none",
            "metadata_refs": [],
            "retrieval_jobs": [],
            "pandas_execution_plan": [],
            "output_contract": {
                "result_mode": "detail",
                "required_columns": [],
                "grain_columns": [],
                "metric_columns": [],
                "result_columns": [],
                "strict_result_columns": True,
            },
            "validation_errors": [deepcopy(error)],
        },
        "metadata_refs": [],
        "trace": {
            "decision_reason": ["table_catalog_metadata_unavailable"],
            "errors": [deepcopy(error)],
        },
    }


# 함수 설명: 요청 로더가 구조화해 둔 continuation 입력 오류만 의도 LLM 전에 fail-close로 읽습니다.
def _blocked_request_error(payload: dict[str, Any]) -> dict[str, Any]:
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    if str(orchestration.get("status") or "").strip().lower() != "blocked":
        return {}
    error = orchestration.get("error") if isinstance(orchestration.get("error"), dict) else {}
    error_type = str(error.get("type") or "").strip()
    if not error_type.startswith("continuation_contract_"):
        return {}
    return deepcopy(error)


# 함수 설명: MongoDB에서 재개 검증에 필요한 최소 필드만 projection으로 읽습니다.
def _load_stored_document(
    ref_id: str,
    mongo_uri: str = "",
    mongo_database: str = "",
    collection_name: str = "",
) -> dict[str, Any]:
    uri = str(mongo_uri or "").strip()
    database = str(mongo_database or DEFAULT_DATABASE).strip()
    collection = str(collection_name or DEFAULT_COLLECTION).strip()
    if not uri:
        raise ValueError("continuation 계획을 복원할 MongoDB 연결 URI가 비어 있습니다.")
    client = None
    try:
        mongo_client_cls = getattr(import_module("pymongo"), "MongoClient")
        client = mongo_client_cls(uri, serverSelectionTimeoutMS=5000)
        projection = {
            "_id": 0,
            "session_id": 1,
            "expires_at": 1,
            "payload.request": 1,
            "payload.metadata_refs": 1,
            "payload.intent_plan": 1,
            "payload.analysis": 1,
            "payload.data.row_count": 1,
            "payload.storage_manifest": 1,
        }
        document = client[database][collection].find_one({"_id": ref_id}, projection) or {}
        if not document:
            raise ValueError("upstream_result_ref에 해당하는 저장 결과가 없습니다.")
        return deepcopy(document)
    finally:
        if client is not None:
            client.close()


# 주요 함수: 공개 계약과 저장된 세션·계획·단계·행 완전성을 교차 검증합니다.
def _validated_resume_envelope(
    payload: dict[str, Any],
    continuation: dict[str, Any],
    document_value: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    document = _payload(document_value)
    contract = continuation.get("continuation_contract")
    if not isinstance(contract, dict):
        raise ValueError("continuation_contract가 없습니다.")
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    request_session = str(request.get("session_id") or "").strip()
    stored_session = str(document.get("session_id") or "").strip()
    contract_session = str(contract.get("session_id") or "").strip()
    if not request_session or request_session != stored_session or request_session != contract_session:
        raise ValueError("continuation session_id가 저장 결과와 일치하지 않습니다.")
    expiry_error = _expiry_error(document.get("expires_at"))
    if expiry_error:
        raise ValueError(expiry_error)

    stored_payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
    analysis = stored_payload.get("analysis") if isinstance(stored_payload.get("analysis"), dict) else {}
    if str(analysis.get("status") or "").strip().lower() not in {"ok", "success"}:
        raise ValueError("정상 완료된 1차 분석 결과만 continuation으로 재개할 수 있습니다.")
    manifest = stored_payload.get("storage_manifest") if isinstance(stored_payload.get("storage_manifest"), dict) else {}
    result_manifest = manifest.get("result_rows") if isinstance(manifest.get("result_rows"), dict) else {}
    if result_manifest.get("complete") is not True:
        raise ValueError("1차 결과가 저장 한도로 잘려 continuation에 사용할 수 없습니다.")
    stored_count = _safe_int(result_manifest.get("stored_count"), -1)
    if stored_count <= 0:
        data = stored_payload.get("data") if isinstance(stored_payload.get("data"), dict) else {}
        stored_count = _safe_int(data.get("row_count"), 0)
    if stored_count <= 0:
        raise ValueError("빈 1차 결과는 continuation으로 재개하지 않습니다.")

    stored_plan = stored_payload.get("intent_plan") if isinstance(stored_payload.get("intent_plan"), dict) else {}
    dependent = (
        stored_plan.get("dependent_retrieval_plan")
        if isinstance(stored_plan.get("dependent_retrieval_plan"), dict)
        else {}
    )
    stages = [item for item in dependent.get("stages", []) if isinstance(item, dict)]
    runtime = dependent.get("runtime") if isinstance(dependent.get("runtime"), dict) else {}
    if len(stages) != MAX_STAGES or _safe_int(dependent.get("max_stages"), 0) != MAX_STAGES:
        raise ValueError("저장된 continuation 계획은 정확히 2단계여야 합니다.")
    calculated_hash = _plan_hash(dependent)
    for key, expected in (
        ("version", CONTRACT_VERSION),
        ("plan_id", str(dependent.get("plan_id") or "")),
        ("plan_hash", calculated_hash),
    ):
        actual = str(contract.get(key) or "").strip()
        stored = str(dependent.get(key) or "").strip()
        if not actual or actual != expected or actual != stored:
            raise ValueError(f"continuation {key}가 저장된 계획과 일치하지 않습니다.")
    if _safe_int(contract.get("max_stages"), 0) != MAX_STAGES:
        raise ValueError("continuation max_stages는 2여야 합니다.")
    current_index = _safe_int(contract.get("current_stage_index"), -1)
    next_index = _safe_int(contract.get("next_stage_index"), -1)
    if (
        current_index != 0
        or next_index != 1
        or _safe_int(runtime.get("active_stage_index"), -1) != current_index
        or str(runtime.get("status") or "").strip().lower() != "pending"
    ):
        raise ValueError("저장된 단계와 재개하려는 단계가 연속되지 않습니다.")
    expected_ref = f"continuation:{dependent.get('plan_id')}:{dependent.get('plan_hash')}"
    supplied_ref = str(continuation.get("continuation_ref") or contract.get("continuation_ref") or "").strip()
    if supplied_ref != expected_ref:
        raise ValueError("continuation_ref가 저장된 계획과 일치하지 않습니다.")
    if _canonical_json(contract.get("input_bindings", [])) != _canonical_json(
        stages[next_index].get("input_bindings", [])
    ):
        raise ValueError("continuation input_bindings가 저장된 다음 단계와 일치하지 않습니다.")

    envelope = {
        "intent_plan": deepcopy(stored_plan),
        "metadata_refs": deepcopy(stored_payload.get("metadata_refs", [])),
        "trace": {
            "decision_reason": ["저장된 종속 조회 계획의 검증된 다음 단계를 재개합니다."],
            "continuation": {
                "status": "resume_verified",
                "plan_id": str(dependent.get("plan_id") or ""),
                "plan_hash": str(dependent.get("plan_hash") or ""),
                "next_stage_index": next_index,
                "intent_llm_skipped": True,
            },
        },
    }
    return envelope, {
        "mode": "continuation_resume",
        "model_called": False,
        "intent_llm_skipped": True,
        "plan_id": str(dependent.get("plan_id") or ""),
        "plan_hash": str(dependent.get("plan_hash") or ""),
        "next_stage_index": next_index,
        "stored_row_count": stored_count,
    }


# 함수 설명: plan_id·hash·runtime을 제외한 전체 단계 계약을 안정적으로 해시합니다.
def _plan_hash(value: dict[str, Any]) -> str:
    canonical = deepcopy(value)
    for key in ("plan_id", "plan_hash", "runtime", "active_stage_index"):
        canonical.pop(key, None)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# 함수 설명: JSON 비교 시 키 순서 차이를 제거한 canonical 문자열을 만듭니다.
def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# 함수 설명: 요청 payload에서 구조화된 continuation 입력만 복사합니다.
def _continuation_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    value = request.get("continuation")
    return deepcopy(value) if isinstance(value, dict) else {}


# 함수 설명: 명시적 orchestration 영역에서 MongoDB 결과 참조 ID를 읽습니다.
def _upstream_result_ref(payload: dict[str, Any]) -> str:
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    value = orchestration.get("upstream_result_ref")
    if isinstance(value, dict):
        return str(value.get("ref_id") or value.get("data_ref") or value.get("_id") or "").strip()
    return str(value or "").strip()


# 함수 설명: Langflow Data 또는 일반 dict 입력을 독립 복사본으로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: Message 또는 일반 값을 공백이 정리된 문자열로 바꿉니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", value)
    if isinstance(text, dict):
        return json.dumps(text, ensure_ascii=False)
    return str(text or "").strip()


# 함수 설명: 잘못된 정수 입력을 예외 없이 지정 기본값으로 변환합니다.
def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 함수 설명: TTL 정리 전 남아 있는 문서도 사용할 수 없도록 UTC 만료 시각을 직접 판정합니다.
def _expiry_error(value: Any) -> str:
    if value in (None, ""):
        return "저장 결과의 expires_at을 확인할 수 없어 continuation을 재개하지 않았습니다."
    try:
        expires_at = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)
    except Exception:
        return "저장 결과의 expires_at 형식이 유효하지 않아 continuation을 재개하지 않았습니다."
    if expires_at <= datetime.now(timezone.utc):
        return "만료된 저장 결과로 continuation을 재개할 수 없습니다."
    return ""


# Langflow 컴포넌트 클래스: 최초 모델 호출과 저장 계획 재개를 하나의 조건부 경계에서 분리합니다.
class ContinuationAwareIntentRouter(Component):
    display_name = "03B Continuation 인식 의도 라우터"
    description = "일반 요청만 의도 LLM을 호출하고 continuation은 저장된 Typed IR로 재개합니다."
    inputs = [
        DataInput(name="payload", display_name="요청 페이로드", required=True),
        DataInput(name="metadata_candidates", display_name="메타데이터 후보", required=False),
        MessageTextInput(name="intent_prompt", display_name="의도 분석 프롬프트", required=False),
        ModelInput(name="model", display_name="의도 분석 언어 모델", required=False, real_time_refresh=True),
        SecretStrInput(name="api_key", display_name="의도 모델 API 키", required=False, advanced=True, real_time_refresh=True),
        MessageTextInput(name="mongo_uri", display_name="MongoDB 연결 URI", required=False, advanced=False),
        MessageTextInput(name="mongo_database", display_name="MongoDB 데이터베이스", required=False, value=DEFAULT_DATABASE),
        MessageTextInput(name="collection_name", display_name="결과 컬렉션", required=False, value=DEFAULT_COLLECTION),
    ]
    outputs = [Output(name="text_output", display_name="의도 응답", method="build_response", types=["Message"])]

    # Langflow 출력 함수: 조건을 판정한 뒤 LLM 응답 또는 저장 계획 JSON을 Message로 반환합니다.
    def build_response(self) -> Message:
        text, trace = route_intent_response(
            getattr(self, "payload", None),
            getattr(self, "intent_prompt", ""),
            self._invoke_model,
            None,
            getattr(self, "mongo_uri", ""),
            getattr(self, "mongo_database", ""),
            getattr(self, "collection_name", ""),
            getattr(self, "metadata_candidates", None),
        )
        self.status = trace
        return Message(text=text)

    # 함수 설명: 선택된 Langflow 언어 모델을 최초 의도 분석 요청에서만 실제 호출합니다.
    def _invoke_model(self, prompt: str) -> Any:
        from lfx.base.models.unified_models import get_llm

        llm = get_llm(
            model=getattr(self, "model", None),
            user_id=getattr(self, "user_id", None),
            api_key=getattr(self, "api_key", None),
        )
        if llm is None or not hasattr(llm, "invoke"):
            raise RuntimeError("의도 분석 Language Model이 연결되지 않았습니다.")
        return llm.invoke(prompt)

    # 함수 설명: 모델 선택 변경 시 Langflow 1.9.2 provider별 설정 필드를 갱신합니다.
    def update_build_config(self, build_config: dict, field_value: str, field_name: str | None = None):
        from lfx.base.models.unified_models import (
            apply_provider_variable_config_to_build_config,
            get_language_model_options,
            get_provider_for_model_name,
            update_model_options_in_build_config,
        )

        build_config = update_model_options_in_build_config(
            component=self,
            build_config=build_config,
            cache_key_prefix="continuation_intent_language_model_options",
            get_options_func=get_language_model_options,
            field_name=field_name,
            field_value=field_value,
        )
        current_model = field_value if field_name == "model" else build_config.get("model", {}).get("value")
        provider = ""
        if isinstance(current_model, list) and current_model:
            selected = current_model[0]
            provider = str(selected.get("provider") or "").strip()
            if not provider and selected.get("name"):
                provider = get_provider_for_model_name(str(selected["name"]))
        return apply_provider_variable_config_to_build_config(build_config, provider) if provider else build_config
