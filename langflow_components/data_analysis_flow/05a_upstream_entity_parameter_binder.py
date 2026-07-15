# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05A 상위 결과 파라미터 바인더
# 역할: Route V3가 전달한 상위 분석 결과의 식별자를 신뢰 카탈로그 규칙에 따라 다음 조회 작업의 필수 파라미터로 연결합니다.
# 주요 입력: 상위 결과와 신뢰 조회 작업이 포함된 페이로드 (payload) · 필수
# 주요 출력: 파라미터가 연결된 페이로드 (payload_out)
# 처리 흐름: upstream_result 전체 행을 읽고 source_config.upstream_bindings를 검증한 뒤 required_params에 식별자 값을 주입합니다.
# 유지보수 포인트: LLM이 만든 binding은 사용하지 않으며 누락·중복·상한 초과 시 조회 작업을 실행 불가능 상태로 바꾸는 fail-closed 정책을 유지합니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

UPSTREAM_SOURCE_ALIAS = "upstream_result"
DEFAULT_MAX_VALUES = 200
MAX_ALLOWED_VALUES = 10_000
SUPPORTED_OPERATORS = {"in", "eq"}
BLOCKED_SOURCE_TYPE = "upstream_binding_blocked"


# 주요 함수: 명시적 orchestration 요청에서만 상위 결과 식별자를 다음 retrieval job의 required_params로 연결합니다.
def bind_upstream_entity_parameters(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    upstream_ref = _ref_id(orchestration.get("upstream_result_ref"))
    if not upstream_ref:
        # 기존 단일/후속 분석에서는 payload와 trace를 전혀 바꾸지 않습니다.
        return payload

    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)] if isinstance(plan.get("retrieval_jobs"), list) else []
    if not jobs:
        _record_inspection(payload, "skipped", [], [], reason="no_retrieval_jobs")
        return payload

    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    upstream_rows = runtime_sources.get(UPSTREAM_SOURCE_ALIAS)
    errors: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    bound_jobs: list[dict[str, Any]] = []

    if str(orchestration.get("status") or "").lower() != "ok" or not isinstance(upstream_rows, list) or not upstream_rows:
        errors.append(
            _issue(
                "upstream_result_unavailable",
                "MongoDB에서 완전한 상위 결과를 복원하지 못해 후속 데이터 조회를 차단했습니다.",
            )
        )
    else:
        for index, job in enumerate(jobs):
            bound_job, job_summaries, job_errors = _bind_job(job, upstream_rows, index)
            bound_jobs.append(bound_job)
            summaries.extend(job_summaries)
            errors.extend(job_errors)

    # 하나라도 binding이 불완전하면 일부 조회만 먼저 실행되는 일을 막기 위해 모든 job을 검증 불가 상태로 바꿉니다.
    # 다음 06 검증기가 이 source_type을 거부하므로 broad query가 실제 어댑터까지 도달하지 않습니다.
    if errors:
        plan["retrieval_jobs"] = [_blocked_job(job) for job in jobs]
    else:
        plan["retrieval_jobs"] = bound_jobs
    payload["intent_plan"] = plan

    trace = payload.setdefault("trace", {})
    trace.setdefault("errors", []).extend(deepcopy(errors))
    status = "error" if errors else "ok"
    _record_inspection(payload, status, summaries, errors)
    orchestration["binding_status"] = status
    orchestration["bound_job_count"] = len(bound_jobs) if not errors else 0
    payload["orchestration"] = orchestration
    return payload


# 함수 설명: `_bind_job()`은 단일 신뢰 조회 작업의 binding 목록을 검증하고 충돌 없이 파라미터를 채웁니다.
def _bind_job(
    job: dict[str, Any],
    upstream_rows: list[Any],
    job_index: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    next_job = deepcopy(job)
    dataset_key = str(next_job.get("dataset_key") or "").strip()
    if next_job.get("trusted_catalog") is not True:
        return next_job, [], [
            _issue(
                "untrusted_upstream_binding",
                "신뢰 카탈로그에서 구성되지 않은 조회 작업에는 상위 결과를 연결할 수 없습니다.",
                dataset_key=dataset_key,
                index=job_index,
            )
        ]

    source_config = next_job.get("source_config") if isinstance(next_job.get("source_config"), dict) else {}
    bindings = source_config.get("upstream_bindings")
    if not isinstance(bindings, list) or not bindings:
        return next_job, [], [
            _issue(
                "upstream_binding_missing",
                "table catalog source_config에 upstream_bindings가 없어 상위 결과 기반 조회를 차단했습니다.",
                dataset_key=dataset_key,
                index=job_index,
            )
        ]

    params = deepcopy(next_job.get("required_params")) if isinstance(next_job.get("required_params"), dict) else {}
    summaries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    target_params: set[str] = set()

    for binding_index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            errors.append(
                _issue(
                    "invalid_upstream_binding",
                    "upstream_bindings 항목은 object여야 합니다.",
                    dataset_key=dataset_key,
                    index=job_index,
                    binding_index=binding_index,
                )
            )
            continue
        binding_errors, summary, target_param, bound_value = _resolve_binding(
            binding,
            upstream_rows,
            dataset_key,
            job_index,
            binding_index,
        )
        errors.extend(binding_errors)
        if binding_errors:
            continue
        target_marker = target_param.casefold()
        if target_marker in target_params:
            errors.append(
                _issue(
                    "ambiguous_upstream_binding",
                    f"같은 target_param에 둘 이상의 binding이 선언되었습니다: {target_param}",
                    dataset_key=dataset_key,
                    index=job_index,
                    binding_index=binding_index,
                )
            )
            continue
        target_params.add(target_marker)
        existing_key = _dict_key_ci(params, target_param)
        if existing_key and not _same_value(params.get(existing_key), bound_value):
            errors.append(
                _issue(
                    "upstream_parameter_conflict",
                    f"기존 required_params 값과 상위 결과 binding 값이 충돌합니다: {target_param}",
                    dataset_key=dataset_key,
                    index=job_index,
                    binding_index=binding_index,
                )
            )
            continue
        params[existing_key or target_param] = deepcopy(bound_value)
        summaries.append(summary)

    if not errors:
        next_job["required_params"] = params
        next_job["upstream_binding_applied"] = True
        next_job["upstream_source_alias"] = UPSTREAM_SOURCE_ALIAS
    return next_job, summaries, errors


# 함수 설명: `_resolve_binding()`은 한 binding의 컬럼·연산자·최대값을 검사하고 실제 주입 값과 추적 요약을 반환합니다.
def _resolve_binding(
    binding: dict[str, Any],
    upstream_rows: list[Any],
    dataset_key: str,
    job_index: int,
    binding_index: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], str, Any]:
    entity_type = str(binding.get("entity_type") or "").strip()
    source_column = str(binding.get("source_column") or "").strip()
    target_param = str(binding.get("target_param") or "").strip()
    source_alias = str(binding.get("source_alias") or UPSTREAM_SOURCE_ALIAS).strip()
    operator = str(binding.get("operator") or "in").strip().lower()
    location = {"dataset_key": dataset_key, "index": job_index, "binding_index": binding_index}
    errors: list[dict[str, Any]] = []

    if not entity_type or not source_column or not target_param:
        errors.append(
            _issue(
                "invalid_upstream_binding",
                "upstream binding에는 entity_type, source_column, target_param이 모두 필요합니다.",
                **location,
            )
        )
    if source_alias != UPSTREAM_SOURCE_ALIAS:
        errors.append(
            _issue(
                "unsupported_upstream_source_alias",
                f"현재 지원하는 상위 source alias는 {UPSTREAM_SOURCE_ALIAS} 하나입니다.",
                **location,
            )
        )
    if operator not in SUPPORTED_OPERATORS:
        errors.append(
            _issue(
                "unsupported_upstream_operator",
                f"지원하지 않는 upstream binding operator입니다: {operator}",
                **location,
            )
        )
    if errors:
        return errors, {}, target_param, None

    actual_columns = _matching_columns(upstream_rows, source_column)
    if not actual_columns:
        return [
            _issue(
                "upstream_source_column_missing",
                f"상위 결과에 binding source_column이 없습니다: {source_column}",
                **location,
            )
        ], {}, target_param, None
    if len(actual_columns) > 1:
        return [
            _issue(
                "ambiguous_upstream_source_column",
                f"대소문자만 다른 상위 결과 컬럼이 둘 이상 존재합니다: {', '.join(actual_columns)}",
                **location,
            )
        ], {}, target_param, None

    values, invalid_value = _unique_scalar_values(upstream_rows, actual_columns[0])
    if invalid_value:
        return [
            _issue(
                "invalid_upstream_entity_value",
                f"상위 식별자 컬럼에 scalar가 아닌 값이 포함되어 있습니다: {actual_columns[0]}",
                **location,
            )
        ], {}, target_param, None
    if not values:
        return [
            _issue(
                "upstream_entity_values_missing",
                f"상위 식별자 컬럼에 전달할 값이 없습니다: {actual_columns[0]}",
                **location,
            )
        ], {}, target_param, None

    max_values = _max_values(binding.get("max_values"))
    if len(values) > max_values:
        return [
            _issue(
                "upstream_entity_limit_exceeded",
                f"상위 식별자 {len(values)}개가 catalog 상한 {max_values}개를 초과했습니다.",
                value_count=len(values),
                max_values=max_values,
                **location,
            )
        ], {}, target_param, None
    if operator == "eq" and len(values) != 1:
        return [
            _issue(
                "ambiguous_upstream_entity_value",
                f"eq binding에는 식별자가 정확히 1개여야 하지만 {len(values)}개입니다.",
                value_count=len(values),
                **location,
            )
        ], {}, target_param, None

    bound_value = values[0] if operator == "eq" else values
    summary = {
        "dataset_key": dataset_key,
        "entity_type": entity_type,
        "source_alias": source_alias,
        "source_column": actual_columns[0],
        "target_param": target_param,
        "operator": operator,
        "value_count": len(values),
        "max_values": max_values,
    }
    return [], summary, target_param, bound_value


# 함수 설명: `_matching_columns()`는 전체 상위 행에서 요청 컬럼과 대소문자 무시 기준으로 일치하는 실제 컬럼명을 찾습니다.
def _matching_columns(rows: list[Any], requested: str) -> list[str]:
    result: list[str] = []
    marker = requested.casefold()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            text = str(key)
            if text.casefold() == marker and text not in result:
                result.append(text)
    return result


# 함수 설명: `_unique_scalar_values()`는 순서를 유지하며 중복을 제거하고 dict/list 같은 위험한 파라미터 값은 거부합니다.
def _unique_scalar_values(rows: list[Any], column: str) -> tuple[list[Any], bool]:
    result: list[Any] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(column)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            return [], True
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result, False


# 함수 설명: `_blocked_job()`은 binding 오류가 난 요청이 실제 source 어댑터에 도달하지 않도록 06 검증기에서 거부될 표시를 남깁니다.
def _blocked_job(job: dict[str, Any]) -> dict[str, Any]:
    blocked = deepcopy(job)
    blocked["upstream_binding_original_source_type"] = blocked.get("source_type")
    blocked["source_type"] = BLOCKED_SOURCE_TYPE
    blocked["upstream_binding_blocked"] = True
    return blocked


# 함수 설명: `_max_values()`는 catalog 설정값을 보수적인 기본값과 절대 상한 안의 양의 정수로 정규화합니다.
def _max_values(value: Any) -> int:
    try:
        parsed = int(str(value).strip()) if str(value or "").strip() else DEFAULT_MAX_VALUES
    except Exception:
        parsed = DEFAULT_MAX_VALUES
    return max(1, min(parsed, MAX_ALLOWED_VALUES))


# 함수 설명: `_dict_key_ci()`는 required_params 안에서 대소문자만 다른 기존 키를 찾아 중복 키 생성을 막습니다.
def _dict_key_ci(value: dict[str, Any], key: str) -> str:
    marker = key.casefold()
    for existing in value:
        if str(existing).casefold() == marker:
            return str(existing)
    return ""


# 함수 설명: `_same_value()`는 기존 파라미터와 binding 값이 의미상 같은 JSON 값인지 비교합니다.
def _same_value(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


# 함수 설명: `_record_inspection()`은 실제 식별자 값을 복제하지 않고 적용 건수와 오류만 trace에 기록합니다.
def _record_inspection(
    payload: dict[str, Any],
    status: str,
    summaries: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    reason: str = "",
) -> None:
    payload.setdefault("trace", {}).setdefault("inspection", {})["upstream_parameter_binding"] = {
        "stage": "05a_upstream_entity_parameter_binder",
        "status": status,
        "reason": reason,
        "binding_count": len(summaries),
        "bindings": deepcopy(summaries),
        "error_count": len(errors),
        "errors": deepcopy(errors),
    }


# 함수 설명: `_issue()`는 binding 오류를 표준 type/message와 위치 정보가 있는 dict로 만듭니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": message, **extra}


# 함수 설명: `_ref_id()`는 문자열 또는 표준 data_ref object에서 실제 MongoDB 참조 ID를 추출합니다.
def _ref_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("ref_id") or value.get("data_ref") or value.get("_id") or "").strip()
    return str(value or "").strip()


# 함수 설명: `_payload()`는 입력 Data를 독립적으로 수정할 수 있도록 표준 dict 복사본으로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: standalone 캔버스에서 한 입력·한 출력의 결정론적 파라미터 바인딩 노드로 사용합니다.
class UpstreamEntityParameterBinder(Component):
    display_name = "05A 상위 결과 파라미터 바인더"
    description = "상위 분석 결과의 식별자를 신뢰 카탈로그 규칙으로 다음 조회 작업 파라미터에 연결합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: binding 검증과 주입이 완료된 단일 페이로드를 다음 조회 검증기로 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=bind_upstream_entity_parameters(getattr(self, "payload", None)))
