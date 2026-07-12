# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 07 데이터 조회 작업 라우터
# 역할: main payload를 복사하지 않고 선택된 job bundle만 소스 유형별 분기로 전달합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 더미 작업 (dummy_jobs), Oracle 작업 (oracle_jobs), H-API 작업 (h_api_jobs), 데이터레이크 작업 (datalake_jobs), Goodocs 작업
#        (goodocs_jobs)
# 처리 흐름: 단일 retrieval_mode를 적용해 작업을 dummy·Oracle·H API·Datalake·Goodocs 실행 포트로 나눕니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

SOURCE_TYPES = ("dummy", "oracle", "h_api", "datalake", "goodocs")


# 주요 함수: 검증된 조회 작업을 실행 모드와 source type별 최소 bundle로 나눕니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def route_retrieval_jobs(payload_value: Any, target_source_type: str) -> dict[str, Any]:
    payload = _payload(payload_value)
    return _route_retrieval_jobs_from_payload(payload, target_source_type)


# 함수 설명: `_route_retrieval_jobs_from_payload()`는 이미 분리된 payload에서 선택 source의 최소 job bundle만 만듭니다.
def _route_retrieval_jobs_from_payload(payload: dict[str, Any], target_source_type: str) -> dict[str, Any]:
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


# 함수 설명: `_retrieval_mode()`는 payload와 작업 설정에서 실제 dummy/live 조회 모드를 결정합니다.
def _retrieval_mode(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    mode = str(request.get("retrieval_mode") or "").strip().lower()
    return "live" if mode in {"live", "actual", "real", "실제", "true", "on", "1", "yes"} else "dummy"


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
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

    # 함수 설명: `_routes_once()`는 다섯 source output이 같은 main payload를 다섯 번 deepcopy하지 않도록 한 번만 분기합니다.
    def _routes_once(self) -> dict[str, dict[str, Any]]:
        payload_value = getattr(self, "payload", None)
        cache_key = id(payload_value)
        if getattr(self, "_routes_cache_key", None) != cache_key:
            payload = _payload(payload_value)
            self._routes_cache_key = cache_key
            self._routes_cache = {
                source_type: _route_retrieval_jobs_from_payload(payload, source_type)
                for source_type in SOURCE_TYPES
            }
        return self._routes_cache

    # Langflow 출력 함수: '더미 작업 (dummy_jobs)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def dummy_jobs_out(self) -> Data:
        return Data(data=self._routes_once()["dummy"])

    # Langflow 출력 함수: 'Oracle 작업 (oracle_jobs)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def oracle_jobs_out(self) -> Data:
        return Data(data=self._routes_once()["oracle"])

    # Langflow 출력 함수: 'H-API 작업 (h_api_jobs)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def h_api_jobs_out(self) -> Data:
        return Data(data=self._routes_once()["h_api"])

    # Langflow 출력 함수: '데이터레이크 작업 (datalake_jobs)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def datalake_jobs_out(self) -> Data:
        return Data(data=self._routes_once()["datalake"])

    # Langflow 출력 함수: 'Goodocs 작업 (goodocs_jobs)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def goodocs_jobs_out(self) -> Data:
        return Data(data=self._routes_once()["goodocs"])
