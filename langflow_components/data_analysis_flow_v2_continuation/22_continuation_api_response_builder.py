# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 22 API 응답 생성기
# 역할: 최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다.
# 주요 입력: 페이로드 (payload) · 필수, 채팅 표시 메시지 (display_message)
# 주요 출력: API 응답 (api_response)
# 처리 흐름: 웹/API 소비자가 필요한 결과만 남기고 runtime source와 대용량 내부 필드를 제거한 응답 envelope을 만듭니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data

MAX_PUBLIC_CONTINUATION_BYTES = 4 * 1024

# 주요 함수: 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_api_response(payload_value: Any, display_message_value: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    answer_message = str(payload.get("answer_message") or "")
    display_message = _text(display_message_value) or answer_message
    status, stage_status = _pipeline_status(payload)
    continuation = _build_continuation(payload)
    if str(continuation.get("status") or "") == "followup_unavailable":
        status = "partial"
        stage_status = {**stage_status, "overall": "partial", "continuation": "followup_unavailable"}
        display_message = (
            "1차 분석은 완료했지만 저장된 결과를 안전하게 다시 불러올 수 없어 "
            "후속 조회를 실행하지 않습니다."
        )
    return {
        "response_type": "data_analysis",
        "status": status,
        "stage_status": stage_status,
        "message": display_message,
        "data_mode": _data_mode(payload),
        "answer_sections": payload.get("answer_sections", {}),
        "request": payload.get("request", {}),
        "intent_plan": _public_intent_plan(payload.get("intent_plan")),
        "analysis": payload.get("analysis", {}),
        "data": payload.get("data", {}),
        "data_refs": payload.get("data_refs", []),
        "download_manifest": payload.get("download_manifest", []),
        "state": payload.get("state", {}),
        "trace": payload.get("trace", {}),
        "continuation": continuation,
    }


# 함수 설명: 공개 intent 진단 정보는 유지하되 서버 저장형 dependent stage IR은 작은 실행 요약으로 치환합니다.
def _public_intent_plan(value: Any) -> dict[str, Any]:
    plan = deepcopy(value) if isinstance(value, dict) else {}
    dependent = (
        plan.get("dependent_retrieval_plan")
        if isinstance(plan.get("dependent_retrieval_plan"), dict)
        else None
    )
    if not isinstance(dependent, dict):
        return plan
    runtime = dependent.get("runtime") if isinstance(dependent.get("runtime"), dict) else {}
    plan["dependent_retrieval_plan"] = {
        "version": str(dependent.get("version") or ""),
        "plan_id": str(dependent.get("plan_id") or ""),
        "plan_hash": str(dependent.get("plan_hash") or ""),
        "max_stages": _safe_int(dependent.get("max_stages"), 2),
        "status": str(runtime.get("status") or ""),
        "active_stage_id": str(runtime.get("current_stage_id") or ""),
        "active_stage_index": _safe_int(runtime.get("active_stage_index"), 0),
        "next_stage_id": str(runtime.get("next_stage_id") or ""),
        "intent_llm_skipped": bool(runtime.get("intent_llm_skipped")),
    }
    return plan


# 주요 함수: 저장 IR을 노출하지 않는 4 KiB 이하 공개 continuation 계약을 구성합니다.
def _build_continuation(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    dependent = plan.get("dependent_retrieval_plan") if isinstance(plan.get("dependent_retrieval_plan"), dict) else {}
    runtime = dependent.get("runtime") if isinstance(dependent.get("runtime"), dict) else {}
    if not dependent or not runtime:
        return {"status": "not_applicable"}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    analysis_ok = str(analysis.get("status") or "").strip().lower() in {"ok", "success"}
    row_count = _safe_int(data.get("row_count"), 0)
    runtime_status = str(runtime.get("status") or "").strip().lower()
    status = runtime_status if analysis_ok else "blocked"
    if runtime_status == "pending" and analysis_ok and row_count <= 0:
        status = "empty_complete"
    active_index = _safe_int(runtime.get("active_stage_index"), 0)
    stages = [item for item in dependent.get("stages", []) if isinstance(item, dict)]
    next_index = active_index + 1
    result_ref = _result_ref(payload)
    base = {
        "status": status,
        "version": str(dependent.get("version") or ""),
        "plan_id": str(dependent.get("plan_id") or ""),
        "plan_hash": str(dependent.get("plan_hash") or ""),
        # Public summary is 1-based for operator/UI progress (1 of 2, 2 of 2).
        # The resume contract below keeps explicit current/next indexes 0-based.
        "stage_index": active_index + 1,
        "current_stage_index": active_index,
        "max_stages": _safe_int(dependent.get("max_stages"), 2),
        "current_stage_id": str(runtime.get("current_stage_id") or ""),
        "next_stage_id": str(runtime.get("next_stage_id") or ""),
        "row_count": row_count,
        "result_ref": result_ref,
        "intent_llm_skipped": bool(runtime.get("intent_llm_skipped")),
    }
    if status != "pending" or next_index >= len(stages):
        return base
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    session_id = str(request.get("session_id") or "").strip()
    store = (
        payload.get("trace", {}).get("inspection", {}).get("result_store", {})
        if isinstance(payload.get("trace"), dict)
        and isinstance(payload.get("trace", {}).get("inspection"), dict)
        and isinstance(payload.get("trace", {}).get("inspection", {}).get("result_store"), dict)
        else {}
    )
    store_status = str(store.get("status") or "").strip().lower()
    unavailable_reason = ""
    if not session_id:
        unavailable_reason = "continuation_session_missing"
    elif not result_ref:
        unavailable_reason = "continuation_result_ref_missing"
    elif store_status not in {"ok", "success", "complete", "completed"}:
        unavailable_reason = "continuation_result_store_unavailable"
    if unavailable_reason:
        base.update(
            {
                "status": "followup_unavailable",
                "error": {
                    "type": unavailable_reason,
                    "message": "저장된 1차 결과를 안전하게 다시 불러올 수 없어 후속 단계를 실행하지 않습니다.",
                },
            }
        )
        return base
    continuation_ref = f"continuation:{dependent.get('plan_id')}:{dependent.get('plan_hash')}"
    # The client receives only an authenticated lookup key and the binding
    # summary required by the router.  The complete Typed IR remains in the
    # result store and is loaded again by the resume-only intent boundary.
    contract = {
        "version": base["version"],
        "plan_id": base["plan_id"],
        "plan_hash": base["plan_hash"],
        "max_stages": base["max_stages"],
        "current_stage_index": active_index,
        "next_stage_index": next_index,
        "continuation_ref": continuation_ref,
        "session_id": session_id,
        "input_bindings": deepcopy(stages[next_index].get("input_bindings", [])),
    }
    contract_bytes = len(
        json.dumps(contract, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )
    if contract_bytes > MAX_PUBLIC_CONTINUATION_BYTES:
        base.update(
            {
                "status": "blocked",
                "error": {
                    "type": "continuation_contract_too_large",
                    "message": "공개 continuation 계약이 4 KiB 제한을 초과했습니다.",
                    "contract_bytes": contract_bytes,
                },
            }
        )
        return base
    base.update(
        {
            "continuation_ref": continuation_ref,
            "continuation_contract": contract,
            "input_bindings": deepcopy(contract["input_bindings"]),
            "contract_bytes": contract_bytes,
        }
    )
    return base


# 함수 설명: data 또는 data_refs에서 현재 분석 결과 저장 참조 ID를 찾습니다.
def _result_ref(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidate = data.get("data_ref")
    if isinstance(candidate, dict):
        value = str(candidate.get("ref_id") or candidate.get("data_ref") or candidate.get("_id") or "").strip()
        if value:
            return value
    elif str(candidate or "").strip():
        return str(candidate).strip()
    for item in payload.get("data_refs", []) if isinstance(payload.get("data_refs"), list) else []:
        if isinstance(item, dict) and str(item.get("kind") or "") == "analysis_result":
            return str(item.get("ref_id") or "").strip()
    return ""


# 함수 설명: 공개 응답 숫자 값을 예외 없이 정수로 변환합니다.
def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 함수 설명: `_pipeline_status()`는 조회와 pandas 분석 상태를 함께 평가해 ok·partial·error를 결정합니다.
def _pipeline_status(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    analysis_status = _normalize_status(analysis.get("status"), default="error")
    retrieval_status = _retrieval_status(payload)

    if analysis_status == "error" or retrieval_status == "error":
        overall = "error"
    elif analysis_status == "partial" or retrieval_status == "partial":
        overall = "partial"
    else:
        overall = "ok"
    return overall, {"overall": overall, "retrieval": retrieval_status, "analysis": analysis_status}


# 함수 설명: `_retrieval_status()`는 필수 조회 작업별 성공·실패와 검증 오류를 집계합니다.
def _retrieval_status(payload: dict[str, Any]) -> str:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    retrieval_inspection = inspection.get("data_retrieval") if isinstance(inspection.get("data_retrieval"), dict) else {}
    validation = retrieval_inspection.get("job_validation") if isinstance(retrieval_inspection.get("job_validation"), dict) else {}
    hydration = inspection.get("catalog_hydration") if isinstance(inspection.get("catalog_hydration"), dict) else {}
    if _positive_int(validation.get("error_count")) or _normalize_status(hydration.get("status"), default="ok") == "error":
        return "error"
    inspection_status = _normalize_status(retrieval_inspection.get("status"), default="ok")

    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = [job for job in plan.get("retrieval_jobs", []) if isinstance(job, dict)] if isinstance(plan.get("retrieval_jobs"), list) else []
    source_results = [item for item in payload.get("source_results", []) if isinstance(item, dict)] if isinstance(payload.get("source_results"), list) else []
    # 조회 작업이 없는 직접/재사용 응답은 분석 단계 상태만으로 판단합니다.
    if not jobs and not source_results and not retrieval_inspection:
        return "ok"

    result_by_alias = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in source_results
        if str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    }
    statuses: list[tuple[str, bool]] = []
    for job in jobs:
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if not alias:
            continue
        result = result_by_alias.get(alias)
        statuses.append(("error" if result is None else _source_status(result), _job_required(job)))
    if not statuses:
        statuses = [(_source_status(item), True) for item in source_results]

    if not statuses:
        return "error" if inspection_status == "error" else inspection_status
    required_failed = any(status == "error" and required for status, required in statuses)
    optional_failed = any(status == "error" and not required for status, required in statuses)
    if required_failed:
        return "error"
    if optional_failed:
        return "partial"
    if inspection_status == "error":
        return "error"
    return "partial" if any(status == "partial" for status, _ in statuses) else "ok"


# 함수 설명: `_source_status()`는 개별 source result의 status·success·errors를 하나의 상태로 정규화합니다.
def _source_status(source: dict[str, Any]) -> str:
    if source.get("success") is False or source.get("errors"):
        return "error"
    return _normalize_status(source.get("status"), default="ok")


# 함수 설명: `_job_required()`는 required=false가 명시된 source만 선택 항목으로 판정합니다.
def _job_required(job: dict[str, Any]) -> bool:
    value = job.get("required", True)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"false", "0", "no", "off", "optional", "선택"}


# 함수 설명: `_normalize_status()`는 다양한 내부 상태 문자열을 외부 계약의 ok·partial·error로 제한합니다.
def _normalize_status(value: Any, default: str = "ok") -> str:
    text = str(value or "").strip().lower()
    if text in {"error", "failed", "failure", "invalid"}:
        return "error"
    if text in {"partial", "warning", "degraded"}:
        return "partial"
    if text in {"ok", "success", "completed", "complete"}:
        return "ok"
    return default


# 함수 설명: `_positive_int()`는 오류 개수처럼 0보다 큰 값인지 안전하게 확인합니다.
def _positive_int(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except Exception:
        return False


# 함수 설명: `_data_mode()`는 payload의 retrieval_mode와 source 결과를 확인해 dummy/live 응답 표시 모드를 결정합니다.
def _data_mode(payload: dict[str, Any]) -> str:
    source_results = payload.get("source_results") if isinstance(payload.get("source_results"), list) else []
    for source in source_results:
        if not isinstance(source, dict):
            continue
        execution = source.get("source_execution") if isinstance(source.get("source_execution"), dict) else {}
        if (
            execution.get("used_dummy_data") is True
            or source.get("dummy") is True
            or str(source.get("source_type") or "").strip().lower() == "dummy"
        ):
            return "dummy"
    return "live"


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return {}
    excluded = {"runtime_sources", "_runtime_rows_by_alias", "_full_result_rows", "_runtime_result_rows"}
    return {key: deepcopy(item) for key, item in data.items() if key not in excluded}


# 함수 설명: `_text()`는 Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", value)
    if text is None:
        return ""
    return str(text).strip()


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class ApiResponseBuilder(Component):
    display_name = "22 API 응답 생성기"
    description = "최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다."

    # 함수 설명: 이 컴포넌트 자체가 Flow의 구조화 최종 출력임을 Langflow에 알립니다.
    # 코드를 저장하면 Langflow가 graph output 메타데이터를 자동 생성하므로 Flow JSON을 직접 수정할 필요가 없습니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
    ]
    outputs = [Output(name="api_response", display_name="API 응답", method="build_payload")]

    # Langflow 출력 함수: 'API 응답 (api_response)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_api_response(getattr(self, "payload", None), getattr(self, "display_message", "")))
