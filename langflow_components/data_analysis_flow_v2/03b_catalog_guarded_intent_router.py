# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 03B Table Catalog 검증 의도 라우터
# 역할: 등록된 실행 데이터셋이 확인된 요청에만 의도 분석 LLM을 호출합니다.
# 주요 입력: 요청 페이로드, Table Catalog 후보, 의도 분석 프롬프트, 언어 모델
# 주요 출력: 모델 응답 또는 메타데이터 연결 오류를 담은 Message
# 처리 흐름: Catalog 상태 확인 → 연결/등록 오류 차단 → 정상일 때만 모델 호출입니다.
# =============================================================================
"""Catalog-gated initial intent routing for the standalone V2 analysis flow.

The intent model is allowed to plan only when the current request has at least
one registered Table Catalog dataset.  This keeps a metadata connection
failure from turning into an invented dataset, source alias, or column plan.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Callable

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, ModelInput, Output, SecretStrInput
from lfx.schema.message import Message


# 함수 설명: Table Catalog가 유효한 경우에만 의도 모델을 호출하고, 오류면 모델 호출 없이 차단 계획을 반환합니다.
def route_intent_response(
    payload_value: Any,
    intent_prompt: Any = "",
    model_invoker: Callable[[str], Any] | None = None,
    metadata_candidates_value: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Invoke the intent model only after Table Catalog availability is proven."""

    error = table_catalog_metadata_error(metadata_candidates_value)
    if error:
        envelope = blocked_intent_envelope(error)
        return _json_text(envelope), {
            "stage": "03B_catalog_guarded_intent_router",
            "mode": "metadata_blocked",
            "model_called": False,
            "intent_llm_skipped": True,
            "error": deepcopy(error),
        }

    prompt = _text(intent_prompt)
    if not prompt:
        raise ValueError("Intent analysis prompt is empty.")
    if model_invoker is None:
        raise RuntimeError("Intent analysis language model is not connected.")
    response = model_invoker(prompt)
    return _text(response), {
        "stage": "03B_catalog_guarded_intent_router",
        "mode": "catalog_verified",
        "model_called": True,
        "intent_llm_skipped": False,
        "prompt_chars": len(prompt),
    }


# 함수 설명: 후보 메타데이터의 Table Catalog 로드 상태와 등록된 데이터셋 존재 여부를 검증합니다.
def table_catalog_metadata_error(metadata_candidates_value: Any) -> dict[str, Any]:
    """Return a deterministic error unless registered Table Catalog data exists.

    The guard deliberately depends on the loader output rather than question
    keywords.  A domain description alone is never evidence that a dataset is
    registered or executable.
    """

    envelope = _data(metadata_candidates_value)
    candidates = envelope.get("metadata_candidates")
    if not isinstance(candidates, dict):
        candidates = envelope if isinstance(envelope, dict) else {}
    metadata_load = envelope.get("metadata_load")
    if not isinstance(metadata_load, dict):
        metadata_load = candidates.get("metadata_load") if isinstance(candidates, dict) else {}
    metadata_load = metadata_load if isinstance(metadata_load, dict) else {}
    loads = metadata_load.get("loads") if isinstance(metadata_load.get("loads"), dict) else {}
    table_load = loads.get("table_catalog_items") if isinstance(loads.get("table_catalog_items"), dict) else {}
    table_status = str(table_load.get("status") or "").strip().lower()
    overall_status = str(metadata_load.get("status") or "").strip().lower()
    registry = envelope.get("table_catalog_registry") if isinstance(envelope, dict) else {}
    registry_items = registry.get("items") if isinstance(registry, dict) else registry
    table_items = registry_items if isinstance(registry_items, list) else []
    if not table_items:
        table_items = candidates.get("table_catalog_items") if isinstance(candidates, dict) else []
    registered_keys = [
        str(item.get("dataset_key") or _payload(item).get("dataset_key") or "").strip()
        for item in table_items
        if isinstance(item, dict)
    ] if isinstance(table_items, list) else []
    registered_keys = list(dict.fromkeys(key for key in registered_keys if key))

    failed_loads = _failed_metadata_loads(loads)
    if failed_loads or overall_status in {"error", "failed", "failure", "invalid"}:
        return _metadata_unavailable_error(
            failed_loads or [{"metadata_kind": "metadata", "status": overall_status or "error"}],
            registered_dataset_count=len(registered_keys),
        )
    if not registered_keys:
        return {
            "type": "table_catalog_metadata_unavailable",
            "reason": "no_active_table_catalog",
            "message": "MongoDB 메타데이터 연결은 되었지만 활성 Table Catalog에 등록된 데이터셋이 없습니다. 데이터셋 등록 상태와 collection의 status=active 설정을 확인해 주세요.",
            "table_catalog_load_status": table_status or overall_status or "not_available",
            "registered_dataset_count": 0,
        }
    return {}


# 함수 설명: 여러 메타데이터 로더 중 실패한 항목을 자격 증명 없이 짧은 진단 정보로 추립니다.
def _failed_metadata_loads(loads: dict[str, Any]) -> list[dict[str, str]]:
    """Expose a bounded, credential-safe reason for each failed metadata loader."""

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
        detail = _safe_error_detail(first_error.get("message") or raw_load.get("message") or "")
        failed.append(
            {
                "metadata_kind": str(metadata_kind or "metadata"),
                "status": status,
                "error_type": error_type,
                "detail": detail,
            }
        )
    return failed


# 함수 설명: 메타데이터 연결 또는 로드 실패를 사용자가 조치할 수 있는 단일 오류 계약으로 만듭니다.
def _metadata_unavailable_error(
    failed_loads: list[dict[str, str]],
    *,
    registered_dataset_count: int,
) -> dict[str, Any]:
    """Build a direct response without exposing a MongoDB credential or URI."""

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


# 함수 설명: 실행 데이터셋 권한의 기준인 Table Catalog 실패를 우선 표시할 원인으로 선택합니다.
def _primary_metadata_failure(failed_loads: list[dict[str, str]]) -> dict[str, str]:
    """Prefer the Table Catalog cause because it authorizes executable datasets."""

    return next(
        (item for item in failed_loads if str(item.get("metadata_kind") or "") == "table_catalog_items"),
        failed_loads[0] if failed_loads else {},
    )


# 함수 설명: 공급자 오류의 진단 가치는 유지하면서 URI 계정 정보와 과도한 길이를 제거합니다.
def _safe_error_detail(value: Any) -> str:
    """Keep a useful provider error while redacting credentials and bounding output."""

    text = " ".join(str(value or "").split())
    text = re.sub(r"mongodb(?:\+srv)?://[^\s@/]+@", "mongodb://***@", text, flags=re.IGNORECASE)
    return text[:500] if text else "상세 오류 정보가 없습니다."


# 함수 설명: 메타데이터가 없을 때 임의 데이터셋을 만들지 않는 최소 차단 의도 계약을 생성합니다.
def blocked_intent_envelope(error: dict[str, Any]) -> dict[str, Any]:
    """Build the same minimal typed plan for every metadata-unavailable case."""

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
            "inspection": {
                "intent_router": {
                    "stage": "03B_catalog_guarded_intent_router",
                    "model_called": False,
                    "reason": "table_catalog_metadata_unavailable",
                }
            },
        },
    }


# 함수 설명: Langflow Data 또는 dict 입력을 복사 가능한 일반 dict로 정규화합니다.
def _data(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: 메타데이터 항목에서 선택적으로 중첩된 payload dict만 안전하게 읽습니다.
def _payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload")
    return payload if isinstance(payload, dict) else {}


# 함수 설명: Message·dict·문자열 모델 응답을 후속 JSON 처리용 텍스트로 바꿉니다.
def _text(value: Any) -> str:
    text = getattr(value, "text", value)
    if isinstance(text, dict):
        return json.dumps(text, ensure_ascii=False, separators=(",", ":"))
    return str(text or "").strip()


# 함수 설명: 차단 의도 계약을 한국어를 보존하는 compact JSON 문자열로 직렬화합니다.
def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# Langflow 컴포넌트 클래스: Table Catalog 확인 뒤에만 의도 분석 모델을 호출하는 안전 라우터입니다.
class CatalogGuardedIntentRouter(Component):
    display_name = "03B Catalog 검증 Intent Router"
    description = "Table Catalog가 확인된 경우에만 Intent LLM을 호출하고, 메타데이터 연결 실패 시 추측 계획 없이 즉시 중단합니다."
    inputs = [
        DataInput(name="payload", display_name="요청 페이로드", required=True),
        DataInput(name="metadata_candidates", display_name="메타데이터 후보", required=False),
        MessageTextInput(name="intent_prompt", display_name="의도 분석 프롬프트", required=False),
        ModelInput(name="model", display_name="의도 분석 언어 모델", required=False, real_time_refresh=True),
        SecretStrInput(name="api_key", display_name="의도 분석 모델 API Key", required=False, advanced=True, real_time_refresh=True),
    ]
    outputs = [Output(name="text_output", display_name="의도 분석 응답", method="build_response", types=["Message"])]

    # 함수 설명: 현재 노드 입력으로 라우팅을 수행하고 모델 응답 또는 차단 계약을 Message로 반환합니다.
    def build_response(self) -> Message:
        text, trace = route_intent_response(
            getattr(self, "payload", None),
            getattr(self, "intent_prompt", ""),
            self._invoke_model,
            getattr(self, "metadata_candidates", None),
        )
        self.status = trace
        return Message(text=text)

    # 함수 설명: Langflow에서 선택된 언어 모델을 찾아 의도 분석 프롬프트를 한 번 호출합니다.
    def _invoke_model(self, prompt: str) -> Any:
        from lfx.base.models.unified_models import get_llm

        llm = get_llm(
            model=getattr(self, "model", None),
            user_id=getattr(self, "user_id", None),
            api_key=getattr(self, "api_key", None),
        )
        if llm is None or not hasattr(llm, "invoke"):
            raise RuntimeError("Intent analysis language model is not connected.")
        return llm.invoke(prompt)

    # 함수 설명: 화면에서 모델 제공자를 바꿀 때 해당 제공자의 설정 항목을 동적으로 반영합니다.
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
            cache_key_prefix="catalog_guarded_intent_language_model_options",
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
