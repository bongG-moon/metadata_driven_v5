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
from datetime import date, datetime
import re
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
    "row_identity_columns",
    "default_detail_columns",
    "context_columns",
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
SAFE_CATALOG_JOB_KEYS = (
    "filter_mappings",
    "standard_column_aliases",
    "default_detail_columns",
    "metric_semantics",
)
RETIRED_OUTPUT_CONTRACT_KEYS = {"row_identity_columns", "context_columns", "default_detail_columns"}


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
    deferred_upstream_params: list[dict[str, Any]] = []
    condition_reconciliation: list[dict[str, Any]] = []
    orchestration = _dict(next_payload.get("orchestration"))
    explicit_upstream = bool(str(orchestration.get("upstream_result_ref") or "").strip())
    reference_mode = str(plan.get("reference_mode") or "").strip()
    deferred_binding_aliases: set[str] = set()
    if explicit_upstream:
        deferred_binding_aliases.add("upstream_result")
    if reference_mode == "previous_result_rows":
        deferred_binding_aliases.add("previous_result")

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
        # 일부 standalone catalog는 필수 파라미터를 source_config 안에 보관하므로
        # 세 위치를 모두 읽고, 실제 job 값은 catalog 계약에 맞게 다시 분류합니다.
        required_names = _required_param_names(catalog_payload, source_config, catalog_item)
        safe_job_contract = _safe_catalog_job_contract(catalog_payload, catalog_item)
        reconciled = _reconcile_job_conditions(
            clean_job,
            required_names,
            safe_job_contract,
            catalog_payload,
            catalog_item,
        )
        supplied_params = reconciled["required_params"]
        previous_result_targets = (
            _upstream_target_params(source_config, {"previous_result"})
            if reference_mode == "previous_result_rows"
            else []
        )
        replaced_by_previous_result_binding: list[str] = []
        for target_name in previous_result_targets:
            supplied_key = next(
                (
                    key
                    for key in supplied_params
                    if str(key).casefold() == target_name.casefold()
                ),
                "",
            )
            if supplied_key:
                supplied_params.pop(supplied_key, None)
                replaced_by_previous_result_binding.append(target_name)
        clean_job["required_params"] = supplied_params
        clean_job["filters"] = reconciled["filters"]
        clean_job.pop("params", None)
        missing_params = [
            name
            for name in required_names
            if _casefold_dict_value(supplied_params, name) in (None, "", [], {})
        ]
        upstream_targets = _upstream_target_params(
            source_config,
            deferred_binding_aliases,
        )
        deferred = [name for name in missing_params if name.casefold() in {item.casefold() for item in upstream_targets}]
        missing_params = [name for name in missing_params if name not in deferred]

        clean_job["source_type"] = source_type or str(clean_job.get("source_type") or "")
        clean_job["source_config"] = source_config
        clean_job["required_param_names"] = required_names
        clean_job["trusted_catalog"] = True
        clean_job["catalog_ref"] = f"table_catalog:{dataset_key}"
        # SQL·접속정보와 달리 컬럼 매핑/기본 상세 표시 계약은 pandas가 실제 source 컬럼을
        # 선택하고 결과 컬럼을 검증하는 데 필요한 작은 신뢰 메타데이터입니다.
        clean_job.update(safe_job_contract)
        reconciled["dataset_key"] = dataset_key
        if (
            reconciled["remapped_required_params"]
            or reconciled["moved_to_filters"]
            or reconciled["dropped_params"]
            or reconciled["normalized_date_fields"]
            or reconciled["canonicalized_filter_fields"]
            or reconciled["filter_field_conflicts"]
            or replaced_by_previous_result_binding
        ):
            reconciled["replaced_by_previous_result_binding"] = (
                replaced_by_previous_result_binding
            )
            condition_reconciliation.append(reconciled)
        for conflict in reconciled["filter_field_conflicts"]:
            warnings.append(
                _issue(
                    "conflicting_filter_alias_conditions",
                    "동일한 표준 filter field로 연결되는 조건 값이 서로 다릅니다.",
                    dataset_key=dataset_key,
                    **conflict,
                )
            )
        for dropped_name in reconciled["dropped_params"]:
            warnings.append(
                _issue(
                    "unexpected_non_catalog_required_param",
                    f"catalog에 필수 파라미터 또는 허용 필터로 등록되지 않은 조건을 제거했습니다: {dropped_name}",
                    dataset_key=dataset_key,
                    param=dropped_name,
                )
            )
        if deferred:
            deferred_upstream_params.append({"dataset_key": dataset_key, "params": deferred})
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

    plan["retrieval_jobs"] = hydrated
    plan["output_contract"] = _output_contract_with_default_detail(plan.get("output_contract"), hydrated)
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
        "deferred_upstream_params": deferred_upstream_params,
        "condition_reconciliation": condition_reconciliation,
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


# 함수 설명: `_safe_catalog_job_contract()`는 실행 job에 전달할 컬럼 매핑·기본 표시·metric 의미 계약만 카탈로그에서 복원합니다.
def _safe_catalog_job_contract(*values: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in SAFE_CATALOG_JOB_KEYS:
        for value in values:
            raw = value.get(key)
            if raw not in (None, "", [], {}):
                result[key] = _sanitize_trusted_config(raw)
                break
    return result


# 함수 설명: `_reconcile_job_conditions()`는 catalog 필수 파라미터를 권위 기준으로 삼아 LLM 조건을 조회 파라미터와 pandas 필터로 다시 나눕니다.
def _reconcile_job_conditions(
    job: dict[str, Any],
    required_names: list[str],
    safe_job_contract: dict[str, Any],
    *catalog_values: dict[str, Any],
) -> dict[str, Any]:
    supplied = _dict(job.get("required_params")) or _dict(job.get("params"))
    required_index = _required_param_alias_index(required_names, safe_job_contract)
    filter_index = _trusted_filter_field_index(safe_job_contract, *catalog_values)
    required_params: dict[str, Any] = {}
    remapped_required_params: list[dict[str, str]] = []
    moved_to_filters: list[str] = []
    dropped_params: list[str] = []
    normalized_date_fields: list[str] = []
    filters = _normalize_date_filters(job.get("filters"), normalized_date_fields)
    filter_field_normalization = _canonicalize_trusted_filter_fields(
        filters,
        filter_index,
    )
    filters = filter_field_normalization["filters"]

    for raw_name, raw_value in supplied.items():
        name = str(raw_name or "").strip()
        key = _contract_key(name)
        if not key:
            continue
        required_name = required_index.get(key)
        if required_name:
            value = _normalize_condition_value(required_name, raw_value)
            required_params[required_name] = value
            if key != _contract_key(required_name):
                remapped_required_params.append({"from": name, "to": required_name})
            if value != raw_value and required_name not in normalized_date_fields:
                normalized_date_fields.append(required_name)
            continue
        filter_name = filter_index.get(key)
        if filter_name:
            value = _normalize_condition_value(filter_name, raw_value)
            filters = _add_filter_if_missing(filters, filter_name, value)
            moved_to_filters.append(name)
            if value != raw_value and filter_name not in normalized_date_fields:
                normalized_date_fields.append(filter_name)
            continue
        dropped_params.append(name)

    return {
        "required_params": required_params,
        "remapped_required_params": remapped_required_params,
        "filters": filters,
        "moved_to_filters": moved_to_filters,
        "dropped_params": dropped_params,
        "normalized_date_fields": normalized_date_fields,
        "canonicalized_filter_fields": filter_field_normalization["renamed"],
        "filter_field_conflicts": filter_field_normalization["conflicts"],
    }


# 함수 설명: 필수 파라미터의 catalog 표준명과 명시적 컬럼 alias만 역색인하고 충돌 alias는 자동 변환하지 않습니다.
def _required_param_alias_index(
    required_names: list[str],
    safe_job_contract: dict[str, Any],
) -> dict[str, str]:
    exact = {
        _contract_key(name): name
        for name in required_names
        if _contract_key(name)
    }
    alias_targets: dict[str, set[str]] = {}
    for mapping_key in ("filter_mappings", "standard_column_aliases"):
        mapping = safe_job_contract.get(mapping_key)
        if not isinstance(mapping, dict):
            continue
        for standard, aliases in mapping.items():
            required_name = exact.get(_contract_key(standard))
            if not required_name:
                continue
            for alias in [str(standard or "").strip(), *_string_list(aliases)]:
                key = _contract_key(alias)
                if key:
                    alias_targets.setdefault(key, set()).add(required_name)
    result = dict(exact)
    for key, targets in alias_targets.items():
        if len(targets) == 1:
            result.setdefault(key, next(iter(targets)))
    return result


# 함수 설명: `_trusted_filter_field_index()`는 catalog의 표준·물리 컬럼 alias를 허용된 pandas 필터 field로 역색인합니다.
def _trusted_filter_field_index(
    safe_job_contract: dict[str, Any],
    *catalog_values: dict[str, Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for mapping_key in ("filter_mappings", "standard_column_aliases"):
        mapping = safe_job_contract.get(mapping_key)
        if not isinstance(mapping, dict):
            continue
        for standard, aliases in mapping.items():
            standard_name = str(standard or "").strip()
            if not standard_name:
                continue
            result.setdefault(_contract_key(standard_name), standard_name)
            for alias in _string_list(aliases):
                result.setdefault(_contract_key(alias), standard_name)
    for catalog_value in catalog_values:
        for column in _catalog_column_names(catalog_value):
            result.setdefault(_contract_key(column), column)
    return result


# 함수 설명: Table Catalog의 표준->물리 mapping을 역으로 적용해 LLM filter field를 표준 key로 확정합니다.
# source row가 pandas 직전에 표준화되므로 실행 filter도 같은 표준 namespace를 사용해야 합니다.
def _canonicalize_trusted_filter_fields(
    filters: Any,
    filter_index: dict[str, str],
) -> dict[str, Any]:
    renamed: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    if isinstance(filters, list):
        result_list: list[Any] = []
        for raw in filters:
            if not isinstance(raw, dict):
                result_list.append(deepcopy(raw))
                continue
            item = deepcopy(raw)
            raw_field = str(item.get("field") or item.get("column") or "").strip()
            canonical = filter_index.get(_contract_key(raw_field), raw_field)
            if canonical and raw_field and canonical != raw_field:
                renamed.append({"from": raw_field, "to": canonical})
            if canonical:
                item["field"] = canonical
                item.pop("column", None)
            result_list.append(item)
        return {"filters": result_list, "renamed": renamed, "conflicts": conflicts}

    if not isinstance(filters, dict):
        return {"filters": {}, "renamed": renamed, "conflicts": conflicts}

    result: dict[str, Any] = {}
    source_fields: dict[str, str] = {}
    for raw_field, condition in filters.items():
        field = str(raw_field or "").strip()
        if not field:
            continue
        canonical = filter_index.get(_contract_key(field), field)
        canonical_key = _contract_key(canonical)
        if canonical != field:
            renamed.append({"from": field, "to": canonical})
        existing_name = next(
            (name for name in result if _contract_key(name) == canonical_key),
            "",
        )
        if not existing_name:
            result[canonical] = deepcopy(condition)
            source_fields[canonical_key] = field
            continue
        if result[existing_name] == condition:
            continue
        existing_source = source_fields.get(canonical_key, existing_name)
        current_is_exact = _contract_key(field) == canonical_key
        existing_is_exact = _contract_key(existing_source) == canonical_key
        if current_is_exact and not existing_is_exact:
            result[existing_name] = deepcopy(condition)
            source_fields[canonical_key] = field
        conflicts.append(
            {
                "canonical_field": canonical,
                "source_fields": [existing_source, field],
            }
        )
    return {"filters": result, "renamed": renamed, "conflicts": conflicts}


# 함수 설명: 카탈로그의 여러 스키마 표현에서 실제 컬럼 이름 목록을 중복 없이 추출합니다.
def _catalog_column_names(value: dict[str, Any]) -> list[str]:
    raw = value.get("columns") or value.get("schema") or value.get("column_names") or []
    if isinstance(raw, dict):
        raw = list(raw)
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("name") or item.get("column") or item.get("key")
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_normalize_date_filters()`는 dict/list 필터 안의 날짜 조건값만 YYYYMMDD로 통일하고 원래 필터 구조는 유지합니다.
def _normalize_date_filters(filters: Any, normalized_fields: list[str]) -> Any:
    if isinstance(filters, dict):
        result: dict[str, Any] = {}
        for field, condition in filters.items():
            field_name = str(field or "").strip()
            normalized = _normalize_filter_condition_value(field_name, condition)
            result[str(field)] = normalized
            if normalized != condition and field_name not in normalized_fields:
                normalized_fields.append(field_name)
        return result
    if isinstance(filters, list):
        result_list: list[Any] = []
        for condition in filters:
            if not isinstance(condition, dict):
                result_list.append(deepcopy(condition))
                continue
            field_name = str(condition.get("field") or condition.get("column") or "").strip()
            normalized = deepcopy(condition)
            if _is_date_condition_key(field_name):
                for key in ("value", "values"):
                    if key in normalized:
                        normalized[key] = _normalize_nested_date_values(normalized[key])
                if normalized != condition and field_name not in normalized_fields:
                    normalized_fields.append(field_name)
            result_list.append(normalized)
        return result_list
    return {}


# 함수 설명: `_normalize_filter_condition_value()`는 한 filter field가 날짜 의미일 때 value/values 또는 축약값을 YYYYMMDD로 바꿉니다.
def _normalize_filter_condition_value(field: str, condition: Any) -> Any:
    if not _is_date_condition_key(field):
        return deepcopy(condition)
    if isinstance(condition, dict):
        normalized = deepcopy(condition)
        for key in ("value", "values"):
            if key in normalized:
                normalized[key] = _normalize_nested_date_values(normalized[key])
        return normalized
    return _normalize_nested_date_values(condition)


# 함수 설명: `_normalize_nested_date_values()`는 목록을 포함한 날짜 조건값을 재귀적으로 YYYYMMDD로 정규화합니다.
def _normalize_nested_date_values(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_nested_date_values(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_nested_date_values(item) for item in value]
    return _canonical_date_text(value)


# 함수 설명: `_add_filter_if_missing()`는 기존 명시 필터를 우선하며 이동된 비필수 파라미터를 중복 없이 추가합니다.
def _add_filter_if_missing(filters: Any, field: str, value: Any) -> Any:
    if isinstance(filters, list):
        if any(
            isinstance(item, dict)
            and _contract_key(item.get("field") or item.get("column")) == _contract_key(field)
            for item in filters
        ):
            return filters
        return [*filters, {"field": field, "operator": "eq", "value": deepcopy(value)}]
    result = deepcopy(filters) if isinstance(filters, dict) else {}
    if any(_contract_key(key) == _contract_key(field) for key in result):
        return result
    result[field] = {"operator": "eq", "value": deepcopy(value)}
    return result


# 함수 설명: `_normalize_condition_value()`는 날짜 의미 field에만 날짜 정규화를 적용합니다.
def _normalize_condition_value(field: str, value: Any) -> Any:
    if not _is_date_condition_key(field):
        return deepcopy(value)
    return _normalize_nested_date_values(value)


# 함수 설명: `_canonical_date_text()`는 여러 날짜 표기를 검증된 YYYYMMDD 문자열로 정규화하고 해석 불가 값은 보존합니다.
def _canonical_date_text(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    if isinstance(value, bool) or value is None:
        return value
    text = str(value).strip()
    if not text:
        return value
    if re.fullmatch(r"\d{8}(?:\.0+)?", text):
        candidate = text[:8]
    else:
        match = re.match(
            r"^(\d{4})\s*(?:[-/.]|년)\s*(\d{1,2})\s*(?:[-/.]|월)\s*(\d{1,2})(?:\s*일)?(?:\D.*)?$",
            text,
        )
        if not match:
            return value
        candidate = f"{int(match.group(1)):04d}{int(match.group(2)):02d}{int(match.group(3)):02d}"
    try:
        datetime.strptime(candidate, "%Y%m%d")
    except ValueError:
        return value
    return candidate


# 함수 설명: `_is_date_condition_key()`는 DATE 또는 DT 토큰으로 선언된 날짜 조건 field를 판별합니다.
def _is_date_condition_key(value: Any) -> bool:
    key = _contract_key(value)
    return bool(key and re.search(r"(?:^|_)(?:DATE|DT)(?:$|_)", key))


# 함수 설명: `_contract_key()`는 catalog field 비교에 사용할 대문자 underscore 키를 만듭니다.
def _contract_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


# 함수 설명: `_casefold_dict_value()`는 대소문자와 구분자 차이를 무시하고 dict 값을 찾습니다.
def _casefold_dict_value(value: dict[str, Any], key: str) -> Any:
    target = _contract_key(key)
    for raw_key, item in value.items():
        if _contract_key(raw_key) == target:
            return item
    return None


# 함수 설명: `_output_contract_with_default_detail()`은 상세 결과에만 trusted catalog 기본 컬럼을 required_columns로 합칩니다.
def _output_contract_with_default_detail(value: Any, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    contract = {
        str(key): deepcopy(item)
        for key, item in source.items()
        if str(key) not in RETIRED_OUTPUT_CONTRACT_KEYS
    }
    result_mode = str(contract.get("result_mode") or contract.get("mode") or "").strip().lower()
    if result_mode not in {"detail", "entity_list"}:
        return contract

    required_columns = _string_list(contract.get("required_columns") or contract.get("columns"))
    for job in jobs:
        required_columns = _merge_strings(
            required_columns,
            _string_list(job.get("default_detail_columns")),
        )
    if required_columns:
        contract["required_columns"] = required_columns
    return contract


# 함수 설명: `_string_list()`는 컬럼 입력을 순서가 유지되는 중복 없는 문자열 목록으로 정규화합니다.
def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_merge_strings()`는 여러 컬럼 목록을 첫 등장 순서로 합칩니다.
def _merge_strings(*values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in value:
            if item not in result:
                result.append(item)
    return result


# 함수 설명: `_upstream_target_params()`는 신뢰 source_config의 binding이 후속 단계에서 채울 파라미터 이름만 추출합니다.
def _upstream_target_params(
    source_config: dict[str, Any],
    allowed_source_aliases: set[str] | None = None,
) -> list[str]:
    bindings = source_config.get("upstream_bindings")
    if not isinstance(bindings, list):
        return []
    result: list[str] = []
    allowed = allowed_source_aliases or set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        source_alias = str(binding.get("source_alias") or "upstream_result").strip()
        if not allowed or source_alias not in allowed:
            continue
        target = str(binding.get("target_param") or "").strip()
        if target and target.casefold() not in {item.casefold() for item in result}:
            result.append(target)
    return result


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
