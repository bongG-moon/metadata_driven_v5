# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 17 V2 Hybrid 분석 실행기
# 역할: Fast 계약은 고정 실행하고 Complex 계약일 때만 pandas 생성 LLM을 지연 호출합니다.
# 주요 입력: 페이로드, pandas prompt, 선택 Function Case Helper, 복구 프롬프트, pandas/복구 언어 모델
#        (api_key), 최대 Repair 횟수 (max_repair_attempts)
# 주요 출력: 페이로드 출력 (payload_out)
# 처리 흐름: 생성 코드를 AST로 검사하고 제한된 pandas/numpy 환경에서 실행하며, 실패하면 이전 코드와 오류를 포함해 LLM 복구를 최대 한 번 수행합니다.
# 유지보수 포인트: 파일·네트워크 I/O와 임의 import는 차단하고 pandas/numpy alias만 허용합니다. 복구 호출은 실행 오류당 최대 한 번입니다.
# =============================================================================

from __future__ import annotations

import ast
import hashlib
import json
from pprint import pformat
import re
import traceback
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import BoolInput, DataInput, DropdownInput, MessageTextInput, ModelInput, Output, SecretStrInput
from lfx.schema.data import Data

RUNTIME_BUFFER_KEYS = {
    "runtime_sources",
    "_runtime_rows_by_alias",
    "_full_result_rows",
    "_runtime_result_rows",
    "_intermediate_download_rows",
    "_intermediate_download_metadata",
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


# Langflow 컴포넌트 클래스: 부분 실행 체크포인트를 포함한 계약 오류입니다.
class PartialExecutionError(OutputContractError):
    """Execution stopped after a bounded checkpoint was captured."""

    # 함수 설명: bounded intermediate result preview와 오류 메시지를 보존합니다.
    def __init__(self, message: str, intermediate_results: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.intermediate_results = deepcopy(intermediate_results or [])


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
    next_payload = _canonicalize_standardized_output_contract(payload)
    next_payload.setdefault("intermediate_results", _initial_intermediate_results(next_payload))
    checkpoint_values: dict[str, Any] = {}
    intermediate_results: list[dict[str, Any]] = []
    sources: dict[str, Any] = {}
    llm_code, response_parse = _parse_pandas_llm_response(llm_response)
    normalized_llm_code, safe_imports = _normalize_safe_imports(llm_code)
    deterministic_execution = _deterministic_execution_contract(next_payload)
    fast_execution = str(deterministic_execution.get("operation") or "").strip() == "execute_fast_path_recipe"
    deterministic_logic_code = ""
    if deterministic_execution:
        deterministic_logic_code = (
            _fast_deterministic_logic_display(deterministic_execution)
            if fast_execution
            else _deterministic_contract_display_code(deterministic_execution)
        )
    deterministic_function = (
        _fast_function_trace(deterministic_execution)
        if fast_execution
        else {}
    )
    execution_mode = str(
        deterministic_execution.get("operation") or "llm_generated_code"
    )
    trusted_helper_preamble = ""
    complex_source_transforms: list[dict[str, Any]] = []
    complex_transform_preamble = ""
    function_case_rewrite_trace: dict[str, Any] = {}
    deterministic_transform_error = ""
    if not deterministic_execution:
        (
            normalized_llm_code,
            trusted_helper_preamble,
            trusted_helper_trace,
            trusted_helper_error,
        ) = _prepare_trusted_function_case_helpers(
            normalized_llm_code,
            function_case_helper_code,
        )
        if trusted_helper_trace:
            safe_imports["trusted_helper_override"] = trusted_helper_trace
        if trusted_helper_error:
            return _analysis_error(
                next_payload,
                "trusted_function_case_contract_invalid",
                trusted_helper_error,
                normalized_llm_code,
                "",
                llm_code,
                "",
                [],
                safe_imports,
                response_parse=response_parse,
            )
        complex_source_transforms = _selected_function_case_source_transforms(
            next_payload
        )
        # 기존 import/fixture 경로에는 helper 코드를 LLM 코드 안에 직접 넣는
        # 경우가 있다. 신뢰된 helper 코드가 전달된 경우에만 결정론적 전처리로
        # 강화하고, 그렇지 않으면 기존 실행 경로를 유지한다.
        if complex_source_transforms and _text_value(function_case_helper_code).strip():
            (
                normalized_llm_code,
                function_case_rewrite_trace,
                rewrite_error,
        ) = _replace_selected_function_case_calls(
            normalized_llm_code,
            complex_source_transforms,
            active_source_aliases=_active_retrieval_source_aliases(next_payload),
            runtime_source_aliases=set(
                str(alias).strip()
                for alias in (next_payload.get("runtime_sources") or {}).keys()
                if str(alias).strip()
            ),
        )
            if rewrite_error:
                return _analysis_error(
                    next_payload,
                    "trusted_function_case_contract_invalid",
                    rewrite_error,
                    normalized_llm_code,
                    "",
                    llm_code,
                    "",
                    [],
                    safe_imports,
                    response_parse=response_parse,
                )
            (
                complex_transform_preamble,
                deterministic_transform_error,
            ) = _deterministic_function_case_preamble(
                {"source_transforms": complex_source_transforms},
                function_case_helper_code,
                include_helper_code=False,
            )
            if function_case_rewrite_trace:
                safe_imports["selected_function_case_pre_transform"] = deepcopy(
                    function_case_rewrite_trace
                )
        safe_imports["normalized_llm_code"] = normalized_llm_code
    code = normalized_llm_code
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
    display_filter_plan = _pandas_filter_plan(next_payload)
    filter_plan = [] if fast_execution else display_filter_plan
    row_match_plan = [] if fast_execution else _pandas_row_match_plan(next_payload)
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
        analysis_code = "\n\n".join(
            item
            for item in (
                complex_transform_preamble.strip(),
                code.strip(),
            )
            if item
        )
        code = "\n\n".join(
            segment
            for segment in (
                trusted_helper_preamble.strip(),
                _with_pandas_execution_preambles(
                    analysis_code,
                    row_match_preamble,
                    filter_preamble,
                ).strip(),
            )
            if segment
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
    physical_alias_issues = (
        []
        if deterministic_execution
        else _generated_code_physical_alias_issues(next_payload, normalized_llm_code)
    )
    if physical_alias_issues:
        transitions = ", ".join(
            f"{item['physical_column']}→{item['canonical_column']}"
            for item in physical_alias_issues[:8]
        )
        return _analysis_error(
            next_payload,
            "generated_code_uses_physical_alias",
            "표준화된 source에서 제거된 물리 컬럼 alias를 pandas 코드가 다시 참조했습니다: "
            + transitions,
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
        # 함수 설명: `record_checkpoint()`는 실행 중 생성된 원본·필터·계산 결과를
        # 화면 후보와 다운로드용 원본 값으로 함께 보관합니다.
        def record_checkpoint(
            key: Any,
            value: Any,
            description: Any = "",
            role: Any = "",
        ) -> dict[str, Any]:
            item = _recorded_output(key, value, description, role)
            marker = str(item.get("key") or "").strip()
            if marker:
                checkpoint_values[marker] = value
            intermediate_results.append(item)
            return item

        # 함수 설명: `publish_checkpoint()`는 여러 후보 중 하나만 현재 payload에
        # 노출하고, 전체 행은 답변 모델에 전달하지 않는 runtime buffer로 저장합니다.
        def publish_checkpoint(
            *,
            completed: bool = False,
            final_rows: list[dict[str, Any]] | None = None,
            final_columns: list[str] | None = None,
        ) -> None:
            visible, rows_by_key, metadata_by_key = _project_intermediate_checkpoint(
                intermediate_results,
                checkpoint_values,
                next_payload,
                display_filter_plan,
                completed=completed,
                final_rows=final_rows,
                final_columns=final_columns,
            )
            next_payload["intermediate_results"] = visible
            if rows_by_key:
                next_payload["_intermediate_download_rows"] = rows_by_key
                next_payload["_intermediate_download_metadata"] = metadata_by_key
            else:
                next_payload.pop("_intermediate_download_rows", None)
                next_payload.pop("_intermediate_download_metadata", None)

        for alias, frame in sources.items():
            record_checkpoint(
                f"source:{alias}",
                frame,
                "조회된 원본 데이터",
                "source_input",
            )
        publish_checkpoint()
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
            recorded = record_checkpoint(key, value, description, role or "step_output")
            step_outputs.append(deepcopy(recorded))
            publish_checkpoint()
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
            fast_intermediate_frames: dict[str, Any] = {}
            deterministic_result = _execute_deterministic_contract(
                deterministic_execution,
                sources,
                pd,
                runtime_intermediate_frames=fast_intermediate_frames,
            )
            if (
                isinstance(deterministic_result, tuple)
                and len(deterministic_result) == 2
            ):
                result, semantic_execution_certificate = deterministic_result
            else:
                result = deterministic_result
                semantic_execution_certificate = {}
            for checkpoint_key, frame in fast_intermediate_frames.items():
                is_typed_step = str(checkpoint_key).startswith("typed_step:")
                record_checkpoint(
                    checkpoint_key,
                    frame,
                    "Typed 실행 계획 단계 결과" if is_typed_step else "필터 적용 후 원본 데이터",
                    "step_output" if is_typed_step else "filtered_source",
                )
            if fast_execution:
                record_checkpoint(
                    "computed_result",
                    result,
                    "최종 계약 적용 전 계산 결과",
                    "computed_result",
                )
                publish_checkpoint()
                next_payload = _apply_fast_result_contract(next_payload, result, semantic_execution_certificate)
            else:
                result = _apply_deterministic_result_ordering(
                    result,
                    next_payload,
                )
            row_match_execution = []
            function_case_grain_enrichment = {}
        else:
            exec(compile(code, "<pandas_code>", "exec"), exec_ns, exec_ns)
            sources = (
                exec_ns.get("sources")
                if isinstance(exec_ns.get("sources"), dict)
                else sources
            )
            row_match_execution_value = exec_ns.get("_row_match_execution", [])
            row_match_execution = (
                deepcopy(row_match_execution_value)
                if isinstance(row_match_execution_value, list)
                else []
            )
            result = exec_ns.get("result")
            if result is None:
                result = exec_ns.get("result_df")
            result, function_case_grain_enrichment = _enrich_unique_function_case_grain(
                result,
                exec_ns,
                next_payload,
                pd,
            )
            semantic_execution_certificate = {}
            deterministic_transform_execution_value = exec_ns.get(
                "_deterministic_function_case_execution",
                [],
            )
            deterministic_transform_execution = (
                deepcopy(deterministic_transform_execution_value)
                if isinstance(deterministic_transform_execution_value, list)
                else []
            )
            if deterministic_transform_execution:
                helper_trace["helper_sources"] = deepcopy(
                    deterministic_transform_execution
                )
            for alias, frame in sources.items():
                record_checkpoint(
                    f"filtered:{alias}",
                    frame,
                    "필터 적용 후 데이터",
                    "filtered_source",
                )
            publish_checkpoint()
        if isinstance(semantic_execution_certificate, dict):
            certificate_results = semantic_execution_certificate.get("intermediate_results")
            if isinstance(certificate_results, list):
                intermediate_results.extend(
                    item for item in deepcopy(certificate_results)
                    if isinstance(item, dict)
                )
        deduped_intermediate_results: list[dict[str, Any]] = []
        seen_intermediate_keys: set[str] = set()
        for item in intermediate_results:
            marker = str(item.get("key") or "") if isinstance(item, dict) else ""
            if marker and marker in seen_intermediate_keys:
                continue
            if marker:
                seen_intermediate_keys.add(marker)
            if isinstance(item, dict):
                deduped_intermediate_results.append(item)
        intermediate_results = deduped_intermediate_results
        if "computed_result" not in checkpoint_values:
            record_checkpoint(
                "computed_result",
                result,
                "최종 계약 적용 전 계산 결과",
                "computed_result",
            )
        publish_checkpoint()
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
        publish_checkpoint(completed=True, final_rows=rows, final_columns=columns)
        next_payload["analysis"] = {
            "status": "ok",
            "row_count": len(rows),
            "columns": columns,
            "used_helpers": helper_trace["used_helpers"],
            "step_outputs": step_outputs,
            "function_case_results": function_case_results,
            "intermediate_results": deepcopy(next_payload.get("intermediate_results", [])),
            "execution_mode": execution_mode,
        }
        if deterministic_execution:
            next_payload["analysis"]["code_generation_type"] = "deterministic_function"
        if semantic_execution_certificate:
            next_payload["analysis"]["semantic_execution_certificate"] = deepcopy(
                semantic_execution_certificate
            )
        next_payload["data"] = {"columns": columns, "rows": rows[:RESULT_PREVIEW_LIMIT], "row_count": len(rows), "data_ref": ""}
        next_payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_execution"] = {
            "stage": "17_hybrid_analysis_executor",
            "status": "ok",
            "generated_code": code,
            "llm_generated_code": normalized_llm_code,
            "deterministic_logic_code": deterministic_logic_code,
            "code_generation_type": "deterministic_function" if deterministic_execution else "llm_generated",
            "deterministic_function": deepcopy(deterministic_function),
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
            "function_case_grain_enrichment": deepcopy(function_case_grain_enrichment),
            "execution_result": {"row_count": len(rows), "columns": columns, "preview_rows": rows[:TRACE_PREVIEW_LIMIT]},
            "intermediate_results": deepcopy(next_payload.get("intermediate_results", [])),
            "semantic_execution_certificate": deepcopy(semantic_execution_certificate),
            "error": None,
        }
        return next_payload
    except PartialExecutionError as exc:
        if exc.intermediate_results and not intermediate_results:
            intermediate_results.extend(
                item for item in exc.intermediate_results if isinstance(item, dict)
            )
        if intermediate_results:
            publish_checkpoint()
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
            intermediate_results=next_payload.get("intermediate_results"),
        )
    except OutputContractError as exc:
        if intermediate_results:
            publish_checkpoint()
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
            intermediate_results=next_payload.get("intermediate_results"),
        )
    except Exception as exc:
        if sources and "computed_result" not in checkpoint_values and callable(locals().get("record_checkpoint")):
            for alias, frame in sources.items():
                record_checkpoint(
                    f"last_available:{alias}",
                    frame,
                    "오류 전 마지막 확인 가능 데이터",
                    "last_available_source",
                )
        if intermediate_results and callable(locals().get("publish_checkpoint")):
            publish_checkpoint()
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
            intermediate_results=next_payload.get("intermediate_results"),
        )


# 함수 설명: 정규화기가 만든 신뢰 가능한 다중 source 계약 중 실행할 하나를 선택합니다.
def _deterministic_execution_contract(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    extreme_row_contract = _select_extreme_row_per_group_contract(payload)
    if extreme_row_contract:
        return extreme_row_contract
    # A trusted previous-result enrichment has two real input frames even when
    # the Fast resolver sees only the newly retrieved source.  Prefer the
    # explicit left-preserving contract so the prior result grain/metrics are
    # not discarded by a single-source Fast recipe.
    reference_join = plan.get("resolved_reference_join_plan")
    if (
        isinstance(reference_join, dict)
        and reference_join.get("strict") is True
        and str(reference_join.get("operation") or "").strip()
        == "enrich_previous_result"
    ):
        return deepcopy(reference_join)
    fast_plan = payload.get("simple_analysis_contract")
    if (
        isinstance(fast_plan, dict)
        and fast_plan.get("strict") is True
        and (
            (
                str(fast_plan.get("route") or "").strip().lower() == "fast"
                and str(fast_plan.get("operation") or "").strip() == "execute_fast_path_recipe"
            )
            or str(fast_plan.get("operation") or "").strip()
            == "execute_typed_pandas_plan"
        )
    ):
        return deepcopy(fast_plan)
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
    return _single_source_execution_contract(payload)


# 함수 설명: pandas 단계가 비어 있는 단일 source 조회는 표준 output contract와 hydrated job만으로 projection/aggregate 계약을 구성합니다.
def _select_extreme_row_per_group_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Compile the generic same-row extreme selection primitive from Typed IR."""

    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    steps = [item for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    extreme = next(
        (
            item
            for item in steps
            if str(item.get("operation") or "").strip().lower() == "select_extreme_row_per_group"
        ),
        None,
    )
    if not isinstance(extreme, dict):
        return {}
    if extreme.get("strict") is not True:
        return {}
    partition_by = _string_list(extreme.get("partition_by"))
    order_by = _normalized_order_specs(extreme.get("order_by"), default_direction="desc")
    tie_breakers = _normalized_order_specs(extreme.get("tie_breakers"), default_direction="asc")
    projection = _string_list(extreme.get("projection"))
    if not partition_by or not order_by or not projection:
        return {}
    try:
        limit_per_group = int(extreme.get("limit_per_group"))
    except (TypeError, ValueError):
        return {}
    if limit_per_group != 1:
        return {}
    tie_policy = str(extreme.get("tie_policy") or "").strip().lower()
    if tie_policy not in {"first", "include_all", "error"}:
        return {}
    if tie_policy == "first" and not tie_breakers:
        return {}

    aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in plan.get("retrieval_jobs", [])
        if isinstance(job, dict)
    }
    source_alias, predecessor_steps = _external_source_chain_for_step(extreme, steps, aliases)
    if not source_alias:
        return {}
    pre_operations: list[dict[str, Any]] = []
    for predecessor in predecessor_steps:
        operation = str(predecessor.get("operation") or "").strip().lower()
        if operation == "apply_filters":
            raw_filters = predecessor.get("filters")
            if raw_filters in (None, "", [], {}):
                raw_filters = predecessor.get("conditions")
            conditions = [
                {
                    "canonical_field": str(item.get("field") or "").strip(),
                    "operator": str(item.get("operator") or "eq").strip().lower(),
                    "typed_values": deepcopy(item.get("values", [])),
                }
                for item in _filter_conditions(raw_filters)
            ]
            if _unsupported_filter_operators([{"conditions": _filter_conditions(raw_filters)}]):
                return {}
            pre_operations.append({"operation": "apply_filters", "conditions": conditions})
        elif operation == "select_columns":
            columns = _string_list(
                predecessor.get("projection")
                or predecessor.get("columns")
                or predecessor.get("result_columns")
            )
            if not columns:
                return {}
            pre_operations.append({"operation": "select_columns", "columns": columns})
        elif operation == "apply_row_match_groups":
            match_source = str(predecessor.get("source_alias") or "").strip()
            reference_alias = str(
                predecessor.get("reference_source_alias") or ""
            ).strip()
            match_columns = _string_list(predecessor.get("match_columns"))
            # Only compile an explicit, normalized prior-result match.  Other
            # row-match shapes remain on the guarded Typed-plan path rather
            # than being approximated by this deterministic primitive.
            if (
                match_source != source_alias
                or reference_alias not in {"previous_result", "upstream_result"}
                or not match_columns
            ):
                return {}
            pre_operations.append(
                {
                    "operation": "apply_row_match_groups",
                    "reference_source_alias": reference_alias,
                    "match_columns": match_columns,
                }
            )
        else:
            # Never jump over a transform that this deterministic primitive
            # cannot reproduce.  The caller will keep the safe Complex path.
            return {}
    # Row matching is an in-place source transform rather than a node-output
    # transform, so it is not necessarily on the typed predecessor chain.
    # Collect only the declared match for this exact retrieval source and put
    # it before other source transforms.  This is the same generic semantics
    # used by the guarded Typed-plan executor.
    declared_row_matches = [
        item
        for item in steps
        if str(item.get("operation") or "").strip().lower()
        == "apply_row_match_groups"
        and str(item.get("source_alias") or "").strip() == source_alias
    ]
    for row_match in declared_row_matches:
        reference_alias = str(
            row_match.get("reference_source_alias") or ""
        ).strip()
        match_columns = _string_list(row_match.get("match_columns"))
        if reference_alias not in {"previous_result", "upstream_result"} or not match_columns:
            return {}
        row_match_contract = {
            "operation": "apply_row_match_groups",
            "reference_source_alias": reference_alias,
            "match_columns": match_columns,
        }
        if row_match_contract not in pre_operations:
            pre_operations.insert(0, row_match_contract)
    contract: dict[str, Any] = {
        "strict": True,
        "operation": "select_extreme_row_per_group",
        "source_alias": source_alias,
        "partition_by": partition_by,
        "order_by": order_by,
        "tie_breakers": tie_breakers,
        "limit_per_group": limit_per_group,
        "tie_policy": tie_policy,
        "projection": projection,
        "pre_operations": pre_operations,
        "result_columns": _string_list(
            (plan.get("output_contract") or {}).get("result_columns")
            if isinstance(plan.get("output_contract"), dict)
            else []
        ),
    }
    extreme_refs = {
        str(extreme.get("node_id") or "").strip(),
        str(extreme.get("output_alias") or "").strip(),
    } - {""}
    join_step = next(
        (
            item
            for item in steps
            if str(item.get("operation") or "").strip().lower() in {"join", "left_join", "merge"}
            and _step_references_any(item, extreme_refs)
        ),
        None,
    )
    if isinstance(join_step, dict):
        left_alias = str(join_step.get("left_source_alias") or "").strip()
        right_alias = str(join_step.get("right_source_alias") or "").strip()
        input_refs = [
            str(item.get("ref") or "").strip()
            for item in join_step.get("inputs", [])
            if isinstance(item, dict)
        ]
        prior_result_aliases = {"upstream_result", "previous_result"}
        upstream_on_left = left_alias in prior_result_aliases or (
            bool(prior_result_aliases.intersection(input_refs))
            and left_alias not in extreme_refs
        )
        join_type = str(join_step.get("join_type") or "left").strip().lower()
        if join_type != "left" or not upstream_on_left:
            return {}
        left_on = _string_list(join_step.get("left_on") or join_step.get("left_keys"))
        right_on = _string_list(join_step.get("right_on") or join_step.get("right_keys"))
        shared_keys = _string_list(join_step.get("join_keys"))
        left_on = left_on or shared_keys or partition_by
        right_on = right_on or shared_keys or partition_by
        if not left_on or len(left_on) != len(right_on):
            return {}
        contract["join"] = {
            "left_source_alias": left_alias or "upstream_result",
            "join_type": "left",
            "left_on": left_on,
            "right_on": right_on,
            "population_policy": "left_source_only",
        }
    return contract


# 함수 설명: Typed pandas 계획이 same-row extreme primitive를 선언했는지 확인해 malformed 계약의 LLM 우회를 차단합니다.
def _declares_select_extreme_row_per_group(payload: dict[str, Any]) -> bool:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    return any(
        isinstance(step, dict)
        and str(step.get("operation") or "").strip().lower()
        == "select_extreme_row_per_group"
        for step in plan.get("pandas_execution_plan", [])
    )


# 함수 설명: 정렬 컬럼과 asc·desc 방향을 중복 없는 표준 spec 목록으로 정리합니다.
def _normalized_order_specs(value: Any, default_direction: str) -> list[dict[str, str]]:
    items = value if isinstance(value, list) else [value]
    result: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            column = str(item.get("column") or item.get("field") or item.get("sort_by") or "").strip()
            direction = str(item.get("direction") or item.get("order") or default_direction).strip().lower()
        else:
            column = str(item or "").strip()
            direction = default_direction
        if not column or direction not in {"asc", "desc"}:
            continue
        if column not in {entry["column"] for entry in result}:
            result.append({"column": column, "direction": direction})
    return result


# 주요 함수: node_output 입력을 외부 source까지 역추적하되 건너뛸 수 없는 선행 변환 목록도 함께 반환합니다.
def _external_source_chain_for_step(
    step: dict[str, Any],
    steps: list[dict[str, Any]],
    aliases: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve a node input without skipping declared predecessor transforms."""

    by_ref: dict[str, dict[str, Any]] = {}
    for candidate in steps:
        for key in ("node_id", "output_alias"):
            ref = str(candidate.get(key) or "").strip()
            if ref:
                by_ref[ref] = candidate
    visited: set[str] = set()

    # 함수 설명: 현재 node의 typed input을 재귀적으로 따라 source와 순서화된 predecessor chain을 찾습니다.
    def resolve(candidate: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        for item in candidate.get("inputs", []) if isinstance(candidate.get("inputs"), list) else []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            ref = str(item.get("ref") or "").strip()
            if kind == "external_source" and ref in aliases:
                return ref, []
            if kind == "node_output" and ref and ref not in visited and ref in by_ref:
                visited.add(ref)
                predecessor = by_ref[ref]
                source_alias, chain = resolve(predecessor)
                if source_alias:
                    return source_alias, [*chain, predecessor]
        source = str(candidate.get("source_alias") or "").strip()
        return (source, []) if source in aliases else ("", [])

    return resolve(step)


# 함수 설명: join 단계가 지정된 node_id 또는 output_alias를 참조하는지 확인합니다.
def _step_references_any(step: dict[str, Any], references: set[str]) -> bool:
    direct = {
        str(step.get("left_source_alias") or "").strip(),
        str(step.get("right_source_alias") or "").strip(),
    }
    typed = {
        str(item.get("ref") or "").strip()
        for item in step.get("inputs", [])
        if isinstance(item, dict)
    } if isinstance(step.get("inputs"), list) else set()
    return bool((direct | typed) & references)


# 함수 설명: 단계 limit 값을 정수로 변환하고 실패하면 안전한 기본값을 사용합니다.
def _positive_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 주요 함수: pandas 단계가 없는 strict 단일 source 계획을 projection 또는 aggregate 계약으로 변환합니다.
def _single_source_execution_contract(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    if steps:
        return {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    result_mode = str(contract.get("result_mode") or "").strip().lower()
    result_columns = _string_list(contract.get("result_columns"))
    if (
        contract.get("strict_result_columns") is not True
        or result_mode not in {"aggregate", "detail", "entity_list"}
        or not result_columns
    ):
        return {}
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    standardized_aliases = _standardized_source_aliases(payload)
    jobs = [
        job
        for job in plan.get("retrieval_jobs", [])
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        in runtime_sources
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        in standardized_aliases
    ]
    aliases = {
        str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        for job in jobs
    }
    if len(aliases) != 1:
        return {}
    source_alias = next(iter(aliases))
    available = {
        column.casefold(): column
        for column in _runtime_source_columns_by_alias(payload).get(source_alias, [])
    }
    if not available:
        return {}

    if result_mode in {"detail", "entity_list"}:
        if any(column.casefold() not in available for column in result_columns):
            return {}
        return {
            "strict": True,
            "operation": "project_single_source",
            "source_alias": source_alias,
            "result_columns": result_columns,
        }

    grain_columns = _string_list(contract.get("grain_columns"))
    if any(column.casefold() not in available for column in grain_columns):
        return {}
    metric_columns = _string_list(contract.get("metric_columns"))
    if not metric_columns:
        return {}
    bindings = [
        item
        for item in contract.get("metric_bindings", [])
        if isinstance(item, dict)
        and str(item.get("source_alias") or "").strip() in {"", source_alias}
    ]
    job = jobs[0]
    semantics = job.get("metric_semantics") if isinstance(job.get("metric_semantics"), dict) else {}
    metrics: list[dict[str, str]] = []
    for output_column in metric_columns:
        binding = next(
            (
                item
                for item in bindings
                if str(item.get("output_column") or "").strip().casefold()
                == output_column.casefold()
            ),
            {},
        )
        source_column = str(binding.get("source_column") or output_column).strip()
        actual_source = available.get(source_column.casefold())
        if not actual_source:
            return {}
        semantic = semantics.get(output_column) if isinstance(semantics.get(output_column), dict) else {}
        aggregation = str(
            binding.get("aggregation")
            or semantic.get("default_rollup")
        ).strip().lower()
        if not aggregation:
            return {}
        if aggregation in {"avg", "average"}:
            aggregation = "mean"
        if aggregation not in {
            "sum", "mean", "min", "max", "count",
            "nunique", "median", "first", "last", "collect_unique",
        }:
            return {}
        metrics.append(
            {
                "source_column": actual_source,
                "output_column": output_column,
                "aggregation": aggregation,
            }
        )
    return {
        "strict": True,
        "operation": "aggregate_single_source",
        "source_alias": source_alias,
        "grain_columns": grain_columns,
        "metrics": metrics,
    }


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
    elif operation == "aggregate_single_source":
        lines.append("# action: aggregate one standardized source from the output contract")
    elif operation == "project_single_source":
        lines.append("# action: project canonical result columns from one standardized source")
    elif operation == "execute_typed_pandas_plan":
        lines.append("# action: execute the validated Typed IR DataFrame plan without model-generated code")
    elif operation == "select_extreme_row_per_group":
        lines.append("# action: select complete winning rows per partition, then optionally left-enrich upstream_result")
    return "\n".join(lines)


# 함수 설명: `_fast_function_trace()`는 Fast 계약이 실제로 선택한 dispatcher와 고정 handler 이름을 실행 추적으로 반환합니다.
def _fast_function_trace(contract: dict[str, Any]) -> dict[str, Any]:
    recipe = str(contract.get("recipe") or "").strip().lower()
    handlers: list[str] = []
    filters = [item for item in contract.get("filters", []) if isinstance(item, dict)]
    if any(str(item.get("execution_stage") or "post_retrieval") != "retrieval_pushdown" for item in filters):
        handlers.append("_apply_fast_filters")
    recipe_handlers = {
        "detail_query": ["_fast_project"],
        "scalar_summary": ["_fast_aggregate"],
        "group_summary": ["_fast_aggregate"],
        "ranked_summary": ["_fast_aggregate"],
        "frequency_summary": ["_fast_frequency"],
        "distinct_summary": ["_fast_distinct"],
        "list_summary": ["_fast_aggregate"],
        "existence_summary": [],
        "quality_summary": ["_fast_quality_summary"],
        "latest_earliest": ["_fast_project"],
        "percent_of_total": ["_fast_percent_of_total"],
        "rank_within_group": ["_fast_rank_within_group"],
        "threshold_after_aggregate": ["_fast_aggregate", "_apply_fast_post_filters"],
        "time_bucket_summary": ["_fast_time_bucket_summary"],
        "period_change": ["_fast_period_change"],
        "running_total": ["_fast_running_total"],
        "moving_aggregate": ["_fast_moving_aggregate"],
        "percentile_summary": ["_fast_percentile_summary"],
        "pivot_summary": ["_fast_pivot_summary"],
    }
    recipe_specific_handlers = recipe_handlers.get(recipe, [])
    calculation = contract.get("calculation") if isinstance(contract.get("calculation"), dict) else {}
    if recipe == "scalar_summary" and str(calculation.get("scalar_operation") or "") == "count_rows":
        recipe_specific_handlers = []
    handlers.extend(recipe_specific_handlers)
    handlers.append("_apply_fast_ordering_and_limit")
    return {
        "dispatcher": "_execute_fast_path_recipe",
        "handlers": list(dict.fromkeys(handlers)),
        "recipe": recipe,
        "source_alias": str(contract.get("source_alias") or ""),
    }


# 함수 설명: `_fast_deterministic_logic_display()`는 실행된 Fast 고정 함수와 실제 계약 인자를 사용자 진단용 Python 표현으로 만듭니다.
def _fast_deterministic_logic_display(contract: dict[str, Any]) -> str:
    trace = _fast_function_trace(contract)
    visible_keys = (
        "operation",
        "recipe",
        "source_alias",
        "dataset_key",
        "filters",
        "projection",
        "group_by",
        "metrics",
        "post_filters",
        "ordering",
        "limit",
        "tie_policy",
        "null_policy",
        "calculation",
        "result_columns",
    )
    visible_contract = {
        key: deepcopy(contract.get(key))
        for key in visible_keys
        if contract.get(key) not in (None, "", [], {})
    }
    handlers = " -> ".join(trace.get("handlers") or []) or "dispatcher inline operation"
    return "\n".join(
        [
            "# Fast Path deterministic function call",
            "# LLM 생성 코드가 아니며 아래 고정 함수와 계약으로 실행했습니다.",
            f"# dispatcher: {trace.get('dispatcher')}",
            f"# handlers: {handlers}",
            "fast_contract = " + pformat(visible_contract, width=100, sort_dicts=False),
            "result, execution_certificate = _execute_fast_path_recipe(",
            "    contract=fast_contract,",
            "    sources=sources,",
            "    pd=pd,",
            ")",
        ]
    )


# 함수 설명: trace/display 주석 값에서 줄바꿈과 주석 제어 문자를 제거합니다.
def _single_line_comment_value(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("#", "").strip()


# 함수 설명: 결정론적 다중 source 계약의 typed Function Case 단계를 제한된 실행 preamble로 변환합니다.
def _deterministic_function_case_preamble(
    contract: dict[str, Any],
    helper_code_value: Any,
    *,
    include_helper_code: bool = True,
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

    lines = []
    if include_helper_code:
        lines.append(helper_code)
    lines.append("_deterministic_function_case_execution = []")
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
    *,
    runtime_intermediate_frames: dict[str, Any] | None = None,
) -> Any:
    operation = str(contract.get("operation") or "").strip()
    if operation == "execute_fast_path_recipe":
        return _execute_fast_path_recipe(
            contract,
            sources,
            pd,
            runtime_intermediate_frames=runtime_intermediate_frames,
        )
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
    if operation == "select_extreme_row_per_group":
        return _execute_select_extreme_row_per_group(contract, sources, pd)
    if operation in {"aggregate_single_source", "project_single_source"}:
        return _execute_single_source_contract(contract, sources, pd)
    if operation == "execute_typed_pandas_plan":
        return _execute_typed_pandas_plan(
            contract,
            sources,
            pd,
            runtime_intermediate_frames=runtime_intermediate_frames,
        )
    raise OutputContractError(f"지원하지 않는 deterministic 실행 계약입니다: {operation}")


# 함수 설명: V2 Fast Path의 단일 source 레시피를 질문 문구나 물리 컬럼 fallback 없이 실행합니다.
def _execute_select_extreme_row_per_group(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> tuple[Any, dict[str, Any]]:
    """Select all projected values from the same winning physical row per group."""

    source_alias = str(contract.get("source_alias") or "").strip()
    source = sources.get(source_alias)
    if source is None:
        raise OutputContractError(f"select_extreme_row_per_group source가 없습니다: {source_alias}")
    frame = source.copy()
    predecessor_execution: list[dict[str, Any]] = []
    for pre_operation in contract.get("pre_operations", []):
        if not isinstance(pre_operation, dict):
            raise OutputContractError("select_extreme_row_per_group pre_operations 계약이 유효하지 않습니다.")
        operation = str(pre_operation.get("operation") or "").strip().lower()
        before = int(len(frame))
        if operation == "apply_filters":
            conditions = [
                item
                for item in pre_operation.get("conditions", [])
                if isinstance(item, dict)
            ]
            frame, filter_execution = _apply_fast_filters(frame, conditions, pd)
            predecessor_execution.append(
                {
                    "operation": operation,
                    "row_count_before": before,
                    "row_count_after": int(len(frame)),
                    "conditions": filter_execution,
                }
            )
        elif operation == "select_columns":
            columns = _string_list(pre_operation.get("columns"))
            frame = _fast_project(frame, columns)
            predecessor_execution.append(
                {
                    "operation": operation,
                    "row_count_before": before,
                    "row_count_after": int(len(frame)),
                    "columns": columns,
                }
            )
        elif operation == "apply_row_match_groups":
            reference_alias = str(
                pre_operation.get("reference_source_alias") or ""
            ).strip()
            reference = sources.get(reference_alias)
            match_columns = _string_list(pre_operation.get("match_columns"))
            if reference is None or not match_columns:
                raise OutputContractError(
                    "select_extreme_row_per_group의 이전 결과 매칭 계약이 유효하지 않습니다."
                )
            frame, row_match_execution = _apply_deterministic_row_match_groups(
                frame,
                reference,
                match_columns,
                pd,
            )
            predecessor_execution.append(
                {
                    "operation": operation,
                    "row_count_before": before,
                    "row_count_after": int(len(frame)),
                    "reference_source_alias": reference_alias,
                    **row_match_execution,
                }
            )
        else:
            raise OutputContractError(
                f"지원하지 않는 select_extreme_row_per_group 선행 연산입니다: {operation}"
            )
    partition_by = _string_list(contract.get("partition_by"))
    order_by = [item for item in contract.get("order_by", []) if isinstance(item, dict)]
    tie_breakers = [item for item in contract.get("tie_breakers", []) if isinstance(item, dict)]
    projection = _string_list(contract.get("projection"))
    required = list(
        dict.fromkeys(
            [
                *partition_by,
                *[str(item.get("column") or "").strip() for item in order_by],
                *[str(item.get("column") or "").strip() for item in tie_breakers],
                *projection,
            ]
        )
    )
    missing = [column for column in required if column and column not in frame.columns]
    if missing:
        raise OutputContractError(
            "select_extreme_row_per_group 필수 컬럼이 없습니다: " + ", ".join(missing)
        )
    if not partition_by or not order_by:
        raise OutputContractError("select_extreme_row_per_group에는 partition_by와 order_by가 필요합니다.")
    limit_per_group = _positive_int(contract.get("limit_per_group"), 1)
    if limit_per_group < 1:
        raise OutputContractError("limit_per_group는 1 이상이어야 합니다.")
    tie_policy = str(contract.get("tie_policy") or "first").strip().lower()
    if tie_policy not in {"first", "include_all", "error"}:
        raise OutputContractError(f"지원하지 않는 tie_policy입니다: {tie_policy}")
    strict = bool(contract.get("strict", False))
    if strict and tie_policy == "first" and not tie_breakers:
        raise OutputContractError(
            "strict select_extreme_row_per_group의 tie_policy=first에는 검증된 tie_breakers가 필요합니다."
        )

    ordering = [*order_by, *tie_breakers]
    sort_columns = [str(item.get("column") or "").strip() for item in ordering]
    ascending = [str(item.get("direction") or "asc").strip().lower() == "asc" for item in ordering]
    working = frame.reset_index(drop=True).sort_values(
        sort_columns,
        ascending=ascending,
        kind="mergesort",
        na_position="last",
    )
    if tie_policy == "first":
        parts = []
        comparison_columns = [
            str(order_by[0].get("column") or "").strip(),
            *[str(item.get("column") or "").strip() for item in tie_breakers],
        ]
        for _, group in working.groupby(partition_by, dropna=False, sort=False):
            selected = group.head(limit_per_group)
            if strict and len(group) > limit_per_group:
                threshold = group.iloc[limit_per_group - 1]
                mask = _same_value_mask(group, threshold, comparison_columns, pd)
                if int(mask.sum()) > 1:
                    raise OutputContractError(
                        "tie_policy=first의 검증된 tie_breakers가 경계 동률을 해소하지 못했습니다."
                    )
            parts.append(selected)
        winners = pd.concat(parts, ignore_index=False) if parts else working.head(0)
    else:
        primary_order_column = str(order_by[0].get("column") or "").strip()
        error_columns = [
            primary_order_column,
            *[str(item.get("column") or "").strip() for item in tie_breakers],
        ]
        parts = []
        for _, group in working.groupby(partition_by, dropna=False, sort=False):
            if len(group) <= limit_per_group:
                parts.append(group)
                continue
            threshold_row = group.iloc[limit_per_group - 1]
            comparison_columns = error_columns if tie_policy == "error" else [primary_order_column]
            mask = _same_value_mask(group, threshold_row, comparison_columns, pd)
            tied = group[mask]
            if tie_policy == "error" and int(mask.sum()) > 1:
                raise OutputContractError(
                    "select_extreme_row_per_group에서 tie_breakers로 해소되지 않은 동률이 발생했습니다."
                )
            selected = group.head(limit_per_group)
            if tie_policy == "include_all":
                selected_indices = list(dict.fromkeys([*selected.index.tolist(), *tied.index.tolist()]))
                selected = group.loc[selected_indices]
            parts.append(selected)
        winners = pd.concat(parts, ignore_index=False) if parts else working.head(0)
    winners = winners.loc[:, projection].reset_index(drop=True)

    join = contract.get("join") if isinstance(contract.get("join"), dict) else {}
    if join:
        left_alias = str(join.get("left_source_alias") or "upstream_result").strip()
        left = sources.get(left_alias)
        if left is None:
            raise OutputContractError(f"continuation left source가 없습니다: {left_alias}")
        left_on = _string_list(join.get("left_on"))
        right_on = _string_list(join.get("right_on"))
        missing_left = [column for column in left_on if column not in left.columns]
        missing_right = [column for column in right_on if column not in winners.columns]
        if missing_left or missing_right:
            raise OutputContractError(
                "continuation join key가 없습니다: "
                + ", ".join([*missing_left, *missing_right])
            )
        if winners.duplicated(subset=right_on, keep=False).any():
            raise OutputContractError("extreme-row 결과가 join key 기준으로 유일하지 않습니다.")
        result = left.copy().merge(
            winners,
            how="left",
            left_on=left_on,
            right_on=right_on,
            suffixes=("", "_right"),
            sort=False,
        )
        redundant_right_keys = [
            right
            for left_key, right in zip(left_on, right_on)
            if left_key != right and right in result.columns
        ]
        if redundant_right_keys:
            result = result.drop(columns=redundant_right_keys)
    else:
        result = winners

    result_columns = _string_list(contract.get("result_columns"))
    if result_columns:
        missing_result = [column for column in result_columns if column not in result.columns]
        if missing_result:
            raise OutputContractError(
                "select_extreme_row_per_group 결과 컬럼이 없습니다: " + ", ".join(missing_result)
            )
        result = result.loc[:, result_columns]
    certificate = {
        "operation": "select_extreme_row_per_group",
        "source_alias": source_alias,
        "partition_by": partition_by,
        "order_by": deepcopy(order_by),
        "tie_breakers": deepcopy(tie_breakers),
        "tie_policy": tie_policy,
        "limit_per_group": limit_per_group,
        "same_row_projection": True,
        "winner_row_count": int(len(winners)),
        "predecessor_execution": predecessor_execution,
        "left_population_preserved": bool(join),
        "postcondition_validation": "passed",
    }
    return result.reset_index(drop=True), certificate


# 함수 설명: 한 기준 행과 여러 정렬 컬럼 값이 모두 같은 행을 null-safe 방식으로 판정합니다.
def _apply_deterministic_row_match_groups(
    target: Any,
    reference: Any,
    match_columns: list[str],
    pd: Any,
) -> tuple[Any, dict[str, Any]]:
    """Restrict a source to entity groups present in trusted previous rows."""

    def _actual_column(frame: Any, canonical: str) -> str:
        by_casefold = {str(column).casefold(): str(column) for column in frame.columns}
        return by_casefold.get(str(canonical).casefold(), "")

    def _key_value(value: Any) -> str:
        if value is None:
            return ""
        try:
            is_missing = pd.isna(value)
            if isinstance(is_missing, bool) and is_missing:
                return ""
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if text.casefold() in BLANK_MATCH_TEXTS:
            return ""
        if re.fullmatch(r"-?\d+\.0+", text):
            return text.split(".", 1)[0]
        return text

    pairs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for canonical in _string_list(match_columns):
        target_column = _actual_column(target, canonical)
        reference_column = _actual_column(reference, canonical)
        if not target_column or not reference_column:
            raise OutputContractError(
                "apply_row_match_groups 필수 컬럼을 찾을 수 없습니다: "
                + str(canonical)
            )
        pair_key = (target_column.casefold(), reference_column.casefold())
        if pair_key not in seen:
            seen.add(pair_key)
            pairs.append((canonical, target_column, reference_column))
    if not pairs:
        raise OutputContractError("apply_row_match_groups에는 매칭 컬럼이 필요합니다.")

    groups = {
        tuple(_key_value(row.get(reference_column)) for _, _, reference_column in pairs)
        for row in reference.to_dict(orient="records")
    }
    before = int(len(target))
    if groups:
        keys = [
            tuple(_key_value(row.get(target_column)) for _, target_column, _ in pairs)
            for row in target.to_dict(orient="records")
        ]
        mask = pd.Series([key in groups for key in keys], index=target.index, dtype=bool)
        filtered = target[mask].copy()
    else:
        filtered = target.iloc[0:0].copy()
    return filtered, {
        "match_columns": [item[0] for item in pairs],
        "resolved_columns": [
            {
                "canonical_column": canonical,
                "source_column": target_column,
                "reference_column": reference_column,
            }
            for canonical, target_column, reference_column in pairs
        ],
        "reference_row_count": int(len(reference)),
        "unique_condition_group_count": int(len(groups)),
        "source_row_count_before": before,
        "source_row_count_after": int(len(filtered)),
        "blank_policy": "normalize_blank",
    }


def _same_value_mask(frame: Any, threshold_row: Any, columns: list[str], pd: Any) -> Any:
    mask = pd.Series(True, index=frame.index)
    for column in columns:
        threshold = threshold_row[column]
        mask &= frame[column].isna() if pd.isna(threshold) else frame[column].eq(threshold)
    return mask


# 주요 함수: 검증된 V2 Fast recipe를 LLM 없이 고정 pandas 연산으로 실행합니다.
def _execute_fast_path_recipe(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
    *,
    runtime_intermediate_frames: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    source_alias = str(contract.get("source_alias") or "").strip()
    source = sources.get(source_alias)
    if source is None:
        raise OutputContractError(f"Fast Path source를 찾을 수 없습니다: {source_alias}")
    working = source.copy()
    intermediate_results: list[dict[str, Any]] = [
        _recorded_output(
            f"source:{source_alias}",
            working,
            "조회된 원본 데이터",
            "source_input",
        )
    ]
    source_row_count = int(len(working))
    all_filters = [item for item in contract.get("filters", []) if isinstance(item, dict)]
    retrieval_filters = [
        item for item in all_filters if str(item.get("execution_stage") or "") == "retrieval_pushdown"
    ]
    post_retrieval_filters = [item for item in all_filters if item not in retrieval_filters]
    working, filter_execution = _apply_fast_filters(
        working,
        post_retrieval_filters,
        pd,
        checkpoints=intermediate_results,
    )
    filter_execution = [
        {
            "canonical_field": str(item.get("canonical_field") or ""),
            "operator": str(item.get("operator") or "eq"),
            "values": deepcopy(item.get("typed_values") or []),
            "execution_stage": "retrieval_pushdown",
            "status": "already_applied",
        }
        for item in retrieval_filters
    ] + filter_execution
    filtered_row_count = int(len(working))
    if runtime_intermediate_frames is not None:
        runtime_intermediate_frames[f"filtered:{source_alias}"] = working.copy()
    if not post_retrieval_filters:
        intermediate_results.append(
            _recorded_output(
                f"filtered:{source_alias}",
                working,
                "필터 적용 후 데이터",
                "filtered_source",
            )
        )
    recipe = str(contract.get("recipe") or "").strip().lower()
    group_by = _string_list(contract.get("group_by"))
    metrics = [item for item in contract.get("metrics", []) if isinstance(item, dict)]
    calculation = contract.get("calculation") if isinstance(contract.get("calculation"), dict) else {}
    result_columns = _string_list(contract.get("result_columns"))
    projection = _string_list(contract.get("projection"))
    if recipe in {"detail_query", "latest_earliest", "distinct_summary"} and projection:
        # A stale/weak resolver may carry catalog defaults in result_columns;
        # the executable projection is the safer detail output shape.
        result_columns = list(dict.fromkeys(projection))
    elif recipe in {"detail_query", "latest_earliest", "distinct_summary"} and result_columns:
        available_detail_columns = [
            column for column in result_columns if column in working.columns
        ]
        if available_detail_columns:
            result_columns = available_detail_columns

    if recipe == "detail_query":
        detail_columns = projection or result_columns
        if not projection and detail_columns:
            available_detail_columns = [
                column for column in detail_columns if column in working.columns
            ]
            if available_detail_columns:
                detail_columns = available_detail_columns
        result = _fast_project(working, detail_columns)
    elif recipe == "scalar_summary":
        if str(calculation.get("scalar_operation") or "") == "count_rows":
            output_column = _fast_output_column(calculation, result_columns, metrics)
            result = pd.DataFrame([{output_column: int(len(working))}])
        else:
            result = _fast_aggregate(working, [], metrics, pd)
    elif recipe in {"group_summary", "ranked_summary", "list_summary"}:
        result = _fast_aggregate(working, group_by, metrics, pd)
    elif recipe == "frequency_summary":
        result = _fast_frequency(working, group_by, metrics, result_columns, pd)
    elif recipe == "distinct_summary":
        result = _fast_distinct(working, group_by, _string_list(contract.get("projection")), result_columns)
    elif recipe == "existence_summary":
        output_column = _fast_output_column(calculation, result_columns, metrics)
        result = pd.DataFrame([{output_column: bool(len(working) > 0)}])
    elif recipe == "quality_summary":
        result = _fast_quality_summary(working, calculation, pd)
    elif recipe == "latest_earliest":
        detail_columns = _string_list(contract.get("projection")) or result_columns
        if not projection and detail_columns:
            available_detail_columns = [
                column for column in detail_columns if column in working.columns
            ]
            if available_detail_columns:
                detail_columns = available_detail_columns
        result = _fast_project(working, detail_columns)
    elif recipe == "percent_of_total":
        result = _fast_percent_of_total(working, group_by, metrics, calculation, pd)
    elif recipe == "rank_within_group":
        result = _fast_rank_within_group(working, group_by, metrics, calculation, contract, pd)
    elif recipe == "threshold_after_aggregate":
        result = _fast_aggregate(working, group_by, metrics, pd)
        result = _apply_fast_post_filters(result, contract.get("post_filters"), pd)
    elif recipe == "time_bucket_summary":
        result = _fast_time_bucket_summary(working, group_by, metrics, calculation, pd)
    elif recipe == "period_change":
        result = _fast_period_change(working, group_by, metrics, calculation, pd)
    elif recipe == "running_total":
        result = _fast_running_total(working, group_by, metrics, calculation, pd)
    elif recipe == "moving_aggregate":
        result = _fast_moving_aggregate(working, group_by, metrics, calculation, pd)
    elif recipe == "percentile_summary":
        result = _fast_percentile_summary(working, group_by, metrics, calculation, pd)
    elif recipe == "pivot_summary":
        result = _fast_pivot_summary(working, calculation, pd)
    else:
        raise OutputContractError(f"지원하지 않는 Fast Path recipe입니다: {recipe}")

    result = _apply_fast_ordering_and_limit(result, contract)
    intermediate_results.append(
        _recorded_output(
            "computed_result",
            result,
            "최종 계약 적용 전 계산 결과",
            "computed_result",
        )
    )
    if recipe != "pivot_summary" and result_columns:
        missing = [column for column in result_columns if column not in result.columns]
        if missing:
            raise OutputContractError(
                "Fast Path 결과 계약에 필요한 컬럼이 없습니다: " + ", ".join(missing)
            )
        result = result[result_columns].copy()
    result = result.reset_index(drop=True)
    certificate = {
        "operation": "execute_fast_path_recipe",
        "recipe": recipe,
        "source_alias": source_alias,
        "source_row_count": source_row_count,
        "filtered_row_count": filtered_row_count,
        "result_row_count": int(len(result)),
        "result_columns": [str(column) for column in result.columns],
        "filter_execution": filter_execution,
        "deterministic_function": _fast_function_trace(contract),
        "postcondition_validation": "passed",
        "intermediate_results": intermediate_results[-12:],
    }
    return result, certificate


# 함수 설명: `_apply_fast_filters()`는 17 V2 Hybrid 분석 실행기 처리 중 FAST·필터 관련 값을 계산·변환하는 내부 helper입니다.
def _apply_fast_filters(
    frame: Any,
    conditions: list[dict[str, Any]],
    pd: Any,
    checkpoints: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    result = frame.copy()
    execution: list[dict[str, Any]] = []
    for condition in conditions:
        field = str(condition.get("canonical_field") or "").strip()
        operator = str(condition.get("operator") or "eq").strip().lower()
        values = condition.get("typed_values") if isinstance(condition.get("typed_values"), list) else []
        if not field or field not in result.columns:
            raise OutputContractError(f"Fast Path filter canonical 컬럼이 없습니다: {field}")
        before = int(len(result))
        series = result[field]
        mask = _fast_filter_mask(series, operator, values, pd)
        result = result[mask].copy()
        execution.append(
            {
                "canonical_field": field,
                "operator": operator,
                "values": deepcopy(values),
                "execution_stage": "post_retrieval",
                "status": "applied",
                "row_count_before": before,
                "row_count_after": int(len(result)),
            }
        )
        if checkpoints is not None:
            checkpoints.append(_recorded_output(
                f"filtered_after:{field}",
                result,
                f"{field} 필터 적용 후 데이터",
                "filtered_source",
            ))
    return result, execution


# 함수 설명: `_fast_filter_mask()`는 17 V2 Hybrid 분석 실행기 처리 중 필터·MASK 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_filter_mask(series: Any, operator: str, values: list[Any], pd: Any) -> Any:
    blank = series.isna() | series.astype(str).str.strip().str.casefold().isin(BLANK_MATCH_TEXTS)
    if operator in {"is_null", "is_empty", "null_or_empty"}:
        return blank
    if operator in {"not_null", "not_empty", "not_blank"}:
        return ~blank
    if operator in {"or", "any"}:
        mask = pd.Series(False, index=series.index)
        for item in values:
            if not isinstance(item, dict):
                continue
            nested_values = item.get("values") if isinstance(item.get("values"), list) else [item.get("value")]
            mask = mask | _fast_filter_mask(series, str(item.get("operator") or item.get("op") or "eq"), nested_values, pd)
        return mask
    normalized_series, normalized_values = _fast_comparable_values(series, values, pd)
    if operator in {"eq", "in"}:
        return normalized_series.isin(normalized_values)
    if operator in {"ne", "not_in"}:
        return ~normalized_series.isin(normalized_values)
    if operator in {"gt", "ge", "lt", "le"}:
        numeric_series = pd.to_numeric(series, errors="coerce")
        numeric_value = pd.to_numeric(pd.Series(values[:1]), errors="coerce").iloc[0] if values else float("nan")
        if pd.isna(numeric_value):
            return pd.Series(False, index=series.index)
        return {
            "gt": numeric_series > numeric_value,
            "ge": numeric_series >= numeric_value,
            "lt": numeric_series < numeric_value,
            "le": numeric_series <= numeric_value,
        }[operator].fillna(False)
    text = series.astype(str)
    mask = pd.Series(False, index=series.index)
    for value in values:
        if operator in {"contains", "like"}:
            mask = mask | text.str.contains(str(value), case=False, na=False, regex=False)
        elif operator == "starts_with":
            mask = mask | text.str.startswith(str(value), na=False)
        elif operator == "ends_with":
            mask = mask | text.str.endswith(str(value), na=False)
        else:
            raise OutputContractError(f"지원하지 않는 Fast Path filter 연산자입니다: {operator}")
    return mask


# 함수 설명: `_fast_comparable_values()`는 17 V2 Hybrid 분석 실행기 처리 중 comparable·값 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_comparable_values(series: Any, values: list[Any], pd: Any) -> tuple[Any, list[Any]]:
    if values and all(_looks_like_date_value(value) for value in values):
        return series.map(_normalize_date_identifier), [_normalize_date_identifier(value) for value in values]
    return series, values


# 함수 설명: `_looks_like_date_value()`는 입력값이 LIKE·날짜·값 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _looks_like_date_value(value: Any) -> bool:
    text = str(value or "").strip()
    compact = re.sub(r"[^0-9]", "", text)
    return len(compact) == 8 and compact.isdigit()


# 함수 설명: `_fast_project()`는 17 V2 Hybrid 분석 실행기 처리 중 project 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_project(frame: Any, columns: list[str]) -> Any:
    if not columns:
        return frame.copy()
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise OutputContractError("Fast Path projection canonical 컬럼이 없습니다: " + ", ".join(missing))
    return frame[columns].copy()


# 함수 설명: `_fast_aggregate()`는 17 V2 Hybrid 분석 실행기 처리 중 aggregate 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_aggregate(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], pd: Any) -> Any:
    metric_columns = {
        str(metric.get("source_column") or "").strip().casefold()
        for metric in metrics
        if isinstance(metric, dict)
    } | {
        str(metric.get("output_column") or "").strip().casefold()
        for metric in metrics
        if isinstance(metric, dict)
    }
    # A metric cannot also be a grouping key. LLM plans occasionally copy a
    # requested detail column into grain_columns; keeping it would make
    # pandas reset_index() create the same column twice. The metric contract
    # is authoritative, so remove the conflicting dimension generically.
    effective_group_by = [
        column
        for column in group_by
        if str(column).strip().casefold() not in metric_columns
    ]
    missing_group = [column for column in effective_group_by if column not in frame.columns]
    if missing_group:
        raise OutputContractError("Fast Path grain canonical 컬럼이 없습니다: " + ", ".join(missing_group))
    if not metrics:
        raise OutputContractError("Fast Path 집계 metric 계약이 없습니다.")
    working = frame.copy()
    named: dict[str, Any] = {}
    for metric in metrics:
        source_column = str(metric.get("source_column") or "").strip()
        output_column = str(metric.get("output_column") or "").strip()
        method = str(metric.get("aggregation") or "").strip().lower()
        if source_column not in working.columns or not output_column:
            raise OutputContractError("Fast Path metric source 또는 output 컬럼이 유효하지 않습니다.")
        if method in {"sum", "mean", "median", "min", "max"}:
            working[source_column] = pd.to_numeric(working[source_column], errors="coerce")
        named[output_column] = pd.NamedAgg(column=source_column, aggfunc=_pandas_aggregation_method(method))
    output_columns = [*effective_group_by, *named]
    if working.empty:
        return pd.DataFrame(columns=output_columns)
    if effective_group_by:
        return working.groupby(effective_group_by, dropna=False).agg(**named).reset_index()
    row: dict[str, Any] = {}
    for output_column, aggregation in named.items():
        row[output_column] = working[str(aggregation.column)].agg(aggregation.aggfunc)
    return pd.DataFrame([row], columns=output_columns)


# 함수 설명: `_fast_frequency()`는 17 V2 Hybrid 분석 실행기 처리 중 frequency 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_frequency(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], result_columns: list[str], pd: Any) -> Any:
    keys = group_by or [column for column in result_columns if column in frame.columns]
    if not keys:
        raise OutputContractError("frequency_summary의 기준 컬럼이 없습니다.")
    missing = [column for column in keys if column not in frame.columns]
    if missing:
        raise OutputContractError("frequency_summary canonical 컬럼이 없습니다: " + ", ".join(missing))
    output_column = str(metrics[0].get("output_column") or "") if metrics else next((column for column in result_columns if column not in keys), "")
    if not output_column:
        raise OutputContractError("frequency_summary의 count output 컬럼이 없습니다.")
    return frame.groupby(keys, dropna=False).size().reset_index(name=output_column)


# 함수 설명: `_fast_distinct()`는 17 V2 Hybrid 분석 실행기 처리 중 distinct 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_distinct(frame: Any, group_by: list[str], projection: list[str], result_columns: list[str]) -> Any:
    columns = projection or group_by or [column for column in result_columns if column in frame.columns]
    if not columns:
        raise OutputContractError("distinct_summary의 대상 컬럼이 없습니다.")
    return _fast_project(frame, columns).drop_duplicates().reset_index(drop=True)


# 함수 설명: `_fast_quality_summary()`는 quality·요약의 건수·조건·상태를 진단과 답변에 쓸 짧은 요약으로 만듭니다.
def _fast_quality_summary(frame: Any, calculation: dict[str, Any], pd: Any) -> Any:
    columns = _string_list(calculation.get("quality_columns"))
    output_column = str(calculation.get("output_column") or "").strip()
    check = str(calculation.get("quality_check") or "").strip().lower()
    missing = [column for column in columns if column not in frame.columns]
    if missing or not columns or not output_column:
        raise OutputContractError("quality_summary 계약이 유효하지 않습니다.")
    subset = frame[columns]
    if check in {"null_count", "null_rate"}:
        mask = subset.isna().any(axis=1)
    elif check in {"blank_count", "blank_rate"}:
        mask = subset.isna() | subset.astype(str).apply(lambda col: col.str.strip().str.casefold().isin(BLANK_MATCH_TEXTS))
        mask = mask.any(axis=1)
    elif check in {"duplicate_count", "duplicate_rate"}:
        policy = str(calculation.get("duplicate_policy") or "").strip().lower()
        if policy not in {"all_rows", "excess_rows"}:
            raise OutputContractError("duplicate quality 검사에는 duplicate_policy가 필요합니다.")
        mask = subset.duplicated(keep=False if policy == "all_rows" else "first")
    else:
        raise OutputContractError(f"지원하지 않는 quality check입니다: {check}")
    value = float(mask.mean()) if check.endswith("_rate") and len(frame) else (0.0 if check.endswith("_rate") else int(mask.sum()))
    return pd.DataFrame([{output_column: value}])


# 함수 설명: `_fast_percent_of_total()`는 17 V2 Hybrid 분석 실행기 처리 중 percent·OF·total 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_percent_of_total(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], pd: Any) -> Any:
    result = _fast_aggregate(frame, group_by, metrics, pd)
    metric_column = str(metrics[0].get("output_column") or "")
    output_column = str(calculation.get("output_column") or "").strip()
    partitions = _string_list(calculation.get("partition_by"))
    scope = str(calculation.get("denominator_scope") or "").strip()
    if scope == "partition_total":
        missing = [column for column in partitions if column not in result.columns]
        if missing or not partitions:
            raise OutputContractError("partition 구성비의 partition_by 계약이 유효하지 않습니다.")
        denominator = result.groupby(partitions, dropna=False)[metric_column].transform("sum")
    elif scope == "grand_total":
        denominator = pd.Series(result[metric_column].sum(), index=result.index)
    else:
        raise OutputContractError("percent_of_total denominator_scope가 유효하지 않습니다.")
    numerator = pd.to_numeric(result[metric_column], errors="coerce")
    denominator = pd.to_numeric(denominator, errors="coerce")
    ratio = numerator / denominator.where(denominator != 0)
    if str(calculation.get("zero_division_policy") or "") == "zero":
        ratio = ratio.fillna(0)
    result[output_column] = ratio
    return result


# 함수 설명: `_fast_rank_within_group()`는 17 V2 Hybrid 분석 실행기 처리 중 RANK·within·그룹 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_rank_within_group(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], contract: dict[str, Any], pd: Any) -> Any:
    result = _fast_aggregate(frame, group_by, metrics, pd)
    output_column = str(calculation.get("output_column") or "").strip()
    partitions = _string_list(calculation.get("partition_by"))
    ordering = contract.get("ordering") if isinstance(contract.get("ordering"), list) else []
    first_ordering = ordering[0] if ordering and isinstance(ordering[0], dict) else {}
    sort_column = str(first_ordering.get("column") or "").strip() if first_ordering else str(metrics[0].get("output_column") or "")
    ascending = str(first_ordering.get("direction") or "desc") == "asc" if first_ordering else False
    method = {"row_number": "first", "rank": "min", "dense_rank": "dense"}.get(str(calculation.get("rank_method") or ""))
    if not method or sort_column not in result.columns:
        raise OutputContractError("rank_within_group 정렬 또는 rank_method 계약이 유효하지 않습니다.")
    if partitions:
        result[output_column] = result.groupby(partitions, dropna=False)[sort_column].rank(method=method, ascending=ascending)
    else:
        result[output_column] = result[sort_column].rank(method=method, ascending=ascending)
    return result


# 함수 설명: `_fast_time_bucket_summary()`는 TIME·bucket·요약의 건수·조건·상태를 진단과 답변에 쓸 짧은 요약으로 만듭니다.
def _fast_time_bucket_summary(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], pd: Any) -> Any:
    working = frame.copy()
    time_column = str(calculation.get("time_column") or "").strip()
    bucket_column = str(calculation.get("time_bucket_column") or "").strip()
    if time_column not in working.columns or not bucket_column:
        raise OutputContractError("time bucket canonical 컬럼 계약이 유효하지 않습니다.")
    parsed = _fast_datetime_series(working[time_column], pd, calculation.get("timezone"))
    frequency = {"day": "D", "week": "W", "month": "M", "quarter": "Q", "year": "Y"}.get(str(calculation.get("frequency") or ""))
    if not frequency:
        raise OutputContractError("지원하지 않는 time bucket frequency입니다.")
    # Period uses left-closed boundaries. For a right-closed contract, an
    # observation exactly on a boundary belongs to the preceding period.
    bucket_basis = parsed
    if getattr(bucket_basis.dt, "tz", None) is not None:
        bucket_basis = bucket_basis.dt.tz_localize(None)
    if str(calculation.get("closed") or "").strip() == "right":
        bucket_basis = bucket_basis - pd.to_timedelta(1, unit="ns")
    period = bucket_basis.dt.to_period(frequency)
    label = str(calculation.get("label") or "").strip()
    working[bucket_column] = period.dt.end_time if label == "right" else period.dt.start_time
    effective_group = [column for column in group_by if column != time_column]
    if bucket_column not in effective_group:
        effective_group.append(bucket_column)
    return _fast_aggregate(working, effective_group, metrics, pd)


# 함수 설명: `_fast_period_change()`는 17 V2 Hybrid 분석 실행기 처리 중 period·change 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_period_change(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], pd: Any) -> Any:
    result = _fast_aggregate(frame, group_by, metrics, pd)
    time_column = str(calculation.get("time_column") or "").strip()
    metric_column = str(metrics[0].get("output_column") or "")
    output_column = str(calculation.get("output_column") or "").strip()
    partitions = _string_list(calculation.get("partition_by"))
    if time_column not in result.columns:
        raise OutputContractError("period_change 결과에 시간 컬럼이 없습니다.")
    result[time_column] = _fast_datetime_series(result[time_column], pd, calculation.get("timezone"))
    result = result.sort_values([*partitions, time_column] if partitions else [time_column], kind="mergesort")
    periods = int(calculation.get("periods") or 1)
    previous = result.groupby(partitions, dropna=False)[metric_column].shift(periods) if partitions else result[metric_column].shift(periods)
    current = pd.to_numeric(result[metric_column], errors="coerce")
    if str(calculation.get("change_method") or "") == "percent":
        value = (current - previous) / previous.where(previous != 0)
        if str(calculation.get("zero_division_policy") or "") == "zero":
            value = value.fillna(0)
    else:
        value = current - previous
    result[output_column] = value
    return result


# 함수 설명: `_fast_running_total()`는 17 V2 Hybrid 분석 실행기 처리 중 running·total 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_running_total(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], pd: Any) -> Any:
    result = _fast_aggregate(frame, group_by, metrics, pd)
    time_column = str(calculation.get("time_column") or "").strip()
    metric_column = str(metrics[0].get("output_column") or "")
    output_column = str(calculation.get("output_column") or "").strip()
    partitions = _string_list(calculation.get("partition_by"))
    if time_column not in result.columns:
        raise OutputContractError("running_total 결과에 시간 컬럼이 없습니다.")
    result[time_column] = _fast_datetime_series(result[time_column], pd, calculation.get("timezone"))
    result = result.sort_values([*partitions, time_column] if partitions else [time_column], kind="mergesort")
    values = pd.to_numeric(result[metric_column], errors="coerce").fillna(0)
    result[output_column] = values.groupby([result[column] for column in partitions], dropna=False).cumsum() if partitions else values.cumsum()
    return result


# 함수 설명: `_fast_moving_aggregate()`는 17 V2 Hybrid 분석 실행기 처리 중 moving·aggregate 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_moving_aggregate(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], pd: Any) -> Any:
    result = _fast_aggregate(frame, group_by, metrics, pd)
    time_column = str(calculation.get("time_column") or "").strip()
    metric_column = str(metrics[0].get("output_column") or "")
    output_column = str(calculation.get("output_column") or "").strip()
    partitions = _string_list(calculation.get("partition_by"))
    window = int(calculation.get("window") or 0)
    min_periods = int(calculation.get("min_periods") or 1)
    method = str(calculation.get("moving_method") or metrics[0].get("aggregation") or "mean").strip().lower()
    if time_column not in result.columns or window <= 0 or method not in {"sum", "mean", "min", "max", "median"}:
        raise OutputContractError("moving_aggregate 계약이 유효하지 않습니다.")
    result[time_column] = _fast_datetime_series(result[time_column], pd, calculation.get("timezone"))
    result = result.sort_values([*partitions, time_column] if partitions else [time_column], kind="mergesort")
    values = pd.to_numeric(result[metric_column], errors="coerce")
    if partitions:
        rolled = values.groupby([result[column] for column in partitions], dropna=False).rolling(window, min_periods=min_periods)
        result[output_column] = getattr(rolled, method)().reset_index(level=list(range(len(partitions))), drop=True)
    else:
        result[output_column] = getattr(values.rolling(window, min_periods=min_periods), method)()
    return result


# 함수 설명: `_fast_datetime_series()`는 17 V2 Hybrid 분석 실행기 처리 중 datetime·series 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_datetime_series(series: Any, pd: Any, timezone: Any = "") -> Any:
    """Parse a time column strictly enough for deterministic time recipes."""

    parsed = pd.to_datetime(series, errors="coerce")
    original_present = ~(series.isna() | series.astype(str).str.strip().str.casefold().isin(BLANK_MATCH_TEXTS))
    if bool((original_present & parsed.isna()).any()):
        raise OutputContractError("Fast Path 시간 컬럼에 해석할 수 없는 값이 있습니다.")
    timezone_name = str(timezone or "").strip()
    if timezone_name:
        try:
            if getattr(parsed.dt, "tz", None) is None:
                parsed = parsed.dt.tz_localize(timezone_name, ambiguous="NaT", nonexistent="NaT")
            else:
                parsed = parsed.dt.tz_convert(timezone_name)
        except Exception as exc:
            raise OutputContractError(f"Fast Path timezone 계약을 적용할 수 없습니다: {timezone_name}") from exc
    return parsed


# 함수 설명: `_fast_percentile_summary()`는 percentile·요약의 건수·조건·상태를 진단과 답변에 쓸 짧은 요약으로 만듭니다.
def _fast_percentile_summary(frame: Any, group_by: list[str], metrics: list[dict[str, Any]], calculation: dict[str, Any], pd: Any) -> Any:
    metric = metrics[0]
    source_column = str(metric.get("source_column") or "")
    output_column = str(metric.get("output_column") or "")
    values = frame.copy()
    values[source_column] = pd.to_numeric(values[source_column], errors="coerce")
    percentile = float(calculation.get("percentile"))
    interpolation = "linear" if str(calculation.get("percentile_method") or "") == "continuous" else "higher"
    if group_by:
        return values.groupby(group_by, dropna=False)[source_column].quantile(percentile, interpolation=interpolation).reset_index(name=output_column)
    return pd.DataFrame([{output_column: values[source_column].quantile(percentile, interpolation=interpolation)}])


# 함수 설명: `_fast_pivot_summary()`는 pivot·요약의 건수·조건·상태를 진단과 답변에 쓸 짧은 요약으로 만듭니다.
def _fast_pivot_summary(frame: Any, calculation: dict[str, Any], pd: Any) -> Any:
    index = _string_list(calculation.get("pivot_index"))
    columns = _string_list(calculation.get("pivot_columns"))
    values = _string_list(calculation.get("pivot_values"))
    method = str(calculation.get("pivot_aggregation") or "").strip().lower()
    required = [*index, *columns, *values]
    missing = [column for column in required if column not in frame.columns]
    if missing or not columns or not values:
        raise OutputContractError("pivot_summary canonical 컬럼 계약이 유효하지 않습니다: " + ", ".join(missing))
    aggfunc = _pandas_aggregation_method(method)
    result = pd.pivot_table(
        frame,
        index=index or None,
        columns=columns,
        values=values[0] if len(values) == 1 else values,
        aggfunc=aggfunc,
        fill_value=calculation.get("pivot_fill_value"),
        dropna=False,
    )
    if hasattr(result.columns, "to_flat_index"):
        result.columns = [" | ".join(str(part) for part in item if str(part) != "") if isinstance(item, tuple) else str(item) for item in result.columns.to_flat_index()]
    result = result.reset_index()
    maximum = int(calculation.get("max_pivot_columns") or 0)
    if maximum <= 0 or len(result.columns) > maximum:
        raise OutputContractError(f"pivot_summary 결과 컬럼 수가 허용 범위를 초과했습니다: {len(result.columns)}/{maximum}")
    return result


# 함수 설명: `_apply_fast_post_filters()`는 17 V2 Hybrid 분석 실행기 처리 중 FAST·POST·필터 관련 값을 계산·변환하는 내부 helper입니다.
def _apply_fast_post_filters(frame: Any, conditions: Any, pd: Any) -> Any:
    result = frame.copy()
    for condition in conditions if isinstance(conditions, list) else []:
        if not isinstance(condition, dict):
            continue
        column = str(condition.get("column") or "").strip()
        operator = str(condition.get("operator") or "").strip()
        if column not in result.columns:
            raise OutputContractError(f"집계 후 filter 결과 컬럼이 없습니다: {column}")
        value = condition.get("value")
        series = pd.to_numeric(result[column], errors="coerce")
        target = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        if pd.isna(target) or operator not in {"gt", "ge", "lt", "le", "eq", "ne"}:
            raise OutputContractError("집계 후 threshold 계약이 유효하지 않습니다.")
        mask = {
            "gt": series > target, "ge": series >= target, "lt": series < target,
            "le": series <= target, "eq": series == target, "ne": series != target,
        }[operator]
        result = result[mask.fillna(False)].copy()
    return result


# 함수 설명: `_apply_fast_ordering_and_limit()`는 17 V2 Hybrid 분석 실행기 처리 중 FAST·ordering·AND·제한값 관련 값을 계산·변환하는 내부
#        helper입니다.
def _apply_fast_ordering_and_limit(frame: Any, contract: dict[str, Any]) -> Any:
    result = frame.copy()
    ordering = contract.get("ordering") if isinstance(contract.get("ordering"), list) else []
    sort_columns: list[str] = []
    ascending: list[bool] = []
    for item in ordering:
        if not isinstance(item, dict):
            continue
        column = str(item.get("column") or "").strip()
        if column and column in result.columns:
            sort_columns.append(column)
            ascending.append(str(item.get("direction") or "asc") == "asc")
    if sort_columns:
        result = result.sort_values(sort_columns, ascending=ascending, na_position="last", kind="mergesort")
    try:
        limit = max(0, int(contract.get("limit") or 0))
    except Exception:
        limit = 0
    if limit and len(result) > limit:
        if str(contract.get("tie_policy") or "") == "include_all" and sort_columns:
            boundary = result.iloc[limit - 1][sort_columns[0]]
            result = result[result[sort_columns[0]] >= boundary] if ascending[0] is False else result[result[sort_columns[0]] <= boundary]
        else:
            result = result.head(limit)
    return result.reset_index(drop=True)


# 함수 설명: `_fast_output_column()`는 17 V2 Hybrid 분석 실행기 처리 중 output·컬럼 관련 값을 계산·변환하는 내부 helper입니다.
def _fast_output_column(calculation: dict[str, Any], result_columns: list[str], metrics: list[dict[str, Any]]) -> str:
    output = str(calculation.get("output_column") or "").strip()
    if output:
        return output
    metric_outputs = [str(item.get("output_column") or "").strip() for item in metrics]
    candidates = [column for column in result_columns if column not in metric_outputs]
    if metric_outputs:
        return metric_outputs[0]
    if candidates:
        return candidates[-1]
    raise OutputContractError("Fast Path 결과 output 컬럼을 확정할 수 없습니다.")


# 함수 설명: `_apply_fast_result_contract()`는 17 V2 Hybrid 분석 실행기 처리 중 FAST·결과·contract 관련 값을 계산·변환하는 내부 helper입니다.
def _apply_fast_result_contract(payload: dict[str, Any], result: Any, certificate: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    output_contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    fast_plan = payload.get("simple_analysis_contract") if isinstance(payload.get("simple_analysis_contract"), dict) else {}
    recipe = str(fast_plan.get("recipe") or "").strip().lower()
    projection = _string_list(fast_plan.get("projection"))
    # The explicit Fast projection owns detail-like output shape.  This keeps
    # catalog defaults that a weak model copied into the generic contract from
    # becoming false missing-column failures after the selected frame exists.
    if recipe in {"detail_query", "latest_earliest", "distinct_summary"} and hasattr(result, "columns"):
        actual_columns = [str(column) for column in result.columns]
        declared_columns = _string_list(fast_plan.get("result_columns"))
        requested_columns = projection or declared_columns
        reconciled_columns = [column for column in requested_columns if column in actual_columns]
        if reconciled_columns:
            output_contract["result_columns"] = reconciled_columns
            output_contract["required_columns"] = reconciled_columns
            output_contract["strict_result_columns"] = True
            output_contract["contract_reconciliation"] = {
                "status": "applied",
                "policy": (
                    "explicit_fast_projection_owns_detail_shape"
                    if projection
                    else "available_detail_columns_own_shape"
                ),
                "columns": reconciled_columns,
                "dropped_unavailable_columns": [
                    column for column in requested_columns if column not in actual_columns
                ],
            }
    if str(fast_plan.get("result_schema_mode") or "") == "derived_bounded" and hasattr(result, "columns"):
        columns = [str(column) for column in result.columns]
        output_contract["result_columns"] = columns
        output_contract["required_columns"] = columns
        output_contract["strict_result_columns"] = True
        plan["output_contract"] = output_contract
        payload["intent_plan"] = plan
    return payload


# 함수 설명: 단일 표준 source의 projection 또는 집계를 LLM 코드 없이 strict output contract대로 실행합니다.
def _execute_single_source_contract(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
) -> Any:
    source_alias = str(contract.get("source_alias") or "").strip()
    source = sources.get(source_alias)
    if source is None:
        raise OutputContractError(f"단일 source 실행 계약의 source를 찾을 수 없습니다: {source_alias}")
    working = source.copy()
    operation = str(contract.get("operation") or "").strip()
    if operation == "project_single_source":
        result_columns = _string_list(contract.get("result_columns"))
        missing = [column for column in result_columns if column not in working.columns]
        if missing:
            raise OutputContractError(
                "단일 source projection에 필요한 표준 컬럼이 없습니다: " + ", ".join(missing)
            )
        return working[result_columns].copy()

    grain_columns = _string_list(contract.get("grain_columns"))
    metrics = [item for item in contract.get("metrics", []) if isinstance(item, dict)]
    if not metrics:
        raise OutputContractError("단일 source 집계 계약에 metric이 없습니다.")
    missing_grain = [column for column in grain_columns if column not in working.columns]
    if missing_grain:
        raise OutputContractError(
            "단일 source 집계에 필요한 grain 컬럼이 없습니다: " + ", ".join(missing_grain)
        )
    named_aggregations: dict[str, Any] = {}
    for metric in metrics:
        source_column = str(metric.get("source_column") or "").strip()
        output_column = str(metric.get("output_column") or "").strip()
        if not source_column or source_column not in working.columns or not output_column:
            raise OutputContractError("단일 source 집계 metric 컬럼 또는 출력명이 유효하지 않습니다.")
        named_aggregations[output_column] = pd.NamedAgg(
            column=source_column,
            aggfunc=_pandas_aggregation_method(metric.get("aggregation") or "sum"),
        )
    output_columns = [*grain_columns, *named_aggregations]
    if working.empty:
        return pd.DataFrame(columns=output_columns)
    if grain_columns:
        return (
            working.groupby(grain_columns, dropna=False)
            .agg(**named_aggregations)
            .reset_index()
        )
    row: dict[str, Any] = {}
    for output_column, aggregation in named_aggregations.items():
        source_column = str(aggregation.column)
        row[output_column] = working[source_column].agg(aggregation.aggfunc)
    return pd.DataFrame([row], columns=output_columns)


# 함수 설명: `_apply_deterministic_result_ordering()`는 17 pandas 실행/1회 복구기 처리 중 deterministic·결과·ordering 관련 값을 계산·변환하는
#        내부 helper입니다.
def _execute_typed_pandas_plan(
    contract: dict[str, Any],
    sources: dict[str, Any],
    pd: Any,
    *,
    runtime_intermediate_frames: dict[str, Any] | None = None,
) -> Any:
    """Execute the narrow, validated DataFrame DAG used by deterministic Complex mode."""

    steps = [item for item in contract.get("steps", []) if isinstance(item, dict)]
    if not steps:
        raise OutputContractError("Typed Pandas 실행 계약에 단계가 없습니다.")
    frames = {
        str(alias): frame.copy()
        for alias, frame in sources.items()
        if hasattr(frame, "columns")
    }
    last_result: Any = None
    for index, step in enumerate(steps, start=1):
        operation = str(step.get("operation") or step.get("step") or "").strip().lower()
        node_id = str(step.get("node_id") or "").strip()
        output_alias = str(step.get("output_alias") or node_id).strip()
        inputs = [item for item in step.get("inputs", []) if isinstance(item, dict)]
        input_frames: list[Any] = []
        for item in inputs:
            reference = str(item.get("ref") or "").strip()
            frame = frames.get(reference)
            if frame is None:
                raise OutputContractError(
                    "Typed Pandas 단계 입력을 찾을 수 없습니다: " + reference
                )
            input_frames.append(frame.copy())
        if operation == "apply_filters":
            result = _apply_typed_step_filters(input_frames[0], step)
        elif operation == "select_columns":
            projection = _string_list(step.get("projection") or step.get("columns"))
            missing = [column for column in projection if column not in input_frames[0].columns]
            if missing:
                raise OutputContractError(
                    "Typed Pandas projection 컬럼을 찾을 수 없습니다: " + ", ".join(missing)
                )
            result = input_frames[0][projection].copy()
        elif operation == "groupby_and_aggregate":
            result = _typed_groupby_and_aggregate(input_frames[0], step, pd)
        elif operation == "sort_and_top_n":
            result = _typed_sort_and_top_n(input_frames[0], step)
        elif operation == "join":
            result = _typed_join_frames(input_frames[0], input_frames[1], step, pd)
        else:
            raise OutputContractError("지원하지 않는 Typed Pandas 연산입니다: " + operation)
        if not hasattr(result, "columns"):
            raise OutputContractError("Typed Pandas 단계 결과가 DataFrame이 아닙니다.")
        frames[node_id] = result
        frames[output_alias] = result
        last_result = result
        if runtime_intermediate_frames is not None:
            runtime_intermediate_frames[f"typed_step:{index}:{output_alias}"] = result.copy()
    return last_result


def _apply_typed_step_filters(frame: Any, step: dict[str, Any]) -> Any:
    """Apply only explicit, source-local filter conditions from a Typed IR step."""

    raw_filters = step.get("filters")
    if not isinstance(raw_filters, dict) or not raw_filters:
        return frame.copy()
    result = frame.copy()
    for field, raw_condition in raw_filters.items():
        column = str(field or "").strip()
        if not column or column not in result.columns:
            raise OutputContractError("Typed Pandas filter 컬럼을 찾을 수 없습니다: " + column)
        condition = raw_condition if isinstance(raw_condition, dict) else {"value": raw_condition}
        operator = str(condition.get("operator") or "eq").strip().lower().replace("-", "_")
        raw_values = condition.get("value", condition.get("values"))
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        values = [value for value in values if value is not None]
        series = result[column]
        text = series.fillna("").astype(str).str.strip()
        if operator == "eq":
            if len(values) != 1:
                raise OutputContractError("Typed Pandas eq filter에는 값 하나가 필요합니다.")
            mask = text.eq(str(values[0]).strip())
        elif operator == "in":
            mask = text.isin({str(value).strip() for value in values})
        elif operator == "starts_with":
            if len(values) != 1:
                raise OutputContractError("Typed Pandas starts_with filter에는 값 하나가 필요합니다.")
            mask = text.str.startswith(str(values[0]).strip(), na=False)
        elif operator in {"contains", "like"}:
            if len(values) != 1:
                raise OutputContractError("Typed Pandas contains filter에는 값 하나가 필요합니다.")
            mask = text.str.contains(re.escape(str(values[0]).strip()), na=False)
        elif operator in {"not_blank", "not_empty"}:
            mask = text.ne("")
        elif operator in {"is_null", "is_empty", "null_or_empty"}:
            mask = series.isna() | text.eq("")
        elif operator == "not_null":
            mask = ~(series.isna() | text.eq(""))
        else:
            raise OutputContractError("지원하지 않는 Typed Pandas filter 연산입니다: " + operator)
        result = result[mask].copy()
    return result


def _typed_groupby_and_aggregate(frame: Any, step: dict[str, Any], pd: Any) -> Any:
    group_by = _string_list(step.get("group_by") or step.get("group_by_columns"))
    missing_group = [column for column in group_by if column not in frame.columns]
    if missing_group:
        raise OutputContractError("Typed Pandas group_by 컬럼을 찾을 수 없습니다: " + ", ".join(missing_group))
    aggregations = [item for item in step.get("aggregations", []) if isinstance(item, dict)]
    if not aggregations:
        raise OutputContractError("Typed Pandas 집계 정의가 없습니다.")
    working = frame.copy()
    named_aggregations: dict[str, Any] = {}
    for aggregation in aggregations:
        source_column = str(aggregation.get("column") or aggregation.get("source_column") or "").strip()
        output_column = str(aggregation.get("output_column") or "").strip()
        method = str(aggregation.get("method") or aggregation.get("aggregation") or "").strip().lower()
        if not source_column or source_column not in working.columns or not output_column:
            raise OutputContractError("Typed Pandas 집계 컬럼 또는 출력명이 올바르지 않습니다.")
        if method in {"sum", "mean", "median", "min", "max"}:
            working[source_column] = pd.to_numeric(working[source_column], errors="coerce").fillna(0)
        named_aggregations[output_column] = pd.NamedAgg(
            column=source_column,
            aggfunc=_pandas_aggregation_method(method),
        )
    output_columns = [*group_by, *named_aggregations]
    if working.empty:
        return pd.DataFrame(columns=output_columns)
    if group_by:
        return working.groupby(group_by, dropna=False).agg(**named_aggregations).reset_index()
    row: dict[str, Any] = {}
    for output_column, aggregation in named_aggregations.items():
        row[output_column] = working[str(aggregation.column)].agg(aggregation.aggfunc)
    return pd.DataFrame([row], columns=output_columns)


def _typed_sort_and_top_n(frame: Any, step: dict[str, Any]) -> Any:
    sort_by = str(step.get("sort_by") or "").strip()
    if not sort_by or sort_by not in frame.columns:
        raise OutputContractError("Typed Pandas 정렬 컬럼을 찾을 수 없습니다: " + sort_by)
    order = str(step.get("order") or "desc").strip().lower()
    if order not in {"asc", "desc"}:
        raise OutputContractError("Typed Pandas 정렬 방향은 asc 또는 desc여야 합니다.")
    try:
        limit = max(0, int(step.get("limit") or 0))
    except (TypeError, ValueError) as exc:
        raise OutputContractError("Typed Pandas 정렬 limit이 올바르지 않습니다.") from exc
    result = frame.sort_values(
        by=sort_by,
        ascending=order == "asc",
        na_position="last",
        kind="mergesort",
    )
    if limit:
        result = result.head(limit)
    return result.reset_index(drop=True)


def _typed_join_frames(left: Any, right: Any, step: dict[str, Any], pd: Any) -> Any:
    left_keys = _string_list(step.get("left_on"))
    right_keys = _string_list(step.get("right_on"))
    if not left_keys:
        left_keys = _string_list(step.get("on")) or _string_list(step.get("group_by"))
        right_keys = list(left_keys)
    if not left_keys or len(left_keys) != len(right_keys):
        raise OutputContractError("Typed Pandas join key 계약이 올바르지 않습니다.")
    missing_left = [column for column in left_keys if column not in left.columns]
    missing_right = [column for column in right_keys if column not in right.columns]
    if missing_left or missing_right:
        missing = [*(f"left.{column}" for column in missing_left), *(f"right.{column}" for column in missing_right)]
        raise OutputContractError("Typed Pandas join key 컬럼을 찾을 수 없습니다: " + ", ".join(missing))
    join_type = str(step.get("join_type") or "inner").strip().lower()
    if join_type not in {"inner", "left", "right", "outer"}:
        raise OutputContractError("Typed Pandas join 방식이 올바르지 않습니다: " + join_type)
    left_working = left.copy()
    right_working = right.copy()
    temporary_keys: list[str] = []
    for index, (left_key, right_key) in enumerate(zip(left_keys, right_keys), start=1):
        temporary = f"__typed_join_key_{index}__"
        temporary_keys.append(temporary)
        left_working[temporary] = left_working[left_key].map(_normalize_deterministic_join_value)
        right_working[temporary] = right_working[right_key].map(_normalize_deterministic_join_value)
    aggregations = [
        item for item in step.get("aggregations", []) if isinstance(item, dict)
    ]
    if aggregations:
        # A join with declared aggregate outputs is a grouped enrichment, not
        # a raw many-to-many merge.  Reducing the right side first preserves
        # the left population and one output value per declared product grain.
        right_grouped, fill_defaults = _typed_aggregate_join_right(
            right_working,
            temporary_keys,
            aggregations,
            pd,
        )
        result = left_working.merge(
            right_grouped,
            on=temporary_keys,
            how=join_type,
        )
        for output_column, default in fill_defaults.items():
            if output_column in result.columns:
                result[output_column] = result[output_column].fillna(default)
        return result.drop(columns=temporary_keys)
    # When both sides use the same key name, the normalized temporary key has
    # already established equality. Keep the left key as the canonical output
    # dimension and remove the redundant right key before merge; otherwise
    # pandas would rename both to ``*_left``/``*_right`` and a following typed
    # aggregate could no longer refer to its declared grain.
    redundant_right_keys = [
        right_key
        for left_key, right_key in zip(left_keys, right_keys)
        if left_key == right_key and right_key in right_working.columns
    ]
    if redundant_right_keys:
        right_working = right_working.drop(columns=redundant_right_keys)
    result = left_working.merge(
        right_working,
        on=temporary_keys,
        how=join_type,
        suffixes=("_left", "_right"),
    )
    return result.drop(columns=temporary_keys)


def _typed_aggregate_join_right(
    frame: Any,
    group_columns: list[str],
    aggregations: list[dict[str, Any]],
    pd: Any,
) -> tuple[Any, dict[str, Any]]:
    """Aggregate a typed join's right side without inferring business rules."""

    working = frame.copy()
    named_aggregations: dict[str, Any] = {}
    fill_defaults: dict[str, Any] = {}
    for aggregation in aggregations:
        source_column = str(
            aggregation.get("column") or aggregation.get("source_column") or ""
        ).strip()
        output_column = str(aggregation.get("output_column") or "").strip()
        method = str(
            aggregation.get("method") or aggregation.get("aggregation") or ""
        ).strip().lower()
        if (
            not source_column
            or source_column not in working.columns
            or not output_column
            or output_column in named_aggregations
        ):
            raise OutputContractError("Typed Pandas join 집계 컬럼 또는 출력명이 올바르지 않습니다.")
        if method in {"sum", "mean", "median", "min", "max"}:
            working[source_column] = pd.to_numeric(
                working[source_column], errors="coerce"
            ).fillna(0)
        named_aggregations[output_column] = pd.NamedAgg(
            column=source_column,
            aggfunc=_pandas_aggregation_method(method),
        )
        fill_defaults[output_column] = "" if method == "collect_unique" else 0
    if working.empty:
        return pd.DataFrame(columns=[*group_columns, *named_aggregations]), fill_defaults
    return (
        working.groupby(group_columns, dropna=False)
        .agg(**named_aggregations)
        .reset_index(),
        fill_defaults,
    )


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


# 함수 설명: 실제 생성 코드 오류와 표준화 후 물리 alias 재사용만 repair 대상으로 허용하고 구조 계약 오류의 동일 재시도를 막습니다.
def _repairable_execution_failure(payload: dict[str, Any]) -> bool:
    error = _analysis_error_value(payload)
    error_type = str(error.get("type") or "").strip().lower() if isinstance(error, dict) else ""
    return error_type in {
        "pandas_execution_error",
        "unsafe_code",
        "generated_code_uses_physical_alias",
    }


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


# 함수 설명: Complex 경로에서도 선택된 Function Case를 source 단위의 결정론적 전처리 계약으로 읽습니다.
def _selected_function_case_source_transforms(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    raw_cases: list[dict[str, Any]] = []
    for key in ("pandas_function_cases", "pandas_function_case"):
        value = plan.get(key)
        if isinstance(value, dict):
            raw_cases.append(deepcopy(value))
        elif isinstance(value, list):
            raw_cases.extend(deepcopy(item) for item in value if isinstance(item, dict))
    steps = plan.get("pandas_execution_plan") if isinstance(plan.get("pandas_execution_plan"), list) else []
    raw_cases.extend(
        deepcopy(item)
        for item in steps
        if isinstance(item, dict)
        and str(item.get("operation") or item.get("step") or "").strip()
        == "apply_pandas_function_case"
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_cases, start=1):
        function_name = str(item.get("function_name") or "").strip()
        source_alias = str(item.get("source_alias") or "").strip()
        if not function_name or not function_name.isidentifier() or not source_alias:
            continue
        arguments = deepcopy(item.get("arguments")) if isinstance(item.get("arguments"), dict) else {}
        if isinstance(item.get("kwargs"), dict):
            arguments.update(deepcopy(item["kwargs"]))
        transform = {
            "node_id": str(item.get("node_id") or f"__selected_function_case_{index}").strip(),
            "source_alias": source_alias,
            "function_case_key": str(item.get("function_case_key") or item.get("key") or "").strip(),
            "function_name": function_name,
            "input_text": str(item.get("input_text") or ""),
            "arguments": arguments,
        }
        # A normalized plan stores the same selected helper both in the
        # semantic case list and in its typed pandas step.  Node ids differ
        # between those two representations, but execution intent does not;
        # dedupe by the callable contract so a helper never filters the same
        # source twice.
        marker_payload = {
            "source_alias": source_alias,
            "function_case_key": transform["function_case_key"],
            "function_name": function_name,
            "input_text": transform["input_text"],
        }
        # A nonblank case key identifies one semantic function selection. The
        # typed step may omit derived arguments, so retain the richer first
        # representation instead of executing the same selection twice.
        if not transform["function_case_key"]:
            marker_payload["arguments"] = arguments
        marker = json.dumps(
            marker_payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if marker not in seen:
            seen.add(marker)
            result.append(transform)
    return result


# 함수 설명: 선택 helper 호출을 이미 전처리된 source copy로 바꿔 LLM의 함수 인자 순서 오류를 제거합니다.
def _active_retrieval_source_aliases(payload: dict[str, Any]) -> set[str]:
    """Return aliases owned by active retrieval jobs, not model-made result variables."""

    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    aliases = {
        str(job.get("source_alias") or "").strip()
        for job in (plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else [])
        if isinstance(job, dict) and str(job.get("source_alias") or "").strip()
    }
    if aliases:
        return aliases
    return {
        str(item.get("source_alias") or "").strip()
        for item in (payload.get("source_results") if isinstance(payload.get("source_results"), list) else [])
        if isinstance(item, dict) and str(item.get("source_alias") or "").strip()
    }


def _replace_selected_function_case_calls(
    generated_code: str,
    transforms: list[dict[str, Any]],
    *,
    active_source_aliases: set[str] | None = None,
    runtime_source_aliases: set[str] | None = None,
) -> tuple[str, dict[str, Any], str]:
    aliases_by_function: dict[str, set[str]] = {}
    for item in transforms:
        if not isinstance(item, dict):
            continue
        function_name = str(item.get("function_name") or "").strip()
        source_alias = str(item.get("source_alias") or "").strip()
        if function_name and source_alias:
            aliases_by_function.setdefault(function_name, set()).add(source_alias)
    if not aliases_by_function:
        return generated_code, {}, ""
    try:
        tree = ast.parse(generated_code)
    except SyntaxError as exc:
        return generated_code, {}, f"Function Case 호출 정규화 전 pandas 코드가 유효하지 않습니다: {exc}"
    called_functions = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in aliases_by_function
    }
    ambiguous = sorted(
        name for name in called_functions if len(aliases_by_function.get(name, set())) != 1
    )
    if ambiguous:
        return generated_code, {}, "같은 Function Case helper가 여러 source에 선택되어 생성 코드 호출을 하나로 연결할 수 없습니다: " + ", ".join(ambiguous)
    source_by_function = {
        name: next(iter(aliases))
        for name, aliases in aliases_by_function.items()
        if len(aliases) == 1
    }
    active_aliases = {
        str(alias).strip()
        for alias in (active_source_aliases or set())
        if str(alias).strip()
    }
    runtime_aliases = {
        str(alias).strip()
        for alias in (runtime_source_aliases or set())
        if str(alias).strip()
    }
    # A model-made result alias (for example `matched_df`) is safe to rewrite
    # only for a single retrieved source with a single selected transform.
    # Multi-source plans retain their literal aliases to avoid schema mixing.
    fallback_source_alias = ""
    if len(active_aliases) == 1:
        only_alias = next(iter(active_aliases))
        if set(source_by_function.values()) == {only_alias}:
            fallback_source_alias = only_alias

    def source_alias_from_expression(node: ast.AST) -> str:
        """Return a literal ``sources['alias']`` reference through harmless wrappers."""
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "sources":
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return key.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "copy":
            return source_alias_from_expression(node.func.value)
        return ""

    def source_alias_from_call(node: ast.Call) -> str:
        aliases = {
            alias
            for value in [*node.args, *(item.value for item in node.keywords)]
            if (alias := source_alias_from_expression(value))
        }
        return next(iter(aliases)) if len(aliases) == 1 else ""

    class FunctionCaseCallRewriter(ast.NodeTransformer):
        def __init__(self) -> None:
            self.replaced: list[str] = []
            self.replaced_unknown_source_refs: list[str] = []
            self.replaced_unbound_calls: list[str] = []

        def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
            node = self.generic_visit(node)
            if not isinstance(node, ast.Subscript) or not fallback_source_alias:
                return node
            if not isinstance(node.value, ast.Name) or node.value.id != "sources":
                return node
            key = node.slice
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return node
            source_ref = str(key.value).strip()
            if not source_ref or source_ref in active_aliases or source_ref in runtime_aliases:
                return node
            self.replaced_unknown_source_refs.append(source_ref)
            node.slice = ast.copy_location(ast.Constant(value=fallback_source_alias), key)
            return node

        def visit_Call(self, node: ast.Call) -> ast.AST:
            node = self.generic_visit(node)
            if not isinstance(node, ast.Call):
                return node
            if not isinstance(node.func, ast.Name):
                return node
            function_name = node.func.id
            source_alias = source_by_function.get(function_name)
            if not source_alias or function_name not in called_functions:
                return node
            call_source_alias = source_alias_from_call(node)
            if call_source_alias != source_alias:
                if not (fallback_source_alias == source_alias and not call_source_alias):
                    return node
                self.replaced_unbound_calls.append(function_name)
            replacement = ast.Call(
                func=ast.Attribute(
                    value=ast.Subscript(
                        value=ast.Name(id="sources", ctx=ast.Load()),
                        slice=ast.Constant(value=source_alias),
                        ctx=ast.Load(),
                    ),
                    attr="copy",
                    ctx=ast.Load(),
                ),
                args=[],
                keywords=[],
            )
            self.replaced.append(function_name)
            return ast.copy_location(replacement, node)

    rewriter = FunctionCaseCallRewriter()
    rewritten = rewriter.visit(tree)
    ast.fix_missing_locations(rewritten)
    if not rewriter.replaced:
        if not rewriter.replaced_unknown_source_refs:
            return generated_code, {}, ""
    return (
        ast.unparse(rewritten),
        {
            "policy": "selected_function_case_pretransform_replaces_generated_calls",
            "replaced_function_names": list(dict.fromkeys(rewriter.replaced)),
            "replacement_count": len(rewriter.replaced),
            "replaced_unknown_source_refs": list(
                dict.fromkeys(rewriter.replaced_unknown_source_refs)
            ),
            "replaced_unbound_function_names": list(
                dict.fromkeys(rewriter.replaced_unbound_calls)
            ),
        },
        "",
    )


# 함수 설명: 선택된 trusted Function Case와 같은 이름의 LLM 함수 정의를 제거하고 canonical helper만 실행 코드에 주입합니다.
def _prepare_trusted_function_case_helpers(
    generated_code: Any,
    helper_code_value: Any,
) -> tuple[str, str, dict[str, Any], str]:
    code = str(generated_code or "")
    helper_code = _text_value(helper_code_value).strip()
    if not helper_code:
        return code, "", {}, ""
    try:
        helper_tree = ast.parse(helper_code)
    except SyntaxError as exc:
        return code, "", {}, f"선택 Function Case helper 구문이 유효하지 않습니다: {exc}"
    helper_nodes = [
        node
        for node in helper_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not helper_nodes or len(helper_nodes) != len(helper_tree.body):
        return code, "", {}, "선택 Function Case helper에는 최상위 함수 정의만 허용됩니다."
    trusted_names = [node.name for node in helper_nodes]
    if len(trusted_names) != len(set(trusted_names)):
        return code, "", {}, "선택 Function Case helper 이름이 중복되었습니다."
    helper_guard_error = _guard_code(helper_code)
    if helper_guard_error:
        return code, "", {}, f"선택 Function Case helper 안전성 검증에 실패했습니다: {helper_guard_error}"
    try:
        generated_tree = ast.parse(code)
    except SyntaxError as exc:
        return code, "", {}, f"생성 pandas 코드 구문이 유효하지 않습니다: {exc}"

    trusted_name_set = set(trusted_names)
    removed_names: list[str] = []
    removal_ranges: list[tuple[int, int]] = []
    for node in generated_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in trusted_name_set:
            decorated_lines = [int(item.lineno) for item in node.decorator_list]
            start_line = min([int(node.lineno), *decorated_lines])
            end_line = int(getattr(node, "end_lineno", node.lineno))
            removal_ranges.append((start_line, end_line))
            removed_names.append(node.name)

    normalized_lines = code.splitlines(keepends=True)
    for start_line, end_line in removal_ranges:
        for line_index in range(max(0, start_line - 1), min(len(normalized_lines), end_line)):
            line = normalized_lines[line_index]
            content = line.rstrip("\r\n")
            normalized_lines[line_index] = line[len(content) :]
    sanitized = "".join(normalized_lines)
    try:
        sanitized_tree = ast.parse(sanitized)
    except SyntaxError as exc:
        return code, "", {}, f"trusted helper 재정의 제거 후 pandas 코드가 유효하지 않습니다: {exc}"
    rebound = _top_level_rebound_trusted_names(sanitized_tree, trusted_name_set)
    if rebound:
        return (
            code,
            "",
            {},
            "생성 pandas 코드가 선택 Function Case helper 이름을 함수 정의 외 방식으로 덮어씁니다: "
            + ", ".join(rebound),
        )
    return (
        sanitized,
        helper_code,
        {
            "policy": "canonical_selected_helper_overrides_generated_definition",
            "trusted_helper_names": trusted_names,
            "removed_generated_definitions": list(dict.fromkeys(removed_names)),
        },
        "",
    )


# 함수 설명: 모듈 실행 범위의 제어문 안에서 trusted helper 이름을 다시 바인딩하는 비함수 구문을 찾습니다.
def _top_level_rebound_trusted_names(tree: ast.Module, trusted_names: set[str]) -> list[str]:
    rebound: list[str] = []

    # 함수 설명: 현재 모듈 실행 범위의 AST 노드를 순회하되 새 함수·람다의 로컬 범위는 재귀 검사하지 않습니다.
    def inspect(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in trusted_names and node.name not in rebound:
                rebound.append(node.name)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.ClassDef):
            if node.name in trusted_names and node.name not in rebound:
                rebound.append(node.name)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id in trusted_names and node.id not in rebound:
                rebound.append(node.id)
        for child in ast.iter_child_nodes(node):
            inspect(child)

    for statement in tree.body:
        inspect(statement)
    return sorted(rebound)


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
    trusted_override = (
        deepcopy(value.get("trusted_helper_override"))
        if isinstance(value.get("trusted_helper_override"), dict)
        else {}
    )
    selected_pre_transform = (
        deepcopy(value.get("selected_function_case_pre_transform"))
        if isinstance(value.get("selected_function_case_pre_transform"), dict)
        else {}
    )
    if not removed and not trusted_override and not selected_pre_transform:
        return {}
    namespaces = ["pd"]
    if value.get("numpy_requested") is True:
        namespaces.append("np_safe")
    trace = {
        "policy": str(value.get("policy") or SAFE_IMPORT_POLICY),
        "removed_imports": removed,
        "provided_namespaces": namespaces,
    }
    if trusted_override:
        trace["trusted_helper_override"] = trusted_override
    if selected_pre_transform:
        trace["selected_function_case_pre_transform"] = selected_pre_transform
    return trace


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


# 함수 설명: trusted Function Case 중간 결과에서 group key별로 유일한 누락 entity grain만 최종 결과에 복원합니다.
def _enrich_unique_function_case_grain(
    result: Any,
    execution_namespace: dict[str, Any],
    payload: dict[str, Any],
    pd_module: Any,
) -> tuple[Any, dict[str, Any]]:
    trace: dict[str, Any] = {
        "status": "not_applicable",
        "requested_columns": [],
        "restored_columns": [],
    }
    if not isinstance(result, pd_module.DataFrame):
        return result, trace
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    declared_grain = _string_list(contract.get("grain_columns"))
    missing = [column for column in declared_grain if column not in result.columns]
    if not missing:
        return result, trace
    steps = [item for item in plan.get("pandas_execution_plan", []) if isinstance(item, dict)]
    output_aliases = []
    for step in steps:
        if str(step.get("operation") or step.get("step") or "").strip().lower() != "apply_pandas_function_case":
            continue
        alias = str(step.get("output_alias") or "").strip()
        if alias and alias not in output_aliases:
            output_aliases.append(alias)
    frames = [
        (alias, execution_namespace.get(alias))
        for alias in output_aliases
        if isinstance(execution_namespace.get(alias), pd_module.DataFrame)
    ]
    trace["requested_columns"] = list(missing)
    trace["candidate_output_aliases"] = list(output_aliases)
    if len(frames) != 1:
        trace["status"] = "ambiguous_helper_output" if frames else "helper_output_unavailable"
        return result, trace
    alias, frame = frames[0]
    restorable = [column for column in missing if column in frame.columns]
    if not restorable:
        trace["status"] = "source_columns_unavailable"
        return result, trace
    next_result = result.copy()
    if next_result.empty:
        for column in restorable:
            next_result[column] = pd_module.Series(dtype=frame[column].dtype)
        trace.update(
            {
                "status": "restored_empty_schema",
                "source_output_alias": alias,
                "restored_columns": restorable,
            }
        )
        return next_result, trace

    join_keys = [column for column in declared_grain if column in result.columns and column in frame.columns]
    mapping_columns = [*join_keys, *restorable]
    mapping = frame[mapping_columns].drop_duplicates().copy()
    if join_keys:
        cardinality = mapping.groupby(join_keys, dropna=False)[restorable].nunique(dropna=False)
        if bool((cardinality > 1).any(axis=None)):
            trace["status"] = "ambiguous_values"
            trace["source_output_alias"] = alias
            return result, trace
        mapping = mapping.drop_duplicates(subset=join_keys, keep="first")
        next_result = next_result.merge(mapping, on=join_keys, how="left", validate="many_to_one")
    else:
        if len(mapping) != 1:
            trace["status"] = "ambiguous_values"
            trace["source_output_alias"] = alias
            return result, trace
        for column in restorable:
            next_result[column] = mapping.iloc[0][column]
    trace.update(
        {
            "status": "restored",
            "source_output_alias": alias,
            "join_keys": join_keys,
            "restored_columns": restorable,
        }
    )
    return next_result, trace


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
    # `column_labels` is a presentation contract, not a second metric name.
    # Lower-capability models sometimes use that visible label as the DataFrame
    # column name.  Accept it only when the label belongs to exactly one
    # declared canonical result column; this lets the strict contract project
    # it back to the canonical key without accepting arbitrary prose aliases.
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    labels = contract.get("column_labels") if isinstance(contract.get("column_labels"), dict) else {}
    label_owners: dict[str, list[str]] = {}
    for canonical, label in labels.items():
        canonical_name = str(canonical or "").strip()
        label_name = str(label or "").strip()
        if canonical_name and label_name:
            label_owners.setdefault(label_name.casefold(), []).append(canonical_name)
    candidate_keys = {item.casefold() for item in candidates}
    for label_key, owners in label_owners.items():
        if len(owners) != 1 or owners[0].casefold() not in candidate_keys:
            continue
        label = next(
            (
                str(value).strip()
                for key, value in labels.items()
                if str(key or "").strip().casefold() == owners[0].casefold()
                and str(value or "").strip().casefold() == label_key
            ),
            "",
        )
        if label and label.casefold() not in candidate_keys:
            candidates.append(label)
            candidate_keys.add(label.casefold())
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
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


# 함수 설명: 실행 전에 원본 source의 제한된 미리보기만 만들어 오류 시에도 복원합니다.
INTERMEDIATE_DOWNLOAD_KEY = "last_successful"

# 함수 설명: `_project_intermediate_checkpoint()`는 정상 완료 시 계산 직전 결과를,
# 오류 시 마지막 정상 결과를 하나만 선택해 화면 미리보기와 다운로드용 전체 행으로 분리합니다.
# 답변 모델에는 미리보기만 전달하고 전체 행은 runtime key로만 보존합니다.
def _project_intermediate_checkpoint(
    checkpoints: Any,
    checkpoint_values: dict[str, Any],
    payload: dict[str, Any],
    filter_plan: Any,
    *,
    completed: bool,
    final_rows: list[dict[str, Any]] | None = None,
    final_columns: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    items = [item for item in checkpoints if isinstance(item, dict)] if isinstance(checkpoints, list) else []
    if not items:
        return [], {}, {}

    selected: dict[str, Any] | None = None
    if completed:
        multi_source_projection = _project_multi_source_intermediate_checkpoints(
            items,
            checkpoint_values,
            payload,
            filter_plan,
            final_rows or [],
            final_columns or [],
        )
        if multi_source_projection is not None:
            return multi_source_projection
        # A successful aggregate may be identical to its final contract.  In
        # that case, publish the closest earlier checkpoint that differs from
        # the final table, normally the fully filtered source frame.
        for item in reversed(items):
            key = str(item.get("key") or "").strip()
            if not key or key not in checkpoint_values:
                continue
            candidate_rows, candidate_columns = _result_to_rows(
                checkpoint_values.get(key),
                payload,
            )
            if not _intermediate_matches_final_result(
                candidate_rows,
                candidate_columns,
                final_rows or [],
                final_columns or [],
                payload,
            ):
                selected = item
                break
    if selected is None:
        for item in reversed(items):
            if str(item.get("key") or "") in checkpoint_values:
                selected = item
                break
    if selected is None:
        fallback = deepcopy(items[-1])
        fallback["description"] = _intermediate_checkpoint_label(
            fallback,
            payload,
            filter_plan,
            completed=False,
        )
        return [fallback], {}, {}

    key = str(selected.get("key") or "").strip()
    rows, columns = _result_to_rows(checkpoint_values.get(key), payload)
    # 성공한 경우에만 최종 표와 비교한다. 오류 때는 마지막 정상 산출물을
    # 항상 남겨야 원인 확인과 다운로드가 가능하다.
    visible = deepcopy(selected)
    selected_role = str(visible.get("role") or "").strip()
    if completed and selected_role in {"filtered_source", "step_output"}:
        visible["description"] = "최종 집계 전 중간 데이터"
        criteria = _intermediate_criteria_fields(payload, filter_plan)
        if criteria:
            visible["description"] += f" ({', '.join(criteria)} 필터 적용 후)"
    elif completed and selected_role == "source_input":
        visible["description"] = "최종 집계 전 중간 데이터"
    else:
        visible["description"] = _intermediate_checkpoint_label(
            visible,
            payload,
            filter_plan,
            completed=completed,
        )
    visible["row_count"] = len(rows)
    visible["columns"] = columns
    visible["preview_rows"] = deepcopy(rows[:TRACE_PREVIEW_LIMIT])
    visible["download_key"] = INTERMEDIATE_DOWNLOAD_KEY
    artifact = {
        "rows": _json_ready(rows),
        "columns": [str(column) for column in columns],
        "row_count": len(rows),
        "label": visible["description"],
        "role": str(visible.get("role") or ""),
        "checkpoint_key": key,
    }
    metadata = {
        INTERMEDIATE_DOWNLOAD_KEY: {
            "label": visible["description"],
            "role": str(visible.get("role") or ""),
            "checkpoint_key": key,
            "row_count": len(rows),
            "columns": [str(column) for column in columns],
        }
    }
    return [visible], {INTERMEDIATE_DOWNLOAD_KEY: artifact}, metadata


# 함수 설명: 다중 source 분석이 정상 완료되면 source별 필터 적용 데이터와 중복되지 않는 결합·계산 결과를 함께 선택합니다.
def _project_multi_source_intermediate_checkpoints(
    items: list[dict[str, Any]],
    checkpoint_values: dict[str, Any],
    payload: dict[str, Any],
    filter_plan: Any,
    final_rows: list[dict[str, Any]],
    final_columns: list[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]] | None:
    """Publish filtered inputs per source and a non-duplicate derived result."""
    source_aliases = _intermediate_retrieval_source_aliases(payload)
    if len(source_aliases) < 2:
        return None

    selected: list[dict[str, Any]] = []
    for source_alias in source_aliases:
        item = _latest_intermediate_item(
            items,
            checkpoint_values,
            (f"filtered:{source_alias}", f"source:{source_alias}"),
        )
        if item is not None:
            selected.append(item)
    if len(selected) < 2:
        return None

    computed = _latest_intermediate_item(
        items,
        checkpoint_values,
        ("computed_result",),
    )
    if computed is not None:
        computed_key = str(computed.get("key") or "").strip()
        computed_rows, computed_columns = _result_to_rows(
            checkpoint_values.get(computed_key),
            payload,
        )
        if not _intermediate_matches_final_result(
            computed_rows,
            computed_columns,
            final_rows,
            final_columns,
            payload,
        ):
            selected.append(computed)

    return _build_intermediate_projection(
        selected,
        checkpoint_values,
        payload,
        filter_plan,
        completed=True,
        multi_source=True,
    )


# 함수 설명: 지정된 checkpoint key 후보 중 실제 값이 남아 있는 가장 최근 항목을 찾습니다.
def _latest_intermediate_item(
    items: list[dict[str, Any]],
    checkpoint_values: dict[str, Any],
    keys: tuple[str, ...],
) -> dict[str, Any] | None:
    for key in keys:
        for item in reversed(items):
            item_key = str(item.get("key") or "").strip()
            if item_key == key and item_key in checkpoint_values:
                return item
    return None


# 함수 설명: 선택된 중간 checkpoint들을 화면 preview와 개별 CSV 다운로드 artifact로 분리해 구성합니다.
def _build_intermediate_projection(
    selected_items: list[dict[str, Any]],
    checkpoint_values: dict[str, Any],
    payload: dict[str, Any],
    filter_plan: Any,
    *,
    completed: bool,
    multi_source: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    visible_items: list[dict[str, Any]] = []
    artifacts: dict[str, dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    used_download_keys: set[str] = set()

    for index, selected in enumerate(selected_items):
        key = str(selected.get("key") or "").strip()
        if not key or key not in checkpoint_values:
            continue
        rows, columns = _result_to_rows(checkpoint_values.get(key), payload)
        download_key = _intermediate_download_key(selected, index, used_download_keys)
        visible = deepcopy(selected)
        if multi_source:
            visible["description"] = _multi_source_intermediate_label(
                visible,
                payload,
                filter_plan,
            )
        else:
            visible["description"] = _intermediate_checkpoint_label(
                visible,
                payload,
                filter_plan,
                completed=completed,
            )
        visible["row_count"] = len(rows)
        visible["columns"] = columns
        visible["preview_rows"] = deepcopy(rows[:TRACE_PREVIEW_LIMIT])
        visible["download_key"] = download_key
        visible_items.append(visible)
        artifacts[download_key] = {
            "rows": _json_ready(rows),
            "columns": [str(column) for column in columns],
            "row_count": len(rows),
            "label": visible["description"],
            "role": str(visible.get("role") or ""),
            "checkpoint_key": key,
        }
        metadata[download_key] = {
            "label": visible["description"],
            "role": str(visible.get("role") or ""),
            "checkpoint_key": key,
            "row_count": len(rows),
            "columns": [str(column) for column in columns],
        }
    return visible_items, artifacts, metadata


# 함수 설명: source별 중간 데이터와 계산 결과가 충돌하지 않도록 안정적인 다운로드 key를 만듭니다.
def _intermediate_download_key(
    item: dict[str, Any],
    index: int,
    used: set[str],
) -> str:
    role = str(item.get("role") or "").strip()
    source_alias = _intermediate_source_alias(item)
    if role in {"source_input", "filtered_source"} and source_alias:
        base = f"source_{_safe_name(source_alias)}"
    elif role == "computed_result":
        base = "pre_contract_result"
    else:
        base = f"intermediate_{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


# 함수 설명: checkpoint key에 기록된 source 또는 filter 접두사에서 source alias를 복원합니다.
def _intermediate_source_alias(item: dict[str, Any]) -> str:
    key = str(item.get("key") or "").strip()
    for prefix in ("filtered:", "source:", "last_available:"):
        if key.startswith(prefix):
            return key[len(prefix):].strip()
    return ""


# 함수 설명: 현재 intent의 retrieval job에서 중복 없이 source alias 목록을 수집합니다.
def _intermediate_retrieval_source_aliases(payload: dict[str, Any]) -> list[str]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    aliases: list[str] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias and alias.casefold() not in {item.casefold() for item in aliases}:
            aliases.append(alias)
    return aliases


# 함수 설명: 다중 source 중간 결과의 dataset·필터·계산 단계를 사람이 읽을 수 있는 표시명으로 만듭니다.
def _multi_source_intermediate_label(
    item: dict[str, Any],
    payload: dict[str, Any],
    filter_plan: Any,
) -> str:
    role = str(item.get("role") or "").strip()
    source_alias = _intermediate_source_alias(item)
    if role in {"source_input", "filtered_source"} and source_alias:
        source_name = _intermediate_source_name(source_alias, payload)
        criteria = _intermediate_criteria_fields_for_source(
            payload,
            filter_plan,
            source_alias,
        )
        if role == "filtered_source" and criteria:
            return f"{source_name} — 최종 집계 전 중간 데이터 ({', '.join(criteria)} 필터 적용 후)"
        return f"{source_name} — 최종 집계 전 중간 데이터"
    if role == "computed_result":
        return "결합·계산 결과 (최종 계약 적용 전)"
    return _intermediate_checkpoint_label(item, payload, filter_plan, completed=True)


# 함수 설명: source alias에 연결된 dataset key를 찾아 중간 결과의 사용자 표시명으로 사용합니다.
def _intermediate_source_name(source_alias: str, payload: dict[str, Any]) -> str:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias.casefold() == source_alias.casefold():
            return str(job.get("dataset_key") or source_alias).strip()
    return source_alias


# 함수 설명: 특정 source에 실제 적용된 필터와 조회 파라미터 컬럼을 중간 결과 설명용으로 수집합니다.
def _intermediate_criteria_fields_for_source(
    payload: dict[str, Any],
    filter_plan: Any,
    source_alias: str,
) -> list[str]:
    fields: list[str] = []

    # 함수 설명: source 조건 컬럼을 공백과 대소문자 중복 없이 순서대로 추가합니다.
    def append(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in fields}:
            fields.append(text)

    for item in filter_plan if isinstance(filter_plan, list) else []:
        if not isinstance(item, dict):
            continue
        item_alias = str(item.get("source_alias") or item.get("alias") or "").strip()
        if item_alias and item_alias.casefold() != source_alias.casefold():
            continue
        conditions = item.get("conditions") if isinstance(item.get("conditions"), list) else []
        for condition in conditions:
            if isinstance(condition, dict):
                append(condition.get("field") or condition.get("column"))

    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias.casefold() != source_alias.casefold():
            continue
        for condition in _filter_conditions(job.get("filters")):
            append(condition.get("field"))
        required_params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
        for field, value in required_params.items():
            if value not in (None, "", [], {}):
                append(field)
    return fields[:12]


# 함수 설명: `_intermediate_matches_final_result()`는 컬럼 alias와 숫자 표현을
# 정규화해 중간 계산 표가 최종 결과와 의미적으로 같은지 판정하고 중복 표시를 막습니다.
def _intermediate_matches_final_result(
    checkpoint_rows: list[dict[str, Any]],
    checkpoint_columns: list[str],
    final_rows: list[dict[str, Any]],
    final_columns: list[str],
    payload: dict[str, Any],
) -> bool:
    if len(checkpoint_rows) != len(final_rows) or not checkpoint_columns or not final_columns:
        return False

    checkpoint_by_key = {
        str(column).strip().casefold(): str(column)
        for column in checkpoint_columns
        if str(column).strip()
    }
    pairs: list[tuple[str, str]] = []
    used_checkpoint_columns: set[str] = set()
    for final_column in final_columns:
        text = str(final_column).strip()
        if not text:
            return False
        matched = next(
            (
                checkpoint_by_key.get(str(candidate).strip().casefold())
                for candidate in _equivalent_column_names(text, payload)
                if checkpoint_by_key.get(str(candidate).strip().casefold())
            ),
            "",
        )
        if not matched or matched in used_checkpoint_columns:
            return False
        pairs.append((matched, text))
        used_checkpoint_columns.add(matched)

    # 중간 결과에 최종 표에 없는 컬럼이 있으면 사용자가 확인할 의미 있는
    # 계산 정보가 있으므로 숨기지 않는다.
    if len(used_checkpoint_columns) != len(checkpoint_by_key):
        return False
    for checkpoint_row, final_row in zip(checkpoint_rows, final_rows):
        if not isinstance(checkpoint_row, dict) or not isinstance(final_row, dict):
            return False
        for checkpoint_column, final_column in pairs:
            if _json_ready(checkpoint_row.get(checkpoint_column)) != _json_ready(final_row.get(final_column)):
                return False
    return True


# 함수 설명: `_intermediate_checkpoint_label()`은 실제 적용된 필터·조회 파라미터를
# 기준으로 선택된 중간 결과의 짧고 일관된 사용자 표시명을 생성합니다.
def _intermediate_checkpoint_label(
    item: dict[str, Any],
    payload: dict[str, Any],
    filter_plan: Any,
    *,
    completed: bool,
) -> str:
    role = str(item.get("role") or "").strip()
    criteria = _intermediate_criteria_fields(payload, filter_plan)
    suffix = f" ({', '.join(criteria)} 필터 적용 후)" if criteria else ""
    if completed:
        return "최종 계약 적용 전 계산 결과" + suffix
    if role == "computed_result":
        return "오류 전 마지막 정상 계산 결과" + suffix
    if role in {"filtered_source", "step_output"}:
        return "오류 전 마지막 정상 단계 결과" + suffix
    if role == "last_available_source":
        return "오류 전 마지막 확인 가능 데이터"
    description = str(item.get("description") or "").strip()
    return description or "오류 전 마지막 확인 가능 데이터"


# 함수 설명: `_intermediate_criteria_fields()`는 중간 결과 설명에 넣을 실제 필터와
# 조회 파라미터 컬럼명을 순서와 중복 없이 수집합니다.
def _intermediate_criteria_fields(payload: dict[str, Any], filter_plan: Any) -> list[str]:
    fields: list[str] = []

    # 함수 설명: `append()`는 실제 조건 컬럼명을 공백·대소문자 중복 없이 추가합니다.
    def append(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in fields}:
            fields.append(text)

    for item in filter_plan if isinstance(filter_plan, list) else []:
        conditions = item.get("conditions") if isinstance(item, dict) else []
        for condition in conditions if isinstance(conditions, list) else []:
            if isinstance(condition, dict):
                append(condition.get("field") or condition.get("column"))
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for condition in _filter_conditions(job.get("filters")):
            append(condition.get("field"))
        required_params = job.get("required_params") if isinstance(job.get("required_params"), dict) else {}
        for field, value in required_params.items():
            if value not in (None, "", [], {}):
                append(field)
    return fields[:12]


# 함수 설명: `_initial_intermediate_results()`는 pandas와 결과 계약 실행 전 원본의
# 제한된 미리보기 후보를 만들며, 이후에는 선택된 한 결과만 화면에 남습니다.
def _initial_intermediate_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    return [
        _recorded_output(f"source:{alias}", rows, "조회된 원본 데이터", "source_input")
        for alias, rows in list(runtime_sources.items())[:8]
    ]


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


# 함수 설명: 계약 오류가 나도 마지막으로 확정된 source/filter 프리뷰를 API data로 복원합니다.
def _partial_data_from_intermediate(value: Any) -> dict[str, Any]:
    items = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    preferred_roles = (
        "computed_result",
        "last_available_source",
        "step_output",
        "filtered_source",
        "source_input",
    )
    for role in preferred_roles:
        matches = [item for item in items if str(item.get("role") or "") == role]
        if not matches:
            continue
        item = matches[-1]
        rows = item.get("preview_rows") if isinstance(item.get("preview_rows"), list) else []
        columns = [str(column) for column in item.get("columns", []) if str(column).strip()]
        if not columns and rows:
            columns = [str(column) for column in rows[0]] if isinstance(rows[0], dict) else []
        return {
            "columns": columns,
            "rows": deepcopy(rows[:TRACE_PREVIEW_LIMIT]),
            "row_count": int(item.get("row_count") or len(rows)),
            "data_ref": "",
            "partial": True,
            "preview_only": True,
            "stage": str(item.get("key") or role),
        }
    return {}


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
    intermediate_results: Any = None,
) -> dict[str, Any]:
    safe_import_info = safe_imports if isinstance(safe_imports, dict) else {}
    helper_trace = _runtime_helper_trace(code)
    bounded_intermediate_results = [
        deepcopy(item)
        for item in (
            intermediate_results
            if isinstance(intermediate_results, list)
            else payload.get("intermediate_results")
            if isinstance(payload.get("intermediate_results"), list)
            else []
        )
        if isinstance(item, dict)
    ][-1:]
    if bounded_intermediate_results:
        payload["intermediate_results"] = bounded_intermediate_results
        if not isinstance(payload.get("data"), dict) or not payload.get("data", {}).get("rows"):
            partial_data = _partial_data_from_intermediate(bounded_intermediate_results)
            if partial_data:
                payload["data"] = partial_data
    result_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    recovered_result = _recovered_result_metadata(result_data, bounded_intermediate_results)
    payload["analysis"] = {
        "status": "error",
        "row_count": 0,
        "columns": [],
        "error": {"type": error_type, "message": message},
        "errors": [message],
        "repairable_errors": [message],
        "used_helpers": helper_trace["used_helpers"],
        "step_outputs": [],
        "intermediate_results": deepcopy(bounded_intermediate_results),
        "function_case_results": [],
        "recovered_result": recovered_result,
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
        "intermediate_results": deepcopy(bounded_intermediate_results),
        "used_helpers": helper_trace["used_helpers"],
        "helper_sources": helper_trace["helper_sources"],
        "error": {"type": error_type, "message": message, "traceback_summary": tb[:1000]},
        "recovered_result": deepcopy(recovered_result),
    }
    return payload


# 함수 설명: 오류 뒤 직전 정상 체크포인트를 응답 data로 사용한 사실만 간결하게 기록합니다.
# 원본 행이나 pandas 코드는 이 메타데이터에 넣지 않아 답변 모델 토큰을 늘리지 않습니다.
def _recovered_result_metadata(data: dict[str, Any], checkpoints: list[dict[str, Any]]) -> dict[str, Any]:
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    try:
        row_count = int(data.get("row_count") or len(rows))
    except (TypeError, ValueError):
        row_count = len(rows)
    if not checkpoints or (not rows and row_count <= 0 and not data.get("data_ref")):
        return {}
    checkpoint = checkpoints[-1]
    return {
        "available": True,
        "checkpoint_key": str(checkpoint.get("key") or "").strip(),
        "checkpoint_role": str(checkpoint.get("role") or "").strip(),
        "row_count": max(0, row_count),
    }


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


# 함수 설명: 표준화가 끝난 source의 strict output contract를 Table Catalog filter_mappings의 canonical key로 한 번 더 단일화합니다.
def _canonicalize_standardized_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    contract = plan.get("output_contract") if isinstance(plan.get("output_contract"), dict) else {}
    standardized_aliases = _standardized_source_aliases(payload)
    if not contract or not standardized_aliases:
        return payload
    jobs = [
        job
        for job in plan.get("retrieval_jobs", [])
        if isinstance(job, dict)
        and str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        in standardized_aliases
    ]
    if not jobs:
        return payload

    changes: list[dict[str, str]] = []

    # 함수 설명: 한 output contract field를 유일한 canonical 실행 컬럼으로 바꾸고 변경 근거를 기록합니다.
    def canonicalize(value: Any, path: str, source_alias: str = "") -> str:
        original = str(value or "").strip()
        if not original:
            return ""
        canonical = _canonical_standardized_contract_field(
            original,
            payload,
            jobs,
            source_alias,
        )
        if canonical != original:
            changes.append({"path": path, "from": original, "to": canonical})
        return canonical

    normalized = deepcopy(contract)
    for key in ("required_columns", "grain_columns", "metric_columns", "result_columns"):
        if key not in normalized:
            continue
        values: list[str] = []
        seen: set[str] = set()
        for index, value in enumerate(_string_list(normalized.get(key))):
            column = canonicalize(value, f"output_contract.{key}[{index}]")
            marker = column.casefold()
            if not column or marker in seen:
                continue
            seen.add(marker)
            values.append(column)
        normalized[key] = values

    if normalized.get("primary_metric"):
        normalized["primary_metric"] = canonicalize(
            normalized.get("primary_metric"),
            "output_contract.primary_metric",
        )
    ordering = normalized.get("ordering") if isinstance(normalized.get("ordering"), dict) else {}
    for key in ("sort_by", "rank_by", "rank_column"):
        if ordering.get(key):
            ordering[key] = canonicalize(
                ordering.get(key),
                f"output_contract.ordering.{key}",
            )

    labels = normalized.get("column_labels") if isinstance(normalized.get("column_labels"), dict) else {}
    if labels:
        normalized_labels: dict[str, Any] = {}
        for raw_column, label in labels.items():
            column = canonicalize(
                raw_column,
                f"output_contract.column_labels.{raw_column}",
            )
            normalized_labels.setdefault(column, label)
        normalized["column_labels"] = normalized_labels

    bindings = normalized.get("metric_bindings") if isinstance(normalized.get("metric_bindings"), list) else []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            continue
        alias = str(binding.get("source_alias") or "").strip()
        for key in ("source_column", "output_column"):
            if binding.get(key):
                binding[key] = canonicalize(
                    binding.get(key),
                    f"output_contract.metric_bindings[{index}].{key}",
                    alias,
                )

    if not changes:
        return payload
    plan["output_contract"] = normalized
    payload.setdefault("trace", {}).setdefault("inspection", {})[
        "runtime_output_contract_canonicalization"
    ] = {
        "stage": "17_pandas_code_executor",
        "status": "applied",
        "policy": "standardized_filter_mappings_canonical_only",
        "changes": changes,
    }
    return payload


# 함수 설명: 표준화된 source schema에 실제 존재하는 canonical key로 유일하게 매핑되는 output field만 변환합니다.
def _canonical_standardized_contract_field(
    field: str,
    payload: dict[str, Any],
    jobs: list[dict[str, Any]],
    source_alias: str = "",
) -> str:
    target = str(field or "").strip()
    if not target:
        return ""
    target_key = target.casefold()
    source_columns = _runtime_source_columns_by_alias(payload)
    matches: dict[str, str] = {}
    for job in jobs:
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if source_alias and alias != source_alias:
            continue
        available = {column.casefold() for column in source_columns.get(alias, [])}
        mapping = job.get("filter_mappings") if isinstance(job.get("filter_mappings"), dict) else {}
        for raw_canonical, raw_aliases in mapping.items():
            canonical = str(raw_canonical or "").strip()
            if not canonical or canonical.casefold() not in available:
                continue
            group = [canonical, *_string_list(raw_aliases)]
            if target_key in {item.casefold() for item in group if item}:
                matches.setdefault(canonical.casefold(), canonical)
    return next(iter(matches.values())) if len(matches) == 1 else target


# 함수 설명: runtime/source-result에서 source alias별 현재 실행 컬럼 목록을 수집합니다.
def _runtime_source_columns_by_alias(payload: dict[str, Any]) -> dict[str, list[str]]:
    result = _source_columns_by_alias(payload)
    runtime_sources = payload.get("runtime_sources") if isinstance(payload.get("runtime_sources"), dict) else {}
    for alias, rows in runtime_sources.items():
        if not isinstance(rows, list):
            continue
        columns = _string_list(
            [
                column
                for row in rows[:20]
                if isinstance(row, dict)
                for column in row
            ]
        )
        if columns:
            result[str(alias)] = columns
    return result


# 함수 설명: 표준화 완료 source에서 사라진 물리 alias가 생성 코드의 컬럼 문맥에 다시 등장했는지 검사합니다.
def _generated_code_physical_alias_issues(
    payload: dict[str, Any],
    code: str,
) -> list[dict[str, str]]:
    referenced = _code_column_reference_literals(code)
    if not referenced:
        return []
    referenced_keys = {item.casefold() for item in referenced}
    standardized_aliases = _standardized_source_aliases(payload)
    source_columns = _runtime_source_columns_by_alias(payload)
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for job in plan.get("retrieval_jobs", []) if isinstance(plan.get("retrieval_jobs"), list) else []:
        if not isinstance(job, dict):
            continue
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if alias not in standardized_aliases:
            continue
        available = {column.casefold() for column in source_columns.get(alias, [])}
        mapping = job.get("filter_mappings") if isinstance(job.get("filter_mappings"), dict) else {}
        for raw_canonical, raw_aliases in mapping.items():
            canonical = str(raw_canonical or "").strip()
            if not canonical or canonical.casefold() not in available:
                continue
            for physical in _string_list(raw_aliases):
                physical_key = physical.casefold()
                if physical == canonical or physical_key in available or physical_key not in referenced_keys:
                    continue
                marker = (alias, canonical.casefold(), physical_key)
                if marker in seen:
                    continue
                seen.add(marker)
                issues.append(
                    {
                        "source_alias": alias,
                        "canonical_column": canonical,
                        "physical_column": physical,
                    }
                )
    return issues


# 함수 설명: AST에서 DataFrame 컬럼 선택·검사·group/sort/join 인자로 사용된 문자열만 수집합니다.
def _code_column_reference_literals(code: str) -> set[str]:
    try:
        tree = ast.parse(str(code or ""))
    except SyntaxError:
        return set()
    result: set[str] = set()

    # 함수 설명: 선택한 AST 하위 트리에서 비어 있지 않은 문자열 literal만 컬럼 후보로 수집합니다.
    def collect(node: Any) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                text = child.value.strip()
                if text:
                    result.add(text)

    column_methods = {
        "agg",
        "aggregate",
        "drop",
        "drop_duplicates",
        "filter",
        "groupby",
        "join",
        "melt",
        "merge",
        "pivot",
        "pivot_table",
        "reindex",
        "rename",
        "set_index",
        "sort_values",
    }
    column_keywords = {"by", "columns", "index", "left_on", "on", "right_on", "subset", "values"}
    column_variable = re.compile(r"(?:^|_)(?:cols?|columns?|keys?|grain|metrics?|required|available)(?:_|$)", re.I)
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            collect(node.slice)
            continue
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(
                isinstance(item, ast.Attribute) and item.attr == "columns"
                for item in operands
            ):
                collect(node)
            continue
        if isinstance(node, ast.Call):
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if method in column_methods:
                for argument in node.args:
                    collect(argument)
                for keyword in node.keywords:
                    if keyword.arg in column_keywords or method == "rename":
                        collect(keyword.value)
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [target.id for target in targets if isinstance(target, ast.Name)]
            if any(column_variable.search(name) for name in names):
                collect(node.value)
    return result


# 함수 설명: 14번 노드에서 표준 컬럼 단일화가 완료된 source alias를 trace에서 확인합니다.
def _standardized_source_aliases(payload: dict[str, Any]) -> set[str]:
    """Return source aliases normalized to canonical columns by node 14."""
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    schema_resolution = (
        inspection.get("source_schema_resolution")
        if isinstance(inspection.get("source_schema_resolution"), dict)
        else {}
    )
    schema_sources = [
        item
        for item in schema_resolution.get("sources", [])
        if isinstance(item, dict)
    ]
    if schema_sources:
        return {
            str(item.get("source_alias") or "").strip()
            for item in schema_sources
            if str(item.get("status") or "").strip().lower() == "complete"
            and str(item.get("source_alias") or "").strip()
        }
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
    runtime_sources = (
        payload.get("runtime_sources")
        if isinstance(payload.get("runtime_sources"), dict)
        else {}
    )
    allowed_effective_aliases = {
        *jobs_by_alias,
        *(str(alias).strip() for alias in runtime_sources if str(alias).strip()),
        "previous_result",
        "upstream_result",
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
        if alias not in allowed_effective_aliases:
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


# 함수 설명: `execute_hybrid_analysis()`는 hybrid·분석 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
def execute_hybrid_analysis(
    payload_value: Any,
    pandas_prompt: Any,
    model_invoker: Callable[[str], Any] | None,
    repair_prompt_template: str,
    function_case_helper_code: str = "",
    max_repair_attempts: Any = DEFAULT_MAX_REPAIR_ATTEMPTS,
    fallback_to_complex_on_internal_error: Any = False,
) -> dict[str, Any]:
    """Run Fast deterministically and invoke the pandas model only for Complex."""

    started = perf_counter()
    payload = _payload(payload_value)
    contract = payload.get("simple_analysis_contract") if isinstance(payload.get("simple_analysis_contract"), dict) else {}
    route = str(contract.get("route") or "complex").strip().lower()
    inspection = payload.setdefault("trace", {}).setdefault("inspection", {})
    fast_trace = inspection.setdefault("fast_path", {})
    llm_calls = fast_trace.setdefault(
        "llm_calls",
        {"intent": 1, "pandas_generation": 0, "repair": 0, "answer": 0},
    )
    fast_trace.setdefault("prompt_chars", {})["pandas_generation"] = 0
    if route == "blocked" or _execution_blocked(payload):
        result = execute_pandas_with_repair(
            payload,
            "",
            repair_invoker=None,
            repair_prompt_template=repair_prompt_template,
            function_case_helper_code=function_case_helper_code,
            max_repair_attempts=0,
        )
        return _finalize_hybrid_trace(result, "blocked", started, llm_calls)

    if _declares_select_extreme_row_per_group(payload) and not _select_extreme_row_per_group_contract(payload):
        result = _hybrid_model_error(
            payload,
            "strict_extreme_contract_invalid",
            "select_extreme_row_per_group 계약이 strict deterministic 실행 조건을 충족하지 않습니다.",
        )
        return _finalize_hybrid_trace(result, route, started, llm_calls)

    if route == "fast":
        result = execute_pandas_with_repair(
            payload,
            "",
            repair_invoker=None,
            repair_prompt_template=repair_prompt_template,
            function_case_helper_code=function_case_helper_code,
            max_repair_attempts=0,
        )
        status = str(result.get("analysis", {}).get("status") or "").strip().lower()
        if status in {"ok", "success"} or not _bool_value(fallback_to_complex_on_internal_error, False):
            return _finalize_hybrid_trace(result, "fast", started, llm_calls)
        payload = _payload(payload_value)
        payload.setdefault("trace", {}).setdefault("warnings", []).append(
            {
                "type": "fast_path_internal_error_fallback",
                "message": "Fast Path 내부 실행 오류로 Complex 경로를 한 번 시도합니다.",
            }
        )
        route = "complex"

    deterministic_contract = _deterministic_execution_contract(payload) if route == "complex" else {}
    strict_extreme_contract = bool(
        deterministic_contract
        and deterministic_contract.get("strict") is True
        and str(deterministic_contract.get("operation") or "").strip()
        == "select_extreme_row_per_group"
    )
    if route == "complex" and (
        contract.get("requires_pandas_llm") is False or strict_extreme_contract
    ):
        if not deterministic_contract:
            result = _hybrid_model_error(
                payload,
                "deterministic_contract_unavailable",
                "Resolver가 결정론적 Complex 실행을 선택했지만 실행 계약을 확인할 수 없습니다.",
            )
            return _finalize_hybrid_trace(result, "complex", started, llm_calls)
        if strict_extreme_contract:
            fast_trace["deterministic_complex_reason"] = "strict_select_extreme_row_per_group"
        result = execute_pandas_with_repair(
            payload,
            "",
            repair_invoker=None,
            repair_prompt_template=repair_prompt_template,
            function_case_helper_code=function_case_helper_code,
            max_repair_attempts=0,
        )
        return _finalize_hybrid_trace(result, "complex", started, llm_calls)

    prompt = _text_value(pandas_prompt).strip()
    fast_trace.setdefault("prompt_chars", {})["pandas_generation"] = len(prompt)
    if not prompt or model_invoker is None:
        result = _hybrid_model_error(payload, "pandas_model_unavailable", "Complex 분석에 필요한 pandas 모델 또는 prompt가 없습니다.")
        return _finalize_hybrid_trace(result, "complex", started, llm_calls)
    try:
        llm_calls["pandas_generation"] = int(llm_calls.get("pandas_generation") or 0) + 1
        response = model_invoker(prompt)
    except Exception as exc:
        result = _hybrid_model_error(payload, "pandas_model_invocation_failed", f"{type(exc).__name__}: {exc}")
        return _finalize_hybrid_trace(result, "complex", started, llm_calls)
    result = execute_pandas_with_repair(
        payload,
        response,
        repair_invoker=model_invoker,
        repair_prompt_template=repair_prompt_template,
        function_case_helper_code=function_case_helper_code,
        max_repair_attempts=max_repair_attempts,
    )
    repair_trace = result.get("trace", {}).get("inspection", {}).get("pandas_repair", {})
    if isinstance(repair_trace, dict) and repair_trace.get("llm_called") is True:
        llm_calls["repair"] = int(llm_calls.get("repair") or 0) + 1
    return _finalize_hybrid_trace(result, "complex", started, llm_calls)


# 함수 설명: `_hybrid_model_error()`는 17 V2 Hybrid 분석 실행기 처리 중 model·오류 관련 값을 계산·변환하는 내부 helper입니다.
def _hybrid_model_error(payload: dict[str, Any], error_type: str, message: str) -> dict[str, Any]:
    next_payload = payload
    error = {"type": error_type, "message": message}
    next_payload["analysis"] = {"status": "error", "row_count": 0, "columns": [], "execution_mode": "complex_model"}
    next_payload["data"] = {"columns": [], "rows": [], "row_count": 0, "data_ref": ""}
    next_payload.setdefault("trace", {}).setdefault("errors", []).append(error)
    next_payload.setdefault("trace", {}).setdefault("inspection", {})["pandas_execution"] = {
        "stage": "17_hybrid_analysis_executor",
        "status": "error",
        "error": error,
        "execution_mode": "complex_model",
    }
    return next_payload


# 함수 설명: `_finalize_hybrid_trace()`는 17 V2 Hybrid 분석 실행기 처리 중 hybrid·실행 추적 관련 값을 계산·변환하는 내부 helper입니다.
def _finalize_hybrid_trace(
    payload: dict[str, Any],
    route: str,
    started: float,
    llm_calls: dict[str, Any],
) -> dict[str, Any]:
    analysis = payload.setdefault("analysis", {})
    analysis["execution_route"] = route
    contract = payload.get("simple_analysis_contract") if isinstance(payload.get("simple_analysis_contract"), dict) else {}
    analysis_execution_mode = str(contract.get("analysis_execution_mode") or "").strip()
    if analysis_execution_mode:
        analysis["analysis_execution_mode"] = analysis_execution_mode
    if contract.get("recipe"):
        analysis["fast_path_recipe"] = str(contract.get("recipe"))
    fast_trace = payload.setdefault("trace", {}).setdefault("inspection", {}).setdefault("fast_path", {})
    fast_trace["selected_route"] = route
    if analysis_execution_mode:
        fast_trace["analysis_execution_mode"] = analysis_execution_mode
    fast_trace["requires_pandas_llm"] = bool(contract.get("requires_pandas_llm"))
    fast_trace["llm_calls"] = deepcopy(llm_calls)
    timing = fast_trace.setdefault("timing_ms", {})
    timing["analysis_execution"] = round((perf_counter() - started) * 1000, 3)
    return payload


# 함수 설명: `_bool_value()`는 17 V2 Hybrid 분석 실행기 처리 중 값 관련 값을 계산·변환하는 내부 helper입니다.
def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return default


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class HybridAnalysisExecutor(Component):
    display_name = "17 V2 Hybrid 분석 실행기"
    description = "Fast 계약은 고정 실행하고 Complex일 때만 pandas 생성·복구 모델을 호출합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="pandas_prompt", display_name="pandas 생성 프롬프트", required=True),
        MessageTextInput(name="function_case_helper_code", display_name="선택 Function Case Helper", required=False),
        MessageTextInput(name="repair_prompt_template", display_name="pandas 복구 프롬프트", required=True, advanced=False),
        ModelInput(name="model", display_name="pandas 생성/복구 언어 모델", required=True, real_time_refresh=True),
        SecretStrInput(name="api_key", display_name="pandas 모델 API 키", required=False, advanced=True, real_time_refresh=True),
        DropdownInput(name="max_repair_attempts", display_name="최대 Repair 횟수", options=["0", "1"], value="1", advanced=True),
        BoolInput(name="fallback_to_complex_on_internal_error", display_name="Fast 내부 오류 시 Complex 재시도", value=False, advanced=True),
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
            cache_key_prefix="v2_pandas_language_model_options",
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

    # 함수 설명: `_invoke_model()`는 model 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    def _invoke_model(self, prompt: str) -> Any:
        from lfx.base.models.unified_models import get_llm

        llm = get_llm(
            model=getattr(self, "model", None),
            user_id=getattr(self, "user_id", None),
            api_key=getattr(self, "api_key", None),
        )
        if llm is None or not hasattr(llm, "invoke"):
            raise RuntimeError("Pandas Language Model이 연결되지 않았습니다.")
        return llm.invoke(prompt)

    # Langflow 출력 함수: '페이로드 출력 (payload_out)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=execute_hybrid_analysis(
                getattr(self, "payload", None),
                getattr(self, "pandas_prompt", ""),
                model_invoker=self._invoke_model,
                repair_prompt_template=getattr(self, "repair_prompt_template", ""),
                function_case_helper_code=getattr(self, "function_case_helper_code", ""),
                max_repair_attempts=getattr(self, "max_repair_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS),
                fallback_to_complex_on_internal_error=getattr(self, "fallback_to_complex_on_internal_error", False),
            )
        )
