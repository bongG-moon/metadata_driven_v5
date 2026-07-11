# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 08 도메인 등록 응답 정규화기
# 역할: 도메인 등록 결과를 메시지/API에서 재사용할 수 있는 구조화 페이로드로 정리합니다.
# 주요 입력: 페이로드 (payload) · 필수
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 도메인 등록 상태와 요청 key/실제 canonical key를 사람이 확인하기 쉬운 구조화 응답으로 요약합니다.
# 유지보수 포인트: LLM은 후보 작성에만 사용하고 key 충돌·필수 필드·비밀값·실제 저장 여부는 Python에서 결정론적으로 판정합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

METADATA_TYPE = "domain"
METADATA_LABEL = "도메인"


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
    next_steps = _next_steps(status, write_result)
    columns = ["구분", "키", "표시명", "상태", "처리"]
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
                "title": "등록 대상 도메인",
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
            "conflict_warnings": _list(payload.get("conflict_warnings")),
            "errors": _list(write_result.get("errors")) + _list(review.get("errors")),
        },
    }


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


def _summary(status: str, write_result: dict[str, Any], items: list[Any]) -> str:
    saved_count = _int(write_result.get("saved_count"), 0)
    would_save_count = _int(write_result.get("would_save_count"), len(items))
    if status == "saved":
        return f"{METADATA_LABEL} 메타데이터 {saved_count}건 저장이 완료되었습니다."
    if status == "dry_run":
        return f"{METADATA_LABEL} 메타데이터 {would_save_count}건을 저장 전 검토했습니다. 현재 Dry Run이라 MongoDB에는 반영하지 않았습니다."
    if status == "needs_input":
        return "도메인 메타데이터 저장 전 보완이 필요한 항목이 있습니다."
    if status == "skipped":
        return "기존 도메인 메타데이터를 유지하고 저장을 건너뛰었습니다."
    if status == "error":
        return "도메인 메타데이터 저장 처리 중 문제가 발생했습니다."
    return write_result.get("message") or "도메인 메타데이터를 저장하지 않았습니다."


def _key_points(status: str, write_result: dict[str, Any], review: dict[str, Any], payload: dict[str, Any], items: list[Any]) -> list[str]:
    points = [f"생성된 등록 후보는 {len(items)}건입니다."]
    if status == "saved":
        points.append(f"MongoDB에 {_int(write_result.get('saved_count'), 0)}건을 저장했습니다.")
    if status == "dry_run":
        points.append("Dry Run 모드라 실제 MongoDB 저장은 수행하지 않았습니다.")
    operation_summary = _operation_summary(write_result)
    if operation_summary:
        points.append(f"처리 구분: {operation_summary}")
    if payload.get("existing_matches"):
        points.append(f"비슷한 기존 메타데이터 {len(_list(payload.get('existing_matches')))}건이 확인되었습니다.")
    if _list(review.get("supplement_requests")):
        points.append("저장 전 추가 확인이 필요한 질문이 있습니다.")
    if _list(write_result.get("errors")) or _list(review.get("errors")):
        points.append("오류 또는 검수 실패 항목이 있어 저장 여부를 확인해야 합니다.")
    return points


def _item_row(item: Any, write_result: dict[str, Any]) -> dict[str, Any]:
    item = _dict(item)
    payload = _dict(item.get("payload"))
    section = str(item.get("section") or "").strip()
    key = str(item.get("key") or "").strip()
    requested_key = f"{section}:{key}" if section and key else key
    operation = _operation_for_key(write_result, requested_key)
    target_key = str(operation.get("target_key") or operation.get("key") or requested_key).strip()
    target_section, _target_item_key = _split_logical_key(target_key)
    return {
        "구분": target_section or section or "domain",
        "키": target_key,
        "표시명": payload.get("display_name") or key,
        "상태": _item_status(write_result, operation),
        "처리": _operation_label(str(operation.get("operation") or ""), bool(write_result.get("dry_run"))),
    }


def _item_status(write_result: dict[str, Any], operation: dict[str, Any] | None = None) -> str:
    operation = _dict(operation)
    if operation.get("operation") == "skipped":
        return "기존 유지"
    if write_result.get("dry_run"):
        return "저장 예정"
    if write_result.get("success"):
        return "저장 완료"
    return "확인 필요"


def _operation_for_key(write_result: dict[str, Any], requested_key: str) -> dict[str, Any]:
    operations = [_dict(item) for item in _list(write_result.get("operation_by_key"))]
    requested_lower = requested_key.lower()
    for operation in operations:
        candidate_keys = {
            str(operation.get("requested_key") or "").lower(),
            str(operation.get("key") or "").lower(),
            str(operation.get("target_key") or "").lower(),
        }
        if requested_lower in candidate_keys:
            return operation
    return {}


def _operation_label(operation: str, dry_run: bool) -> str:
    labels = {
        "inserted": "신규 저장",
        "replaced": "기존 항목 교체",
        "merged": "기존 항목 병합",
        "skipped": "기존 항목 유지",
        "created_new": "새 키로 저장",
        "create_new": "새 키로 저장",
    }
    label = labels.get(operation, "처리 안 함" if not operation else operation)
    return f"{label} 예정" if dry_run and operation != "skipped" else label


def _operation_summary(write_result: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for item in _list(write_result.get("operation_by_key")):
        operation = str(_dict(item).get("operation") or "")
        label = _operation_label(operation, bool(write_result.get("dry_run")))
        if operation:
            counts[label] = counts.get(label, 0) + 1
    return ", ".join(f"{label} {count}건" for label, count in counts.items())


def _split_logical_key(value: str) -> tuple[str, str]:
    section, separator, key = str(value or "").partition(":")
    return (section, key) if separator else ("", section)


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
    if payload.get("conflict_warnings"):
        notices.append({"type": "warning", "title": "충돌 가능성", "message": f"충돌 가능성 {len(_list(payload.get('conflict_warnings')))}건이 있습니다."})
    return notices


def _next_steps(status: str, write_result: dict[str, Any]) -> list[str]:
    if status == "dry_run":
        return ["저장 결과가 맞으면 Dry Run을 false로 바꿔 다시 실행하세요.", "저장 후 Metadata QA에서 등록 내용을 확인하세요."]
    if status == "saved":
        return ["Metadata QA에서 등록된 도메인 정보를 확인하세요.", "분석 flow에서 관련 질문을 테스트하세요."]
    if status == "needs_input":
        return ["표와 안내 메시지의 보완 항목을 확인한 뒤 원문을 보강해 다시 실행하세요."]
    if status == "error":
        return ["오류 메시지를 확인하고 MongoDB 설정 또는 입력 메타데이터를 수정하세요."]
    return ["입력 원문과 검수 결과를 확인한 뒤 다시 실행하세요."]


def _keys(write_result: dict[str, Any], items: list[Any]) -> list[str]:
    keys = [str(key) for key in _list(write_result.get("keys")) if str(key).strip()]
    if keys:
        return keys
    operations = _list(write_result.get("operation_by_key"))
    keys = [str(_dict(item).get("key")) for item in operations if str(_dict(item).get("key") or "").strip()]
    if keys:
        return keys
    result = []
    for item in items:
        item = _dict(item)
        section = str(item.get("section") or "").strip()
        key = str(item.get("key") or "").strip()
        if section and key:
            result.append(f"{section}:{key}")
        elif key:
            result.append(key)
    return result


def _compact_write_result(write_result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "success",
        "ready_to_save",
        "dry_run",
        "saved_count",
        "would_save_count",
        "database",
        "collection_name",
        "message",
        "errors",
        "keys",
        "operation_by_key",
        "skipped_count",
        "status",
        "partial_success",
    }
    return {key: deepcopy(value) for key, value in write_result.items() if key in allowed}


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class DomainSavingResponseBuilder(Component):
    display_name = "08 도메인 등록 응답 정규화기"
    description = "도메인 등록 결과를 메시지/API에서 재사용할 수 있는 구조화 페이로드로 정리합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload", types=["Data"])]

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=build_response(getattr(self, "payload", None)))
