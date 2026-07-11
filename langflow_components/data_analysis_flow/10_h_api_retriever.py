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


def _jobs_for_source(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    bundle_jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if bundle_jobs:
        return [deepcopy(job) for job in bundle_jobs if isinstance(job, dict)]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    return [deepcopy(job) for job in jobs if isinstance(job, dict) and _source_type(job.get("source_type")) in {"h_api", "hapi"}]


def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("api_url", "url", "endpoint_url", "endpoint", "path", "method", "headers", "params", "query_params", "body", "payload"):
        if job.get(key) not in (None, "", [], {}):
            config.setdefault(key, deepcopy(job[key]))
    return config


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


def _job_params(job: dict[str, Any]) -> dict[str, Any]:
    if isinstance(job.get("params"), dict):
        return deepcopy(job["params"])
    if isinstance(job.get("required_params"), dict):
        return deepcopy(job["required_params"])
    return {}


def _required_param_names(job: dict[str, Any], source_config: dict[str, Any]) -> list[Any]:
    if isinstance(source_config.get("required_params"), (list, tuple, set)):
        return _as_list(source_config.get("required_params"))
    if isinstance(job.get("required_param_names"), (list, tuple, set)):
        return _as_list(job.get("required_param_names"))
    if not isinstance(job.get("required_params"), dict):
        return _as_list(job.get("required_params"))
    return []


def _merge_query_params(source_config: dict[str, Any], params: dict[str, Any], include_job_params: bool) -> dict[str, Any]:
    query = {}
    for key in ("query_params", "params"):
        if isinstance(source_config.get(key), dict):
            query.update(deepcopy(source_config[key]))
    if include_job_params:
        for key, value in params.items():
            query.setdefault(key, deepcopy(value))
    return query


def _request_body(source_config: dict[str, Any], params: dict[str, Any], method: str) -> Any:
    for key in ("body", "payload", "json_body"):
        if source_config.get(key) not in (None, "", [], {}):
            return deepcopy(source_config[key])
    if method in {"POST", "PUT", "PATCH"}:
        return deepcopy(params)
    return None


def _request_headers(source_config: dict[str, Any], api_token: str) -> dict[str, str]:
    headers = {}
    if isinstance(source_config.get("headers"), dict):
        headers.update({str(key): str(value) for key, value in source_config["headers"].items() if value not in (None, "")})
    if api_token and not any(key.lower() == "authorization" for key in headers):
        token_header = str(source_config.get("token_header_name") or "Authorization")
        token_prefix = str(source_config.get("token_prefix") if source_config.get("token_prefix") is not None else "Bearer").strip()
        headers[token_header] = f"{token_prefix} {api_token}".strip()
    return headers


def _append_query(url: str, params: dict[str, Any]) -> str:
    clean_params = {key: value for key, value in params.items() if value not in (None, "", [], {})}
    if not clean_params:
        return url
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return url + separator + urllib.parse.urlencode(clean_params, doseq=True)


def _read_response_payload(response: Any) -> Any:
    raw = response.read() if hasattr(response, "read") else response
    text = _safe_decode(raw)
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {"value": text}


def _extract_rows(value: Any, response_path: Any = "") -> list[dict[str, Any]]:
    selected = _select_path(value, response_path)
    rows = _rows_from_value(selected)
    return [_row_dict(row) for row in rows]


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


def _first_nested_list(value: Any) -> list[Any]:
    if isinstance(value, dict):
        for item in value.values():
            found = _first_nested_list(item)
            if found:
                return found
    if isinstance(value, list):
        return value
    return []


def _row_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return {"value": _json_ready(value)}


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


def _render_template(template: str, params: dict[str, Any], url_encode: bool) -> tuple[str, list[str]]:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = _dict_get_ci(params, key)
        if value in (None, "", []):
            missing.append(key)
            return match.group(0)
        text = str(value)
        return urllib.parse.quote(text, safe="") if url_encode else text

    return re.sub(r"\{([^{}]+)\}", replace, str(template or "")), missing


def _missing_required_params(params: dict[str, Any], required_params: Any) -> list[str]:
    missing = []
    for item in _as_list(required_params):
        key = str(item or "").strip()
        if key and _dict_get_ci(params, key) in (None, "", []):
            missing.append(key)
    return missing


def _rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


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


def _timeout(value: Any) -> int:
    try:
        return max(1, int(value or 30))
    except Exception:
        return 30


def _fetch_limit(value: Any) -> int:
    try:
        return max(1, int(value or 5000))
    except Exception:
        return 5000


def _source_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_key(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "").strip().lower())


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


def _safe_decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


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

    def build_payload(self) -> Data:
        return Data(
            data=h_api_retrieve(
                getattr(self, "payload", None),
                getattr(self, "api_token", ""),
                getattr(self, "timeout_seconds", ""),
                getattr(self, "fetch_limit", ""),
            )
        )
