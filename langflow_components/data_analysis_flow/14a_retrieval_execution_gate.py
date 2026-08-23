# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 14A 필수 조회 실행 게이트
# 역할: 필수 source 조회 실패를 판정해 모델 응답 사용과 pandas 실행을 결정론적으로 차단합니다.
# 주요 입력: 조회 어댑터가 만든 페이로드 (payload) · 필수
# 주요 출력: 실행 제어 정보가 추가된 페이로드 (payload_out)
# 처리 흐름: retrieval job은 required=false가 명시된 경우만 선택 항목으로 보고, 그 외 누락·오류는 필수 실패로 처리합니다.
# 유지보수 포인트: 기본 Language Model은 실행되더라도 blocked 상태에서는 그 응답을 사용하지 않고 한 개의 최종 ChatOutput/API 경로를 유지합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data


# 주요 함수: 조회 결과와 검증 trace를 비교해 필수 실패는 blocked, 선택 실패는 continue 상태로 기록합니다.
def apply_retrieval_execution_gate(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    payload["trace"] = deepcopy(payload.get("trace")) if isinstance(payload.get("trace"), dict) else {}
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)] if isinstance(plan.get("retrieval_jobs"), list) else []
    source_results = [item for item in payload.get("source_results", []) if isinstance(item, dict)] if isinstance(payload.get("source_results"), list) else []
    result_by_alias = {
        _alias(item): item
        for item in source_results
        if _alias(item)
    }

    critical_failures: list[dict[str, Any]] = []
    optional_failures: list[dict[str, Any]] = []
    # Preserve a pre-retrieval safety block. Otherwise an empty job list can
    # overwrite a deliberate metadata failure and permit later model execution.
    preblocked_failures = _preblocked_failures(payload)
    metadata_preblocked = [
        item
        for item in preblocked_failures
        if str(item.get("type") or "") in {"table_catalog_metadata_unavailable", "unregistered_dataset_key"}
    ]
    # A deterministic metadata block is authoritative. Do not add the generic
    # catalog_hydration_failed validation error afterwards, otherwise the user
    # loses the actionable MongoDB/registration reason behind a vague retrieval
    # failure message.
    if metadata_preblocked:
        critical_failures.extend(metadata_preblocked)
    else:
        critical_failures.extend(preblocked_failures)
        critical_failures.extend(_validation_failures(payload))

    for job in jobs:
        alias = _alias(job)
        result = result_by_alias.get(alias)
        failure = _source_failure(job, result)
        if not failure:
            continue
        if _is_required(job):
            critical_failures.append(failure)
        else:
            optional_failures.append(failure)

    runtime_sources = (
        payload.get("runtime_sources")
        if isinstance(payload.get("runtime_sources"), dict)
        else {}
    )
    graph = (
        plan.get("resolved_execution_graph")
        if isinstance(plan.get("resolved_execution_graph"), dict)
        else {}
    )
    job_aliases = {_alias(job) for job in jobs}
    for requirement in graph.get("external_source_requirements", []):
        if not isinstance(requirement, dict):
            continue
        alias = str(requirement.get("source_alias") or "").strip()
        provider = str(requirement.get("provider") or "").strip()
        if not alias or alias in job_aliases or provider == "retrieval_job":
            continue
        if alias in runtime_sources:
            continue
        failure = {
            "type": "required_source_result_missing",
            "message": f"{provider} provider가 복원해야 할 source 결과가 없습니다: {alias}",
            "source_alias": alias,
            "dataset_key": str(requirement.get("dataset_key") or "").strip(),
            "provider": provider,
        }
        if requirement.get("required", True) is False:
            optional_failures.append(failure)
        else:
            critical_failures.append(failure)

    blocked = bool(critical_failures)
    validation_warnings = _validation_warnings(payload)
    gate = {
        "stage": "14a_retrieval_execution_gate",
        "status": "blocked" if blocked else "continue",
        "required_source_policy": "required_by_default",
        "critical_failures": critical_failures,
        "optional_failures": optional_failures,
        "recoverable_warnings": validation_warnings,
        "pandas_execution_allowed": not blocked,
        "model_response_policy": "ignore" if blocked else "use",
    }
    payload["execution_gate"] = deepcopy(gate)
    trace = payload.setdefault("trace", {})
    trace.setdefault("inspection", {})["retrieval_execution_gate"] = deepcopy(gate)

    if optional_failures:
        warning = {
            "type": "optional_source_retrieval_failed",
            "message": "선택 source 조회에 실패했지만 필수 source가 정상이라 분석을 계속합니다.",
            "sources": [item.get("source_alias") for item in optional_failures if item.get("source_alias")],
        }
        trace.setdefault("warnings", []).append(warning)

    if blocked:
        message = _blocked_message(critical_failures)
        execution_plan_only = _execution_plan_only_failures(critical_failures)
        schema_only = bool(critical_failures) and all(
            str(item.get("type") or "") == "source_schema_contract_unresolved"
            for item in critical_failures
        )
        metadata_contract_only = bool(critical_failures) and all(
            str(item.get("type") or "")
            in {"table_catalog_metadata_unavailable", "unregistered_dataset_key"}
            for item in critical_failures
        )
        error = {
            "type": (
                str(critical_failures[0].get("type") or "metadata_contract_blocked")
                if metadata_contract_only
                else (
                    "source_schema_contract_unresolved"
                    if schema_only
                    else (
                        "execution_plan_invalid"
                        if execution_plan_only
                        else "required_source_retrieval_failed"
                    )
                )
            ),
            "message": message,
            "failures": deepcopy(critical_failures),
        }
        payload["analysis"] = {
            "status": "error",
            "row_count": 0,
            "columns": [],
            "error": error,
            "errors": [message],
            "repairable_errors": [],
            "step_outputs": [],
            "function_case_results": [],
        }
        payload["data"] = {"columns": [], "rows": [], "row_count": 0, "data_ref": ""}
        payload["answer_message"] = message
        trace.setdefault("errors", []).append(error)
    return payload


# 함수 설명: `_validation_failures()`는 job validation과 trusted catalog hydration의 치명 오류를 실행 차단 사유로 바꿉니다.
def _preblocked_failures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    gate = payload.get("execution_gate") if isinstance(payload.get("execution_gate"), dict) else {}
    if str(gate.get("status") or "").strip().lower() != "blocked":
        return []
    failures = [
        deepcopy(item)
        for item in gate.get("critical_failures", [])
        if isinstance(item, dict)
    ]
    if failures:
        return failures
    if str(gate.get("reason") or "").strip() == "table_catalog_metadata_unavailable":
        return [
            {
                "type": "table_catalog_metadata_unavailable",
                "message": "Table Catalog 메타데이터를 확인하지 못해 분석을 실행하지 않았습니다.",
            }
        ]
    return []


# 함수 설명: 조회 검증·카탈로그 보정·스키마 해석 trace에서 실행을 막아야 하는 오류를 수집합니다.
def _validation_failures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    retrieval = inspection.get("data_retrieval") if isinstance(inspection.get("data_retrieval"), dict) else {}
    validation = retrieval.get("job_validation") if isinstance(retrieval.get("job_validation"), dict) else {}
    hydration = inspection.get("catalog_hydration") if isinstance(inspection.get("catalog_hydration"), dict) else {}
    schema_resolution = (
        inspection.get("source_schema_resolution")
        if isinstance(inspection.get("source_schema_resolution"), dict)
        else {}
    )
    failures: list[dict[str, Any]] = []
    duplicate_result_errors = [
        deepcopy(item)
        for item in trace.get("errors", [])
        if isinstance(item, dict)
        and item.get("type") == "duplicate_source_alias_result"
    ] if isinstance(trace.get("errors"), list) else []
    for item in duplicate_result_errors:
        failures.append(
            {
                "type": "duplicate_source_alias_result",
                "message": str(
                    item.get("message")
                    or "동일 source_alias의 조회 결과가 중복되었습니다."
                ),
                "source_aliases": deepcopy(item.get("source_aliases") or []),
            }
        )
    if _positive_int(validation.get("error_count")):
        validation_errors = [
            deepcopy(item)
            for item in validation.get("errors", [])
            if isinstance(item, dict)
        ] if isinstance(validation.get("errors"), list) else []
        issues = []
        for item in validation_errors:
            for issue in item.get("issues", []) if isinstance(item.get("issues"), list) else []:
                text = str(issue or "").strip()
                if text and text not in issues:
                    issues.append(text)
        failure = {
            "type": "retrieval_job_validation_failed",
            "message": "데이터 조회 작업 검증에 실패했습니다.",
            "error_count": int(validation.get("error_count") or 0),
        }
        if validation_errors:
            failure["validation_errors"] = validation_errors
        if issues:
            failure["issues"] = issues
        failures.append(failure)
    if str(hydration.get("status") or "").strip().lower() == "error":
        failures.append(
            {
                "type": "catalog_hydration_failed",
                "message": "신뢰 카탈로그에서 필수 조회 설정을 구성하지 못했습니다.",
            }
        )
    if str(schema_resolution.get("status") or "").strip().lower() == "error":
        unresolved_sources = [
            {
                "source_alias": str(item.get("source_alias") or ""),
                "dataset_key": str(item.get("dataset_key") or ""),
                "unresolved_required_columns": deepcopy(
                    item.get("unresolved_required_columns") or []
                ),
                "observed_columns": deepcopy(item.get("observed_columns") or []),
                "source_bindings": deepcopy(item.get("source_bindings") or {}),
            }
            for item in schema_resolution.get("sources", [])
            if isinstance(item, dict)
            and str(item.get("status") or "").strip().lower() in {"partial", "unresolved"}
        ]
        failures.append(
            {
                "type": "source_schema_contract_unresolved",
                "message": "필수 표준 컬럼을 실제 조회 컬럼으로 확정하지 못했습니다.",
                "sources": unresolved_sources,
            }
        )
    return failures


# 함수 설명: 조회 작업 검증기가 비차단으로 분류한 경고를 실행 게이트 trace에 보존합니다.
def _validation_warnings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    retrieval = inspection.get("data_retrieval") if isinstance(inspection.get("data_retrieval"), dict) else {}
    validation = retrieval.get("job_validation") if isinstance(retrieval.get("job_validation"), dict) else {}
    return [
        deepcopy(item)
        for item in validation.get("warnings", [])
        if isinstance(item, dict)
    ] if isinstance(validation.get("warnings"), list) else []


# 함수 설명: `_source_failure()`는 job에 대응하는 source result의 누락·명시 오류를 표준 실패 정보로 만듭니다.
def _source_failure(job: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    alias = _alias(job)
    dataset_key = str(job.get("dataset_key") or "").strip()
    if result is None:
        return {
            "type": "required_source_result_missing" if _is_required(job) else "optional_source_result_missing",
            "message": f"source 결과가 없습니다: {alias or dataset_key}",
            "source_alias": alias,
            "dataset_key": dataset_key,
        }
    status = str(result.get("status") or "ok").strip().lower()
    errors = [item for item in result.get("errors", []) if isinstance(item, dict)] if isinstance(result.get("errors"), list) else []
    if result.get("success") is False or status in {"error", "failed", "failure", "invalid", "skipped"} or errors:
        return {
            "type": "source_retrieval_failed",
            "message": str(result.get("error_message") or _first_error_message(errors) or f"source 조회 실패: {alias or dataset_key}"),
            "source_alias": alias,
            "dataset_key": dataset_key,
            "source_type": result.get("source_type") or job.get("source_type"),
            "errors": deepcopy(errors),
        }
    return {}


# 함수 설명: `_is_required()`는 required=false가 명시된 job만 선택 source로 취급합니다.
def _is_required(job: dict[str, Any]) -> bool:
    value = job.get("required", True)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"false", "0", "no", "off", "optional", "선택"}


# 함수 설명: `_blocked_message()`는 사용자와 운영자가 실패 source를 바로 확인할 수 있는 결정론적 메시지를 만듭니다.
def _blocked_message(failures: list[dict[str, Any]]) -> str:
    if failures and all(
        str(item.get("type") or "")
        in {"table_catalog_metadata_unavailable", "unregistered_dataset_key"}
        for item in failures
    ):
        return str(
            failures[0].get("message")
            or "Table Catalog 메타데이터를 확인하지 못해 분석을 시작하지 않았습니다."
        )
    if failures and all(
        str(item.get("type") or "") == "source_schema_contract_unresolved"
        for item in failures
    ):
        details: list[str] = []
        for failure in failures:
            for source in failure.get("sources", []) if isinstance(failure.get("sources"), list) else []:
                if not isinstance(source, dict):
                    continue
                alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
                columns = [str(item) for item in source.get("unresolved_required_columns", [])]
                if alias and columns:
                    details.append(f"{alias}({', '.join(columns)})")
        suffix = f" 미확정 컬럼: {'; '.join(details)}." if details else ""
        return "필수 실행 컬럼 계약을 확정하지 못해 pandas 분석을 실행하지 않았습니다." + suffix
    plan_validation_errors = _execution_plan_validation_errors(failures)
    if any(
        str(item.get("type") or "")
        == "same_run_dependent_retrieval_unsupported"
        for item in plan_validation_errors
    ) and not any(
        str(item.get("type") or "") in {"source_retrieval_failed", "required_source_result_missing"}
        for item in failures
        if isinstance(item, dict)
    ):
        return (
            "첫 조회 결과의 식별자를 다음 조회의 필수 조건으로 사용해야 하는 요청입니다. "
            "Flow 01은 조회 작업을 단일 단계로 실행하므로, 먼저 식별자를 조회한 뒤 "
            "해당 식별자를 포함해 새 질문으로 나눠 실행해 주세요."
        )
    if _execution_plan_only_failures(failures):
        if all(
            str(item.get("type") or "")
            in {
                "same_run_dependent_retrieval_unsupported",
                "required_retrieval_parameter_unresolved",
            }
            for item in plan_validation_errors
        ):
            split_execution_required = any(
                str(item.get("type") or "")
                == "same_run_dependent_retrieval_unsupported"
                for item in plan_validation_errors
            )
            if split_execution_required:
                return (
                    "분석 계획에서 앞선 조회 결과를 다음 조회의 필수 조건으로 사용하려 했습니다. "
                    "Flow 01에서는 먼저 식별자를 조회한 뒤 해당 식별자를 포함한 새 질문으로 나눠 실행해야 합니다."
                )
            return "Table Catalog에서 필수로 지정한 조회 조건 값이 비어 있어 분석을 시작하지 않았습니다."
        details = _execution_plan_failure_details(failures)
        suffix = f" 확인 필요: {', '.join(details)}." if details else ""
        return (
            "분석 실행 계획의 단계 연결 또는 컬럼 소유권을 확정하지 못해 "
            "데이터 조회를 시작하지 않았습니다."
            + suffix
        )
    aliases = []
    for item in failures:
        alias = str(item.get("source_alias") or item.get("dataset_key") or "").strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    suffix = f" 실패 source: {', '.join(aliases)}." if aliases else ""
    return "필수 데이터 조회에 실패하여 pandas 분석을 실행하지 않았고 모델 응답도 사용하지 않았습니다." + suffix


# 함수 설명: 조회 자체의 실패와 실행 계획 연결·컬럼 소유권 검증 실패를 구분합니다.
def _execution_plan_only_failures(failures: list[dict[str, Any]]) -> bool:
    """Recognize plan compilation failures without relabeling them as retrieval errors."""

    if not failures:
        return False
    allowed_types = {
        "unresolved_execution_input",
        "invalid_metric_source_contract",
        "catalog_metric_ownership_mismatch",
        "execution_plan_invalid",
        "same_run_dependent_retrieval_unsupported",
        "required_retrieval_parameter_unresolved",
    }
    observed: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            return False
        failure_type = str(failure.get("type") or "").strip()
        if failure_type in allowed_types:
            observed.append(failure)
            continue
        if failure_type != "retrieval_job_validation_failed":
            return False
        validation_errors = [
            item
            for item in failure.get("validation_errors", [])
            if isinstance(item, dict)
        ]
        if not validation_errors:
            return False
        if any(
            str(item.get("type") or "").strip() not in allowed_types
            for item in validation_errors
        ):
            return False
        observed.extend(validation_errors)
    return bool(observed)


# 함수 설명: `_execution_plan_validation_errors()`는 중첩된 실행 계획 검증 오류를 하나의 목록으로 펼칩니다.
def _execution_plan_validation_errors(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten gate failures to their typed plan-validation entries."""

    result: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        if str(failure.get("type") or "") == "retrieval_job_validation_failed":
            result.extend(
                item
                for item in failure.get("validation_errors", [])
                if isinstance(item, dict)
            )
        else:
            result.append(failure)
    return result


# 함수 설명: 실행 계획 오류에서 사용자에게 확인할 단계와 입력 별칭만 추려 반환합니다.
def _execution_plan_failure_details(failures: list[dict[str, Any]]) -> list[str]:
    details: list[str] = []
    for failure in failures:
        entries = (
            failure.get("validation_errors", [])
            if isinstance(failure, dict)
            and str(failure.get("type") or "").strip()
            == "retrieval_job_validation_failed"
            else [failure]
        )
        for item in entries:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("node_id") or "").strip()
            reference = str(
                item.get("node_output_ref") or item.get("source_alias") or ""
            ).strip()
            if node_id and reference:
                detail = f"{node_id} ← {reference}"
            elif reference:
                detail = reference
            else:
                continue
            if detail not in details:
                details.append(detail)
    return details


# 함수 설명: `_first_error_message()`는 source errors 배열에서 첫 번째 사람이 읽을 수 있는 메시지를 반환합니다.
def _first_error_message(errors: list[dict[str, Any]]) -> str:
    for item in errors:
        message = str(item.get("message") or "").strip()
        if message:
            return message
    return ""


# 함수 설명: `_alias()`는 source_alias가 없으면 dataset_key를 사용해 job/result identity를 맞춥니다.
def _alias(value: dict[str, Any]) -> str:
    return str(value.get("source_alias") or value.get("dataset_key") or "").strip()


# 함수 설명: `_positive_int()`는 검증 오류 개수가 0보다 큰지 안전하게 확인합니다.
def _positive_int(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except Exception:
        return False


# 함수 설명: `_payload()`는 대용량 runtime_sources 행은 공유하고 변경하는 최상위 key만 분리하도록 얕은 복사합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return dict(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: 단일 output을 사용해 stop/merge 없이 downstream control payload를 전달합니다.
class RetrievalExecutionGate(Component):
    display_name = "14A 필수 조회 실행 게이트"
    description = "필수 source 조회 실패 시 모델 응답 사용과 pandas 실행을 차단하는 control payload를 만듭니다."
    inputs = [DataInput(name="payload", display_name="조회 페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="실행 제어 페이로드", method="build_payload")]

    # Langflow 출력 함수: 필수/선택 source 상태를 평가한 단일 페이로드를 다음 선형 경로로 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=apply_retrieval_execution_gate(getattr(self, "payload", None)))
