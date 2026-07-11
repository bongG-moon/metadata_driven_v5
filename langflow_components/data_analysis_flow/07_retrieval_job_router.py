from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

SOURCE_TYPES = ("dummy", "oracle", "h_api", "datalake", "goodocs")


def route_retrieval_jobs(payload_value: Any, target_source_type: str) -> dict[str, Any]:
    payload = _payload(payload_value)
    jobs = payload.get("intent_plan", {}).get("retrieval_jobs", [])
    jobs = jobs if isinstance(jobs, list) else []
    retrieval_mode = _retrieval_mode(payload)
    live_enabled = retrieval_mode == "live"
    if not live_enabled:
        selected = [deepcopy(job) for job in jobs if isinstance(job, dict)] if target_source_type == "dummy" else []
    else:
        selected = [deepcopy(job) for job in jobs if isinstance(job, dict) and job.get("source_type") == target_source_type]
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    return {
        "retrieval_job_bundle": {
            "source_type": target_source_type,
            "jobs": selected,
            "retrieval_mode": retrieval_mode,
            "live_source_retrieval": live_enabled,
        },
        "request_context": {
            "session_id": request.get("session_id", ""),
            "reference_date": request.get("reference_date", ""),
        },
        "routing_trace": {
            "input_job_count": len(jobs),
            "selected_job_count": len(selected),
            "source_type": target_source_type,
            "retrieval_mode": retrieval_mode,
        },
    }


def _retrieval_mode(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    mode = str(request.get("retrieval_mode") or "").strip().lower()
    return "live" if mode in {"live", "actual", "real", "실제", "true", "on", "1", "yes"} else "dummy"


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


class RetrievalJobRouter(Component):
    display_name = "07 데이터 조회 작업 라우터"
    description = "main payload를 복사하지 않고 선택된 job bundle만 소스 유형별 분기로 전달합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [
        Output(name="dummy_jobs", display_name="더미 작업", method="dummy_jobs_out", group_outputs=True),
        Output(name="oracle_jobs", display_name="Oracle 작업", method="oracle_jobs_out", group_outputs=True),
        Output(name="h_api_jobs", display_name="H-API 작업", method="h_api_jobs_out", group_outputs=True),
        Output(name="datalake_jobs", display_name="데이터레이크 작업", method="datalake_jobs_out", group_outputs=True),
        Output(name="goodocs_jobs", display_name="Goodocs 작업", method="goodocs_jobs_out", group_outputs=True),
    ]

    def dummy_jobs_out(self) -> Data:
        return Data(data=route_retrieval_jobs(getattr(self, "payload", None), "dummy"))

    def oracle_jobs_out(self) -> Data:
        return Data(data=route_retrieval_jobs(getattr(self, "payload", None), "oracle"))

    def h_api_jobs_out(self) -> Data:
        return Data(data=route_retrieval_jobs(getattr(self, "payload", None), "h_api"))

    def datalake_jobs_out(self) -> Data:
        return Data(data=route_retrieval_jobs(getattr(self, "payload", None), "datalake"))

    def goodocs_jobs_out(self) -> Data:
        return Data(data=route_retrieval_jobs(getattr(self, "payload", None), "goodocs"))
