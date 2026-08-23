# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 22 API 응답 생성기
# 역할: 최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다.
# 주요 입력: 페이로드 (payload) · 필수, 채팅 표시 메시지 (display_message)
# 주요 출력: API 응답 (api_response)
# 처리 흐름: 웹/API 소비자가 필요한 결과만 남기고 runtime source와 대용량 내부 필드를 제거한 응답 envelope을 만듭니다.
# 유지보수 포인트: inputs/outputs의 name은 Langflow JSON edge 계약이므로 변경 시 모든 Flow JSON을 재생성하고 source sync 검증을 실행해야 합니다.
# =============================================================================

from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, IntInput, MessageTextInput, Output
from lfx.schema.data import Data

MAX_INTERMEDIATE_TABLE_COUNT = 8
DEFAULT_INTERMEDIATE_PREVIEW_ROWS = 5
MAX_INTERMEDIATE_PREVIEW_ROWS = 5
MAX_PUBLIC_ARTIFACTS = 8
MAX_ARTIFACT_TEXT_LENGTH = 500

# The execution-trace publisher owns creation of this descriptor.  The API
# boundary only passes through the safe, display-ready metadata, never HTML,
# source rows, trace payloads, or publisher internals.
ANALYSIS_EXECUTION_REPORT_TYPE = "analysis_execution_report"
ANALYSIS_EXECUTION_HTML_ARTIFACT_TYPE = "analysis_execution_html"

# API contract: final rows remain in data.rows; curated checkpoints are projected
# into intermediate_tables so a web client can render them without parsing the message.

# 주요 함수: 내부 실행 필드를 제거하고 외부 API가 소비할 안정적인 응답을 만듭니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def build_api_response(
    payload_value: Any,
    display_message_value: Any = "",
    intermediate_preview_limit: Any = DEFAULT_INTERMEDIATE_PREVIEW_ROWS,
) -> dict[str, Any]:
    payload = _payload(payload_value)
    answer_message = str(payload.get("answer_message") or "")
    display_message = _text(display_message_value) or answer_message
    status, stage_status = _pipeline_status(payload)
    intermediate_tables = _build_intermediate_tables(
        payload,
        _intermediate_preview_limit(intermediate_preview_limit),
    )
    confirmation_items = _blocked_retrieval_confirmation_items(payload)
    response = {
        "response_type": "data_analysis",
        "status": status,
        "stage_status": stage_status,
        "message": display_message,
        "data_mode": _data_mode(payload),
        "answer_sections": _answer_sections_with_intermediate_tables(
            payload.get("answer_sections"),
            intermediate_tables,
        ),
        "request": payload.get("request", {}),
        "intent_plan": payload.get("intent_plan", {}),
        "analysis": _without_intermediate_results(payload.get("analysis")),
        "data": payload.get("data", {}),
        "intermediate_tables": intermediate_tables,
        "data_refs": payload.get("data_refs", []),
        "download_manifest": payload.get("download_manifest", []),
        "artifacts": _public_artifacts(payload),
        "state": payload.get("state", {}),
        "trace": _without_intermediate_results(payload.get("trace")),
    }
    # Keep the existing envelope unchanged unless the Answer Builder explicitly
    # publishes a sanitized confirmation section. API consumers then receive a
    # small display-ready projection and never need to parse trace internals.
    if confirmation_items:
        response["confirmation_items"] = confirmation_items
    return response


# 함수 설명: `_blocked_retrieval_confirmation_items()`는 Answer Builder가 안전하게 정제한 사용자 확인 사유만 API 최상위 계약으로 투영합니다.
def _blocked_retrieval_confirmation_items(payload: dict[str, Any]) -> list[str]:
    """Copy the canonical answer section without independently classifying failures.

    Raw trace reasoning and retrieval errors remain diagnostic data. The
    Answer Builder owns gate eligibility, canonicalization, filtering, and
    wording, so this API boundary never creates a second explanation.
    """
    answer_sections = payload.get("answer_sections")
    if not isinstance(answer_sections, dict):
        return []
    return _confirmation_items_from_section(answer_sections.get("confirmation_required"))


# 함수 설명: `_confirmation_items_from_section()`는 Answer Builder가 정제한 confirmation_required 항목을 변경 없이 API로 전달합니다.
def _confirmation_items_from_section(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    items = value.get("items")
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        return []
    return deepcopy(items)


# 주요 함수: 중간 결과 체크포인트를 웹 테이블 계약으로 투영합니다.
def _answer_sections_with_intermediate_tables(value: Any, tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep table metadata in answer_sections while rows have one API owner."""
    sections = _without_intermediate_results(value) if isinstance(value, dict) else {}
    sections["intermediate_tables"] = [
        {
            "render_type": "table",
            "table_id": table["table_id"],
            "title": table["title"],
            "role": table["role"],
            "checkpoint_key": table["checkpoint_key"],
            "columns": deepcopy(table["columns"]),
            "display_columns": deepcopy(table["display_columns"]),
            "column_labels": deepcopy(table["column_labels"]),
            "row_source": table["row_source"],
            "row_count": table["row_count"],
            "preview_row_count": table["preview_row_count"],
            "preview_only": table["preview_only"],
            **({"download": deepcopy(table["download"])} if table.get("download") else {}),
        }
        for table in tables
    ]
    return sections


# 함수 설명: `_build_intermediate_tables()`는 공개 가능한 체크포인트만 웹 테이블 형식으로 정규화합니다.
def _build_intermediate_tables(payload: dict[str, Any], preview_limit: int) -> list[dict[str, Any]]:
    """Project curated checkpoints into a direct, web-renderable table contract."""
    downloads = _intermediate_downloads(payload)
    tables: list[dict[str, Any]] = []
    for index, item in enumerate(_selected_intermediate_results(payload)[:MAX_INTERMEDIATE_TABLE_COUNT]):
        preview_rows = [
            deepcopy(row)
            for row in item.get("preview_rows", [])
            if isinstance(row, dict)
        ][:preview_limit]
        columns = _string_list(item.get("columns")) or _columns_from_rows(preview_rows)
        configured_display_columns = _string_list(item.get("display_columns"))
        display_columns = [column for column in configured_display_columns if column in columns] or list(columns)
        raw_labels = item.get("column_labels") if isinstance(item.get("column_labels"), dict) else {}
        column_labels = {
            str(column): _text(label)
            for column, label in raw_labels.items()
            if str(column) in columns and _text(label)
        }
        download_key = _text(item.get("download_key"))
        checkpoint_key = _text(item.get("key")) or download_key
        title = (
            _text(item.get("description"))
            or _text(item.get("label"))
            or checkpoint_key
            or "중간 결과"
        )
        row_count = _safe_int(item.get("row_count"), len(preview_rows))
        table = {
            "render_type": "table",
            "table_id": f"intermediate:{download_key or checkpoint_key or index + 1}",
            "title": title,
            "role": _text(item.get("role")) or "intermediate_result",
            "checkpoint_key": checkpoint_key,
            "download_key": download_key,
            "columns": columns,
            "display_columns": display_columns,
            "column_labels": column_labels,
            "rows": preview_rows,
            "row_source": f"intermediate_tables[{index}].rows",
            "row_count": row_count,
            "preview_row_count": len(preview_rows),
            "preview_only": row_count > len(preview_rows),
        }
        if download_key and download_key in downloads:
            table["download"] = downloads[download_key]
        tables.append(table)
    return tables


# 함수 설명: `_selected_intermediate_results()`는 다운로드 가능 결과를 우선하고 레거시 결과는 마지막 계산값만 선택합니다.
def _selected_intermediate_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = _intermediate_candidates(payload)
    if not items:
        return []
    published = [item for item in items if _text(item.get("download_key"))]
    if published:
        return published[:MAX_INTERMEDIATE_TABLE_COUNT]
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    if _normalize_status(analysis.get("status"), default="error") == "ok":
        computed = [item for item in items if _text(item.get("role")) == "computed_result"]
        if computed:
            return [computed[-1]]
    return [items[-1]]


# 함수 설명: `_intermediate_candidates()`는 공개 payload와 제한된 진단 영역에서 중간 결과 후보를 찾습니다.
def _intermediate_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    pandas_execution = (
        inspection.get("pandas_execution")
        if isinstance(inspection.get("pandas_execution"), dict)
        else {}
    )
    answer_sections = payload.get("answer_sections") if isinstance(payload.get("answer_sections"), dict) else {}
    evidence = answer_sections.get("evidence") if isinstance(answer_sections.get("evidence"), dict) else {}
    for value in (
        payload.get("intermediate_results"),
        analysis.get("intermediate_results"),
        pandas_execution.get("intermediate_results"),
        evidence.get("intermediate_results"),
    ):
        items = [deepcopy(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        if items:
            return items
    return []


# 함수 설명: `_intermediate_downloads()`는 저장소 data reference를 다운로드 정보로 연결합니다.
def _intermediate_downloads(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    downloads: dict[str, dict[str, str]] = {}
    refs = payload.get("data_refs") if isinstance(payload.get("data_refs"), list) else []
    for ref in refs:
        if not isinstance(ref, dict) or _text(ref.get("role")) != "intermediate_result":
            continue
        path = _text(ref.get("path"))
        prefix = "payload.intermediate_rows."
        download_key = path[len(prefix):] if path.startswith(prefix) else _text(ref.get("download_key"))
        download_url = _text(ref.get("download_url"))
        if not download_key or not download_url:
            continue
        downloads[download_key] = {
            "url": download_url,
            "format": _text(ref.get("download_format")) or "csv",
            "expires_at": _text(ref.get("expires_at")),
        }
    return downloads


# 함수 설명: `_string_list()`는 비어 있지 않은 문자열 목록만 안전하게 반환합니다.
def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


# 함수 설명: `_columns_from_rows()`는 미리보기 행의 키 순서로 표시 컬럼을 추론합니다.
def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            column = str(key)
            if column and column not in columns:
                columns.append(column)
    return columns


# 함수 설명: `_safe_int()`는 잘못된 행 수 값을 안전한 정수 기본값으로 바꿉니다.
def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


# 함수 설명: `_intermediate_preview_limit()`는 API 중간 결과 미리보기 행 수를 1~5 범위로 제한합니다.
def _intermediate_preview_limit(value: Any) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERMEDIATE_PREVIEW_ROWS
    return max(1, min(MAX_INTERMEDIATE_PREVIEW_ROWS, resolved))


# 함수 설명: `_without_intermediate_results()`는 중간 행을 전용 `intermediate_tables`에만 남깁니다.
def _without_intermediate_results(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_intermediate_results(item)
            for key, item in value.items()
            if key != "intermediate_results"
        }
    if isinstance(value, list):
        return [_without_intermediate_results(item) for item in value]
    return deepcopy(value)


# 함수 설명: pipeline_status는 조회와 pandas 분석 상태를 함께 평가해 ok·partial·error를 결정합니다.
def _pipeline_status(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    analysis_status = _normalize_status(analysis.get("status"), default="error")
    retrieval_status = _retrieval_status(payload)

    if analysis_status == "error" or retrieval_status == "error":
        overall = "error"
    elif analysis_status == "partial" or retrieval_status == "partial":
        overall = "partial"
    else:
        overall = "ok"
    return overall, {"overall": overall, "retrieval": retrieval_status, "analysis": analysis_status}


# 함수 설명: `_retrieval_status()`는 필수 조회 작업별 성공·실패와 검증 오류를 집계합니다.
def _retrieval_status(payload: dict[str, Any]) -> str:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    inspection = trace.get("inspection") if isinstance(trace.get("inspection"), dict) else {}
    retrieval_inspection = inspection.get("data_retrieval") if isinstance(inspection.get("data_retrieval"), dict) else {}
    validation = retrieval_inspection.get("job_validation") if isinstance(retrieval_inspection.get("job_validation"), dict) else {}
    hydration = inspection.get("catalog_hydration") if isinstance(inspection.get("catalog_hydration"), dict) else {}
    if _positive_int(validation.get("error_count")) or _normalize_status(hydration.get("status"), default="ok") == "error":
        return "error"
    inspection_status = _normalize_status(retrieval_inspection.get("status"), default="ok")

    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = [job for job in plan.get("retrieval_jobs", []) if isinstance(job, dict)] if isinstance(plan.get("retrieval_jobs"), list) else []
    source_results = [item for item in payload.get("source_results", []) if isinstance(item, dict)] if isinstance(payload.get("source_results"), list) else []
    # 조회 작업이 없는 직접/재사용 응답은 분석 단계 상태만으로 판단합니다.
    if not jobs and not source_results and not retrieval_inspection:
        return "ok"

    result_by_alias = {
        str(item.get("source_alias") or item.get("dataset_key") or "").strip(): item
        for item in source_results
        if str(item.get("source_alias") or item.get("dataset_key") or "").strip()
    }
    statuses: list[tuple[str, bool]] = []
    for job in jobs:
        alias = str(job.get("source_alias") or job.get("dataset_key") or "").strip()
        if not alias:
            continue
        result = result_by_alias.get(alias)
        statuses.append(("error" if result is None else _source_status(result), _job_required(job)))
    if not statuses:
        statuses = [(_source_status(item), True) for item in source_results]

    if not statuses:
        return "error" if inspection_status == "error" else inspection_status
    required_failed = any(status == "error" and required for status, required in statuses)
    optional_failed = any(status == "error" and not required for status, required in statuses)
    if required_failed:
        return "error"
    if optional_failed:
        return "partial"
    if inspection_status == "error":
        return "error"
    return "partial" if any(status == "partial" for status, _ in statuses) else "ok"


# 함수 설명: `_source_status()`는 개별 source result의 status·success·errors를 하나의 상태로 정규화합니다.
def _source_status(source: dict[str, Any]) -> str:
    if source.get("success") is False or source.get("errors"):
        return "error"
    return _normalize_status(source.get("status"), default="ok")


# 함수 설명: `_job_required()`는 required=false가 명시된 source만 선택 항목으로 판정합니다.
def _job_required(job: dict[str, Any]) -> bool:
    value = job.get("required", True)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"false", "0", "no", "off", "optional", "선택"}


# 함수 설명: `_normalize_status()`는 다양한 내부 상태 문자열을 외부 계약의 ok·partial·error로 제한합니다.
def _normalize_status(value: Any, default: str = "ok") -> str:
    text = str(value or "").strip().lower()
    if text in {"error", "failed", "failure", "invalid"}:
        return "error"
    if text in {"partial", "warning", "degraded"}:
        return "partial"
    if text in {"ok", "success", "completed", "complete"}:
        return "ok"
    return default


# 함수 설명: `_positive_int()`는 오류 개수처럼 0보다 큰 값인지 안전하게 확인합니다.
def _positive_int(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except Exception:
        return False


# 함수 설명: `_data_mode()`는 payload의 retrieval_mode와 source 결과를 확인해 dummy/live 응답 표시 모드를 결정합니다.
def _data_mode(payload: dict[str, Any]) -> str:
    source_results = payload.get("source_results") if isinstance(payload.get("source_results"), list) else []
    for source in source_results:
        if not isinstance(source, dict):
            continue
        execution = source.get("source_execution") if isinstance(source.get("source_execution"), dict) else {}
        if (
            execution.get("used_dummy_data") is True
            or source.get("dummy") is True
            or str(source.get("source_type") or "").strip().lower() == "dummy"
        ):
            return "dummy"
    return "live"


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    data = getattr(value, "data", value)
    if not isinstance(data, dict):
        return {}
    excluded = {"runtime_sources", "_runtime_rows_by_alias", "_full_result_rows", "_runtime_result_rows"}
    return {key: deepcopy(item) for key, item in data.items() if key not in excluded}


# 함수 설명: `_public_artifacts()`는 API가 안전하게 전달할 수 있는 분석 과정 HTML
# artifact만 독립 필드로 투영합니다. data_refs나 download_manifest에 합치지 않아
# 데이터 파일과 실행 설명서를 클라이언트가 명확히 구분할 수 있게 합니다.
def _public_artifacts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    values = payload.get("artifacts")
    if not isinstance(values, list):
        return []

    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        descriptor = _public_analysis_execution_artifact(value)
        if not descriptor:
            continue
        signature = "|".join(
            (
                _text(descriptor.get("report_id")),
                _text(descriptor.get("view_url")),
                _text(descriptor.get("download_url")),
            )
        )
        if signature in seen:
            continue
        seen.add(signature)
        artifacts.append(descriptor)
        if len(artifacts) >= MAX_PUBLIC_ARTIFACTS:
            break
    return artifacts


# 함수 설명: `_public_analysis_execution_artifact()`는 실행 과정 HTML descriptor의
# 허용 필드만 복사합니다. publisher 오류·raw HTML·진단 원문처럼 API에 불필요한
# 내부 값은 포함하지 않습니다.
def _public_analysis_execution_artifact(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not _is_analysis_execution_artifact(value):
        return {}

    status = _artifact_text(value.get("status"), 40).lower()
    if status in {"error", "failed", "failure", "unavailable", "disabled", "skipped"}:
        return {}
    view_url = _public_artifact_url(value.get("view_url"))
    download_url = _public_artifact_url(value.get("download_url"))
    if not view_url and not download_url:
        return {}

    descriptor = {
        "artifact_type": ANALYSIS_EXECUTION_HTML_ARTIFACT_TYPE,
        "type": ANALYSIS_EXECUTION_REPORT_TYPE,
        "status": status or "ok",
        "title": _artifact_text(value.get("title"), 200) or "분석 처리 과정",
        "label": _artifact_text(value.get("label"), 200) or "분석 처리 과정 HTML",
        "mime_type": _artifact_text(value.get("mime_type"), 120) or "text/html",
    }
    for key in ("report_id", "expires_at", "storage_backend"):
        text = _artifact_text(value.get(key), MAX_ARTIFACT_TEXT_LENGTH)
        if text:
            descriptor[key] = text
    if view_url:
        descriptor["view_url"] = view_url
    if download_url:
        descriptor["download_url"] = download_url
    ttl_hours = _artifact_ttl_hours(value.get("ttl_hours"))
    if ttl_hours:
        descriptor["ttl_hours"] = ttl_hours
    return descriptor


# 함수 설명: type 또는 artifact_type 중 하나가 발행기 계약과 일치할 때만 분석
# 과정 HTML artifact로 인식합니다. 임의 URL 객체가 API 링크로 노출되는 것을 막습니다.
def _is_analysis_execution_artifact(value: dict[str, Any]) -> bool:
    descriptor_type = _artifact_text(value.get("type"), 80).lower()
    artifact_type = _artifact_text(value.get("artifact_type"), 80).lower()
    return (
        descriptor_type == ANALYSIS_EXECUTION_REPORT_TYPE
        or artifact_type == ANALYSIS_EXECUTION_HTML_ARTIFACT_TYPE
    )


# 함수 설명: `_public_artifact_url()`는 report API가 반환한 public HTTP(S) URL만
# 허용합니다. 인증 정보, 제어문자, fragment는 API 응답과 클라이언트 링크에서 제외합니다.
def _public_artifact_url(value: Any) -> str:
    raw_url = str(value or "").strip()
    if any(ord(character) < 32 for character in raw_url):
        return ""
    url = _artifact_text(raw_url, 4_096)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return ""
    return url


# 함수 설명: `_artifact_text()`는 descriptor의 사용자/API 표시 텍스트에서 제어문자와
# 과도한 길이를 제거합니다. URL은 `_public_artifact_url()`에서 추가 검증합니다.
def _artifact_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(character for character in text if ord(character) >= 32)
    return text[:limit].strip()


# 함수 설명: `_artifact_ttl_hours()`는 실행 과정 Report의 보관 시간을 API 숫자
# 계약으로 제한합니다. 0·음수·비숫자 값은 생략합니다.
def _artifact_ttl_hours(value: Any) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return 0
    return resolved if 0 < resolved <= 24 * 7 else 0


# 함수 설명: `_text()`는 Message나 일반 값을 앞뒤 공백이 정리된 문자열로 변환합니다.
def _text(value: Any) -> str:
    if value is None:
        return ""
    text = getattr(value, "text", value)
    if text is None:
        return ""
    return str(text).strip()


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class ApiResponseBuilder(Component):
    display_name = "22 API 응답 생성기"
    description = "최종 API 응답을 만들고 전체 런타임 소스 데이터를 제거합니다."

    # 함수 설명: 이 컴포넌트 자체가 Flow의 구조화 최종 출력임을 Langflow에 알립니다.
    # 코드를 저장하면 Langflow가 graph output 메타데이터를 자동 생성하므로 Flow JSON을 직접 수정할 필요가 없습니다.
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.is_output = True

    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="display_message", display_name="채팅 표시 메시지", required=False),
        IntInput(
            name="intermediate_preview_limit",
            display_name="중간 결과 미리보기 행 수",
            info="API 중간 결과 표에 표시할 행 수입니다. 1~5 범위로 적용됩니다.",
            value=DEFAULT_INTERMEDIATE_PREVIEW_ROWS,
            required=False,
            advanced=True,
        ),
    ]
    outputs = [Output(name="api_response", display_name="API 응답", method="build_payload")]

    # Langflow 출력 함수: 'API 응답 (api_response)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=build_api_response(
                getattr(self, "payload", None),
                getattr(self, "display_message", ""),
                getattr(self, "intermediate_preview_limit", DEFAULT_INTERMEDIATE_PREVIEW_ROWS),
            )
        )
