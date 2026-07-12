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

FORBIDDEN_NAMES = {"open", "exec", "eval", "__import__", "compile", "input"}
RESULT_PREVIEW_LIMIT = 50
TRACE_PREVIEW_LIMIT = 5
DEFAULT_MAX_REPAIR_ATTEMPTS = 1
REPAIR_CODE_PREVIEW_LIMIT = 1000
SAFE_IMPORT_POLICY = "exact pandas/numpy aliases are removed and trusted namespaces are injected"
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


# 주요 함수: 안전성 검사를 통과한 pandas 코드를 제한된 namespace에서 한 번 실행합니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def execute_pandas_code(payload_value: Any, llm_response: Any) -> dict[str, Any]:
    payload = _payload(payload_value)
    parsed = _json(llm_response)
    llm_code = str(parsed.get("code") or parsed.get("pandas_code") or "")
    normalized_llm_code, safe_imports = _normalize_safe_imports(llm_code)
    code = normalized_llm_code
    next_payload = deepcopy(payload)
    if not normalized_llm_code.strip():
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
        )
    filter_plan = _pandas_filter_plan(next_payload)
    filter_preamble = _pandas_filter_preamble(filter_plan)
    code = _with_pandas_filter_preamble(code, filter_plan)
    helper_trace = _runtime_helper_trace(code)
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
        exec(compile(code, "<pandas_code>", "exec"), exec_ns, exec_ns)
        result = exec_ns.get("result")
        if result is None:
            result = exec_ns.get("result_df")
        rows, columns = _result_to_rows(result, next_payload)
        next_payload["_full_result_rows"] = rows
        next_payload["analysis"] = {
            "status": "ok",
            "row_count": len(rows),
            "columns": columns,
            "used_helpers": helper_trace["used_helpers"],
            "step_outputs": step_outputs,
            "function_case_results": function_case_results,
        }
        next_payload["data"] = {"columns": columns, "rows": rows[:RESULT_PREVIEW_LIMIT], "row_count": len(rows), "data_ref": ""}
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_execution"] = {
            "stage": "17_pandas_code_executor",
            "status": "ok",
            "generated_code": code,
            "safe_import_normalization": _safe_import_trace(safe_imports),
            "used_helpers": helper_trace["used_helpers"],
            "helper_sources": helper_trace["helper_sources"],
            "pandas_filter_plan": filter_plan,
            "execution_result": {"row_count": len(rows), "columns": columns, "preview_rows": rows[:TRACE_PREVIEW_LIMIT]},
            "error": None,
        }
        return next_payload
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
    initial = execute_pandas_code(original_payload, llm_response)
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

    retry = execute_pandas_code(original_payload, repair_response)
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
    pandas_trace = _pandas_execution_trace(payload)
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    values = {
        "repair_required": "true",
        "intent_plan_json": json.dumps(plan, ensure_ascii=False, indent=2),
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
                "pandas_filter_preamble": str(pandas_trace.get("pandas_filter_preamble") or analysis.get("pandas_filter_preamble") or ""),
                "pandas_filter_plan": deepcopy(pandas_trace.get("pandas_filter_plan", [])),
                "repair_code_scope": "executor가 동일한 pandas filter preamble을 retry 코드에 다시 자동 적용합니다.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        "function_case_selection_json": json.dumps(_repair_function_case_selection(plan), ensure_ascii=False, indent=2),
        "function_case_helper_code": _text_value(function_case_helper_code),
        "output_schema": json.dumps({"code": "수정된 pandas code. 반드시 result 또는 result_df를 설정한다."}, ensure_ascii=False, indent=2),
    }
    try:
        return prompt_template.format(**values)
    except KeyError as exc:
        raise ValueError(f"unknown repair prompt variable: {exc.args[0]}") from exc


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
    payload = deepcopy(payload_value)
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
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return "dunder attribute 접근은 허용하지 않습니다."
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_IO_ATTRIBUTES:
            return f"파일/네트워크 I/O attribute '{node.attr}' 접근은 허용하지 않습니다."
    return ""


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
        "safe_import_normalization": _safe_import_trace(safe_import_info),
        "pandas_filter_preamble": filter_preamble,
        "pandas_filter_plan": filter_plan or [],
        "used_helpers": helper_trace["used_helpers"],
        "helper_sources": helper_trace["helper_sources"],
        "error": {"type": error_type, "message": message, "traceback_summary": tb[:1000]},
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


# 함수 설명: `_pandas_filter_plan()`는 조회 작업의 filter를 source alias별 결정론적 pandas 필터 계획으로 바꿉니다.
def _pandas_filter_plan(payload: dict[str, Any]) -> list[dict[str, Any]]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    filter_plan: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if not alias:
            continue
        conditions = _filter_conditions(job.get("filters"))
        if conditions:
            filter_plan.append({"source_alias": alias, "dataset_key": job.get("dataset_key", ""), "conditions": conditions})
    return filter_plan


# 함수 설명: `_with_pandas_filter_preamble()`는 생성 코드 앞에 결정론적 필터 preamble을 한 번만 결합합니다.
def _with_pandas_filter_preamble(code: Any, filter_plan: list[dict[str, Any]]) -> str:
    base_code = str(code or "").strip()
    preamble = _pandas_filter_preamble(filter_plan)
    if not preamble:
        return base_code
    return preamble + "\n\n" + base_code


# 함수 설명: `_pandas_filter_preamble()`는 의도 계획의 필터 조건을 생성 코드보다 먼저 적용할 안전한 pandas 전처리 코드로 만듭니다.
def _pandas_filter_preamble(filter_plan: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for job_index, item in enumerate(filter_plan, start=1):
        alias = str(item.get("source_alias") or "").strip()
        conditions = item.get("conditions") if isinstance(item.get("conditions"), list) else []
        if not alias or not conditions:
            continue
        df_var = f"_filtered_source_{job_index}_{_safe_name(alias)}"
        lines.append(f"{df_var} = sources.get({alias!r})")
        lines.append(f"if {df_var} is not None:")
        lines.append("    sources = dict(sources)")
        lines.append(f"    {df_var} = {df_var}.copy()")
        for condition_index, condition in enumerate(conditions, start=1):
            lines.extend(_condition_code(df_var, job_index, condition_index, condition))
        lines.append(f"    sources[{alias!r}] = {df_var}")
    return "\n".join(lines)


# 함수 설명: `_condition_code()`는 단일 필터 조건을 pandas boolean mask 표현식으로 변환합니다.
def _condition_code(df_var: str, job_index: int, condition_index: int, condition: dict[str, Any]) -> list[str]:
    field = str(condition.get("field") or "").strip()
    operator = _normalize_filter_operator(condition.get("operator") or "eq")
    values = condition.get("values") if isinstance(condition.get("values"), list) else []
    if not field or (not values and operator not in {"is_null", "is_empty", "null_or_empty", "not_null", "not_empty"}):
        return []
    col_var = f"_filter_col_{job_index}_{condition_index}"
    values_var = f"_filter_values_{job_index}_{condition_index}"
    mask_var = f"_filter_mask_{job_index}_{condition_index}"
    candidates = _field_candidates(field)
    lines = [f"    {col_var} = {_column_choice_expression(df_var, candidates)}", f"    {values_var} = {values!r}", f"    if {col_var}:"]
    if operator in {"eq", "in"}:
        lines.append(f"        {df_var} = {df_var}[{df_var}[{col_var}].isin({values_var})]")
    elif operator in {"ne", "not_in"}:
        lines.append(f"        {df_var} = {df_var}[~{df_var}[{col_var}].isin({values_var})]")
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
    elif operator in {"is_null", "is_empty", "null_or_empty", "not_null", "not_empty"}:
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
        return [f"        {mask_var} = {series}.isna() | {series}.astype(str).str.strip().eq('')", f"        {df_var} = {df_var}[{mask_var}]"]
    if operator == "not_null":
        return [f"        {df_var} = {df_var}[{series}.notna()]"]
    if operator == "not_empty":
        return [f"        {df_var} = {df_var}[~{series}.astype(str).str.strip().eq('')]"]
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
        raw_values = _as_values(item.get("values", item.get("value", [])))
        if op == "is_null":
            lines.append(f"        {mask_var} = {mask_var} | {series}.isna()")
        elif op == "is_empty":
            lines.append(f"        {mask_var} = {mask_var} | {series}.astype(str).str.strip().eq('')")
        elif op == "null_or_empty":
            lines.append(f"        {mask_var} = {mask_var} | {series}.isna() | {series}.astype(str).str.strip().eq('')")
        elif op in {"eq", "in"} and raw_values:
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
        normalized_values = _as_values(values)
        normalized_operator = _normalize_filter_operator(operator or "eq")
        if normalized_values or normalized_operator in {"is_null", "is_empty", "null_or_empty", "not_null", "not_empty"}:
            result.append({"field": field_text, "operator": normalized_operator, "values": normalized_values})
    return result


# 함수 설명: `_as_values()`는 단일 필터 값과 목록 값을 같은 값 목록 형태로 맞춥니다.
def _as_values(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return [item for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [value]


# 함수 설명: `_field_candidates()`는 표준 필터 field에 대응할 수 있는 실제 컬럼 alias 후보를 반환합니다.
def _field_candidates(field: str) -> list[str]:
    aliases = {
        "DATE": ["DATE", "WORK_DATE", "WORK_DT", "LOAD_DT", "BASE_DT"],
        "WORK_DATE": ["WORK_DATE", "WORK_DT", "DATE"],
        "MODE": ["MODE", "Mode"],
        "DEN": ["DEN", "DENSITY"],
        "PKG_TYPE1": ["PKG_TYPE1", "PKG1"],
        "PKG_TYPE2": ["PKG_TYPE2", "PKG2"],
        "MCP_NO": ["MCP_NO", "MCP NO"],
        "TSV_DIE_TYP": ["TSV_DIE_TYP", "TSV_DIE_TYPE"],
        "OPER_NUM": ["OPER_NUM", "OPER"],
        "OPER_NAME": ["OPER_NAME", "OPER_NM"],
        "EQP_ID": ["EQP_ID", "EQUIP_ID"],
        "EQP_MODEL": ["EQP_MODEL", "EQUIP_MODEL", "EQPIP_MODEL"],
    }
    return aliases.get(field, [field])


# 함수 설명: `_safe_name()`는 생성 코드에서 사용할 문자열을 안전한 Python 식별자 조각으로 정리합니다.
def _safe_name(value: str) -> str:
    cleaned = re.sub(r"\W+", "_", value)
    return cleaned.strip("_") or "source"


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    return deepcopy(data) if isinstance(data, dict) else {}


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
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


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


# 함수 설명: `_text_value()`는 Langflow Message/Data에서 실제 문자열 값을 꺼내 공통 텍스트 형식으로 맞춥니다.
def _text_value(value: Any) -> str:
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
        response = llm.invoke(prompt)
        return getattr(response, "content", response)

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
