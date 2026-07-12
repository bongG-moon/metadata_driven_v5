# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 12 Goodocs 조회기
# 역할: Goodocs 문서 기반 source job을 실행하고, dummy 모드에서만 인증이 없을 때 fixture fallback을 허용합니다.
# 주요 입력: 페이로드 (payload) · 필수, Goodocs 사용자 ID (user_id), Goodocs 토큰 소스 (token_source), Goodocs 토큰 키 (token_key), 조회
#        제한 건수 (fetch_limit)
# 주요 출력: 조회 페이로드 (retrieval_payload)
# 처리 흐름: Goodocs 문서 또는 inline rows를 읽고 시스템 컬럼을 정리해 공통 source result 형식으로 반환합니다.
# 유지보수 포인트: 실행 오류를 다른 source의 성공처럼 위장하는 과도한 fallback은 만들지 말고 공통 errors 계약으로 전달합니다.
# =============================================================================

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


GOODOCS_SYSTEM_COLUMNS = {"ROW_INDEX", "LastUser", "LastTime", "LastEditType", "FirstUser", "FirstTime", "ROW_ID"}
PREVIEW_LIMIT = 5


# 내부 연동 도우미 클래스: 외부 라이브러리나 클라이언트 차이를 이 파일의 표준 호출 형태로 감쌉니다.
class Goodocs:
    # 함수 설명: `__init__()`는 외부 클라이언트나 실행 설정을 인스턴스에 보관해 뒤의 메서드가 같은 연결 문맥을 사용하게 합니다.
    def __init__(self, auth: dict[str, Any]):
        self.auth = auth

    # 주요 메서드: Goodocs 클라이언트에서 문서의 전체 행을 읽습니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
    def read_all(self) -> Any:
        raise RuntimeError("Goodocs class implementation is not configured. Paste the real Goodocs class into this component.")


# 주요 함수: Goodocs 또는 inline 데이터를 읽어 분석용 표준 행 목록으로 바꿉니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def goodocs_retrieve(
    payload_value: Any,
    user_id: Any = "",
    token_source: Any = "",
    token_key: Any = "",
    fetch_limit: Any = "",
) -> dict[str, Any]:
    payload = _payload(payload_value)
    jobs = _jobs_for_source(payload)
    if not jobs:
        return _skipped("goodocs", "no goodocs retrieval jobs")

    limit = _fetch_limit(fetch_limit or os.getenv("SOURCE_FETCH_LIMIT", "5000"))
    resolved_user_id = str(user_id or os.getenv("GOODOCS_USER_ID", "")).strip()
    resolved_token_source = str(token_source or os.getenv("GOODOCS_TOKEN_SOURCE", "")).strip()
    resolved_token_key = str(token_key or os.getenv("GOODOCS_TOKEN_KEY") or os.getenv("GOODOCS_TOKEN", "")).strip()
    retrieval_mode = _retrieval_mode(payload)
    results = [
        _run_goodocs_job(job, resolved_user_id, resolved_token_source, resolved_token_key, limit, retrieval_mode)
        for job in jobs
    ]
    errors = [error for result in results for error in result.get("errors", []) if isinstance(error, dict)]
    warnings = [warning for result in results for warning in result.get("warnings", []) if isinstance(warning, dict)]
    return {
        "source_type": "goodocs",
        "status": "error" if errors else "ok",
        "skipped": False,
        "executed_jobs": [str(job.get("job_id") or job.get("dataset_key") or index) for index, job in enumerate(jobs, 1)],
        "source_results": results,
        "errors": errors,
        "warnings": warnings,
    }


retrieve_goodocs_data = goodocs_retrieve


# 함수 설명: `_run_goodocs_job()`는 goodocs·조회 작업 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
def _run_goodocs_job(
    job: dict[str, Any],
    user_id: str,
    token_source: str,
    token_key: str,
    fetch_limit: int,
    retrieval_mode: str,
) -> dict[str, Any]:
    source_config = _source_config(job)
    params = _job_params(job)
    missing = _missing_required_params(params, _required_param_names(job, source_config))
    if missing:
        return _error_result(job, "missing_required_params", f"필수 파라미터가 없습니다: {', '.join(missing)}", params=params)

    doc_id = str(source_config.get("doc_id") or "").strip()
    sheet_name = str(source_config.get("sheet_name") or source_config.get("sheet") or "").strip()
    if not doc_id and not _has_inline_rows(source_config):
        return _error_result(job, "missing_doc_id", "Goodocs source_config에 doc_id가 없습니다.", params=params)

    inline_rows = _inline_rows(source_config)
    if inline_rows:
        rows = _drop_system_columns([_row_dict(row) for row in inline_rows])[:fetch_limit]
        return _standard_result(job, rows, params, source_config, used_dummy_data=False, source_configured=True)

    credentials = {"USER_ID": user_id, "TOKEN_SOURCE": token_source, "TOKEN_KEY": token_key}
    if not any(str(value or "").strip() for value in credentials.values()):
        if retrieval_mode == "live":
            return _error_result(
                job,
                "missing_goodocs_credentials",
                "live 모드에서는 Goodocs 인증 누락 시 dummy fallback을 사용하지 않습니다.",
                params=params,
            )
        rows = _dummy_rows(job, doc_id)[:fetch_limit]
        return _standard_result(job, rows, params, source_config, used_dummy_data=True, source_configured=False)

    missing_credentials = [key for key, value in credentials.items() if not str(value or "").strip()]
    if missing_credentials:
        return _error_result(
            job,
            "missing_goodocs_credentials",
            f"Goodocs 인증 값이 없습니다: {', '.join(missing_credentials)}",
            params=params,
        )

    auth = {"USER_ID": user_id, "DOC_ID": doc_id, "TOKEN_SOURCE": token_source, "TOKEN_KEY": token_key}
    if sheet_name:
        auth["SHEET_NAME"] = sheet_name
    try:
        goodocs_cls = _goodocs_class()
        goodocs = goodocs_cls(auth)
        if sheet_name and hasattr(goodocs, "read_sheet"):
            frame = goodocs.read_sheet(sheet_name)
        else:
            frame = goodocs.read_all()
        rows = _frame_to_rows(frame)[:fetch_limit]
        return _standard_result(job, rows, params, source_config, used_dummy_data=False, source_configured=True)
    except Exception as exc:
        return _error_result(job, "goodocs_retrieval_failed", f"Goodocs 조회 실패: {exc}", params=params)


# 함수 설명: `_goodocs_class()`는 테스트 override가 있으면 우선 사용하고 아니면 기본 Goodocs 클라이언트 class를 가져옵니다.
def _goodocs_class() -> Any:
    override = getattr(GoodocsRetriever, "goodocs_class", None) if "GoodocsRetriever" in globals() else None
    if override is not None:
        return override
    return Goodocs


# 함수 설명: `_jobs_for_source()`는 전체 조회 작업 중 지정한 source type에 해당하는 작업만 골라냅니다.
def _jobs_for_source(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    bundle_jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if bundle_jobs:
        return [
            deepcopy(job)
            for job in bundle_jobs
            if isinstance(job, dict)
            and _source_type(job.get("source_type") or _source_config(job).get("source_type")) in {"goodocs", "goodoc", "godocs"}
        ]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else payload
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    return [
        deepcopy(job)
        for job in jobs
        if isinstance(job, dict) and _source_type(job.get("source_type") or _source_config(job).get("source_type")) in {"goodocs", "goodoc", "godocs"}
    ]


# 함수 설명: `_source_config()`는 조회 작업 또는 카탈로그에서 허용된 데이터 소스 설정만 dict로 꺼냅니다.
def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("doc_id", "document_id", "sheet_name", "sheet", "range", "table_name", "columns", "required_columns", "rows", "data", "items"):
        if job.get(key) not in (None, "", [], {}):
            config.setdefault(key, deepcopy(job[key]))
    if config.get("document_id") and not config.get("doc_id"):
        config["doc_id"] = config["document_id"]
    return config


# 함수 설명: `_job_params()`는 조회 작업의 params를 안전한 dict로 정리해 retriever에 전달합니다.
def _job_params(job: dict[str, Any]) -> dict[str, Any]:
    if isinstance(job.get("params"), dict):
        return deepcopy(job["params"])
    if isinstance(job.get("required_params"), dict):
        return deepcopy(job["required_params"])
    return {}


# 함수 설명: `_required_param_names()`는 카탈로그 설정에서 실행 전에 반드시 있어야 하는 파라미터 이름을 추출합니다.
def _required_param_names(job: dict[str, Any], source_config: dict[str, Any]) -> list[Any]:
    if isinstance(source_config.get("required_params"), (list, tuple, set)):
        return _as_list(source_config.get("required_params"))
    if isinstance(job.get("required_param_names"), (list, tuple, set)):
        return _as_list(job.get("required_param_names"))
    if not isinstance(job.get("required_params"), dict):
        return _as_list(job.get("required_params"))
    return []


# 함수 설명: `_standard_result()`는 정상 조회 결과를 dataset/source alias와 rows가 포함된 공통 결과 구조로 만듭니다.
def _standard_result(
    job: dict[str, Any],
    rows: list[dict[str, Any]],
    params: dict[str, Any],
    source_config: dict[str, Any],
    used_dummy_data: bool,
    source_configured: bool,
) -> dict[str, Any]:
    return {
        "success": True,
        "source_alias": job.get("source_alias") or job.get("dataset_key"),
        "dataset_key": job.get("dataset_key"),
        "source_type": "goodocs",
        "status": "ok",
        "row_count": len(rows),
        "columns": _rows_columns(rows),
        "preview_rows": rows[:PREVIEW_LIMIT],
        "rows": rows,
        "applied_params": deepcopy(params),
        "applied_filters": deepcopy(job.get("filters", [])),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "summary": f"{job.get('dataset_key', 'source')} goodocs retrieval complete: {len(rows)} rows",
        "source_execution": {
            "used_dummy_data": used_dummy_data,
            "adapter": "goodocs",
            "doc_id": source_config.get("doc_id") or "",
            "sheet_name": source_config.get("sheet_name") or source_config.get("sheet") or "",
            "range": source_config.get("range") or "",
            "source_configured": source_configured,
            "filters_applied_in_retriever": False,
        },
        "warnings": [],
        "errors": [],
    }


# 함수 설명: `_error_result()`는 예외 정보를 공통 errors 배열과 status가 포함된 실패 결과 구조로 만듭니다.
def _error_result(job: dict[str, Any], error_type: str, message: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    error = {"type": error_type, "message": message, "dataset_key": job.get("dataset_key", "")}
    return {
        "success": False,
        "source_alias": job.get("source_alias") or job.get("dataset_key"),
        "dataset_key": job.get("dataset_key"),
        "source_type": "goodocs",
        "status": "error",
        "row_count": 0,
        "columns": [],
        "preview_rows": [],
        "rows": [],
        "applied_params": deepcopy(params if params is not None else _job_params(job)),
        "applied_filters": deepcopy(job.get("filters", [])),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "summary": "",
        "failure_type": error_type,
        "error_message": message,
        "source_execution": {"used_dummy_data": False, "adapter": "goodocs", "source_configured": False},
        "warnings": [],
        "errors": [error],
    }


# 함수 설명: `_dummy_rows()`는 행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _dummy_rows(job: dict[str, Any], doc_id: str) -> list[dict[str, Any]]:
    params = _job_params(job)
    rows = []
    for index in range(20):
        rows.append(
            {
                "DATE": params.get("DATE", "2026-06-12"),
                "TECH": "TSV" if index % 4 == 0 else "FC",
                "DEN": "2048G" if index % 4 == 0 else "128G",
                "MODE": "HBM3E" if index % 4 == 0 else "LPDDR5",
                "PKG_TYPE1": "HBM" if index % 4 == 0 else "UFBGA",
                "PKG_TYPE2": "HBM" if index % 4 == 0 else "MOBILE",
                "LEAD": "LF",
                "MCP_NO": "H-HBM16E" if index % 4 == 0 else "EMPTY",
                "INPUT_PLAN": 120000 + index * 2000,
                "OUT_PLAN": 90000 + index * 1500,
                "doc_id": doc_id,
            }
        )
    return rows


# 함수 설명: `_frame_to_rows()`는 TO·행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _frame_to_rows(frame: Any) -> list[dict[str, Any]]:
    if hasattr(frame, "reset_index"):
        try:
            frame = frame.reset_index(drop=True)
        except Exception:
            pass
    if hasattr(frame, "drop"):
        try:
            drop_columns = [column for column in GOODOCS_SYSTEM_COLUMNS if column in getattr(frame, "columns", [])]
            if drop_columns:
                frame = frame.drop(columns=drop_columns)
        except Exception:
            pass
    return _drop_system_columns([_row_dict(row) for row in _rows_from_value(frame)])


# 함수 설명: `_inline_rows()`는 행 목록을 표준 행 목록으로 생성하거나 입력 행 중 필요한 부분만 선택합니다.
def _inline_rows(source_config: dict[str, Any]) -> list[Any]:
    for key in ("rows", "data", "items"):
        if isinstance(source_config.get(key), list):
            return deepcopy(source_config[key])
    return []


# 함수 설명: `_has_inline_rows()`는 입력값이 inline·행 목록 조건에 해당하는지 부작용 없이 bool로 판정합니다.
def _has_inline_rows(source_config: dict[str, Any]) -> bool:
    return bool(_inline_rows(source_config))


# 함수 설명: `_rows_from_value()`는 외부 클라이언트의 DataFrame·list·dict 결과를 공통 dict 행 목록으로 변환합니다.
def _rows_from_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("rows", "data", "items", "result", "results", "records"):
            if isinstance(value.get(key), list):
                return value[key]
            if isinstance(value.get(key), dict):
                nested = _rows_from_value(value[key])
                if nested:
                    return nested
        return [value]
    if value is None or (isinstance(value, str) and value == ""):
        return []
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict("records")
    return [{"value": value}]


# 함수 설명: `_drop_system_columns()`는 system·컬럼에서 후속 단계에 불필요하거나 노출하면 안 되는 부분을 제거합니다.
def _drop_system_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if str(key) not in GOODOCS_SYSTEM_COLUMNS} for row in rows]


# 함수 설명: `_row_dict()`는 객체·매핑·튜플 형태의 한 행을 컬럼명이 있는 dict 행으로 변환합니다.
def _row_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return {"value": _json_ready(value)}


# 함수 설명: `_rows_columns()`는 행 목록과 명시 컬럼을 함께 정규화해 표준 rows/columns 쌍을 만듭니다.
def _rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


# 함수 설명: `_json_ready()`는 datetime·Decimal·NaN 등 JSON이 직접 표현하지 못하는 값을 안전한 기본형으로 재귀 변환합니다.
def _json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item) for item in value]
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


# 함수 설명: `_missing_required_params()`는 필수 파라미터 중 실제 작업 값에 없는 항목을 찾아 오류 목록으로 반환합니다.
def _missing_required_params(params: dict[str, Any], required_params: Any) -> list[str]:
    missing = []
    for item in _as_list(required_params):
        key = str(item or "").strip()
        if key and _dict_get_ci(params, key) in (None, "", []):
            missing.append(key)
    return missing


# 함수 설명: `_fetch_limit()`는 설정된 조회 제한을 안전한 정수 범위로 보정합니다.
def _fetch_limit(value: Any) -> int:
    try:
        return max(1, int(value or 5000))
    except Exception:
        return 5000


# 함수 설명: `_source_type()`는 조회 작업의 source type을 표준 소문자 식별자로 정규화합니다.
def _source_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


# 함수 설명: `_retrieval_mode()`는 request와 routed bundle에서 현재 dummy/live 실행 모드를 확인합니다.
def _retrieval_mode(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    mode = str(request.get("retrieval_mode") or bundle.get("retrieval_mode") or "dummy").strip().lower()
    return "live" if mode in {"live", "actual", "real", "실제", "true", "on", "1", "yes"} else "dummy"


# 함수 설명: `_normalize_key()`는 key의 대소문자·공백·구분자 차이를 제거해 비교 가능한 표준 식별자로 바꿉니다.
def _normalize_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


# 함수 설명: `_dict_get_ci()`는 키의 대소문자 차이를 무시하고 dict에서 요청한 값을 찾습니다.
def _dict_get_ci(mapping: dict[str, Any], key: Any, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    text = str(key or "").strip()
    if text in mapping:
        return mapping[text]
    normalized = _normalize_key(text)
    for item_key, value in mapping.items():
        if _normalize_key(item_key) == normalized:
            return value
    return default


# 함수 설명: `_as_list()`는 단일 값과 여러 값 입력을 모두 같은 list 형태로 맞춰 반복 처리를 단순화합니다.
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return deepcopy(data)
    text = getattr(value, "text", None) or getattr(value, "content", None)
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"text": text}
        except Exception:
            return {"text": text}
    return {}


# 함수 설명: `_skipped()`는 설정이나 대상 작업이 없어 실행하지 않은 이유를 표준 skipped 결과로 남깁니다.
def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class GoodocsRetriever(Component):
    goodocs_class = None

    display_name = "12 Goodocs 조회기"
    description = "Goodocs 문서 기반 source job을 실행하며, live 모드에서는 인증 누락을 오류로 반환합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="user_id", display_name="Goodocs 사용자 ID", required=False, value="", advanced=True),
        MessageTextInput(name="token_source", display_name="Goodocs 토큰 소스", required=False, value="", advanced=True),
        MessageTextInput(name="token_key", display_name="Goodocs 토큰 키", required=False, value="", advanced=True),
        MessageTextInput(name="fetch_limit", display_name="조회 제한 건수", required=False, value="5000", advanced=True),
    ]
    outputs = [Output(name="retrieval_payload", display_name="조회 페이로드", method="build_payload")]

    # Langflow 출력 함수: '조회 페이로드 (retrieval_payload)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=goodocs_retrieve(
                getattr(self, "payload", None),
                getattr(self, "user_id", ""),
                getattr(self, "token_source", ""),
                getattr(self, "token_key", ""),
                getattr(self, "fetch_limit", ""),
            )
        )
