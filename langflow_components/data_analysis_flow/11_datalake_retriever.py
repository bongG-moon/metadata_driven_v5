# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 11 데이터레이크 조회기
# 역할: table catalog의 Datalake source_config와 LakeHouse 계열 client를 사용해 실제 SQL 조회를 실행합니다.
# 주요 입력: 페이로드 (payload) · 필수, Datalake 모듈명 (module_name), Datalake 클래스명 (class_name), LakeHouse 사용자 ID (user_id),
#        LakeHouse 토큰 (token), S3 접근 키 (s3_access_key), S3 보안 키 (s3_secret_key), 조회 제한 건수 (fetch_limit)
# 주요 출력: 조회 페이로드 (retrieval_payload)
# 처리 흐름: 사내 Datalake 클라이언트를 동적으로 준비하고 SQL 결과의 다양한 반환 형식을 표준 rows로 변환합니다.
# 유지보수 포인트: 실행 오류를 다른 source의 성공처럼 위장하는 과도한 fallback은 만들지 말고 공통 errors 계약으로 전달합니다.
# =============================================================================

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


# 주요 함수: Datalake SQL 작업을 실행하고 결과 객체를 표준 행 목록으로 바꿉니다.
# Langflow 클래스와 단위 테스트가 같은 업무 규칙을 쓰도록 일반 Python 값 중심으로 처리합니다.
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


# 함수 설명: `_run_datalake_job()`는 datalake·조회 작업 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
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


# 내부 연동 도우미 클래스: 외부 라이브러리나 클라이언트 차이를 이 파일의 표준 호출 형태로 감쌉니다.
class DatalakeClientFactory:
    # 함수 설명: `__init__()`는 외부 클라이언트나 실행 설정을 인스턴스에 보관해 뒤의 메서드가 같은 연결 문맥을 사용하게 합니다.
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

    # 주요 메서드: Datalake 클라이언트에 SQL을 전달하고 원시 결과를 반환합니다.
    # Langflow의 동적 빌드 또는 공개 실행 계약에서 호출될 수 있으므로 이름과 반환형을 유지합니다.
    def execute_sql(self, sql: str, source_config: dict[str, Any], fetch_limit: int) -> list[dict[str, Any]]:
        self._prepare_environment()
        client = self._create_client()
        cluster_type = str(source_config.get("cluster_type") or os.getenv("LAKEHOUSE_CLUSTER_TYPE", "starrocks")).strip()
        if hasattr(client, "ensure_running"):
            self._call_with_fallback(client.ensure_running, {"cluster_type": cluster_type}, [cluster_type])
        raw_result = self._run_query_method(client, sql)
        rows = self._read_result(client, raw_result)
        return [_row_dict(row) for row in _rows_from_value(rows)[:fetch_limit]]

    # 함수 설명: `_prepare_environment()`는 명시적으로 제공된 Datalake 인증·접속 설정만 실행 환경변수에 반영합니다.
    def _prepare_environment(self) -> None:
        for key, value in {
            "LAKEHOUSE_USER_ID": self.user_id,
            "LAKEHOUSE_TOKEN": self.token,
            "LAKEHOUSE_S3_ACCESS_KEY": self.s3_access_key,
            "LAKEHOUSE_S3_SECRET_KEY": self.s3_secret_key,
        }.items():
            if value:
                os.environ[key] = value

    # 함수 설명: `_create_client()`는 client 구성 요소를 모아 다음 단계가 사용할 표준 결과로 만듭니다.
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

    # 함수 설명: `_run_query_method()`는 쿼리·method 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    def _run_query_method(self, client: Any, sql: str) -> Any:
        if hasattr(client, "auto_run_sync_paragraph"):
            return self._call_with_fallback(client.auto_run_sync_paragraph, {"code": sql}, [sql])
        for method_name in ("run_sql", "execute_sql", "query", "execute", "read_sql"):
            if hasattr(client, method_name):
                return self._call_with_fallback(getattr(client, method_name), {"sql": sql}, [sql])
        raise AttributeError("Datalake client에 실행 메서드(auto_run_sync_paragraph/run_sql/query/execute)가 없습니다.")

    # 함수 설명: `_read_result()`는 입력 또는 외부 저장소에서 결과을 읽고 호출자가 사용할 형태로 반환합니다.
    def _read_result(self, client: Any, raw_result: Any) -> Any:
        if raw_result not in (None, ""):
            return raw_result
        if hasattr(client, "get_rst"):
            return client.get_rst()
        if hasattr(client, "get_result"):
            return client.get_result()
        return raw_result

    # 함수 설명: `_call_with_fallback()`는 WITH·fallback 실행 경계를 담당하고 성공 결과와 오류를 공통 계약으로 반환합니다.
    @staticmethod
    def _call_with_fallback(method: Any, kwargs: dict[str, Any], args: list[Any]) -> Any:
        try:
            return method(**kwargs)
        except TypeError:
            return method(*args)


# 함수 설명: `_jobs_for_source()`는 전체 조회 작업 중 지정한 source type에 해당하는 작업만 골라냅니다.
def _jobs_for_source(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bundle = payload.get("retrieval_job_bundle") if isinstance(payload.get("retrieval_job_bundle"), dict) else {}
    bundle_jobs = bundle.get("jobs") if isinstance(bundle.get("jobs"), list) else []
    if bundle_jobs:
        return [deepcopy(job) for job in bundle_jobs if isinstance(job, dict)]
    plan = payload.get("intent_plan") if isinstance(payload.get("intent_plan"), dict) else {}
    jobs = plan.get("retrieval_jobs") if isinstance(plan.get("retrieval_jobs"), list) else []
    return [deepcopy(job) for job in jobs if isinstance(job, dict) and _source_type(job.get("source_type")) in {"datalake", "data_lake", "lakehouse"}]


# 함수 설명: `_source_config()`는 조회 작업 또는 카탈로그에서 허용된 데이터 소스 설정만 dict로 꺼냅니다.
def _source_config(job: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(job.get("source_config")) if isinstance(job.get("source_config"), dict) else {}
    for key in ("query_template", "sql_template", "datalake_sql", "sql", "query", "cluster_type"):
        if job.get(key) not in (None, "", [], {}):
            config.setdefault(key, deepcopy(job[key]))
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


# 함수 설명: `_error_result()`는 예외 정보를 공통 errors 배열과 status가 포함된 실패 결과 구조로 만듭니다.
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


# 함수 설명: `_render_template()`는 검증된 파라미터를 SQL·URL·본문 템플릿에 치환해 실제 요청 문자열을 만듭니다.
def _render_template(template: str, params: dict[str, Any]) -> tuple[str, list[str]]:
    missing: list[str] = []

    # 함수 설명: `replace()`는 Datalake SQL 템플릿 placeholder를 자료형에 맞는 SQL literal로 치환하고 누락 key를 기록합니다.
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = _dict_get_ci(params, key)
        if value in (None, "", []):
            missing.append(key)
            return match.group(0)
        return _sql_literal(value)

    return re.sub(r"\{([^{}]+)\}", replace, str(template or "")), missing


# 함수 설명: `_sql_literal()`는 SQL 템플릿 파라미터를 자료형에 맞는 안전한 literal 표현으로 변환합니다.
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


# 함수 설명: `_missing_required_params()`는 필수 파라미터 중 실제 작업 값에 없는 항목을 찾아 오류 목록으로 반환합니다.
def _missing_required_params(params: dict[str, Any], required_params: Any) -> list[str]:
    missing = []
    for item in _as_list(required_params):
        key = str(item or "").strip()
        if key and _dict_get_ci(params, key) in (None, "", []):
            missing.append(key)
    return missing


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
    if value in (None, ""):
        return []
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict(orient="records")
        except TypeError:
            return value.to_dict()
    return [{"value": value}]


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

    # Langflow 출력 함수: '조회 페이로드 (retrieval_payload)' 포트가 요청될 때 실행됩니다.
    # 핵심 처리 결과를 Langflow Data/Message 형식으로 감싸 다음 노드에 전달합니다.
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
