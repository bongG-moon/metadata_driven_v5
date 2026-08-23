# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 24 런타임 페이로드 정리기
# 역할: 최종 응답과 세션 상태 저장이 끝난 뒤 대용량 행 버퍼의 공유 참조를 해제하고 작은 응답 페이로드만 전달합니다.
# 주요 입력: 응답 페이로드 (payload), GC 모드 (gc_mode)
# 주요 출력: 정리된 페이로드 (payload_out)
# 처리 흐름: 대용량 런타임 key를 제외한 응답을 먼저 복사하고, 공유 행 list를 비운 뒤 선택한 세대의 GC를 실행합니다.
# 유지보수 포인트: 이 노드는 MongoDB 결과·세션 저장 뒤, 최종 Message/API 어댑터 직전에만 연결해야 합니다.
# =============================================================================

from __future__ import annotations

import gc
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, DropdownInput, Output
from lfx.schema.data import Data

RUNTIME_BUFFER_KEYS = (
    "runtime_sources",
    "_runtime_rows_by_alias",
    "_full_result_rows",
    "_runtime_result_rows",
    "_intermediate_download_rows",
    "_intermediate_download_metadata",
    "_execution_report_domain_details",
)
GC_MODES = ("disabled", "generation_0", "full")

# Node 25 consumes this one bounded, transient projection immediately after
# cleanup.  It deliberately does not enter result-store/session payloads and
# is removed before Message/API output.
EXECUTION_REPORT_DATA_PREVIEW_KEY = "_execution_report_data_preview"
MAX_REPORT_SOURCE_TABLES = 8
MAX_REPORT_INTERMEDIATE_TABLES = 4
MAX_REPORT_COLUMNS = 20
MAX_REPORT_SOURCE_ROWS = 10
MAX_REPORT_INTERMEDIATE_ROWS = 10
MAX_REPORT_FINAL_ROWS = 30
MAX_REPORT_CELL_CHARACTERS = 160


# 함수 설명: `release_runtime_payload()`는 최종 응답용 작은 필드를 보존한 뒤 공유된 대용량 행 버퍼를 해제합니다.
def release_runtime_payload(payload_value: Any, gc_mode: Any = "generation_0") -> dict[str, Any]:
    """Return the compact response and deterministically release shared row buffers."""

    source = _payload_view(payload_value)
    if not source:
        return {
            "runtime_cleanup": {
                "stage": "24_runtime_payload_cleanup",
                "status": "skipped",
                "reason": "empty_payload",
                "gc_mode": _gc_mode(gc_mode),
                "gc_collected": 0,
                "released_row_count": 0,
                "released_buffer_count": 0,
            }
        }

    # Build the small HTML-only data view before clearing runtime buffers.  A
    # malformed preview must never stop normal cleanup or the analysis answer.
    try:
        report_preview = _build_execution_report_data_preview(source)
        report_domain_details = source.get("_execution_report_domain_details")
        if isinstance(report_domain_details, list):
            # Node 04 already builds this bounded and credential-free
            # projection.  Keep it only in Node 25's transient sidecar so the
            # full candidate envelope never reaches normal answer/session
            # payloads.
            report_preview["domains"] = deepcopy(report_domain_details[:24])
    except Exception:  # noqa: BLE001 - best-effort sidecar for Node 25 only.
        report_preview = {}

    compact_payload = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in RUNTIME_BUFFER_KEYS and key != EXECUTION_REPORT_DATA_PREVIEW_KEY
    }
    if report_preview:
        compact_payload[EXECUTION_REPORT_DATA_PREVIEW_KEY] = report_preview
    released_row_count, released_buffer_count = _clear_runtime_buffers(source)
    selected_mode = _gc_mode(gc_mode)
    compact_payload["runtime_cleanup"] = {
        "stage": "24_runtime_payload_cleanup",
        "status": "ok",
        "gc_mode": selected_mode,
        "gc_collected": _collect(selected_mode),
        "released_row_count": released_row_count,
        "released_buffer_count": released_buffer_count,
    }
    return compact_payload


# 함수 설명: HTML Report에서만 사용할 원본·중간·최종 데이터의 작은 표 projection을 구성합니다.
def _build_execution_report_data_preview(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    refs = payload.get("data_refs") if isinstance(payload.get("data_refs"), list) else []
    source_results = _source_results_by_alias(payload.get("source_results"))
    jobs = _jobs_by_alias(payload.get("intent_plan"))

    original_tables: list[dict[str, Any]] = []
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    for alias, rows in list(runtime_sources.items())[:MAX_REPORT_SOURCE_TABLES]:
        alias_text = str(alias or "").strip()
        if not alias_text or not isinstance(rows, list):
            continue
        source_result = source_results.get(alias_text.casefold(), {})
        job = jobs.get(alias_text.casefold(), {})
        dataset_key = str(source_result.get("dataset_key") or job.get("dataset_key") or alias_text).strip()
        original_tables.append(
            _preview_table(
                key=f"source:{alias_text}",
                title=f"사용 원본 데이터: {dataset_key}",
                description=f"소스 별칭: {alias_text}",
                rows=rows,
                columns=source_result.get("columns"),
                row_count=source_result.get("row_count"),
                priority_columns=_job_condition_columns(job),
                max_rows=MAX_REPORT_SOURCE_ROWS,
                download=_matching_download_ref(refs, "source_rows", source_alias=alias_text),
            )
        )

    intermediate_tables: list[dict[str, Any]] = []
    artifacts = (
        payload.get("_intermediate_download_rows")
        if isinstance(payload.get("_intermediate_download_rows"), dict)
        else {}
    )
    metadata = (
        payload.get("_intermediate_download_metadata")
        if isinstance(payload.get("_intermediate_download_metadata"), dict)
        else {}
    )
    for key, artifact_value in list(artifacts.items())[:MAX_REPORT_INTERMEDIATE_TABLES]:
        key_text = str(key or "").strip()
        if not key_text:
            continue
        artifact = artifact_value if isinstance(artifact_value, dict) else {"rows": artifact_value}
        item_metadata = metadata.get(key_text) if isinstance(metadata.get(key_text), dict) else {}
        rows = artifact.get("rows") if isinstance(artifact.get("rows"), list) else []
        label = str(artifact.get("label") or item_metadata.get("label") or "중간 결과").strip()
        checkpoint_key = str(
            artifact.get("checkpoint_key") or item_metadata.get("checkpoint_key") or key_text
        ).strip()
        intermediate_tables.append(
            _preview_table(
                key=f"intermediate:{key_text}",
                title=label,
                description="최종 결과를 만들기 전 선택된 실행 단계",
                rows=rows,
                columns=artifact.get("columns") or item_metadata.get("columns"),
                row_count=artifact.get("row_count") or item_metadata.get("row_count"),
                max_rows=MAX_REPORT_INTERMEDIATE_ROWS,
                download=_matching_download_ref(
                    refs,
                    "intermediate_result",
                    checkpoint_key=checkpoint_key,
                ),
            )
        )

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    final_rows = payload.get("_full_result_rows")
    if not isinstance(final_rows, list):
        final_rows = payload.get("_runtime_result_rows")
    if not isinstance(final_rows, list):
        final_rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    final_tables: list[dict[str, Any]] = []
    if final_rows or data.get("row_count") is not None or data.get("columns"):
        final_tables.append(
            _preview_table(
                key="final_result",
                title="최종 결과 데이터",
                description="최종 결과 계약을 적용한 결과",
                rows=final_rows,
                columns=data.get("columns"),
                row_count=data.get("row_count"),
                max_rows=MAX_REPORT_FINAL_ROWS,
                download=_matching_download_ref(refs, "analysis_result"),
            )
        )

    preview = {
        "original": original_tables,
        "intermediate": intermediate_tables,
        "final": final_tables,
    }
    return preview if any(preview.values()) else {}


# 함수 설명: source result 및 계획 alias를 소문자 key map으로 정리합니다.
def _source_results_by_alias(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias:
            result[alias.casefold()] = item
    return result


def _jobs_by_alias(value: Any) -> dict[str, dict[str, Any]]:
    plan = value if isinstance(value, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    raw_jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    for item in raw_jobs:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias:
            result[alias.casefold()] = item
    return result


# 함수 설명: 조회 조건·필수 파라미터 컬럼을 원본 표의 앞쪽에 배치합니다.
def _job_condition_columns(job: dict[str, Any]) -> list[str]:
    result: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in result}:
            result.append(text)

    required_params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
    for field in required_params:
        add(field)
    filters = job.get("filters") if isinstance(job.get("filters"), dict) else {}
    for field in filters:
        add(field)
    return result


# 함수 설명: table snapshot은 행·컬럼·셀 크기를 제한해 cleanup 뒤에도 작은 payload만 유지합니다.
def _preview_table(
    *,
    key: str,
    title: str,
    description: str,
    rows: Any,
    columns: Any,
    row_count: Any,
    max_rows: int,
    download: dict[str, Any] | None = None,
    priority_columns: list[str] | None = None,
) -> dict[str, Any]:
    raw_rows = rows if isinstance(rows, list) else []
    row_items = [item if isinstance(item, dict) else {"value": item} for item in raw_rows]
    table_columns, columns_truncated = _preview_columns(
        row_items,
        columns,
        priority_columns or [],
    )
    displayed_rows = [
        {
            column: _preview_cell(row.get(column))
            for column in table_columns
        }
        for row in row_items[:max_rows]
    ]
    # Runtime rows are the execution fact of last resort.  A stale or missing
    # metadata count must not make a non-empty preview claim that it has zero
    # rows.
    total_count = max(_positive_int(row_count, len(row_items)), len(row_items))
    return {
        "key": key,
        "title": str(title or "데이터").strip(),
        "description": str(description or "").strip(),
        "row_count": total_count,
        "shown_row_count": len(displayed_rows),
        "truncated": total_count > len(displayed_rows),
        "columns": table_columns,
        "columns_truncated": columns_truncated,
        "rows": displayed_rows,
        "download": deepcopy(download or {}),
    }


def _preview_columns(rows: list[dict[str, Any]], declared: Any, priority: list[str]) -> tuple[list[str], bool]:
    candidates: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in candidates}:
            candidates.append(text)

    for value in priority:
        add(value)
    for value in declared if isinstance(declared, list) else []:
        add(value)
    for row in rows[:MAX_REPORT_SOURCE_ROWS]:
        for value in row:
            add(value)
    return candidates[:MAX_REPORT_COLUMNS], len(candidates) > MAX_REPORT_COLUMNS


def _preview_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_REPORT_CELL_CHARACTERS:
            return value[:MAX_REPORT_CELL_CHARACTERS].rstrip() + "…"
        return value
    text = str(value)
    return text[:MAX_REPORT_CELL_CHARACTERS].rstrip() + ("…" if len(text) > MAX_REPORT_CELL_CHARACTERS else "")


def _matching_download_ref(
    refs: Any,
    role: str,
    *,
    source_alias: str = "",
    checkpoint_key: str = "",
) -> dict[str, Any]:
    for item in refs if isinstance(refs, list) else []:
        if not isinstance(item, dict) or str(item.get("role") or "").strip() != role:
            continue
        if source_alias and str(item.get("source_alias") or "").strip().casefold() != source_alias.casefold():
            continue
        if checkpoint_key and str(item.get("checkpoint_key") or "").strip() != checkpoint_key:
            continue
        return {
            "url": str(item.get("download_url") or "").strip(),
            "expires_at": str(item.get("expires_at") or "").strip(),
            "complete": item.get("complete"),
        }
    return {}


def _positive_int(value: Any, fallback: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return max(int(fallback), 0)


# 함수 설명: `_clear_runtime_buffers()`는 같은 list를 여러 key가 공유해도 한 번만 비우고 해제 행·버퍼 수를 계산합니다.
def _clear_runtime_buffers(payload: dict[str, Any]) -> tuple[int, int]:
    released_rows = 0
    released_buffers = 0
    seen_lists: set[int] = set()
    seen_dicts: set[int] = set()

    for key in RUNTIME_BUFFER_KEYS:
        buffer = payload.get(key)
        if isinstance(buffer, dict):
            buffer_id = id(buffer)
            if buffer_id in seen_dicts:
                continue
            seen_dicts.add(buffer_id)
            for rows in list(buffer.values()):
                if isinstance(rows, list):
                    rows_id = id(rows)
                    if rows_id in seen_lists:
                        continue
                    seen_lists.add(rows_id)
                    released_rows += len(rows)
                    released_buffers += 1
                    rows.clear()
            buffer.clear()
            continue
        if isinstance(buffer, list):
            buffer_id = id(buffer)
            if buffer_id in seen_lists:
                continue
            seen_lists.add(buffer_id)
            released_rows += len(buffer)
            released_buffers += 1
            buffer.clear()

    return released_rows, released_buffers


# 함수 설명: `_collect()`는 선택한 GC 모드에 따라 세대 0 또는 전체 garbage collection을 한 번 실행합니다.
def _collect(mode: str) -> int:
    if mode == "full":
        return int(gc.collect())
    if mode == "generation_0":
        return int(gc.collect(0))
    return 0


# 함수 설명: `_gc_mode()`는 노드 입력을 지원하는 세 가지 GC 모드 중 하나로 제한합니다.
def _gc_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in GC_MODES else "generation_0"


# 함수 설명: `_payload_view()`는 종료 노드가 공유 버퍼를 직접 해제할 수 있도록 원본 payload view를 반환합니다.
def _payload_view(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return data if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: 최종 Message/API 분기 직전에서 런타임 행 참조 해제와 선택적 GC를 한 번 수행합니다.
class RuntimePayloadCleanup(Component):
    display_name = "24 런타임 페이로드 정리기"
    description = "최종 저장 뒤 대용량 런타임 행 참조를 해제하고 가벼운 세대 0 GC를 실행합니다."
    icon = "Recycle"
    name = "RuntimePayloadCleanup"
    inputs = [
        DataInput(name="payload", display_name="응답 페이로드", required=True),
        DropdownInput(
            name="gc_mode",
            display_name="GC 모드",
            options=list(GC_MODES),
            value="generation_0",
            advanced=True,
        ),
    ]
    outputs = [
        Output(name="payload_out", display_name="정리된 페이로드", method="build_payload", types=["Data"])
    ]

    # Langflow 출력 함수: 정리된 최종 응답 페이로드와 해제 통계를 downstream 어댑터에 전달합니다.
    def build_payload(self) -> Data:
        result = release_runtime_payload(
            getattr(self, "payload", None),
            getattr(self, "gc_mode", "generation_0"),
        )
        self.status = result.get("runtime_cleanup", {})
        return Data(data=result)
