# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 13 소스 조회 결과 병합기
# 역할: 소스별 조회 페이로드를 병합하고 데이터 조회 추적 정보를 작성합니다.
# 주요 입력: 메인 페이로드 (main_payload) · 필수, 더미 조회 결과 (dummy_retrieval), Oracle 조회 결과 (oracle_retrieval), H-API 조회 결과
#        (h_api_retrieval), 데이터레이크 조회 결과 (datalake_retrieval), Goodocs 조회 결과 (goodocs_retrieval)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 각 조회 분기의 결과를 입력 순서대로 합치면서 warnings·errors·trace를 잃지 않고 하나의 페이로드로 만듭니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

# 주요 함수: 여러 조회 분기에서 돌아온 source result와 trace를 하나로 병합합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def merge_source_retrieval_payloads(main_payload_value: Any, *retrieval_values: Any) -> dict[str, Any]:
    payload = _payload(main_payload_value)
    source_results = []
    skipped_sources = []
    errors = []
    warnings = []
    for value in retrieval_values:
        retrieval = _payload(value)
        if retrieval.get("skipped"):
            skipped_sources.append({"source_type": retrieval.get("source_type"), "skip_reason": retrieval.get("skip_reason")})
            continue
        source_results.extend([deepcopy(item) for item in retrieval.get("source_results", []) if isinstance(item, dict)])
        errors.extend(retrieval.get("errors", []))
        warnings.extend(retrieval.get("warnings", []))
    next_payload = deepcopy(payload)
    compact_results = [{key: value for key, value in item.items() if key != "rows"} for item in source_results]
    if compact_results:
        next_payload["source_results"] = compact_results
        next_payload["_runtime_rows_by_alias"] = {item.get("source_alias"): item.get("rows", []) for item in source_results if item.get("source_alias")}
    else:
        next_payload["source_results"] = deepcopy(payload.get("source_results", []))
    trace = next_payload.setdefault("trace", {})
    trace.setdefault("errors", []).extend(errors)
    trace.setdefault("warnings", []).extend(warnings)
    trace.setdefault("inspection", {})["data_retrieval"] = {
        "stage": "13_source_retrieval_merger",
        "status": "error" if errors else "ok",
        "executed_source_count": len(compact_results),
        "sources": compact_results or deepcopy(payload.get("source_results", [])),
        "skipped_sources": skipped_sources,
        "preserved_existing_runtime_sources": not compact_results and bool(payload.get("runtime_sources")),
    }
    return next_payload


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class SourceRetrievalMerger(Component):
    display_name = "13 소스 조회 결과 병합기"
    description = "소스별 조회 페이로드를 병합하고 데이터 조회 추적 정보를 작성합니다."
    inputs = [
        DataInput(name="main_payload", display_name="메인 페이로드", required=True),
        DataInput(name="dummy_retrieval", display_name="더미 조회 결과", required=False),
        DataInput(name="oracle_retrieval", display_name="Oracle 조회 결과", required=False),
        DataInput(name="h_api_retrieval", display_name="H-API 조회 결과", required=False),
        DataInput(name="datalake_retrieval", display_name="데이터레이크 조회 결과", required=False),
        DataInput(name="goodocs_retrieval", display_name="Goodocs 조회 결과", required=False),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=merge_source_retrieval_payloads(
                getattr(self, "main_payload", None),
                getattr(self, "dummy_retrieval", None),
                getattr(self, "oracle_retrieval", None),
                getattr(self, "h_api_retrieval", None),
                getattr(self, "datalake_retrieval", None),
                getattr(self, "goodocs_retrieval", None),
            )
        )
