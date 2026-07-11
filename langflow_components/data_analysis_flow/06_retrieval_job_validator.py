from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

ALLOWED_SOURCE_TYPES = {"dummy", "oracle", "h_api", "datalake", "goodocs"}


def validate_retrieval_payload(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    valid_jobs = []
    errors = []
    for index, job in enumerate(jobs):
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
    next_payload = deepcopy(payload)
    next_payload.setdefault("intent_plan", {})["retrieval_jobs"] = valid_jobs
    trace = next_payload.setdefault("trace", {})
    trace.setdefault("errors", []).extend(errors)
    trace.setdefault("inspection", {}).setdefault("data_retrieval", {})["job_validation"] = {
        "input_job_count": len(jobs),
        "valid_job_count": len(valid_jobs),
        "error_count": len(errors),
    }
    return next_payload


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


def _error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    error = {"type": error_type, "message": message}
    error.update(extra)
    return error


class RetrievalJobValidator(Component):
    display_name = "06 데이터 조회 작업 검증기"
    description = "의도 계획의 데이터 조회 작업 구조를 검증하고 실행 가능한 작업만 남깁니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=validate_retrieval_payload(getattr(self, "payload", None)))
