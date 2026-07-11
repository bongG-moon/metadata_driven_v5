from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, MessageTextInput, Output
from lfx.schema.data import Data


SINGLE_ORACLE_CONFIG_KEY = "__single_oracle_config__"
PREVIEW_LIMIT = 5


def retrieve_oracle_data(payload_value: Any, oracle_config: Any = "", fetch_limit: Any = "") -> dict[str, Any]:
    payload = _payload(payload_value)
    jobs = _jobs_for_source(payload)
    if not jobs:
        return _skipped("oracle", "no oracle retrieval jobs")

    config_value = oracle_config or os.getenv("ORACLE_CONFIG_JSON", "")
    limit = _fetch_limit(fetch_limit or os.getenv("SOURCE_FETCH_LIMIT", "5000"))
    config, config_errors = _oracle_config_from_value(config_value)
    if config_errors:
        results = [_error_result(job, "invalid_oracle_config", f"Oracle 설정/TNS 파싱 실패: {'; '.join(config_errors)}") for job in jobs]
    elif not _config_has_values(config):
        results = [_error_result(job, "missing_oracle_config", "Oracle 설정/TNS가 비어 있어 실제 조회를 실행할 수 없습니다.") for job in jobs]
    else:
        oracle_module = getattr(OracleQueryRetriever, "oracledb", None) if "OracleQueryRetriever" in globals() else None
        results = [_run_oracle_job(job, config, limit, oracle_module) for job in jobs]

    errors = [error for result in results for error in result.get("errors", []) if isinstance(error, dict)]
    warnings = [warning for result in results for warning in result.get("warnings", []) if isinstance(warning, dict)]
    return {
        "source_type": "oracle",
        "status": "error" if errors else "ok",
        "skipped": False,
        "executed_jobs": [str(job.get("job_id") or job.get("dataset_key") or index) for index, job in enumerate(jobs, 1)],
        "source_results": results,
        "errors": errors,
        "warnings": warnings,
    }


def ensure_package(package_name: str, import_name: str | None = None) -> None:
    module_name = import_name or package_name
    if importlib.util.find_spec(module_name) is None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--trusted-host", "nexus.skhynix.com", package_name])


def _run_oracle_job(job: dict[str, Any], oracle_config: dict[str, Any], fetch_limit: int, oracle_module: Any | None = None) -> dict[str, Any]:
    source_config = _source_config(job)
    params = _job_params(job)
    missing = _missing_required_params(params, _required_param_names(job, source_config))
    if missing:
        return _error_result(job, "missing_required_params", f"필수 파라미터가 없습니다: {', '.join(missing)}", params=params)

    query_template = str(
        source_config.get("query_template")
        or source_config.get("sql_template")
        or source_config.get("oracle_sql")
        or source_config.get("sql")
        or source_config.get("query")
        or ""
    ).strip()
    if not query_template:
        return _error_result(job, "missing_query_template", "Oracle source_config에 query_template이 없습니다.", params=params)

    sql, missing_template_params = _render_template(query_template, params)
    if missing_template_params:
        return _error_result(job, "missing_template_params", f"SQL 템플릿 파라미터가 없습니다: {', '.join(missing_template_params)}", params=params)

    db_key = str(source_config.get("db_key") or job.get("db_key") or "").strip()
    if not db_key:
        return _error_result(job, "missing_db_key", "Oracle source_config에 db_key가 없습니다.", params=params)

    try:
        connector = OracleConnector(oracle_config, oracle_module)
        rows = connector.execute_query(db_key, sql, fetch_limit=fetch_limit)
        columns = getattr(connector, "last_columns", [])
        rows = _json_ready(rows)
        if not isinstance(rows, list):
            rows = []
        return _standard_result(job, rows, params, db_key, sql, columns=columns)
    except Exception as exc:
        return _error_result(job, "oracle_retrieval_failed", f"Oracle 조회 실패: {exc}", params=params)


class OracleConnector:
    def __init__(self, config: dict[str, Any], oracle_module: Any | None = None):
        self.config = config
        self.oracle_module = oracle_module
        self.last_columns: list[str] = []

    def _oracledb(self) -> Any:
        if self.oracle_module is not None:
            return self.oracle_module
        ensure_package("oracledb")
        self.oracle_module = import_module("oracledb")
        return self.oracle_module

    def get_connection(self, target_db: str) -> Any:
        resolved = next((key for key in self.config if _normalize_key(key) == _normalize_key(target_db)), "")
        if not resolved and len(self.config) == 1:
            resolved = next(iter(self.config))
        if not resolved:
            raise ValueError(f"알 수 없는 Oracle DB 설정입니다: {target_db}")
        db_conf = self.config[resolved] if isinstance(self.config.get(resolved), dict) else {}
        user = str(db_conf.get("user") or db_conf.get("username") or db_conf.get("id") or "").strip()
        password = str(db_conf.get("password") or db_conf.get("pw") or "").strip()
        dsn = str(db_conf.get("dsn") or db_conf.get("tns") or db_conf.get("tns_name") or db_conf.get("tns_alias") or "").strip()
        if not dsn:
            raise ValueError(f"{target_db} Oracle 설정에 dsn/tns가 없습니다.")
        if user and password:
            return self._oracledb().connect(user=user, password=password, dsn=dsn)
        return self._oracledb().connect(dsn=dsn)

    def execute_query(self, target_db: str, sql: str, fetch_limit: int | None = None) -> list[dict[str, Any]]:
        conn = None
        cursor = None
        try:
            conn = self.get_connection(target_db)
            cursor = conn.cursor()
            cursor.execute(sql)
            columns = [column[0] for column in cursor.description]
            self.last_columns = [str(column) for column in columns]
            rows = cursor.fetchmany(fetch_limit) if fetch_limit else cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()


def _jobs_for_source(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    bundle_jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if bundle_jobs:
        return [deepcopy(job) for job in bundle_jobs if isinstance(job, dict)]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    return [deepcopy(job) for job in jobs if isinstance(job, dict) and _source_type(job.get("source_type")) in {"oracle", "oracle_db", "oracledb"}]


def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("db_key", "query_template", "sql_template", "oracle_sql", "sql", "query"):
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


def _standard_result(job: dict[str, Any], rows: list[dict[str, Any]], params: dict[str, Any], db_key: str, sql: str, columns: list[str] | None = None) -> dict[str, Any]:
    result_columns = _rows_columns(rows) or _string_list(columns)
    return {
        "source_alias": job.get("source_alias") or job.get("dataset_key"),
        "dataset_key": job.get("dataset_key"),
        "source_type": "oracle",
        "status": "ok",
        "row_count": len(rows),
        "columns": result_columns,
        "preview_rows": rows[:PREVIEW_LIMIT],
        "rows": rows,
        "applied_params": deepcopy(params),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "source_execution": {
            "used_dummy_data": False,
            "adapter": "oracle",
            "db_key": db_key,
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
        "source_type": "oracle",
        "status": "error",
        "row_count": 0,
        "columns": [],
        "preview_rows": [],
        "rows": [],
        "applied_params": deepcopy(params if params is not None else _job_params(job)),
        "pandas_filters": deepcopy(job.get("filters", {})),
        "data_ref": "",
        "source_execution": {"used_dummy_data": False, "adapter": "oracle", "source_configured": False},
        "warnings": [],
        "errors": [error],
    }


def _skipped(source_type: str, reason: str) -> dict[str, Any]:
    return {"source_type": source_type, "status": "skipped", "skipped": True, "skip_reason": reason, "source_results": [], "errors": [], "warnings": []}


def _oracle_config_from_value(value: Any) -> tuple[dict[str, Any], list[str]]:
    if value in (None, "", {}, []):
        return {}, []
    parsed, errors = _parse_jsonish(value)
    if isinstance(parsed, dict) and isinstance(parsed.get("oracle_config"), dict):
        parsed = parsed["oracle_config"]
    if isinstance(parsed, dict) and parsed:
        return parsed, []
    text = str(value or "").strip()
    named_tns = _parse_named_tns_blocks(text)
    if named_tns:
        return named_tns, []
    if _looks_like_tns(text):
        return {SINGLE_ORACLE_CONFIG_KEY: {"tns": text}}, []
    if errors and not parsed:
        return {}, errors
    return {}, ["Oracle 설정은 JSON 객체 또는 TNS block이어야 합니다."]


def _parse_jsonish(value: Any) -> tuple[Any, list[str]]:
    if isinstance(value, (dict, list)):
        return deepcopy(value), []
    text = str(value or "").strip()
    if not text:
        return {}, []
    errors: list[str] = []
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(text), []
        except Exception as exc:
            errors.append(str(exc))
    normalized = _normalize_triple_quoted_json(text)
    if normalized != text:
        for parser in (json.loads, ast.literal_eval):
            try:
                return parser(normalized), []
            except Exception as exc:
                errors.append(str(exc))
    return {}, errors


def _normalize_triple_quoted_json(text: str) -> str:
    return re.sub(r'("""|\'\'\')(.*?)(\1)', lambda match: json.dumps(match.group(2)), str(text or ""), flags=re.DOTALL)


def _looks_like_tns(text: str) -> bool:
    upper_text = str(text or "").upper()
    return "(DESCRIPTION=" in upper_text or ("(ADDRESS=" in upper_text and "(CONNECT_DATA=" in upper_text)


def _parse_named_tns_blocks(text: str) -> dict[str, Any]:
    configs: dict[str, Any] = {}
    current_key = ""
    current_lines: list[str] = []

    def save_current() -> None:
        nonlocal current_key, current_lines
        tns = "\n".join(current_lines).strip()
        if current_key and _looks_like_tns(tns):
            configs[current_key] = {"tns": tns}
        current_key = ""
        current_lines = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        key_match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
        if key_match and not line.startswith("("):
            save_current()
            current_key = key_match.group(1).strip()
            possible_tns = key_match.group(2).strip()
            if possible_tns:
                current_lines.append(possible_tns)
            continue
        if current_key:
            current_lines.append(raw_line)
    save_current()
    return configs


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


def _rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []


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


def _config_has_values(config: Any) -> bool:
    return isinstance(config, dict) and any(value not in (None, "", [], {}) for value in config.values())


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


class OracleQueryRetriever(Component):
    oracledb = None

    display_name = "09 Oracle 쿼리 조회기"
    description = "table catalog의 Oracle source_config와 Oracle 설정/TNS를 사용해 실제 SQL 조회를 실행합니다."
    inputs = [
        DataInput(name="payload", display_name="페이로드", required=True),
        MessageTextInput(name="oracle_config", display_name="Oracle 설정/TNS", required=False, value=""),
        MessageTextInput(name="fetch_limit", display_name="조회 제한 건수", required=False, value="5000", advanced=True),
    ]
    outputs = [Output(name="retrieval_payload", display_name="조회 페이로드", method="build_payload")]

    def build_payload(self) -> Data:
        return Data(data=retrieve_oracle_data(getattr(self, "payload", None), getattr(self, "oracle_config", ""), getattr(self, "fetch_limit", "")))
