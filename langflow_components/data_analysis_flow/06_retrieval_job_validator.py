# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 06 데이터 조회 작업 검증기
# 역할: 의도 계획의 데이터 조회 작업 구조를 검증하고 실행 가능한 작업만 남깁니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 조회 작업의 데이터셋·source type·필수 설정을 검사하고 실행 가능한 작업과 검증 오류를 분리합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

RUNTIME_BUFFER_KEYS = {
    "runtime_sources",
    "_runtime_rows_by_alias",
    "_full_result_rows",
    "_runtime_result_rows",
}

ALLOWED_SOURCE_TYPES = {"dummy", "oracle", "h_api", "datalake", "goodocs"}


# 주요 함수: 조회 작업별 필수 필드와 허용 source type을 검사합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def validate_retrieval_payload(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    valid_jobs = []
    plan_validation_items = [
        deepcopy(item)
        for item in plan.get("validation_errors", [])
        if isinstance(item, dict)
    ] if isinstance(plan.get("validation_errors"), list) else []
    explicit_warnings = [
        {**deepcopy(item), "severity": "warning"}
        for item in plan.get("validation_warnings", [])
        if isinstance(item, dict)
    ] if isinstance(plan.get("validation_warnings"), list) else []
    intent_warnings = [
        item for item in plan_validation_items if _validation_severity(item) == "warning"
    ]
    intent_warnings.extend(explicit_warnings)
    intent_errors = [
        item for item in plan_validation_items if _validation_severity(item) != "warning"
    ]
    errors = list(intent_errors)
    warnings = list(intent_warnings)
    if not intent_errors:
        aliases = [
            str(job.get("source_alias") or "").strip()
            for job in jobs
            if isinstance(job, dict)
        ]
        duplicate_aliases = sorted(
            {
                alias
                for alias in aliases
                if alias and aliases.count(alias) > 1
            }
        )
        if duplicate_aliases:
            errors.append(
                _error(
                    "duplicate_source_alias",
                    "source_alias must be unique within retrieval_jobs",
                    source_aliases=duplicate_aliases,
                )
            )
        for index, job in enumerate(jobs if not duplicate_aliases else []):
            if not isinstance(job, dict):
                errors.append(_error("invalid_retrieval_job", "retrieval job must be an object", index=index))
                continue
            job_errors = []
            for field in ("dataset_key", "source_alias", "source_type"):
                if not job.get(field):
                    job_errors.append(_error("missing_retrieval_job_field", f"{field} is required", field=field, index=index))
            if job.get("source_type") and job.get("source_type") not in ALLOWED_SOURCE_TYPES:
                job_errors.append(_error("unsupported_source_type", f"unsupported source_type: {job.get('source_type')}", index=index))
            if job_errors:
                errors.extend(job_errors)
                continue
            next_job = deepcopy(job)
            next_job.setdefault("job_id", f"job_{index + 1}")
            valid_jobs.append(next_job)
    next_payload = payload
    next_payload.setdefault("intent_plan", {})["retrieval_jobs"] = valid_jobs
    trace = next_payload.setdefault("trace", {})
    trace.setdefault("errors", []).extend(errors)
    trace.setdefault("warnings", []).extend(warnings)
    job_validation = {
        "input_job_count": len(jobs),
        "valid_job_count": len(valid_jobs),
        "error_count": len(errors),
        "intent_error_count": len(intent_errors),
        "errors": deepcopy(errors),
    }
    if warnings:
        job_validation.update(
            {
                "warning_count": len(warnings),
                "intent_warning_count": len(intent_warnings),
                "warnings": deepcopy(warnings),
            }
        )
    trace.setdefault("inspection", {}).setdefault("data_retrieval", {})["job_validation"] = job_validation
    return next_payload


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return {}
    payload = {
        key: deepcopy(item)
        for key, item in data.items()
        if key not in RUNTIME_BUFFER_KEYS
    }
    for key in RUNTIME_BUFFER_KEYS:
        if key in data:
            payload[key] = data[key]
    return payload


# 함수 설명: `_error()`는 조회 작업 검증 오류를 dataset·field·message가 포함된 표준 오류 dict로 만듭니다.
def _error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    error = {"type": error_type, "message": message}
    error.update(extra)
    return error


# 함수 설명: 명시적으로 recoverable/warning으로 분류된 계획 검증 항목만 비차단 경고로 인식합니다. 분류가 없으면 기존처럼 차단합니다.
def _validation_severity(item: dict[str, Any]) -> str:
    severity = str(item.get("severity") or "").strip().lower()
    if severity in {"warning", "warn", "recoverable"}:
        return "warning"
    if item.get("blocking") is False or item.get("recoverable") is True:
        return "warning"
    return "blocking"


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class RetrievalJobValidator(Component):
    display_name = "06 데이터 조회 작업 검증기"
    description = "의도 계획의 데이터 조회 작업 구조를 검증하고 실행 가능한 작업만 남깁니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=validate_retrieval_payload(getattr(self, "payload", None)))
