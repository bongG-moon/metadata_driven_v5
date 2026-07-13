# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04A 신뢰 카탈로그 조회 작업 구성기
# 역할: LLM job의 source 설정을 버리고 active table catalog의 신뢰 가능한 설정으로 다시 구성합니다.
# 주요 입력: 의도 페이로드 (payload) · 필수, 전체 테이블 카탈로그 (table_catalog_items) · 필수, 데이터 조회 모드 (retrieval_mode)
# 주요 출력: 신뢰 조회 작업 페이로드 (payload_out)
# 처리 흐름: LLM이 제안한 데이터셋 키를 활성 카탈로그와 다시 대조해 신뢰할 수 있는 source 설정과 필수 파라미터만 복원합니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, DropdownInput, Output
from lfx.schema.data import Data

UNTRUSTED_JOB_KEYS = {
    "source_type",
    "source_config",
    "db_key",
    "query_template",
    "sql_template",
    "oracle_sql",
    "sql",
    "query",
    "endpoint",
    "url",
    "api_url",
    "headers",
}
SECRET_KEYS = {
    "password",
    "passwd",
    "pw",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "mongo_uri",
    "mongodb_uri",
}
# 주요 함수: 활성 카탈로그를 기준으로 조회 작업의 source 설정을 다시 구성합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def hydrate_retrieval_jobs(
    payload_value: Any,
    table_catalog_items_value: Any = None,
    retrieval_mode: Any = "dummy",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    next_payload = payload
    plan = _dict(next_payload.get("intent_plan"))
    jobs = _list(plan.get("retrieval_jobs"))
    catalog_items = _catalog_items(table_catalog_items_value)
    catalog_index = {
        key: item
        for item in catalog_items
        if (key := _dataset_key(item))
    }
    mode = _mode(retrieval_mode)
    request = next_payload.get("request")
    if not isinstance(request, dict):
        request = {}
        next_payload["request"] = request
    request["retrieval_mode"] = mode

    hydrated: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    used_refs: list[dict[str, Any]] = []
    shared_param_values, conflicting_shared_param_names = _explicit_shared_required_param_values(plan, jobs)
    propagated_required_params: dict[str, dict[str, str]] = {}
    if conflicting_shared_param_names:
        warnings.append(
            _issue(
                "conflicting_shared_required_params",
                "공통 필수 파라미터 값이 job별 값과 충돌하여 자동 전파하지 않았습니다.",
                param_names=sorted(conflicting_shared_param_names),
            )
        )

    for index, raw_job in enumerate(jobs):
        if not isinstance(raw_job, dict):
            errors.append(_issue("invalid_retrieval_job", "retrieval job이 object가 아닙니다.", index=index))
            continue
        dataset_key = str(raw_job.get("dataset_key") or "").strip()
        clean_job = {
            str(key): deepcopy(value)
            for key, value in raw_job.items()
            if str(key) not in UNTRUSTED_JOB_KEYS
        }
        catalog_item = catalog_index.get(dataset_key)
        if not catalog_item:
            issue = _issue(
                "unknown_dataset_key",
                f"active table catalog에서 dataset_key를 찾지 못했습니다: {dataset_key or '(empty)'}",
                dataset_key=dataset_key,
                index=index,
            )
            if mode == "live":
                errors.append(issue)
                continue
            warnings.append({**issue, "message": issue["message"] + " dummy mode에서는 source_config 없이 계속합니다."})
            clean_job["source_type"] = "dummy"
            clean_job["trusted_catalog"] = False
            clean_job["dummy_only"] = True
            hydrated.append(clean_job)
            continue

        catalog_payload = _dict(catalog_item.get("payload")) or catalog_item
        source_type = str(catalog_payload.get("source_type") or catalog_item.get("source_type") or "").strip()
        source_config = _sanitize_trusted_config(
            _dict(catalog_payload.get("source_config")) or _dict(catalog_item.get("source_config"))
        )
        required_names = _required_param_names(catalog_payload, source_config, catalog_item)
        raw_supplied_params = _dict(clean_job.get("required_params")) or _dict(clean_job.get("params"))
        supplied_params: dict[str, Any] = {}
        for name in required_names:
            supplied_value = _param_value(raw_supplied_params, name)
            if supplied_value not in (None, "", [], {}):
                supplied_params[str(name)] = deepcopy(supplied_value)
        clean_job.pop("params", None)
        propagated_for_job: dict[str, str] = {}
        for name in required_names:
            if _param_value(supplied_params, name) not in (None, "", [], {}):
                continue
            normalized_name = _normalize_param_name(name)
            propagated_value = None
            propagation_source = ""
            if normalized_name not in conflicting_shared_param_names:
                propagated_value = shared_param_values.get(normalized_name)
                if propagated_value not in (None, "", [], {}):
                    propagation_source = "shared_required_params"
            if propagated_value in (None, "", [], {}):
                continue
            _set_param_value(supplied_params, name, propagated_value)
            propagated_for_job[name] = propagation_source
        if required_names or supplied_params:
            clean_job["required_params"] = supplied_params
        missing_params = [name for name in required_names if _param_value(supplied_params, name) in (None, "", [], {})]

        clean_job["source_type"] = source_type or str(clean_job.get("source_type") or "")
        clean_job["source_config"] = source_config
        clean_job["required_param_names"] = required_names
        clean_job["trusted_catalog"] = True
        clean_job["catalog_ref"] = f"table_catalog:{dataset_key}"
        if missing_params:
            warnings.append(
                _issue(
                    "missing_catalog_required_params",
                    f"catalog 필수 파라미터 값이 없습니다: {', '.join(missing_params)}",
                    dataset_key=dataset_key,
                    missing_params=missing_params,
                )
            )
        hydrated.append(clean_job)
        used_refs.append({"type": "table_catalog", "key": dataset_key})
        if propagated_for_job:
            propagated_required_params[str(clean_job.get("source_alias") or dataset_key)] = propagated_for_job

    plan["retrieval_jobs"] = hydrated
    # 공통 binding은 job별 값으로 materialize한 뒤 제거해 후속 payload 중복과 재전파를 막습니다.
    plan.pop("shared_required_params", None)
    next_payload["intent_plan"] = plan
    next_payload["metadata_refs"] = _merge_refs(_list(next_payload.get("metadata_refs")), used_refs)
    trace = next_payload.setdefault("trace", {})
    trace.setdefault("warnings", []).extend(warnings)
    trace.setdefault("errors", []).extend(errors)
    trace.setdefault("inspection", {})["catalog_hydration"] = {
        "stage": "04a_trusted_retrieval_job_hydrator",
        "status": "error" if errors else ("warning" if warnings else "ok"),
        "retrieval_mode": mode,
        "input_job_count": len(jobs),
        "hydrated_job_count": len(hydrated),
        "catalog_item_count": len(catalog_items),
        "trusted_dataset_keys": [job.get("dataset_key") for job in hydrated if job.get("trusted_catalog")],
        "dummy_only_dataset_keys": [job.get("dataset_key") for job in hydrated if job.get("dummy_only")],
        "shared_required_param_names": sorted(shared_param_values),
        "conflicting_shared_required_param_names": sorted(conflicting_shared_param_names),
        "propagated_required_params": propagated_required_params,
    }
    return next_payload


# 함수 설명: `_catalog_items()`는 MongoDB 로드 결과에서 active 테이블 카탈로그 항목만 안전하게 꺼냅니다.
def _catalog_items(value: Any) -> list[dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, list):
        return [deepcopy(item) for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    items = data.get("table_catalog_items")
    if not isinstance(items, list) and isinstance(data.get("metadata_candidates"), dict):
        items = data["metadata_candidates"].get("table_catalog_items")
    return [deepcopy(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


# 함수 설명: `_dataset_key()`는 key 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _dataset_key(item: dict[str, Any]) -> str:
    payload = _dict(item.get("payload"))
    return str(item.get("dataset_key") or item.get("key") or payload.get("dataset_key") or payload.get("key") or "").strip()


# 함수 설명: `_required_param_names()`는 카탈로그 설정에서 실행 전에 반드시 있어야 하는 파라미터 이름을 추출합니다.
def _required_param_names(*values: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        raw = value.get("required_params") or value.get("required_param_names") or []
        if isinstance(raw, dict):
            raw = list(raw)
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        for item in raw:
            if isinstance(item, dict):
                item = item.get("name") or item.get("key")
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result


# 함수 설명: `_explicit_shared_required_param_values()`는 planner가 공통 scope로 명시한 값만 전파 대상으로 인정하고 job별 값과의 충돌을 차단합니다.
def _explicit_shared_required_param_values(plan: dict[str, Any], jobs: list[Any]) -> tuple[dict[str, Any], set[str]]:
    raw_shared = _dict(plan.get("shared_required_params"))
    shared = {
        normalized_name: deepcopy(value)
        for name, value in raw_shared.items()
        if (normalized_name := _normalize_param_name(name)) and value not in (None, "", [], {})
    }
    conflicts: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        params = _dict(job.get("required_params")) or _dict(job.get("params"))
        for name, value in params.items():
            if value in (None, "", [], {}):
                continue
            normalized_name = _normalize_param_name(name)
            if normalized_name in shared and value != shared[normalized_name]:
                conflicts.add(normalized_name)
    for name in conflicts:
        shared.pop(name, None)
    return shared, conflicts


# 함수 설명: `_param_value()`는 required parameter key의 대소문자 차이를 허용해 현재 값을 찾습니다.
def _param_value(params: dict[str, Any], name: Any) -> Any:
    normalized_name = _normalize_param_name(name)
    for key, value in params.items():
        if _normalize_param_name(key) == normalized_name:
            return value
    return None


# 함수 설명: `_set_param_value()`는 기존 key 표기를 보존하면서 공통 required parameter 값을 채웁니다.
def _set_param_value(params: dict[str, Any], name: Any, value: Any) -> None:
    normalized_name = _normalize_param_name(name)
    target_name = str(name or "").strip()
    for key in params:
        if _normalize_param_name(key) == normalized_name:
            target_name = str(key)
            break
    if target_name:
        params[target_name] = deepcopy(value)


# 함수 설명: `_normalize_param_name()`은 required parameter 이름을 대소문자와 공백 차이가 없는 비교 key로 변환합니다.
def _normalize_param_name(value: Any) -> str:
    return str(value or "").strip().upper()


# 함수 설명: `_sanitize_trusted_config()`는 trusted·설정에서 비밀값·내부 필드·직렬화 불가 값을 제거하거나 마스킹합니다.
def _sanitize_trusted_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_trusted_config(item)
            for key, item in value.items()
            if str(key).lower() not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_trusted_config(item) for item in value]
    return deepcopy(value)


# 함수 설명: `_merge_refs()`는 여러 참조 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다.
def _merge_refs(existing: list[Any], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in [*existing, *additions]:
        if not isinstance(value, dict):
            continue
        ref_type = str(value.get("type") or value.get("section") or "").strip()
        key = str(value.get("key") or value.get("dataset_key") or "").strip()
        marker = (ref_type, key)
        if not key or marker in seen:
            continue
        seen.add(marker)
        result.append(deepcopy(value))
    return result


# 함수 설명: `_issue()`는 조회 작업 hydration 중 발견한 문제를 type·dataset·message 구조로 만듭니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": message, **extra}


# 함수 설명: `_mode()`는 retrieval_mode 입력을 dummy/live 중 하나로 정규화합니다.
def _mode(value: Any) -> str:
    return "live" if str(value or "").strip().lower() in {"live", "real", "actual", "true", "1"} else "dummy"


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


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class TrustedRetrievalJobHydrator(Component):
    display_name = "04A 신뢰 카탈로그 조회 작업 구성기"
    description = "LLM job의 source 설정을 버리고 active table catalog의 신뢰 가능한 설정으로 다시 구성합니다."
    inputs = [
        DataInput(name="payload", display_name="의도 페이로드", required=True),
        DataInput(name="table_catalog_items", display_name="전체 테이블 카탈로그", required=True),
        DropdownInput(name="retrieval_mode", display_name="데이터 조회 모드", options=["dummy", "live"], value="dummy"),
    ]
    outputs = [Output(name="payload_out", display_name="신뢰 조회 작업 페이로드", method="build_payload")]

    # Langflow 출력 함수: '신뢰 조회 작업 페이로드 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=hydrate_retrieval_jobs(
                getattr(self, "payload", None),
                getattr(self, "table_catalog_items", None),
                getattr(self, "retrieval_mode", "dummy"),
            )
        )
