from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


PREVIEW_LIMIT = 5


def datalake_retrieve(
    payload_value: Any,
    module_name: Any = "",
    class_name: Any = "",
    user_id: Any = "",
    token: Any = "",
    s3_access_key: Any = "",
    s3_secret_key: Any = "",
    fetch_limit: Any = "",
    client_cls: Any = None,
) -> dict[str, Any]:
    payload = _payload(payload_value)
    jobs = _jobs_for_source(payload)
    if not jobs:
        return _skipped("datalake", "no datalake retrieval jobs")

    limit = _fetch_limit(fetch_limit or os.getenv("SOURCE_FETCH_LIMIT", "5000"))
    client_factory = DatalakeClientFactory(
        module_name=str(module_name or os.getenv("DATALAKE_MODULE_NAME", "lakes")).strip(),
        class_name=str(class_name or os.getenv("DATALAKE_CLASS_NAME", "LakeHouse")).strip(),
        user_id=str(user_id or os.getenv("LAKEHOUSE_USER_ID", "")).strip(),
        token=str(token or os.getenv("LAKEHOUSE_TOKEN", "")).strip(),
        s3_access_key=str(s3_access_key or os.getenv("LAKEHOUSE_S3_ACCESS_KEY", "")).strip(),
        s3_secret_key=str(s3_secret_key or os.getenv("LAKEHOUSE_S3_SECRET_KEY", "")).strip(),
        client_cls=client_cls,
    )
    results = [_run_datalake_job(job, client_factory, limit) for job in jobs]
    errors = [error for result in results for error in result.get("errors", []) if isinstance(error, dict)]
    warnings = [warning for result in results for warning in result.get("warnings", []) if isinstance(warning, dict)]
    return {
        "source_type": "datalake",
        "status": "error" if errors else "ok",
        "skipped": False,
        "executed_jobs": [str(job.get("job_id") or job.get("dataset_key") or index) for index, job in enumerate(jobs, 1)],
        "source_results": results,
        "errors": errors,
        "warnings": warnings,
    }


def _run_datalake_job(job: dict[str, Any], client_factory: "DatalakeClientFactory", fetch_limit: int) -> dict[str, Any]:
    source_config = _source_config(job)
    params = _job_params(job)
    missing = _missing_required_params(params, _required_param_names(job, source_config))
    if missing:
        return _error_result(job, "missing_required_params", f"필수 파라미터가 없습니다: {', '.join(missing)}", params=params)

    query_template = str(
        source_config.get("query_template")
        or source_config.get("sql_template")
        or source_config.get("datalake_sql")
        or source_config.get("sql")
        or source_config.get("query")
        or ""
    ).strip()
    if not query_template:
        return _error_result(job, "missing_query_template", "Datalake source_config에 query_template이 없습니다.", params=params)

    sql, missing_template_params = _render_template(query_template, params)
    if missing_template_params:
        return _error_result(job, "missing_template_params", f"SQL 템플릿 파라미터가 없습니다: {', '.join(missing_template_params)}", params=params)

    try:
        rows = client_factory.execute_sql(sql, source_config, fetch_limit)
        rows = _json_ready(rows)
        if not isinstance(rows, list):
            rows = []
        return _standard_result(job, rows, params, sql, client_factory.module_name, client_factory.class_name)
    except Exception as exc:
        return _error_result(job, "datalake_retrieval_failed", f"Datalake 조회 실패: {exc}", params=params)


class DatalakeClientFactory:
    def __init__(
        self,
        module_name: str,
        class_name: str,
        user_id: str,
        token: str,
        s3_access_key: str,
        s3_secret_key: str,
        client_cls: Any = None,
    ):
        self.module_name = module_name or "lakes"
        self.class_name = class_name or "LakeHouse"
        self.user_id = user_id
        self.token = token
        self.s3_access_key = s3_access_key
        self.s3_secret_key = s3_secret_key
        self.client_cls = client_cls

    def execute_sql(self, sql: str, source_config: dict[str, Any], fetch_limit: int) -> list[dict[str, Any]]:
        self._prepare_environment()
        client = self._create_client()
        cluster_type = str(source_config.get("cluster_type") or os.getenv("LAKEHOUSE_CLUSTER_TYPE", "starrocks")).strip()
        if hasattr(client, "ensure_running"):
            self._call_with_fallback(client.ensure_running, {"cluster_type": cluster_type}, [cluster_type])
        raw_result = self._run_query_method(client, sql)
        rows = self._read_result(client, raw_result)
        return [_row_dict(row) for row in _rows_from_value(rows)[:fetch_limit]]

    def _prepare_environment(self) -> None:
        for key, value in {
            "LAKEHOUSE_USER_ID": self.user_id,
            "LAKEHOUSE_TOKEN": self.token,
            "LAKEHOUSE_S3_ACCESS_KEY": self.s3_access_key,
            "LAKEHOUSE_S3_SECRET_KEY": self.s3_secret_key,
        }.items():
            if value:
                os.environ[key] = value

    def _create_client(self) -> Any:
        cls = self.client_cls or getattr(import_module(self.module_name), self.class_name)
        attempts = (
            ((), {"real_user_id": self.user_id}) if self.user_id else ((), {}),
            ((), {"user_id": self.user_id}) if self.user_id else ((), {}),
            ((), {}),
        )
        last_error = None
        for args, kwargs in attempts:
            try:
                return cls(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return cls()

    def _run_query_method(self, client: Any, sql: str) -> Any:
        if hasattr(client, "auto_run_sync_paragraph"):
            return self._call_with_fallback(client.auto_run_sync_paragraph, {"code": sql}, [sql])
        for method_name in ("run_sql", "execute_sql", "query", "execute", "read_sql"):
            if hasattr(client, method_name):
                return self._call_with_fallback(getattr(client, method_name), {"sql": sql}, [sql])
        raise AttributeError("Datalake client에 실행 메서드(auto_run_sync_paragraph/run_sql/query/execute)가 없습니다.")

    def _read_result(self, client: Any, raw_result: Any) -> Any:
        if raw_result not in (None, ""):
            return raw_result
        if hasattr(client, "get_rst"):
            return client.get_rst()
        if hasattr(client, "get_result"):
            return client.get_result()
        return raw_result

    @staticmethod
    def _call_with_fallback(method: Any, kwargs: dict[str, Any], args: list[Any]) -> Any:
        try:
            return method(**kwargs)
        except TypeError:
            return method(*args)


def _jobs_for_source(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    bundle_jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if bundle_jobs:
        return [deepcopy(job) for job in bundle_jobs if isinstance(job, dict)]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    return [deepcopy(job) for job in jobs if isinstance(job, dict) and _source_type(job.get("source_type")) in {"datalake", "data_lake", "lakehouse"}]


def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("query_template", "sql_template", "datalake_sql", "sql", "query", "cluster_type"):
        if job.get(key) not in (None, "", [], {}):
            config.setdefault(key, deepcopy(job[key]))
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
    sql: str,
    module_name: str,
    class_name: str,
) -> dict[str, Any]:
    return {
        "source_alias": job.get("source_alias") or job.get("dataset_key"),
        "dataset_key": job.get("dataset_key"),
        "source_type": "datalake",
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
            "adapter": "datalake",
            "module_name": module_name,
            "class_name": class_name,
            "executed_query": sql,
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
        "source_type": "datalake",
        "status": "error",
        "row_count": 0,
        "columns": [],
        "preview_rows": [],
        "rows": [],
        "applied_params": deepcopy(params if params is not None else _job_params(job)),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "source_execution": {"used_dummy_data": False, "adapter": "datalake", "source_configured": False},
        "warnings": [],
        "errors": [error],
    }


def _render_template(template: str, params: dict[str, Any]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = _dict_get_ci(params, key)
        if value in (None, "", []):
            missing.append(key)
            return match.group(0)
        return _sql_literal(value)

    return re.sub(r"\{([^{}]+)\}", replace, str(template or "")), missing


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_sql_literal(item) for item in value)
    if isinstance(value, (datetime, date)):
        return f"'{value.strftime('%Y%m%d')}'"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _missing_required_params(params: dict[str, Any], required_params: Any) -> list[str]:
    missing = []
    for item in _as_list(required_params):
        key = str(item or "").strip()
        if key and _dict_get_ci(params, key) in (None, "", []):
            missing.append(key)
    return missing


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
    if value in (None, ""):
        return []
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    return [{"value": value}]


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


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return deepcopy(value)
    data = getattr(value, "data", None)
    return deepcopy(data) if isinstance(data, dict) else {}


def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


class DatalakeRetriever(Component):
    display_name = "11 데이터레이크 조회기"
    description = "table catalog의 Datalake source_config와 LakeHouse 계열 client를 사용해 실제 SQL 조회를 실행합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="module_name", display_name="Datalake 모듈명", required=False, value="lakes", advanced=True),
        MessageTextInput(name="class_name", display_name="Datalake 클래스명", required=False, value="LakeHouse", advanced=True),
        MessageTextInput(name="user_id", display_name="LakeHouse 사용자 ID", required=False, value="", advanced=True),
        MessageTextInput(name="token", display_name="LakeHouse 토큰", required=False, value="", advanced=True),
        MessageTextInput(name="s3_access_key", display_name="S3 접근 키", required=False, value="", advanced=True),
        MessageTextInput(name="s3_secret_key", display_name="S3 보안 키", required=False, value="", advanced=True),
        MessageTextInput(name="fetch_limit", display_name="조회 제한 건수", required=False, value="5000", advanced=True),
    ]
    outputs = [Output(name="retrieval_payload", display_name="조회 페이로드", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(
            data=datalake_retrieve(
                getattr(self, "payload", None),
                getattr(self, "module_name", ""),
                getattr(self, "class_name", ""),
                getattr(self, "user_id", ""),
                getattr(self, "token", ""),
                getattr(self, "s3_access_key", ""),
                getattr(self, "s3_secret_key", ""),
                getattr(self, "fetch_limit", ""),
            )
        )
