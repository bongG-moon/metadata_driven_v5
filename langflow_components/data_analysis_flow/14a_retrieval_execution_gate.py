# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 14A 필수 조회 실행 게이트
# 역할: 필수 source 조회 실패를 판정해 모델 응답 사용과 pandas 실행을 결정론적으로 차단합니다.
# 주요 입력: 조회 어댑터가 만든 페이로드 (payload) · 필수
# 주요 출력: 실행 제어 정보가 추가된 페이로드 (payload_out)
# 처리 흐름: retrieval job은 required=false가 명시된 경우만 선택 항목으로 보고, 그 외 누락·오류는 필수 실패로 처리합니다.
# 유지보수 포인트: 기본 Language Model은 실행되더라도 blocked 상태에서는 그 응답을 사용하지 않고 한 개의 최종 ChatOutput/API 경로를 유지합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


# 주요 함수: 조회 결과와 검증 trace를 비교해 필수 실패는 blocked, 선택 실패는 continue 상태로 기록합니다.
def apply_retrieval_execution_gate(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    payload["trace"] = deepcopy(payload.get("trace")) if isinstance(payload.get("trace"), dict) else {}
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)] if isinstance(plan.get("retrieval_jobs"), list) else []
    source_results = [item for item in payload.get("source_results", []) if isinstance(item, dict)] if isinstance(payload.get("source_results"), list) else []
    result_by_alias = {
        _alias(item): item
        for item in source_results
        if _alias(item)
    }

    critical_failures: list[dict[str, Any]] = []
    optional_failures: list[dict[str, Any]] = []
    validation_failures = _validation_failures(payload)
    critical_failures.extend(validation_failures)

    for job in jobs:
        alias = _alias(job)
        result = result_by_alias.get(alias)
        failure = _source_failure(job, result)
        if not failure:
            continue
        if _is_required(job):
            critical_failures.append(failure)
        else:
            optional_failures.append(failure)

    blocked = bool(critical_failures)
    gate = {
        "stage": "14a_retrieval_execution_gate",
        "status": "blocked" if blocked else "continue",
        "required_source_policy": "required_by_default",
        "critical_failures": critical_failures,
        "optional_failures": optional_failures,
        "pandas_execution_allowed": not blocked,
        "model_response_policy": "ignore" if blocked else "use",
    }
    payload["execution_gate"] = deepcopy(gate)
    trace = payload.setdefault("trace", {})
    trace.setdefault("inspection", {})["retrieval_execution_gate"] = deepcopy(gate)

    if optional_failures:
        warning = {
            "type": "optional_source_retrieval_failed",
            "message": "선택 source 조회에 실패했지만 필수 source가 정상이라 분석을 계속합니다.",
            "sources": [item.get("source_alias") for item in optional_failures if item.get("source_alias")],
        }
        trace.setdefault("warnings", []).append(warning)

    if blocked:
        message = _blocked_message(critical_failures)
        error = {
            "type": "required_source_retrieval_failed",
            "message": message,
            "failures": deepcopy(critical_failures),
        }
        payload["analysis"] = {
            "status": "error",
            "row_count": 0,
            "columns": [],
            "error": error,
            "errors": [message],
            "repairable_errors": [],
            "step_outputs": [],
            "function_case_results": [],
        }
        payload["data"] = {"columns": [], "rows": [], "row_count": 0, "data_ref": ""}
        payload["answer_message"] = message
        trace.setdefault("errors", []).append(error)
    return payload


# 함수 설명: `_validation_failures()`는 job validation과 trusted catalog hydration의 치명 오류를 실행 차단 사유로 바꿉니다.
def _validation_failures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    retrieval = inspection.get("data_retrieval") if isinstance(inspection.get("data_retrieval"), dict) else {}
    validation = retrieval.get("job_validation") if isinstance(retrieval.get("job_validation"), dict) else {}
    hydration = inspection.get("catalog_hydration") if isinstance(inspection.get("catalog_hydration"), dict) else {}
    failures: list[dict[str, Any]] = []
    if _positive_int(validation.get("error_count")):
        failures.append(
            {
                "type": "retrieval_job_validation_failed",
                "message": "데이터 조회 작업 검증에 실패했습니다.",
                "error_count": int(validation.get("error_count") or 0),
            }
        )
    if str(hydration.get("status") or "").strip().lower() == "error":
        failures.append(
            {
                "type": "catalog_hydration_failed",
                "message": "신뢰 카탈로그에서 필수 조회 설정을 구성하지 못했습니다.",
            }
        )
    return failures


# 함수 설명: `_source_failure()`는 job에 대응하는 source result의 누락·명시 오류를 표준 실패 정보로 만듭니다.
def _source_failure(job: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    alias = _alias(job)
    dataset_key = str(job.get("dataset_key") or "").strip()
    if result is None:
        return {
            "type": "required_source_result_missing" if _is_required(job) else "optional_source_result_missing",
            "message": f"source 결과가 없습니다: {alias or dataset_key}",
            "source_alias": alias,
            "dataset_key": dataset_key,
        }
    status = str(result.get("status") or "ok").strip().lower()
    errors = [item for item in result.get("errors", []) if isinstance(item, dict)] if isinstance(result.get("errors"), list) else []
    if result.get("success") is False or status in {"error", "failed", "failure", "invalid", "skipped"} or errors:
        return {
            "type": "source_retrieval_failed",
            "message": str(result.get("error_message") or _first_error_message(errors) or f"source 조회 실패: {alias or dataset_key}"),
            "source_alias": alias,
            "dataset_key": dataset_key,
            "source_type": result.get("source_type") or job.get("source_type"),
            "errors": deepcopy(errors),
        }
    return {}


# 함수 설명: `_is_required()`는 required=false가 명시된 job만 선택 source로 취급합니다.
def _is_required(job: dict[str, Any]) -> bool:
    value = job.get("required", True)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"false", "0", "no", "off", "optional", "선택"}


# 함수 설명: `_blocked_message()`는 사용자와 운영자가 실패 source를 바로 확인할 수 있는 결정론적 메시지를 만듭니다.
def _blocked_message(failures: list[dict[str, Any]]) -> str:
    aliases = []
    for item in failures:
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    suffix = f" 실패 source: {', '.join(aliases)}." if aliases else ""
    return "필수 데이터 조회에 실패하여 pandas 분석을 실행하지 않았고 모델 응답도 사용하지 않았습니다." + suffix


# 함수 설명: `_first_error_message()`는 source errors 배열에서 첫 번째 사람이 읽을 수 있는 메시지를 반환합니다.
def _first_error_message(errors: list[dict[str, Any]]) -> str:
    for item in errors:
        message = str(item.get("message") or "").strip()
        if message:
            return message
    return ""


# 함수 설명: `_alias()`는 source_alias가 없으면 dataset_key를 사용해 job/result identity를 맞춥니다.
def _alias(value: dict[str, Any]) -> str:
    return str(value.get("source_alias") or value.get("dataset_key") or "").strip()


# 함수 설명: `_positive_int()`는 검증 오류 개수가 0보다 큰지 안전하게 확인합니다.
def _positive_int(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except Exception:
        return False


# 함수 설명: `_payload()`는 대용량 runtime_sources 행은 공유하고 변경하는 최상위 key만 분리하도록 얕은 복사합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return dict(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: 단일 output을 사용해 stop/merge 없이 downstream control payload를 전달합니다.
class RetrievalExecutionGate(Component):
    display_name = "14A 필수 조회 실행 게이트"
    description = "필수 source 조회 실패 시 모델 응답 사용과 pandas 실행을 차단하는 control payload를 만듭니다."
    inputs = [DataInput(name="payload", display_name="조회 페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="실행 제어 페이로드", method="build_payload")]

    # Langflow 출력 함수: 필수/선택 source 상태를 평가한 단일 페이로드를 다음 선형 경로로 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=apply_retrieval_execution_gate(getattr(self, "payload", None)))
