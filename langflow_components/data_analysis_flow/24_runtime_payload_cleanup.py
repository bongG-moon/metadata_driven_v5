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
)
GC_MODES = ("disabled", "generation_0", "full")


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

    compact_payload = {
        key: deepcopy(value)
        for key, value in source.items()
        if key not in RUNTIME_BUFFER_KEYS
    }
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
