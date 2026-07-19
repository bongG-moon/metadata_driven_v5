# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 02 최종 합성 Context 생성기
# 역할: 기본 Loop가 모은 최대 4개 compact step 결과를 마지막 native Language Model/Agent용 prompt 변수로 변환합니다.
# 주요 입력: Loop 완료 결과, 선택 실행 Context, 사용자 원문 질문, 최종 Context 최대 바이트
# 주요 출력: 질문 Message, Workflow Context Message, 합성 지시 Message, 전체 Context Data
# 처리 흐름: Loop 결과 정규화 -> run/step 순서 검증 -> 허용 필드 투영 -> 바이트 제한 -> prompt 변수 생성
# 유지보수 포인트: 하위 Flow 전체 rows·trace·artifact.raw·코드를 복원하거나 LLM에 전달하지 않습니다.
# =============================================================================

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, MessageTextInput, Output
from lfx.schema.data import Data
from lfx.schema.message import Message

STEP_RESULT_CONTRACT_VERSION = "workflow.step_result.v1"
FINAL_CONTEXT_CONTRACT_VERSION = "workflow.final_context.v1"
DEFAULT_MAX_CONTEXT_BYTES = 24 * 1024
MIN_CONTEXT_BYTES = 4 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_STEPS = 4
MAX_ARTIFACTS = 4
MAX_PUBLIC_URL_CHARS = 2_048


# 주요 함수: Loop/선택 context의 compact step 결과를 검증하고 마지막 모델에 연결할 prompt 변수 묶음을 만듭니다.
def build_final_context(
    loop_results_value: Any,
    execution_context_value: Any = None,
    user_question: Any = "",
    max_context_bytes: Any = DEFAULT_MAX_CONTEXT_BYTES,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    step_results = _collect_step_results(loop_results_value)
    context = _execution_context(execution_context_value)
    parser_errors = [deepcopy(item) for item in _list(context.get("plan_errors")) if isinstance(item, dict)]
    errors.extend(parser_errors)
    if not step_results and context:
        step_results = [
            deepcopy(item)
            for item in _dict(context.get("results_by_step")).values()
            if isinstance(item, dict)
        ]
    step_results.sort(key=lambda item: _safe_int(item.get("step_index")))
    if not step_results and not parser_errors:
        errors.append(_issue("workflow_results_missing", "최종 합성에 사용할 Workflow 단계 결과가 없습니다."))
    if len(step_results) > MAX_STEPS:
        errors.append(_issue("workflow_result_limit_exceeded", f"단계 결과는 최대 {MAX_STEPS}개까지만 허용됩니다."))
        step_results = step_results[:MAX_STEPS]

    run_ids = {str(item.get("workflow_run_id") or "").strip() for item in step_results if str(item.get("workflow_run_id") or "").strip()}
    if len(run_ids) > 1:
        errors.append(_issue("mixed_workflow_runs", "서로 다른 workflow_run_id의 단계 결과가 섞여 있습니다."))
    indices = [_safe_int(item.get("step_index")) for item in step_results]
    if indices and indices != list(range(1, len(indices) + 1)):
        errors.append(_issue("invalid_step_result_order", "단계 결과의 step_index가 1부터 연속되지 않습니다."))
    duplicates = _duplicates([str(item.get("step_id") or "") for item in step_results])
    if duplicates:
        errors.append(_issue("duplicate_step_results", f"중복 step 결과가 있습니다: {', '.join(duplicates)}"))

    compact_steps = [_project_step_result(item) for item in step_results]
    artifacts = _collect_artifacts(step_results)
    statuses = [str(item.get("status") or "") for item in compact_steps]
    execution_status = _execution_status(statuses, errors)
    question = str(user_question or context.get("original_question") or "").strip()
    workflow_context = {
        "contract_version": FINAL_CONTEXT_CONTRACT_VERSION,
        "workflow_run_id": next(iter(run_ids), str(context.get("workflow_run_id") or "")),
        "workflow_key": str((compact_steps[0].get("workflow_key") if compact_steps else "") or context.get("workflow_key") or ""),
        "execution_status": execution_status,
        "step_count": len(compact_steps),
        "steps": compact_steps,
        "errors": errors,
    }
    byte_limit = _bounded_int(max_context_bytes, DEFAULT_MAX_CONTEXT_BYTES, MIN_CONTEXT_BYTES, MAX_CONTEXT_BYTES)
    workflow_context = _fit_context_bytes(workflow_context, byte_limit)
    context_json = json.dumps(workflow_context, ensure_ascii=False, separators=(",", ":"), default=str)
    instruction = _synthesis_instruction(execution_status)
    prompt_variables = {
        "question": question,
        "workflow_context": context_json,
        "synthesis_instruction": instruction,
    }
    return {
        "status": "error" if errors and not compact_steps else execution_status,
        "question": question,
        "workflow_context": context_json,
        "synthesis_instruction": instruction,
        "prompt_variables": prompt_variables,
        "context_bytes": len(context_json.encode("utf-8")),
        "artifacts": artifacts,
        "errors": errors,
    }


# 함수 설명: `_collect_step_results()`는 Loop Done의 list/Data/DataFrame/중첩 results 형태에서 step 결과 계약만 재귀 수집합니다.
def _collect_step_results(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_objects: set[int] = set()

    # 함수 설명: `visit()`는 순환 객체를 피하면서 지원 wrapper 안의 workflow.step_result.v1만 찾습니다.
    def visit(item: Any) -> None:
        if item is None:
            return
        marker = id(item)
        if marker in seen_objects:
            return
        seen_objects.add(marker)
        data = getattr(item, "data", item)
        if hasattr(data, "to_dict") and not isinstance(data, dict):
            try:
                for row in data.to_dict(orient="records"):
                    visit(row)
            except Exception:
                return
            return
        if isinstance(data, (list, tuple)):
            for child in data:
                visit(child)
            return
        if not isinstance(data, dict):
            return
        if str(data.get("contract_version") or "") == STEP_RESULT_CONTRACT_VERSION:
            result.append(deepcopy(data))
            return
        for key in ("results", "items", "data", "outputs", "step_results"):
            child = data.get(key)
            if isinstance(child, (list, tuple, dict)):
                visit(child)

    visit(value)
    return result


# 함수 설명: `_execution_context()`는 실행 context 또는 00 parser 결과를 공통 최종 요약 context로 변환합니다.
def _execution_context(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if isinstance(data, dict) and isinstance(data.get("execution_context"), dict):
        data = data["execution_context"]
    if isinstance(data, dict) and str(data.get("contract_version")) == "workflow.execution.v1":
        return deepcopy(data)
    if isinstance(data, dict) and isinstance(data.get("workflow_plan"), dict):
        plan = data.get("normalized_plan") if isinstance(data.get("normalized_plan"), dict) else data["workflow_plan"]
        return {
            "contract_version": "workflow.parse_result.v1",
            "workflow_run_id": str(plan.get("workflow_run_id") or ""),
            "workflow_key": str(plan.get("workflow_key") or ""),
            "original_question": str(plan.get("user_question") or ""),
            "parser_status": str(data.get("status") or ""),
            "plan_errors": [deepcopy(item) for item in _list(data.get("errors")) if isinstance(item, dict)],
            "results_by_step": {},
        }
    return {}


# 함수 설명: `_project_step_result()`는 최종 모델이 필요한 결과 요약·참조·작은 식별자 preview만 허용 목록으로 복사합니다.
def _project_step_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow_run_id": _clip(value.get("workflow_run_id"), 160),
        "workflow_key": _clip(value.get("workflow_key"), 128),
        "step_index": _safe_int(value.get("step_index")),
        "total_steps": _safe_int(value.get("total_steps")),
        "step_id": _clip(value.get("step_id"), 64),
        "tool_name": _clip(value.get("tool_name"), 100),
        "question": _clip(value.get("question"), 1000),
        "status": _clip(value.get("status"), 20),
        "summary": _clip(value.get("summary"), 1600),
        "result_ref": _clip(value.get("result_ref"), 512),
        "result_ref_meta": _project_ref_meta(value.get("result_ref_meta")),
        "entity_ids": _project_entity_ids(value.get("entity_ids")),
        "artifacts": _project_artifacts(value.get("artifacts"), include_path=False),
        "warnings": _project_issues(value.get("warnings")),
        "errors": _project_issues(value.get("errors")),
    }


# 함수 설명: `_project_ref_meta()`는 결과 건수와 컬럼 일부 외의 저장소 내부 정보를 제거합니다.
def _project_ref_meta(value: Any, column_limit: int = 20) -> dict[str, Any]:
    meta = value if isinstance(value, dict) else {}
    columns = [_clip(item, 100) for item in _list(meta.get("columns"))[:column_limit]]
    return {
        key: deepcopy(item)
        for key, item in {
            "role": _clip(meta.get("role"), 60),
            "row_count": meta.get("row_count"),
            "columns": columns,
            "dataset_key": _clip(meta.get("dataset_key"), 100),
        }.items()
        if item not in (None, "", [], {})
    }


# 함수 설명: `_safe_artifact_path()`는 각 단계가 전달한 경로를 다시 검증해 `flow_id/file.html` 논리 경로 외 값을 제거합니다.
def _safe_artifact_path(value: Any) -> str:
    path = _clip(value, 1024).replace("\\", "/")
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "://" in path
        or (len(path) >= 2 and path[1] == ":")
        or len(parts) != 2
        or any(part in {"", ".", ".."} for part in parts)
        or any(marker in path for marker in ("?", "#", "\x00"))
        or not parts[-1].lower().endswith(".html")
    ):
        return ""
    return path


# 함수 설명: 최종 사용자에게 전달할 Report API 링크만 절대 http(s) URL로 검증합니다.
def _safe_public_url(value: Any) -> str:
    candidate = _clip(value, MAX_PUBLIC_URL_CHARS)
    if not candidate or any(ord(character) < 32 for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        return ""
    return candidate


# 함수 설명: `_project_artifacts()`는 HTML 본문 없이 시각화 파일 descriptor의 허용 필드만 최종 단계에 투영합니다.
def _project_artifacts(value: Any, *, include_path: bool, count: int = 2) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in _list(value)[:count]:
        if not isinstance(item, dict):
            continue
        artifact_type = _clip(item.get("artifact_type"), 40).lower()
        mime_type = _clip(item.get("mime_type"), 80).lower().split(";", 1)[0].strip()
        path = _safe_artifact_path(item.get("path"))
        download_name = _clip(item.get("download_name"), 180)
        if artifact_type != "html_chart" or mime_type != "text/html" or not path:
            continue
        if "/" in download_name or "\\" in download_name or not download_name.lower().endswith(".html"):
            continue
        if include_path and (not path or path in seen_paths):
            continue
        descriptor: dict[str, Any] = {
            "artifact_type": artifact_type,
            "mime_type": mime_type,
            "title": _clip(item.get("title") or download_name.rsplit(".", 1)[0], 180),
            "download_name": download_name,
        }
        if include_path:
            descriptor["path"] = path
            report_id = _clip(item.get("report_id"), 160)
            view_url = _safe_public_url(item.get("view_url"))
            download_url = _safe_public_url(item.get("download_url"))
            expires_at = _clip(item.get("expires_at"), 80)
            if report_id:
                descriptor["report_id"] = report_id
            if view_url:
                descriptor["view_url"] = view_url
            if download_url:
                descriptor["download_url"] = download_url
            if expires_at:
                descriptor["expires_at"] = expires_at
            if view_url or download_url:
                try:
                    ttl_hours = max(1, min(int(item.get("ttl_hours") or 24), 168))
                except (TypeError, ValueError):
                    ttl_hours = 24
                descriptor["ttl_hours"] = ttl_hours
        for text_name, limit in (("chart_type", 40), ("x_column", 120)):
            text_value = _clip(item.get(text_name), limit)
            if text_value:
                descriptor[text_name] = text_value
        y_columns = [_clip(column, 120) for column in _list(item.get("y_columns"))[:8] if _clip(column, 120)]
        if y_columns:
            descriptor["y_columns"] = y_columns
        for count_name in ("row_count", "plotted_row_count", "size_bytes"):
            try:
                count_value = int(item.get(count_name)) if item.get(count_name) is not None else None
            except (TypeError, ValueError):
                count_value = None
            if count_value is not None and count_value >= 0:
                descriptor[count_name] = count_value
        result.append(descriptor)
        if path:
            seen_paths.add(path)
    return result


# 함수 설명: `_collect_artifacts()`는 모든 단계의 descriptor를 경로 기준으로 중복 제거해 최종 Message/API용 별도 목록으로 만듭니다.
def _collect_artifacts(step_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for step in step_results:
        for descriptor in _project_artifacts(step.get("artifacts"), include_path=True):
            path = str(descriptor.get("path") or "")
            if not path or path in seen_paths:
                continue
            result.append(descriptor)
            seen_paths.add(path)
            if len(result) >= MAX_ARTIFACTS:
                return result
    return result


# 함수 설명: `_project_entity_ids()`는 최종 답변 근거로 필요한 식별자 preview만 최대 5종·종류별 10개로 제한합니다.
def _project_entity_ids(value: Any, entity_limit: int = 5, value_limit: int = 10) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(value)[:entity_limit]:
        if not isinstance(item, dict):
            continue
        values = [_clip(entry, 160) for entry in _list(item.get("values"))[:value_limit]]
        result.append(
            {
                "entity_type": _clip(item.get("entity_type"), 80),
                "column": _clip(item.get("column"), 100),
                "values": values,
                "observed_count": len(values),
                "total_count": item.get("total_count"),
                "complete": item.get("complete") is True and len(_list(item.get("values"))) <= value_limit,
            }
        )
    return result


# 함수 설명: `_project_issues()`는 사용자에게 설명 가능한 type/message만 최대 5건 보존합니다.
def _project_issues(value: Any, count: int = 5, text_limit: int = 300) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in _list(value)[:count]:
        if isinstance(item, dict):
            result.append({"type": _clip(item.get("type"), 100), "message": _clip(item.get("message"), text_limit)})
        else:
            result.append({"type": "message", "message": _clip(item, text_limit)})
    return result


# 함수 설명: `_fit_context_bytes()`는 최대 4개 step의 summary/preview를 단계적으로 줄여 전체 JSON 바이트 상한을 지킵니다.
def _fit_context_bytes(value: dict[str, Any], byte_limit: int) -> dict[str, Any]:
    result = deepcopy(value)
    if _json_bytes(result) <= byte_limit:
        return result
    for step in _list(result.get("steps")):
        if isinstance(step, dict):
            step["summary"] = _clip(step.get("summary"), 800)
            step["question"] = _clip(step.get("question"), 500)
            step["entity_ids"] = _project_entity_ids(step.get("entity_ids"), entity_limit=3, value_limit=5)
            step["artifacts"] = _project_artifacts(step.get("artifacts"), include_path=False, count=1)
            step["warnings"] = _project_issues(step.get("warnings"), count=2, text_limit=160)
            step["errors"] = _project_issues(step.get("errors"), count=2, text_limit=160)
    if _json_bytes(result) <= byte_limit:
        return result
    for step in _list(result.get("steps")):
        if isinstance(step, dict):
            step["summary"] = _clip(step.get("summary"), 350)
            step["question"] = _clip(step.get("question"), 200)
            step["entity_ids"] = []
            step["result_ref_meta"] = _project_ref_meta(step.get("result_ref_meta"), column_limit=5)
            step["result_ref"] = _clip(step.get("result_ref"), 256)
    if _json_bytes(result) <= byte_limit:
        return result
    result["steps"] = [
        {
            "step_index": step.get("step_index"),
            "step_id": step.get("step_id"),
            "tool_name": step.get("tool_name"),
            "status": step.get("status"),
            "summary": _clip(step.get("summary"), 180),
            "result_ref": _clip(step.get("result_ref"), 160),
            "artifacts": _project_artifacts(step.get("artifacts"), include_path=False, count=1),
            "errors": _project_issues(step.get("errors"), count=1, text_limit=100),
        }
        for step in _list(result.get("steps"))
        if isinstance(step, dict)
    ]
    result["context_truncated"] = True
    return result


# 함수 설명: `_execution_status()`는 단계 결과와 구조 오류를 complete/partial/error로 요약합니다.
def _execution_status(statuses: list[str], errors: list[dict[str, Any]]) -> str:
    if errors and not statuses:
        return "error"
    if statuses and all(status == "ok" for status in statuses) and not errors:
        return "complete"
    if any(status in {"ok", "partial"} for status in statuses):
        return "partial"
    return "error"


# 함수 설명: `_synthesis_instruction()`은 마지막 모델이 중간 ref나 내부 JSON을 노출하지 않고 근거·실패를 함께 설명하도록 지시합니다.
def _synthesis_instruction(execution_status: str) -> str:
    return (
        "사용자 원문 질문에 한국어로 한 번만 답변하세요. workflow_context.steps의 summary와 상태만 사실 근거로 사용하고 "
        "result_ref, 내부 계약명, Tool 호출 JSON은 사용자에게 노출하지 마세요. 성공한 단계 결과는 자연스럽게 종합하고, "
        "error 또는 blocked 단계가 있으면 누락된 결과와 이유를 숨기지 마세요. 근거가 없는 값은 추정하지 마세요. "
        f"현재 전체 실행 상태는 {execution_status}입니다."
    )


# 함수 설명: `_duplicates()`는 순서를 유지하며 두 번 이상 등장한 비어 있지 않은 문자열만 반환합니다.
def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value:
            continue
        if value in seen and value not in result:
            result.append(value)
        seen.add(value)
    return result


# 함수 설명: `_json_bytes()`는 최종 Context의 UTF-8 JSON 직렬화 크기를 계산합니다.
def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


# 함수 설명: `_bounded_int()`는 화면 최대 바이트 입력을 안전한 범위의 정수로 제한합니다.
def _bounded_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(str(value).strip()) if str(value or "").strip() else default
    except Exception:
        parsed = default
    return max(lower, min(parsed, upper))


# 함수 설명: `_safe_int()`는 외부 step index가 숫자가 아니어도 최종 응답 생성이 중단되지 않도록 0으로 정규화합니다.
def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


# 함수 설명: `_clip()`은 긴 문자열을 허용 길이에서 말줄임 처리합니다.
def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# 함수 설명: `_dict()`는 dict가 아닌 값을 빈 dict로 바꿉니다.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# 함수 설명: `_list()`는 list가 아닌 값을 빈 목록으로 바꿉니다.
def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# 함수 설명: `_issue()`는 최종 Context 구성 오류를 표준 type/message dict로 만듭니다.
def _issue(issue_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"type": issue_type, "message": message, **extra}


# Langflow 컴포넌트 클래스: Prompt Template 또는 native Agent의 변수 입력에 직접 연결할 Message들을 제공합니다.
class WorkflowFinalContextBuilder(Component):
    display_name = "02 최종 합성 Context 생성기"
    description = "Loop의 compact 단계 결과를 마지막 native 모델이 종합할 안전한 prompt 변수로 만듭니다."
    name = "WorkflowFinalContextBuilder"
    icon = "Braces"

    inputs = [
        HandleInput(
            name="loop_results",
            display_name="Loop 완료 결과",
            info="기본 Loop Done 포트가 반환한 단계 결과 모음입니다.",
            input_types=["Data", "DataFrame"],
            required=True,
        ),
        HandleInput(
            name="execution_context",
            display_name="Workflow 계획/실행 Context",
            info="00 parser의 workflow_plan을 연결해 계획 검증 실패도 최종 응답에 보존합니다.",
            input_types=["Data"],
            required=False,
            advanced=False,
        ),
        MessageTextInput(name="user_question", display_name="사용자 원문 질문", value="", required=False),
        MessageTextInput(
            name="max_context_bytes",
            display_name="최종 Context 최대 바이트",
            value=str(DEFAULT_MAX_CONTEXT_BYTES),
            required=False,
            advanced=True,
        ),
    ]
    outputs = [
        Output(name="question", display_name="사용자 질문", method="build_question", types=["Message"], group_outputs=True),
        Output(name="workflow_context", display_name="Workflow 실행 Context", method="build_workflow_context", types=["Message"], group_outputs=True),
        Output(name="synthesis_instruction", display_name="최종 합성 지시", method="build_instruction", types=["Message"], group_outputs=True),
        Output(name="final_context", display_name="최종 Context Data", method="build_context_data", types=["Data"], group_outputs=True),
    ]

    # 함수 설명: `_result_once()`는 네 출력이 같은 compact Context와 상태를 재사용하도록 결과를 한 번만 계산합니다.
    def _result_once(self) -> dict[str, Any]:
        loop_results = getattr(self, "loop_results", None)
        execution_context = getattr(self, "execution_context", None)
        cache_key = (id(loop_results), id(execution_context), str(getattr(self, "user_question", "")), str(getattr(self, "max_context_bytes", "")))
        if getattr(self, "_final_context_cache_key", None) != cache_key:
            self._final_context_cache_key = cache_key
            self._final_context_result = build_final_context(
                loop_results,
                execution_context,
                getattr(self, "user_question", ""),
                getattr(self, "max_context_bytes", DEFAULT_MAX_CONTEXT_BYTES),
            )
        return self._final_context_result

    # Langflow 출력 함수: 마지막 Prompt의 question 변수에 연결할 Message를 반환합니다.
    def build_question(self) -> Message:
        return Message(text=self._result_once()["question"])

    # Langflow 출력 함수: 마지막 Prompt의 workflow_context 변수에 연결할 compact JSON Message를 반환합니다.
    def build_workflow_context(self) -> Message:
        return Message(text=self._result_once()["workflow_context"])

    # Langflow 출력 함수: 마지막 Prompt의 synthesis_instruction 변수에 연결할 Message를 반환합니다.
    def build_instruction(self) -> Message:
        return Message(text=self._result_once()["synthesis_instruction"])

    # Langflow 출력 함수: 최종 응답 어댑터와 운영 진단에 사용할 전체 변수 Data를 반환합니다.
    def build_context_data(self) -> Data:
        result = deepcopy(self._result_once())
        self.status = {"status": result.get("status"), "context_bytes": result.get("context_bytes"), "errors": result.get("errors")}
        return Data(data=result)
