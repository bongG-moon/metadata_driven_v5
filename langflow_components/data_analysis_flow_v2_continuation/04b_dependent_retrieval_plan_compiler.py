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
    # The regular V2 normalizer consumes explicit output aliases.  For a
    # two-stage wrapper that may safely be flattened, repair local node
    # lineage before comparing the wrapper with the top-level plan.  Keep
    # ordinary plans and malformed one-stage wrappers byte-shape compatible
    # with their existing behavior; their own validators remain authoritative.
    if isinstance(dependent, dict) and _explicit_dependency_fallback_shape(dependent):
        plan = _normalize_flat_plan_node_aliases(plan)
        dependent = plan.get("dependent_retrieval_plan")
    dataset_scope_reconciliation: dict[str, Any] | None = None
    existence_predicate: dict[str, Any] | None = None
    flat_latest_selection: dict[str, Any] | None = None
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
        # ``select_extreme_row_per_group`` is a deterministic primitive, not a
        # policy the model should be allowed to weaken.  A direct history
        # lookup may legitimately have its required identifier already filled
        # (rather than needing a new two-stage continuation), so complete only
        # a unique Catalog recipe's omitted safety fields here.
        if not isinstance(dependent, dict):
            plan, flat_latest_selection = _apply_catalog_latest_selection_to_flat_plan(
                plan,
                metadata_candidates_value,
            )
    # A model may describe an already-declared two-stage recipe, but still
    # invent a fragile row-matching or latest-row step.  When the selected
    # recipe, the two dataset keys and the Catalog binding all agree, rebuild
    # the stages from the metadata-owned contract instead of trusting those
    # model-authored transform details.  This is intentionally generic: it
    # works for any structured current/history recipe, not a named dataset or
    # question pattern.
    recipe_dependent_canonicalized = False
    if isinstance(dependent, dict) and dependent and not continuation_request:
        canonical_dependent = _canonicalize_explicit_recipe_dependent_plan(
            plan,
            dependent,
            payload,
            metadata_candidates_value,
        )
        if canonical_dependent:
            dependent = canonical_dependent
            plan["dependent_retrieval_plan"] = dependent
            recipe_dependent_canonicalized = True
    # ``explicit_dependent`` means an untrusted model-authored wrapper.  A
    # recipe-canonicalized contract is metadata-owned and must not be
    # flattened or discarded by the weak-plan fallback path below.
    explicit_dependent = (
        isinstance(dependent, dict)
        and bool(dependent)
        and not recipe_dependent_canonicalized
    )
    # A weak model can still emit a two-stage wrapper after a selected recipe
    # explicitly says that the question does not require its secondary source.
    # Retain the first stage only when it independently satisfies the original
    # output contract.  This is a generic metadata activation guard: it does
    # not inspect dataset names or question-specific business words.
    if explicit_dependent and not continuation_request:
        nonactivated_recipe_fallback = _nonactivated_recipe_stage2_fallback(
            plan,
            dependent,
            payload,
            metadata_candidates_value,
        )
        if nonactivated_recipe_fallback is not None:
            direct_plan, evidence = nonactivated_recipe_fallback
            return _ignored_dependent_passthrough(
                envelope,
                direct_plan,
                reason="dependent_stage2_not_activated_by_metadata",
                evidence=evidence,
            )
    flat_fallback: dict[str, Any] | None = None
    fallback_shape_eligible = (
        explicit_dependent and _explicit_dependency_fallback_shape(dependent)
    )
    # Do not flatten a two-stage wrapper before checking whether its first
    # stage is already represented by trusted conversation rows.  This covers
    # a generic enrichment shape where the second source has no required
    # query parameter (for example a catalog join on the prior result's
    # declared grain), so an ``upstream_bindings`` parameter contract is not
    # the relevant proof.
    conversational_structural_handoff = (
        _project_conversational_structural_handoff(
            plan,
            dependent,
            payload,
            metadata_candidates_value,
        )
        if fallback_shape_eligible and not continuation_request
        else None
    )
    if conversational_structural_handoff is not None:
        projected_plan, evidence = conversational_structural_handoff
        return _conversational_handoff_passthrough(
            envelope,
            projected_plan,
            evidence=evidence,
        )
    conversational_row_handoff_candidate = (
        _has_conversational_row_handoff_candidate(
            plan,
            dependent,
            payload,
            metadata_candidates_value,
        )
        if fallback_shape_eligible and not continuation_request
        else False
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
    if (
        fallback_shape_eligible
        and not continuation_request
        and not conversational_row_handoff_candidate
    ):
        stages = [item for item in dependent.get("stages", []) if isinstance(item, dict)]
        stage2_missing_required = (
            _stage2_missing_required_params(stages[1], metadata_candidates_value)
            if len(stages) == MAX_STAGES
            else None
        )
        flat_dataset_keys = {
            str(job.get("dataset_key") or "").strip()
            for job in (flat_fallback or {}).get("retrieval_jobs", [])
            if isinstance(job, dict) and str(job.get("dataset_key") or "").strip()
        }
        staged_dataset_keys = {
            str(job.get("dataset_key") or "").strip()
            for stage in stages
            for job in stage.get("retrieval_jobs", [])
            if isinstance(job, dict) and str(job.get("dataset_key") or "").strip()
        }
        # A model may wrap an independently executable multi-source plan in
        # two nominal stages simply because the prose says "first find, then
        # show".  When the second catalog has no required parameter to bind
        # and the top-level plan is already complete, that wrapper is neither
        # a retrieval dependency nor an authorization to invent bindings.
        # Preserve the complete flat contract rather than rejecting it against
        # unrelated ``upstream_bindings`` metadata.
        if (
            flat_fallback is not None
            and stage2_missing_required == set()
            and staged_dataset_keys
            and staged_dataset_keys.issubset(flat_dataset_keys)
            and not _dependent_bindings_catalog_proven(dependent, metadata_candidates_value)
        ):
            return _ignored_dependent_passthrough(
                envelope,
                flat_fallback,
                reason="untrusted_nonparam_dependent_wrapper",
            )
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
        # A selected analysis recipe may declare a safe two-stage selection
        # contract.  This is deliberately driven by structured metadata (the
        # recipe reference, dataset keys, trusted Catalog binding and declared
        # activation terms), rather than by a dataset/question-specific branch.
        dependent = _lift_recipe_driven_dependent_plan(
            plan,
            payload,
            metadata_candidates_value,
        )
        if not dependent:
            # A compact flat-model contract may still list both current and
            # history datasets.  When they exactly match one selected recipe
            # and the history query has only blank Catalog-required parameters,
            # rebuild the stages from metadata instead of treating model-owned
            # latest-row details as executable policy.
            dependent = _lift_recipe_driven_flat_wrapper(
                plan,
                payload,
                metadata_candidates_value,
            )
        if not dependent:
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
        if flat_latest_selection and flat_latest_selection.get("status") == "applied":
            passthrough_trace["catalog_latest_selection"] = deepcopy(
                flat_latest_selection
            )
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), passthrough_trace

    shape_repaired = False
    try:
        dependent, shape_repaired = _repair_recoverable_dependent_shape(
            plan,
            dependent,
            metadata_candidates_value,
        )
        dependent, projection_completed = _complete_strict_extreme_projections(
            dependent,
            metadata_candidates_value,
        )
        shape_repaired = shape_repaired or projection_completed
        dependent = _normalize_and_validate_plan(dependent, metadata_candidates_value)
    except ValueError:
        if (
            explicit_dependent
            and not continuation_request
            and flat_fallback is not None
            and _has_two_stage_dependent_shape(dependent)
            and not _dependent_bindings_catalog_proven(dependent, metadata_candidates_value)
        ):
            # A weak model can append a malformed continuation object to an
            # otherwise complete, independent multi-source plan.  The flat
            # plan is accepted only after its own graph/schema/required-param
            # validation succeeded, while the untrusted dependency is
            # discarded.  This is intentionally not a question-specific
            # recovery: a real dependent retrieval remains fail-closed unless
            # its Catalog upstream binding is proven.
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

    # A conversational follow-up can already have the first stage's complete
    # entity result in its trusted session state.  In that case, scheduling the
    # same stage again is both wasteful and unsafe: a weak model can turn a
    # two-turn request into a second nested continuation.  Reuse is allowed
    # only when the follow-up contract, prior-result provenance, handoff
    # columns, final-stage selection and Catalog binding all agree.  This is a
    # Typed IR rule, not a rule for a particular dataset or business term.
    conversational_handoff = _validated_conversational_handoff(
        plan,
        dependent,
        payload,
        metadata_candidates_value,
    )
    if conversational_handoff:
        projected_plan = conversational_handoff.pop("_projected_plan", None)
        if not isinstance(projected_plan, dict):
            raise ValueError("검증된 후속 handoff의 최종 단계 계획이 없습니다.")
        return _conversational_handoff_passthrough(
            envelope,
            projected_plan,
            evidence=conversational_handoff,
        )

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
    if recipe_dependent_canonicalized:
        trace["continuation"]["dependent_recipe_canonicalized"] = True
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
def _conversational_handoff_passthrough(
    envelope: dict[str, Any],
    plan_value: dict[str, Any],
    *,
    evidence: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Keep a verified user follow-up as one ordinary previous-result query.

    The generic V2 normalizer owns the actual previous-result row matching and
    trusted parameter binding. This compiler only removes a duplicate staged
    wrapper after proving that the state already supplies the first handoff.
    """

    plan = deepcopy(plan_value)
    plan.pop("dependent_retrieval_plan", None)
    plan.pop("_continuation_stage_active", None)
    plan["request_scope"] = "followup_requery"
    plan["reference_mode"] = "previous_result_rows"
    plan["reuse_strategy"] = "previous_result"
    envelope["intent_plan"] = plan
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), {
        "status": "passthrough",
        "dependent": False,
        "active_stage_index": 0,
        "conversational_handoff_reused": True,
        "dependent_ignore_reason": "previous_result_satisfies_first_stage_handoff",
        "conversational_handoff": deepcopy(evidence),
        "retained_source_aliases": [
            str(job.get("source_alias") or job.get("dataset_key") or "").strip()
            for job in plan.get("retrieval_jobs", [])
            if isinstance(job, dict)
        ],
    }


def _has_conversational_row_handoff_candidate(
    plan: dict[str, Any],
    dependent: dict[str, Any],
    payload: dict[str, Any],
    metadata_candidates_value: Any,
) -> bool:
    """Recognize a safe prior-result enrichment before flat-plan fallback.

    The check is deliberately structural: it needs a follow-up hint, a
    same-session first-stage result, an explicit typed reference to the first
    stage from the final stage, and a Catalog-declared common grain.  It never
    infers a business term, dataset name, or join key from prose.
    """

    hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    if (
        hint.get("followup_candidate") is not True
        # The upstream follow-up classifier may label a request as a
        # ``followup_transform`` when it starts from prior rows.  A typed
        # dependent plan with a new final-stage retrieval is still a real
        # requery, so accept either neutral classifier spelling here and let
        # the structural/Catalog checks below decide whether it is safe.
        or str(hint.get("request_scope_hint") or "").strip()
        not in {"followup_requery", "followup_transform"}
        or str(hint.get("reuse_strategy_hint") or "").strip() != "previous_result"
        or str(plan.get("reference_mode") or "").strip()
        not in {"previous_result_rows", "previous_result"}
    ):
        return False
    stages = [item for item in dependent.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return False
    first_stage, final_stage = stages
    if not _catalog_proven_structural_handoff_columns(
        first_stage,
        final_stage,
        metadata_candidates_value,
    ):
        return False
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    return _positive_int(current_data.get("row_count")) > 0


def _project_conversational_structural_handoff(
    plan: dict[str, Any],
    dependent: dict[str, Any],
    payload: dict[str, Any],
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Reuse an already-complete first stage for a Catalog-backed row join.

    This covers a normal two-turn enrichment such as "those products -> their
    assigned equipment": the right source does not need an upstream *query
    parameter*, but its rows must be matched to the first result's declared
    grain.  The compiler accepts it only when the typed stage graph itself
    proves that relationship and both stages are backed by active Catalog
    schemas.  The projected flat plan is still normalized and validated by
    Flow 01 before retrieval.
    """

    if not _has_conversational_row_handoff_candidate(
        plan,
        dependent,
        payload,
        metadata_candidates_value,
    ):
        return None
    stages = [item for item in dependent.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return None
    first_stage, final_stage = stages
    first_stage_id = str(first_stage.get("stage_id") or "").strip()
    final_stage_id = str(final_stage.get("stage_id") or "").strip()
    if not first_stage_id or not final_stage_id or _strings(final_stage.get("depends_on")) != [first_stage_id]:
        return None
    final_jobs = [item for item in final_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    first_jobs = [item for item in first_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(final_jobs) != 1 or not first_jobs:
        return None
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    final_dataset = str(final_jobs[0].get("dataset_key") or "").strip()
    final_catalog = catalogs.get(final_dataset)
    if not isinstance(final_catalog, dict) or _catalog_required_params(final_catalog):
        return None
    top_jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    top_final_jobs = [
        deepcopy(job)
        for job in top_jobs
        if str(job.get("dataset_key") or "").strip() == final_dataset
    ]
    if not _same_stage_job_identity(top_final_jobs, final_jobs):
        return None
    first_datasets = _unique_job_datasets(first_jobs)
    if not first_datasets:
        return None
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    previous_datasets = set(_strings(current_data.get("source_dataset_keys")))
    previous_plan = state.get("last_intent_plan") if isinstance(state.get("last_intent_plan"), dict) else {}
    previous_datasets.update(
        str(job.get("dataset_key") or "").strip()
        for job in previous_plan.get("retrieval_jobs", [])
        if isinstance(job, dict) and str(job.get("dataset_key") or "").strip()
    )
    if not first_datasets.issubset(previous_datasets):
        return None
    row_match_columns = _catalog_proven_structural_handoff_columns(
        first_stage,
        final_stage,
        metadata_candidates_value,
    )
    if not row_match_columns:
        return None
    final_contract = final_stage.get("output_contract") if isinstance(final_stage.get("output_contract"), dict) else {}
    if not _contract_result_columns(final_contract):
        return None
    projected = _project_conversational_final_stage(
        plan,
        first_stage,
        final_stage,
        top_final_jobs,
        row_match_columns=row_match_columns,
    )
    if not projected:
        return None
    return projected, {
        "first_stage_id": first_stage_id,
        "final_stage_id": final_stage_id,
        "handoff_columns": row_match_columns,
        "handoff_proof": "typed_join_with_catalog_common_grain",
        "previous_result_row_count": _positive_int(current_data.get("row_count")),
        "previous_source_datasets": sorted(previous_datasets),
        "final_source_datasets": [final_dataset],
    }


def _catalog_proven_structural_handoff_columns(
    first_stage: dict[str, Any],
    final_stage: dict[str, Any],
    metadata_candidates_value: Any,
) -> list[str]:
    """Return a Catalog-backed row-match grain for a typed stage join.

    This is used only when the downstream source does not require an upstream
    query parameter.  The final stage must explicitly reference an output of
    the first stage; merely sharing similarly named columns is not enough.
    """

    handoff = first_stage.get("handoff") if isinstance(first_stage.get("handoff"), dict) else {}
    handoff_columns = _strings(handoff.get("columns"))
    if not handoff_columns or handoff.get("require_complete") is False:
        return []
    first_aliases = _stage_output_aliases(first_stage)
    if not first_aliases:
        return []
    references_first = False
    for step in final_stage.get("pandas_execution_plan", []):
        if not isinstance(step, dict):
            continue
        if any(
            str(step.get(key) or "").strip() in first_aliases
            for key in ("source_alias", "left_source_alias", "right_source_alias")
        ):
            references_first = True
            break
        for item in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
            if isinstance(item, dict) and str(item.get("ref") or "").strip() in first_aliases:
                references_first = True
                break
        if references_first:
            break
    if not references_first:
        return []
    final_jobs = [item for item in final_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(final_jobs) != 1:
        return []
    dataset_key = str(final_jobs[0].get("dataset_key") or "").strip()
    catalog = _catalog_by_dataset(metadata_candidates_value).get(dataset_key)
    if not isinstance(catalog, dict):
        return []
    available = set(_catalog_canonical_columns(catalog))
    return handoff_columns if set(handoff_columns).issubset(available) else []


def _validated_conversational_handoff(
    plan: dict[str, Any],
    dependent: dict[str, Any],
    payload: dict[str, Any],
    metadata_candidates_value: Any,
) -> dict[str, Any]:
    """Return compact proof that the first dependent stage is already present.

    Missing state, ambiguous source provenance, a non-follow-up request, or a
    binding not backed by the active Table Catalog leaves the normal two-stage
    continuation path unchanged.
    """

    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    hint = payload.get("followup_hint") if isinstance(payload.get("followup_hint"), dict) else {}
    if (
        hint.get("followup_candidate") is not True
        or str(hint.get("request_scope_hint") or "").strip()
        not in {"followup_requery", "followup_transform"}
        or str(hint.get("reuse_strategy_hint") or "").strip() != "previous_result"
    ):
        return {}
    if str(plan.get("request_scope") or "").strip() not in {
        "followup_requery",
        "followup_transform",
    }:
        return {}
    # Some model responses use the legacy reuse-strategy spelling
    # ``previous_result``.  With a followup requery and a new final-stage job,
    # it has the same Typed-IR meaning as ``previous_result_rows``.  Accept it
    # here so a proven conversational handoff is not turned into a nested
    # continuation solely because of that spelling variance.
    reference_mode = str(plan.get("reference_mode") or "").strip()
    if reference_mode not in {"previous_result_rows", "previous_result"}:
        return {}
    if not str(request.get("question") or "").strip():
        return {}
    stages = [item for item in dependent.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return {}
    first_stage, final_stage = stages
    first_stage_id = str(first_stage.get("stage_id") or "").strip()
    final_stage_id = str(final_stage.get("stage_id") or "").strip()
    if not first_stage_id or not final_stage_id:
        return {}
    if _strings(final_stage.get("depends_on")) != [first_stage_id]:
        return {}

    handoff = first_stage.get("handoff") if isinstance(first_stage.get("handoff"), dict) else {}
    handoff_columns = _strings(handoff.get("columns"))
    if not handoff_columns or handoff.get("require_complete") is False:
        return {}
    bindings = [
        item
        for item in final_stage.get("input_bindings", [])
        if isinstance(item, dict)
    ]
    binding_shape_valid = bool(bindings) and not any(
        str(binding.get("source_stage_id") or "").strip() != first_stage_id
        or str(binding.get("source_column") or "").strip() not in handoff_columns
        for binding in bindings
    )
    binding_proven = binding_shape_valid and _dependent_bindings_catalog_proven(
        dependent,
        metadata_candidates_value,
    )
    # A second source can enrich prior rows without receiving a retrieval
    # parameter.  In that shape the proof is the typed stage join plus a
    # Catalog-declared common grain, not a fictitious query binding.  Only a
    # single final source and an explicit first-stage output reference qualify.
    row_match_columns = (
        _strings([binding.get("source_column") for binding in bindings])
        if binding_proven
        else _catalog_proven_structural_handoff_columns(
            first_stage,
            final_stage,
            metadata_candidates_value,
        )
    )
    if not row_match_columns:
        return {}

    # The top-level plan may redundantly contain both source jobs because a
    # weak model narrated the first stage again.  It is safe to remove that
    # duplicate only when every top-level job belongs to the verified two-stage
    # contract and the final-stage dataset set is unique and complete.
    top_jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    final_jobs = [item for item in final_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    first_jobs = [item for item in first_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    final_datasets = _unique_job_datasets(final_jobs)
    first_datasets = _unique_job_datasets(first_jobs)
    top_datasets = _unique_job_datasets(top_jobs)
    if (
        not top_jobs
        or not final_jobs
        or not final_datasets
        or not first_datasets
        or not top_datasets
        or not final_datasets.issubset(top_datasets)
        or not top_datasets.issubset(first_datasets.union(final_datasets))
    ):
        return {}
    if not _dependent_final_contract_coherent(plan, dependent):
        return {}

    top_final_jobs = [
        deepcopy(job)
        for job in top_jobs
        if str(job.get("dataset_key") or "").strip() in final_datasets
    ]
    if not _same_stage_job_identity(top_final_jobs, final_jobs):
        return {}

    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    row_count = _positive_int(current_data.get("row_count"))
    rows = current_data.get("rows") if isinstance(current_data.get("rows"), list) else []
    preview_rows = current_data.get("preview_rows") if isinstance(current_data.get("preview_rows"), list) else []
    if row_count <= 0 or not (rows or preview_rows or str(current_data.get("data_ref") or "").strip()):
        return {}

    available_columns = set(_strings(current_data.get("columns")))
    available_columns.update(_strings(current_data.get("result_columns")))
    source_columns = current_data.get("source_columns_by_alias")
    if isinstance(source_columns, dict):
        for columns in source_columns.values():
            available_columns.update(_strings(columns))
    for row in [*rows[:1], *preview_rows[:1]]:
        if isinstance(row, dict):
            available_columns.update(str(key).strip() for key in row if str(key).strip())
    if not set(row_match_columns).issubset(available_columns):
        return {}

    # A same-session result must identify the source dataset which supplied the
    # handoff. This prevents a coincidentally named column from an unrelated
    # previous analysis from authorizing the stage skip.
    previous_datasets = set(_strings(current_data.get("source_dataset_keys")))
    previous_plan = state.get("last_intent_plan") if isinstance(state.get("last_intent_plan"), dict) else {}
    previous_datasets.update(
        str(job.get("dataset_key") or "").strip()
        for job in previous_plan.get("retrieval_jobs", [])
        if isinstance(job, dict) and str(job.get("dataset_key") or "").strip()
    )
    if not first_datasets or not previous_datasets or not first_datasets.issubset(previous_datasets):
        return {}

    projected_plan = _project_conversational_final_stage(
        plan,
        first_stage,
        final_stage,
        top_final_jobs,
        row_match_columns=row_match_columns,
    )
    if not projected_plan:
        return {}

    return {
        "first_stage_id": first_stage_id,
        "final_stage_id": final_stage_id,
        "handoff_columns": row_match_columns,
        "handoff_proof": (
            "catalog_upstream_binding"
            if binding_proven
            else "typed_join_with_catalog_common_grain"
        ),
        "previous_result_row_count": row_count,
        "previous_source_datasets": sorted(previous_datasets),
        "final_source_datasets": sorted(final_datasets),
        "_projected_plan": projected_plan,
    }


def _same_stage_job_identity(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    """Compare catalog datasets while permitting plan-local source aliases.

    Source aliases are execution-plan scoped: a model may call the same
    catalog source ``history_src`` while a metadata-owned stage calls it
    ``history``.  Dataset identity is the stable trust boundary, provided
    each side selects it only once; duplicate dataset jobs remain ambiguous
    and therefore do not qualify for conversational stage reuse.
    """

    return bool(left) and len(left) == len(right) and _unique_job_datasets(left) == _unique_job_datasets(right)


def _unique_job_datasets(items: list[dict[str, Any]]) -> set[str]:
    """Return a non-ambiguous dataset set, or an empty set on malformed jobs."""

    values = [str(item.get("dataset_key") or "").strip() for item in items]
    if not values or any(not value for value in values) or len(values) != len(set(values)):
        return set()
    return set(values)


def _project_conversational_final_stage(
    root_plan: dict[str, Any],
    first_stage: dict[str, Any],
    final_stage: dict[str, Any],
    top_final_jobs: list[dict[str, Any]],
    *,
    row_match_columns: list[str],
) -> dict[str, Any]:
    """Project the metadata-owned final stage using the caller's aliases.

    The model's alias is retained only as a local execution name.  Retrieval
    schema, latest-row selection and join semantics remain the validated
    stage's contract.  An ambiguous alias mapping fails closed.
    """

    stage_jobs = [item for item in final_stage.get("retrieval_jobs", []) if isinstance(item, dict)]
    stage_alias_by_dataset = {
        str(job.get("dataset_key") or "").strip(): str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in stage_jobs
        if str(job.get("dataset_key") or "").strip()
    }
    top_alias_by_dataset = {
        str(job.get("dataset_key") or "").strip(): str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in top_final_jobs
        if str(job.get("dataset_key") or "").strip()
    }
    if (
        len(stage_alias_by_dataset) != len(stage_jobs)
        or len(top_alias_by_dataset) != len(top_final_jobs)
        or set(stage_alias_by_dataset) != set(top_alias_by_dataset)
        or any(not value for value in [*stage_alias_by_dataset.values(), *top_alias_by_dataset.values()])
    ):
        return {}
    alias_map = {
        stage_alias_by_dataset[dataset]: top_alias_by_dataset[dataset]
        for dataset in stage_alias_by_dataset
        if stage_alias_by_dataset[dataset] != top_alias_by_dataset[dataset]
    }
    # ``upstream_result`` is the reserved alias for an explicit continuation
    # child run.  A regular conversation restores the same trusted rows under
    # ``previous_result``.  Convert only this reserved handoff reference; a
    # catalog source is never allowed to use either reserved name here.
    if {"upstream_result", "previous_result"}.intersection(stage_alias_by_dataset.values()):
        return {}
    alias_map["upstream_result"] = "previous_result"
    # A weak model can refer to the already-finished first-stage output by its
    # local node/output name. Only aliases that the verified first stage
    # actually declares may be rewritten to the restored conversation result.
    for alias in _stage_output_aliases(first_stage):
        if alias and alias not in stage_alias_by_dataset.values():
            alias_map[alias] = "previous_result"
    # This is a legacy reference-mode spelling rather than a Catalog source.
    # The conversational handoff proof above has already established a
    # same-session previous result, so canonicalizing it cannot widen scope.
    alias_map["previous_result_rows"] = "previous_result"

    projected = deepcopy(root_plan)
    projected["retrieval_jobs"] = deepcopy(top_final_jobs)
    steps = deepcopy(final_stage.get("pandas_execution_plan") or [])
    if not isinstance(steps, list):
        return {}
    # The first stage is already represented by trusted session rows in a
    # conversational follow-up.  Materialize the catalog-proven handoff as a
    # deterministic row-match before the final-stage transform.  The generic
    # V2 normalizer will resolve the match columns from the prior result's
    # output contract, so this remains portable across entity types instead of
    # relying on a LOT- or history-specific branch.
    if not any(
        isinstance(step, dict)
        and str(step.get("operation") or "").strip().lower()
        == "apply_row_match_groups"
        for step in steps
    ):
        row_match = _conversational_row_match_step(
            final_stage,
            stage_alias_by_dataset,
            row_match_columns=row_match_columns,
        )
        if not isinstance(row_match, dict):
            return {}
        steps.insert(0, row_match)
    projected["pandas_execution_plan"] = steps
    projected["output_contract"] = deepcopy(final_stage.get("output_contract") or {})
    projected.pop("dependent_retrieval_plan", None)
    projected.pop("_continuation_stage_active", None)
    if not _rewrite_stage_source_aliases(projected, alias_map):
        return {}
    return projected


# 함수 설명: 검증된 이전 stage가 만든 node/output 별칭만 수집해 대화형 handoff 재작성 범위를 안전하게 제한합니다.
def _stage_output_aliases(stage: dict[str, Any]) -> set[str]:
    """Return declared node/output names, never a source alias."""

    values: set[str] = set()
    steps = stage.get("pandas_execution_plan") if isinstance(stage.get("pandas_execution_plan"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for key in ("node_id", "output_alias", "result_alias"):
            value = str(step.get(key) or "").strip()
            if value:
                values.add(value)
    return values


def _conversational_row_match_step(
    final_stage: dict[str, Any],
    stage_alias_by_dataset: dict[str, str],
    *,
    row_match_columns: list[str],
) -> dict[str, Any]:
    """Build one safe current-result match from the typed stage bindings.

    The caller has already verified that every binding is backed by the active
    Catalog.  This helper still requires all bindings to target exactly one
    final-stage retrieval alias; multi-target contracts retain their normal
    continuation behavior rather than guessing which population to restrict.
    """

    bindings = [
        item
        for item in final_stage.get("input_bindings", [])
        if isinstance(item, dict)
    ]
    target_aliases = {
        str(item.get("target_source_alias") or "").strip()
        for item in bindings
        if str(item.get("target_source_alias") or "").strip()
    }
    if not target_aliases and len(stage_alias_by_dataset) == 1:
        # Structural join handoff: no retrieval parameter is required, so the
        # final stage's sole Catalog source is the row-match target.
        target_aliases = set(stage_alias_by_dataset.values())
    match_columns = _strings(row_match_columns)
    if len(target_aliases) != 1 or not match_columns:
        return {}
    target_alias = next(iter(target_aliases))
    if target_alias not in set(stage_alias_by_dataset.values()):
        return {}
    return {
        "operation": "apply_row_match_groups",
        "source_alias": target_alias,
        "reference_source_alias": "upstream_result",
        "match_columns": match_columns,
        "blank_policy": "normalize_blank",
    }


def _rewrite_stage_source_aliases(plan: dict[str, Any], alias_map: dict[str, str]) -> bool:
    """Rewrite verified cross-stage references into the restored result source.

    A final stage may call the previous stage's terminal node by
    ``node_output`` (for example ``top_products``) instead of the canonical
    ``previous_result`` alias.  Once the first stage has been proven to be the
    same-session handoff, that node no longer exists in this child execution.
    Rewrite only an alias supplied by the verified stage-output map and turn
    it into the reserved external provider.  Normal node-output lineage inside
    the current final stage remains untouched.
    """

    if not alias_map:
        return True
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    outputs = {
        str(step.get(key) or "").strip()
        for step in steps
        if isinstance(step, dict)
        for key in ("node_id", "output_alias")
        if str(step.get(key) or "").strip()
    }
    if outputs.intersection(alias_map):
        return False
    for step in steps:
        if not isinstance(step, dict):
            return False
        for key in (
            "source_alias",
            "left_source_alias",
            "right_source_alias",
            "reference_source_alias",
        ):
            value = str(step.get(key) or "").strip()
            if value in alias_map:
                step[key] = alias_map[value]
        inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        for item in inputs:
            if not isinstance(item, dict):
                return False
            ref = str(item.get("ref") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if ref not in alias_map:
                continue
            replacement = alias_map[ref]
            if kind == "external_source":
                item["ref"] = replacement
            elif kind == "node_output" and replacement == "previous_result":
                # This is a first-stage terminal alias established by the
                # conversational-handoff proof above, not a local final-stage
                # computation.  The earlier output collision guard ensures it
                # cannot accidentally replace a local node.
                item["kind"] = "external_source"
                item["ref"] = replacement
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    for binding in contract.get("metric_bindings", []) if isinstance(contract.get("metric_bindings"), list) else []:
        if not isinstance(binding, dict):
            return False
        alias = str(binding.get("source_alias") or "").strip()
        if alias in alias_map:
            binding["source_alias"] = alias_map[alias]
    return True


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


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
    if not _explicit_dependency_fallback_shape(dependent_plan):
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
    # Flattening is a recovery for an untrusted model-authored wrapper.  Unlike
    # a normal flat plan, it must prove that the *last* typed operation can
    # actually produce every final contract column.  A global union of columns
    # from earlier branches would otherwise accept a dangling stage and return
    # an arbitrary source frame at runtime.
    return _independently_complete_flat_plan(
        result,
        metadata_candidates_value,
        require_terminal_contract=True,
    )


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
        # A two-stage *presentation* wrapper often describes a stage-1
        # aggregate as though it were a physical column of the hand-off
        # frame, for example ``source_column=TOTAL`` and
        # ``output_column=TOTAL``.  That is not a new source-column
        # requirement: it is a reference to the already-derived stage-1
        # metric.  Resolve it only when the output metric and dataset point to
        # exactly one stage-1 binding.  This remains fail-closed for an
        # ambiguous or genuinely different physical source column.
        source_column = str(binding.get("source_column") or "").strip()
        references_stage_output = bool(output_column and source_column == output_column)
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
                not source_column
                or references_stage_output
                or str(item.get("source_column") or "").strip() == source_column
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


# 함수 설명: 약한 모델이 만든 불완전한 두 단계 계획을 안전한 flat fallback과 구분합니다.
def _has_two_stage_dependent_shape(value: Any) -> bool:
    """Return true only for an explicitly two-stage object.

    This deliberately does not approve the stages themselves.  Structural and
    Catalog validation remain mandatory for a real continuation; the helper is
    used only to decide whether a *separate already-complete flat plan* may
    safely survive an untrusted two-stage add-on.
    """

    if not isinstance(value, dict):
        return False
    stages = value.get("stages")
    return (
        isinstance(stages, list)
        and len(stages) == MAX_STAGES
        and all(isinstance(stage, dict) for stage in stages)
    )


# Function description: reject an unactivated recipe-owned second stage only
# when the verified current stage already covers the complete requested output.
# The recipe supplies the activation terms and dataset pair; no dataset name or
# business vocabulary is embedded here.
def _nonactivated_recipe_stage2_fallback(
    flat_plan: dict[str, Any],
    dependent_plan: dict[str, Any],
    payload_value: Any,
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Project a sufficient current stage when recipe activation is absent."""

    if not _explicit_dependency_fallback_shape(dependent_plan):
        return None
    stages = [deepcopy(item) for item in dependent_plan.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return None
    first_jobs = [item for item in stages[0].get("retrieval_jobs", []) if isinstance(item, dict)]
    second_jobs = [item for item in stages[1].get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(first_jobs) != 1 or len(second_jobs) != 1:
        return None
    first_dataset = str(first_jobs[0].get("dataset_key") or "").strip()
    second_dataset = str(second_jobs[0].get("dataset_key") or "").strip()
    if not first_dataset or not second_dataset:
        return None

    metadata_refs = flat_plan.get("metadata_refs") if isinstance(flat_plan.get("metadata_refs"), list) else []
    recipes = _domain_items_by_ref(metadata_candidates_value)
    matched: list[tuple[tuple[str, str], dict[str, Any], list[str]]] = []
    for reference in metadata_refs:
        if not isinstance(reference, dict):
            continue
        section = _normalized_section(reference.get("section") or reference.get("type"))
        key = str(reference.get("key") or "").strip()
        recipe = recipes.get((section, key)) if section == "analysis_recipes" and key else None
        if not isinstance(recipe, dict):
            continue
        current_selection = recipe.get("current_selection")
        history_selection = recipe.get("history_selection")
        activation = recipe.get("dependent_selection")
        if not (
            isinstance(current_selection, dict)
            and isinstance(history_selection, dict)
            and isinstance(activation, dict)
        ):
            continue
        if str(activation.get("current_stage") or "").strip() != "current_selection":
            continue
        if str(activation.get("next_stage") or "").strip() != "history_selection":
            continue
        terms = _strings(activation.get("when_question_includes_any"))
        if not terms:
            continue
        if (
            str(current_selection.get("dataset_key") or "").strip() != first_dataset
            or str(history_selection.get("dataset_key") or "").strip() != second_dataset
        ):
            continue
        matched.append(((section, key), current_selection, terms))
    if len(matched) != 1:
        return None

    payload = _payload(payload_value)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or payload.get("question") or "").strip().casefold()
    recipe_ref, current_selection, activation_terms = matched[0]
    matched_terms = [term for term in activation_terms if term.casefold() in question]
    if matched_terms:
        return None

    # Do not downgrade a request when the current Catalog cannot provide every
    # requested output, or the stage itself only prepared a handoff.  In those
    # cases the ordinary dependent-plan validation remains fail-closed.
    requested_columns = _contract_result_columns(
        flat_plan.get("output_contract") if isinstance(flat_plan.get("output_contract"), dict) else {}
    )
    stage_columns = _contract_result_columns(
        stages[0].get("output_contract") if isinstance(stages[0].get("output_contract"), dict) else {}
    )
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    current_catalog = catalogs.get(first_dataset)
    current_columns = set(_catalog_canonical_columns(current_catalog)) if isinstance(current_catalog, dict) else set()
    if (
        not requested_columns
        or not set(requested_columns).issubset(current_columns)
        or not set(requested_columns).issubset(set(stage_columns))
    ):
        return None

    stage1 = deepcopy(stages[0])
    _normalize_stage_node_aliases(stage1)
    projected = deepcopy(flat_plan)
    _project_stage(projected, stage1)
    projected.pop("dependent_retrieval_plan", None)
    complete = _independently_complete_flat_plan(projected, metadata_candidates_value)
    if complete is None:
        return None
    return complete, {
        "policy": "recipe_activation_and_current_stage_sufficiency",
        "metadata_ref": {"section": recipe_ref[0], "key": recipe_ref[1]},
        "activation_terms": activation_terms,
        "matched_terms": [],
        "retained_dataset": first_dataset,
        "discarded_dataset": second_dataset,
        "requested_columns": requested_columns,
        "current_selection": {
            "dataset_key": str(current_selection.get("dataset_key") or "").strip(),
        },
    }


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
    *,
    require_terminal_contract: bool = False,
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
    if require_terminal_contract and not _terminal_flat_output_contract_reachable(
        steps,
        kept_jobs,
        catalogs,
        output_columns,
    ):
        return None

    result = deepcopy(working_plan)
    result["retrieval_jobs"] = kept_jobs
    result.pop("dependent_retrieval_plan", None)
    return result


# 함수 설명: flatten recovery가 마지막 typed step에서 최종 결과 계약의 모든 컬럼을 실제로 만들 수 있는지 정적으로 증명합니다.
def _terminal_flat_output_contract_reachable(
    steps: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    catalogs: dict[str, dict[str, Any]],
    output_columns: list[str],
) -> bool:
    """Prove a flattened recovery DAG reaches its final output contract.

    This intentionally understands only the narrow deterministic Typed-IR
    operations.  It is used for an untrusted two-stage-to-flat recovery, not
    for ordinary plans; unknown transformations therefore remain on their
    established execution path instead of being guessed here.
    """

    frames: dict[str, set[str]] = {}
    for job in jobs:
        dataset_key = str(job.get("dataset_key") or "").strip()
        alias = str(job.get("source_alias") or dataset_key).strip()
        catalog = catalogs.get(dataset_key)
        columns = set(_catalog_canonical_columns(catalog)) if isinstance(catalog, dict) else set()
        if not alias or not columns or alias in frames:
            return False
        frames[alias] = columns
        frames.setdefault(dataset_key, columns)

    terminal_columns: set[str] = set()
    for index, step in enumerate(steps):
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        node_id = str(step.get("node_id") or "").strip()
        output_alias = str(step.get("output_alias") or node_id).strip()
        inputs = [item for item in step.get("inputs", []) if isinstance(item, dict)]
        if not operation or not node_id or not output_alias or not inputs:
            return False
        input_columns: list[set[str]] = []
        for item in inputs:
            reference = str(item.get("ref") or "").strip()
            # References must be real external frames or outputs from an
            # earlier step.  A later alias is not an executable provider.
            if not reference or reference not in frames:
                return False
            input_columns.append(set(frames[reference]))

        if operation in {"apply_filters", "apply_row_match_groups"}:
            produced = input_columns[0]
        elif operation == "select_columns":
            projection = set(_strings(step.get("projection") or step.get("columns")))
            if not projection or not projection.issubset(input_columns[0]):
                return False
            produced = projection
        elif operation in {"groupby_and_aggregate", "aggregate_single_source"}:
            group_by = set(_strings(step.get("group_by") or step.get("group_by_columns")))
            aggregations = [item for item in step.get("aggregations", []) if isinstance(item, dict)]
            aggregate_sources = {
                str(item.get("column") or item.get("source_column") or "").strip()
                for item in aggregations
            }
            aggregate_outputs = {
                str(item.get("output_column") or "").strip()
                for item in aggregations
            }
            if (
                not aggregations
                or "" in aggregate_sources
                or "" in aggregate_outputs
                or not group_by.issubset(input_columns[0])
                or not aggregate_sources.issubset(input_columns[0])
            ):
                return False
            produced = group_by | aggregate_outputs
        elif operation == "sort_and_top_n":
            sort_by = str(step.get("sort_by") or "").strip()
            if not sort_by or sort_by not in input_columns[0]:
                return False
            produced = input_columns[0]
        elif operation in {"join", "left_join", "merge", "outer_join"}:
            if len(input_columns) != 2:
                return False
            # The normalizer may materialize a shared grain into explicit join
            # keys later.  For recovery safety we only prove the declared
            # source lineage and output availability here.
            produced = input_columns[0] | input_columns[1]
        elif operation in {"distinct_values", "project_single_source"}:
            projection = set(
                _strings(step.get("group_by") or step.get("projection") or step.get("columns"))
            )
            if not projection or not projection.issubset(input_columns[0]):
                return False
            produced = projection
        else:
            return False

        # A collision with a source alias can make a later reference resolve
        # to the wrong frame.  Normalized stage plans should never do this.
        if node_id in frames or output_alias in frames:
            return False
        frames[node_id] = produced
        frames[output_alias] = produced
        terminal_columns = produced

    return bool(terminal_columns) and set(output_columns).issubset(terminal_columns)


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
# Function description: turn a selected, structured analysis recipe into a
# two-stage plan only when its own activation and both Catalog contracts prove
# that the handoff is safe.  The compiler contains no dataset/entity-specific
# rules: the recipe supplies the activation, selections and binding.
def _lift_recipe_driven_dependent_plan(
    plan_value: dict[str, Any],
    payload_value: Any,
    metadata_candidates_value: Any,
) -> dict[str, Any]:
    """Lift a current-selection recipe to its declared history selection."""

    plan = deepcopy(plan_value)
    payload = _payload(payload_value)
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    question = str(request.get("question") or payload.get("question") or "").strip().casefold()
    if not question:
        return {}
    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(jobs) != 1:
        return {}
    current_job = jobs[0]
    current_dataset = str(current_job.get("dataset_key") or "").strip()
    current_alias = str(current_job.get("source_alias") or current_dataset).strip()
    if not current_dataset or not current_alias:
        return {}

    domain_items = _domain_items_by_ref(metadata_candidates_value)
    refs = plan.get("metadata_refs") if isinstance(plan.get("metadata_refs"), list) else []
    recipes: list[tuple[str, dict[str, Any]]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        section = _normalized_section(ref.get("section") or ref.get("type"))
        key = str(ref.get("key") or "").strip()
        recipe = domain_items.get((section, key)) if section == "analysis_recipes" and key else None
        if isinstance(recipe, dict):
            recipes.append((key, recipe))
    if len(recipes) != 1:
        return {}

    recipe_key, recipe = recipes[0]
    current_selection = recipe.get("current_selection")
    history_selection = recipe.get("history_selection")
    activation = recipe.get("dependent_selection")
    if not (
        isinstance(current_selection, dict)
        and isinstance(history_selection, dict)
        and isinstance(activation, dict)
    ):
        return {}
    if str(activation.get("current_stage") or "").strip() != "current_selection":
        return {}
    if str(activation.get("next_stage") or "").strip() != "history_selection":
        return {}
    activation_terms = _strings(activation.get("when_question_includes_any"))
    if not activation_terms or not any(term.casefold() in question for term in activation_terms):
        return {}

    selected_current_dataset = str(current_selection.get("dataset_key") or "").strip()
    history_dataset = str(history_selection.get("dataset_key") or "").strip()
    if current_dataset != selected_current_dataset or not history_dataset:
        return {}
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    current_catalog = catalogs.get(current_dataset)
    history_catalog = catalogs.get(history_dataset)
    if not isinstance(current_catalog, dict) or not isinstance(history_catalog, dict):
        return {}

    current_filters = current_selection.get("filter") if isinstance(current_selection.get("filter"), dict) else {}
    job_filters = deepcopy(current_job.get("filters")) if isinstance(current_job.get("filters"), dict) else {}
    for column, expected in current_filters.items():
        canonical_column = str(column or "").strip()
        if not canonical_column:
            return {}
        existing_key = next(
            (key for key in job_filters if str(key).strip().casefold() == canonical_column.casefold()),
            None,
        )
        if existing_key is None:
            job_filters[canonical_column] = deepcopy(expected)
        elif _canonical_json(job_filters.get(existing_key)) != _canonical_json(expected):
            return {}
    if job_filters:
        current_job["filters"] = job_filters

    declared_binding = history_selection.get("upstream_binding")
    if not isinstance(declared_binding, dict):
        return {}
    source_column = str(declared_binding.get("source_column") or "").strip()
    target_param = str(declared_binding.get("target_param") or "").strip()
    operator = str(declared_binding.get("operator") or "in").strip().lower()
    if not source_column or not target_param or operator not in {"eq", "in"}:
        return {}
    current_allowed = set(_catalog_canonical_columns(current_catalog))
    history_allowed = set(_catalog_canonical_columns(history_catalog))
    if source_column not in current_allowed or target_param not in set(_catalog_required_params(history_catalog)):
        return {}
    trusted_matches = [
        item
        for item in _catalog_upstream_bindings(history_catalog)
        if str(item.get("source_column") or "").strip() == source_column
        and str(item.get("target_param") or "").strip() == target_param
        and str(item.get("operator") or "in").strip().lower() == operator
        and _canonical_reference_alias(item.get("source_alias")) == "upstream_result"
    ]
    if len(trusted_matches) != 1:
        return {}
    trusted_binding = trusted_matches[0]

    root_contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    current_columns = _strings(root_contract.get("result_columns")) or _strings(root_contract.get("required_columns"))
    current_columns = [column for column in current_columns if column in current_allowed]
    if source_column not in current_columns:
        current_columns.append(source_column)
    if not current_columns:
        return {}
    history_columns = _strings(history_selection.get("result_columns"))
    latest = history_selection.get("latest_per_group")
    if not isinstance(latest, dict):
        return {}
    partition_by = _strings(latest.get("partition_by"))
    order_by = [deepcopy(item) for item in latest.get("order_by", []) if isinstance(item, dict)]
    order_columns = _strings([item.get("column") for item in order_by])
    if not partition_by or not order_by:
        return {}
    projection = _strings([*history_columns, target_param, *partition_by, *order_columns])
    if not projection or any(column not in history_allowed for column in projection):
        return {}
    history_alias = str(history_selection.get("source_alias") or history_dataset).strip()
    if not history_alias or history_alias == current_alias:
        return {}

    stage1_id = f"stage_1_{_safe_id(current_alias)}"
    stage2_id = f"stage_2_{_safe_id(history_alias)}"
    stage1_output = f"{_safe_id(current_alias)}_handoff"
    latest_output = f"latest_{_safe_id(history_alias)}"
    final_output = f"{_safe_id(history_alias)}_with_current"
    history_params = history_selection.get("required_params") if isinstance(history_selection.get("required_params"), dict) else {}
    history_job = {
        "dataset_key": history_dataset,
        "source_alias": history_alias,
        "required_params": {
            name: deepcopy(history_params.get(name, ""))
            for name in _catalog_required_params(history_catalog)
        },
    }
    history_source_type = str(history_catalog.get("source_type") or "").strip()
    if history_source_type:
        history_job["source_type"] = history_source_type
    final_columns = _strings([*current_columns, *history_columns])
    if not final_columns:
        return {}
    stage1_contract = {
        "result_mode": "entity_list",
        "required_columns": list(current_columns),
        "grain_columns": [source_column],
        "metric_columns": [],
        "result_columns": list(current_columns),
        "strict_result_columns": True,
        "null_group_policy": "preserve_as_blank",
        "metric_null_policy": "display_zero",
    }
    final_contract = {
        "result_mode": "entity_list",
        "required_columns": final_columns,
        "grain_columns": [source_column],
        "metric_columns": [],
        "result_columns": final_columns,
        "strict_result_columns": True,
        "null_group_policy": "preserve_as_blank",
        "metric_null_policy": "display_zero",
    }
    return {
        "version": CONTRACT_VERSION,
        "max_stages": MAX_STAGES,
        "final_stage_id": stage2_id,
        "activation": {
            "reason": "analysis_recipe_dependent_selection",
            "metadata_ref": {"section": "analysis_recipes", "key": recipe_key},
            "matched_terms": [term for term in activation_terms if term.casefold() in question],
        },
        "stages": [
            {
                "stage_id": stage1_id,
                "depends_on": [],
                "retrieval_jobs": [current_job],
                "pandas_execution_plan": [
                    {
                        "node_id": f"select_{_safe_id(current_alias)}_handoff",
                        "operation": "select_columns",
                        "inputs": [{"kind": "external_source", "ref": current_alias}],
                        "source_alias": current_alias,
                        "output_alias": stage1_output,
                        "projection": list(current_columns),
                    }
                ],
                "output_contract": stage1_contract,
                "handoff": {"columns": [source_column], "require_complete": True},
            },
            {
                "stage_id": stage2_id,
                "depends_on": [stage1_id],
                "retrieval_jobs": [history_job],
                "pandas_execution_plan": [
                    {
                        "node_id": f"latest_{_safe_id(history_alias)}",
                        "operation": "select_extreme_row_per_group",
                        "inputs": [{"kind": "external_source", "ref": history_alias}],
                        "source_alias": history_alias,
                        "output_alias": latest_output,
                        "partition_by": partition_by,
                        "order_by": order_by,
                        "tie_breakers": [deepcopy(item) for item in latest.get("tie_breakers", []) if isinstance(item, dict)],
                        "limit_per_group": int(latest.get("limit_per_group") or 1),
                        "tie_policy": str(latest.get("tie_policy") or "error").strip().lower(),
                        "projection": projection,
                        "strict": True,
                    },
                    {
                        "node_id": f"join_{_safe_id(history_alias)}_current",
                        "operation": "join",
                        "inputs": [
                            {"kind": "node_output", "ref": "upstream_result"},
                            {"kind": "node_output", "ref": latest_output},
                        ],
                        "left_source_alias": "upstream_result",
                        "right_source_alias": latest_output,
                        "left_on": [source_column],
                        "right_on": [target_param],
                        "join_type": "left",
                        "population_policy": "preserve_left",
                        "output_alias": final_output,
                    },
                ],
                "output_contract": final_contract,
                "input_bindings": [
                    {
                        "source_stage_id": stage1_id,
                        "source_column": source_column,
                        "target_source_alias": history_alias,
                        "target_param": target_param,
                        "operator": operator,
                        "source_alias": str(trusted_binding.get("source_alias") or "previous_result").strip(),
                        "entity_type": str(trusted_binding.get("entity_type") or "").strip(),
                    }
                ],
            },
        ],
    }


# 함수 설명: 모델이 current/history 두 dataset을 flat 계획에 함께 적어도 선택 recipe가 완전히 증명되면 metadata-owned 2단계 계약으로 다시 만듭니다.
# 함수 설명: direct history lookup의 latest-row safety field가 빠졌을 때 유일한 Catalog recipe에서만 Typed primitive 계약을 보완합니다.
def _apply_catalog_latest_selection_to_flat_plan(
    plan_value: dict[str, Any],
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Complete omitted latest-row fields from one structured history recipe.

    This is intentionally independent of any dataset or Korean/English query
    phrase.  It applies only when one candidate recipe owns the retrieved
    history dataset, its selection fields are internally complete, and the
    model did not explicitly contradict them.  A direct identifier lookup is
    still a single query; it does not become a continuation merely because
    the same Catalog also supports upstream binding.
    """

    plan = deepcopy(plan_value)
    jobs = [item for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    steps = [item for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    if not jobs or not steps:
        return plan, {"status": "not_needed", "applied": [], "conflicts": []}

    aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip(): str(
            job.get("dataset_key") or ""
        ).strip()
        for job in jobs
        if str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        and str(job.get("dataset_key") or "").strip()
    }
    output_to_step: dict[str, dict[str, Any]] = {}
    for step in steps:
        for field_name in ("node_id", "output_alias", "result_alias"):
            key = str(step.get(field_name) or "").strip()
            if key:
                output_to_step[key] = step

    def source_dataset(step: dict[str, Any], visited: set[str] | None = None) -> str:
        source_alias = str(step.get("source_alias") or "").strip()
        if source_alias in aliases:
            return aliases[source_alias]
        seen = visited or set()
        for item in step.get("inputs", []) if isinstance(step.get("inputs"), list) else []:
            if not isinstance(item, dict):
                continue
            reference = str(item.get("ref") or "").strip()
            if reference in aliases:
                return aliases[reference]
            predecessor = output_to_step.get(reference)
            if predecessor is not None and reference not in seen:
                resolved = source_dataset(predecessor, {*seen, reference})
                if resolved:
                    return resolved
        return ""

    recipe_candidates: dict[str, list[tuple[tuple[str, str], dict[str, Any], dict[str, Any]]]] = {}
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    for ref, recipe in _domain_items_by_ref(metadata_candidates_value).items():
        if ref[0] != "analysis_recipes":
            continue
        history = recipe.get("history_selection") if isinstance(recipe.get("history_selection"), dict) else {}
        latest = history.get("latest_per_group") if isinstance(history.get("latest_per_group"), dict) else {}
        dataset_key = str(history.get("dataset_key") or "").strip()
        partition_by = _strings(latest.get("partition_by"))
        order_by = [deepcopy(item) for item in latest.get("order_by", []) if isinstance(item, dict)]
        result_columns = _strings(history.get("result_columns"))
        if not dataset_key or not partition_by or not order_by or not result_columns:
            continue
        try:
            limit_per_group = int(latest.get("limit_per_group") or 1)
        except (TypeError, ValueError):
            continue
        tie_policy = str(latest.get("tie_policy") or "error").strip().lower()
        if limit_per_group != 1 or tie_policy not in {"first", "include_all", "error"}:
            continue
        if tie_policy == "first" and not [item for item in latest.get("tie_breakers", []) if isinstance(item, dict)]:
            continue
        catalog = catalogs.get(dataset_key)
        allowed_columns = set(_catalog_canonical_columns(catalog)) if isinstance(catalog, dict) else set()
        source_columns = _strings(
            [
                *partition_by,
                *result_columns,
                *[item.get("column") for item in order_by],
                *[
                    item.get("column")
                    for item in latest.get("tie_breakers", [])
                    if isinstance(item, dict)
                ],
            ]
        )
        if not allowed_columns or any(column not in allowed_columns for column in source_columns):
            continue
        recipe_candidates.setdefault(dataset_key, []).append((ref, history, latest))

    applied: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    accepted_operations = {
        "select_extreme_row_per_group",
        # ``latest_earliest`` is a legacy generic Typed-IR shorthand.  It
        # becomes the strict per-group primitive only when the unique recipe
        # below proves the complete semantics; otherwise it keeps its normal
        # established execution path.
        "latest_earliest",
    }
    for step in steps:
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation not in accepted_operations:
            continue
        dataset_key = source_dataset(step)
        matches = recipe_candidates.get(dataset_key, [])
        if len(matches) != 1:
            continue
        ref, history, latest = matches[0]
        partition_by = _strings(latest.get("partition_by"))
        order_by = [deepcopy(item) for item in latest.get("order_by", []) if isinstance(item, dict)]
        tie_breakers = [
            deepcopy(item)
            for item in latest.get("tie_breakers", [])
            if isinstance(item, dict)
        ]
        expected_limit = int(latest.get("limit_per_group") or 1)
        expected_policy = str(latest.get("tie_policy") or "error").strip().lower()
        existing_partition = _strings(step.get("partition_by"))
        existing_order = [item for item in step.get("order_by", []) if isinstance(item, dict)]
        existing_breakers = [item for item in step.get("tie_breakers", []) if isinstance(item, dict)]
        existing_limit = step.get("limit_per_group")
        existing_policy = str(step.get("tie_policy") or "").strip().lower()
        try:
            has_conflicting_limit = (
                existing_limit not in (None, "")
                and int(existing_limit) != expected_limit
            )
        except (TypeError, ValueError):
            has_conflicting_limit = True
        conflicts_found = (
            (existing_partition and existing_partition != partition_by)
            or (existing_order and _canonical_json(existing_order) != _canonical_json(order_by))
            or (existing_breakers and _canonical_json(existing_breakers) != _canonical_json(tie_breakers))
            or has_conflicting_limit
            or (existing_policy and existing_policy != expected_policy)
        )
        if conflicts_found:
            conflicts.append(
                {
                    "node_id": str(step.get("node_id") or "").strip(),
                    "dataset_key": dataset_key,
                    "metadata_ref": {"section": ref[0], "key": ref[1]},
                }
            )
            continue
        history_params = (
            history.get("required_params")
            if isinstance(history.get("required_params"), dict)
            else {}
        )
        required_param_names = _strings(list(history_params))
        projection = _strings(
            [
                *(step.get("projection", []) if isinstance(step.get("projection"), list) else []),
                *_strings(history.get("result_columns")),
                *required_param_names,
                *partition_by,
                *[item.get("column") for item in order_by],
                *[item.get("column") for item in tie_breakers],
            ]
        )
        if not projection:
            continue
        step["partition_by"] = partition_by
        step["order_by"] = order_by
        step["tie_breakers"] = tie_breakers
        step["limit_per_group"] = expected_limit
        step["tie_policy"] = expected_policy
        step["projection"] = projection
        step["strict"] = True
        step["operation"] = "select_extreme_row_per_group"
        applied.append(
            {
                "node_id": str(step.get("node_id") or "").strip(),
                "dataset_key": dataset_key,
                "metadata_ref": {"section": ref[0], "key": ref[1]},
            }
        )
    plan["pandas_execution_plan"] = steps
    return plan, {
        "status": "applied" if applied else "conflict" if conflicts else "not_needed",
        "applied": applied,
        "conflicts": conflicts,
    }


# 함수 설명: 모델이 current/history 두 dataset을 flat 계획에 함께 적어도 선택 recipe가 완전히 증명되면 metadata-owned 2단계 계약으로 다시 만듭니다.
def _lift_recipe_driven_flat_wrapper(
    plan_value: dict[str, Any],
    payload_value: Any,
    metadata_candidates_value: Any,
) -> dict[str, Any]:
    """Recover only a recipe-owned two-source flat wrapper.

    The intent model is deliberately not asked to author a continuation IR.
    Some models still include both datasets in a flat plan.  This helper keeps
    that output safe only when one selected structured recipe proves the exact
    current/history pair and the history source genuinely needs a value from
    the current result.  The underlying recipe compiler then owns all latest
    selection, strictness and join details.
    """

    plan = deepcopy(plan_value)
    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    if len(jobs) != MAX_STAGES:
        return {}
    domain_items = _domain_items_by_ref(metadata_candidates_value)
    refs = plan.get("metadata_refs") if isinstance(plan.get("metadata_refs"), list) else []
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        section = _normalized_section(ref.get("section") or ref.get("type"))
        key = str(ref.get("key") or "").strip()
        recipe = domain_items.get((section, key)) if section == "analysis_recipes" and key else None
        if not isinstance(recipe, dict):
            continue
        current_selection = recipe.get("current_selection")
        history_selection = recipe.get("history_selection")
        if not isinstance(current_selection, dict) or not isinstance(history_selection, dict):
            continue
        current_dataset = str(current_selection.get("dataset_key") or "").strip()
        history_dataset = str(history_selection.get("dataset_key") or "").strip()
        current_jobs = [
            job
            for job in jobs
            if str(job.get("dataset_key") or "").strip() == current_dataset
        ]
        history_jobs = [
            job
            for job in jobs
            if str(job.get("dataset_key") or "").strip() == history_dataset
        ]
        if len(current_jobs) == 1 and len(history_jobs) == 1:
            matched.append((current_jobs[0], history_jobs[0]))
    if len(matched) != 1:
        return {}

    current_job, history_job = matched[0]
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    history_dataset = str(history_job.get("dataset_key") or "").strip()
    history_catalog = catalogs.get(history_dataset)
    if not isinstance(history_catalog, dict):
        return {}
    history_params = (
        history_job.get("required_params")
        if isinstance(history_job.get("required_params"), dict)
        else {}
    )
    required_params = _catalog_required_params(history_catalog)
    # A direct parameter value is an ordinary single-query request, not a
    # permission to replace it with prior-result data.
    if not required_params or any(not _blank(history_params.get(name)) for name in required_params):
        return {}

    seed = deepcopy(plan)
    seed["retrieval_jobs"] = [current_job]
    return _lift_recipe_driven_dependent_plan(
        seed,
        payload_value,
        metadata_candidates_value,
    )


# Function description: an explicit two-stage model plan may name the same
# current/history recipe but contain unsafe transform details.  Reconstruct it
# only when the model's stage dataset choices match the selected structured
# recipe, then delegate all actual contract construction to the existing
# recipe-driven compiler.
def _canonicalize_explicit_recipe_dependent_plan(
    plan_value: dict[str, Any],
    dependent_value: dict[str, Any],
    payload_value: Any,
    metadata_candidates_value: Any,
) -> dict[str, Any]:
    """Return a metadata-owned replacement for a matching explicit wrapper.

    A non-match is deliberately a no-op.  The caller then follows the normal
    validation/fail-closed path for an explicit dependent plan, so this helper
    never broadens what a weak model is allowed to execute.
    """

    if not _has_two_stage_dependent_shape(dependent_value):
        return {}
    stages = [item for item in dependent_value.get("stages", []) if isinstance(item, dict)]
    if len(stages) != MAX_STAGES:
        return {}
    stage_jobs = [
        [deepcopy(item) for item in stage.get("retrieval_jobs", []) if isinstance(item, dict)]
        for stage in stages
    ]
    if any(len(jobs) != 1 for jobs in stage_jobs):
        return {}
    current_job = stage_jobs[0][0]
    history_job = stage_jobs[1][0]
    current_dataset = str(current_job.get("dataset_key") or "").strip()
    history_dataset = str(history_job.get("dataset_key") or "").strip()
    if not current_dataset or not history_dataset:
        return {}

    # Verify that exactly one selected structured recipe owns these two
    # datasets before using the existing compiler.  This prevents a generic
    # recipe reference from authorizing an unrelated model-selected source.
    domain_items = _domain_items_by_ref(metadata_candidates_value)
    refs = plan_value.get("metadata_refs") if isinstance(plan_value.get("metadata_refs"), list) else []
    matches: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        section = _normalized_section(ref.get("section") or ref.get("type"))
        key = str(ref.get("key") or "").strip()
        recipe = domain_items.get((section, key)) if section == "analysis_recipes" and key else None
        if not isinstance(recipe, dict):
            continue
        current_selection = recipe.get("current_selection")
        history_selection = recipe.get("history_selection")
        activation = recipe.get("dependent_selection")
        if not (
            isinstance(current_selection, dict)
            and isinstance(history_selection, dict)
            and isinstance(activation, dict)
        ):
            continue
        if (
            str(current_selection.get("dataset_key") or "").strip() == current_dataset
            and str(history_selection.get("dataset_key") or "").strip() == history_dataset
        ):
            matches.append(recipe)
    if len(matches) != 1:
        return {}

    # _lift_recipe_driven_dependent_plan intentionally accepts one current
    # source.  Seed it with the explicit stage-1 job so filters provided by the
    # model remain subject to its declared recipe filter reconciliation, while
    # history retrieval, latest-row selection and left-population preservation
    # are rebuilt from metadata rather than reused from model code.
    seed = deepcopy(plan_value)
    seed["retrieval_jobs"] = [current_job]
    stage1_steps = stages[0].get("pandas_execution_plan")
    seed["pandas_execution_plan"] = (
        [deepcopy(item) for item in stage1_steps if isinstance(item, dict)]
        if isinstance(stage1_steps, list)
        else []
    )
    stage1_contract = stages[0].get("output_contract")
    if isinstance(stage1_contract, dict) and not isinstance(seed.get("output_contract"), dict):
        seed["output_contract"] = deepcopy(stage1_contract)
    seed.pop("dependent_retrieval_plan", None)
    return _lift_recipe_driven_dependent_plan(seed, payload_value, metadata_candidates_value)


# Function description: complete an otherwise proven strict latest-row step
# from the stage Catalog-backed output columns.  The helper never invents a
# source column, and leaves non-strict or ambiguous steps fail-closed.
def _complete_strict_extreme_projections(
    dependent_value: dict[str, Any],
    metadata_candidates_value: Any,
) -> tuple[dict[str, Any], bool]:
    dependent = deepcopy(dependent_value)
    catalogs = _catalog_by_dataset(metadata_candidates_value)
    changed = False
    stages = dependent.get("stages") if isinstance(dependent.get("stages"), list) else []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        jobs = [item for item in stage.get("retrieval_jobs", []) if isinstance(item, dict)]
        aliases = {
            str(job.get("source_alias") or job.get("dataset_key") or "").strip(): job
            for job in jobs
            if str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        }
        contract = stage.get("output_contract") if isinstance(stage.get("output_contract"), dict) else {}
        contract_columns = _strings(contract.get("result_columns")) or _strings(contract.get("required_columns"))
        steps = stage.get("pandas_execution_plan") if isinstance(stage.get("pandas_execution_plan"), list) else []
        for step in steps:
            if not isinstance(step, dict):
                continue
            if str(step.get("operation") or "").strip().lower() != "select_extreme_row_per_group":
                continue
            if step.get("strict") is not True or _strings(step.get("projection")):
                continue
            source_alias = str(step.get("source_alias") or "").strip()
            job = aliases.get(source_alias)
            dataset_key = str(job.get("dataset_key") or "").strip() if isinstance(job, dict) else ""
            catalog = catalogs.get(dataset_key)
            if not isinstance(catalog, dict):
                continue
            allowed = set(_catalog_canonical_columns(catalog))
            partition_by = _strings(step.get("partition_by"))
            order_columns = _strings(
                [item.get("column") for item in step.get("order_by", []) if isinstance(item, dict)]
            )
            required = _strings([*partition_by, *order_columns])
            if not required or any(column not in allowed for column in required):
                continue
            projection = _strings([
                *[column for column in contract_columns if column in allowed],
                *required,
            ])
            if projection:
                step["projection"] = projection
                changed = True
    return dependent, changed


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

    def _prior_operation_output(reference: str, current_index: int) -> str:
        """Resolve an unambiguous legacy operation-name reference within one stage.

        Some model responses use ``groupby_and_aggregate`` or ``join`` as a
        node-output reference instead of copying the preceding node id.  This
        is safe to repair only when exactly one earlier stage step has that
        operation name.  A repeated operation remains unresolved and is later
        rejected rather than guessed.
        """

        text = str(reference or "").strip()
        if not text:
            return ""
        normalized = re.sub(r"[^0-9a-z]+", "_", text.casefold()).strip("_")
        if not normalized:
            return ""
        candidates: list[str] = []
        for prior_index, prior in enumerate(steps[:current_index]):
            operation = str(prior.get("operation") or prior.get("step") or "").strip()
            operation_key = re.sub(r"[^0-9a-z]+", "_", operation.casefold()).strip("_")
            if operation_key != normalized:
                continue
            node_id = str(prior.get("node_id") or prior.get("output_alias") or "").strip()
            if node_id and node_id not in candidates:
                candidates.append(node_id)
        return candidates[0] if len(candidates) == 1 else ""

    for index, step in enumerate(steps):
        raw_inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        if not raw_inputs:
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
            raw_inputs = [{"ref": alias} for alias in aliases]

        inputs: list[dict[str, Any]] = []
        for raw_input in raw_inputs:
            if not isinstance(raw_input, dict):
                continue
            input_value = deepcopy(raw_input)
            alias = str(input_value.get("ref") or "").strip()
            if not alias:
                continue
            if alias in external_aliases:
                input_value["kind"] = "external_source"
                input_value["ref"] = alias
            else:
                owner_index = owners.get(alias)
                if owner_index is not None and owner_index < index:
                    input_value["kind"] = "node_output"
                    input_value["ref"] = alias_nodes.get(alias) or alias
                else:
                    operation_output = _prior_operation_output(alias, index)
                    if operation_output:
                        input_value["kind"] = "node_output"
                        input_value["ref"] = operation_output
                    else:
                        # An unresolved logical alias can be a stage-1 handoff
                        # reference; cross-stage normalization below either
                        # rewrites it or validation rejects it.  It must never
                        # be guessed as a retrieval source.
                        input_value.setdefault("kind", "node_output")
                        input_value["ref"] = alias
            inputs.append(input_value)
        if inputs:
            step["inputs"] = inputs
    stage["pandas_execution_plan"] = steps


# 함수 설명: continuation stage2가 참조한 stage1 식별자를 예약 외부 source upstream_result로 정규화합니다.
def _normalize_flat_plan_node_aliases(plan: dict[str, Any]) -> dict[str, Any]:
    """Apply the same safe alias completion to an ordinary flat plan."""

    if not isinstance(plan, dict):
        return {}
    steps = plan.get("pandas_execution_plan")
    if not isinstance(steps, list) or not any(isinstance(item, dict) for item in steps):
        return plan
    normalized = deepcopy(plan)
    stage = {
        "stage_id": "flat",
        "retrieval_jobs": normalized.get("retrieval_jobs", []),
        "pandas_execution_plan": normalized.get("pandas_execution_plan", []),
    }
    _normalize_stage_node_aliases(stage)
    normalized["pandas_execution_plan"] = stage["pandas_execution_plan"]
    return normalized


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
        r"([,{]\s*)-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:",
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
