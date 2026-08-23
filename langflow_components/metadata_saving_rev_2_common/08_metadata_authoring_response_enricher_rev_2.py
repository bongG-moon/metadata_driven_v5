# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 08 메타데이터 등록 응답 보강기 rev_2
# 역할: 기존 저장 응답 계약을 유지하면서 원문·정제안·해석 근거·재입력 예시를 additive 필드로 보강합니다.
# 주요 입력: 기존 응답 payload, writer 직전부터 이어진 authoring payload
# 주요 출력: rev_2 응답 payload
# =============================================================================

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

MONGODB_ERROR_TYPES = {
    "identity_lookup_unavailable",
    "missing_mongo_config",
    "missing_mongo_uri",
    "mongo_duplicate_lookup_error",
    "mongo_write_error",
}


def enrich_response(response_value: Any, authoring_payload_value: Any) -> dict[str, Any]:
    response = _payload(response_value)
    authoring = _payload(authoring_payload_value)
    draft = _dict(authoring.get("metadata_authoring_draft"))
    refinement = _dict(authoring.get("refinement"))
    trace = _dict(authoring.get("trace"))
    write_result = _dict(authoring.get("write_result"))

    original_text = _redact(str(draft.get("original_text") or _dict(authoring.get("request")).get("raw_text") or ""))
    refined_text = _redact(str(draft.get("refined_text") or refinement.get("refined_text") or ""))
    resolved = [deepcopy(item) for item in _list(draft.get("resolved_references")) if isinstance(item, dict)]
    unresolved = [deepcopy(item) for item in _list(draft.get("unresolved_references")) if isinstance(item, dict)]
    missing = _unique_text([*_string_list(draft.get("missing_information")), *_string_list(refinement.get("missing_information"))])
    assumptions = _unique_text([*_string_list(draft.get("assumptions")), *_string_list(refinement.get("assumptions"))])
    retry_example = _redact(str(draft.get("retry_example") or ""), 4000)
    retry_examples = [_redact(item, 4000) for item in _string_list(draft.get("retry_examples"))]
    if retry_example and not retry_examples:
        retry_examples = [retry_example]
    needs_input = bool(draft.get("needs_more_input")) or bool(missing) or bool(unresolved)

    metadata_authoring = deepcopy(_dict(response.get("metadata_authoring")))
    metadata_authoring.update(
        {
            "contract_version": "metadata_authoring.rev_2.v1",
            "original_text": original_text,
            "refined_text": refined_text,
            "resolved_references": resolved,
            "unresolved_references": unresolved,
            "missing_information": missing,
            "assumptions": assumptions,
            "retry_example": retry_example,
            "retry_examples": retry_examples,
            "contract_validation": deepcopy(_dict(draft.get("contract_validation"))),
        }
    )
    response["metadata_authoring"] = metadata_authoring
    if needs_input:
        response["status"] = "needs_input"
        response["success"] = False
        metadata_authoring["status"] = "needs_input"
        response["message"] = "저장하지 않았습니다. 미확정 계약을 확인하고 아래 예시처럼 입력해 주세요."

    sections = deepcopy(_dict(response.get("answer_sections")))
    summary = deepcopy(_dict(sections.get("summary")))
    if needs_input:
        summary.update({"headline": response["message"], "description": response["message"]})
    sections["summary"] = summary
    key_points = _unique_text(_list(sections.get("key_points")))
    if refined_text and refined_text != original_text:
        key_points.insert(0, "사용자 원문과 분리된 rev_2 정제안을 생성했습니다.")
    if resolved:
        key_points.append(f"활성 metadata 계약으로 참조 {len(resolved)}건을 확정했습니다.")
    sections["key_points"] = _unique_text(key_points)
    notices = [deepcopy(item) for item in _list(sections.get("notices")) if isinstance(item, dict)]
    if unresolved:
        notices.append({"type": "supplement", "title": "미확정 참조", "message": f"dataset 또는 표준 컬럼 참조 {len(unresolved)}건을 하나로 확정해야 합니다."})
    if retry_examples:
        notices.append({"type": "retry_example", "title": "다시 입력 예시", "message": "아래 복사 가능한 입력 예시를 참고해 다시 실행하세요."})
    sections["notices"] = _dedupe_notices(notices)
    next_steps = _unique_text(_list(sections.get("next_steps")))
    error_types = _error_types(response, authoring)
    if str(response.get("status") or "") == "error" and error_types and not _has_mongodb_error(error_types):
        next_steps = [item for item in next_steps if "MongoDB 설정" not in item]
        next_steps.insert(
            0,
            "생성된 저장 후보가 메타데이터 계약 검증을 통과하지 못했습니다. 같은 원문을 반복 입력하지 말고 오류 내용을 확인하세요.",
        )
    if retry_examples:
        next_steps.insert(0, "응답의 완성된 '다시 입력 예시' 중 실제 계약과 맞는 내용을 그대로 복사해 다시 실행하세요.")
    sections["next_steps"] = _unique_text(next_steps)
    response["answer_sections"] = sections

    response_trace = deepcopy(_dict(response.get("trace")))
    response_trace["contract_resolution"] = deepcopy(_dict(trace.get("contract_resolution")))
    response_trace["write_status"] = str(write_result.get("status") or "")
    response["trace"] = response_trace
    return response


def _redact(value: str, limit: int = 2000) -> str:
    pattern = re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")
    return pattern.sub(r"\1\2***", str(value or ""))[:limit]


def _dedupe_notices(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in values:
        marker = (str(item.get("type") or ""), str(item.get("title") or ""), str(item.get("message") or ""))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def _error_types(response: dict[str, Any], authoring: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    values.extend(_list(authoring.get("errors")))
    values.extend(_list(_dict(authoring.get("review")).get("errors")))
    values.extend(_list(_dict(authoring.get("write_result")).get("errors")))
    values.extend(_list(_dict(response.get("write_result")).get("errors")))
    values.extend(_list(_dict(response.get("trace")).get("errors")))
    return {
        str(_dict(item).get("type") or "").strip()
        for item in values
        if isinstance(item, dict) and str(item.get("type") or "").strip()
    }


def _has_mongodb_error(error_types: set[str]) -> bool:
    return any(error_type in MONGODB_ERROR_TYPES or error_type.startswith("mongo_") for error_type in error_types)


def _unique_text(values: Any) -> list[str]:
    result = []
    seen = set()
    for value in values if isinstance(values, (list, tuple, set)) else []:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item or "").strip()] if isinstance(value, list) else []


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


class MetadataAuthoringResponseEnricherRev2(Component):
    display_name = "08 메타데이터 등록 응답 보강기 rev_2"
    description = "기존 응답에 원문, 정제안, 계약 해석 근거와 복사 가능한 재입력 예시를 추가합니다."
    inputs = [
        DataInput(name="payload", display_name="기존 저장 응답", required=True),
        DataInput(name="authoring_payload", display_name="등록 처리 페이로드", required=True),
    ]
    outputs = [Output(name="payload_out", display_name="rev_2 응답", method="build_payload", types=["Data"])]

    def build_payload(self) -> Data:
        return Data(data=enrich_response(getattr(self, "payload", None), getattr(self, "authoring_payload", None)))
