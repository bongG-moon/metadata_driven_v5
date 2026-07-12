# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 10 H-API 데이터 조회기
# 역할: table catalog의 H-API source_config를 사용해 실제 API 조회를 실행합니다.
# 주요 입력: 페이로드 (payload) · 필수, H-API 토큰 (api_token), 요청 제한 시간(초) (timeout_seconds), 조회 제한 건수 (fetch_limit)
# 주요 출력: 조회 페이로드 (retrieval_payload)
# 처리 흐름: 카탈로그 설정으로 HTTP 요청을 만들고 응답 경로에서 행을 추출해 공통 source result 형식으로 반환합니다.
# 유지보수 포인트: 실행 오류를 다른 source의 성공처럼 위장하는 과도한 fallback은 만들지 말고 공통 errors 계약으로 전달합니다.
# =============================================================================

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


PREVIEW_LIMIT = 5


# 주요 함수: HTTP API 작업을 실행하고 지정된 응답 경로에서 결과 행을 꺼냅니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
def h_api_retrieve(
    payload_value: Any,
    api_token: Any = "",
    timeout_seconds: Any = "",
    fetch_limit: Any = "",
    opener: Any = None,
) -> dict[str, Any]:
    payload = _payload(payload_value)
    jobs = _jobs_for_source(payload)
    if not jobs:
        return _skipped("h_api", "no h_api retrieval jobs")

    token = str(api_token or os.getenv("H_API_TOKEN", "")).strip()
    timeout = _timeout(timeout_seconds or os.getenv("H_API_TIMEOUT_SECONDS", "30"))
    limit = _fetch_limit(fetch_limit or os.getenv("SOURCE_FETCH_LIMIT", "5000"))
    results = [_run_h_api_job(job, token, timeout, limit, opener) for job in jobs]
    errors = [error for result in results for error in result.get("errors", []) if isinstance(error, dict)]
    warnings = [warning for result in results for warning in result.get("warnings", []) if isinstance(warning, dict)]
    return {
        "source_type": "h_api",
        "status": "error" if errors else "ok",
        "skipped": False,
        "executed_jobs": [str(job.get("job_id") or job.get("dataset_key") or index) for index, job in enumerate(jobs, 1)],
        "source_results": results,
        "errors": errors,
        "warnings": warnings,
    }


# 함수 설명: `_run_h_api_job()`는 H·API·조회 작업 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
def _run_h_api_job(job: dict[str, Any], api_token: str, timeout: int, fetch_limit: int, opener: Any = None) -> dict[str, Any]:
    source_config = _source_config(job)
    params = _job_params(job)
    missing = _missing_required_params(params, _required_param_names(job, source_config))
    if missing:
        return _error_result(job, "missing_required_params", f"필수 파라미터가 없습니다: {', '.join(missing)}", params=params)

    url_template = _api_url(source_config)
    if not url_template:
        return _error_result(job, "missing_api_url", "H-API source_config에 api_url/url/endpoint가 없습니다.", params=params)

    url, url_missing = _render_template(str(url_template), params, url_encode=True)
    if url_missing:
        return _error_result(job, "missing_template_params", f"URL 템플릿 파라미터가 없습니다: {', '.join(url_missing)}", params=params)

    method = str(source_config.get("method") or "GET").upper().strip()
    query_params = _merge_query_params(source_config, params, include_job_params=method == "GET")
    query_params, query_missing = _render_any(query_params, params, url_encode=False)
    if query_missing:
        return _error_result(job, "missing_template_params", f"query 파라미터 템플릿 값이 없습니다: {', '.join(query_missing)}", params=params)

    request_body = _request_body(source_config, params, method)
    request_body, body_missing = _render_any(request_body, params, url_encode=False)
    if body_missing:
        return _error_result(job, "missing_template_params", f"body 템플릿 파라미터 값이 없습니다: {', '.join(body_missing)}", params=params)

    headers = _request_headers(source_config, api_token)
    headers, header_missing = _render_any(headers, params, url_encode=False)
    if header_missing:
        return _error_result(job, "missing_template_params", f"header 템플릿 파라미터 값이 없습니다: {', '.join(header_missing)}", params=params)

    request_url = _append_query(url, query_params if isinstance(query_params, dict) else {})
    encoded_body = None
    if request_body not in (None, "", {}, []):
        encoded_body = json.dumps(_json_ready(request_body), ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
    headers.setdefault("Accept", "application/json")

    try:
        request = urllib.request.Request(request_url, data=encoded_body, headers=headers, method=method)
        response = (opener or urllib.request.urlopen)(request, timeout=timeout)
        response_payload = _read_response_payload(response)
        rows = _extract_rows(response_payload, source_config.get("response_path"))
        rows = _json_ready(rows[:fetch_limit])
        return _standard_result(job, rows, params, method, request_url)
    except urllib.error.HTTPError as exc:
        detail = _safe_decode(exc.read()) if hasattr(exc, "read") else str(exc)
        return _error_result(job, "h_api_http_error", f"H-API HTTP 오류: {exc.code} {detail}", params=params)
    except Exception as exc:
        return _error_result(job, "h_api_retrieval_failed", f"H-API 조회 실패: {exc}", params=params)


# 함수 설명: `_jobs_for_source()`는 전체 조회 작업 중 지정한 source type에 해당하는 작업만 골라냅니다.
def _jobs_for_source(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    bundle_jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if bundle_jobs:
        return [deepcopy(job) for job in bundle_jobs if isinstance(job, dict)]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    return [deepcopy(job) for job in jobs if isinstance(job, dict) and _source_type(job.get("source_type")) in {"h_api", "hapi"}]


# 함수 설명: `_source_config()`는 조회 작업 또는 카탈로그에서 허용된 데이터 소스 설정만 dict로 꺼냅니다.
def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("api_url", "url", "endpoint_url", "endpoint", "path", "method", "headers", "params", "query_params", "body", "payload"):
        if job.get(key) not in (None, "", [], {}):
            config.setdefault(key, deepcopy(job[key]))
    return config


# 함수 설명: `_api_url()`는 URL에 접근할 URL을 설정과 식별자로부터 안전하게 구성합니다.
def _api_url(source_config: dict[str, Any]) -> str:
    direct_url = str(source_config.get("api_url") or source_config.get("url") or source_config.get("endpoint_url") or "").strip()
    if direct_url:
        return direct_url
    endpoint = str(source_config.get("endpoint") or source_config.get("path") or "").strip()
    base_url = str(source_config.get("base_url") or os.getenv("H_API_BASE_URL", "")).strip()
    if not endpoint:
        return ""
    if endpoint.lower().startswith(("http://", "https://")):
        return endpoint
    return base_url.rstrip("/") + "/" + endpoint.lstrip("/") if base_url else endpoint


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


# 함수 설명: `_merge_query_params()`는 여러 쿼리·파라미터 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다.
def _merge_query_params(source_config: dict[str, Any], params: dict[str, Any], include_job_params: bool) -> dict[str, Any]:
    query = {}
    for key in ("query_params", "params"):
        if isinstance(source_config.get(key), dict):
            query.update(deepcopy(source_config[key]))
    if include_job_params:
        for key, value in params.items():
            query.setdefault(key, deepcopy(value))
    return query


# 함수 설명: `_request_body()`는 BODY에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _request_body(source_config: dict[str, Any], params: dict[str, Any], method: str) -> Any:
    for key in ("body", "payload", "json_body"):
        if source_config.get(key) not in (None, "", [], {}):
            return deepcopy(source_config[key])
    if method in {"POST", "PUT", "PATCH"}:
        return deepcopy(params)
    return None


# 함수 설명: `_request_headers()`는 headers에서 현재 단계가 사용할 필드만 추출해 표준 구조로 정리합니다.
def _request_headers(source_config: dict[str, Any], api_token: str) -> dict[str, str]:
    headers = {}
    if isinstance(source_config.get("headers"), dict):
        headers.update({str(key): str(value) for key, value in source_config["headers"].items() if value not in (None, "")})
    if api_token and not any(key.lower() == "authorization" for key in headers):
        token_header = str(source_config.get("token_header_name") or "Authorization")
        token_prefix = str(source_config.get("token_prefix") if source_config.get("token_prefix") is not None else "Bearer").strip()
        headers[token_header] = f"{token_prefix} {api_token}".strip()
    return headers


# 함수 설명: `_append_query()`는 여러 쿼리 값을 순서와 중복 정책을 지키며 하나의 결과로 합칩니다.
def _append_query(url: str, params: dict[str, Any]) -> str:
    clean_params = {key: value for key, value in params.items() if value not in (None, "", [], {})}
    if not clean_params:
        return url
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return url + separator + urllib.parse.urlencode(clean_params, doseq=True)


# 함수 설명: `_read_response_payload()`는 입력 또는 외부 저장소에서 응답·페이로드을 읽고 호출자가 사용할 형태로 반환합니다.
def _read_response_payload(response: Any) -> Any:
    raw = response.read() if hasattr(response, "read") else response
    text = _safe_decode(raw)
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"value": text}


# 함수 설명: `_extract_rows()`는 복합 입력이나 응답에서 행 목록을 찾아 검증 가능한 기본 Python 값으로 변환합니다.
def _extract_rows(value: Any, response_path: Any = "") -> list[dict[str, Any]]:
    selected = _select_path(value, response_path)
    rows = _rows_from_value(selected)
    return [_row_dict(row) for row in rows]


# 함수 설명: `_select_path()`는 조건과 우선순위에 맞는 PATH만 골라 원래 순서를 유지해 반환합니다.
def _select_path(value: Any, response_path: Any = "") -> Any:
    current = value
    for part in [item for item in str(response_path or "").split(".") if item]:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


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
        nested_list = _first_nested_list(value)
        if nested_list:
            return nested_list
        return [value]
    if value in (None, ""):
        return []
    return [{"value": value}]


# 함수 설명: `_first_nested_list()`는 알려진 응답 key가 없을 때 중첩 dict에서 첫 번째 행 목록 후보를 찾습니다.
def _first_nested_list(value: Any) -> list[Any]:
    if isinstance(value, dict):
        for item in value.values():
            found = _first_nested_list(item)
            if found:
                return found
    if isinstance(value, list):
        return value
    return []


# 함수 설명: `_row_dict()`는 객체·매핑·튜플 형태의 한 행을 컬럼명이 있는 dict 행으로 변환합니다.
def _row_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return {"value": _json_ready(value)}


# 함수 설명: `_standard_result()`는 정상 조회 결과를 dataset/source alias와 rows가 포함된 공통 결과 구조로 만듭니다.
def _standard_result(job: dict[str, Any], rows: list[dict[str, Any]], params: dict[str, Any], method: str, url: str) -> dict[str, Any]:
    return {
        "source_alias": job.get("source_alias") or job.get("dataset_key"),
        "dataset_key": job.get("dataset_key"),
        "source_type": "h_api",
        "status": "ok",
        "row_count": len(rows),
        "columns": _rows_columns(rows),
        "preview_rows": rows[:PREVIEW_LIMIT],
        "rows": rows,
        "applied_params": deepcopy(params),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "source_execution": {
            "used_dummy_data": False,
            "adapter": "h_api",
            "method": method,
            "api_url": url,
            "source_configured": True,
            "filters_applied_in_retriever": False,
        },
        "warnings": [],
        "errors": [],
    }


# 함수 설명: `_error_result()`는 예외 정보를 공통 errors 배열과 status가 포함된 실패 결과 구조로 만듭니다.
def _error_result(job: dict[str, Any], error_type: str, message: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    error = {"type": error_type, "message": message, "dataset_key": job.get("dataset_key", "")}
    return {
        "source_alias": job.get("source_alias") or job.get("dataset_key"),
        "dataset_key": job.get("dataset_key"),
        "source_type": "h_api",
        "status": "error",
        "row_count": 0,
        "columns": [],
        "preview_rows": [],
        "rows": [],
        "applied_params": deepcopy(params if params is not None else _job_params(job)),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "source_execution": {"used_dummy_data": False, "adapter": "h_api", "source_configured": False},
        "warnings": [],
        "errors": [error],
    }


# 함수 설명: `_render_any()`는 ANY을 Markdown 또는 사용자 화면에서 안전하게 읽을 수 있는 표현으로 변환합니다.
def _render_any(value: Any, params: dict[str, Any], url_encode: bool) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        return _render_template(value, params, url_encode=url_encode)
    if isinstance(value, dict):
        rendered = {}
        missing = []
        for key, item in value.items():
            next_value, next_missing = _render_any(item, params, url_encode=url_encode)
            rendered[str(key)] = next_value
            missing.extend(next_missing)
        return rendered, missing
    if isinstance(value, list):
        rendered_items = []
        missing = []
        for item in value:
            next_value, next_missing = _render_any(item, params, url_encode=url_encode)
            rendered_items.append(next_value)
            missing.extend(next_missing)
        return rendered_items, missing
    return value, []


# 함수 설명: `_render_template()`는 검증된 파라미터를 SQL·URL·본문 템플릿에 치환해 실제 요청 문자열을 만듭니다.
def _render_template(template: str, params: dict[str, Any], url_encode: bool) -> tuple[str, list[str]]:
    missing: list[str] = []

    # 함수 설명: `replace()`는 HTTP URL·본문 템플릿 placeholder를 요청 파라미터 값으로 치환하고 누락 key를 기록합니다.
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = _dict_get_ci(params, key)
        if value in (None, "", []):
            missing.append(key)
            return match.group(0)
        text = str(value)
        return urllib.parse.quote(text, safe="") if url_encode else text

    return re.sub(r"\{([^{}]+)\}", replace, str(template or "")), missing


# 함수 설명: `_missing_required_params()`는 필수 파라미터 중 실제 작업 값에 없는 항목을 찾아 오류 목록으로 반환합니다.
def _missing_required_params(params: dict[str, Any], required_params: Any) -> list[str]:
    missing = []
    for item in _as_list(required_params):
        key = str(item or "").strip()
        if key and _dict_get_ci(params, key) in (None, "", []):
            missing.append(key)
    return missing


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


# 함수 설명: `_timeout()`는 HTTP timeout 입력을 허용 범위의 초 단위 숫자로 보정합니다.
def _timeout(value: Any) -> int:
    try:
        return max(1, int(value or 30))
    except Exception:
        return 30


# 함수 설명: `_fetch_limit()`는 설정된 조회 제한을 안전한 정수 범위로 보정합니다.
def _fetch_limit(value: Any) -> int:
    try:
        return max(1, int(value or 5000))
    except Exception:
        return 5000


# 함수 설명: `_source_type()`는 조회 작업의 source type을 표준 소문자 식별자로 정규화합니다.
def _source_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


# 함수 설명: `_normalize_key()`는 key의 대소문자·공백·구분자 차이를 제거해 비교 가능한 표준 식별자로 바꿉니다.
def _normalize_key(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "").strip().lower())


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


# 함수 설명: `_safe_decode()`는 bytes 응답을 UTF-8로 해석하고 실패해도 예외 대신 읽을 수 있는 문자열을 반환합니다.
def _safe_decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


# 함수 설명: `_payload()`는 Langflow Data/Message 또는 일반 dict 입력에서 안전한 dict 페이로드 복사본을 꺼냅니다.
def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


# 함수 설명: `_skipped()`는 설정이나 대상 작업이 없어 실행하지 않은 이유를 표준 skipped 결과로 남깁니다.
def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


# Langflow 컴포넌트 클래스: inputs/outputs가 캔버스 포트와 JSON edge 계약을 정의합니다.
# 실제 업무 규칙은 위의 주요 함수에 두어 UI 실행과 단위 테스트가 같은 로직을 사용합니다.
class HApiRetriever(Component):
    display_name = "10 H-API 데이터 조회기"
    description = "table catalog의 H-API source_config를 사용해 실제 API 조회를 실행합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="api_token", display_name="H-API 토큰", required=False, value="", advanced=True),
        MessageTextInput(name="timeout_seconds", display_name="요청 제한 시간(초)", required=False, value="30", advanced=True),
        MessageTextInput(name="fetch_limit", display_name="조회 제한 건수", required=False, value="5000", advanced=True),
    ]
    outputs = [Output(name="retrieval_payload", display_name="조회 페이로드", method="build_payload")]

    # Langflow 출력 함수: '조회 페이로드 (retrieval_payload)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
    def build_payload(self) -> Data:
        return Data(
            data=h_api_retrieve(
                getattr(self, "payload", None),
                getattr(self, "api_token", ""),
                getattr(self, "timeout_seconds", ""),
                getattr(self, "fetch_limit", ""),
            )
        )
