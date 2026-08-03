# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 17 pandas 실행/1회 복구기
# 역할: pandas 코드를 안전 실행하고 실제 오류일 때만 repair LLM을 최대 1회 호출해 수정 코드를 재실행합니다.
# 주요 입력: 페이로드 (payload) · 필수, pandas 코드 LLM 응답 (llm_response) · 필수, 선택 Function Case Helper
#        (function_case_helper_code), pandas 복구 프롬프트 (repair_prompt_template) · 필수, 복구 언어 모델 (model) · 필수, 복구 API 키
#        (api_key), 최대 Repair 횟수 (max_repair_attempts)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 생성 코드를 AST로 검사하고 제한된 pandas/numpy 환경에서 실행하며, 실패하면 이전 코드와 오류를 포함해 LLM 복구를 최대 한 번 수행합니다.
# 유지보수 포인트: 파일·네트워크 I/O와 임의 import는 차단하고 pandas/numpy alias만 허용합니다. 복구 호출은 실행 오류당 최대 한 번입니다.
# =============================================================================

from __future__ import annotations

import ast
import hashlib
import json
import re
import traceback
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, DropdownInput, MessageTextInput, ModelInput, Output, SecretStrInput
from lfx.schema.data import Data

RUNTIME_BUFFER_KEYS = {
    "runtime_sources",
    "_runtime_rows_by_alias",
    "_full_result_rows",
    "_runtime_result_rows",
}

FORBIDDEN_NAMES = {"open", "exec", "eval", "__import__", "compile", "input"}
RESULT_PREVIEW_LIMIT = 50
TRACE_PREVIEW_LIMIT = 5
DEFAULT_MAX_REPAIR_ATTEMPTS = 1
REPAIR_CODE_PREVIEW_LIMIT = 1000
LLM_RESPONSE_PREVIEW_LIMIT = 500
SAFE_IMPORT_POLICY = "exact pandas/numpy aliases are removed and trusted namespaces are injected"
BLANK_MATCH_TEXTS = {"", "null", "none", "nan", "nat", "<na>", "empty"}
SAFE_NUMPY_ATTRIBUTES = (
    "abs",
    "array",
    "asarray",
    "ceil",
    "clip",
    "float32",
    "float64",
    "floor",
    "inf",
    "int32",
    "int64",
    "isfinite",
    "isinf",
    "isnan",
    "maximum",
    "minimum",
    "nan",
    "nan_to_num",
    "ndarray",
    "round",
    "select",
    "where",
)
FORBIDDEN_IO_ATTRIBUTES = {
    "ctypeslib",
    "dump",
    "dumps",
    "fromfile",
    "genfromtxt",
    "load",
    "load_library",
    "loadtxt",
    "memmap",
    "read_clipboard",
    "read_csv",
    "read_excel",
    "read_feather",
    "read_fwf",
    "read_hdf",
    "read_html",
    "read_json",
    "read_orc",
    "read_parquet",
    "read_pickle",
    "read_sas",
    "read_spss",
    "read_sql",
    "read_sql_query",
    "read_sql_table",
    "read_stata",
    "read_table",
    "read_xml",
    "save",
    "savez",
    "savez_compressed",
    "savetxt",
    "to_clipboard",
    "to_csv",
    "to_excel",
    "to_feather",
    "to_hdf",
    "to_orc",
    "to_parquet",
    "to_pickle",
    "to_sql",
    "to_stata",
    "tofile",
    "urlopen",
}
SUPPORTED_FILTER_OPERATORS = {
    "eq",
    "in",
    "ne",
    "not_in",
    "gt",
    "ge",
    "lt",
    "le",
    "contains",
    "like",
    "starts_with",
    "ends_with",
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
    "or",
    "any",
}
VALUELESS_FILTER_OPERATORS = {
    "is_null",
    "is_empty",
    "null_or_empty",
    "not_null",
    "not_empty",
    "not_blank",
}
SUPPORTED_COMPOUND_FILTER_OPERATORS = {
    "eq",
    "in",
    "contains",
    "like",
    "starts_with",
    "is_null",
    "is_empty",
    "null_or_empty",
}


# 내부 연동 도우미 클래스: 상세/목록 결과가 카탈로그의 필수 표시 컬럼을 누락했을 때 1회 repair 대상으로 전달합니다.
class OutputContractError(ValueError):
    pass


# 주요 함수: 안전성 검사를 통과한 pandas 코드를 제한된 namespace에서 한 번 실행합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def execute_pandas_code(
    payload_value: Any,
    llm_response: Any,
    function_case_helper_code: Any = "",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    if _execution_blocked(payload):
        return _blocked_execution_payload(payload)
    llm_code, response_parse = _parse_pandas_llm_response(llm_response)
    normalized_llm_code, safe_imports = _normalize_safe_imports(llm_code)
    deterministic_execution = _deterministic_execution_contract(payload)
    execution_mode = str(
        deterministic_execution.get("operation") or "llm_generated_code"
    )
    code = normalized_llm_code
    next_payload = payload
    if not normalized_llm_code.strip() and not deterministic_execution:
        return _analysis_error(
            next_payload,
            "missing_code",
            "pandas code LLM 응답에 실행 가능한 code가 없습니다.",
            normalized_llm_code,
            "",
            llm_code,
            "",
            [],
            safe_imports,
            response_parse=response_parse,
        )
    filter_plan = _pandas_filter_plan(next_payload)
    row_match_plan = _pandas_row_match_plan(next_payload)
    filter_preamble = _pandas_filter_preamble(filter_plan)
    row_match_preamble = _pandas_row_match_preamble(row_match_plan)
    unsupported_filters = _unsupported_filter_operators(filter_plan)
    if unsupported_filters:
        return _analysis_error(
            next_payload,
            "unsupported_filter_operator",
            "지원하지 않는 pandas 필터 연산자가 있습니다: " + ", ".join(unsupported_filters),
            normalized_llm_code,
            "",
            llm_code,
            filter_preamble,
            filter_plan,
            safe_imports,
            row_match_plan,
            row_match_preamble,
            response_parse,
        )
    deterministic_transform_error = ""
    if deterministic_execution:
        deterministic_transform_preamble, deterministic_transform_error = (
            _deterministic_function_case_preamble(
                deterministic_execution,
                function_case_helper_code,
            )
        )
        code = "\n\n".join(
            item
            for item in (
                filter_preamble.strip(),
                deterministic_transform_preamble.strip(),
                _deterministic_contract_display_code(deterministic_execution).strip(),
            )
            if item
        )
    else:
        code = _with_pandas_execution_preambles(
            code,
            row_match_preamble,
            filter_preamble,
        )
    helper_trace = _runtime_helper_trace(code)
    if deterministic_transform_error:
        return _analysis_error(
            next_payload,
            "output_contract_violation",
            deterministic_transform_error,
            code,
            "",
            llm_code,
            filter_preamble,
            filter_plan,
            safe_imports,
            row_match_plan,
            row_match_preamble,
            response_parse,
        )
    guard_error = _guard_code(code)
    if guard_error:
        return _analysis_error(
            next_payload,
            "unsafe_code",
            guard_error,
            code,
            "",
            llm_code,
            filter_preamble,
            filter_plan,
            safe_imports,
            row_match_plan,
            row_match_preamble,
            response_parse,
        )
    metric_semantics_error = (
        ""
        if deterministic_execution
        else _metric_semantics_contract_error(next_payload, code)
    )
    if metric_semantics_error:
        return _analysis_error(
            next_payload,
            "output_contract_violation",
            metric_semantics_error,
            code,
            "",
            llm_code,
            filter_preamble,
            filter_plan,
            safe_imports,
            row_match_plan,
            row_match_preamble,
            response_parse,
        )
    try:
        import pandas as pd  # type: ignore

        source_columns_by_alias = _source_columns_by_alias(next_payload)
        sources = {}
        for alias, rows in next_payload.get("runtime_sources", {}).items():
            frame = pd.DataFrame(rows)
            if len(frame.columns) == 0:
                configured_columns = source_columns_by_alias.get(str(alias), [])
                if configured_columns:
                    frame = pd.DataFrame(columns=configured_columns)
            sources[alias] = frame
        safe_builtins = {
            "Exception": Exception,
            "all": all,
            "any": any,
            "bool": bool,
            "dict": dict,
            "enumerate": enumerate,
            "float": float,
            "hasattr": hasattr,
            "int": int,
            "isinstance": isinstance,
            "len": len,
            "list": list,
            "max": max,
            "min": min,
            "object": object,
            "range": range,
            "round": round,
            "set": set,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "zip": zip,
        }
        step_outputs: list[dict[str, Any]] = []
        function_case_results: list[dict[str, Any]] = []

        # 함수 설명: `record_step()`는 pandas 실행 중 단계별 DataFrame 크기와 설명을 trace에 기록합니다.
        def record_step(key: Any, value: Any, description: Any = "", role: Any = "") -> Any:
            step_outputs.append(_recorded_output(key, value, description, role))
            return value

        # 함수 설명: `record_function_case_result()`는 선택 helper 실행 결과의 함수명·입력·행 수를 분석 근거로 기록합니다.
        def record_function_case_result(function_name: Any, input_text: Any, result_value: Any, description: Any = "") -> Any:
            function_case_results.append(_recorded_function_case(function_name, input_text, result_value, description))
            return result_value

        exec_ns: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "pd": pd,
            "sources": sources,
            "result": None,
            "result_df": None,
            "record_step": record_step,
            "record_function_case_result": record_function_case_result,
        }
        if safe_imports.get("numpy_requested") is True:
            exec_ns["np"] = _safe_numpy_namespace()
        if deterministic_execution:
            if code.strip():
                exec(compile(code, "<pandas_filter_preamble>", "exec"), exec_ns, exec_ns)
                sources = (
                    exec_ns.get("sources")
                    if isinstance(exec_ns.get("sources"), dict)
                    else sources
                )
            deterministic_transform_execution_value = exec_ns.get(
                "_deterministic_function_case_execution",
                [],
            )
            deterministic_transform_execution = (
                deepcopy(deterministic_transform_execution_value)
                if isinstance(deterministic_transform_execution_value, list)
                else []
            )
            helper_trace["helper_sources"] = deepcopy(
                deterministic_transform_execution
            )
            deterministic_result = _execute_deterministic_contract(
                deterministic_execution,
                sources,
                pd,
            )
            if (
                isinstance(deterministic_result, tuple)
                and len(deterministic_result) == 2
            ):
                result, semantic_execution_certificate = deterministic_result
            else:
                result = deterministic_result
                semantic_execution_certificate = {}
            result = _apply_deterministic_result_ordering(
                result,
                next_payload,
            )
            row_match_execution = []
        else:
            exec(compile(code, "<pandas_code>", "exec"), exec_ns, exec_ns)
            row_match_execution_value = exec_ns.get("_row_match_execution", [])
            row_match_execution = (
                deepcopy(row_match_execution_value)
                if isinstance(row_match_execution_value, list)
                else []
            )
            result = exec_ns.get("result")
            if result is None:
                result = exec_ns.get("result_df")
            semantic_execution_certificate = {}
            deterministic_transform_execution = []
        rows, columns = _result_to_rows(result, next_payload)
        rows, columns = _apply_strict_result_columns(
            rows,
            columns,
            next_payload,
        )
        rows = _normalize_blank_dimension_values(rows, next_payload)
        rows = _normalize_missing_metric_values(rows, next_payload)
        missing_columns = _missing_required_output_columns(next_payload, columns)
        missing_columns.extend(_missing_aggregate_grain_columns(next_payload, columns))
        missing_columns.extend(_missing_result_segment_columns(next_payload, columns))
        if missing_columns:
            raise OutputContractError(
                "결과 계약에 필요한 컬럼이 누락되었습니다: " + ", ".join(missing_columns)
            )
        ordering_error = _ordering_contract_error(next_payload, rows, columns)
        if ordering_error:
            raise OutputContractError(ordering_error)
        next_payload["_full_result_rows"] = rows
        next_payload["analysis"] = {
            "status": "ok",
            "row_count": len(rows),
            "columns": columns,
            "used_helpers": helper_trace["used_helpers"],
            "step_outputs": step_outputs,
            "function_case_results": function_case_results,
            "execution_mode": execution_mode,
        }
        if semantic_execution_certificate:
            next_payload["analysis"]["semantic_execution_certificate"] = deepcopy(
                semantic_execution_certificate
            )
        next_payload["data"] = {"columns": columns, "rows": rows[:RESULT_PREVIEW_LIMIT], "row_count": len(rows), "data_ref": ""}
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_execution"] = {
            "stage": "17_pandas_code_executor",
            "status": "ok",
            "generated_code": code,
            "llm_generated_code": normalized_llm_code,
            "execution_mode": execution_mode,
            "llm_code_executed": not bool(deterministic_execution),
            "llm_response_parse": response_parse,
            "safe_import_normalization": _safe_import_trace(safe_imports),
            "used_helpers": helper_trace["used_helpers"],
            "helper_sources": helper_trace["helper_sources"],
            "pandas_filter_plan": filter_plan,
            "row_match_preamble": row_match_preamble,
            "row_match_plan": row_match_plan,
            "row_match_execution": row_match_execution,
            "deterministic_source_transforms": deterministic_transform_execution,
            "execution_result": {"row_count": len(rows), "columns": columns, "preview_rows": rows[:TRACE_PREVIEW_LIMIT]},
            "semantic_execution_certificate": deepcopy(semantic_execution_certificate),
            "error": None,
        }
        return next_payload
    except OutputContractError as exc:
        return _analysis_error(
            next_payload,
            "output_contract_violation",
            str(exc),
            code,
            traceback.format_exc(limit=3),
            llm_code,
            filter_preamble,
            filter_plan,
            safe_imports,
            row_match_plan,
            row_match_preamble,
            response_parse,
        )
    except Exception as exc:
        return _analysis_error(
            next_payload,
            "pandas_execution_error",
            f"{type(exc).__name__}: {exc}",
            code,
            traceback.format_exc(limit=3),
            llm_code,
            filter_preamble,
            filter_plan,
            safe_imports,
            row_match_plan,
            row_match_preamble,
            response_parse,
        )


# 함수 설명: 정규화기가 만든 신뢰 가능한 다중 source 계약 중 실행할 하나를 선택합니다.
def _deterministic_execution_contract(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    for key in (
        "resolved_empty_result_plan",
        "resolved_presence_comparison_plan",
        "resolved_metric_comparison_plan",
        "resolved_metric_merge_plan",
        "resolved_reference_join_plan",
    ):
        value = plan.get(key)
        if isinstance(value, dict) and value.get("strict") is True:
            return deepcopy(value)
    return {}


# 함수 설명: 내부 결정론적 executor가 실제 적용하는 join 이후 조건을 생성 코드 영역의 안전한 주석으로 표시합니다.
def _deterministic_contract_display_code(contract: dict[str, Any]) -> str:
    operation = str(contract.get("operation") or "").strip()
    if not operation:
        return ""
    lines = [
        "# --- deterministic execution contract (executed internally) ---",
        f"# operation: {_single_line_comment_value(operation)}",
    ]
    if operation == "compare_presence":
        left_metric = contract.get("left_metric") if isinstance(contract.get("left_metric"), dict) else {}
        right_metric = contract.get("right_metric") if isinstance(contract.get("right_metric"), dict) else {}
        left_column = str(left_metric.get("output_column") or "").strip()
        right_column = str(right_metric.get("output_column") or "").strip()
        lines.extend(
            [
                "# predicate: "
                f"{_single_line_comment_value(left_column)} > 0 and "
                f"{_single_line_comment_value(right_column)} <= 0 (missing right metric -> 0)",
                "# postcondition: every returned row must satisfy the predicate",
            ]
        )
    elif operation == "compare_metrics":
        lhs_column = str(contract.get("lhs_metric_column") or "").strip()
        rhs_column = str(contract.get("rhs_metric_column") or "").strip()
        operator = str(contract.get("operator") or "").strip()
        symbols = {"gt": ">", "ge": ">=", "lt": "<", "le": "<=", "eq": "==", "ne": "!="}
        lines.extend(
            [
                "# predicate: "
                f"{_single_line_comment_value(lhs_column)} {symbols.get(operator, operator)} "
                f"{_single_line_comment_value(rhs_column)}",
                "# postcondition: every returned row must satisfy the predicate",
            ]
        )
    elif operation == "merge_metric_sources":
        lines.append("# action: aggregate each metric source and merge on the resolved grain")
    return "\n".join(lines)


# 함수 설명: trace/display 주석 값에서 줄바꿈과 주석 제어 문자를 제거합니다.
def _single_line_comment_value(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("#", "").strip()


# 함수 설명: 결정론적 다중 source 계약의 typed Function Case 단계를 제한된 실행 preamble로 변환합니다.
def _deterministic_function_case_preamble(
    contract: dict[str, Any],
    helper_code_value: Any,
) -> tuple[str, str]:
    transforms = [
        item
        for item in contract.get("source_transforms", [])
        if isinstance(item, dict)
    ]
    if not transforms:
        return "", ""
    helper_code = _text_value(helper_code_value).strip()
    if not helper_code:
        return "", "결정론적 source transform에 필요한 Function Case helper 코드가 없습니다."
    try:
        tree = ast.parse(helper_code)
    except SyntaxError as exc:
        return "", f"Function Case helper 코드 구문이 유효하지 않습니다: {exc}"
    defined_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    if any(not isinstance(node, ast.FunctionDef) for node in tree.body):
        return "", "결정론적 source transform helper에는 최상위 함수 정의만 허용됩니다."
    guard_error = _guard_code(helper_code)
    if guard_error:
        return "", f"Function Case helper 안전성 검증에 실패했습니다: {guard_error}"

    lines = [helper_code, "_deterministic_function_case_execution = []"]
    for index, transform in enumerate(transforms, start=1):
        function_name = str(transform.get("function_name") or "").strip()
        source_alias = str(transform.get("source_alias") or "").strip()
        node_id = str(transform.get("node_id") or f"transform_{index}").strip()
        input_text = str(transform.get("input_text") or "")
        if not function_name.isidentifier() or function_name not in defined_names:
            return "", f"선택된 Function Case helper 정의를 찾을 수 없습니다: {function_name}"
        if not source_alias:
            return "", f"Function Case source alias가 비어 있습니다: {node_id}"
        arguments = (
            transform.get("arguments")
            if isinstance(transform.get("arguments"), dict)
            else {}
        )
        rendered_arguments: list[str] = []
        for key, value in arguments.items():
            name = str(key or "").strip()
            if not name.isidentifier():
                return "", f"Function Case argument 이름이 유효하지 않습니다: {name}"
            rendered, error = _python_literal(value)
            if error:
                return "", f"Function Case argument를 안전한 literal로 만들 수 없습니다: {name}"
            rendered_arguments.append(f"{name}={rendered}")
        call_suffix = (", " + ", ".join(rendered_arguments)) if rendered_arguments else ""
        source_var = f"_function_case_source_{index}"
        result_var = f"_function_case_result_{index}"
        lines.extend(
            [
                f"{source_var} = sources.get({source_alias!r})",
                f"if {source_var} is None:",
                f"    raise Exception({('Function Case source를 찾을 수 없습니다: ' + source_alias)!r})",
                f"{result_var} = {function_name}({input_text!r}, {source_var}{call_suffix})",
                f"if not hasattr({result_var}, 'columns'):",
                f"    raise Exception({('Function Case 결과가 DataFrame이 아닙니다: ' + function_name)!r})",
                f"sources[{source_alias!r}] = {result_var}",
                "_deterministic_function_case_execution.append({",
                f"    'node_id': {node_id!r},",
                f"    'source_alias': {source_alias!r},",
                f"    'function_name': {function_name!r},",
                f"    'input_text': {input_text!r},",
                f"    'input_row_count': len({source_var}),",
                f"    'output_row_count': len({result_var}),",
                "})",
            ]
        )
    return "\n".join(lines), ""


# 함수 설명: Function Case 구조화 인자를 코드 주입 없이 재현 가능한 Python literal로 제한합니다.
def _python_literal(value: Any) -> tuple[str, str]:
    rendered = repr(value)
    try:
        ast.literal_eval(rendered)
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    return rendered, ""


# 함수 설명: 다중 metric 병합 또는 직전 결과 enrich 계약을 pandas 코드 대신 내부 구현으로 실행합니다.
def _execute_deterministic_contract(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> Any:
    operation = str(contract.get("operation") or "").strip()
    if operation == "empty_result":
        columns = [
            str(item).strip()
            for item in contract.get("columns", [])
            if str(item or "").strip()
        ]
        return pd.DataFrame(columns=columns)
    if operation == "merge_metric_sources":
        return _execute_metric_source_merge(contract, sources, pd)
    if operation == "enrich_previous_result":
        return _execute_previous_result_enrichment(contract, sources, pd)
    if operation == "compare_presence":
        return _execute_presence_comparison(contract, sources, pd)
    if operation == "compare_metrics":
        return _execute_metric_comparison(contract, sources, pd)
    raise OutputContractError(f"지원하지 않는 deterministic 실행 계약입니다: {operation}")


# 함수 설명: `_apply_deterministic_result_ordering()`는 17 pandas 실행/1회 복구기 처리 중 deterministic·결과·ordering 관련 값을 계산·변환하는
#        내부 helper입니다.
def _apply_deterministic_result_ordering(
    result: Any,
    payload: dict[str, Any],
) -> Any:
    """Apply the normalized ordering contract after deterministic execution."""
    if not hasattr(result, "columns"):
        return result
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    output_contract = (
        plan.get("output_contract")
        if isinstance(plan.get("output_contract"), dict)
        else {}
    )
    ordering = (
        output_contract.get("ordering")
        if isinstance(output_contract.get("ordering"), dict)
        else {}
    )
    sort_by = str(ordering.get("sort_by") or "").strip()
    if not sort_by:
        return result
    actual_sort_column = _find_frame_column(result, [sort_by])
    if not actual_sort_column:
        return result
    order = str(ordering.get("order") or "desc").strip().lower()
    ordered = result.sort_values(
        by=actual_sort_column,
        ascending=order == "asc",
        na_position="last",
        kind="mergesort",
    )
    try:
        limit = max(0, int(ordering.get("limit") or 0))
    except (TypeError, ValueError):
        limit = 0
    if limit:
        ordered = ordered.head(limit)
    return ordered.reset_index(drop=True)


# 함수 설명: `_execute_presence_comparison()`는 presence·comparison 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
def _execute_presence_comparison(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> tuple[Any, dict[str, Any]]:
    """Execute a left-positive/right-missing-or-zero contract and verify its postcondition."""
    if str(contract.get("presence_rule") or "").strip() != "left_positive_right_missing_or_zero":
        raise OutputContractError("지원하지 않는 존재·부재 비교 규칙입니다.")
    grain_mappings = [
        item for item in contract.get("grain_mappings", []) if isinstance(item, dict)
    ]
    left_metric = contract.get("left_metric") if isinstance(contract.get("left_metric"), dict) else {}
    right_metric = contract.get("right_metric") if isinstance(contract.get("right_metric"), dict) else {}
    if not grain_mappings or not left_metric or not right_metric:
        raise OutputContractError("존재·부재 비교 계약에 grain 또는 metric이 부족합니다.")

    join_columns = [
        str(item.get("output_column") or item.get("canonical_column") or "").strip()
        for item in grain_mappings
    ]
    if any(not column for column in join_columns):
        raise OutputContractError("존재·부재 비교 grain output column이 비어 있습니다.")

    # 함수 설명: `aggregate_metric()`는 17 pandas 실행/1회 복구기 처리 중 metric 관련 값을 계산·변환하는 내부 helper입니다.
    def aggregate_metric(metric: dict[str, Any]) -> Any:
        alias = str(metric.get("source_alias") or "").strip()
        frame = sources.get(alias)
        if frame is None:
            raise OutputContractError(f"존재·부재 비교 source를 찾을 수 없습니다: {alias}")
        working = frame.copy()
        shaped = pd.DataFrame(index=working.index)
        for mapping, output_column in zip(grain_mappings, join_columns):
            source_candidates = (
                mapping.get("source_candidates", {}).get(alias, [])
                if isinstance(mapping.get("source_candidates"), dict)
                else []
            )
            source_column = _find_frame_column(working, _string_list(source_candidates))
            if not source_column:
                raise OutputContractError(
                    f"{alias} source에서 존재·부재 grain 컬럼을 찾을 수 없습니다: {output_column}"
                )
            shaped[output_column] = working[source_column].map(
                _normalize_deterministic_join_value
            )
        metric_column = _find_frame_column(
            working,
            _string_list(metric.get("source_candidates"))
            or _string_list(metric.get("source_column")),
        )
        output_column = str(metric.get("output_column") or "").strip()
        if not metric_column or not output_column:
            raise OutputContractError(f"{alias} source의 존재·부재 metric 계약이 유효하지 않습니다.")
        shaped["__metric_value__"] = pd.to_numeric(
            working[metric_column], errors="coerce"
        ).fillna(0)
        return _group_and_aggregate_frame(
            shaped,
            join_columns,
            "__metric_value__",
            output_column,
            str(metric.get("aggregation") or "sum").strip().lower(),
            pd,
        )

    left_grouped = aggregate_metric(left_metric)
    right_grouped = aggregate_metric(right_metric)
    left_output = str(left_metric.get("output_column") or "").strip()
    right_output = str(right_metric.get("output_column") or "").strip()
    left_positive = left_grouped[left_grouped[left_output].fillna(0) > 0].copy()
    merged = left_positive.merge(right_grouped, on=join_columns, how="left")
    merged[right_output] = pd.to_numeric(
        merged[right_output], errors="coerce"
    ).fillna(0)
    right_positive_mask = merged[right_output] > 0
    result = merged[~right_positive_mask].copy().reset_index(drop=True)
    postcondition_passed = bool(
        (result[left_output].fillna(0) > 0).all()
        and (result[right_output].fillna(0) <= 0).all()
    )
    if not postcondition_passed:
        raise OutputContractError("존재·부재 비교 결과가 실행 후 조건 검증을 통과하지 못했습니다.")
    certificate = {
        "operation": "compare_presence",
        "presence_rule": "left_positive_right_missing_or_zero",
        "postcondition_validation": "passed",
        "left_positive_key_count": int(len(left_positive)),
        "excluded_right_positive_key_count": int(right_positive_mask.sum()),
        "result_key_count": int(len(result)),
        "left_metric_column": left_output,
        "right_metric_column": right_output,
        "grain_columns": join_columns,
    }
    return result, certificate


# 함수 설명: 병합된 두 수치 metric에 typed 비교 연산을 적용하고 모든 결과 행의 조건 충족 여부를 검증합니다.
def _execute_metric_comparison(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> tuple[Any, dict[str, Any]]:
    merge_plan = contract.get("merge_plan") if isinstance(contract.get("merge_plan"), dict) else {}
    if merge_plan.get("strict") is not True:
        raise OutputContractError("수치 비교의 선행 metric 병합 계약이 유효하지 않습니다.")
    merged = _execute_metric_source_merge(merge_plan, sources, pd)
    lhs_name = str(contract.get("lhs_metric_column") or "").strip()
    rhs_name = str(contract.get("rhs_metric_column") or "").strip()
    operator = str(contract.get("operator") or "").strip().lower()
    lhs_column = _find_frame_column(merged, [lhs_name])
    rhs_column = _find_frame_column(merged, [rhs_name])
    if not lhs_column or not rhs_column or lhs_column == rhs_column:
        raise OutputContractError("수치 비교 결과에서 양쪽 metric 컬럼을 확정할 수 없습니다.")
    comparisons: dict[str, Callable[[Any, Any], Any]] = {
        "gt": lambda lhs, rhs: lhs > rhs,
        "ge": lambda lhs, rhs: lhs >= rhs,
        "lt": lambda lhs, rhs: lhs < rhs,
        "le": lambda lhs, rhs: lhs <= rhs,
        "eq": lambda lhs, rhs: lhs == rhs,
        "ne": lambda lhs, rhs: lhs != rhs,
    }
    comparison = comparisons.get(operator)
    if comparison is None:
        raise OutputContractError(f"지원하지 않는 수치 metric 비교 연산자입니다: {operator}")

    working = merged.copy()
    working[lhs_column] = pd.to_numeric(working[lhs_column], errors="coerce")
    working[rhs_column] = pd.to_numeric(working[rhs_column], errors="coerce")
    valid_operands = working[lhs_column].notna() & working[rhs_column].notna()
    mask = valid_operands & comparison(working[lhs_column], working[rhs_column]).fillna(False)
    result = working[mask].copy().reset_index(drop=True)
    postcondition_mask = comparison(result[lhs_column], result[rhs_column]).fillna(False)
    if not bool(postcondition_mask.all()):
        raise OutputContractError("수치 metric 비교 결과가 실행 후 조건 검증을 통과하지 못했습니다.")
    certificate = {
        "operation": "compare_metrics",
        "lhs_metric_column": lhs_name,
        "operator": operator,
        "rhs_metric_column": rhs_name,
        "null_numeric_policy": "exclude_missing_operand",
        "postcondition_validation": "passed",
        "input_row_count": int(len(working)),
        "missing_operand_row_count": int((~valid_operands).sum()),
        "excluded_row_count": int((~mask).sum()),
        "result_row_count": int(len(result)),
    }
    return result, certificate


# 함수 설명: 서로 다른 source의 metric을 각자 집계하고 정규화 grain으로 outer merge합니다.
def _execute_metric_source_merge(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> Any:
    grain_mappings = [
        item
        for item in contract.get("grain_mappings", [])
        if isinstance(item, dict)
    ]
    metrics = [
        item for item in contract.get("metrics", []) if isinstance(item, dict)
    ]
    if not grain_mappings or len(metrics) < 2:
        raise OutputContractError("다중 metric 병합 계약에 grain 또는 metric이 부족합니다.")
    aggregated_frames: list[Any] = []
    join_columns = [
        str(item.get("output_column") or item.get("canonical_column") or "").strip()
        for item in grain_mappings
    ]
    if any(not column for column in join_columns):
        raise OutputContractError("다중 metric 병합 계약의 grain output column이 비어 있습니다.")

    for metric in metrics:
        alias = str(metric.get("source_alias") or "").strip()
        frame = sources.get(alias)
        if frame is None:
            raise OutputContractError(f"metric source를 찾을 수 없습니다: {alias}")
        working = frame.copy()
        selected_columns: dict[str, str] = {}
        for mapping, output_column in zip(grain_mappings, join_columns):
            source_candidates = (
                mapping.get("source_candidates", {}).get(alias, [])
                if isinstance(mapping.get("source_candidates"), dict)
                else []
            )
            source_column = _find_frame_column(working, source_candidates)
            if not source_column:
                raise OutputContractError(
                    f"{alias} source에서 grain 컬럼을 찾을 수 없습니다: {output_column}"
                )
            selected_columns[output_column] = source_column
        metric_column = _find_frame_column(
            working,
            _string_list(metric.get("source_candidates"))
            or _string_list(metric.get("source_column")),
        )
        output_metric = str(metric.get("output_column") or "").strip()
        aggregation = str(metric.get("aggregation") or "sum").strip().lower()
        if not metric_column or not output_metric:
            raise OutputContractError(
                f"{alias} source의 metric 컬럼 또는 출력명이 유효하지 않습니다."
            )
        shaped = pd.DataFrame(index=working.index)
        for output_column, source_column in selected_columns.items():
            shaped[output_column] = working[source_column].map(
                _normalize_deterministic_join_value
            )
        shaped["__metric_value__"] = working[metric_column]
        grouped = _group_and_aggregate_frame(
            shaped,
            join_columns,
            "__metric_value__",
            output_metric,
            aggregation,
            pd,
        )
        aggregated_frames.append(grouped)

    result = aggregated_frames[0]
    join_type = str(contract.get("join_type") or "outer").strip().lower()
    merge_how = join_type if join_type in {"left", "inner", "outer"} else "outer"
    for right in aggregated_frames[1:]:
        result = result.merge(right, on=join_columns, how=merge_how)
    if contract.get("fill_zero_on_success") is True:
        for metric in metrics:
            output_metric = str(metric.get("output_column") or "").strip()
            if output_metric in result.columns:
                result[output_metric] = result[output_metric].fillna(
                    metric.get("fill_value", 0)
                )
    return result


# 함수 설명: 직전 결과를 left table로 고정하고 신규 source 집계를 실제 물리 key로 병합합니다.
def _execute_previous_result_enrichment(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> Any:
    left_alias = str(contract.get("left_source_alias") or "").strip()
    right_alias = str(contract.get("right_source_alias") or "").strip()
    left = sources.get(left_alias)
    right = sources.get(right_alias)
    if left is None or right is None:
        missing = left_alias if left is None else right_alias
        raise OutputContractError(f"후속 enrich source를 찾을 수 없습니다: {missing}")
    left = left.copy()
    right = right.copy()
    key_mappings = [
        item for item in contract.get("key_mappings", []) if isinstance(item, dict)
    ]
    aggregations = [
        item for item in contract.get("aggregations", []) if isinstance(item, dict)
    ]
    if not key_mappings or not aggregations:
        raise OutputContractError("후속 enrich 계약에 key 또는 aggregation이 부족합니다.")

    temp_keys: list[str] = []
    for index, mapping in enumerate(key_mappings, start=1):
        left_column = str(mapping.get("left_column") or "").strip()
        right_column = _find_frame_column(
            right,
            _string_list(mapping.get("right_candidates")),
        )
        if left_column not in left.columns or not right_column:
            raise OutputContractError(
                "후속 enrich key 컬럼을 찾을 수 없습니다: "
                + str(mapping.get("canonical_key") or left_column)
            )
        temp_key = f"__join_key_{index}__"
        temp_keys.append(temp_key)
        left[temp_key] = left[left_column].map(_normalize_deterministic_join_value)
        right[temp_key] = right[right_column].map(_normalize_deterministic_join_value)

    named_aggregations: dict[str, Any] = {}
    fill_defaults: dict[str, Any] = {}
    for item in aggregations:
        source_column = _find_frame_column(
            right,
            _string_list(item.get("source_candidates"))
            or _string_list(item.get("source_column")),
        )
        output_column = str(item.get("output_column") or "").strip()
        method = str(item.get("aggregation") or "sum").strip().lower()
        if not source_column or not output_column:
            raise OutputContractError("후속 enrich aggregation 컬럼 계약이 유효하지 않습니다.")
        if output_column in left.columns:
            left = left.drop(columns=[output_column])
        if method == "collect_unique":
            named_aggregations[output_column] = pd.NamedAgg(
                column=source_column,
                aggfunc=_collect_unique_display_values,
            )
            fill_defaults[output_column] = ""
        else:
            named_aggregations[output_column] = pd.NamedAgg(
                column=source_column,
                aggfunc=_pandas_aggregation_method(method),
            )
            fill_defaults[output_column] = (
                0 if method in {"sum", "count", "nunique"} else None
            )
    if right.empty:
        right_grouped = pd.DataFrame(columns=[*temp_keys, *named_aggregations])
    else:
        right_grouped = (
            right.groupby(temp_keys, dropna=False)
            .agg(**named_aggregations)
            .reset_index()
        )
    result = left.merge(right_grouped, on=temp_keys, how="left")
    result = result.drop(columns=temp_keys)
    for output_column, default in fill_defaults.items():
        if output_column in result.columns and default is not None:
            result[output_column] = result[output_column].fillna(default)
    return result


# 함수 설명: 단일 metric 집계를 NamedAgg 호환 방식으로 실행하고 빈 source schema도 보존합니다.
def _group_and_aggregate_frame(
    frame: Any,
    group_columns: list[str],
    source_column: str,
    output_column: str,
    method: str,
    pd: Any,
) -> Any:
    if frame.empty:
        return pd.DataFrame(columns=[*group_columns, output_column])
    aggregation = _pandas_aggregation_method(method)
    return (
        frame.groupby(group_columns, dropna=False)
        .agg(
            **{
                output_column: pd.NamedAgg(
                    column=source_column,
                    aggfunc=aggregation,
                )
            }
        )
        .reset_index()
    )


# 함수 설명: 허용 집계명을 pandas NamedAgg에서 사용할 함수명으로 제한합니다.
def _pandas_aggregation_method(method: str) -> Any:
    normalized = str(method or "").strip().lower()
    if normalized == "collect_unique":
        return _collect_unique_display_values
    allowed = {
        "sum",
        "mean",
        "min",
        "max",
        "count",
        "nunique",
        "median",
        "first",
        "last",
    }
    if normalized not in allowed:
        raise OutputContractError(f"지원하지 않는 deterministic 집계 방식입니다: {method}")
    return normalized


# 함수 설명: collect_unique 계약에 따라 중복 없는 값을 원래 순서대로 쉼표 문자열로 만듭니다.
def _collect_unique_display_values(values: Any) -> str:
    result: list[str] = []
    for value in values:
        normalized = _normalize_deterministic_join_value(value)
        if normalized and normalized not in result:
            result.append(normalized)
    return ", ".join(result)


# 함수 설명: join key의 null·공백·정수형 float 표현을 동일한 문자열로 정규화합니다.
def _normalize_deterministic_join_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.casefold() in BLANK_MATCH_TEXTS:
        return ""
    signless = text[1:] if text.startswith("-") else text
    parts = signless.split(".", 1)
    if (
        len(parts) == 2
        and parts[0].isdigit()
        and parts[1]
        and set(parts[1]) == {"0"}
    ):
        return text.split(".", 1)[0]
    return text


# 함수 설명: DataFrame 실제 컬럼에서 후보와 대소문자 무시 일치하는 첫 값을 선택합니다.
def _find_frame_column(frame: Any, candidates: list[str]) -> str:
    available = {
        str(column).strip().casefold(): str(column)
        for column in getattr(frame, "columns", [])
        if str(column).strip()
    }
    for candidate in candidates:
        matched = available.get(str(candidate).strip().casefold())
        if matched:
            return matched
    return ""


# 함수 설명: strict result_columns 계약에 따라 alias를 하나로 rename하고 선언되지 않은 중복·추가 컬럼을 제거합니다.
def _apply_strict_result_columns(
    rows: list[dict[str, Any]],
    columns: list[str],
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = (
        plan.get("output_contract")
        if isinstance(plan.get("output_contract"), dict)
        else {}
    )
    if contract.get("strict_result_columns") is not True:
        return rows, columns
    declared = _string_list(contract.get("result_columns"))
    if not declared:
        return rows, columns

    selections: list[tuple[str, str]] = []
    used_actual: dict[str, str] = {}
    for target in declared:
        equivalents = {
            str(item).strip().casefold()
            for item in _equivalent_column_names(target, payload)
            if str(item).strip()
        }
        matches = [
            column
            for column in columns
            if str(column).strip().casefold() in equivalents
        ]
        if not matches:
            continue
        exact = next(
            (
                column
                for column in matches
                if str(column).strip().casefold() == target.casefold()
            ),
            "",
        )
        selected = exact or matches[0]
        _validate_equivalent_result_values(rows, target, matches)
        marker = selected.casefold()
        if marker in used_actual and used_actual[marker] != target:
            raise OutputContractError(
                "동일한 실제 컬럼이 여러 결과 컬럼으로 중복 선언되었습니다: "
                f"{used_actual[marker]}, {target}"
            )
        used_actual[marker] = target
        selections.append((target, selected))

    projected_rows = [
        {
            target: row.get(actual)
            for target, actual in selections
            if actual in row
        }
        for row in rows
    ]
    return projected_rows, [target for target, _ in selections]


# 함수 설명: canonical/physical 동의 컬럼이 동시에 존재할 때 값이 다르면 임의 선택하지 않고 계약 오류로 차단합니다.
def _validate_equivalent_result_values(
    rows: list[dict[str, Any]],
    target: str,
    matches: list[str],
) -> None:
    if len(matches) < 2:
        return
    for row in rows:
        values = [
            _normalize_deterministic_join_value(row.get(column))
            for column in matches
            if column in row
        ]
        if len(set(values)) > 1:
            raise OutputContractError(
                f"동일 의미 결과 컬럼의 값이 충돌합니다: {target} ({', '.join(matches)})"
            )


# 주요 함수: 최초 실행 실패 시 이전 코드와 오류를 전달해 최대 한 번 복구한 결과를 반환합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def execute_pandas_with_repair(
    payload_value: Any,
    llm_response: Any,
    repair_invoker: Callable[[str], Any] | None = None,
    repair_prompt_template: str = "",
    function_case_helper_code: str = "",
    max_repair_attempts: Any = DEFAULT_MAX_REPAIR_ATTEMPTS,
) -> dict[str, Any]:
    """Execute once and invoke one prompt-based repair only for an actual execution error."""

    original_payload = _payload(payload_value)
    if _execution_blocked(original_payload):
        return _blocked_execution_payload(original_payload)
    initial = execute_pandas_code(
        original_payload,
        llm_response,
        function_case_helper_code=function_case_helper_code,
    )
    initial_status = _analysis_status(initial)
    current_attempt = _nonnegative_int(original_payload.get("pandas_retry_attempt"), 0)
    max_attempts = min(DEFAULT_MAX_REPAIR_ATTEMPTS, _nonnegative_int(max_repair_attempts, DEFAULT_MAX_REPAIR_ATTEMPTS))
    base_trace = {
        "stage": "17_pandas_code_executor",
        "initial_status": initial_status or "missing",
        "initial_error": deepcopy(_analysis_error_value(initial)),
        "max_attempts": max_attempts,
        "attempt": current_attempt,
        "attempted": False,
        "llm_called": False,
        "selected": "initial",
    }
    if initial_status in {"ok", "success"}:
        base_trace["reason"] = "초기 pandas 실행이 성공하여 repair LLM을 호출하지 않았습니다."
        return _with_repair_trace(initial, base_trace)
    if not _repairable_execution_failure(initial):
        base_trace["reason"] = "조회·스키마·결과 계약 오류는 pandas 코드 재생성으로 해결할 수 없어 repair LLM을 호출하지 않았습니다."
        base_trace["selected"] = "blocked_contract_error"
        return _with_repair_trace(initial, base_trace)
    if max_attempts == 0 or current_attempt >= max_attempts:
        base_trace["reason"] = "pandas repair가 비활성화되었거나 최대 1회 시도 한도에 도달했습니다."
        return _with_repair_trace(initial, base_trace)
    if not callable(repair_invoker):
        base_trace["reason"] = "repair model 호출기가 없어 초기 오류 결과를 유지했습니다."
        base_trace["repair_error"] = {"type": "missing_repair_invoker", "message": "repair_invoker is required"}
        return _with_repair_trace(initial, base_trace)

    attempt = current_attempt + 1
    base_trace.update({"attempt": attempt, "attempted": True})
    initial_code = _initial_failed_code(initial)
    if initial_code:
        base_trace["initial_code_sha256"] = hashlib.sha256(initial_code.encode("utf-8")).hexdigest()
        base_trace["initial_code_preview"] = initial_code[:REPAIR_CODE_PREVIEW_LIMIT]
    try:
        repair_prompt = build_pandas_repair_prompt(initial, repair_prompt_template, function_case_helper_code)
        base_trace["repair_prompt_chars"] = len(repair_prompt)
    except Exception as exc:
        base_trace["reason"] = "repair prompt를 만들지 못해 초기 오류 결과를 유지했습니다."
        base_trace["repair_error"] = {"type": "repair_prompt_error", "message": f"{type(exc).__name__}: {exc}"}
        return _with_repair_trace(initial, base_trace)

    try:
        repair_response = repair_invoker(repair_prompt)
        base_trace["llm_called"] = True
    except Exception as exc:
        base_trace["reason"] = "repair LLM 호출이 실패해 초기 오류 결과를 유지했습니다."
        base_trace["repair_error"] = {"type": "repair_llm_error", "message": f"{type(exc).__name__}: {exc}"}
        return _with_repair_trace(initial, base_trace)

    retry = execute_pandas_code(
        original_payload,
        repair_response,
        function_case_helper_code=function_case_helper_code,
    )
    retry["pandas_retry_attempt"] = attempt
    retry_status = _analysis_status(retry)
    base_trace["retry_status"] = retry_status or "missing"
    base_trace["retry_error"] = deepcopy(_analysis_error_value(retry))
    if retry_status in {"ok", "success"}:
        base_trace["selected"] = "retry"
        base_trace["reason"] = "repair LLM이 수정한 pandas 코드의 1회 재실행이 성공했습니다."
        retry.setdefault("analysis", {})["repair_applied"] = True
        return _with_repair_trace(retry, base_trace)

    base_trace["selected"] = "retry_error"
    base_trace["reason"] = "repair 코드 재실행도 실패하여 최종 재실행 오류를 반환했습니다."
    retry_code = _initial_failed_code(retry)
    if retry_code:
        base_trace["retry_code_sha256"] = hashlib.sha256(retry_code.encode("utf-8")).hexdigest()
        base_trace["retry_code_preview"] = retry_code[:REPAIR_CODE_PREVIEW_LIMIT]
    return _with_repair_trace(retry, base_trace)


# 함수 설명: 실제 생성 코드의 실행 오류만 repair 대상으로 허용하고 구조 계약 오류의 동일 재시도를 막습니다.
def _repairable_execution_failure(payload: dict[str, Any]) -> bool:
    error = _analysis_error_value(payload)
    error_type = str(error.get("type") or "").strip().lower() if isinstance(error, dict) else ""
    return error_type in {"pandas_execution_error", "unsafe_code"}


# 주요 함수: 복구 LLM이 원인과 기존 코드를 함께 볼 수 있도록 수정 프롬프트를 조립합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_pandas_repair_prompt(payload_value: Any, template: Any, function_case_helper_code: str = "") -> str:
    payload = _payload(payload_value)
    prompt_template = _text_value(template).strip()
    if not prompt_template:
        raise ValueError("repair_prompt_template is empty")
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    source_columns = _source_columns_by_alias(payload)
    source_schema: dict[str, list[str]] = {}
    source_preview: dict[str, list[dict[str, Any]]] = {}
    for alias, rows in runtime_sources.items():
        if not isinstance(rows, list):
            continue
        row_columns = sorted({str(column) for row in rows[:20] if isinstance(row, dict) for column in row})
        source_schema[str(alias)] = row_columns or source_columns.get(str(alias), [])
        source_preview[str(alias)] = [deepcopy(row) for row in rows[:TRACE_PREVIEW_LIMIT] if isinstance(row, dict)]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    prompt_plan = _repair_prompt_contract(plan)
    pandas_trace = _pandas_execution_trace(payload)
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    values = {
        "repair_required": "true",
        "intent_plan_json": json.dumps(prompt_plan, ensure_ascii=False, indent=2),
        "source_schema_json": json.dumps(source_schema, ensure_ascii=False, indent=2),
        "source_preview_json": json.dumps(source_preview, ensure_ascii=False, indent=2),
        "failed_code": _initial_failed_code(payload),
        "error_context_json": json.dumps(
            {
                "analysis_error": deepcopy(analysis.get("error", {})),
                "analysis_errors": deepcopy(analysis.get("errors", [])),
                "repairable_errors": deepcopy(analysis.get("repairable_errors", [])),
                "trace_error": deepcopy(pandas_trace.get("error", {})),
                "executed_code_with_preamble": str(pandas_trace.get("generated_code") or analysis.get("analysis_code") or ""),
                "row_match_preamble": str(pandas_trace.get("row_match_preamble") or ""),
                "pandas_filter_preamble": str(pandas_trace.get("pandas_filter_preamble") or analysis.get("pandas_filter_preamble") or ""),
                "pandas_filter_plan": deepcopy(pandas_trace.get("pandas_filter_plan", [])),
                "row_match_plan": deepcopy(pandas_trace.get("row_match_plan", [])),
                "repair_code_scope": "executor가 동일한 pandas filter preamble을 retry 코드에 다시 자동 적용하며, 동일한 row match도 먼저 재적용합니다.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        "function_case_selection_json": json.dumps(_repair_function_case_selection(prompt_plan), ensure_ascii=False, indent=2),
        "function_case_helper_code": _text_value(function_case_helper_code),
        "output_schema": json.dumps({"code": "수정된 pandas code. 반드시 result 또는 result_df를 설정한다."}, ensure_ascii=False, indent=2),
    }
    try:
        return prompt_template.format(**values)
    except KeyError as exc:
        raise ValueError(f"unknown repair prompt variable: {exc.args[0]}") from exc


# 함수 설명: repair LLM도 최초 생성기와 같은 표준 컬럼 계약만 보도록
# 물리 컬럼 후보용 lineage 필드를 제거합니다. 실패 코드는 오류 분석을 위해
# 별도 필드로 그대로 제공됩니다.
def _repair_prompt_contract(value: Any) -> Any:
    physical_lineage_keys = {
        "source_candidates",
        "left_candidates",
        "right_candidates",
        "column_mappings",
        "key_mappings",
        "right_value_mappings",
        "filter_mappings",
        "standard_column_aliases",
    }
    if isinstance(value, list):
        return [_repair_prompt_contract(item) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    return {
        str(key): _repair_prompt_contract(item)
        for key, item in value.items()
        if str(key) not in physical_lineage_keys
    }


# 함수 설명: `_repair_function_case_selection()`는 복구 프롬프트에 전달할 선택 Function Case와 실행 단계만 작은 구조로 복사합니다.
def _repair_function_case_selection(plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    selected_steps = [deepcopy(step) for step in steps if isinstance(step, dict) and str(step.get("operation") or "") == "apply_pandas_function_case"]
    selected_cases: list[dict[str, Any]] = []
    single = plan.get("pandas_function_case")
    if isinstance(single, dict) and single:
        selected_cases.append(deepcopy(single))
    for item in plan.get("pandas_function_cases", []) if isinstance(plan.get("pandas_function_cases"), list) else []:
        if isinstance(item, dict) and item not in selected_cases:
            selected_cases.append(deepcopy(item))
    return {"selected_cases": selected_cases, "selected_steps": selected_steps}


# 함수 설명: `_pandas_execution_trace()`는 payload trace에서 기존 pandas 실행 기록을 안전한 dict로 꺼냅니다.
def _pandas_execution_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    value = inspection.get("pandas_execution")
    return value if isinstance(value, dict) else {}


# 함수 설명: `_initial_failed_code()`는 실패 trace와 분석 결과에서 최초 생성 코드를 우선순위대로 복원합니다.
def _initial_failed_code(payload: dict[str, Any]) -> str:
    pandas_trace = _pandas_execution_trace(payload)
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    return str(pandas_trace.get("llm_generated_code") or analysis.get("llm_generated_code") or pandas_trace.get("generated_code") or analysis.get("analysis_code") or "")


# 함수 설명: `_analysis_status()`는 분석 payload의 현재 pandas 실행 상태를 표준 문자열로 읽습니다.
def _analysis_status(payload: dict[str, Any]) -> str:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    return str(analysis.get("status") or "").strip().lower()


# 함수 설명: `_analysis_error_value()`는 분석 payload에 기록된 실행 오류를 안전한 dict로 꺼냅니다.
def _analysis_error_value(payload: dict[str, Any]) -> Any:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    return analysis.get("error") or analysis.get("errors") or []


# 함수 설명: `_with_repair_trace()`는 최초 코드·오류·수정 코드·재실행 결과를 한 번의 repair trace로 합칩니다.
def _with_repair_trace(payload_value: dict[str, Any], repair_trace: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(payload_value)
    payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_repair"] = deepcopy(repair_trace)
    return payload


# 함수 설명: `_nonnegative_int()`는 입력값을 0 이상의 정수로 제한해 횟수·크기 설정에 음수가 들어가지 않게 합니다.
def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return default


# 함수 설명: `_normalize_safe_imports()`는 허용된 pandas/numpy import 문만 제거하고 executor가 주입한 신뢰 namespace를 사용하게 합니다.
def _normalize_safe_imports(code: str) -> tuple[str, dict[str, Any]]:
    raw_code = str(code or "")
    removed_imports: list[str] = []
    numpy_requested = False
    normalized_lines = raw_code.splitlines(keepends=True)
    try:
        tree = ast.parse(raw_code)
    except SyntaxError:
        tree = None
    removable_lines: dict[int, str] = {}
    for node in tree.body if tree is not None else []:
        if not isinstance(node, ast.Import) or len(node.names) != 1:
            continue
        if node.lineno != node.end_lineno or node.col_offset != 0:
            continue
        line_index = node.lineno - 1
        if line_index < 0 or line_index >= len(normalized_lines):
            continue
        content = normalized_lines[line_index].rstrip("\r\n")
        import_name = _safe_import_name(content)
        if import_name:
            removable_lines[line_index] = import_name
    for line_index, import_name in removable_lines.items():
        line = normalized_lines[line_index]
        content = line.rstrip("\r\n")
        newline = line[len(content) :]
        normalized_lines[line_index] = newline
        removed_imports.append(import_name)
        numpy_requested = numpy_requested or import_name == "import numpy as np"
    normalized = "".join(normalized_lines)
    return normalized, {
        "policy": SAFE_IMPORT_POLICY,
        "removed_imports": list(dict.fromkeys(removed_imports)),
        "numpy_requested": numpy_requested,
        "normalized_llm_code": normalized,
    }


# 함수 설명: `_safe_import_name()`는 import 문이 정확히 허용된 pandas/numpy alias 형태인지 확인합니다.
def _safe_import_name(line: str) -> str:
    patterns = {
        "import pandas as pd": r"import[ \t]+pandas[ \t]+as[ \t]+pd(?:[ \t]*#.*)?",
        "import numpy as np": r"import[ \t]+numpy[ \t]+as[ \t]+np(?:[ \t]*#.*)?",
    }
    for canonical, pattern in patterns.items():
        if re.fullmatch(pattern, line):
            return canonical
    return ""


# 함수 설명: `_safe_numpy_namespace()`는 허용 attribute만 노출하는 제한된 numpy namespace를 구성합니다.
def _safe_numpy_namespace() -> Any:
    import numpy as numpy_module  # type: ignore

    class SafeNumpyNamespace:
        pass

    namespace = SafeNumpyNamespace()
    for attribute in SAFE_NUMPY_ATTRIBUTES:
        setattr(namespace, attribute, getattr(numpy_module, attribute))
    return namespace


# 함수 설명: `_safe_import_trace()`는 허용 import 정규화 내역을 실행 근거에 남길 수 있는 작은 trace로 만듭니다.
def _safe_import_trace(value: dict[str, Any]) -> dict[str, Any]:
    removed = [str(item) for item in value.get("removed_imports", []) if str(item).strip()]
    if not removed:
        return {}
    namespaces = ["pd"]
    if value.get("numpy_requested") is True:
        namespaces.append("np_safe")
    return {
        "policy": str(value.get("policy") or SAFE_IMPORT_POLICY),
        "removed_imports": removed,
        "provided_namespaces": namespaces,
    }


# 함수 설명: `_guard_code()`는 생성된 pandas 코드 AST를 검사해 import·파일·네트워크·위험 builtin 사용을 차단합니다.
def _guard_code(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "import 문은 허용하지 않습니다."
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            return f"{node.func.id} 호출은 허용하지 않습니다."
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "dtype"
        ):
            return (
                "str(series.dtype)는 제한 실행 환경에서 KeyError: '__import__'를 유발할 수 있습니다. "
                "dtype 확인이 필요하면 series.dtype == object를 사용하거나 join key의 dtype 분기를 제거하세요."
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return "dunder attribute 접근은 허용하지 않습니다."
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_IO_ATTRIBUTES:
            return f"파일/네트워크 I/O attribute '{node.attr}' 접근은 허용하지 않습니다."
    return ""


# 함수 설명: trusted catalog의 비가산 metric 계약과 pandas 계획·코드의 집계 방식이 충돌하는지 검사합니다.
def _metric_semantics_contract_error(payload: dict[str, Any], code: str) -> str:
    lineage_error = _cross_metric_copy_contract_error(payload, code)
    if lineage_error:
        return lineage_error
    semantics = _non_additive_metric_semantics(payload)
    if not semantics:
        return ""
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        aggregation_specs = [
            {
                "column": (
                    step.get("agg_column")
                    or step.get("aggregate_column")
                    or step.get("aggregation_column")
                    or step.get("metric_column")
                ),
                "method": (
                    step.get("agg_method")
                    or step.get("aggregate_method")
                    or step.get("aggregation")
                ),
            }
        ]
        if isinstance(step.get("aggregations"), list):
            aggregation_specs.extend(
                item
                for item in step["aggregations"]
                if isinstance(item, dict)
            )
        for spec in aggregation_specs:
            metric = str(spec.get("column") or "").strip()
            method = str(spec.get("method") or "").strip().lower()
            contract = semantics.get(metric.casefold())
            # collect_unique produces an identifier/list projection, not a
            # numeric rollup of the source metric. Its validity is governed by
            # the aggregation/output contract and must not be rejected by a
            # non-additive numeric metric policy such as EQP_ID/nunique.
            if method == "collect_unique":
                continue
            if contract and method and method not in contract["allowed_rollups"]:
                return f"비가산 metric {metric}에는 {method} 집계를 사용할 수 없습니다."
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return ""
    declared_metrics = {
        item.casefold()
        for item in _string_list(
            (plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}).get("metric_columns")
        )
    }
    active_metrics = {
        metric: contract
        for metric, contract in semantics.items()
        if not declared_metrics or metric in declared_metrics
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = (
            node.func.attr.lower()
            if isinstance(node.func, ast.Attribute)
            else node.func.id.lower()
            if isinstance(node.func, ast.Name)
            else ""
        )
        string_values = {
            str(item.value).strip().casefold()
            for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        if function_name in {"agg", "aggregate"} and "sum" in string_values:
            matched = [metric for metric in active_metrics if metric in string_values]
            if matched:
                return f"비가산 metric {matched[0]}에는 sum 집계를 사용할 수 없습니다."
        if function_name != "sum":
            continue
        matched = [metric for metric in active_metrics if metric in string_values]
        if matched:
            return f"비가산 metric {matched[0]}에는 sum 집계를 사용할 수 없습니다."
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        has_groupby = any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == "groupby"
            for item in ast.walk(receiver)
        ) if receiver is not None else False
        has_column_selection = any(isinstance(item, ast.Subscript) for item in ast.walk(receiver)) if receiver is not None else False
        if has_groupby and not has_column_selection and active_metrics:
            metric = next(iter(active_metrics))
            return f"비가산 metric {metric}을 포함한 groupby 결과 전체에 sum을 사용할 수 없습니다."
    return ""


# 함수 설명: 서로 다른 source binding을 가진 metric 컬럼 간 직접 복사를 AST에서 찾아 차단합니다.
def _cross_metric_copy_contract_error(payload: dict[str, Any], code: str) -> str:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = (
        plan.get("output_contract")
        if isinstance(plan.get("output_contract"), dict)
        else {}
    )
    bindings = [
        item for item in contract.get("metric_bindings", []) if isinstance(item, dict)
    ]
    if len(bindings) < 2:
        return ""
    identities: dict[str, tuple[str, str, str]] = {}
    for item in bindings:
        identity = (
            str(item.get("source_alias") or "").casefold(),
            str(item.get("dataset_key") or "").casefold(),
            str(item.get("source_column") or "").casefold(),
        )
        for column in (
            item.get("output_column"),
            item.get("source_column"),
        ):
            text = str(column or "").strip()
            if text:
                identities[text.casefold()] = identity
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        source_column = _subscript_string_column(value)
        if not source_column:
            continue
        source_identity = identities.get(source_column.casefold())
        if not source_identity:
            continue
        for target in targets:
            target_column = _subscript_string_column(target)
            target_identity = identities.get(target_column.casefold())
            if (
                target_column
                and target_identity
                and target_identity != source_identity
            ):
                return (
                    "서로 다른 조회 source의 metric을 직접 복사할 수 없습니다: "
                    f"{target_column} = {source_column}"
                )
    return ""


# 함수 설명: frame['COLUMN'] 형태 AST에서 문자열 컬럼명을 추출합니다.
def _subscript_string_column(node: Any) -> str:
    if not isinstance(node, ast.Subscript):
        return ""
    slice_value = node.slice
    if isinstance(slice_value, ast.Constant) and isinstance(slice_value.value, str):
        return str(slice_value.value).strip()
    return ""


# 함수 설명: retrieval job별 metric_semantics에서 additive=false metric과 허용 집계를 모읍니다.
def _non_additive_metric_semantics(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    result: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        semantics = job.get("metric_semantics") if isinstance(job.get("metric_semantics"), dict) else {}
        for metric, raw_contract in semantics.items():
            if not isinstance(raw_contract, dict) or raw_contract.get("additive") is not False:
                continue
            allowed = {
                str(item).strip().lower()
                for item in _string_list(raw_contract.get("allowed_rollups"))
                if str(item).strip()
            }
            default_rollup = str(raw_contract.get("default_rollup") or "").strip().lower()
            if default_rollup:
                allowed.add(default_rollup)
            result[str(metric).strip().casefold()] = {
                "allowed_rollups": allowed or {"mean"},
            }
    return result


# 함수 설명: `_result_to_rows()`는 DataFrame·list·dict·scalar 실행 결과를 rows와 columns 계약으로 변환합니다.
def _result_to_rows(result: Any, payload: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    if result is None:
        return [], []
    source_columns: list[str] = []
    if hasattr(result, "to_dict"):
        source_columns = [str(column) for column in getattr(result, "columns", [])]
        try:
            rows = result.to_dict(orient="records")
        except TypeError:
            converted = result.to_dict()
            rows = converted if isinstance(converted, list) else [converted]
    elif isinstance(result, list):
        rows = result
    elif isinstance(result, dict):
        rows = [result]
    else:
        row = _scalar_result_row(result, payload)
        return [row], list(row)
    rows = [_json_ready(row if isinstance(row, dict) else {"value": row}) for row in rows]
    if not rows and source_columns:
        return [], source_columns
    if len(rows) == 1 and len(rows[0]) == 1 and next(iter(rows[0].keys()), "") in {"result", "value"}:
        value = next(iter(rows[0].values()))
        row = _scalar_result_row(value, payload)
        return [row], list(row)
    columns = _ordered_columns(rows, source_columns)
    return rows, columns


# 함수 설명: `_normalize_blank_dimension_values()`는 집계 차원 컬럼의 null·공백을 빈 문자열로 맞춥니다.
def _normalize_blank_dimension_values(rows: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions = set(_dimension_output_columns(payload))
    if not dimensions:
        return rows
    for row in rows:
        for column in dimensions.intersection(row):
            value = row.get(column)
            if value is None or (isinstance(value, str) and not value.strip()):
                row[column] = ""
    return rows


# 함수 설명: `_normalize_missing_metric_values()`는 최종 표시용 metric 결측치만 0으로 맞춥니다.
def _normalize_missing_metric_values(rows: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not rows:
        return rows

    columns = _ordered_columns(rows)
    dimensions = {column.casefold() for column in _dimension_output_columns(payload)}
    metrics = _metric_output_columns(rows, payload, columns)
    for row in rows:
        for column in metrics:
            if column.casefold() in dimensions or column not in row:
                continue
            if _is_missing_display_value(row.get(column)):
                row[column] = 0
    return rows


# 함수 설명: `_metric_output_columns()`는 계약 우선, 이름·실제 숫자 값 차선으로 metric 컬럼을 보수적으로 선택합니다.
def _metric_output_columns(
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    available_columns: list[str],
) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    declared_metrics = _string_list(contract.get("metric_columns"))
    dimensions = {column.casefold() for column in _dimension_output_columns(payload)}

    if declared_metrics:
        declared = {column.casefold() for column in declared_metrics}
        return [
            column
            for column in available_columns
            if column.casefold() in declared and column.casefold() not in dimensions
        ]

    metrics: list[str] = []
    for column in available_columns:
        if column.casefold() in dimensions or _looks_like_identifier_or_dimension(column):
            continue
        if _looks_like_metric_name(column) or _has_numeric_result_value(rows, column):
            metrics.append(column)
    return metrics


# 함수 설명: `_is_missing_display_value()`는 표시 단계에서 0 또는 빈 문자열로 바꿀 null·blank 값을 판별합니다.
def _is_missing_display_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


# 함수 설명: `_has_numeric_result_value()`는 bool을 제외한 실제 숫자 값이 결과 컬럼에 하나라도 있는지 확인합니다.
def _has_numeric_result_value(rows: list[dict[str, Any]], column: str) -> bool:
    for row in rows:
        value = row.get(column)
        if _is_missing_display_value(value) or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return True
    return False


# 함수 설명: `_looks_like_metric_name()`은 생산·재공·수량·비율 등 지표 의미가 분명한 컬럼명을 판별합니다.
def _looks_like_metric_name(column: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9가-힣]+", "_", column.upper()).strip("_")
    tokens = {token for token in normalized.split("_") if token}
    metric_tokens = {
        "PRODUCTION",
        "WIP",
        "UPH",
        "QTY",
        "QUANTITY",
        "COUNT",
        "CNT",
        "RATE",
        "RATIO",
        "AMOUNT",
        "PLAN",
        "ACTUAL",
        "TARGET",
        "CAPACITY",
        "TAT",
        "SHORTFALL",
        "ACHIEVEMENT",
        "SUM",
        "AVG",
        "AVERAGE",
        "TOTAL",
        "SCORE",
        "VALUE",
    }
    korean_metric_terms = (
        "수량",
        "생산",
        "재공",
        "실적",
        "계획",
        "목표",
        "달성",
        "비율",
        "건수",
        "대수",
        "평균",
        "합계",
    )
    return bool(tokens & metric_tokens) or any(term in column for term in korean_metric_terms)


# 함수 설명: `_looks_like_identifier_or_dimension()`은 숫자여도 metric으로 취급하면 안 되는 ID·날짜·차원명을 판별합니다.
def _looks_like_identifier_or_dimension(column: str) -> bool:
    normalized = re.sub(r"[^A-Z0-9가-힣]+", "_", column.upper()).strip("_")
    tokens = {token for token in normalized.split("_") if token}
    identifier_suffixes = (
        "_ID",
        "_NO",
        "_CODE",
        "_CD",
        "_KEY",
        "_SEQ",
        "_DATE",
        "_DT",
        "_TIME",
        "_TM",
    )
    calendar_tokens = {"DATE", "DATETIME", "TIMESTAMP", "YEAR", "MONTH", "DAY", "WEEK", "QUARTER"}
    if normalized.endswith(identifier_suffixes) or tokens & calendar_tokens:
        return True
    return normalized in {
        "DEVICE",
        "TECH",
        "DEN",
        "DENSITY",
        "MODE",
        "ORG",
        "LEAD",
        "OPER",
        "OPER_NUM",
        "OPER_NAME",
        "SHIFT",
        "FAB",
        "FACTORY",
        "FAMILY",
        "STATUS",
        "PKG1",
        "PKG2",
        "PKG_TYPE1",
        "PKG_TYPE2",
        "EQP_MODEL",
        "EQUIP_MODEL",
    }


# 함수 설명: `_dimension_output_columns()`는 output contract와 group_by 단계에서 결과 차원 컬럼을 중복 없이 추출합니다.
def _dimension_output_columns(payload: dict[str, Any]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    columns = _string_list(contract.get("grain_columns"))
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for key in ("group_by", "group_by_columns", "groupby_columns", "group_columns"):
            for column in _string_list(step.get(key)):
                if column not in columns:
                    columns.append(column)
    return columns


# 함수 설명: `_missing_required_output_columns()`는 상세/엔터티 목록에서 source에 실제 존재하는 필수 컬럼의 누락만 반환합니다.
def _missing_required_output_columns(payload: dict[str, Any], result_columns: list[str]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result_mode = str(contract.get("result_mode") or "").strip().lower()
    if result_mode not in {"detail", "entity_list"}:
        return []
    required = _string_list(contract.get("required_columns"))
    if not required:
        return []
    source_columns = _available_source_columns(payload)
    missing: list[str] = []
    for required_column in required:
        equivalents = _equivalent_column_names(required_column, payload)
        # 잘못 등록된 catalog 컬럼 때문에 정상 결과 전체가 막히지 않도록,
        # source schema에서 확인되는 필수 컬럼만 실행 결과 계약으로 강제합니다.
        if source_columns and not _has_equivalent_column(source_columns, equivalents):
            continue
        if not _has_equivalent_column(result_columns, equivalents):
            missing.append(required_column)
    return missing


# 함수 설명: 집계 결과가 선언된 분석 grain을 잃고 전체 한 행으로 축약되지 않았는지 검증합니다.
def _missing_aggregate_grain_columns(payload: dict[str, Any], result_columns: list[str]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    if str(contract.get("result_mode") or "").strip().lower() != "aggregate":
        return []
    missing: list[str] = []
    for column in _string_list(contract.get("grain_columns")):
        if not _has_equivalent_column(result_columns, _equivalent_column_names(column, payload)):
            missing.append(column)
    return missing


# 함수 설명: 최종 결과가 구조화 ordering 계약의 컬럼·방향·limit를 실제로 지켰는지 검증합니다.
def _ordering_contract_error(
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    result_columns: list[str],
) -> str:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    ordering = contract.get("ordering") if isinstance(contract.get("ordering"), dict) else {}
    sort_by = str(ordering.get("sort_by") or "").strip()
    if not sort_by:
        return ""
    actual_column = _equivalent_result_column(sort_by, result_columns, payload)
    if not actual_column:
        return f"정렬 기준 컬럼이 결과에 없습니다: {sort_by}"
    try:
        limit = max(0, int(ordering.get("limit") or 0))
    except Exception:
        limit = 0
    if limit and len(rows) > limit:
        return f"정렬 결과 limit={limit}을 초과했습니다: {len(rows)}건"
    values: list[float] = []
    for row in rows:
        value = row.get(actual_column)
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            values.append(float(value))
        except Exception:
            return ""
    if len(values) < 2:
        return ""
    order = str(ordering.get("order") or "desc").strip().lower()
    pairs = zip(values, values[1:])
    valid = (
        all(left <= right for left, right in pairs)
        if order == "asc"
        else all(left >= right for left, right in pairs)
    )
    if not valid:
        return f"결과가 ordering 계약({sort_by} {order})대로 정렬되지 않았습니다."
    return ""


# 함수 설명: canonical/physical alias 후보 중 실제 결과에 존재하는 컬럼명을 반환합니다.
def _equivalent_result_column(
    column: str,
    result_columns: list[str],
    payload: dict[str, Any],
) -> str:
    available = {str(item).strip().casefold(): str(item) for item in result_columns if str(item).strip()}
    for candidate in _equivalent_column_names(column, payload):
        matched = available.get(str(candidate).strip().casefold())
        if matched:
            return matched
    return ""


# 함수 설명: 여러 결과 구간을 합친 표가 구분과 구간 내 순위를 잃지 않았는지 검증합니다.
def _missing_result_segment_columns(payload: dict[str, Any], result_columns: list[str]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    segments = contract.get("result_segments") if isinstance(contract.get("result_segments"), list) else []
    if len(segments) < 2:
        return []
    required = [str(contract.get("segment_column") or "RESULT_GROUP").strip()]
    rank_column = str(contract.get("rank_column") or "").strip()
    if rank_column:
        required.append(rank_column)
    available = {str(column).casefold() for column in result_columns}
    return [column for column in required if column and column.casefold() not in available]


# 함수 설명: `_available_source_columns()`는 전체 행 복사 없이 source schema와 일부 runtime row key에서 실제 컬럼을 수집합니다.
def _available_source_columns(payload: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    for values in _source_columns_by_alias(payload).values():
        for column in values:
            if column not in columns:
                columns.append(column)
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    for rows in runtime_sources.values():
        if not isinstance(rows, list):
            continue
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            for column in row:
                text = str(column)
                if text and text not in columns:
                    columns.append(text)
    return columns


# 함수 설명: `_equivalent_column_names()`는 현재 컬럼과 retrieval job에 명시된 카탈로그 alias만 같은 출력 컬럼 후보로 묶습니다.
def _equivalent_column_names(column: str, payload: dict[str, Any]) -> list[str]:
    candidates = [column] if str(column or "").strip() else []
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    candidate_keys = {item.casefold() for item in candidates}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for mapping_key in ("standard_column_aliases", "filter_mappings"):
            mapping = job.get(mapping_key) if isinstance(job.get(mapping_key), dict) else {}
            for standard, aliases in mapping.items():
                group = [str(standard), *_string_list(aliases)]
                group_keys = {item.casefold() for item in group}
                if candidate_keys.intersection(group_keys):
                    for item in group:
                        if item and item.casefold() not in {value.casefold() for value in candidates}:
                            candidates.append(item)
                    candidate_keys.update(group_keys)
    return candidates


# 함수 설명: `_has_equivalent_column()`는 대소문자를 무시해 실제 컬럼 목록에 alias 후보가 하나라도 있는지 확인합니다.
def _has_equivalent_column(columns: list[str], equivalents: list[str]) -> bool:
    available = {str(column).strip().casefold() for column in columns if str(column).strip()}
    return bool(available.intersection(str(item).strip().casefold() for item in equivalents if str(item).strip()))


# 함수 설명: `_ordered_columns()`는 원본 컬럼 순서를 우선 유지하고 새 결과 컬럼을 뒤에 추가합니다.
def _ordered_columns(rows: list[dict[str, Any]], preferred: list[str] | None = None) -> list[str]:
    columns: list[str] = []
    for column in preferred or []:
        text = str(column)
        if text and text not in columns and any(text in row for row in rows):
            columns.append(text)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for column in row:
            text = str(column)
            if text and text not in columns:
                columns.append(text)
    return columns


# 함수 설명: `_scalar_result_row()`는 스칼라 pandas 결과를 지표명과 조건 문맥이 포함된 한 행 결과로 만듭니다.
def _scalar_result_row(value: Any, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = _scalar_context_row(payload)
    metric_label = _scalar_metric_label(payload)
    row[metric_label] = _json_ready(value)
    if len(row) == 1:
        return {"지표": metric_label, "값": _json_ready(value)}
    return row


# 함수 설명: `_scalar_context_row()`는 첫 조회 작업에서 날짜·공정·제품 조건을 스칼라 결과 표시 문맥으로 추출합니다.
def _scalar_context_row(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    row: dict[str, Any] = {}
    if not jobs:
        return row
    job = jobs[0] if isinstance(jobs[0], dict) else {}
    params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
    filters = job.get("filters") if isinstance(job.get("filters"), dict) else {}
    date_value = params.get("DATE") or params.get("WORK_DATE") or params.get("date")
    if date_value not in (None, "", [], {}):
        row["기준일"] = _json_ready(date_value)
    for field, label in (
        ("OPER_NAME", "공정"),
        ("MCP_NO", "MCP NO"),
        ("DEVICE", "Device"),
    ):
        value = _filter_display_value(filters.get(field))
        if value not in (None, "", [], {}):
            row[label] = _json_ready(value)
    return row


# 함수 설명: `_filter_display_value()`는 필터의 단일/복수 값을 사람이 읽을 수 있는 짧은 표시값으로 변환합니다.
def _filter_display_value(condition: Any) -> Any:
    if not isinstance(condition, dict):
        return condition
    if "value" in condition:
        return condition.get("value")
    if "values" in condition:
        values = condition.get("values")
        if isinstance(values, list):
            return ", ".join(str(value) for value in values)
        return values
    return condition


# 함수 설명: `_scalar_metric_label()`는 출력 계약과 질문을 바탕으로 스칼라 결과의 지표명을 결정합니다.
def _scalar_metric_label(payload: dict[str, Any] | None = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    output_contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    metric_name = str(output_contract.get("metric") or output_contract.get("measure") or output_contract.get("value_name") or "").strip()
    if metric_name:
        return metric_name
    text = " ".join(
        str(item or "")
        for item in (
            request.get("question"),
            plan.get("analysis_kind"),
            output_contract.get("description"),
            output_contract.get("title"),
        )
    ).upper()
    if any(token in text for token in ("INPUT", "투입")):
        return "INPUT 수량"
    if any(token in text for token in ("WIP", "재공")):
        return "재공 수량"
    if any(token in text for token in ("PRODUCTION", "OUTPUT", "OUT", "생산", "실적")):
        return "생산 실적"
    return "결과값"


# 함수 설명: `_recorded_output()`는 pandas 단계 실행 결과를 행 수·컬럼·제한 preview가 포함된 trace 항목으로 만듭니다.
def _recorded_output(key: Any, value: Any, description: Any = "", role: Any = "") -> dict[str, Any]:
    rows, columns, row_count = _preview_rows_columns_count(value)
    return _json_ready(
        {
            "key": str(key or ""),
            "description": str(description or ""),
            "role": str(role or ""),
            "row_count": row_count,
            "columns": columns,
            "preview_rows": rows[:TRACE_PREVIEW_LIMIT],
        }
    )


# 함수 설명: `_recorded_function_case()`는 Function Case 실행 결과를 함수명·입력·행 수·preview가 포함된 trace 항목으로 만듭니다.
def _recorded_function_case(function_name: Any, input_text: Any, result_value: Any, description: Any = "") -> dict[str, Any]:
    rows, columns, row_count = _preview_rows_columns_count(result_value)
    return _json_ready(
        {
            "function_name": str(function_name or ""),
            "input_text": str(input_text or ""),
            "description": str(description or ""),
            "matched_count": row_count,
            "columns": columns,
            "preview_rows": rows[:TRACE_PREVIEW_LIMIT],
        }
    )


# 함수 설명: `_preview_rows_columns_count()`는 대형 실행 결과에서 제한된 preview rows·columns·전체 행 수만 계산합니다.
def _preview_rows_columns_count(value: Any) -> tuple[list[dict[str, Any]], list[str], int]:
    if hasattr(value, "head") and hasattr(value, "to_dict"):
        try:
            row_count = len(value)
        except Exception:
            row_count = 0
        try:
            preview_value = value.head(TRACE_PREVIEW_LIMIT)
            rows = preview_value.to_dict(orient="records")
            columns = [str(column) for column in getattr(value, "columns", [])]
            if not columns:
                columns = sorted({column for row in rows for column in row})
            return [_json_ready(row if isinstance(row, dict) else {"value": row}) for row in rows], columns, int(row_count)
        except Exception:
            pass
    rows, columns = _result_to_rows(value)
    return rows[:TRACE_PREVIEW_LIMIT], columns, len(rows)


# 함수 설명: `_json_ready()`는 datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.
def _json_ready(value: Any) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return None if value != value or value in (float("inf"), -float("inf")) else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_ready(item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): _json_ready(item_value) for key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item_value) for item_value in value]
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


# 함수 설명: `_analysis_error()`는 실행 예외를 type·message·짧은 traceback이 포함된 공개 가능한 오류로 정리합니다.
def _analysis_error(
    payload: dict[str, Any],
    error_type: str,
    message: str,
    code: str,
    tb: str = "",
    llm_code: str = "",
    filter_preamble: str = "",
    filter_plan: list[dict[str, Any]] | None = None,
    safe_imports: dict[str, Any] | None = None,
    row_match_plan: list[dict[str, Any]] | None = None,
    row_match_preamble: str = "",
    response_parse: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_import_info = safe_imports if isinstance(safe_imports, dict) else {}
    helper_trace = _runtime_helper_trace(code)
    payload["analysis"] = {
        "status": "error",
        "row_count": 0,
        "columns": [],
        "error": {"type": error_type, "message": message},
        "errors": [message],
        "repairable_errors": [message],
        "used_helpers": helper_trace["used_helpers"],
        "step_outputs": [],
        "function_case_results": [],
    }
    payload.setdefault("trace", {}).setdefault("errors", []).append({"type": error_type, "message": message})
    payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_execution"] = {
        "stage": "17_pandas_code_executor",
        "status": "error",
        "generated_code": code,
        "llm_generated_code": llm_code or code,
        "llm_response_parse": deepcopy(response_parse) if isinstance(response_parse, dict) else {},
        "safe_import_normalization": _safe_import_trace(safe_import_info),
        "row_match_preamble": row_match_preamble,
        "pandas_filter_preamble": filter_preamble,
        "pandas_filter_plan": filter_plan or [],
        "row_match_plan": row_match_plan or [],
        "used_helpers": helper_trace["used_helpers"],
        "helper_sources": helper_trace["helper_sources"],
        "error": {"type": error_type, "message": message, "traceback_summary": tb[:1000]},
    }
    return payload


# 함수 설명: `_execution_blocked()`는 필수 조회 실패 gate가 pandas와 repair 실행을 금지했는지 확인합니다.
def _execution_blocked(payload: dict[str, Any]) -> bool:
    gate = payload.get("execution_gate") if isinstance(payload.get("execution_gate"), dict) else {}
    return str(gate.get("status") or "").strip().lower() == "blocked"


# 함수 설명: `_blocked_execution_payload()`는 upstream 모델 응답을 사용하지 않고 pandas·repair 실행 없이 기존 조회 오류를 유지합니다.
def _blocked_execution_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("analysis", {}).setdefault("status", "error")
    payload.setdefault("data", {"columns": [], "rows": [], "row_count": 0, "data_ref": ""})
    payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_execution"] = {
        "stage": "17_pandas_code_executor",
        "status": "skipped",
        "reason": "required_source_retrieval_failed",
        "model_response_policy": "ignored",
        "code_execution_attempted": False,
        "repair_attempted": False,
    }
    payload["trace"]["inspection"]["pandas_repair"] = {
        "stage": "17_pandas_code_executor",
        "initial_status": "skipped",
        "max_attempts": 0,
        "attempt": 0,
        "attempted": False,
        "llm_called": False,
        "selected": "blocked",
        "reason": "필수 조회 실패로 pandas 및 repair 실행을 생략했습니다.",
    }
    return payload


# 함수 설명: `_runtime_helper_trace()`는 생성 코드가 실제 호출한 inline helper와 원본 정보를 실행 trace로 정리합니다.
def _runtime_helper_trace(code: str) -> dict[str, Any]:
    helper_names = _used_inline_helpers(code)
    return {
        "used_helpers": helper_names,
        "helper_sources": [],
        "effective_code_with_helpers": str(code or "").strip(),
    }


# 함수 설명: `_used_inline_helpers()`는 생성 코드 AST에서 실제 호출된 helper 함수 이름만 찾아냅니다.
def _used_inline_helpers(code: str) -> list[str]:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    instrumentation_functions = {"record_step", "record_function_case_result"}
    top_level_functions = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_") and node.name not in instrumentation_functions
    ]
    used: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in top_level_functions:
            if node.func.id not in used:
                used.append(node.func.id)
    return used


# 함수 설명: `_as_list()`는 단일 값과 여러 값 입력을 모두 같은 list 형태로 맞춰 반복 처리를 단순화합니다.
def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, "", {}, []):
        return []
    return [value]


# 함수 설명: 14번 노드에서 표준 컬럼 단일화가 완료된 source alias를 trace에서 확인합니다.
def _standardized_source_aliases(payload: dict[str, Any]) -> set[str]:
    """Return source aliases normalized to canonical columns by node 14."""
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    standardization = (
        inspection.get("source_column_standardization")
        if isinstance(inspection.get("source_column_standardization"), dict)
        else {}
    )
    return {
        str(item.get("source_alias") or "").strip()
        for item in standardization.get("sources", [])
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() in {"applied", "not_needed"}
        and str(item.get("source_alias") or "").strip()
    }


# 함수 설명: `_pandas_filter_plan()`는 조회 작업의 filter를 source alias별 결정론적 pandas 필터 계획으로 바꿉니다.
def _pandas_filter_plan(payload: dict[str, Any]) -> list[dict[str, Any]]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    standardized_aliases = _standardized_source_aliases(payload)
    jobs_by_alias = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip(): job
        for job in jobs
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
    }
    filter_plan_by_alias: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if not alias:
            continue
        conditions = _filter_conditions(job.get("filters"))
        if alias in standardized_aliases:
            conditions = _canonicalize_filter_condition_fields(conditions, job)
        if conditions:
            item = {
                "source_alias": alias,
                "dataset_key": job.get("dataset_key", ""),
                "conditions": conditions,
                "columns_standardized": alias in standardized_aliases,
            }
            if alias not in standardized_aliases:
                for mapping_key in ("filter_mappings", "standard_column_aliases"):
                    mapping = job.get(mapping_key)
                    if isinstance(mapping, dict) and mapping:
                        item[mapping_key] = deepcopy(mapping)
            filter_plan_by_alias[alias] = item
    condition_resolution = plan.get("condition_resolution") if isinstance(plan.get("condition_resolution"), dict) else {}
    effective_filters = (
        condition_resolution.get("effective_filters")
        if isinstance(condition_resolution.get("effective_filters"), dict)
        else {}
    )
    for raw_alias, raw_item in effective_filters.items():
        alias = str(raw_alias or "").strip()
        if not alias or not isinstance(raw_item, dict):
            continue
        conditions = _filter_conditions(raw_item.get("filters"))
        if alias in standardized_aliases:
            conditions = _canonicalize_filter_condition_fields(
                conditions,
                jobs_by_alias.get(alias),
                raw_item,
            )
        if not conditions:
            continue
        item = filter_plan_by_alias.setdefault(
            alias,
            {
                "source_alias": alias,
                "dataset_key": raw_item.get("dataset_key", ""),
                "conditions": [],
                "columns_standardized": alias in standardized_aliases,
            },
        )
        existing = {
            json.dumps(condition, ensure_ascii=False, sort_keys=True, default=str)
            for condition in item.get("conditions", [])
            if isinstance(condition, dict)
        }
        for condition in conditions:
            marker = json.dumps(condition, ensure_ascii=False, sort_keys=True, default=str)
            if marker not in existing:
                item.setdefault("conditions", []).append(condition)
                existing.add(marker)
        if alias not in standardized_aliases:
            for mapping_key in ("filter_mappings", "standard_column_aliases"):
                mapping = raw_item.get(mapping_key)
                if isinstance(mapping, dict) and mapping:
                    item[mapping_key] = deepcopy(mapping)
    return list(filter_plan_by_alias.values())


# 함수 설명: 표준화된 runtime source에 적용할 filter field를 hydrated catalog의 canonical key로 통일합니다.
def _canonicalize_filter_condition_fields(
    conditions: list[dict[str, Any]],
    *contracts: Any,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in conditions:
        if not isinstance(raw, dict):
            continue
        condition = deepcopy(raw)
        field = str(condition.get("field") or "").strip()
        if field:
            condition["field"] = _canonical_filter_field(field, *contracts)
        marker = json.dumps(condition, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(condition)
    return result


# 함수 설명: filter_mappings를 우선하고 standard_column_aliases를 보조로 사용해 물리 field의 유일한 표준 key를 찾습니다.
def _canonical_filter_field(field: str, *contracts: Any) -> str:
    target = str(field or "").strip()
    if not target:
        return ""
    target_key = target.casefold()
    for mapping_name in ("filter_mappings", "standard_column_aliases"):
        matches: list[str] = []
        exact: list[str] = []
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            mapping = contract.get(mapping_name)
            if not isinstance(mapping, dict):
                continue
            for raw_standard, raw_aliases in mapping.items():
                standard = str(raw_standard or "").strip()
                group = [standard, *_string_list(raw_aliases)]
                if target_key not in {item.casefold() for item in group if item}:
                    continue
                if standard and standard.casefold() == target_key and standard not in exact:
                    exact.append(standard)
                if standard and standard not in matches:
                    matches.append(standard)
        if len({item.casefold() for item in exact}) == 1:
            return exact[0]
        if len({item.casefold() for item in matches}) == 1:
            return matches[0]
    return target


# 함수 설명: `_pandas_row_match_plan()`은 pandas 실행 계획의 reference 행 단위 조건을 source alias별 결정론적 매칭 계획으로 바꿉니다.
def _pandas_row_match_plan(payload: dict[str, Any]) -> list[dict[str, Any]]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    standardized_aliases = _standardized_source_aliases(payload)
    mappings_by_alias: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if not alias:
            continue
        mappings_by_alias[alias] = {} if alias in standardized_aliases else {
            **(job.get("standard_column_aliases") if isinstance(job.get("standard_column_aliases"), dict) else {}),
            **(job.get("filter_mappings") if isinstance(job.get("filter_mappings"), dict) else {}),
        }

    result: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if str(step.get("operation") or "").strip().lower() != "apply_row_match_groups":
            continue
        source_alias = str(step.get("source_alias") or "").strip()
        result.append(
            {
                "step_index": index,
                "source_alias": source_alias,
                "reference_source_alias": str(step.get("reference_source_alias") or "").strip(),
                "match_columns": _string_list(step.get("match_columns")),
                "blank_policy": "normalize_blank",
                "column_mappings": deepcopy(mappings_by_alias.get(source_alias, {})),
            }
        )
    return result


# 함수 설명: `_pandas_row_match_preamble()`은 행 내부 AND·행 사이 OR 매칭을 생성 코드에 포함할 결정론적 pandas 전처리 코드로 만듭니다.
def _pandas_row_match_preamble(row_match_plan: list[dict[str, Any]]) -> str:
    if not row_match_plan:
        return ""

    lines = [
        "_row_match_execution = []",
        f"_row_match_blank_texts = {sorted(BLANK_MATCH_TEXTS)!r}",
        "def _normalize_row_match_value(value):",
        "    if value is None:",
        "        return ''",
        "    text = str(value).strip()",
        "    if text.casefold() in _row_match_blank_texts:",
        "        return ''",
        "    signless = text[1:] if text.startswith('-') else text",
        "    number_parts = signless.split('.', 1)",
        "    if len(number_parts) == 2 and number_parts[0].isdigit() and number_parts[1] and set(number_parts[1]) == {'0'}:",
        "        return text.split('.', 1)[0]",
        "    return text",
        "",
        "def _find_row_match_column(frame, candidates):",
        "    actual_by_casefold = {str(column).casefold(): str(column) for column in frame.columns}",
        "    for candidate in candidates:",
        "        actual = actual_by_casefold.get(str(candidate).casefold())",
        "        if actual:",
        "            return actual",
        "    return ''",
        "",
    ]

    for plan_index, item in enumerate(row_match_plan, start=1):
        source_alias = str(item.get("source_alias") or "").strip()
        reference_alias = str(item.get("reference_source_alias") or "").strip()
        match_columns = _string_list(item.get("match_columns"))
        mappings = item.get("column_mappings") if isinstance(item.get("column_mappings"), dict) else {}
        if not source_alias or not reference_alias:
            lines.append(
                "raise Exception("
                + repr("apply_row_match_groups에는 source_alias와 reference_source_alias가 모두 필요합니다.")
                + ")"
            )
            continue
        if source_alias == reference_alias:
            lines.append(
                "raise Exception("
                + repr("apply_row_match_groups의 source_alias와 reference_source_alias는 서로 달라야 합니다.")
                + ")"
            )
            continue
        minimum_columns = 1 if reference_alias == "previous_result" else 2
        if len(match_columns) < minimum_columns:
            lines.append(
                "raise Exception("
                + repr(
                    "apply_row_match_groups에는 previous_result 기준 1개 이상, "
                    "일반 reference 기준 2개 이상의 match_columns가 필요합니다."
                )
                + ")"
            )
            continue

        suffix = f"{plan_index}_{_safe_name(source_alias)}"
        target_var = f"_row_match_target_{suffix}"
        reference_var = f"_row_match_reference_{suffix}"
        pairs_var = f"_row_match_pairs_{suffix}"
        used_pairs_var = f"_row_match_used_pairs_{suffix}"
        groups_var = f"_row_match_groups_{suffix}"
        keys_var = f"_row_match_keys_{suffix}"
        mask_var = f"_row_match_mask_{suffix}"
        before_count_var = f"_row_match_before_count_{suffix}"

        lines.extend(
            [
                f"{target_var} = sources.get({source_alias!r})",
                f"{reference_var} = sources.get({reference_alias!r})",
                f"if {target_var} is None:",
                f"    raise Exception({f'row match target source를 찾을 수 없습니다: {source_alias}'!r})",
                f"if {reference_var} is None:",
                f"    raise Exception({f'row match reference source를 찾을 수 없습니다: {reference_alias}'!r})",
                f"{target_var} = {target_var}.copy()",
                f"{pairs_var} = []",
                f"{used_pairs_var} = set()",
            ]
        )
        for column_index, canonical_column in enumerate(match_columns, start=1):
            candidates = _mapped_field_candidates(canonical_column, mappings)
            source_column_var = f"_row_match_source_column_{plan_index}_{column_index}"
            reference_column_var = f"_row_match_reference_column_{plan_index}_{column_index}"
            pair_key_var = f"_row_match_pair_key_{plan_index}_{column_index}"
            missing_message = (
                "row match 컬럼을 두 source에서 모두 찾을 수 없습니다: "
                f"{canonical_column} (target={source_alias}, reference={reference_alias})"
            )
            lines.extend(
                [
                    f"{source_column_var} = _find_row_match_column({target_var}, {candidates!r})",
                    f"{reference_column_var} = _find_row_match_column({reference_var}, {candidates!r})",
                    f"if not {source_column_var} or not {reference_column_var}:",
                    f"    raise Exception({missing_message!r})",
                    f"{pair_key_var} = ({source_column_var}.casefold(), {reference_column_var}.casefold())",
                    f"if {pair_key_var} not in {used_pairs_var}:",
                    f"    {used_pairs_var}.add({pair_key_var})",
                    f"    {pairs_var}.append(({canonical_column!r}, {source_column_var}, {reference_column_var}))",
                ]
            )
        lines.extend(
            [
                f"if len({pairs_var}) < {minimum_columns}:",
                (
                    "    raise Exception('apply_row_match_groups에서 중복을 제외한 실제 매칭 컬럼이 "
                    + f"{minimum_columns}개 미만입니다.')"
                ),
                f"{groups_var} = {{",
                "    tuple(_normalize_row_match_value(row.get(pair[2])) for pair in " + pairs_var + ")",
                f"    for row in {reference_var}.to_dict(orient='records')",
                "}",
                f"{before_count_var} = len({target_var})",
                f"if {groups_var}:",
                f"    {keys_var} = [",
                "        tuple(_normalize_row_match_value(row.get(pair[1])) for pair in " + pairs_var + ")",
                f"        for row in {target_var}.to_dict(orient='records')",
                "    ]",
                f"    {mask_var} = pd.Series(",
                f"        [key in {groups_var} for key in {keys_var}],",
                f"        index={target_var}.index,",
                "        dtype=bool,",
                "    )",
                f"    {target_var} = {target_var}[{mask_var}].copy()",
                "else:",
                f"    {target_var} = {target_var}.iloc[0:0].copy()",
                "sources = dict(sources)",
                f"sources[{source_alias!r}] = {target_var}",
                "_row_match_execution.append({",
                f"    'source_alias': {source_alias!r},",
                f"    'reference_source_alias': {reference_alias!r},",
                f"    'match_columns': {match_columns!r},",
                "    'resolved_columns': [",
                "        {",
                "            'canonical_column': pair[0],",
                "            'source_column': pair[1],",
                "            'reference_column': pair[2],",
                "        }",
                f"        for pair in {pairs_var}",
                "    ],",
                f"    'reference_row_count': len({reference_var}),",
                f"    'unique_condition_group_count': len({groups_var}),",
                f"    'source_row_count_before': {before_count_var},",
                f"    'source_row_count_after': len({target_var}),",
                "    'blank_policy': 'normalize_blank',",
                "})",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


# 함수 설명: `_is_blank_match_value()`는 실제 missing 값과 문자열 missing 표기를 행 조건의 동일한 blank 값으로 판정합니다.
def _is_blank_match_value(value: Any) -> bool:
    if value is None:
        return True
    try:
        text = str(value).strip().casefold()
    except Exception:
        return False
    return text in BLANK_MATCH_TEXTS


# 함수 설명: `_unsupported_filter_operators()`는 executor가 구현하지 않은 필터를 실행 전 찾아 무필터 통과를 차단합니다.
def _unsupported_filter_operators(filter_plan: list[dict[str, Any]]) -> list[str]:
    unsupported: list[str] = []
    for item in filter_plan:
        conditions = item.get("conditions") if isinstance(item.get("conditions"), list) else []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            operator = _normalize_filter_operator(condition.get("operator") or "eq")
            if operator not in SUPPORTED_FILTER_OPERATORS and operator not in unsupported:
                unsupported.append(operator)
            if operator not in {"or", "any"}:
                continue
            values = condition.get("values") if isinstance(condition.get("values"), list) else []
            for nested in values:
                if not isinstance(nested, dict):
                    continue
                nested_operator = _normalize_filter_operator(nested.get("operator") or nested.get("op") or "eq")
                if nested_operator not in SUPPORTED_COMPOUND_FILTER_OPERATORS and nested_operator not in unsupported:
                    unsupported.append(nested_operator)
    return unsupported


# 함수 설명: `_with_pandas_execution_preambles()`는 row match·일반 filter·LLM 분석 코드를 실제 실행 순서대로 하나의 코드로 결합합니다.
def _with_pandas_execution_preambles(
    code: Any,
    row_match_preamble: str,
    filter_preamble: str,
) -> str:
    segments = [
        str(segment or "").strip()
        for segment in (row_match_preamble, filter_preamble, code)
        if str(segment or "").strip()
    ]
    return "\n\n".join(segments)


# 함수 설명: `_with_pandas_filter_preamble()`는 기존 호출 호환을 위해 일반 filter와 생성 코드를 같은 결합기로 연결합니다.
def _with_pandas_filter_preamble(code: Any, filter_plan: list[dict[str, Any]]) -> str:
    return _with_pandas_execution_preambles(
        code,
        "",
        _pandas_filter_preamble(filter_plan),
    )


# 함수 설명: `_pandas_filter_preamble()`는 의도 계획의 필터 조건을 생성 코드보다 먼저 적용할 안전한 pandas 전처리 코드로 만듭니다.
def _pandas_filter_preamble(filter_plan: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if _has_date_filter_condition(filter_plan):
        lines.extend(
            [
                "def _normalize_date_filter_value(value):",
                "    text = str(value if value is not None else '').strip()",
                "    if text.casefold() in ('', 'null', 'none', 'nan', 'nat', '<na>', 'empty'):",
                "        return ''",
                "    if len(text) >= 8 and text[:8].isdigit():",
                "        return text[:8]",
                "    normalized = text.replace('년', '-').replace('월', '-').replace('일', '')",
                "    normalized = normalized.replace('/', '-').replace('.', '-')",
                "    parts = [part.strip() for part in normalized.split('-') if part.strip()]",
                "    if len(parts) >= 3 and len(parts[0]) == 4:",
                "        try:",
                "            return f'{int(parts[0]):04d}{int(parts[1]):02d}{int(parts[2][:2]):02d}'",
                "        except Exception:",
                "            return text",
                "    return text",
                "",
            ]
        )
    for job_index, item in enumerate(filter_plan, start=1):
        alias = str(item.get("source_alias") or "").strip()
        conditions = item.get("conditions") if isinstance(item.get("conditions"), list) else []
        column_mappings = {} if item.get("columns_standardized") is True else {
            **(item.get("standard_column_aliases") if isinstance(item.get("standard_column_aliases"), dict) else {}),
            **(item.get("filter_mappings") if isinstance(item.get("filter_mappings"), dict) else {}),
        }
        if not alias or not conditions:
            continue
        df_var = f"_filtered_source_{job_index}_{_safe_name(alias)}"
        lines.append(f"{df_var} = sources.get({alias!r})")
        lines.append(f"if {df_var} is not None:")
        lines.append("    sources = dict(sources)")
        lines.append(f"    {df_var} = {df_var}.copy()")
        for condition_index, condition in enumerate(conditions, start=1):
            lines.extend(_condition_code(df_var, job_index, condition_index, condition, column_mappings))
        lines.append(f"    sources[{alias!r}] = {df_var}")
    return "\n".join(lines)


# 함수 설명: `_condition_code()`는 단일 필터 조건을 pandas boolean mask 표현식으로 변환합니다.
def _condition_code(
    df_var: str,
    job_index: int,
    condition_index: int,
    condition: dict[str, Any],
    column_mappings: dict[str, Any] | None = None,
) -> list[str]:
    field = str(condition.get("field") or "").strip()
    operator = _normalize_filter_operator(condition.get("operator") or "eq")
    values = condition.get("values") if isinstance(condition.get("values"), list) else []
    if not field or (not values and operator not in VALUELESS_FILTER_OPERATORS):
        return []
    col_var = f"_filter_col_{job_index}_{condition_index}"
    values_var = f"_filter_values_{job_index}_{condition_index}"
    mask_var = f"_filter_mask_{job_index}_{condition_index}"
    date_series_var = f"_filter_date_series_{job_index}_{condition_index}"
    candidates = _mapped_field_candidates(field, column_mappings)
    missing_message = (
        "pandas filter 컬럼을 카탈로그 매핑 또는 동일 이름으로 찾을 수 없습니다: "
        f"{field} (candidates={candidates})"
    )
    lines = [
        f"    {col_var} = {_column_choice_expression(df_var, candidates)}",
        f"    {values_var} = {values!r}",
        f"    if not {col_var}:",
        f"        raise Exception({missing_message!r})",
        f"    if {col_var}:",
    ]
    if operator in {"eq", "in"}:
        if _is_date_filter_field(field):
            lines.append(f"        {values_var} = [_normalize_date_filter_value(value) for value in {values_var}]")
            lines.append(f"        {date_series_var} = {df_var}[{col_var}].map(_normalize_date_filter_value)")
            lines.append(f"        {df_var} = {df_var}[{date_series_var}.isin({values_var})]")
        elif _has_blank_match_values(values):
            expression = _blank_aware_membership_expression(f"{df_var}[{col_var}]", values)
            lines.append(f"        {mask_var} = {expression}")
            lines.append(f"        {df_var} = {df_var}[{mask_var}]")
        else:
            lines.append(f"        {df_var} = {df_var}[{df_var}[{col_var}].isin({values_var})]")
    elif operator in {"ne", "not_in"}:
        if _is_date_filter_field(field):
            lines.append(f"        {values_var} = [_normalize_date_filter_value(value) for value in {values_var}]")
            lines.append(f"        {date_series_var} = {df_var}[{col_var}].map(_normalize_date_filter_value)")
            lines.append(f"        {df_var} = {df_var}[~{date_series_var}.isin({values_var})]")
        elif _has_blank_match_values(values):
            expression = _blank_aware_membership_expression(f"{df_var}[{col_var}]", values)
            lines.append(f"        {mask_var} = {expression}")
            lines.append(f"        {df_var} = {df_var}[~{mask_var}]")
        else:
            lines.append(f"        {df_var} = {df_var}[~{df_var}[{col_var}].isin({values_var})]")
    elif operator in {"gt", "ge", "lt", "le"}:
        numeric_series_var = f"_filter_numeric_series_{job_index}_{condition_index}"
        numeric_value_var = f"_filter_numeric_value_{job_index}_{condition_index}"
        lines.append(f"        {numeric_series_var} = pd.to_numeric({df_var}[{col_var}], errors='coerce')")
        lines.append(f"        {numeric_value_var} = pd.to_numeric(pd.Series([{values_var}[0]]), errors='coerce').iloc[0]")
        lines.append(f"        if pd.notna({numeric_value_var}):")
        comparison = {
            "gt": ">",
            "ge": ">=",
            "lt": "<",
            "le": "<=",
        }[operator]
        lines.append(f"            {df_var} = {df_var}[{numeric_series_var} {comparison} {numeric_value_var}]")
        lines.append("        else:")
        lines.append(f"            {df_var} = {df_var}.iloc[0:0]")
    elif operator in {"contains", "like"}:
        lines.append(f"        {mask_var} = {df_var}[{col_var}].astype(str).str.contains(str({values_var}[0]), case=False, na=False, regex=False)")
        lines.append(f"        for _filter_value in {values_var}[1:]:")
        lines.append(f"            {mask_var} = {mask_var} | {df_var}[{col_var}].astype(str).str.contains(str(_filter_value), case=False, na=False, regex=False)")
        lines.append(f"        {df_var} = {df_var}[{mask_var}]")
    elif operator in {"starts_with", "startswith", "prefix"}:
        lines.append(f"        {mask_var} = {df_var}[{col_var}].astype(str).str.startswith(str({values_var}[0]), na=False)")
        lines.append(f"        for _filter_value in {values_var}[1:]:")
        lines.append(f"            {mask_var} = {mask_var} | {df_var}[{col_var}].astype(str).str.startswith(str(_filter_value), na=False)")
        lines.append(f"        {df_var} = {df_var}[{mask_var}]")
    elif operator in {"ends_with", "endswith", "suffix"}:
        lines.append(f"        {mask_var} = {df_var}[{col_var}].astype(str).str.endswith(str({values_var}[0]), na=False)")
        lines.append(f"        for _filter_value in {values_var}[1:]:")
        lines.append(f"            {mask_var} = {mask_var} | {df_var}[{col_var}].astype(str).str.endswith(str(_filter_value), na=False)")
        lines.append(f"        {df_var} = {df_var}[{mask_var}]")
    elif operator in VALUELESS_FILTER_OPERATORS:
        lines.extend(_null_empty_condition_lines(df_var, col_var, mask_var, operator))
    elif operator in {"or", "any"} and _has_operator_dict(values):
        lines.extend(_compound_condition_lines(df_var, col_var, mask_var, values))
    else:
        lines.append("        pass")
    return lines


# 함수 설명: `_normalize_filter_operator()`는 필터 연산자의 여러 alias를 executor가 지원하는 표준 연산자로 바꿉니다.
def _normalize_filter_operator(value: Any) -> str:
    text = re.sub(r"[\s-]+", "_", str(value or "eq").strip()).lower()
    aliases = {
        "=": "eq",
        "==": "eq",
        "!=": "ne",
        ">": "gt",
        ">=": "ge",
        "gte": "ge",
        "greater_than": "gt",
        "greater_than_or_equal": "ge",
        "<": "lt",
        "<=": "le",
        "lte": "le",
        "less_than": "lt",
        "less_than_or_equal": "le",
        "not in": "not_in",
        "notin": "not_in",
        "starts": "starts_with",
        "startwith": "starts_with",
        "startswith": "starts_with",
        "starts_with_any": "starts_with",
        "prefix": "starts_with",
        "endswith": "ends_with",
        "suffix": "ends_with",
        "isnull": "is_null",
        "is_null": "is_null",
        "null": "is_null",
        "none": "is_null",
        "isempty": "is_empty",
        "is_empty": "is_empty",
        "empty": "is_empty",
        "blank": "is_empty",
        "null_or_empty": "null_or_empty",
        "is_null_or_empty": "null_or_empty",
        "notnull": "not_null",
        "not_null": "not_null",
        "notempty": "not_empty",
        "not_empty": "not_empty",
        "notblank": "not_blank",
        "not_blank": "not_blank",
        "is_not_blank": "not_blank",
        "is_not_null_or_empty": "not_blank",
        "is_not_null_and_not_empty": "not_blank",
        "not_null_or_empty": "not_blank",
        "not_null_and_not_empty": "not_blank",
        "any": "any",
        "or": "or",
    }
    return aliases.get(text, text)


# 함수 설명: `_null_empty_condition_lines()`는 null·not null·empty·not empty 조건에 해당하는 pandas mask 코드를 만듭니다.
def _null_empty_condition_lines(df_var: str, col_var: str, mask_var: str, operator: str) -> list[str]:
    series = f"{df_var}[{col_var}]"
    if operator == "is_null":
        return [f"        {df_var} = {df_var}[{series}.isna()]"]
    if operator == "is_empty":
        return [f"        {df_var} = {df_var}[{series}.astype(str).str.strip().eq('')]"]
    if operator == "null_or_empty":
        return [f"        {mask_var} = {_blank_aware_membership_expression(series, [''])}", f"        {df_var} = {df_var}[{mask_var}]"]
    if operator == "not_null":
        return [f"        {df_var} = {df_var}[{series}.notna()]"]
    if operator == "not_empty":
        return [f"        {df_var} = {df_var}[~{series}.astype(str).str.strip().eq('')]"]
    if operator == "not_blank":
        expression = _blank_aware_membership_expression(series, [""])
        return [f"        {mask_var} = {expression}", f"        {df_var} = {df_var}[~{mask_var}]"]
    return ["        pass"]


# 함수 설명: `_has_operator_dict()`는 복합 필터 값이 operator를 가진 조건 dict인지 판정합니다.
def _has_operator_dict(values: list[Any]) -> bool:
    return any(isinstance(item, dict) and (item.get("operator") or item.get("op")) for item in values)


# 함수 설명: `_compound_condition_lines()`는 AND/OR 복합 필터 구조를 pandas mask 코드 여러 줄로 변환합니다.
def _compound_condition_lines(df_var: str, col_var: str, mask_var: str, values: list[Any]) -> list[str]:
    series = f"{df_var}[{col_var}]"
    lines = [f"        {mask_var} = False"]
    for item in values:
        if not isinstance(item, dict):
            continue
        op = _normalize_filter_operator(item.get("operator") or item.get("op") or "eq")
        raw_values = _as_values(
            item.get("values", item.get("value", [])),
            preserve_blank=op in {"eq", "in", "ne", "not_in"},
        )
        if op == "is_null":
            lines.append(f"        {mask_var} = {mask_var} | {series}.isna()")
        elif op == "is_empty":
            lines.append(f"        {mask_var} = {mask_var} | {series}.astype(str).str.strip().eq('')")
        elif op == "null_or_empty":
            lines.append(f"        {mask_var} = {mask_var} | ({_blank_aware_membership_expression(series, [''])})")
        elif op in {"eq", "in"} and raw_values:
            if _has_blank_match_values(raw_values):
                lines.append(f"        {mask_var} = {mask_var} | ({_blank_aware_membership_expression(series, raw_values)})")
            else:
                lines.append(f"        {mask_var} = {mask_var} | {series}.isin({raw_values!r})")
        elif op == "starts_with" and raw_values:
            lines.append(f"        {mask_var} = {mask_var} | {series}.astype(str).str.startswith(str({raw_values[0]!r}), na=False)")
            for raw_value in raw_values[1:]:
                lines.append(f"        {mask_var} = {mask_var} | {series}.astype(str).str.startswith(str({raw_value!r}), na=False)")
        elif op in {"contains", "like"} and raw_values:
            lines.append(f"        {mask_var} = {mask_var} | {series}.astype(str).str.contains(str({raw_values[0]!r}), case=False, na=False, regex=False)")
            for raw_value in raw_values[1:]:
                lines.append(f"        {mask_var} = {mask_var} | {series}.astype(str).str.contains(str({raw_value!r}), case=False, na=False, regex=False)")
    lines.append(f"        {df_var} = {df_var}[{mask_var}]")
    return lines


# 함수 설명: `_column_choice_expression()`는 컬럼 alias 후보 중 실제 DataFrame에 존재하는 첫 컬럼을 선택하는 코드를 만듭니다.
def _column_choice_expression(df_var: str, candidates: list[str]) -> str:
    expression = "''"
    for candidate in reversed(candidates):
        expression = f"{candidate!r} if {candidate!r} in {df_var}.columns else ({expression})"
    return expression


# 함수 설명: `_filter_conditions()`는 dict/list 형태의 필터를 field·operator·values 조건 목록으로 정규화합니다.
def _filter_conditions(filters: Any) -> list[dict[str, Any]]:
    if isinstance(filters, list):
        items = [(condition.get("field") or condition.get("column"), condition) for condition in filters if isinstance(condition, dict)]
    elif isinstance(filters, dict):
        items = list(filters.items())
    else:
        return []
    result: list[dict[str, Any]] = []
    for field, condition in items:
        field_text = str(field or "").strip()
        if not field_text:
            continue
        if isinstance(condition, dict):
            operator = condition.get("operator", condition.get("op", "eq"))
            values = condition.get("values", condition.get("value", []))
        elif isinstance(condition, list) and _has_operator_dict(condition):
            operator = "or"
            values = condition
        else:
            operator = "eq"
            values = condition
        normalized_operator = _normalize_filter_operator(operator or "eq")
        normalized_values = _as_values(
            values,
            preserve_blank=normalized_operator in {"eq", "in", "ne", "not_in"},
        )
        if _is_date_filter_field(field_text):
            normalized_values = [_normalize_date_identifier(value) for value in normalized_values]
        if (
            normalized_values
            or normalized_operator in VALUELESS_FILTER_OPERATORS
            or normalized_operator not in SUPPORTED_FILTER_OPERATORS
        ):
            result.append({"field": field_text, "operator": normalized_operator, "values": normalized_values})
    return result


# 함수 설명: `_has_date_filter_condition()`은 날짜 비교 helper가 필요한 필터가 있는지 확인합니다.
def _has_date_filter_condition(filter_plan: list[dict[str, Any]]) -> bool:
    for item in filter_plan:
        conditions = item.get("conditions") if isinstance(item.get("conditions"), list) else []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            operator = _normalize_filter_operator(condition.get("operator") or "eq")
            if _is_date_filter_field(condition.get("field")) and operator in {"eq", "in", "ne", "not_in"}:
                return True
    return False


# 함수 설명: `_normalize_date_identifier()`는 날짜 필터값을 YYYYMMDD로 통일하고 해석 불가 값은 보존합니다.
def _normalize_date_identifier(value: Any) -> Any:
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


# 함수 설명: `_is_date_filter_field()`는 DATE 또는 DT 토큰으로 선언된 날짜 필터 field를 판별합니다.
def _is_date_filter_field(value: Any) -> bool:
    key = re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")
    return bool(key and re.search(r"(?:^|_)(?:DATE|DT)(?:$|_)", key))


# 함수 설명: `_as_values()`는 단일 필터 값과 목록 값을 같은 값 목록으로 맞추고 equality 계열에서는 blank 값을 조건으로 보존합니다.
def _as_values(value: Any, preserve_blank: bool = False) -> list[Any]:
    if isinstance(value, list):
        return list(value) if preserve_blank else [item for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return list(value) if preserve_blank else [item for item in value if item not in (None, "")]
    if value in (None, "") and not preserve_blank:
        return []
    return [value]


# 함수 설명: `_has_blank_match_values()`는 값 목록에 null·NaN·empty 계열의 명시적 blank 조건이 포함됐는지 확인합니다.
def _has_blank_match_values(values: list[Any]) -> bool:
    return any(_is_blank_match_value(value) for value in values)


# 함수 설명: `_blank_aware_membership_expression()`은 일반 값 isin과 모든 blank 표현을 하나의 pandas mask 식으로 결합합니다.
def _blank_aware_membership_expression(series: str, values: list[Any]) -> str:
    non_blank_values = [value for value in values if not _is_blank_match_value(value)]
    expressions: list[str] = []
    if non_blank_values:
        expressions.append(f"{series}.isin({non_blank_values!r})")
    if _has_blank_match_values(values):
        blank_texts = sorted(BLANK_MATCH_TEXTS)
        expressions.append(
            f"{series}.isna() | {series}.astype(str).str.strip().str.casefold().isin({blank_texts!r})"
        )
    return " | ".join(f"({expression})" for expression in expressions) or "False"


# 함수 설명: `_mapped_field_candidates()`는 Main Flow가 retrieval job에 주입한 카탈로그 매핑과 동일 이름만 실행 후보로 사용합니다.
def _mapped_field_candidates(field: str, mappings: dict[str, Any] | None = None) -> list[str]:
    candidates: list[str] = []
    mapping = mappings if isinstance(mappings, dict) else {}
    for standard, aliases in mapping.items():
        group = [str(standard or "").strip(), *_string_list(aliases)]
        group_keys = {item.casefold() for item in group if item}
        if str(field).strip().casefold() not in group_keys:
            continue
        candidates.extend(_string_list(aliases))
        if str(standard or "").strip():
            candidates.append(str(standard).strip())
        break
    if str(field or "").strip() and str(field).casefold() not in {
        item.casefold() for item in candidates
    }:
        candidates.append(str(field).strip())
    return candidates


# 함수 설명: `_safe_name()`는 생성 코드에서 사용할 문자열을 안전한 Python 식별자 조각으로 정리합니다.
def _safe_name(value: str) -> str:
    cleaned = re.sub(r"\W+", "_", value)
    return cleaned.strip("_") or "source"


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return {}
    payload = {
        key: deepcopy(item)
        for key, item in data.items()
        if key not in RUNTIME_BUFFER_KEYS
    }
    for key in RUNTIME_BUFFER_KEYS:
        if key in data:
            payload[key] = data[key]
    return payload


# 함수 설명: `_source_columns_by_alias()`는 컬럼·BY·alias 정보를 현재 질문과 응답 계약에 맞는 dict 또는 행으로 구성합니다.
def _source_columns_by_alias(payload: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for source in payload.get("source_results", []) if isinstance(payload.get("source_results"), list) else []:
        if not isinstance(source, dict):
            continue
        alias = str(source.get("source_alias") or source.get("dataset_key") or "").strip()
        columns = _string_list(source.get("columns"))
        if alias and columns:
            result[alias] = columns
    return result


# 함수 설명: `_string_list()`는 여러 형태의 입력에서 비어 있지 않은 문자열만 뽑아 중복 없는 목록으로 정리합니다.
def _string_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# 함수 설명: `_json()`는 Message·dict·JSON 문자열에서 Markdown fence를 제거하고 JSON object를 안전하게 추출합니다.
def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    text = _text_value(value)
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    elif "{" in text and "}" in text:
        text = text[text.find("{") : text.rfind("}") + 1]
    try:
        parsed = json.loads(text)
    except Exception:
        try:
            parsed = json.loads(text, strict=False)
        except Exception:
            return {}
    return parsed if isinstance(parsed, dict) else {}


# 함수 설명: `_parse_pandas_llm_response()`는 표준 JSON을 우선 사용하고, 호환용으로 result를 설정하는 원시 Python만 허용합니다.
def _parse_pandas_llm_response(value: Any) -> tuple[str, dict[str, Any]]:
    raw_text = (
        json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, dict)
        else _text_value(value)
    )
    preview = str(raw_text or "").strip()[:LLM_RESPONSE_PREVIEW_LIMIT]
    stripped = str(raw_text or "").strip()
    looks_like_json = (
        isinstance(value, dict)
        or stripped.startswith("{")
        or bool(re.fullmatch(r"```json\s*.*?\s*```", stripped, re.DOTALL | re.IGNORECASE))
    )
    parsed = _json(value) if looks_like_json else {}
    if parsed:
        code_value = parsed.get("code", parsed.get("pandas_code"))
        if code_value not in (None, ""):
            return str(code_value), {
                "mode": "json",
                "error": "",
                "raw_response_preview": preview,
            }
        return "", {
            "mode": "json",
            "error": "JSON 응답의 code 필드가 비어 있습니다.",
            "raw_response_preview": preview,
        }

    candidate = _raw_python_candidate(raw_text)
    if candidate:
        try:
            tree = ast.parse(candidate)
        except SyntaxError as exc:
            parse_error = f"Python 구문 오류: {exc.msg}"
        else:
            if _sets_result_variable(tree):
                return candidate, {
                    "mode": "raw_python",
                    "error": "",
                    "raw_response_preview": preview,
                }
            parse_error = "원시 Python 응답이 result 또는 result_df를 설정하지 않습니다."
    else:
        parse_error = "LLM 응답이 비어 있거나 지원하는 JSON/Python 형식이 아닙니다."
    return "", {
        "mode": "invalid",
        "error": parse_error,
        "raw_response_preview": preview,
    }


# 함수 설명: `_raw_python_candidate()`는 단일 Python Markdown fence만 제거하고 설명이 섞인 응답은 그대로 거부할 수 있게 유지합니다.
def _raw_python_candidate(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"```(?:python|py)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    return (match.group(1) if match else text).strip()


# 함수 설명: `_sets_result_variable()`는 원시 Python 호환 응답이 executor의 최종 결과 계약을 충족하는지 확인합니다.
def _sets_result_variable(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"result", "result_df"}:
                return True
    return False


# 함수 설명: `_text_value()`는 Langflow Message/Data에서 실제 문자열 값을 꺼내 공통 텍스트 형식으로 맞춥니다.
def _text_value(value: Any) -> str:
    structured_text = _structured_text(value)
    if structured_text:
        return structured_text
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str):
            return text
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        for key in ("text", "content", "message", "output"):
            if isinstance(data.get(key), str):
                return data[key]
    return str(value or "")


# 함수 설명: `_structured_text()`는 모델의 문자열 또는 Langflow content block 목록에서 실행 응답 텍스트를 꺼냅니다.
def _structured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_structured_text(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "code", "contents", "content_blocks", "data"):
            text = _structured_text(value.get(key))
            if text:
                return text
        return ""
    for attr in ("text", "content", "message", "output", "code", "contents", "content_blocks", "data"):
        text = _structured_text(getattr(value, attr, None))
        if text:
            return text
    return ""


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class PandasCodeExecutor(Component):
    display_name = "17 pandas 실행/1회 복구기"
    description = "pandas 코드를 안전 실행하고 실제 오류일 때만 repair LLM을 최대 1회 호출해 수정 코드를 재실행합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="llm_response", display_name="pandas 코드 LLM 응답", required=True),
        MessageTextInput(name="function_case_helper_code", display_name="선택 Function Case Helper", required=False),
        MessageTextInput(name="repair_prompt_template", display_name="pandas 복구 프롬프트", required=True, advanced=False),
        ModelInput(name="model", display_name="복구 언어 모델", required=True, real_time_refresh=True),
        SecretStrInput(name="api_key", display_name="복구 API 키", required=False, advanced=True, real_time_refresh=True),
        DropdownInput(name="max_repair_attempts", display_name="최대 Repair 횟수", options=["0", "1"], value="1", advanced=True),
    ]
    outputs = [Output(name="payload_out", display_name="페이로드 출력", method="build_payload")]

    # 주요 메서드: 모델 선택에 따라 동적 입력 필드를 갱신하는 Langflow 빌드 lifecycle 함수입니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
    def update_build_config(self, build_config: dict, field_value: str, field_name: str | None = None):
        from lfx.base.models.unified_models import (
            apply_provider_variable_config_to_build_config,
            get_language_model_options,
            get_provider_for_model_name,
            update_model_options_in_build_config,
        )

        build_config = update_model_options_in_build_config(
            component=self,
            build_config=build_config,
            cache_key_prefix="pandas_repair_language_model_options",
            get_options_func=get_language_model_options,
            field_name=field_name,
            field_value=field_value,
        )
        current_model = field_value if field_name == "model" else build_config.get("model", {}).get("value")
        provider = ""
        if isinstance(current_model, list) and current_model:
            selected = current_model[0]
            provider = str(selected.get("provider") or "").strip()
            if not provider and selected.get("name"):
                provider = get_provider_for_model_name(str(selected["name"]))
        return apply_provider_variable_config_to_build_config(build_config, provider) if provider else build_config

    # 함수 설명: `_invoke_repair_model()`는 기존 코드와 실제 오류가 포함된 프롬프트로 복구 모델을 정확히 한 번 호출합니다.
    def _invoke_repair_model(self, prompt: str) -> Any:
        from lfx.base.models.unified_models import get_llm

        llm = get_llm(
            model=getattr(self, "model", None),
            user_id=getattr(self, "user_id", None),
            api_key=getattr(self, "api_key", None),
        )
        if llm is None or not hasattr(llm, "invoke"):
            raise RuntimeError("Repair Language Model이 연결되지 않았습니다.")
        return llm.invoke(prompt)

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=execute_pandas_with_repair(
                getattr(self, "payload", None),
                getattr(self, "llm_response", ""),
                repair_invoker=self._invoke_repair_model,
                repair_prompt_template=getattr(self, "repair_prompt_template", ""),
                function_case_helper_code=getattr(self, "function_case_helper_code", ""),
                max_repair_attempts=getattr(self, "max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS),
            )
        )
