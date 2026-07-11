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


class Goodocs:
    def __init__(self, auth: dict[str, Any]):
        self.auth = auth

    def read_all(self) -> Any:
        raise RuntimeError("Goodocs class implementation is not configured. Paste the real Goodocs class into this component.")


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
    results = [
        _run_goodocs_job(job, resolved_user_id, resolved_token_source, resolved_token_key, limit)
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


def _run_goodocs_job(
    job: dict[str, Any],
    user_id: str,
    token_source: str,
    token_key: str,
    fetch_limit: int,
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


def _goodocs_class() -> Any:
    override = getattr(GoodocsRetriever, "goodocs_class", None) if "GoodocsRetriever" in globals() else None
    if override is not None:
        return override
    return Goodocs


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


def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("doc_id", "document_id", "sheet_name", "sheet", "range", "table_name", "columns", "required_columns", "rows", "data", "items"):
        if job.get(key) not in (None, "", [], {}):
            config.setdefault(key, deepcopy(job[key]))
    if config.get("document_id") and not config.get("doc_id"):
        config["doc_id"] = config["document_id"]
    return config


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
        "data": rows,
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
        "data": [],
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


def _inline_rows(source_config: dict[str, Any]) -> list[Any]:
    for key in ("rows", "data", "items"):
        if isinstance(source_config.get(key), list):
            return deepcopy(source_config[key])
    return []


def _has_inline_rows(source_config: dict[str, Any]) -> bool:
    return bool(_inline_rows(source_config))


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


def _drop_system_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if str(key) not in GOODOCS_SYSTEM_COLUMNS} for row in rows]


def _row_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return {"value": _json_ready(value)}


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


def _missing_required_params(params: dict[str, Any], required_params: Any) -> list[str]:
    missing = []
    for item in _as_list(required_params):
        key = str(item or "").strip()
        if key and _dict_get_ci(params, key) in (None, "", []):
            missing.append(key)
    return missing


def _fetch_limit(value: Any) -> int:
    try:
        return max(1, int(value or 5000))
    except Exception:
        return 5000


def _source_type(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "").replace(" ", "")


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


def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


class GoodocsRetriever(Component):
    goodocs_class = None

    display_name = "12 Goodocs 조회기"
    description = "Goodocs 문서 기반 source job을 실행하고, 인증 또는 문서 설정이 없으면 dummy fallback으로 대체합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="user_id", display_name="Goodocs 사용자 ID", required=False, value="", advanced=True),
        MessageTextInput(name="token_source", display_name="Goodocs 토큰 소스", required=False, value="", advanced=True),
        MessageTextInput(name="token_key", display_name="Goodocs 토큰 키", required=False, value="", advanced=True),
        MessageTextInput(name="fetch_limit", display_name="조회 제한 건수", required=False, value="5000", advanced=True),
    ]
    outputs = [Output(name="retrieval_payload", display_name="조회 페이로드", method="build_payload")]

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
