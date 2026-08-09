# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 04B 종속 조회 계획 컴파일러
# 역할: Catalog 근거가 있는 최대 2단계 Typed IR을 검증하고 현재 단계만 기존 flat 계약으로 투영합니다.
# 주요 입력: 요청 payload, 의도 분석 JSON, 선택된 metadata candidates
# 주요 출력: 기존 의도 정규화기가 처리할 수 있는 현재 단계 의도 JSON Message
# 처리 흐름: 명시 계획 또는 Catalog 기반 flat plan 승격을 검증하고 plan hash와 단계 runtime을 확정합니다.
# 유지보수 포인트: 필수 파라미터·binding·handoff 컬럼은 선택된 Table Catalog와 정확히 일치해야 합니다.
# =============================================================================

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.message import Message

CONTRACT_VERSION = "analysis.dependent_retrieval.v1"
MAX_STAGES = 2
# Full Typed IR is stored server-side.  The public resume contract has a much
# smaller, independent bound in the API builder.
MAX_INTERNAL_PLAN_BYTES = 64 * 1024
STAGE_PLAN_KEYS = (
    "retrieval_jobs",
    "pandas_execution_plan",
    "output_contract",
    "grain_plan",
    "join_plan",
    "temporal_semantics",
    "pandas_function_cases",
)


# 주요 함수: 일반 plan은 그대로 유지하고 검증된 종속 plan만 현재 실행 단계로 컴파일합니다.
def compile_intent_response(
    payload_value: Any,
    llm_response: Any,
    metadata_candidates_value: Any = None,
) -> tuple[str, dict[str, Any]]:
    """Project only the active stage; leave ordinary one-stage plans unchanged."""

    payload = _payload(payload_value)
    parsed = _parse_json_response(llm_response)
    envelope = parsed if isinstance(parsed.get("intent_plan"), dict) else {"intent_plan": parsed}
    plan = deepcopy(envelope.get("intent_plan") or {})
    continuation_request = _continuation_request(payload)
    dependent_key_present = "dependent_retrieval_plan" in plan
    dependent = plan.get("dependent_retrieval_plan")
    placeholder_removed = False
    if dependent_key_present:
        if not isinstance(dependent, dict):
            raise ValueError("dependent_retrieval_plan은 객체여야 합니다.")
        if _is_empty_dependent_placeholder(dependent):
            plan.pop("dependent_retrieval_plan", None)
            dependent = None
            placeholder_removed = True
    dataset_scope_reconciliation: dict[str, Any] | None = None
    existence_predicate: dict[str, Any] | None = None
    if not continuation_request:
        plan, dataset_scope_reconciliation = _reconcile_single_source_detail_time_scope(
            plan,
            payload,
            metadata_candidates_value,
        )
        plan, existence_predicate = _apply_entity_existence_predicate(
            plan,
            payload,
            metadata_candidates_value,
        )
    explicit_dependent = isinstance(dependent, dict) and bool(dependent)
    flat_fallback: dict[str, Any] | None = None
    fallback_shape_eligible = (
        explicit_dependent and _explicit_dependency_fallback_shape(dependent)
    )
    if explicit_dependent and not continuation_request:
        flat_fallback = _independently_complete_flat_plan(plan, metadata_candidates_value)
        if (
            not fallback_shape_eligible
            and flat_fallback is not None
            and _is_redundant_single_stage_wrapper(dependent, flat_fallback)
        ):
            return _ignored_dependent_passthrough(
                envelope,
                flat_fallback,
                reason="redundant_single_stage_dependent_wrapper",
            )
    if fallback_shape_eligible and not continuation_request:
        if not _dependent_final_contract_coherent(plan, dependent):
            if flat_fallback is not None:
                return _ignored_dependent_passthrough(
                    envelope,
                    flat_fallback,
                    reason="dependent_final_contract_incoherent",
                )
        flattened = _flatten_independent_dependent_plan(
            plan,
            dependent,
            metadata_candidates_value,
        )
        if flattened is not None:
            return _flattened_dependent_passthrough(envelope, flattened)
        stage1_fallback = _catalog_unselected_stage2_fallback(
            plan,
            dependent,
            metadata_candidates_value,
            payload,
        )
        if stage1_fallback is not None:
            fallback_plan, fallback_evidence = stage1_fallback
            return _ignored_dependent_passthrough(
                envelope,
                fallback_plan,
                reason="dependent_stage2_not_selected_by_metadata_refs",
                evidence=fallback_evidence,
            )
    lifted = False
    if not isinstance(dependent, dict) and not continuation_request:
        dependent = _lift_flat_plan_from_catalog(plan, metadata_candidates_value)
        if dependent:
            plan["dependent_retrieval_plan"] = dependent
            lifted = True

    if not isinstance(dependent, dict) or not dependent:
        if continuation_request:
            raise ValueError("continuation 재개 계약에 dependent_retrieval_plan이 없습니다.")
        plan, pruning = _prune_unreachable_flat_branches(plan)
        envelope["intent_plan"] = plan
        passthrough_trace = {
            "status": "passthrough",
            "dependent": False,
            "active_stage_index": 0,
        }
        if placeholder_removed:
            passthrough_trace["empty_placeholder_removed"] = True
        if pruning["pruned_step_count"] or pruning["pruned_job_count"]:
            passthrough_trace["flat_reachability_pruned"] = True
            passthrough_trace.update(pruning)
        if existence_predicate is not None:
            passthrough_trace["existence_predicate_applied"] = deepcopy(existence_predicate)
        if dataset_scope_reconciliation is not None:
            passthrough_trace["dataset_scope_reconciliation"] = deepcopy(
                dataset_scope_reconciliation
            )
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), passthrough_trace

    shape_repaired = False
    try:
        dependent, shape_repaired = _repair_recoverable_dependent_shape(
            plan,
            dependent,
            metadata_candidates_value,
        )
        dependent = _normalize_and_validate_plan(dependent, metadata_candidates_value)
    except ValueError:
        if (
            explicit_dependent
            and fallback_shape_eligible
            and not continuation_request
            and flat_fallback is not None
            and not _dependent_bindings_catalog_proven(dependent, metadata_candidates_value)
        ):
            return _ignored_dependent_passthrough(
                envelope,
                flat_fallback,
                reason="dependent_binding_not_catalog_proven",
            )
        raise
    calculated_hash = _plan_hash(dependent)
    supplied_hash = str(dependent.get("plan_hash") or "").strip()
    if supplied_hash and supplied_hash != calculated_hash:
        raise ValueError("dependent_retrieval_plan.plan_hash가 계약 내용과 일치하지 않습니다.")
    dependent["plan_hash"] = calculated_hash
    dependent["plan_id"] = str(dependent.get("plan_id") or f"drp-{calculated_hash[:12]}").strip()

    active_index = 0
    intent_llm_skipped = False
    if continuation_request:
        contract = continuation_request.get("continuation_contract")
        if not isinstance(contract, dict):
            raise ValueError("continuation_contract가 없습니다.")
        _validate_resume_contract(contract, dependent, continuation_request)
        active_index = _bounded_index(contract.get("next_stage_index"), len(dependent["stages"]))
        previous_index = _bounded_index(contract.get("current_stage_index"), len(dependent["stages"]))
        if active_index != previous_index + 1:
            raise ValueError("continuation 단계는 정확히 한 단계씩만 진행할 수 있습니다.")
        intent_llm_skipped = True

    stage = deepcopy(dependent["stages"][active_index])
    _project_stage(plan, stage)
    status = "pending" if active_index + 1 < len(dependent["stages"]) else "complete"
    dependent["active_stage_index"] = active_index
    dependent["runtime"] = {
        "status": status,
        "active_stage_index": active_index,
        "current_stage_id": stage["stage_id"],
        "next_stage_id": (
            dependent["stages"][active_index + 1]["stage_id"]
            if status == "pending"
            else ""
        ),
        "intent_llm_skipped": intent_llm_skipped,
    }
    plan["dependent_retrieval_plan"] = dependent
    if active_index > 0:
        # continuation은 대화형 previous-result 후속 분석이 아니라 공개
        # result_ref로 복원한 명시적 external source를 실행하는 새 child run이다.
        # current stage는 new_analysis로 투영하고 upstream_result는 typed graph
        # provider로만 보존해 기존 followup/reference_mode 계약과 충돌하지 않는다.
        plan["request_scope"] = "new_analysis"
        # Explicit orchestration owns the upstream_result alias. Keeping the
        # conversational previous_result mode off prevents loading both aliases.
        plan["reference_mode"] = "none"
        plan["reuse_strategy"] = "none"
    envelope["intent_plan"] = plan
    trace = envelope.get("trace") if isinstance(envelope.get("trace"), dict) else {}
    trace["continuation"] = {
        "status": status,
        "plan_id": dependent["plan_id"],
        "plan_hash": calculated_hash,
        "active_stage_index": active_index,
        "intent_llm_skipped": intent_llm_skipped,
    }
    if shape_repaired:
        trace["continuation"]["dependent_shape_repaired"] = True
    envelope["trace"] = trace
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), deepcopy(trace["continuation"])


# 주요 함수: 신뢰할 수 없는 종속 계획을 제거하고 검증된 독립 flat 계획만 반환합니다.
def _ignored_dependent_passthrough(
    envelope: dict[str, Any],
    flat_plan: dict[str, Any],
    *,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    next_plan = deepcopy(flat_plan)
    next_plan.pop("dependent_retrieval_plan", None)
    envelope["intent_plan"] = next_plan
    trace = {
        "status": "passthrough",
        "dependent": False,
        "active_stage_index": 0,
        "untrusted_dependent_ignored": True,
        "dependent_ignore_reason": reason,
        "retained_source_aliases": [
            str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            for job in next_plan.get("retrieval_jobs", [])
            if isinstance(job, dict)
        ],
    }
    if isinstance(evidence, dict) and evidence:
        trace["dependent_ignore_evidence"] = deepcopy(evidence)
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), trace


# 함수 설명: 완전한 flat 계획과 동일한 단일 stage wrapper만 약한 모델의 중복 출력으로 간주합니다.
def _is_redundant_single_stage_wrapper(
    dependent_plan: dict[str, Any],
    flat_plan: dict[str, Any],
) -> bool:
    """Recognize only an exact one-stage copy of an already complete flat plan."""

    stages = dependent_plan.get("stages")
    if not isinstance(stages, list) or len(stages) != 1 or not isinstance(stages[0], dict):
        return False
    stage = stages[0]
    if _strings(stage.get("depends_on")):
        return False
    if [item for item in stage.get("input_bindings", []) if isinstance(item, dict)]:
        return False
    for key in ("activation", "runtime"):
        if not _structurally_empty(dependent_plan.get(key)):
            return False
    for key in ("retrieval_jobs", "pandas_execution_plan", "output_contract"):
        if _canonical_json(stage.get(key)) != _canonical_json(flat_plan.get(key)):
            return False
    return True


# 함수 설명: JSON 비교가 dict key 순서에 영향받지 않도록 안정적인 문자열로 정규화합니다.
def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# 함수 설명: 단일 source 상세/비교 계획을 같은 family의 유일한 요청 time-scope Catalog로 안전 교체합니다.
def _reconcile_single_source_detail_time_scope(
    plan_value: dict[str, Any],
    payload_value: Any,
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    plan = deepcopy(plan_value)
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result_mode = str(contract.get("result_mode") or "").strip().lower()
    if result_mode not in {"detail", "entity_list", "comparison"}:
        return plan, None
    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(jobs) != 1:
        return plan, None
    payload = _payload(payload_value)
    desired_scope = _requested_job_time_scope(jobs[0], payload)
    if not desired_scope:
        return plan, None
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    current_key = str(jobs[0].get("dataset_key") or "").strip()
    current_catalog = catalogs.get(current_key)
    if not current_key or not isinstance(current_catalog, dict):
        return plan, None
    family = _catalog_dataset_family(current_catalog)
    current_scope = _catalog_time_scope(current_catalog)
    if not family or current_scope == desired_scope:
        return plan, None
    required_columns = _flat_plan_source_columns(plan)
    if not required_columns:
        return plan, None
    supplied = jobs[0].get("required_params") if isinstance(jobs[0].get("required_params"), dict) else {}
    eligible: list[tuple[str, dict[str, Any]]] = []
    for dataset_key, catalog in catalogs.items():
        if dataset_key == current_key:
            continue
        if _catalog_dataset_family(catalog) != family:
            continue
        if _catalog_time_scope(catalog) != desired_scope:
            continue
        if not required_columns.issubset(set(_catalog_canonical_columns(catalog))):
            continue
        if any(_blank(supplied.get(name)) for name in _catalog_required_params(catalog)):
            continue
        eligible.append((dataset_key, catalog))
    if len(eligible) != 1:
        return plan, None
    selected_key, selected_catalog = eligible[0]
    jobs[0]["dataset_key"] = selected_key
    source_type = str(selected_catalog.get("source_type") or "").strip()
    if source_type:
        jobs[0]["source_type"] = source_type
    plan["retrieval_jobs"] = jobs
    refs = plan.get("metadata_refs") if isinstance(plan.get("metadata_refs"), list) else []
    normalized_refs: list[Any] = []
    for item in refs:
        if not isinstance(item, dict):
            normalized_refs.append(deepcopy(item))
            continue
        next_item = deepcopy(item)
        section = _normalized_section(item.get("section") or item.get("type"))
        if section in {"table_catalog", "table_catalog_items"} and str(item.get("key") or "").strip() == current_key:
            next_item["key"] = selected_key
        normalized_refs.append(next_item)
    if refs:
        plan["metadata_refs"] = normalized_refs
    return plan, {
        "policy": "unique_same_family_time_scope_with_full_column_support",
        "source_alias": str(jobs[0].get("source_alias") or selected_key).strip(),
        "from_dataset_key": current_key,
        "to_dataset_key": selected_key,
        "dataset_family": family,
        "requested_time_scope": desired_scope,
        "required_columns": sorted(required_columns),
    }


# 함수 설명: job DATE와 요청 reference_date가 가리키는 current_day/history scope를 계산합니다.
def _requested_job_time_scope(job: dict[str, Any], payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    reference_date = re.sub(r"[^0-9]", "", str(request.get("reference_date") or ""))[:8]
    params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
    requested_date = next(
        (
            re.sub(r"[^0-9]", "", str(value or ""))[:8]
            for key, value in params.items()
            if str(key).strip().casefold() == "date"
        ),
        "",
    )
    if not re.fullmatch(r"20\d{6}", reference_date) or not re.fullmatch(r"20\d{6}", requested_date):
        return ""
    return "current_day" if requested_date == reference_date else "history"


# 함수 설명: Catalog selection_criteria의 dataset family를 표준 문자열로 읽습니다.
def _catalog_dataset_family(catalog: dict[str, Any]) -> str:
    criteria = catalog.get("selection_criteria") if isinstance(catalog.get("selection_criteria"), dict) else {}
    return str(catalog.get("dataset_family") or criteria.get("dataset_family") or "").strip().lower()


# 함수 설명: Catalog selection_criteria의 time scope를 current_day/history 표준값으로 읽습니다.
def _catalog_time_scope(catalog: dict[str, Any]) -> str:
    criteria = catalog.get("selection_criteria") if isinstance(catalog.get("selection_criteria"), dict) else {}
    raw = str(criteria.get("time_scope") or catalog.get("time_scope") or "").strip().lower()
    if raw in {"current", "current_day", "today", "realtime", "current_time"}:
        return "current_day"
    if raw in {"history", "historical", "past", "as_of_date"}:
        return "history"
    return raw


# 함수 설명: filter/projection/group/comparison/output에서 원본 source가 제공해야 하는 컬럼을 모두 수집합니다.
def _flat_plan_source_columns(plan: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for job in plan.get("retrieval_jobs", []) if isinstance(plan.get("retrieval_jobs"), list) else []:
        if isinstance(job, dict) and isinstance(job.get("filters"), dict):
            values.extend(job["filters"].keys())
    for step in plan.get("pandas_execution_plan", []) if isinstance(plan.get("pandas_execution_plan"), list) else []:
        if not isinstance(step, dict):
            continue
        for key in (
            "group_by",
            "projection",
            "columns",
            "comparison_columns",
            "partition_by",
            "join_keys",
            "left_keys",
            "right_keys",
            "right_value_columns",
        ):
            values.extend(step.get(key) if isinstance(step.get(key), list) else [])
        for key in ("column", "source_column", "sort_by", "left_column", "right_column"):
            values.append(step.get(key))
        for aggregation in step.get("aggregations", []) if isinstance(step.get("aggregations"), list) else []:
            if isinstance(aggregation, dict):
                values.append(aggregation.get("source_column") or aggregation.get("column"))
        if isinstance(step.get("filters"), dict):
            values.extend(step["filters"].keys())
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    for key in ("required_columns", "result_columns", "grain_columns", "metric_columns"):
        values.extend(contract.get(key) if isinstance(contract.get(key), list) else [])
    for binding in contract.get("metric_bindings", []) if isinstance(contract.get("metric_bindings"), list) else []:
        if isinstance(binding, dict):
            values.append(binding.get("source_column"))
    return set(_strings([value for value in values if value not in (None, "")]))


# 함수 설명: entity 존재 질문에 선택된 단일 quantity metadata의 양수 조건을 trusted source filter로 추가합니다.
def _apply_entity_existence_predicate(
    plan_value: dict[str, Any],
    payload_value: Any,
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    plan = deepcopy(plan_value)
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    if str(contract.get("result_mode") or "").strip().lower() != "entity_list":
        return plan, None
    payload = _payload(payload_value)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or payload.get("question") or "").strip()
    if not _has_existence_expression(question):
        return plan, None

    refs = plan.get("metadata_refs") if isinstance(plan.get("metadata_refs"), list) else []
    quantity_refs = [
        item
        for item in refs
        if isinstance(item, dict)
        and _normalized_section(item.get("section") or item.get("type")) == "quantity_terms"
        and str(item.get("key") or "").strip()
    ]
    if len(quantity_refs) != 1:
        return plan, None
    domain_items = _domain_items_by_ref(metadata_candidates_value)
    ref = quantity_refs[0]
    ref_key = (
        _normalized_section(ref.get("section") or ref.get("type")),
        str(ref.get("key") or "").strip(),
    )
    metric_metadata = domain_items.get(ref_key)
    if not isinstance(metric_metadata, dict):
        return plan, None
    metric_column = str(
        metric_metadata.get("column")
        or metric_metadata.get("source_column")
        or metric_metadata.get("canonical_column")
        or ""
    ).strip()
    if not metric_column:
        return plan, None

    catalogs = _catalog_by_dataset(metadata_candidates_value)
    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    candidates: list[tuple[int, str, str]] = []
    for index, job in enumerate(jobs):
        dataset_key = str(job.get("dataset_key") or "").strip()
        source_alias = str(job.get("source_alias") or dataset_key).strip()
        catalog = catalogs.get(dataset_key)
        if not source_alias or not isinstance(catalog, dict):
            continue
        if metric_column in set(_catalog_canonical_columns(catalog)):
            candidates.append((index, dataset_key, source_alias))
    if len(candidates) != 1:
        return plan, None
    index, dataset_key, source_alias = candidates[0]
    filters = jobs[index].get("filters") if isinstance(jobs[index].get("filters"), dict) else {}
    existing = next(
        (value for key, value in filters.items() if str(key).strip().casefold() == metric_column.casefold()),
        None,
    )
    if existing not in (None, "", [], {}):
        return plan, None
    predicate = {
        "operator": "gt",
        "value": 0,
        "source_alias": source_alias,
        "dataset_key": dataset_key,
        "source_column": metric_column,
        "metadata_ref": {"section": ref_key[0], "key": ref_key[1]},
    }
    filters[metric_column] = {"operator": "gt", "value": 0}
    jobs[index]["filters"] = filters
    plan["retrieval_jobs"] = jobs
    contract["existence_predicate"] = deepcopy(predicate)
    plan["output_contract"] = contract
    return plan, predicate


# 함수 설명: 질문에 entity 존재 또는 양수 존재를 명시하는 완전 표현이 있는지 확인합니다.
def _has_existence_expression(question: str) -> bool:
    text = str(question or "").casefold()
    compact = re.sub(r"\s+", "", text)
    if any(term in compact for term in ("있는", "존재", "nonzero", "non-zero")):
        return True
    return bool(re.search(r"\bhas\b", text))


# 함수 설명: selected metadata candidate의 Domain item을 section/key 복합키로 색인합니다.
def _domain_items_by_ref(value: Any) -> dict[tuple[str, str], dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, dict) and isinstance(data.get("metadata_candidates"), dict):
        data = data["metadata_candidates"]
    items = data.get("domain_items") if isinstance(data, dict) else []
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        section = _normalized_section(item.get("section") or payload.get("section"))
        key = str(item.get("key") or payload.get("key") or "").strip()
        if section and key:
            result[(section, key)] = deepcopy(payload)
    return result


# 함수 설명: metadata section 표기의 구분자와 대소문자 차이를 제거합니다.
def _normalized_section(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


# 주요 함수: 필수 파라미터 의존성이 없는 두 stage를 기존 단일 Complex flat plan으로 안전하게 합칩니다.
def _flatten_independent_dependent_plan(
    flat_plan: dict[str, Any],
    dependent_plan: dict[str, Any],
    metadata_candidates_value: Any,
) -> dict[str, Any] | None:
    top_steps = [item for item in flat_plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    if top_steps or not _explicit_dependency_fallback_shape(dependent_plan):
        return None
    stages = [deepcopy(item) for item in dependent_plan.get("stages", []) if isinstance(item, dict)]
    for stage in stages:
        _normalize_stage_node_aliases(stage)
    missing_required = _stage2_missing_required_params(stages[1], metadata_candidates_value)
    if missing_required is None or missing_required:
        return None
    if _dependent_bindings_catalog_proven(dependent_plan, metadata_candidates_value):
        return None

    stage1_steps = [deepcopy(item) for item in stages[0].get("pandas_execution_plan", []) if isinstance(item, dict)]
    stage2_steps = [deepcopy(item) for item in stages[1].get("pandas_execution_plan", []) if isinstance(item, dict)]
    terminal_alias = _terminal_stage_output_alias(stages[0])
    cross_stage_aliases = _stage_output_reference_aliases(stages[0])
    if not stage1_steps or not stage2_steps or not terminal_alias or not cross_stage_aliases:
        return None
    _rewrite_cross_stage_references(
        stage2_steps,
        cross_stage_aliases,
        replacement=terminal_alias,
        input_kind=None,
    )

    combined_jobs: list[dict[str, Any]] = []
    seen_aliases: set[str] = set()
    for stage in stages:
        for job in stage.get("retrieval_jobs", []) if isinstance(stage.get("retrieval_jobs"), list) else []:
            if not isinstance(job, dict):
                continue
            alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            if not alias or alias in seen_aliases:
                return None
            seen_aliases.add(alias)
            combined_jobs.append(deepcopy(job))
    if not combined_jobs:
        return None

    result = deepcopy(flat_plan)
    result["retrieval_jobs"] = combined_jobs
    result["pandas_execution_plan"] = [*stage1_steps, *stage2_steps]
    final_contract = deepcopy(stages[1].get("output_contract") or {})
    if not _strings(final_contract.get("result_columns")):
        final_contract["result_columns"] = _strings(final_contract.get("required_columns"))
    final_contract = _rewrite_flattened_output_metric_bindings(
        final_contract,
        stages[0].get("output_contract"),
        cross_stage_aliases,
    )
    if final_contract is None:
        return None
    result["output_contract"] = final_contract
    for key in ("grain_plan", "join_plan"):
        if key in stages[1]:
            result[key] = deepcopy(stages[1][key])
        elif key in stages[0]:
            result[key] = deepcopy(stages[0][key])
    for key in ("temporal_semantics", "pandas_function_cases"):
        combined: list[Any] = []
        for stage in stages:
            raw = stage.get(key)
            if isinstance(raw, list):
                combined.extend(deepcopy(raw))
        if combined:
            result[key] = combined
    result.pop("dependent_retrieval_plan", None)
    return _independently_complete_flat_plan(result, metadata_candidates_value)


# 함수 설명: flatten된 최종 metric binding의 stage1 별칭을 stage1 원본 source 계약으로 되돌립니다.
def _rewrite_flattened_output_metric_bindings(
    final_contract_value: Any,
    stage1_contract_value: Any,
    cross_stage_aliases: set[str],
) -> dict[str, Any] | None:
    final_contract = deepcopy(final_contract_value) if isinstance(final_contract_value, dict) else {}
    stage1_contract = stage1_contract_value if isinstance(stage1_contract_value, dict) else {}
    stage1_bindings = [
        item
        for item in stage1_contract.get("metric_bindings", [])
        if isinstance(item, dict)
    ]
    rewritten: list[Any] = []
    for raw_binding in final_contract.get("metric_bindings", []) if isinstance(final_contract.get("metric_bindings"), list) else []:
        if not isinstance(raw_binding, dict):
            rewritten.append(deepcopy(raw_binding))
            continue
        binding = deepcopy(raw_binding)
        source_alias = str(binding.get("source_alias") or "").strip()
        if source_alias not in cross_stage_aliases:
            rewritten.append(binding)
            continue
        output_column = str(binding.get("output_column") or "").strip()
        candidates = [
            item
            for item in stage1_bindings
            if str(item.get("output_column") or "").strip() == output_column
            and (
                not str(binding.get("dataset_key") or "").strip()
                or str(item.get("dataset_key") or "").strip()
                == str(binding.get("dataset_key") or "").strip()
            )
            and (
                not str(binding.get("source_column") or "").strip()
                or str(item.get("source_column") or "").strip()
                == str(binding.get("source_column") or "").strip()
            )
        ]
        if len(candidates) != 1:
            return None
        binding.update(deepcopy(candidates[0]))
        rewritten.append(binding)
    if isinstance(final_contract.get("metric_bindings"), list):
        final_contract["metric_bindings"] = rewritten
    return final_contract


# 함수 설명: 독립 stage flatten 결과를 continuation 없이 기존 normalizer로 전달합니다.
def _flattened_dependent_passthrough(
    envelope: dict[str, Any],
    flat_plan: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    envelope["intent_plan"] = deepcopy(flat_plan)
    trace = {
        "status": "passthrough",
        "dependent": False,
        "active_stage_index": 0,
        "dependent_flattened": True,
        "dependent_flatten_reason": "stage2_has_no_required_param_dependency",
        "retained_source_aliases": [
            str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            for job in flat_plan.get("retrieval_jobs", [])
            if isinstance(job, dict)
        ],
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), trace


# 함수 설명: stage2 Catalog의 아직 채워지지 않은 required parameter 집합을 계산합니다.
def _stage2_missing_required_params(
    stage: dict[str, Any],
    metadata_candidates_value: Any,
) -> set[str] | None:
    jobs = [item for item in stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(jobs) != 1:
        return None
    job = jobs[0]
    catalog = _catalog_by_dataset(metadata_candidates_value).get(
        str(job.get("dataset_key") or "").strip()
    )
    if not isinstance(catalog, dict):
        return None
    supplied = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
    return {
        name
        for name in _catalog_required_params(catalog)
        if _blank(supplied.get(name))
    }


# 함수 설명: stage1을 다른 실행으로 전달할 때 참조 가능한 stage ID와 최종 node/output alias를 만듭니다.
def _stage_output_reference_aliases(stage: dict[str, Any]) -> set[str]:
    stage_id = str(stage.get("stage_id") or "").strip()
    terminal = _terminal_stage_output_alias(stage)
    aliases = {value for value in (stage_id, terminal) if value}
    if stage_id and terminal:
        aliases.add(f"{stage_id}.{terminal}")
    return aliases


# 함수 설명: stage의 마지막 실행 step이 공개하는 output_alias 또는 node_id를 확정합니다.
def _terminal_stage_output_alias(stage: dict[str, Any]) -> str:
    steps = [item for item in stage.get("pandas_execution_plan", []) if isinstance(item, dict)]
    if not steps:
        return str(stage.get("output_alias") or "").strip()
    last = steps[-1]
    return str(last.get("output_alias") or last.get("node_id") or "").strip()


# 함수 설명: stage2의 stage1 참조만 현재 실행에 맞는 source alias로 바꿉니다.
def _rewrite_cross_stage_references(
    steps: list[dict[str, Any]],
    aliases: set[str],
    *,
    replacement: str,
    input_kind: str | None,
) -> None:
    stage2_outputs = {
        str(step.get(key) or "").strip()
        for step in steps
        for key in ("node_id", "output_alias")
        if str(step.get(key) or "").strip()
    }
    if aliases.intersection(stage2_outputs):
        raise ValueError("stage 간 output alias가 충돌하여 안전하게 정규화할 수 없습니다.")
    for step in steps:
        for key in ("source_alias", "left_source_alias", "right_source_alias"):
            if str(step.get(key) or "").strip() in aliases:
                step[key] = replacement
        for item in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
            if not isinstance(item, dict) or str(item.get("ref") or "").strip() not in aliases:
                continue
            item["ref"] = replacement
            if input_kind:
                item["kind"] = input_kind


# 함수 설명: fallback 판정은 두 단계 필수 골격이 모두 있는 explicit 계획에만 허용합니다.
def _explicit_dependency_fallback_shape(value: dict[str, Any]) -> bool:
    stages = value.get("stages")
    if not isinstance(stages, list) or len(stages) != MAX_STAGES:
        return False
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict) or not str(stage.get("stage_id") or "").strip():
            return False
        if not isinstance(stage.get("retrieval_jobs"), list) or not stage.get("retrieval_jobs"):
            return False
        if not isinstance(stage.get("pandas_execution_plan"), list):
            return False
        if not isinstance(stage.get("output_contract"), dict):
            return False
        if index == 0 and not isinstance(stage.get("handoff"), dict):
            return False
        if index == 1 and not isinstance(stage.get("input_bindings"), list):
            return False
    return True


# 함수 설명: Catalog로 증명된 stage2라도 선택 근거와 요청 출력 증거가 없으면 완전한 stage1 계획으로 축소합니다.
def _catalog_unselected_stage2_fallback(
    flat_plan: dict[str, Any],
    dependent_plan: dict[str, Any],
    metadata_candidates_value: Any,
    payload_value: Any,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not _dependent_bindings_catalog_proven(dependent_plan, metadata_candidates_value):
        return None
    stages = [deepcopy(item) for item in dependent_plan.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return None
    metadata_refs = flat_plan.get("metadata_refs")
    if not isinstance(metadata_refs, list) or not metadata_refs:
        return None
    selected_datasets = _explicit_table_catalog_refs(metadata_refs)
    stage2_datasets = {
        str(job.get("dataset_key") or "").strip()
        for job in stages[1].get("retrieval_jobs", [])
        if isinstance(job, dict) and str(job.get("dataset_key") or "").strip()
    }
    if not stage2_datasets or stage2_datasets.intersection(selected_datasets):
        return None
    payload = _payload(payload_value)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or payload.get("question") or "").strip()
    if _stage2_delta_has_question_evidence(question, stages[0], stages[1]):
        return None

    stage1 = stages[0]
    _normalize_stage_node_aliases(stage1)
    projected = deepcopy(flat_plan)
    _project_stage(projected, stage1)
    projected.pop("dependent_retrieval_plan", None)
    complete = _independently_complete_flat_plan(projected, metadata_candidates_value)
    if complete is None:
        return None
    evidence = {
        "policy": "catalog_selection_and_requested_output_delta",
        "selected_table_catalog_refs": sorted(selected_datasets),
        "unselected_stage2_datasets": sorted(stage2_datasets),
        "stage2_delta_columns": sorted(
            set(_contract_result_columns(stages[1].get("output_contract") or {}))
            - set(_contract_result_columns(stage1.get("output_contract") or {}))
        ),
        "question_evidence": False,
    }
    return complete, evidence


# 함수 설명: metadata_refs에서 사용자가 선택한 Table Catalog dataset key만 추출합니다.
def _explicit_table_catalog_refs(metadata_refs: list[Any]) -> set[str]:
    result: set[str] = set()
    for item in metadata_refs:
        if not isinstance(item, dict):
            continue
        section = str(item.get("section") or item.get("type") or "").strip().lower()
        section = re.sub(r"[^a-z0-9]+", "_", section).strip("_")
        if section not in {"table_catalog", "table_catalog_items"}:
            continue
        key = str(item.get("key") or item.get("dataset_key") or "").strip()
        if key:
            result.add(key)
    return result


# 함수 설명: stage2에서 새로 추가되는 canonical column 또는 전체 label 표현이 질문에 명시됐는지 확인합니다.
def _stage2_delta_has_question_evidence(
    question: str,
    stage1: dict[str, Any],
    stage2: dict[str, Any],
) -> bool:
    stage1_contract = stage1.get("output_contract") if isinstance(stage1.get("output_contract"), dict) else {}
    stage2_contract = stage2.get("output_contract") if isinstance(stage2.get("output_contract"), dict) else {}
    delta = [
        column
        for column in _contract_result_columns(stage2_contract)
        if column not in set(_contract_result_columns(stage1_contract))
    ]
    if not delta:
        return False
    labels = stage2_contract.get("column_labels") if isinstance(stage2_contract.get("column_labels"), dict) else {}
    normalized_question = _semantic_text(question)
    if not normalized_question:
        return False
    candidates = [*delta, *[labels.get(column) for column in delta]]
    return any(
        normalized and normalized in normalized_question
        for normalized in (_semantic_text(value) for value in candidates)
    )


# 함수 설명: 의미 표현의 완전 포함 비교를 위해 글자·숫자만 남겨 대소문자를 통일합니다.
def _semantic_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").casefold())


# 함수 설명: 최종 DAG와 output metric에서 도달하지 않는 branch와 retrieval job만 보수적으로 제거합니다.
def _prune_unreachable_flat_branches(
    plan_value: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    plan = deepcopy(plan_value)
    steps = [deepcopy(item) for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    empty_trace = {"pruned_step_count": 0, "pruned_job_count": 0}
    if len(steps) < 2 or not jobs:
        return plan, empty_trace

    producer: dict[str, int] = {}
    for index, step in enumerate(steps):
        for key in ("node_id", "output_alias"):
            alias = str(step.get(key) or "").strip()
            if not alias:
                continue
            if alias in producer and producer[alias] != index:
                return plan, empty_trace
            producer[alias] = index
    job_aliases: dict[str, int] = {}
    for index, job in enumerate(jobs):
        for value in (job.get("source_alias"), job.get("dataset_key")):
            alias = str(value or "").strip()
            if alias:
                job_aliases.setdefault(alias, index)

    reachable_steps: set[int] = {len(steps) - 1}
    reachable_jobs: set[int] = set()
    queue = [len(steps) - 1]
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    for binding in contract.get("metric_bindings", []) if isinstance(contract.get("metric_bindings"), list) else []:
        if not isinstance(binding, dict):
            continue
        reference = str(binding.get("source_alias") or "").strip()
        if reference in producer:
            index = producer[reference]
            if index not in reachable_steps:
                reachable_steps.add(index)
                queue.append(index)
        elif reference in job_aliases:
            reachable_jobs.add(job_aliases[reference])
        elif reference:
            return plan, empty_trace

    while queue:
        index = queue.pop()
        for item in steps[index].get("inputs", []) if isinstance(steps[index].get("inputs"), list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("kind") or "").strip() != "node_output":
                continue
            reference = str(item.get("ref") or "").strip()
            if reference and reference not in producer:
                # A weak model sometimes labels the previous filtered frame with
                # the raw source alias.  Its lineage is ambiguous, so pruning
                # must not silently bypass that predecessor.
                return plan, empty_trace
        references = _step_source_references(steps[index])
        if not references and index > 0:
            return plan, empty_trace
        for reference in references:
            if reference in producer and producer[reference] != index:
                dependency = producer[reference]
                if dependency not in reachable_steps:
                    reachable_steps.add(dependency)
                    queue.append(dependency)
            elif reference in job_aliases:
                reachable_jobs.add(job_aliases[reference])
            elif reference not in producer:
                return plan, empty_trace

    if not reachable_jobs:
        return plan, empty_trace
    kept_steps = [step for index, step in enumerate(steps) if index in reachable_steps]
    kept_jobs = [job for index, job in enumerate(jobs) if index in reachable_jobs]
    plan["pandas_execution_plan"] = kept_steps
    plan["retrieval_jobs"] = kept_jobs
    return plan, {
        "pruned_step_count": len(steps) - len(kept_steps),
        "pruned_job_count": len(jobs) - len(kept_jobs),
    }


# 주요 함수: top-level flat 실행 그래프만으로 결과 계약을 완성할 수 있는지 검증하고 불필요한 job을 제거합니다.
def _independently_complete_flat_plan(
    plan: dict[str, Any],
    metadata_candidates_value: Any,
) -> dict[str, Any] | None:
    working_plan, _ = _prune_unreachable_flat_branches(plan)
    steps = [deepcopy(item) for item in working_plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    output_contract = working_plan.get("output_contract") if isinstance(working_plan.get("output_contract"), dict) else {}
    output_columns = _contract_result_columns(output_contract)
    jobs = [deepcopy(item) for item in working_plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    if not steps or not output_columns or not jobs:
        return None

    aliases: dict[str, dict[str, Any]] = {}
    canonical_alias_by_job_id: dict[int, str] = {}
    for job in jobs:
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        dataset_key = str(job.get("dataset_key") or "").strip()
        if not alias or not dataset_key or alias in aliases:
            return None
        canonical_alias_by_job_id[id(job)] = alias
        aliases[alias] = job
        aliases.setdefault(dataset_key, job)

    node_outputs: set[str] = set()
    for step in steps:
        for key in ("node_id", "output_alias"):
            text = str(step.get(key) or "").strip()
            if text:
                node_outputs.add(text)

    referenced_aliases: set[str] = set()
    unresolved_references: set[str] = set()
    for step in steps:
        for reference in _step_source_references(step):
            if reference in aliases:
                referenced_aliases.add(canonical_alias_by_job_id[id(aliases[reference])])
            elif reference in node_outputs:
                continue
            else:
                unresolved_references.add(reference)
    for binding in output_contract.get("metric_bindings", []) if isinstance(output_contract.get("metric_bindings"), list) else []:
        if not isinstance(binding, dict):
            continue
        reference = str(binding.get("source_alias") or "").strip()
        if not reference:
            continue
        if reference in aliases:
            referenced_aliases.add(canonical_alias_by_job_id[id(aliases[reference])])
        elif reference not in node_outputs:
            unresolved_references.add(reference)
    if unresolved_references:
        return None
    if not referenced_aliases:
        if len(jobs) != 1:
            return None
        referenced_aliases.add(canonical_alias_by_job_id[id(jobs[0])])

    kept_jobs = [
        job
        for job in jobs
        if canonical_alias_by_job_id[id(job)] in referenced_aliases
    ]
    if not kept_jobs:
        return None
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    available_columns: set[str] = set()
    for job in kept_jobs:
        dataset_key = str(job.get("dataset_key") or "").strip()
        catalog = catalogs.get(dataset_key)
        if not isinstance(catalog, dict):
            return None
        params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
        if any(_blank(params.get(name)) for name in _catalog_required_params(catalog)):
            return None
        available_columns.update(_catalog_canonical_columns(catalog))

    available_columns.update(_flat_derived_columns(steps, output_contract))
    if any(column not in available_columns for column in output_columns):
        return None

    result = deepcopy(working_plan)
    result["retrieval_jobs"] = kept_jobs
    result.pop("dependent_retrieval_plan", None)
    return result


# 함수 설명: flat step이 외부 source 또는 앞선 node output을 참조하는 모든 식별자를 수집합니다.
def _step_source_references(step: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        step.get("source_alias"),
        step.get("left_source_alias"),
        step.get("right_source_alias"),
    ]
    for item in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
        if isinstance(item, dict):
            values.append(item.get("ref"))
    return _strings(values)


# 함수 설명: aggregation/projection/metric binding이 생성하거나 전달하는 결과 컬럼을 수집합니다.
def _flat_derived_columns(
    steps: list[dict[str, Any]],
    output_contract: dict[str, Any],
) -> set[str]:
    values: list[Any] = []
    for step in steps:
        for key in ("group_by", "projection", "columns", "right_value_columns"):
            raw = step.get(key)
            if isinstance(raw, list):
                values.extend(raw)
        for aggregation in step.get("aggregations", []) if isinstance(step.get("aggregations"), list) else []:
            if isinstance(aggregation, dict):
                values.append(aggregation.get("output_column") or aggregation.get("column"))
        values.extend((step.get("output_column"), step.get("sort_by")))
    for binding in output_contract.get("metric_bindings", []) if isinstance(output_contract.get("metric_bindings"), list) else []:
        if isinstance(binding, dict):
            values.append(binding.get("output_column"))
    return set(_strings(values))


# 함수 설명: 결과 계약의 대표 컬럼 목록을 순서와 무관한 비교용 목록으로 정규화합니다.
def _contract_result_columns(contract: dict[str, Any]) -> list[str]:
    for key in ("result_columns", "required_columns"):
        columns = _strings(contract.get(key))
        if columns:
            return columns
    return _strings(
        [
            *_strings(contract.get("grain_columns")),
            *_strings(contract.get("metric_columns")),
        ]
    )


# 함수 설명: 종속 최종 stage가 top-level 요청 결과 계약과 의미상 같은 컬럼 집합인지 확인합니다.
def _dependent_final_contract_coherent(
    flat_plan: dict[str, Any],
    dependent_plan: dict[str, Any],
) -> bool:
    stages = [item for item in dependent_plan.get("stages", []) if isinstance(item, dict)]
    if not stages:
        return False
    flat_contract = flat_plan.get("output_contract") if isinstance(flat_plan.get("output_contract"), dict) else {}
    final_contract = stages[-1].get("output_contract") if isinstance(stages[-1].get("output_contract"), dict) else {}
    flat_columns = set(_contract_result_columns(flat_contract))
    final_columns = set(_contract_result_columns(final_contract))
    return bool(flat_columns) and flat_columns == final_columns


# 함수 설명: stage2의 비어 있는 필수 파라미터가 선택 Catalog의 upstream binding으로 정확히 증명되는지 확인합니다.
def _dependent_bindings_catalog_proven(
    dependent_plan: dict[str, Any],
    metadata_candidates_value: Any,
) -> bool:
    stages = [item for item in dependent_plan.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return False
    first_jobs = [item for item in stages[0].get("retrieval_jobs", []) if isinstance(item, dict)]
    second_jobs = [item for item in stages[1].get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(first_jobs) != 1 or len(second_jobs) != 1:
        return False
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    first_catalog = catalogs.get(str(first_jobs[0].get("dataset_key") or "").strip())
    second_catalog = catalogs.get(str(second_jobs[0].get("dataset_key") or "").strip())
    if not isinstance(first_catalog, dict) or not isinstance(second_catalog, dict):
        return False
    handoff = stages[0].get("handoff") if isinstance(stages[0].get("handoff"), dict) else {}
    handoff_columns = set(_strings(handoff.get("columns")))
    if not handoff_columns or not handoff_columns.issubset(set(_catalog_canonical_columns(first_catalog))):
        return False
    second_job = second_jobs[0]
    supplied = second_job.get("required_params") if isinstance(second_job.get("required_params"), dict) else {}
    missing = {name for name in _catalog_required_params(second_catalog) if _blank(supplied.get(name))}
    if not missing:
        return False
    trusted = _catalog_upstream_bindings(second_catalog)
    second_alias = str(second_job.get("source_alias") or second_job.get("dataset_key") or "").strip()
    covered: set[str] = set()
    for binding in stages[1].get("input_bindings", []) if isinstance(stages[1].get("input_bindings"), list) else []:
        if not isinstance(binding, dict):
            return False
        if str(binding.get("target_source_alias") or "").strip() != second_alias:
            return False
        source_column = str(binding.get("source_column") or "").strip()
        target_param = str(binding.get("target_param") or "").strip()
        operator = str(binding.get("operator") or "in").strip().lower()
        entity_type = str(binding.get("entity_type") or "").strip()
        matches = [
            item
            for item in trusted
            if str(item.get("source_column") or "").strip() == source_column
            and str(item.get("target_param") or "").strip() == target_param
            and str(item.get("operator") or "in").strip().lower() == operator
            and _canonical_reference_alias(item.get("source_alias")) == "upstream_result"
            and (not entity_type or str(item.get("entity_type") or "").strip() == entity_type)
        ]
        if len(matches) != 1 or source_column not in handoff_columns or target_param in covered:
            return False
        covered.add(target_param)
    return covered == missing


# 함수 설명: 약한 모델이 일반 flat plan 옆에 붙인 의미 없는 continuation 자리표시자만 식별합니다.
def _is_empty_dependent_placeholder(value: dict[str, Any]) -> bool:
    allowed = {
        "version",
        "max_stages",
        "stages",
        "plan_id",
        "plan_hash",
        "final_stage_id",
        "depends_on",
        "input_bindings",
        "runtime",
        "activation",
    }
    if any(str(key) not in allowed for key in value):
        return False
    version = str(value.get("version") or "").strip()
    if version and version != CONTRACT_VERSION:
        return False
    max_stages = value.get("max_stages")
    if str(max_stages or "").strip() not in {"", "0", "1", str(MAX_STAGES)}:
        return False
    stages = value.get("stages")
    if stages not in (None, []):
        return False
    for key in (
        "plan_id",
        "plan_hash",
        "final_stage_id",
        "depends_on",
        "input_bindings",
        "runtime",
        "activation",
    ):
        if not _structurally_empty(value.get(key)):
            return False
    return True


# 함수 설명: placeholder 필드가 문자열·목록·객체 어느 형태에서도 실제 의미를 갖지 않는지 판정합니다.
def _structurally_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


# 주요 함수: 빈 필수 파라미터와 trusted upstream binding이 정확히 대응할 때만 flat plan을 2단계로 승격합니다.
def _lift_flat_plan_from_catalog(
    plan: dict[str, Any],
    metadata_candidates_value: Any,
) -> dict[str, Any]:
    """Lift only a catalog-proven missing-required-param dependency into two stages."""

    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(jobs) != 2:
        return {}
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    candidates: list[tuple[int, list[str], list[dict[str, Any]]]] = []
    for index, job in enumerate(jobs):
        dataset_key = str(job.get("dataset_key") or "").strip()
        catalog = catalogs.get(dataset_key, {})
        required = _catalog_required_params(catalog)
        params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
        missing = [name for name in required if _blank(params.get(name))]
        bindings = _catalog_upstream_bindings(catalog)
        applicable = [
            binding
            for binding in bindings
            if str(binding.get("target_param") or "").strip() in missing
            and str(binding.get("source_column") or "").strip()
            and str(binding.get("operator") or "in").strip().lower() in {"eq", "in"}
        ]
        if missing and applicable and {str(item.get("target_param") or "").strip() for item in applicable} == set(missing):
            candidates.append((index, missing, applicable))
    if len(candidates) != 1:
        return {}
    second_index, _, bindings = candidates[0]
    first_index = 1 - second_index
    first_job, second_job = jobs[first_index], jobs[second_index]
    first_alias = str(first_job.get("source_alias") or first_job.get("dataset_key") or "").strip()
    second_alias = str(second_job.get("source_alias") or second_job.get("dataset_key") or "").strip()
    if not first_alias or not second_alias or first_alias == second_alias:
        return {}
    first_catalog = catalogs.get(str(first_job.get("dataset_key") or "").strip(), {})
    first_columns = set(_catalog_canonical_columns(first_catalog))
    handoff_columns = _strings([binding.get("source_column") for binding in bindings])
    if not handoff_columns or any(column not in first_columns for column in handoff_columns):
        return {}
    final_contract = deepcopy(plan.get("output_contract")) if isinstance(plan.get("output_contract"), dict) else {}
    final_columns = _strings(final_contract.get("result_columns"))
    stage1_columns = [column for column in final_columns if column in first_columns]
    stage1_columns = _strings([*stage1_columns, *handoff_columns])
    if not stage1_columns:
        return {}
    stage1_id = f"stage_1_{_safe_id(first_alias)}"
    stage2_id = f"stage_2_{_safe_id(second_alias)}"
    stage1_plan = [
        {
            "node_id": f"select_{_safe_id(first_alias)}_handoff",
            "operation": "select_columns",
            "inputs": [{"kind": "external_source", "ref": first_alias}],
            "source_alias": first_alias,
            "output_alias": f"{_safe_id(first_alias)}_handoff",
            "projection": stage1_columns,
        }
    ]
    stage1_contract = {
        "result_mode": "entity_list",
        "required_columns": stage1_columns,
        "grain_columns": handoff_columns,
        "metric_columns": [],
        "result_columns": stage1_columns,
        "strict_result_columns": True,
        "null_group_policy": "preserve_as_blank",
        "metric_null_policy": "display_zero",
    }
    stage2_plan = _rewrite_first_source_as_upstream(
        plan.get("pandas_execution_plan"),
        first_alias,
    )
    normalized_bindings = [
        {
            "source_stage_id": stage1_id,
            "source_column": str(binding.get("source_column") or "").strip(),
            "target_source_alias": second_alias,
            "target_param": str(binding.get("target_param") or "").strip(),
            "operator": str(binding.get("operator") or "in").strip().lower(),
            "entity_type": str(binding.get("entity_type") or "").strip(),
        }
        for binding in bindings
    ]
    return {
        "version": CONTRACT_VERSION,
        "max_stages": MAX_STAGES,
        "final_stage_id": stage2_id,
        "activation": {
            "reason": "catalog_required_param_dependency",
            "source": "table_catalog.upstream_bindings",
        },
        "stages": [
            {
                "stage_id": stage1_id,
                "depends_on": [],
                "retrieval_jobs": [first_job],
                "pandas_execution_plan": stage1_plan,
                "output_contract": stage1_contract,
                "handoff": {"columns": handoff_columns, "require_complete": True},
            },
            {
                "stage_id": stage2_id,
                "depends_on": [stage1_id],
                "retrieval_jobs": [second_job],
                "pandas_execution_plan": stage2_plan,
                "output_contract": final_contract,
                "input_bindings": normalized_bindings,
            },
        ],
    }


# 함수 설명: metadata candidates의 Table Catalog를 dataset_key 기준 dict로 정리합니다.
def _catalog_by_dataset(value: Any) -> dict[str, dict[str, Any]]:
    data = getattr(value, "data", value)
    if isinstance(data, dict) and isinstance(data.get("metadata_candidates"), dict):
        data = data["metadata_candidates"]
    items = data.get("table_catalog_items") if isinstance(data, dict) else []
    result: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        dataset_key = str(
            item.get("key")
            or item.get("dataset_key")
            or payload.get("dataset_key")
            or payload.get("key")
            or ""
        ).strip()
        if dataset_key:
            result[dataset_key] = deepcopy(payload)
    return result


# 함수 설명: Catalog 루트 또는 source_config에서 필수 조회 파라미터 이름을 읽습니다.
def _catalog_required_params(catalog: dict[str, Any]) -> list[str]:
    source_config = catalog.get("source_config") if isinstance(catalog.get("source_config"), dict) else {}
    raw = catalog.get("required_params") or source_config.get("required_params") or []
    if isinstance(raw, dict):
        raw = list(raw)
    return _strings(raw)


# 함수 설명: Catalog 루트 또는 source_config의 trusted upstream binding 목록을 복사합니다.
def _catalog_upstream_bindings(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    source_config = catalog.get("source_config") if isinstance(catalog.get("source_config"), dict) else {}
    raw = catalog.get("upstream_bindings")
    if not isinstance(raw, list):
        raw = source_config.get("upstream_bindings")
    return [deepcopy(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


# 함수 설명: Catalog에 선언된 canonical dimension·metric·filter 컬럼 이름을 수집합니다.
def _catalog_canonical_columns(catalog: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("canonical_columns", "columns"):
        raw = catalog.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    values.append(
                        item.get("canonical_key")
                        or item.get("standard_name")
                        or item.get("column_name")
                        or item.get("name")
                    )
                else:
                    values.append(item)
    for key in ("filter_mappings", "standard_column_aliases", "metric_semantics"):
        mapping = catalog.get(key)
        if isinstance(mapping, dict):
            values.extend(mapping.keys())
    return _strings(values)


# 함수 설명: 두 번째 단계의 첫 source 참조만 예약 upstream_result 별칭으로 바꿉니다.
def _rewrite_first_source_as_upstream(value: Any, source_alias: str) -> list[dict[str, Any]]:
    steps = [deepcopy(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    for step in steps:
        if str(step.get("source_alias") or "").strip() == source_alias:
            step["source_alias"] = "upstream_result"
        if str(step.get("left_source_alias") or "").strip() == source_alias:
            step["left_source_alias"] = "upstream_result"
        if str(step.get("right_source_alias") or "").strip() == source_alias:
            step["right_source_alias"] = "upstream_result"
        for item in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
            if isinstance(item, dict) and item.get("kind") == "external_source" and str(item.get("ref") or "").strip() == source_alias:
                item["ref"] = "upstream_result"
    return steps


# 함수 설명: source alias를 stage와 node ID에 사용할 수 있는 소문자 식별자로 변환합니다.
def _safe_id(value: Any) -> str:
    return re.sub(r"[^0-9a-z]+", "_", str(value or "").strip().lower()).strip("_") or "source"


# 함수 설명: None·빈 문자열·빈 collection을 필수 파라미터 미확정 값으로 판정합니다.
def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


# 주요 함수: 2단계 shape, 의존 방향, handoff, binding, 크기 제한을 모두 fail-closed 검증합니다.
def _repair_recoverable_dependent_shape(
    root_plan: dict[str, Any],
    dependent: dict[str, Any],
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], bool]:
    """Repair only a provable two-stage shape emitted by a weak model.

    Models occasionally wrap the same two retrieval jobs in an extra planning
    stage or omit the stage wrappers while still returning the jobs and the
    Catalog-backed handoff.  We can safely rebuild the wrappers when there are
    exactly two unique jobs, one Catalog with a trusted upstream binding, and a
    complete first-stage handoff.  No dataset, column, parameter value, or
    stage is guessed from HOLD/LOT wording.  Ambiguous plans are returned
    unchanged and remain fail-closed in the strict validator below.
    """
    stages = dependent.get("stages") if isinstance(dependent.get("stages"), list) else []
    if len(stages) == MAX_STAGES:
        return deepcopy(dependent), False

    catalogs = _catalog_by_dataset(metadata_candidates_value)
    stage_candidates = [item for item in stages if isinstance(item, dict)]
    job_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    # 함수 설명: stage 안의 retrieval job을 dataset·source alias 기준으로 중복 없이 수집합니다.
    def add_job(job: Any) -> None:
        if not isinstance(job, dict):
            return
        dataset = str(job.get("dataset_key") or "").strip()
        alias = str(job.get("source_alias") or dataset).strip()
        if not dataset or not alias:
            return
        job_by_key.setdefault((dataset, alias), deepcopy(job))

    for stage in stage_candidates:
        for job in stage.get("retrieval_jobs", []) if isinstance(stage.get("retrieval_jobs"), list) else []:
            add_job(job)
    for job in root_plan.get("retrieval_jobs", []) if isinstance(root_plan.get("retrieval_jobs"), list) else []:
        add_job(job)
    if len(job_by_key) != MAX_STAGES:
        return deepcopy(dependent), False

    jobs = list(job_by_key.values())
    dependent_job_indexes = [
        index for index, job in enumerate(jobs)
        if isinstance(catalogs.get(str(job.get("dataset_key") or "").strip()), dict)
        and _catalog_upstream_bindings(catalogs[str(job.get("dataset_key") or "").strip()])
    ]
    if len(dependent_job_indexes) != 1:
        return deepcopy(dependent), False
    stage2_job = jobs[dependent_job_indexes[0]]
    stage1_job = jobs[1 - dependent_job_indexes[0]]
    stage2_dataset = str(stage2_job.get("dataset_key") or "").strip()
    stage1_dataset = str(stage1_job.get("dataset_key") or "").strip()
    stage2_catalog = catalogs.get(stage2_dataset)
    stage1_catalog = catalogs.get(stage1_dataset)
    if not isinstance(stage1_catalog, dict) or not isinstance(stage2_catalog, dict):
        return deepcopy(dependent), False
    trusted_bindings = _catalog_upstream_bindings(stage2_catalog)
    if not trusted_bindings:
        return deepcopy(dependent), False

    # 함수 설명: 지정 dataset을 조회하는 기존 stage를 찾아 복구 가능한 stage 계획으로 반환합니다.
    def stage_for_dataset(dataset: str) -> dict[str, Any] | None:
        matches = [
            stage for stage in stage_candidates
            if any(
                isinstance(job, dict) and str(job.get("dataset_key") or "").strip() == dataset
                for job in stage.get("retrieval_jobs", []) if isinstance(stage.get("retrieval_jobs"), list)
            )
        ]
        return deepcopy(matches[0]) if len(matches) == 1 else None

    original_stage1 = stage_for_dataset(stage1_dataset)
    original_stage2 = stage_for_dataset(stage2_dataset)
    if original_stage1 is None and original_stage2 is None and stage_candidates:
        return deepcopy(dependent), False

    stage1 = original_stage1 or {"pandas_execution_plan": [], "output_contract": {}}
    stage2 = original_stage2 or {"pandas_execution_plan": [], "output_contract": {}}
    stage1_id = str(stage1.get("stage_id") or "stage_1").strip() or "stage_1"
    stage2_id = str(stage2.get("stage_id") or "stage_2").strip() or "stage_2"
    if stage1_id == stage2_id:
        stage2_id = "stage_2"
    stage1["stage_id"] = stage1_id
    stage1["depends_on"] = []
    stage1["retrieval_jobs"] = [deepcopy(stage1_job)]
    stage2["stage_id"] = stage2_id
    stage2["depends_on"] = [stage1_id]
    stage2["retrieval_jobs"] = [deepcopy(stage2_job)]

    first_contract = stage1.get("output_contract") if isinstance(stage1.get("output_contract"), dict) else {}
    first_result_columns = _strings(first_contract.get("result_columns"))
    if not first_result_columns:
        first_result_columns = _strings(first_contract.get("required_columns"))
    trusted_first_columns = set(_catalog_canonical_columns(stage1_catalog))
    handoff = stage1.get("handoff") if isinstance(stage1.get("handoff"), dict) else {}
    handoff_columns = _strings(handoff.get("columns"))
    if not handoff_columns:
        handoff_columns = [column for column in first_result_columns if column in trusted_first_columns]
    if not handoff_columns:
        # A weak model can omit the stage-1 wrapper while still declaring a
        # trusted stage-2 binding.  Reusing that binding's source column is
        # safe because the Catalog already proves the handoff relationship;
        # no question-specific LOT/ID column is invented here.
        handoff_columns = [
            str(binding.get("source_column") or "").strip()
            for binding in trusted_bindings
            if str(binding.get("source_column") or "").strip() in trusted_first_columns
        ]
        handoff_columns = list(dict.fromkeys(handoff_columns))
    if not handoff_columns or any(column not in trusted_first_columns for column in handoff_columns):
        return deepcopy(dependent), False
    first_contract["result_columns"] = first_result_columns or handoff_columns
    stage1["output_contract"] = first_contract
    stage1["handoff"] = {"columns": handoff_columns, "require_complete": True}

    second_required = _catalog_required_params(stage2_catalog)
    existing_bindings = [item for item in stage2.get("input_bindings", []) if isinstance(item, dict)]
    bindings: list[dict[str, Any]] = []
    for trusted in trusted_bindings:
        source_column = str(trusted.get("source_column") or "").strip()
        target_param = str(trusted.get("target_param") or "").strip()
        operator = str(trusted.get("operator") or "in").strip().lower()
        if not source_column or not target_param or target_param not in second_required:
            continue
        if source_column not in handoff_columns:
            continue
        existing = next(
            (item for item in existing_bindings
             if str(item.get("target_param") or "").strip() == target_param),
            {},
        )
        binding = deepcopy(existing) if existing else {}
        binding.update({
            "source_stage_id": stage1_id,
            "source_column": source_column,
            "target_source_alias": str(stage2_job.get("source_alias") or stage2_dataset).strip(),
            "target_param": target_param,
            "operator": operator,
        })
        for key in ("source_alias", "entity_type"):
            if _blank(binding.get(key)) and not _blank(trusted.get(key)):
                binding[key] = deepcopy(trusted[key])
        bindings.append(binding)
    if set(second_required) - {str(item.get("target_param") or "").strip() for item in bindings}:
        return deepcopy(dependent), False
    stage2["input_bindings"] = bindings
    stage2.setdefault("output_contract", {})
    repaired = deepcopy(dependent)
    repaired["version"] = CONTRACT_VERSION
    repaired["max_stages"] = MAX_STAGES
    repaired["final_stage_id"] = stage2_id
    repaired["stages"] = [stage1, stage2]
    return repaired, True


# 함수 설명: continuation plan을 canonical stage·binding·catalog 계약으로 정규화하고 검증합니다.
def _normalize_and_validate_plan(
    value: dict[str, Any],
    metadata_candidates_value: Any,
) -> dict[str, Any]:
    plan = deepcopy(value)
    version = str(plan.get("version") or CONTRACT_VERSION).strip()
    if version != CONTRACT_VERSION:
        raise ValueError(f"지원하지 않는 dependent retrieval 계약 버전입니다: {version}")
    plan["version"] = version
    stages = [deepcopy(item) for item in plan.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        raise ValueError("dependent retrieval 계약은 정확히 2단계여야 합니다.")
    if int(plan.get("max_stages") or MAX_STAGES) != MAX_STAGES:
        raise ValueError("dependent retrieval max_stages는 2여야 합니다.")
    plan["max_stages"] = MAX_STAGES
    for stage in stages:
        _normalize_stage_node_aliases(stage)
    _normalize_continuation_cross_stage_references(stages)
    stage_ids: list[str] = []
    for index, stage in enumerate(stages):
        stage_id = str(stage.get("stage_id") or "").strip()
        if not stage_id or stage_id in stage_ids:
            raise ValueError("각 dependent retrieval stage에는 고유한 stage_id가 필요합니다.")
        stage["stage_id"] = stage_id
        dependencies = _strings(stage.get("depends_on"))
        if index == 0 and dependencies:
            raise ValueError("첫 단계는 다른 단계에 의존할 수 없습니다.")
        if index == 1 and dependencies != [stage_ids[0]]:
            raise ValueError("두 번째 단계는 첫 번째 단계 하나에만 의존해야 합니다.")
        stage["depends_on"] = dependencies
        jobs = [item for item in stage.get("retrieval_jobs", []) if isinstance(item, dict)]
        if not jobs:
            raise ValueError(f"{stage_id} 단계의 retrieval_jobs가 비어 있습니다.")
        if not isinstance(stage.get("pandas_execution_plan"), list):
            raise ValueError(f"{stage_id} 단계의 pandas_execution_plan이 유효하지 않습니다.")
        stage_steps = [
            deepcopy(item)
            for item in stage.get("pandas_execution_plan", [])
            if isinstance(item, dict)
        ]
        for step in stage_steps:
            _normalize_safe_extreme_tie_policy(step)
        stage["pandas_execution_plan"] = stage_steps
        if not isinstance(stage.get("output_contract"), dict):
            raise ValueError(f"{stage_id} 단계의 output_contract가 유효하지 않습니다.")
        stage_contract = deepcopy(stage["output_contract"])
        if not _strings(stage_contract.get("result_columns")):
            stage_contract["result_columns"] = _strings(stage_contract.get("required_columns"))
        stage["output_contract"] = stage_contract
        if index == 0:
            handoff = stage.get("handoff") if isinstance(stage.get("handoff"), dict) else {}
            columns = _strings(handoff.get("columns"))
            result_columns = _strings(stage["output_contract"].get("result_columns"))
            if not columns or any(column not in result_columns for column in columns):
                raise ValueError("첫 단계 handoff.columns는 첫 단계 result_columns에 모두 포함되어야 합니다.")
            handoff["columns"] = columns
            handoff["require_complete"] = True
            stage["handoff"] = handoff
        else:
            bindings = [item for item in stage.get("input_bindings", []) if isinstance(item, dict)]
            if not bindings:
                raise ValueError("두 번째 단계에는 metadata 기반 input_bindings가 필요합니다.")
            for binding in bindings:
                if str(binding.get("source_stage_id") or "").strip() != stage_ids[0]:
                    raise ValueError("input_binding.source_stage_id가 첫 단계와 일치하지 않습니다.")
                if not str(binding.get("source_column") or "").strip():
                    raise ValueError("input_binding.source_column이 비어 있습니다.")
                if not str(binding.get("target_param") or "").strip():
                    raise ValueError("input_binding.target_param이 비어 있습니다.")
                if str(binding.get("operator") or "in").strip().lower() not in {"eq", "in"}:
                    raise ValueError("input_binding.operator는 eq 또는 in만 허용됩니다.")
            stage["input_bindings"] = bindings
        stage_ids.append(stage_id)
    plan["stages"] = stages
    plan["final_stage_id"] = str(plan.get("final_stage_id") or stages[-1]["stage_id"]).strip()
    if plan["final_stage_id"] != stages[-1]["stage_id"]:
        raise ValueError("final_stage_id는 두 번째 단계여야 합니다.")
    _hydrate_catalog_binding_metadata(plan, metadata_candidates_value)
    _validate_plan_against_catalog(plan, metadata_candidates_value)
    encoded = json.dumps(plan, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > MAX_INTERNAL_PLAN_BYTES:
        raise ValueError("dependent retrieval 계약이 허용 크기를 초과했습니다.")
    return plan


# 함수 설명: 각 stage의 명시 node_id와 output_alias를 동일한 안정 참조 공간에 배치합니다.
def _normalize_stage_node_aliases(stage: dict[str, Any]) -> None:
    """Make implicit node-id lineage explicit without inventing data semantics."""

    stage_id = str(stage.get("stage_id") or "stage").strip() or "stage"
    steps = [
        deepcopy(item)
        for item in stage.get("pandas_execution_plan", [])
        if isinstance(item, dict)
    ]
    owners: dict[str, int] = {}
    for index, step in enumerate(steps):
        operation = str(step.get("operation") or step.get("step") or "operation").strip().lower()
        safe_operation = re.sub(r"[^0-9a-z]+", "_", operation).strip("_") or "operation"
        output_alias = str(step.get("output_alias") or step.get("result_alias") or "").strip()
        node_id = str(step.get("node_id") or "").strip()
        if not node_id:
            node_id = output_alias or f"{stage_id}_step_{index + 1}_{safe_operation}"
        if not output_alias:
            output_alias = node_id
        step["node_id"] = node_id
        step["output_alias"] = output_alias
        for alias in {node_id, output_alias}:
            previous_owner = owners.get(alias)
            if previous_owner is not None and previous_owner != index:
                raise ValueError("stage 내부 node_id/output_alias가 서로 충돌합니다.")
            owners[alias] = index
    external_aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in stage.get("retrieval_jobs", [])
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
    }
    alias_nodes = {
        alias: str(steps[index].get("node_id") or "").strip()
        for alias, index in owners.items()
    }
    for index, step in enumerate(steps):
        if isinstance(step.get("inputs"), list) and step.get("inputs"):
            continue
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation in {"join", "merge", "outer_join", "left_join", "compare_presence"}:
            aliases = _strings(
                [
                    step.get("left_source_alias") or step.get("source_alias"),
                    step.get("right_source_alias") or step.get("reference_source_alias"),
                ]
            )
        else:
            aliases = _strings([step.get("source_alias")])
        inputs: list[dict[str, str]] = []
        for alias in aliases:
            if alias in external_aliases:
                inputs.append({"kind": "external_source", "ref": alias})
                continue
            owner_index = owners.get(alias)
            if owner_index is not None and owner_index < index:
                inputs.append({"kind": "node_output", "ref": alias_nodes.get(alias) or alias})
                continue
            # An unresolved logical alias can be a stage-1 handoff reference;
            # cross-stage normalization below either rewrites it or validation
            # rejects it. It must never be guessed as a retrieval source.
            inputs.append({"kind": "node_output", "ref": alias})
        if inputs:
            step["inputs"] = inputs
    stage["pandas_execution_plan"] = steps


# 함수 설명: continuation stage2가 참조한 stage1 식별자를 예약 외부 source upstream_result로 정규화합니다.
def _normalize_continuation_cross_stage_references(stages: list[dict[str, Any]]) -> None:
    if len(stages) != MAX_STAGES:
        return
    aliases = _stage_output_reference_aliases(stages[0])
    if not aliases:
        return
    steps = [
        deepcopy(item)
        for item in stages[1].get("pandas_execution_plan", [])
        if isinstance(item, dict)
    ]
    _rewrite_cross_stage_references(
        steps,
        aliases,
        replacement="upstream_result",
        # V2 normalizer treats this reserved node_output ref as an external
        # previous-result provider. Using external_source here would first run
        # the generic Catalog leaf recovery and incorrectly demand a table job.
        input_kind="node_output",
    )
    stages[1]["pandas_execution_plan"] = steps


# 함수 설명: Catalog가 tie-breaker를 제공하지 않은 first 요청은 비결정 순서를 쓰지 않고 동률 오류 정책으로 강화합니다.
def _normalize_safe_extreme_tie_policy(step: dict[str, Any]) -> None:
    if str(step.get("operation") or "").strip().lower() != "select_extreme_row_per_group":
        return
    if step.get("strict") is not True:
        return
    tie_policy = str(step.get("tie_policy") or "").strip().lower()
    tie_breakers = step.get("tie_breakers") if isinstance(step.get("tie_breakers"), list) else []
    if tie_policy == "first" and not tie_breakers:
        step["tie_policy"] = "error"
        step["tie_breakers"] = []


# 함수 설명: 선언 binding의 비어 있는 entity_type만 유일하게 일치하는 trusted Catalog 값으로 보완합니다.
def _hydrate_catalog_binding_metadata(
    plan: dict[str, Any],
    metadata_candidates_value: Any,
) -> None:
    stages = plan.get("stages") if isinstance(plan.get("stages"), list) else []
    if len(stages) != MAX_STAGES:
        return
    second_stage = stages[1]
    second_jobs = [item for item in second_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(second_jobs) != 1:
        return
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    second_catalog = catalogs.get(str(second_jobs[0].get("dataset_key") or "").strip())
    if not isinstance(second_catalog, dict):
        return
    trusted = _catalog_upstream_bindings(second_catalog)
    for binding in second_stage.get("input_bindings", []) if isinstance(second_stage.get("input_bindings"), list) else []:
        if not isinstance(binding, dict) or str(binding.get("entity_type") or "").strip():
            continue
        source_column = str(binding.get("source_column") or "").strip()
        target_param = str(binding.get("target_param") or "").strip()
        operator = str(binding.get("operator") or "in").strip().lower()
        matches = [
            item
            for item in trusted
            if str(item.get("source_column") or "").strip() == source_column
            and str(item.get("target_param") or "").strip() == target_param
            and str(item.get("operator") or "in").strip().lower() == operator
            and _canonical_reference_alias(item.get("source_alias")) == "upstream_result"
        ]
        if len(matches) == 1:
            entity_type = str(matches[0].get("entity_type") or "").strip()
            if entity_type:
                binding["entity_type"] = entity_type
            trusted_source_alias = str(matches[0].get("source_alias") or "").strip()
            if _blank(binding.get("source_alias")) and trusted_source_alias:
                binding["source_alias"] = trusted_source_alias
            trusted_target_alias = str(matches[0].get("target_source_alias") or "").strip()
            if _blank(binding.get("target_source_alias")) and trusted_target_alias:
                binding["target_source_alias"] = trusted_target_alias


# 주요 함수: 명시 또는 승격된 모든 단계 계약을 선택된 trusted Catalog와 교차 검증합니다.
def _validate_plan_against_catalog(
    plan: dict[str, Any],
    metadata_candidates_value: Any,
) -> None:
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    stages = plan.get("stages") if isinstance(plan.get("stages"), list) else []
    first_stage = stages[0]
    second_stage = stages[1]
    first_jobs = [item for item in first_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    second_jobs = [item for item in second_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(first_jobs) != 1 or len(second_jobs) != 1:
        raise ValueError("초기 continuation 계약은 stage별 외부 retrieval job 하나만 허용합니다.")
    first_job, second_job = first_jobs[0], second_jobs[0]
    first_dataset = str(first_job.get("dataset_key") or "").strip()
    second_dataset = str(second_job.get("dataset_key") or "").strip()
    first_catalog = catalogs.get(first_dataset)
    second_catalog = catalogs.get(second_dataset)
    if not isinstance(first_catalog, dict) or not isinstance(second_catalog, dict):
        raise ValueError("dependent retrieval의 모든 dataset은 선택된 Table Catalog에 있어야 합니다.")
    handoff = first_stage.get("handoff") if isinstance(first_stage.get("handoff"), dict) else {}
    handoff_columns = _strings(handoff.get("columns"))
    trusted_first_columns = set(_catalog_canonical_columns(first_catalog))
    if any(column not in trusted_first_columns for column in handoff_columns):
        raise ValueError("첫 단계 handoff 컬럼이 trusted stage1 Catalog에 없습니다.")

    second_alias = str(second_job.get("source_alias") or second_dataset).strip()
    catalog_bindings = _catalog_upstream_bindings(second_catalog)
    normalized_catalog_bindings = {
        (
            str(item.get("source_column") or "").strip(),
            str(item.get("target_param") or "").strip(),
            str(item.get("operator") or "in").strip().lower(),
            _canonical_reference_alias(item.get("source_alias")),
            str(item.get("entity_type") or "").strip(),
        )
        for item in catalog_bindings
        if isinstance(item, dict)
    }
    declared = [item for item in second_stage.get("input_bindings", []) if isinstance(item, dict)]
    declared_targets: set[str] = set()
    for binding in declared:
        candidate = (
            str(binding.get("source_column") or "").strip(),
            str(binding.get("target_param") or "").strip(),
            str(binding.get("operator") or "in").strip().lower(),
            "upstream_result",
            str(binding.get("entity_type") or "").strip(),
        )
        if candidate not in normalized_catalog_bindings:
            raise ValueError("input_binding이 selected Table Catalog upstream_bindings와 일치하지 않습니다.")
        if str(binding.get("target_source_alias") or "").strip() != second_alias:
            raise ValueError("input_binding.target_source_alias가 stage2 retrieval alias와 일치하지 않습니다.")
        if candidate[0] not in handoff_columns:
            raise ValueError("input_binding.source_column must be included in stage1 handoff columns.")
        if candidate[1] in declared_targets:
            raise ValueError("A stage2 required parameter cannot have duplicate input bindings.")
        declared_targets.add(candidate[1])
    required = set(_catalog_required_params(second_catalog))
    supplied = second_job.get("required_params") if isinstance(second_job.get("required_params"), dict) else {}
    missing = {name for name in required if _blank(supplied.get(name))}
    if missing != declared_targets:
        raise ValueError("stage2에서 비어 있는 required_params와 trusted input_bindings가 정확히 대응하지 않습니다.")


# 함수 설명: 전체 종속 계획은 보존하면서 현재 stage 필드만 기존 flat intent plan에 투영합니다.
    # The selected stage-2 Catalog owns every source column consumed by a
    # deterministic extreme-row step. This blocks model-invented tie-breakers
    # and preserves fail-closed, source-row-order-independent execution.
    trusted_second_columns = set(_catalog_canonical_columns(second_catalog))
    for step in second_stage.get("pandas_execution_plan", []):
        if not isinstance(step, dict):
            continue
        if str(step.get("operation") or "").strip().lower() != "select_extreme_row_per_group":
            continue
        _validate_extreme_step_shape(step)
        untrusted_columns = [
            column for column in _extreme_step_columns(step) if column not in trusted_second_columns
        ]
        if untrusted_columns:
            raise ValueError(
                "select_extreme_row_per_group columns are missing from the selected stage2 Table Catalog: "
                + ", ".join(untrusted_columns)
            )


# 함수 설명: latest-row primitive의 필수 shape를 검증해 malformed 또는 비결정적 계약을 LLM fallback 전에 차단합니다.
def _validate_extreme_step_shape(step: dict[str, Any]) -> None:
    if step.get("strict") is not True:
        raise ValueError("select_extreme_row_per_group.strict must be true.")
    if not _strings(step.get("partition_by")):
        raise ValueError("select_extreme_row_per_group.partition_by is required.")
    order_by = step.get("order_by") if isinstance(step.get("order_by"), list) else []
    if not order_by or any(
        not isinstance(item, dict)
        or not str(item.get("column") or "").strip()
        or str(item.get("direction") or "").strip().lower() not in {"asc", "desc"}
        for item in order_by
    ):
        raise ValueError("select_extreme_row_per_group.order_by is invalid.")
    try:
        limit_per_group = int(step.get("limit_per_group"))
    except (TypeError, ValueError) as exc:
        raise ValueError("select_extreme_row_per_group.limit_per_group must be 1.") from exc
    if limit_per_group != 1:
        raise ValueError("select_extreme_row_per_group.limit_per_group must be 1.")
    tie_policy = str(step.get("tie_policy") or "").strip().lower()
    if tie_policy not in {"first", "include_all", "error"}:
        raise ValueError("select_extreme_row_per_group.tie_policy is invalid.")
    tie_breakers = step.get("tie_breakers") if isinstance(step.get("tie_breakers"), list) else []
    if any(
        not isinstance(item, dict)
        or not str(item.get("column") or "").strip()
        or str(item.get("direction") or "").strip().lower() not in {"asc", "desc"}
        for item in tie_breakers
    ):
        raise ValueError("select_extreme_row_per_group.tie_breakers is invalid.")
    if tie_policy == "first" and not tie_breakers:
        raise ValueError("tie_policy=first requires Catalog-backed tie_breakers.")
    if not _strings(step.get("projection")):
        raise ValueError("select_extreme_row_per_group.projection is required.")


# Function description: collect the columns used by the strict extreme-row
# primitive so they can be cross-checked against trusted Catalog metadata.
# 함수 설명: strict extreme-row 실행 컬럼을 신뢰 Catalog와 대조할 수 있도록 수집합니다.
def _extreme_step_columns(step: dict[str, Any]) -> list[str]:
    """Collect every source column consumed by an extreme-row primitive."""

    values: list[Any] = []
    values.extend(step.get("partition_by") if isinstance(step.get("partition_by"), list) else [])
    values.extend(step.get("projection") if isinstance(step.get("projection"), list) else [])
    for key in ("order_by", "tie_breakers"):
        raw = step.get(key)
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if isinstance(item, dict):
                values.append(item.get("column") or item.get("field"))
            else:
                values.append(item)
    return _strings(values)


# 함수 설명: 검증된 전체 종속 계획에서 현재 실행할 단계만 flat intent plan에 투영합니다.
def _project_stage(plan: dict[str, Any], stage: dict[str, Any]) -> None:
    for key in STAGE_PLAN_KEYS:
        if key in stage:
            plan[key] = deepcopy(stage[key])
        elif key in {"retrieval_jobs", "pandas_execution_plan", "output_contract"}:
            plan[key] = [] if key != "output_contract" else {}
    stage_metadata_refs = stage.get("metadata_refs")
    if isinstance(stage_metadata_refs, list):
        plan["metadata_refs"] = deepcopy(stage_metadata_refs)
    # The active stage's catalog choices are part of the validated Typed IR.
    # Downstream normalizers may reconcile weak flat plans, but must not swap a
    # stage source after the dependency compiler has selected it.
    plan["_continuation_stage_active"] = True


# 주요 함수: 공개 resume 계약의 버전·plan id/hash·stage 참조를 저장 계획과 비교합니다.
def _validate_resume_contract(
    contract: dict[str, Any],
    plan: dict[str, Any],
    request: dict[str, Any],
) -> None:
    if str(contract.get("version") or "") != CONTRACT_VERSION:
        raise ValueError("continuation contract 버전이 일치하지 않습니다.")
    if str(contract.get("plan_id") or "") != str(plan.get("plan_id") or ""):
        raise ValueError("continuation plan_id가 일치하지 않습니다.")
    if str(contract.get("plan_hash") or "") != str(plan.get("plan_hash") or ""):
        raise ValueError("continuation plan_hash가 일치하지 않습니다.")
    expected_ref = _continuation_ref(plan)
    supplied_ref = str(request.get("continuation_ref") or contract.get("continuation_ref") or "").strip()
    if supplied_ref != expected_ref:
        raise ValueError("continuation_ref가 계약에서 계산한 값과 일치하지 않습니다.")


# 함수 설명: runtime 변동 필드를 제외한 전체 단계 계약의 SHA-256 hash를 계산합니다.
def _plan_hash(value: dict[str, Any]) -> str:
    canonical = deepcopy(value)
    for key in ("plan_id", "plan_hash", "runtime", "active_stage_index"):
        canonical.pop(key, None)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# 함수 설명: plan_id와 plan_hash로 검증 가능한 공개 continuation 참조를 만듭니다.
def _continuation_ref(plan: dict[str, Any]) -> str:
    return f"continuation:{plan.get('plan_id')}:{plan.get('plan_hash')}"


# 함수 설명: 동일 저장 결과를 뜻하는 blank·previous_result·upstream_result 표현을 통일합니다.
def _canonical_reference_alias(value: Any) -> str:
    """Normalize equivalent catalog aliases for a stored upstream result."""

    text = str(value or "").strip()
    if not text or text in {"previous_result", "upstream_result"}:
        return "upstream_result"
    return text


# 함수 설명: 요청 stage index를 정수로 변환하고 계획 범위 밖 값을 차단합니다.
def _bounded_index(value: Any, stage_count: int) -> int:
    try:
        index = int(value)
    except Exception as exc:
        raise ValueError("continuation stage index가 정수가 아닙니다.") from exc
    if index < 0 or index >= stage_count:
        raise ValueError("continuation stage index가 범위를 벗어났습니다.")
    return index


# 함수 설명: Message·dict·Markdown fenced JSON에서 의도 object를 안전하게 추출합니다.
def _parse_json_response(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict):
        return deepcopy(data)
    text = str(getattr(value, "text", data) or "").strip()
    if not text:
        raise ValueError("의도 분석 응답이 비어 있습니다.")
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            repaired = _repair_common_json_syntax(candidate)
            if repaired == candidate:
                continue
            try:
                parsed = json.loads(repaired)
            except Exception:
                continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("의도 분석 응답에서 JSON object를 찾을 수 없습니다.")


# 함수 설명: 요청 payload에서 구조화된 continuation 입력만 복사합니다.
# 함수 설명: `_repair_common_json_syntax()`는 모델이 키 앞에 붙인 하이픈,
# 따옴표를 생략한 단순 object key, trailing comma만 제한적으로 보정합니다.
# 실행 코드나 값 표현은 평가하지 않고 JSON 구조만 복구해 재호출 없이
# 다음 계약 검증 단계로 넘깁니다.
def _repair_common_json_syntax(value: str) -> str:
    text = str(value or "")
    repaired = re.sub(
        r"([,{]\s*)-([A-Za-z_][A-Za-z0-9_]*)\s*:",
        r'\1"\2":',
        text,
    )
    repaired = re.sub(
        r"([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:",
        r'\1"\2":',
        repaired,
    )
    return re.sub(r",\s*([}\]])", r"\1", repaired)


# 함수 설명: continuation 요청 영역만 추출해 stage 재개 계약을 안전하게 처리합니다.
def _continuation_request(payload: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    continuation = request.get("continuation")
    return deepcopy(continuation) if isinstance(continuation, dict) else {}


# 함수 설명: Langflow Data 또는 일반 dict 입력을 독립 복사본으로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: list 입력에서 공백과 중복을 제거한 문자열 목록을 만듭니다.
def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# Langflow 컴포넌트 클래스: 검증된 현재 단계 의도 JSON을 기존 normalizer에 전달합니다.
class DependentRetrievalPlanCompiler(Component):
    display_name = "04B 종속 조회 계획 컴파일러"
    description = "최대 2단계 Typed IR을 검증하고 현재 단계만 기존 flat 실행 계약으로 투영합니다."
    inputs = [
        DataInput(name="payload", display_name="요청 페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="의도 분석 응답", required=True),
        DataInput(name="metadata_candidates", display_name="메타데이터 후보", required=False),
    ]
    outputs = [Output(name="compiled_response", display_name="컴파일된 의도 응답", method="build_response", types=["Message"])]

    # Langflow 출력 함수: 의도 응답을 검증·컴파일한 Message를 반환합니다.
    def build_response(self) -> Message:
        text, trace = compile_intent_response(
            getattr(self, "payload", None),
            getattr(self, "llm_response", ""),
            getattr(self, "metadata_candidates", None),
        )
        self.status = trace
        return Message(text=text)
