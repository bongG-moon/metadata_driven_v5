# -*- coding: utf-8 -*-
# =============================================================================
# 컴포넌트 개요: 00E 공용 Report Context Publisher
# 역할: Report Recipe 또는 단일 Report Data를 후속 분석 가능한 Snapshot/View 저장 페이로드로 자동 변환합니다.
# 주요 입력: Chat Message, report.bundle.v1 또는 단일 Report Data, 표시 컬럼 수
# 주요 출력: 공용 MongoDB 결과 저장소가 저장할 Context payload
# 처리 흐름: 입력 Bundle 정규화 -> 실제 행 Schema 추출 -> Query Source 계약 자동 발행 -> Result Store 호환 Payload 생성
# 유지보수 포인트: 사용자는 source_alias·columns·allowed_operations JSON을 직접 작성하지 않습니다. Recipe는 View 데이터와 업무 계산만 제공합니다.
# =============================================================================

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from lfx.custom.custom_component.component import Component
from lfx.io import DataInput, HandleInput, IntInput, MultilineInput, Output, StrInput
from lfx.schema.data import Data


BUNDLE_CONTRACT_VERSION = "report.view.bundle.v1"
PAYLOAD_CONTRACT_VERSION = "report.context.payload.v1"
QUERY_SOURCE_CONTRACT_VERSION = "report.query_source.v1"
DEFAULT_ALLOWED_OPERATIONS = ["filter", "sort_and_top_n", "select_columns"]
MAX_QUERY_SOURCES = 12
MAX_COLUMNS_PER_SOURCE = 160
MAX_ALIASES_PER_SOURCE = 24
MAX_TEXT_LENGTH = 200
SAFE_SOURCE_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


# 함수 설명: `_text()`는 입력 값을 공백이 제거된 제한 길이 문자열로 정규화합니다.
def _text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    return str(value or "").strip()[:limit]


# 함수 설명: `_payload()`는 Langflow Data 또는 dict에서 안전한 복사본을 추출합니다.
def _payload(value: Any) -> dict[str, Any]:
    raw = getattr(value, "data", value)
    return deepcopy(raw) if isinstance(raw, dict) else {}


# 함수 설명: `_question_parts()`는 Message에서 질문과 session_id를 함께 추출합니다.
def _question_parts(value: Any) -> tuple[str, str]:
    data = getattr(value, "data", None)
    data = data if isinstance(data, dict) else {}
    question = _text(getattr(value, "text", None) or data.get("text") or data.get("question"), 4_000)
    session_id = _text(getattr(value, "session_id", None) or data.get("session_id"), 200)
    return question, session_id


# 함수 설명: `_string_list()`는 중복·빈 값을 제거한 제한 길이 문자열 목록을 만듭니다.
def _string_list(value: Any, limit: int) -> list[str]:
    result: list[str] = []
    values = re.split(r"[,;\n]+", value) if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    for item in values:
        text = _text(item)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


# 함수 설명: `_columns_from_rows()`는 행 key 등장 순서를 보존하며 실제 컬럼 목록을 계산합니다.
def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            column = _text(key)
            if column and column not in columns:
                columns.append(column)
            if len(columns) >= MAX_COLUMNS_PER_SOURCE:
                return columns
    return columns


# 함수 설명: `_safe_alias()`는 사람이 읽을 수 있는 입력을 Langflow 내부 source alias로 안전하게 변환합니다.
def _safe_alias(value: Any, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_]+", "_", _text(value, 120)).strip("_")
    if not candidate:
        candidate = fallback
    if not candidate[:1].isalpha():
        candidate = f"view_{candidate}"
    candidate = candidate[:80]
    if SAFE_SOURCE_ALIAS.fullmatch(candidate):
        return candidate
    return fallback


# 함수 설명: `_allowed_operations()`는 제한형 후속 분석 연산만 정규화해 반환합니다.
def _allowed_operations(value: Any) -> list[str]:
    aliases = {
        "filter": "filter",
        "apply_filters": "filter",
        "sort": "sort_and_top_n",
        "top_n": "sort_and_top_n",
        "sort_and_top_n": "sort_and_top_n",
        "select": "select_columns",
        "select_columns": "select_columns",
    }
    result: list[str] = []
    values = value if isinstance(value, list) and value else DEFAULT_ALLOWED_OPERATIONS
    for item in values:
        normalized = aliases.get(_text(item, 80).casefold())
        if normalized and normalized not in result:
            result.append(normalized)
    return result or list(DEFAULT_ALLOWED_OPERATIONS)


# 함수 설명: `_project_rows()`는 허용된 공개 컬럼만 저장하도록 행을 안전하게 투영합니다.
def _project_rows(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    return [{column: deepcopy(row.get(column)) for column in columns} for row in rows]


# 함수 설명: `_safe_metrics()`는 실제 공개 컬럼에 존재하는 Metric 설명만 보존합니다.
def _safe_metrics(value: Any, columns: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    allowed = set(columns)
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        column = _text(item.get("column"))
        if not column or column not in allowed:
            continue
        metric = {
            "key": _text(item.get("key") or column, 120),
            "column": column,
            "method": _text(item.get("method") or "display", 120),
        }
        for key in ("numerator_column", "denominator_column"):
            candidate = _text(item.get(key))
            if candidate and candidate in allowed:
                metric[key] = candidate
        if isinstance(item.get("scale"), (int, float)) and not isinstance(item.get("scale"), bool):
            metric["scale"] = item["scale"]
        result.append(metric)
        if len(result) >= 50:
            break
    return result


# 함수 설명: `_safe_predicates()`는 실제 공개 컬럼에 관한 고정 View predicate만 보존합니다.
def _safe_predicates(value: Any, columns: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    allowed = set(columns)
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        column = _text(item.get("column"))
        operator = _text(item.get("operator"), 40).casefold()
        if not column or column not in allowed or operator not in {"eq", "ne", "in", "not_in", "contains", "starts_with", "ends_with"}:
            continue
        result.append(
            {
                "column": column,
                "operator": operator,
                "value": deepcopy(item.get("value")),
                **({"materialized": True} if item.get("materialized") is True else {}),
            }
        )
        if len(result) >= 50:
            break
    return result


# 함수 설명: `_direct_bundle()`는 코드 없이 단일 Report Data만 연결한 경우의 기본 Bundle을 생성합니다.
def _direct_bundle(
    report_data_value: Any,
    *,
    report_title: Any,
    report_type: Any,
    view_label: Any,
    view_alias: Any,
    visible_columns: Any,
    identity_columns: Any,
) -> dict[str, Any]:
    data = _payload(report_data_value)
    rows = [deepcopy(item) for item in data.get("rows", []) if isinstance(item, dict)]
    configured_columns = _string_list(data.get("report_columns") or data.get("display_columns") or visible_columns, MAX_COLUMNS_PER_SOURCE)
    source_columns = _columns_from_rows(rows) or _string_list(data.get("columns"), MAX_COLUMNS_PER_SOURCE)
    columns = [column for column in configured_columns if column in source_columns] or source_columns
    label = _text(view_label) or _text(report_title) or "Report 상세"
    alias = _safe_alias(view_alias or data.get("source_alias") or "report_snapshot", "report_snapshot")
    return {
        "contract_version": BUNDLE_CONTRACT_VERSION,
        "report": {
            "report_type": _text(report_type, 120) or _text(data.get("report_type"), 120) or "simple_report",
            "title": _text(report_title) or _text(data.get("report_title")) or label,
            "snapshot_id": _text(data.get("snapshot_id"), 200),
            "snapshot_at": _text(data.get("snapshot_at"), 100),
        },
        "views": [
            {
                "view_key": alias,
                "display_name": label,
                "aliases": [label, "Report 상세"],
                "purpose": "case_detail",
                "rows": rows,
                "columns": columns,
                "identity_columns": _string_list(identity_columns, MAX_COLUMNS_PER_SOURCE),
                "default_view": True,
                "source_type": _text(data.get("source_type"), 80) or "report_snapshot",
            }
        ],
    }


# 함수 설명: `_selected_bundle()`는 Bundle 입력을 우선 사용하고 없으면 단일 Data용 기본 Bundle을 만듭니다.
def _selected_bundle(
    report_bundle_value: Any,
    report_data_value: Any,
    **direct_options: Any,
) -> tuple[dict[str, Any], str]:
    bundle = _payload(report_bundle_value)
    if bundle:
        return bundle, "bundle"
    return _direct_bundle(report_data_value, **direct_options), "direct_data"


# 함수 설명: `_blocked_payload()`는 입력 오류를 Result Store 호환 차단 Payload로 정규화합니다.
def _blocked_payload(question: str, session_id: str, issue_type: str, message: str) -> dict[str, Any]:
    issue = {"type": issue_type, "message": message}
    return {
        "contract_version": PAYLOAD_CONTRACT_VERSION,
        "request": {"question": question, "session_id": session_id, "request_scope": "report_snapshot"},
        "metadata_refs": [],
        "intent_plan": {"analysis_kind": "report_context_snapshot", "request_scope": "report_snapshot", "retrieval_jobs": [], "pandas_execution_plan": []},
        "source_results": [],
        "runtime_sources": {},
        "analysis": {"status": "skipped", "row_count": 0, "columns": []},
        "data": {"row_count": 0, "columns": []},
        "execution_gate": {"status": "blocked", "reason": issue_type},
        "trace": {
            "warnings": [],
            "errors": [issue],
            "inspection": {"report_context_publisher": {"stage": "00e_report_context_publisher", "status": "blocked", "errors": [issue]}},
        },
    }


# 함수 설명: `_normalized_views()`는 Report Bundle의 사람이 읽기 쉬운 View 정의를 저장·조회용 source 정의로 자동 변환합니다.
def _normalized_views(bundle: dict[str, Any], default_display_column_limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_views = bundle.get("views") if isinstance(bundle.get("views"), list) else []
    errors: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    raw_to_alias: dict[str, str] = {}
    aliases: set[str] = set()

    for index, raw in enumerate(raw_views[:MAX_QUERY_SOURCES], start=1):
        if not isinstance(raw, dict):
            errors.append({"type": "report_bundle_view_invalid", "message": f"{index}번째 Report View가 object가 아닙니다."})
            continue
        raw_key = _text(raw.get("source_alias") or raw.get("view_key") or raw.get("display_name"), 120)
        alias = _safe_alias(raw_key, f"report_view_{index}")
        if alias in aliases:
            errors.append({"type": "report_bundle_view_alias_duplicate", "message": f"중복된 Report View alias입니다: {alias}"})
            continue
        aliases.add(alias)
        raw_to_alias[raw_key] = alias
        rows = [deepcopy(item) for item in raw.get("rows", []) if isinstance(item, dict)]
        actual_columns = _columns_from_rows(rows) or _string_list(raw.get("columns"), MAX_COLUMNS_PER_SOURCE)
        requested_columns = _string_list(raw.get("display_columns") or raw.get("columns"), MAX_COLUMNS_PER_SOURCE)
        columns = [column for column in requested_columns if column in actual_columns] or actual_columns
        if not columns:
            errors.append({"type": "report_bundle_view_columns_missing", "message": f"Report View의 실제 컬럼을 확인할 수 없습니다: {raw_key or alias}"})
            continue
        visible_rows = _project_rows(rows, columns)
        display_name = _text(raw.get("display_name")) or alias
        view_aliases = _string_list([display_name, *_string_list(raw.get("aliases"), MAX_ALIASES_PER_SOURCE)], MAX_ALIASES_PER_SOURCE)
        grain = raw.get("grain") if isinstance(raw.get("grain"), dict) else {}
        configured_identity_columns = raw.get("identity_columns") or grain.get("columns")
        identity_columns = [column for column in _string_list(configured_identity_columns, MAX_COLUMNS_PER_SOURCE) if column in columns]
        default_columns = [column for column in _string_list(raw.get("default_display_columns") or raw.get("display_columns"), default_display_column_limit) if column in columns]
        normalized.append(
            {
                "source_alias": alias,
                "dataset_key": _safe_alias(raw.get("dataset_key") or alias, alias),
                "source_type": _text(raw.get("source_type"), 80) or "report_materialized_view",
                "display_name": display_name,
                "purpose": _text(raw.get("purpose"), 120) or ("case_detail" if raw.get("default_view") is True else alias),
                "aliases": view_aliases,
                "rows": visible_rows,
                "columns": columns,
                "identity_columns": identity_columns,
                "grain_kind": _text(grain.get("kind"), 80) or "row",
                "grain_unique": grain.get("unique") is True or raw.get("unique") is True,
                "metrics": _safe_metrics(raw.get("metrics"), columns),
                "predicates": _safe_predicates(raw.get("predicates"), columns),
                "allowed_operations": _allowed_operations(raw.get("allowed_operations")),
                "default_display_columns": default_columns or columns[:default_display_column_limit],
                # Bundle의 모든 원본 View를 자동 공개하지 않습니다. 기본 View이거나
                # Recipe가 명시적으로 허용한 완성 View만 Flow 07-2 query source가 됩니다.
                "followup_enabled": raw.get("followup_enabled") is True or raw.get("default_view") is True,
                "default_view": raw.get("default_view") is True,
                "raw_lineage": _string_list(raw.get("lineage"), MAX_QUERY_SOURCES),
            }
        )

    if not normalized:
        errors.append({"type": "report_bundle_views_missing", "message": "후속 분석용 Report View가 하나도 없습니다."})
        return [], errors
    enabled_views = [item for item in normalized if item["followup_enabled"]]
    if not enabled_views:
        errors.append({"type": "report_bundle_followup_view_missing", "message": "후속 분석을 허용한 Report View가 하나도 없습니다."})
        return [], errors
    default_views = [item for item in enabled_views if item["default_view"]]
    if len(default_views) > 1:
        errors.append({"type": "report_bundle_default_view_ambiguous", "message": "기본 Report View는 하나만 지정할 수 있습니다."})
    elif not default_views:
        enabled_views[0]["default_view"] = True

    known_aliases = {item["source_alias"] for item in normalized}
    for item in normalized:
        resolved_lineage: list[str] = []
        for raw_alias in item.pop("raw_lineage"):
            alias = raw_to_alias.get(raw_alias) or _safe_alias(raw_alias, "")
            if alias and alias in known_aliases and alias not in resolved_lineage:
                resolved_lineage.append(alias)
            elif raw_alias:
                errors.append({"type": "report_bundle_lineage_unknown", "message": f"Report View lineage가 Bundle 안의 View를 가리키지 않습니다: {raw_alias}"})
        item["lineage"] = resolved_lineage
    return normalized, errors


# 함수 설명: `_query_source_contract()`는 View 정의에서 Flow 07-2가 소비할 제한형 Query Source 계약을 자동 생성합니다.
def _query_source_contract(view: dict[str, Any]) -> dict[str, Any]:
    contract = {
        "contract_version": QUERY_SOURCE_CONTRACT_VERSION,
        "source_alias": view["source_alias"],
        "dataset_key": view["dataset_key"],
        "purpose": view["purpose"],
        "display_name": view["display_name"],
        "aliases": list(view["aliases"]),
        "authoritative": True,
        "columns": list(view["columns"]),
        "grain": {"kind": view["grain_kind"], "columns": list(view["identity_columns"]), "unique": view["grain_unique"]},
        "metrics": deepcopy(view["metrics"]),
        "predicates": deepcopy(view["predicates"]),
        "allowed_operations": list(view["allowed_operations"]),
        "default_display_columns": list(view["default_display_columns"]),
        "default_view": view["default_view"],
    }
    if view["lineage"]:
        contract["lineage"] = list(view["lineage"])
        if len(view["lineage"]) == 1:
            contract["materialized_from"] = view["lineage"][0]
    return contract


# 주요 함수: 사람이 작성한 Bundle JSON 대신 View 데이터만 받아 Context 저장 Payload를 자동 생성합니다.
def build_report_context_payload(
    question_value: Any,
    report_bundle_value: Any = None,
    report_data_value: Any = None,
    *,
    report_title: Any = "",
    report_type: Any = "",
    view_label: Any = "",
    view_alias: Any = "",
    visible_columns: Any = "",
    identity_columns: Any = "",
    default_display_column_limit: Any = 12,
) -> dict[str, Any]:
    question, session_id = _question_parts(question_value)
    try:
        display_limit = max(1, min(int(default_display_column_limit), 40))
    except (TypeError, ValueError, OverflowError):
        display_limit = 12
    bundle, input_mode = _selected_bundle(
        report_bundle_value,
        report_data_value,
        report_title=report_title,
        report_type=report_type,
        view_label=view_label,
        view_alias=view_alias,
        visible_columns=visible_columns,
        identity_columns=identity_columns,
    )
    if bundle.get("contract_version") != BUNDLE_CONTRACT_VERSION:
        return _blocked_payload(question, session_id, "report_bundle_contract_invalid", f"Report Context Publisher에는 {BUNDLE_CONTRACT_VERSION} Bundle이 필요합니다.")
    if not session_id:
        return _blocked_payload(question, session_id, "missing_session_id", "현재 실행의 session_id를 확인할 수 없어 Report Context를 저장하지 않았습니다.")
    request = bundle.get("request") if isinstance(bundle.get("request"), dict) else {}
    bundle_session_id = _text(request.get("session_id"), 200)
    if bundle_session_id and bundle_session_id != session_id:
        return _blocked_payload(question, session_id, "report_bundle_session_mismatch", "Report Bundle의 session_id가 현재 요청과 다릅니다.")

    views, errors = _normalized_views(bundle, display_limit)
    if errors:
        first = errors[0]
        return _blocked_payload(question, session_id, _text(first.get("type"), 120) or "report_bundle_invalid", _text(first.get("message"), 500) or "Report Bundle을 안전하게 해석하지 못했습니다.")
    report = bundle.get("report") if isinstance(bundle.get("report"), dict) else {}
    primary = next((item for item in views if item["followup_enabled"] and item["default_view"]), views[0])
    source_results = []
    runtime_sources: dict[str, list[dict[str, Any]]] = {}
    requirements = []
    for view in views:
        source_result = {
                "source_alias": view["source_alias"],
                "dataset_key": view["dataset_key"],
                "source_type": view["source_type"],
                "display_name": view["display_name"],
                "columns": list(view["columns"]),
                "row_count": len(view["rows"]),
                "applied_params": {
                    "snapshot_id": _text(report.get("snapshot_id"), 200),
                    "snapshot_at": _text(report.get("snapshot_at"), 100),
                },
            }
        if view["followup_enabled"]:
            source_result["query_source_contract"] = _query_source_contract(view)
        source_results.append(source_result)
        runtime_sources[view["source_alias"]] = deepcopy(view["rows"])
        requirements.append({"source_alias": view["source_alias"], "dataset_key": view["dataset_key"]})

    return {
        "contract_version": PAYLOAD_CONTRACT_VERSION,
        "request": {"question": question, "session_id": session_id, "request_scope": "report_snapshot"},
        "metadata_refs": [],
        "intent_plan": {
            "analysis_kind": "report_context_snapshot",
            "request_scope": "report_snapshot",
            "retrieval_jobs": [],
            "pandas_execution_plan": [],
            "resolved_execution_graph": {"external_source_requirements": requirements},
        },
        "source_results": source_results,
        "runtime_sources": runtime_sources,
        "_full_result_rows": deepcopy(primary["rows"]),
        "analysis": {
            "status": "ok",
            "row_count": len(primary["rows"]),
            "columns": list(primary["columns"]),
            "analysis_code": "deterministic_report_context_publisher",
            "scope": {
                "report_type": _text(report.get("report_type"), 120) or "simple_report",
                "report_title": _text(report.get("title")) or "Report",
                "snapshot_id": _text(report.get("snapshot_id"), 200),
                "snapshot_at": _text(report.get("snapshot_at"), 100),
            },
        },
        "data": {"row_count": len(primary["rows"]), "columns": list(primary["columns"])},
        "execution_gate": {"status": "ready"},
        "trace": {
            "warnings": [],
            "errors": [],
            "inspection": {
                "report_context_publisher": {
                    "stage": "00e_report_context_publisher",
                    "status": "ready",
                    "input_mode": input_mode,
                    "report_type": _text(report.get("report_type"), 120) or "simple_report",
                    "query_source_aliases": [item["source_alias"] for item in views],
                    "default_source_alias": primary["source_alias"],
                    "errors": [],
                }
            },
        },
    }


# Langflow 컴포넌트 클래스: Recipe Bundle 또는 단일 Data를 자동 후속 분석 Context로 발행합니다.
class ReportContextPublisher(Component):
    display_name = "00E 공용 Report Context Publisher"
    description = "Report View 데이터만 연결하면 Query Source 계약·Snapshot·후속 분석 Context를 자동 생성합니다."
    name = "ReportContextPublisher"
    icon = "DatabaseZap"
    inputs = [
        HandleInput(name="question", display_name="Report 요청", info="Chat Input의 현재 요청과 session_id를 전달합니다.", input_types=["Message"], required=True),
        DataInput(name="report_bundle", display_name="Report View Bundle", info="Recipe가 만든 report.view.bundle.v1입니다. 여러 View가 필요한 Report는 이 입력을 사용합니다.", required=False),
        DataInput(name="report_data", display_name="단일 Report Data", info="코드 없이 단순 Report를 만들 때 rows/columns가 포함된 Data를 연결합니다.", required=False),
        StrInput(name="report_title", display_name="Report 제목", value="", required=False, advanced=True),
        StrInput(name="report_type", display_name="Report 유형", value="", required=False, advanced=True),
        StrInput(name="view_label", display_name="기본 View 표시명", value="", required=False, advanced=True),
        StrInput(name="view_alias", display_name="기본 View 내부 이름", value="", required=False, advanced=True),
        MultilineInput(name="visible_columns", display_name="공개 컬럼", value="", required=False, advanced=True, info="단일 Data 모드에서 쉼표 또는 줄바꿈으로 지정합니다. 비우면 Report Data의 공개 schema를 사용합니다."),
        MultilineInput(name="identity_columns", display_name="행 식별 컬럼", value="", required=False, advanced=True, info="단일 Data 모드에서 선택적으로 지정합니다. filter/sort/select만 사용할 경우 비워 둘 수 있습니다."),
        IntInput(name="default_display_column_limit", display_name="기본 표시 컬럼 수", value=12, required=False, advanced=True),
    ]
    outputs = [Output(name="context_payload", display_name="Context 저장 페이로드", method="build_context_payload", types=["Data"])]

    # 함수 설명: `build_context_payload()`는 입력 Bundle 또는 단일 Data를 Result Store 호환 Context payload로 발행합니다.
    def build_context_payload(self) -> Data:
        result = build_report_context_payload(
            getattr(self, "question", None),
            getattr(self, "report_bundle", None),
            getattr(self, "report_data", None),
            report_title=getattr(self, "report_title", ""),
            report_type=getattr(self, "report_type", ""),
            view_label=getattr(self, "view_label", ""),
            view_alias=getattr(self, "view_alias", ""),
            visible_columns=getattr(self, "visible_columns", ""),
            identity_columns=getattr(self, "identity_columns", ""),
            default_display_column_limit=getattr(self, "default_display_column_limit", 12),
        )
        self.status = result.get("trace", {}).get("inspection", {}).get("report_context_publisher", result.get("execution_gate", {}))
        return Data(data=result)
