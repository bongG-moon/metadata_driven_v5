# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 05A Continuation 참조 별칭 정규화기
# 역할: 저장 결과를 뜻하는 이전 별칭을 명시적 continuation의 upstream_result 별칭으로 통일합니다.
# 주요 입력: 신뢰 카탈로그로 hydrate되고 상위 결과가 복원된 payload
# 주요 출력: source_config.upstream_bindings의 참조 별칭만 정규화한 payload
# 처리 흐름: 명시적 continuation 요청에서만 previous_result와 빈 기본 별칭을 upstream_result로 바꿉니다.
# 유지보수 포인트: 데이터셋·컬럼·파라미터를 만들지 않고 같은 저장 결과를 가리키는 예약 별칭만 정규화합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, Output
from lfx.schema.data import Data

UPSTREAM_RESULT_ALIAS = "upstream_result"
EQUIVALENT_STORED_RESULT_ALIASES = {"", "previous_result", "upstream_result"}


# 주요 함수: 명시적 continuation 경로의 trusted upstream binding 별칭을 runtime alias와 맞춥니다.
def normalize_continuation_binding_aliases(payload_value: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    restored_contract_count = _restore_explicit_detail_projection_contract(plan)
    restored_scalar_grain_count = _restore_scalar_result_grain_contract(plan)
    restored_function_case_grain_columns = _restore_function_case_entity_grain_contract(plan)
    explicit_continuation = _is_explicit_continuation(payload)
    jobs = [deepcopy(item) for item in plan.get("retrieval_jobs", []) if isinstance(item, dict)]
    normalized_count = 0
    removed_blank_param_count = 0
    for job in jobs:
        if not explicit_continuation:
            continue
        if job.get("trusted_catalog") is not True:
            continue
        source_config = job.get("source_config") if isinstance(job.get("source_config"), dict) else {}
        raw_bindings = source_config.get("upstream_bindings")
        if not isinstance(raw_bindings, list):
            continue
        bindings: list[Any] = []
        upstream_target_params: set[str] = set()
        for raw_binding in raw_bindings:
            if not isinstance(raw_binding, dict):
                bindings.append(raw_binding)
                continue
            binding = deepcopy(raw_binding)
            alias = str(binding.get("source_alias") or "").strip()
            if alias in EQUIVALENT_STORED_RESULT_ALIASES and alias != UPSTREAM_RESULT_ALIAS:
                binding["source_alias"] = UPSTREAM_RESULT_ALIAS
                normalized_count += 1
            if str(binding.get("source_alias") or "").strip() == UPSTREAM_RESULT_ALIAS:
                target_param = str(binding.get("target_param") or "").strip()
                if target_param:
                    upstream_target_params.add(target_param.casefold())
            bindings.append(binding)
        source_config["upstream_bindings"] = bindings
        job["source_config"] = source_config
        required_params = (
            deepcopy(job.get("required_params"))
            if isinstance(job.get("required_params"), dict)
            else {}
        )
        for key in list(required_params):
            if str(key).strip().casefold() not in upstream_target_params:
                continue
            if not _blank(required_params.get(key)):
                continue
            required_params.pop(key, None)
            removed_blank_param_count += 1
        job["required_params"] = required_params
    plan["retrieval_jobs"] = jobs
    payload["intent_plan"] = plan
    payload.setdefault("trace", {}).setdefault("inspection", {})["continuation_binding_aliases"] = {
        "stage": "05A_continuation_binding_alias_normalizer",
        "status": "ok",
        "normalized_count": normalized_count,
        "removed_blank_required_param_count": removed_blank_param_count,
        "restored_explicit_projection_contract_count": restored_contract_count,
        "restored_scalar_grain_contract_count": restored_scalar_grain_count,
        "restored_function_case_grain_columns": restored_function_case_grain_columns,
        "runtime_source_alias": UPSTREAM_RESULT_ALIAS,
    }
    return payload


# 함수 설명: detail/entity_list의 명시 result projection을 Catalog 기본 상세 fallback보다 우선합니다.
def _restore_explicit_detail_projection_contract(plan: dict[str, Any]) -> int:
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result_mode = str(contract.get("result_mode") or contract.get("mode") or "").strip().lower()
    result_columns = _strings(contract.get("result_columns"))
    if result_mode not in {"detail", "entity_list"} or not result_columns:
        return 0
    steps = [item for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    terminal_projection: list[str] = []
    if steps:
        terminal = steps[-1]
        operation = str(terminal.get("operation") or terminal.get("step") or "").strip().lower()
        if operation == "select_columns":
            terminal_projection = _strings(
                terminal.get("projection") or terminal.get("columns")
            )
    explicit_contract = contract.get("strict_result_columns") is True
    projection_matches = bool(terminal_projection) and {
        item.casefold() for item in terminal_projection
    } == {item.casefold() for item in result_columns}
    if not explicit_contract and not projection_matches:
        return 0
    required_columns = _strings(contract.get("required_columns"))
    if [item.casefold() for item in required_columns] == [item.casefold() for item in result_columns]:
        return 0
    contract["required_columns"] = list(result_columns)
    allowed = {item.casefold() for item in result_columns}
    for key in ("grain_columns", "metric_columns"):
        values = _strings(contract.get(key))
        if values:
            contract[key] = [item for item in values if item.casefold() in allowed]
    labels = contract.get("column_labels") if isinstance(contract.get("column_labels"), dict) else {}
    if labels:
        contract["column_labels"] = {
            str(key): value
            for key, value in labels.items()
            if str(key).strip().casefold() in allowed
        }
    plan["output_contract"] = contract
    return 1


# 함수 설명: scalar 결과가 source entity grain으로 다시 확장되지 않도록 명시적인 빈 결과 grain을 복원합니다.
def _restore_scalar_result_grain_contract(plan: dict[str, Any]) -> int:
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result_mode = str(contract.get("result_mode") or contract.get("mode") or "").strip().lower()
    if result_mode != "scalar":
        return 0
    changed = contract.get("grain_columns") != []
    contract["grain_columns"] = []
    plan["output_contract"] = contract
    resolved = (
        plan.get("resolved_output_grain_plan")
        if isinstance(plan.get("resolved_output_grain_plan"), dict)
        else None
    )
    if isinstance(resolved, dict):
        for key in ("entity_grain_columns", "breakdown_columns", "grain_columns"):
            if resolved.get(key) != []:
                changed = True
            resolved[key] = []
        plan["resolved_output_grain_plan"] = resolved
    return 1 if changed else 0


# 함수 설명: 신뢰 Function Case가 entity를 고른 뒤 집계할 때 metadata entity grain이 유실되지 않도록 계약과 groupby를 맞춥니다.
def _restore_function_case_entity_grain_contract(plan: dict[str, Any]) -> list[str]:
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    if str(contract.get("result_mode") or "").strip().lower() != "aggregate":
        return []
    steps = [deepcopy(item) for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    function_steps = [
        item
        for item in steps
        if str(item.get("operation") or item.get("step") or "").strip().lower()
        == "apply_pandas_function_case"
        and str(item.get("function_name") or "").strip()
    ]
    if not function_steps:
        return []
    resolved = plan.get("resolved_grain_plan") if isinstance(plan.get("resolved_grain_plan"), dict) else {}
    trusted_grain = _strings(
        resolved.get("grain_columns")
        or resolved.get("entity_grain_columns")
        or resolved.get("canonical_columns")
    )
    if not trusted_grain:
        return []
    supported = _trusted_plan_columns(plan, resolved)
    if supported:
        trusted_grain = [column for column in trusted_grain if column.casefold() in supported]
    if not trusted_grain:
        return []

    function_refs: set[str] = set()
    for step in function_steps:
        function_refs.update(
            text.casefold()
            for text in _strings(
                [step.get("node_id"), step.get("output_alias"), step.get("source_alias")]
            )
        )
    restored: list[str] = []
    for step in steps:
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        if operation != "groupby_and_aggregate":
            continue
        inputs = step.get("inputs") if isinstance(step.get("inputs"), list) else []
        input_refs = {
            str(item.get("ref") or "").strip().casefold()
            for item in inputs
            if isinstance(item, dict) and str(item.get("ref") or "").strip()
        }
        source_alias = str(step.get("source_alias") or "").strip().casefold()
        if input_refs or source_alias:
            if not (input_refs | ({source_alias} if source_alias else set())).intersection(function_refs):
                continue
        group_by = _strings(step.get("group_by"))
        additions = [column for column in trusted_grain if column.casefold() not in {item.casefold() for item in group_by}]
        if not additions:
            continue
        step["group_by"] = [*group_by, *additions]
        restored.extend(column for column in additions if column not in restored)
    if not restored:
        return []
    plan["pandas_execution_plan"] = steps
    grain = _strings(contract.get("grain_columns"))
    metrics = _strings(contract.get("metric_columns"))
    next_grain = [*grain, *[column for column in restored if column.casefold() not in {item.casefold() for item in grain}]]
    contract["grain_columns"] = next_grain
    for key in ("required_columns", "result_columns"):
        values = _strings(contract.get(key))
        metric_keys = {item.casefold() for item in metrics}
        dimensions = [item for item in values if item.casefold() not in metric_keys]
        ordered_metrics = [item for item in values if item.casefold() in metric_keys]
        for column in restored:
            if column.casefold() not in {item.casefold() for item in dimensions}:
                dimensions.append(column)
        contract[key] = [*dimensions, *ordered_metrics]
    plan["output_contract"] = contract
    resolved_output = (
        deepcopy(plan.get("resolved_output_grain_plan"))
        if isinstance(plan.get("resolved_output_grain_plan"), dict)
        else {}
    )
    resolved_output["grain_columns"] = list(next_grain)
    if isinstance(resolved_output.get("entity_grain_columns"), list):
        resolved_output["entity_grain_columns"] = list(next_grain)
    plan["resolved_output_grain_plan"] = resolved_output
    return restored


# 함수 설명: hydrate된 Catalog 매핑과 resolved grain mapping에서 실제 source가 지원하는 canonical 컬럼만 모읍니다.
def _trusted_plan_columns(plan: dict[str, Any], resolved_grain: dict[str, Any]) -> set[str]:
    columns: set[str] = set()
    for item in resolved_grain.get("column_mappings", []) if isinstance(resolved_grain.get("column_mappings"), list) else []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("canonical_key") or item.get("canonical_column") or "").strip()
        if canonical:
            columns.add(canonical.casefold())
    for job in plan.get("retrieval_jobs", []) if isinstance(plan.get("retrieval_jobs"), list) else []:
        if not isinstance(job, dict):
            continue
        for key in ("filter_mappings", "standard_column_aliases"):
            mapping = job.get(key) if isinstance(job.get(key), dict) else {}
            columns.update(str(column).strip().casefold() for column in mapping if str(column).strip())
    return columns


# 함수 설명: continuation 계약과 명시적 상위 결과 참조가 모두 있는 요청인지 확인합니다.
def _is_explicit_continuation(payload: dict[str, Any]) -> bool:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    continuation = request.get("continuation") if isinstance(request.get("continuation"), dict) else {}
    orchestration = payload.get("orchestration") if isinstance(payload.get("orchestration"), dict) else {}
    return bool(
        continuation
        and str(orchestration.get("upstream_result_ref") or "").strip()
        and str(orchestration.get("status") or "").strip().lower() == "ok"
    )


# 함수 설명: upstream binding이 채울 수 있는 비어 있는 placeholder 값만 판별합니다.
def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


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


# 함수 설명: Langflow Data 또는 일반 dict 입력을 독립 복사본으로 변환합니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


# Langflow 컴포넌트 클래스: continuation 전용 alias 정규화 단계를 Data 입출력 포트로 제공합니다.
class ContinuationBindingAliasNormalizer(Component):
    display_name = "05A Continuation 참조 별칭 정규화기"
    description = "trusted previous_result 예약 별칭을 명시적 continuation의 upstream_result 별칭으로 통일합니다."
    inputs = [DataInput(name="payload", display_name="페이로드", required=True)]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # Langflow 출력 함수: 예약 별칭 정규화 결과를 다음 기존 바인더에 전달합니다.
    def build_payload(self) -> Data:
        return Data(data=normalize_continuation_binding_aliases(getattr(self, "payload", None)))
