# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 08A 메인 플로우 필터 Portal 계약 보강기
# 역할: 기존 04 Writer/Response 결과를 바꾸지 않고 Portal이 표시하는 등록 과정 필드를
#       additive 방식으로만 채웁니다.
# 주요 입력: 기존 응답 payload, Writer가 반환한 등록 처리 payload
# 주요 출력: Portal API 계약을 유지한 응답 payload
# 처리 흐름: status/message/data/write_result를 보존하고 원문·정제안·검증 요약·trace만
#           보강합니다.
# 유지보수 포인트: 이 노드는 저장 판단, 중복 정책, 오류 차단을 수행하지 않습니다. 실제
#                 저장 가능 여부는 기존 04 정규화기와 07 Writer가 계속 결정합니다.
# =============================================================================

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


PORTAL_CONTRACT_VERSION = "metadata_authoring.rev_2.v1"
PORTAL_RESPONSE_MODE = "legacy_main_flow_filter_writer_with_initial_transform_v1"
METADATA_TYPE = "main_flow_filter"
METADATA_LABEL = "메인 플로우 필터"


def enrich_portal_contract(response_value: Any, authoring_payload_value: Any) -> dict[str, Any]:
    """Add Portal display fields without changing the legacy save outcome."""

    response = _payload(response_value)
    authoring = _payload(authoring_payload_value)

    metadata_authoring = deepcopy(_dict(response.get("metadata_authoring")))
    request = _dict(authoring.get("request"))
    refinement = _dict(authoring.get("refinement"))
    authoring_trace = _dict(authoring.get("trace"))
    response_trace = deepcopy(_dict(response.get("trace")))
    write_result = _dict(response.get("write_result")) or _dict(authoring.get("write_result"))
    review = _dict(authoring.get("review"))
    items = _list(authoring.get("items"))
    data = _dict(response.get("data"))

    raw_text = _redact(str(request.get("raw_text") or ""))
    refined_text = _redact(str(refinement.get("refined_text") or raw_text))
    validation = _portal_validation(response, authoring, write_result, review)

    # Existing legacy summary fields are authoritative.  This node only fills
    # missing display fields, so it can never change the Writer decision.
    metadata_authoring.setdefault("contract_version", PORTAL_CONTRACT_VERSION)
    metadata_authoring.setdefault("metadata_type", METADATA_TYPE)
    metadata_authoring.setdefault("metadata_label", METADATA_LABEL)
    metadata_authoring.setdefault("status", response.get("status") or write_result.get("status") or "")
    metadata_authoring.setdefault("generated_count", len(items) if items else _int(data.get("row_count"), 0))
    metadata_authoring.setdefault("saved_count", _int(write_result.get("saved_count"), 0))
    metadata_authoring.setdefault("would_save_count", _int(write_result.get("would_save_count"), len(items)))
    metadata_authoring.setdefault("existing_match_count", len(_list(authoring.get("existing_matches"))))
    metadata_authoring.setdefault("dry_run", bool(write_result.get("dry_run")))
    metadata_authoring.setdefault("keys", _keys(write_result, items))
    metadata_authoring.setdefault("original_text", raw_text)
    metadata_authoring.setdefault("refined_text", refined_text)
    metadata_authoring.setdefault("resolved_references", [])
    metadata_authoring.setdefault("unresolved_references", [])
    metadata_authoring.setdefault("missing_information", _string_list(refinement.get("missing_information")))
    metadata_authoring.setdefault("assumptions", _string_list(refinement.get("assumptions")))
    metadata_authoring.setdefault("retry_example", "")
    metadata_authoring.setdefault("retry_examples", [])
    existing_validation = _dict(metadata_authoring.get("contract_validation"))
    metadata_authoring["contract_validation"] = {**validation, **existing_validation}
    response["metadata_authoring"] = metadata_authoring

    if authoring_trace.get("initial_transform") is not None:
        response_trace["initial_transform"] = deepcopy(authoring_trace["initial_transform"])
    response_trace.setdefault("write_status", str(write_result.get("status") or ""))
    response_trace["response_contract"] = {
        "version": PORTAL_CONTRACT_VERSION,
        "mode": PORTAL_RESPONSE_MODE,
        "additive": True,
    }
    response["trace"] = response_trace
    return response


def _portal_validation(
    response: dict[str, Any],
    authoring: dict[str, Any],
    write_result: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    errors = _unique_diagnostics(
        [
            *_list(authoring.get("errors")),
            *_list(review.get("errors")),
            *_list(write_result.get("errors")),
            *_list(_dict(response.get("trace")).get("errors")),
        ]
    )
    warnings = _unique_diagnostics(
        [*_list(authoring.get("warnings")), *_list(review.get("warnings"))]
    )
    response_status = str(response.get("status") or write_result.get("status") or "").strip().casefold()
    if response_status in {"saved", "dry_run", "skipped"} and not errors:
        status = "validated"
    elif response_status == "needs_input":
        status = "needs_input"
    elif response_status in {"error", "not_saved"} or errors:
        status = "error"
    else:
        status = "reviewed"
    return {
        "status": status,
        "mode": "legacy_writer",
        "errors": errors,
        "warnings": warnings,
        "note": "초기 변환 후 기존 Main Flow Filter Writer의 검토·저장 결과를 사용했습니다.",
    }


def _keys(write_result: dict[str, Any], items: list[Any]) -> list[str]:
    keys = [str(value).strip() for value in _list(write_result.get("keys")) if str(value or "").strip()]
    if keys:
        return keys
    operation_keys = [
        str(_dict(value).get("key") or "").strip()
        for value in _list(write_result.get("operation_by_key"))
        if str(_dict(value).get("key") or "").strip()
    ]
    if operation_keys:
        return operation_keys
    return [
        str(_dict(value).get("filter_key") or _dict(value).get("key") or "").strip()
        for value in items
        if str(_dict(value).get("filter_key") or _dict(value).get("key") or "").strip()
    ]


def _unique_diagnostics(values: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if isinstance(value, dict):
            item = deepcopy(value)
        else:
            text = str(value or "").strip()
            if not text:
                continue
            item = {"message": text}
        marker = (str(item.get("type") or ""), str(item.get("message") or item))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def _redact(value: str, limit: int = 8000) -> str:
    pattern = re.compile(
        r"(?i)(password|passwd|token|secret|api[_-]?key|authorization|credential)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
    )
    return pattern.sub(r"\1\2***", str(value or ""))[:limit]


def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in _list(value) if str(item or "").strip()]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


class MainFlowFilterPortalContractEnricher(Component):
    display_name = "08A 메인 플로우 필터 Portal 계약 보강기"
    description = "기존 Writer 결과를 바꾸지 않고 Portal 표시용 API 계약 필드만 보강합니다."
    inputs = [
        DataInput(name="payload", display_name="기존 등록 응답", required=True),
        DataInput(name="authoring_payload", display_name="Writer 등록 처리 페이로드", required=True),
    ]
    outputs = [
        Output(name="payload_out", display_name="Portal 호환 응답", method="build_payload", types=["Data"])
    ]

    def build_payload(self) -> Data:
        return Data(
            data=enrich_portal_contract(
                getattr(self, "payload", None),
                getattr(self, "authoring_payload", None),
            )
        )
