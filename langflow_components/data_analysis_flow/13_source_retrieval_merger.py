from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

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
