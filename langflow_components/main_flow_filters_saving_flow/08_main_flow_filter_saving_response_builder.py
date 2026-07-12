# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 08 메인 플로우 필터 등록 응답 정규화기
# 역할: 메인 플로우 필터 등록 결과를 메시지/API에서 재사용할 수 있는 구조화 페이로드로 정리합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 메인 플로우 필터 등록 상태와 요청 key/실제 canonical key를 사람이 확인하기 쉬운 구조화 응답으로 요약합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

METADATA_TYPE = "main_flow_filter"
METADATA_LABEL = "메인 플로우 필터"


# 주요 함수: 저장 결과와 canonical target을 사용자 응답용 요약으로 바꿉니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_response(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    write_result = _dict(payload.get("write_result"))
    review = _dict(payload.get("review"))
    items = _list(payload.get("items"))
    status = _status(write_result, review)
    rows = [_item_row(item, write_result) for item in items]
    summary = _summary(status, write_result, items)
    key_points = _key_points(status, write_result, review, payload, items)
    notices = _notices(write_result, review, payload)
    next_steps = _next_steps(status)
    columns = ["필터 키", "표시명", "연산자", "값 타입", "값 형태", "상태"]
    return {
        "response_type": "metadata_authoring",
        "metadata_type": METADATA_TYPE,
        "metadata_label": METADATA_LABEL,
        "status": status,
        "success": bool(write_result.get("success")),
        "direct_response_ready": True,
        "message": summary,
        "answer_sections": {
            "summary": {"headline": summary, "description": summary},
            "key_points": key_points,
            "target_table": {
                "title": "등록 대상 메인 필터",
                "columns": columns,
                "row_count": len(rows),
                "row_source": "data.rows",
                "display_limit": 12,
            },
            "notices": notices,
            "next_steps": next_steps,
        },
        "data": {"columns": columns, "rows": rows, "row_count": len(rows)},
        "metadata_authoring": {
            "metadata_type": METADATA_TYPE,
            "metadata_label": METADATA_LABEL,
            "status": status,
            "generated_count": len(items),
            "saved_count": _int(write_result.get("saved_count"), 0),
            "would_save_count": _int(write_result.get("would_save_count"), 0),
            "existing_match_count": len(_list(payload.get("existing_matches"))),
            "dry_run": bool(write_result.get("dry_run")),
            "keys": _keys(write_result, items),
        },
        "write_result": _compact_write_result(write_result),
        "trace": {
            "raw_text_preview": _dict(payload.get("trace")).get("raw_text_preview", ""),
            "errors": _list(write_result.get("errors")) + _list(review.get("errors")),
        },
    }


# 함수 설명: `_status()`는 여러 단계의 실행 결과를 우선순위에 따라 최종 상태 문자열로 결정합니다.
def _status(write_result: dict[str, Any], review: dict[str, Any]) -> str:
    if _list(write_result.get("errors")):
        return "error"
    if write_result.get("status") == "skipped":
        return "skipped"
    if _list(review.get("supplement_requests")) or _list(review.get("errors")):
        return "needs_input"
    if write_result.get("dry_run"):
        return "dry_run"
    if write_result.get("success"):
        return "saved"
    return "not_saved"


# 함수 설명: `_summary()`는 현재 처리 결과의 건수·상태·핵심 정보를 짧은 요약 dict로 만듭니다.
def _summary(status: str, write_result: dict[str, Any], items: list[Any]) -> str:
    saved_count = _int(write_result.get("saved_count"), 0)
    would_save_count = _int(write_result.get("would_save_count"), len(items))
    if status == "saved":
        return f"{METADATA_LABEL} 메타데이터 {saved_count}건 저장이 완료되었습니다."
    if status == "dry_run":
        return f"{METADATA_LABEL} 메타데이터 {would_save_count}건을 저장 전 검토했습니다. 현재 Dry Run이라 MongoDB에는 반영하지 않았습니다."
    if status == "needs_input":
        return "메인 플로우 필터 저장 전 보완이 필요한 항목이 있습니다."
    if status == "skipped":
        return "기존 메인 플로우 필터를 유지하고 저장을 건너뛰었습니다."
    if status == "error":
        return "메인 플로우 필터 저장 처리 중 문제가 발생했습니다."
    return write_result.get("message") or "메인 플로우 필터를 저장하지 않았습니다."


# 함수 설명: `_key_points()`는 구조화 응답에서 사용자가 먼저 확인할 핵심 요약 문장을 추출합니다.
def _key_points(status: str, write_result: dict[str, Any], review: dict[str, Any], payload: dict[str, Any], items: list[Any]) -> list[str]:
    points = [f"생성된 필터 후보는 {len(items)}건입니다."]
    operator_counts = _count_operators(items)
    if operator_counts:
        points.append("연산자 구성: " + ", ".join(f"{key} {value}건" for key, value in operator_counts.items()))
    if status == "saved":
        points.append(f"MongoDB에 {_int(write_result.get('saved_count'), 0)}건을 저장했습니다.")
    if status == "dry_run":
        points.append("Dry Run 모드라 실제 MongoDB 저장은 수행하지 않았습니다.")
    if payload.get("existing_matches"):
        points.append(f"비슷한 기존 메타데이터 {len(_list(payload.get('existing_matches')))}건이 확인되었습니다.")
    if _list(review.get("supplement_requests")):
        points.append("저장 전 추가 확인이 필요한 질문이 있습니다.")
    if _list(write_result.get("errors")) or _list(review.get("errors")):
        points.append("오류 또는 검수 실패 항목이 있어 저장 여부를 확인해야 합니다.")
    return points


# 함수 설명: `_item_row()`는 행을 표 또는 API 응답에 넣을 한 행 dict로 projection합니다.
def _item_row(item: Any, write_result: dict[str, Any]) -> dict[str, Any]:
    item = _dict(item)
    payload = _dict(item.get("payload"))
    return {
        "필터 키": item.get("filter_key") or item.get("key") or "",
        "표시명": payload.get("display_name") or item.get("filter_key") or "",
        "연산자": payload.get("operator") or "",
        "값 타입": payload.get("value_type") or "",
        "값 형태": payload.get("value_shape") or "",
        "상태": _item_status(write_result),
    }


# 함수 설명: `_count_operators()`는 operators의 일치도나 건수를 계산해 후보 비교와 요약에 사용합니다.
def _count_operators(items: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        payload = _dict(_dict(item).get("payload"))
        operator = str(payload.get("operator") or "").strip()
        if operator:
            counts[operator] = counts.get(operator, 0) + 1
    return counts


# 함수 설명: `_item_status()`는 여러 실행 결과를 확인해 상태의 최종 상태를 결정합니다.
def _item_status(write_result: dict[str, Any]) -> str:
    if write_result.get("dry_run"):
        return "저장 예정"
    if write_result.get("success"):
        return "저장 완료"
    return "확인 필요"


# 함수 설명: `_notices()`는 warnings와 errors를 사용자에게 보여 줄 중복 없는 안내 목록으로 정리합니다.
def _notices(write_result: dict[str, Any], review: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, str]]:
    notices = []
    for error in _list(write_result.get("errors")) + _list(review.get("errors")):
        message = str(_dict(error).get("message") or error).strip()
        if message:
            notices.append({"type": "error", "title": "오류", "message": message})
    for request in _list(review.get("supplement_requests")):
        message = str(request.get("question") if isinstance(request, dict) else request).strip()
        if message:
            notices.append({"type": "supplement", "title": "추가 확인 필요", "message": message})
    if payload.get("existing_matches"):
        notices.append({"type": "info", "title": "기존 항목", "message": f"비슷한 기존 항목 {len(_list(payload.get('existing_matches')))}건이 있습니다."})
    return notices


# 함수 설명: `_next_steps()`는 현재 상태와 오류 여부에 맞는 사용자 다음 단계 안내를 구성합니다.
def _next_steps(status: str) -> list[str]:
    if status == "dry_run":
        return ["저장 결과가 맞으면 Dry Run을 false로 바꿔 다시 실행하세요.", "저장 후 Metadata QA에서 main filter 목록을 확인하세요."]
    if status == "saved":
        return ["Metadata QA에서 등록된 필터 기준을 확인하세요.", "data_analysis_flow에서 해당 필터가 적용되는 질문을 테스트하세요."]
    if status == "needs_input":
        return ["operator, value_type, value_shape 누락 여부를 확인하세요."]
    if status == "error":
        return ["오류 메시지를 확인하고 MongoDB 설정 또는 필터 정의를 수정하세요."]
    return ["입력 원문과 검수 결과를 확인한 뒤 다시 실행하세요."]


# 함수 설명: `_keys()`는 저장 요청·operation에서 사용자에게 표시할 논리 key를 중복 없이 모읍니다.
def _keys(write_result: dict[str, Any], items: list[Any]) -> list[str]:
    keys = [str(key) for key in _list(write_result.get("keys")) if str(key).strip()]
    if keys:
        return keys
    operations = _list(write_result.get("operation_by_key"))
    keys = [str(_dict(item).get("key")) for item in operations if str(_dict(item).get("key") or "").strip()]
    if keys:
        return keys
    return [str(_dict(item).get("filter_key") or "") for item in items if str(_dict(item).get("filter_key") or "").strip()]


# 함수 설명: `_compact_write_result()`는 MongoDB 저장 결과에서 사용자 응답에 필요한 상태와 key 정보만 남깁니다.
def _compact_write_result(write_result: dict[str, Any]) -> dict[str, Any]:
    allowed = {"success", "ready_to_save", "dry_run", "saved_count", "would_save_count", "database", "collection_name", "message", "errors", "keys", "operation_by_key", "skipped_count", "status", "partial_success"}
    return {key: deepcopy(value) for key, value in write_result.items() if key in allowed}


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_dict()`는 입력값이 dict인지 확인하고 아니면 빈 dict를 반환해 후속 key 접근 오류를 막습니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 입력값을 list로 정규화하고 목록이 아닌 값은 안전한 기본 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_int()`는 문자열이나 숫자 입력을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class MainFlowFilterSavingResponseBuilder(Component):
    display_name = "08 메인 플로우 필터 등록 응답 정규화기"
    description = "메인 플로우 필터 등록 결과를 메시지/API에서 재사용할 수 있는 구조화 페이로드로 정리합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_response(getattr(self, "payload", None)))
