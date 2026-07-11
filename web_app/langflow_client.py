from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .session_state_store import MongoDBSessionStateStore, MongoSessionStateSettings


DEFAULT_COLLECTIONS = {
    "domain": "agent_v4_domain_items",
    "table_catalog": "agent_v4_table_catalog_items",
    "main_flow_filter": "agent_v4_main_flow_filters",
}
DEFAULT_LANGFLOW_TIMEOUT_SECONDS = 300
_LANGFLOW_NODE_INPUT_SETTINGS_API_FIELD = "twe" + "aks"
DEVELOPER_PAYLOAD_KEYS = (
    "analysis_code",
    "failed_analysis_code",
    "data_preparation_code",
    "prepared_dataframe",
    "pandas_execution_status",
    "analysis_status",
    "analysis_plan",
    "source_summaries",
    "filter_notes",
    "merge_notes",
    "pandas_code_json",
    "reasoning_steps",
)


@dataclass(frozen=True)
class LangflowSettings:
    api_key: str = ""
    router_api_url: str = ""
    data_analysis_api_url: str = ""
    metadata_qa_api_url: str = ""
    domain_saving_api_url: str = ""
    table_catalog_saving_api_url: str = ""
    main_flow_filter_saving_api_url: str = ""
    input_type: str = "chat"
    output_type: str = "chat"
    timeout: int = DEFAULT_LANGFLOW_TIMEOUT_SECONDS
    session_store: str = "disabled"
    mongo_uri: str = ""
    mongo_database: str = "datagov"
    session_state_collection: str = "agent_v4_session_states"
    domain_collection: str = DEFAULT_COLLECTIONS["domain"]
    table_catalog_collection: str = DEFAULT_COLLECTIONS["table_catalog"]
    main_flow_filter_collection: str = DEFAULT_COLLECTIONS["main_flow_filter"]
    session_state_preview_row_limit: int = 5
    session_state_history_limit: int = 10

    @classmethod
    def from_env(cls) -> "LangflowSettings":
        local_env = _load_local_env()
        base_url = _env("LANGFLOW_BASE_URL", local_env) or _env("LANGFLOW_API_BASE_URL", local_env)
        mongo_uri = _env("MONGODB_URI", local_env) or _env("MONGO_URI", local_env)
        session_store = _env("WEB_SESSION_STORE", local_env) or ("mongodb" if mongo_uri else "disabled")
        return cls(
            api_key=_env("LANGFLOW_API_KEY", local_env),
            router_api_url=_env("LANGFLOW_ROUTER_API_URL", local_env) or _flow_run_url(base_url, _env("LANGFLOW_ROUTER_FLOW_ID", local_env)),
            data_analysis_api_url=_env("LANGFLOW_DATA_ANALYSIS_API_URL", local_env)
            or _flow_run_url(base_url, _env("LANGFLOW_DATA_ANALYSIS_FLOW_ID", local_env)),
            metadata_qa_api_url=_env("LANGFLOW_METADATA_QA_API_URL", local_env)
            or _flow_run_url(base_url, _env("LANGFLOW_METADATA_QA_FLOW_ID", local_env)),
            domain_saving_api_url=_env("LANGFLOW_DOMAIN_SAVING_API_URL", local_env)
            or _env("LANGFLOW_DOMAIN_AUTHORING_API_URL", local_env)
            or _flow_run_url(
                base_url,
                _env("LANGFLOW_DOMAIN_SAVING_FLOW_ID", local_env) or _env("LANGFLOW_DOMAIN_AUTHORING_FLOW_ID", local_env),
            ),
            table_catalog_saving_api_url=_env("LANGFLOW_TABLE_CATALOG_SAVING_API_URL", local_env)
            or _env("LANGFLOW_TABLE_CATALOG_AUTHORING_API_URL", local_env)
            or _flow_run_url(
                base_url,
                _env("LANGFLOW_TABLE_CATALOG_SAVING_FLOW_ID", local_env)
                or _env("LANGFLOW_TABLE_CATALOG_AUTHORING_FLOW_ID", local_env),
            ),
            main_flow_filter_saving_api_url=_env("LANGFLOW_MAIN_FILTER_SAVING_API_URL", local_env)
            or _env("LANGFLOW_MAIN_FLOW_FILTER_SAVING_API_URL", local_env)
            or _env("LANGFLOW_MAIN_FILTER_AUTHORING_API_URL", local_env)
            or _env("LANGFLOW_MAIN_FLOW_FILTER_AUTHORING_API_URL", local_env)
            or _flow_run_url(
                base_url,
                _env("LANGFLOW_MAIN_FILTER_SAVING_FLOW_ID", local_env)
                or _env("LANGFLOW_MAIN_FILTER_AUTHORING_FLOW_ID", local_env),
            ),
            input_type=_env("LANGFLOW_INPUT_TYPE", local_env) or "chat",
            output_type=_env("LANGFLOW_OUTPUT_TYPE", local_env) or "chat",
            timeout=_int_env("LANGFLOW_TIMEOUT_SECONDS", DEFAULT_LANGFLOW_TIMEOUT_SECONDS, local_env),
            session_store=session_store,
            mongo_uri=mongo_uri,
            mongo_database=_env("MONGODB_DATABASE", local_env) or _env("MONGO_DB_NAME", local_env) or "datagov",
            session_state_collection=_env("MONGODB_SESSION_STATE_COLLECTION", local_env) or "agent_v4_session_states",
            domain_collection=_env("MONGODB_DOMAIN_COLLECTION", local_env) or _env("DOMAIN_COLLECTION_NAME", local_env) or DEFAULT_COLLECTIONS["domain"],
            table_catalog_collection=_env("MONGODB_TABLE_CATALOG_COLLECTION", local_env)
            or _env("TABLE_CATALOG_COLLECTION_NAME", local_env)
            or DEFAULT_COLLECTIONS["table_catalog"],
            main_flow_filter_collection=_env("MONGODB_MAIN_FLOW_FILTER_COLLECTION", local_env)
            or _env("MAIN_FLOW_FILTER_COLLECTION_NAME", local_env)
            or DEFAULT_COLLECTIONS["main_flow_filter"],
            session_state_preview_row_limit=_int_env("SESSION_STATE_PREVIEW_ROW_LIMIT", 5, local_env),
            session_state_history_limit=_int_env("SESSION_STATE_HISTORY_LIMIT", 10, local_env),
        )

    def authoring_url(self, metadata_type: str) -> str:
        kind = normalize_metadata_type(metadata_type)
        return {
            "domain": self.domain_saving_api_url,
            "table_catalog": self.table_catalog_saving_api_url,
            "main_flow_filter": self.main_flow_filter_saving_api_url,
        }[kind]

    def configured_summary(self) -> dict[str, bool]:
        return {
            "query": bool(self.router_api_url or self.data_analysis_api_url),
            "router": bool(self.router_api_url),
            "data_analysis": bool(self.data_analysis_api_url),
            "metadata_qa": bool(self.metadata_qa_api_url),
            "domain": bool(self.domain_saving_api_url),
            "table_catalog": bool(self.table_catalog_saving_api_url),
            "main_flow_filter": bool(self.main_flow_filter_saving_api_url),
            "session_state": self.session_store == "mongodb" and bool(self.mongo_uri),
        }

class LangflowApiClient:
    def __init__(self, settings: LangflowSettings | None = None) -> None:
        self.settings = settings or LangflowSettings.from_env()
        self.session_state_store = _build_session_state_store(self.settings)

    def run_query(self, question: str, session_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        effective_state = state if state is not None else self._load_session_state(session_id)
        if self.settings.router_api_url:
            result = self.run_router_query(question, session_id, effective_state)
        elif self.settings.data_analysis_api_url:
            result = self.run_data_analysis_query(question, session_id, effective_state)
        else:
            raise ValueError("LANGFLOW_ROUTER_API_URL or LANGFLOW_DATA_ANALYSIS_API_URL is not configured.")
        return self._attach_and_save_session_state(result, session_id, question)

    def run_router_query(self, question: str, session_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call only the router flow; the router flow owns the selected-flow execution."""
        if not self.settings.router_api_url:
            raise ValueError("LANGFLOW_ROUTER_API_URL or LANGFLOW_ROUTER_FLOW_ID is not configured.")
        try:
            raw_route_response = call_langflow_api(
                self.settings.router_api_url,
                api_key=self.settings.api_key,
                input_value=question,
                session_id=session_id,
                input_type=self.settings.input_type,
                output_type=self.settings.output_type,
                node_input_settings=build_router_node_input_settings(state, session_id),
                timeout=self.settings.timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise TimeoutError(
                "Router Langflow API timed out. The web app now calls only the router flow, so check the router canvas, "
                "its selected child Flow API call, and the Langflow Desktop logs for the nested flow failure."
            ) from exc

        router_executed_payload = extract_selected_flow_execution_payload(raw_route_response)
        route_payload = normalize_route_response(router_executed_payload or raw_route_response)
        selected_flow = str(route_payload.get("selected_flow") or router_executed_payload.get("selected_flow") or "")
        normalization_source = (
            router_executed_payload.get("raw_response")
            or router_executed_payload.get("selected_flow_response")
            or raw_route_response
        )
        if _is_authoring_selected_flow(selected_flow) or _looks_like_authoring(extract_authoring_payload(normalization_source)):
            result = normalize_authoring_response(normalization_source)
        elif _is_direct_router_route(route_payload.get("route")) or _router_execution_failed(router_executed_payload):
            result = normalize_direct_router_response(normalization_source, route_payload)
        else:
            result = normalize_query_response(normalization_source)
        if selected_flow:
            _apply_selected_flow_defaults(result, selected_flow)
        result["selected_flow"] = selected_flow
        result["api_mode"] = "langflow_router_only"
        result["route_decision"] = route_payload
        result["raw_route_response"] = raw_route_response
        if not result.get("state") and isinstance(state, dict):
            result["state"] = state
        result["raw_response"] = raw_route_response
        return result

    def run_data_analysis_query(self, question: str, session_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.settings.data_analysis_api_url:
            raise ValueError("LANGFLOW_DATA_ANALYSIS_API_URL or LANGFLOW_DATA_ANALYSIS_FLOW_ID is not configured.")
        raw_response = call_langflow_api(
            self.settings.data_analysis_api_url,
            api_key=self.settings.api_key,
            input_value=question,
            session_id=session_id,
            input_type=self.settings.input_type,
            output_type=self.settings.output_type,
            node_input_settings=build_data_analysis_node_input_settings(state, session_id),
            timeout=self.settings.timeout,
        )
        result = normalize_query_response(raw_response)
        result["api_mode"] = "langflow_data_analysis_direct"
        result["selected_flow"] = "data_analysis_flow"
        result["raw_response"] = raw_response
        return result

    def run_orchestrated_query(self, question: str, session_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.run_router_query(question, session_id, state)

    def _load_session_state(self, session_id: str) -> dict[str, Any]:
        if not self.session_state_store:
            return {}
        return self.session_state_store.load_state(session_id)

    def _attach_and_save_session_state(self, result: dict[str, Any], session_id: str, question: str) -> dict[str, Any]:
        if not self.session_state_store:
            return result
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        write_status = self.session_state_store.save_state(
            session_id,
            state,
            question=question,
            response_type=str(result.get("response_type") or ""),
        )
        result["session_state_store"] = {
            "load": dict(self.session_state_store.last_load_status),
            "write": write_status,
        }
        return result

    def run_authoring(self, metadata_type: str, raw_text: str, session_id: str) -> dict[str, Any]:
        kind = normalize_metadata_type(metadata_type)
        api_url = self.settings.authoring_url(kind)
        if not api_url:
            raise ValueError(f"{kind} saving API URL 또는 flow id가 설정되지 않았습니다.")
        raw_response = call_langflow_api(
            api_url,
            api_key=self.settings.api_key,
            input_value=raw_text,
            session_id=session_id,
            input_type=self.settings.input_type,
            output_type=self.settings.output_type,
            node_input_settings=build_authoring_node_input_settings(kind),
            timeout=self.settings.timeout,
        )
        result = normalize_authoring_response(raw_response)
        result["metadata_type"] = result.get("metadata_type") or kind
        result["api_mode"] = "langflow_api"
        result["raw_response"] = raw_response
        return result


def call_langflow_api(
    api_url: str,
    api_key: str,
    input_value: str,
    session_id: str,
    input_type: str = "chat",
    output_type: str = "chat",
    node_input_settings: dict[str, Any] | None = None,
    timeout: int = DEFAULT_LANGFLOW_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not str(api_url or "").strip():
        raise ValueError("Langflow API URL is not configured.")
    if not str(input_value or "").strip():
        raise ValueError("input_value is empty.")
    payload: dict[str, Any] = {
        "output_type": output_type or "chat",
        "input_type": input_type or "chat",
        "input_value": input_value,
        "session_id": session_id,
    }
    if node_input_settings:
        payload[_LANGFLOW_NODE_INPUT_SETTINGS_API_FIELD] = node_input_settings
    headers = {"Content-Type": "application/json"}
    if str(api_key or "").strip():
        headers["x-api-key"] = str(api_key).strip()
    response = requests.post(api_url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    parsed = response.json()
    return parsed if isinstance(parsed, dict) else {"response": parsed}


def _build_session_state_store(settings: LangflowSettings) -> MongoDBSessionStateStore | None:
    if str(settings.session_store or "").strip().lower() != "mongodb":
        return None
    store_settings = MongoSessionStateSettings(
        enabled=bool(settings.mongo_uri),
        mongo_uri=settings.mongo_uri,
        mongo_database=settings.mongo_database,
        collection_name=settings.session_state_collection,
        preview_row_limit=settings.session_state_preview_row_limit,
        history_limit=settings.session_state_history_limit,
    )
    return MongoDBSessionStateStore(store_settings)


def build_router_node_input_settings(state: dict[str, Any] | None, session_id: str) -> dict[str, Any] | None:
    return None


def build_data_analysis_node_input_settings(state: dict[str, Any] | None, session_id: str) -> dict[str, Any] | None:
    route_state = _as_dict(state)
    return {"00 분석 요청 로더": {"previous_state": route_state}} if route_state else None


def build_split_flow_node_input_settings(
    selected_flow: str,
    route_payload: dict[str, Any],
    state: dict[str, Any] | None,
    session_id: str,
) -> dict[str, Any] | None:
    flow_inputs = _as_dict(route_payload.get("flow_inputs"))
    route_state = _as_dict(flow_inputs.get("state")) or _as_dict(state)
    common = {"previous_state": route_state} if route_state else {}
    if selected_flow == "metadata_qa_flow":
        return {"00 메타데이터 QA 요청 로더": common} if common else None
    if selected_flow == "data_analysis_flow":
        return {"00 분석 요청 로더": common} if common else None
    return None


def build_authoring_node_input_settings(metadata_type: str) -> dict[str, Any]:
    kind = normalize_metadata_type(metadata_type)
    collection_name = _collection_name(kind)
    labels = {
        "domain": {
            "collection": ["00 도메인 기존 항목 로더", "05 도메인 동일 Key 조회기", "07 도메인 검수/저장 처리기"],
        },
        "table_catalog": {
            "collection": ["00 테이블 카탈로그 기존 항목 로더", "05 테이블 카탈로그 동일 Key 조회기", "07 테이블 카탈로그 검수/저장 처리기"],
        },
        "main_flow_filter": {
            "collection": ["00 메인 플로우 필터 기존 항목 로더", "05 메인 플로우 필터 동일 Key 조회기", "07 메인 플로우 필터 검수/저장 처리기"],
        },
    }[kind]
    settings: dict[str, Any] = {}
    for label in labels["collection"]:
        settings.setdefault(label, {})["collection_name"] = collection_name
    return settings


def normalize_query_response(api_response: Any) -> dict[str, Any]:
    payload = extract_main_payload(api_response)
    structured_payload = _looks_like_query(payload)
    data = _query_data(payload)
    developer = _query_developer(api_response, payload)
    developer = _merge_missing(developer, _query_pandas_developer(api_response, payload))
    applied_scope = _as_dict(payload.get("applied_scope") or payload.get("scope"))
    intent_plan = _as_dict(payload.get("intent_plan"))
    intent = _as_dict(payload.get("intent"))
    analysis = _query_analysis(payload, data, developer)
    warnings = _unique_values([*_as_list(payload.get("warnings")), *_as_list(analysis.get("warnings"))])
    errors = _unique_values([*_as_list(payload.get("errors")), *_as_list(analysis.get("errors"))])
    plain_answer = _first_text(payload, ["answer_message", "response", "answer", "text", "content"])
    answer = _first_text(payload, ["display_message", "formatted_message", "chat_message", "message"]) or plain_answer
    if not answer:
        answer = _extract_text_anywhere(api_response)
    if not answer:
        answer = "응답 텍스트를 찾지 못했습니다. Raw payload를 확인하세요."
    metadata_qa = _as_dict(payload.get("metadata_qa"))
    metadata_route = _as_dict(payload.get("metadata_route"))
    direct_response_ready = bool(payload.get("direct_response_ready") or metadata_qa)

    result = {
        "status": str(payload.get("status") or analysis.get("status") or ("error" if errors else "ok")),
        "success": bool(payload.get("success", not errors)),
        "response_type": str(payload.get("response_type") or ("metadata_qa" if direct_response_ready else "analysis" if structured_payload else "message")),
        "direct_response_ready": direct_response_ready,
        "message_only": bool(answer and not structured_payload),
        "answer_message": answer,
        "plain_answer_message": plain_answer,
        "display_message": answer,
        "message": answer,
        "answer_sections": _as_dict(payload.get("answer_sections")),
        "answer_type": str(payload.get("answer_type") or ""),
        "data": data,
        "applied_scope": applied_scope,
        "intent_plan": intent_plan or intent,
        "intent": intent,
        "metadata_qa": metadata_qa,
        "metadata_route": metadata_route,
        "analysis": analysis,
        "state": _as_dict(payload.get("state")),
        "warnings": warnings,
        "errors": errors,
        "data_refs": _collect_data_refs(payload, data, developer, api_response),
        "developer": developer,
    }
    if result["developer"] and not result["analysis"].get("analysis_code"):
        code = result["developer"].get("analysis_code") or _as_dict(result["developer"].get("pandas_code_json")).get("code")
        if code:
            result["analysis"]["analysis_code"] = code
    return result


def normalize_route_response(api_response: Any) -> dict[str, Any]:
    payload = extract_route_payload(api_response)
    metadata_route = _as_dict(payload.get("metadata_route"))
    flow_inputs = _as_dict(payload.get("flow_inputs"))
    selected_flow = str(payload.get("selected_flow") or "")
    if not selected_flow:
        route = str(payload.get("route") or metadata_route.get("route") or "data_analysis")
        selected_flow = {
            "direct_answer": "",
            "clarification": "",
            "flow_error": "",
            "metadata_qa": "metadata_qa_flow",
            "data_analysis": "data_analysis_flow",
            "domain_saving": "domain_saving_flow",
            "domain_authoring": "domain_saving_flow",
            "table_catalog_saving": "table_catalog_saving_flow",
            "table_catalog_authoring": "table_catalog_saving_flow",
            "main_flow_filter_saving": "main_flow_filters_saving_flow",
            "main_flow_filter_authoring": "main_flow_filters_saving_flow",
        }.get(route, "data_analysis_flow")
    route = str(payload.get("route") or metadata_route.get("route") or _route_from_selected_flow(selected_flow) or "data_analysis")
    return {
        "status": str(payload.get("status") or "ok"),
        "response_type": str(payload.get("response_type") or "route_decision"),
        "route": route,
        "selected_flow": selected_flow,
        "route_confidence": payload.get("route_confidence") or metadata_route.get("route_confidence") or metadata_route.get("confidence"),
        "route_source": payload.get("route_source") or metadata_route.get("route_source"),
        "route_llm_used": bool(payload.get("route_llm_used") or metadata_route.get("route_llm_used")),
        "metadata_action": payload.get("metadata_action") or metadata_route.get("metadata_action"),
        "target_dataset": payload.get("target_dataset") or metadata_route.get("target_dataset"),
        "target_family": payload.get("target_family") or metadata_route.get("target_family"),
        "reason": payload.get("reason") or metadata_route.get("reason"),
        "metadata_route": metadata_route or _as_dict(flow_inputs.get("metadata_route")),
        "flow_inputs": flow_inputs,
        "warnings": _as_list(payload.get("warnings")),
        "errors": _as_list(payload.get("errors")),
    }


def normalize_authoring_response(api_response: Any) -> dict[str, Any]:
    payload = extract_authoring_payload(api_response)
    kind = normalize_metadata_type(payload.get("metadata_type") or payload.get("flow_type"))
    review = _as_dict(payload.get("review") or payload.get("review_result"))
    write_result = _as_dict(payload.get("write_result"))
    trace = _normalize_authoring_trace(payload)
    errors = _unique_values([*_as_list(payload.get("errors")), *_trace_values(trace, "errors")])
    warnings = _unique_values([*_as_list(payload.get("warnings")), *_trace_values(trace, "warnings")])
    generated_items = _as_list(payload.get("items")) or _as_list(trace.get("generated_items_preview"))
    existing_matches = _as_list(payload.get("existing_matches")) or _as_list(trace.get("existing_matches"))
    conflict_warnings = _as_list(payload.get("conflict_warnings")) or _as_list(trace.get("conflict_warnings")) or warnings
    ui_status = _authoring_ui_status(payload, review, write_result, trace, existing_matches, conflict_warnings, errors)
    return {
        "response_type": str(payload.get("response_type") or "metadata_authoring"),
        "status": str(payload.get("status") or write_result.get("status") or ui_status),
        "ui_status": ui_status,
        "message": _first_text(payload, ["message", "response", "answer_message"])
        or _extract_text_anywhere(api_response)
        or _authoring_message(ui_status, write_result, review),
        "metadata_type": kind,
        "items": [item for item in generated_items if isinstance(item, dict)],
        "existing_matches": existing_matches,
        "conflict_warnings": conflict_warnings,
        "review": review,
        "write_result": write_result,
        "trace": trace,
        "errors": errors,
        "warnings": warnings,
        "api_response": payload,
    }


def normalize_direct_router_response(api_response: Any, route_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = extract_main_payload(api_response)
    api_payload = _as_dict(payload.get("api_response"))
    if str(api_payload.get("response_type") or "") in {"direct_answer", "clarification", "flow_error", "routed_flow_execution"}:
        payload = api_payload
    nested_payload = _as_dict(_as_dict(payload.get("raw_response")).get("api_response"))
    if str(nested_payload.get("response_type") or "") in {"direct_answer", "clarification", "flow_error"}:
        payload = nested_payload
    route_info = _as_dict(route_payload)
    route = str(payload.get("response_type") or route_info.get("route") or "direct_answer")
    status = str(payload.get("status") or route_info.get("status") or ("needs_more_input" if route == "clarification" else "ok"))
    trace_errors = _as_list(_as_dict(_as_dict(payload.get("trace")).get("execution")).get("errors"))
    message = (
        _first_text(payload, ["message", "answer_message", "response", "text", "content"])
        or _first_text(route_info, ["message", "reason"])
        or _extract_text_anywhere(api_response)
        or ""
    )
    return {
        "status": status,
        "success": status not in {"error", "failed"},
        "response_type": route,
        "direct_response_ready": route in {"direct_answer", "clarification", "flow_error"},
        "message_only": True,
        "answer_message": message,
        "plain_answer_message": message,
        "display_message": message,
        "message": message,
        "data": {"rows": [], "columns": [], "row_count": 0},
        "applied_scope": {},
        "intent_plan": {},
        "intent": {},
        "metadata_qa": {},
        "metadata_route": {},
        "analysis": {"status": status},
        "state": _as_dict(route_info.get("state")) or _as_dict(payload.get("state")),
        "warnings": _as_list(payload.get("warnings")),
        "errors": _as_list(payload.get("errors")) or trace_errors,
        "data_refs": [],
        "developer": {},
    }


def extract_main_payload(value: Any) -> dict[str, Any]:
    for item in _walk(value):
        item = _parse_json_dict(item) if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        api_payload = item.get("api_response")
        if isinstance(api_payload, dict) and _looks_like_query(api_payload):
            return dict(api_payload)
        if _looks_like_query(item):
            return dict(item)
    return _as_dict(value)


def extract_route_payload(value: Any) -> dict[str, Any]:
    for item in _walk(value):
        item = _parse_json_dict(item) if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        route_payload = item.get("route_response")
        if isinstance(route_payload, dict) and _looks_like_route_decision(route_payload):
            return dict(route_payload)
        api_payload = item.get("api_response")
        if isinstance(api_payload, dict) and _looks_like_route_decision(api_payload):
            return dict(api_payload)
        data_payload = item.get("data")
        if isinstance(data_payload, dict) and _looks_like_route_decision(data_payload):
            return dict(data_payload)
        if _looks_like_route_decision(item):
            return dict(item)
    return _as_dict(value)


def extract_authoring_payload(value: Any) -> dict[str, Any]:
    for item in _walk(value):
        item = _parse_json_dict(item) if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        api_payload = item.get("api_response")
        if isinstance(api_payload, dict) and _looks_like_authoring(api_payload):
            return dict(api_payload)
        if _looks_like_authoring(item):
            return dict(item)
    return _as_dict(value)


def extract_selected_flow_execution_payload(value: Any) -> dict[str, Any]:
    for item in _walk(value):
        item = _parse_json_dict(item) if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        api_payload = item.get("api_response")
        if isinstance(api_payload, dict):
            nested = extract_selected_flow_execution_payload(api_payload)
            if nested:
                return nested
        data_payload = item.get("data")
        if isinstance(data_payload, dict):
            nested = extract_selected_flow_execution_payload(data_payload)
            if nested:
                return nested
        if _looks_like_selected_flow_execution(item):
            return dict(item)
    return {}


def normalize_metadata_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"domain", "domains"}:
        return "domain"
    if text in {"table", "table_catalog", "catalog", "data_catalog"}:
        return "table_catalog"
    return "main_flow_filter"


def _apply_selected_flow_defaults(result: dict[str, Any], selected_flow: str) -> None:
    if selected_flow == "metadata_qa_flow" and result.get("message_only"):
        result["response_type"] = "metadata_qa"
        result["direct_response_ready"] = True


def _is_authoring_selected_flow(selected_flow: str) -> bool:
    return str(selected_flow or "") in {
        "domain_saving_flow",
        "table_catalog_saving_flow",
        "main_flow_filters_saving_flow",
        "domain_authoring_flow",
        "table_catalog_authoring_flow",
        "main_flow_filters_authoring_flow",
    }


def _is_direct_router_route(route: Any) -> bool:
    return str(route or "") in {"direct_answer", "clarification", "flow_error"}


def _router_execution_failed(value: dict[str, Any]) -> bool:
    api_payload = _as_dict(value.get("api_response"))
    if api_payload:
        return _router_execution_failed(api_payload)
    if str(value.get("status") or "").lower() == "error":
        return True
    trace = _as_dict(value.get("trace"))
    execution = _as_dict(trace.get("execution"))
    return bool(execution.get("errors"))


def normalize_duplicate_action(value: Any) -> str:
    text = str(value or "skip").strip().lower()
    return text if text in {"merge", "replace", "skip", "create_new"} else "skip"


def _query_data(payload: dict[str, Any]) -> dict[str, Any]:
    data_value = payload.get("data")
    if isinstance(data_value, dict):
        data_source = data_value
        rows = _row_list(data_source.get("rows"))
    else:
        data_source = {}
        rows = _row_list(data_value)
    final_data = _as_dict(payload.get("final_data"))
    analysis = _as_dict(payload.get("analysis") or payload.get("analysis_result"))
    analysis_rows = _row_list(analysis.get("rows")) or _row_list(analysis.get("data"))
    source_columns = _string_list(data_source.get("columns")) or _columns_from_rows(rows)
    analysis_columns = _string_list(analysis.get("columns")) or _columns_from_rows(analysis_rows)
    use_analysis = _should_prefer_analysis_data(rows, source_columns, analysis_rows, analysis_columns)
    if use_analysis:
        rows = analysis_rows
    if not rows:
        rows = _row_list(final_data.get("rows")) or analysis_rows
    columns = (
        (analysis_columns if use_analysis else [])
        or _string_list(data_source.get("columns"))
        or _string_list(payload.get("columns"))
        or _string_list(final_data.get("columns"))
        or _string_list(analysis.get("columns"))
        or _columns_from_rows(rows)
    )
    row_count = (
        _int_value(analysis.get("row_count"), len(rows))
        if use_analysis
        else _int_value(data_source.get("row_count"), _int_value(payload.get("row_count"), _int_value(final_data.get("row_count"), len(rows))))
    )
    data_ref = _normalize_data_ref(
        (analysis.get("data_ref") if use_analysis else None)
        or data_source.get("data_ref")
        or payload.get("data_ref")
        or final_data.get("data_ref")
        or analysis.get("data_ref")
    )
    result = {
        "columns": columns,
        "rows": rows,
        "row_count": row_count,
        "data_ref": data_ref,
    }
    for key in ("data_is_preview", "data_is_reference", "rows_are_preview", "data_ref_loaded", "data_ref_load_mode"):
        value = data_source.get(key) if isinstance(data_source, dict) else None
        if value in (None, "", [], {}):
            value = payload.get(key) or analysis.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    if "data_is_preview" not in result and row_count > len(rows):
        result["data_is_preview"] = True
    return result


def _should_prefer_analysis_data(
    source_rows: list[dict[str, Any]],
    source_columns: list[str],
    analysis_rows: list[dict[str, Any]],
    analysis_columns: list[str],
) -> bool:
    if not analysis_rows:
        return False
    if not source_rows:
        return True
    if analysis_columns and source_columns and analysis_columns != source_columns:
        return True
    return False


def _query_analysis(payload: dict[str, Any], data: dict[str, Any], developer: dict[str, Any] | None = None) -> dict[str, Any]:
    source = _as_dict(payload.get("analysis") or payload.get("analysis_result"))
    developer = _as_dict(developer) or _as_dict(payload.get("developer") or payload.get("debug"))
    pandas_code_json = _as_dict(source.get("pandas_code_json")) or _as_dict(developer.get("pandas_code_json"))
    analysis_code = source.get("analysis_code") or developer.get("analysis_code") or pandas_code_json.get("code") or payload.get("analysis_code")
    analysis_rows = _row_list(source.get("rows"))
    analysis_columns = _string_list(source.get("columns")) or _columns_from_rows(analysis_rows)
    analysis_row_count = None
    if source.get("row_count") not in (None, "", [], {}) or analysis_rows:
        analysis_row_count = _int_value(source.get("row_count"), len(analysis_rows))
    result = {
        "status": source.get("status") or payload.get("analysis_status") or developer.get("analysis_status"),
        "safety_passed": source.get("safety_passed"),
        "executed": source.get("executed"),
        "columns": analysis_columns,
        "rows": analysis_rows,
        "row_count": analysis_row_count,
        "analysis_code": analysis_code or "",
        "pandas_code_json": pandas_code_json,
        "reasoning_steps": _as_list(source.get("reasoning_steps") or developer.get("reasoning_steps")),
        "warnings": _as_list(source.get("warnings")),
        "errors": _as_list(source.get("errors")),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _query_developer(value: Any, preferred_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in (preferred_payload, value):
        for item in _walk(source):
            item = _parse_json_dict(item) if isinstance(item, str) else item
            if not isinstance(item, dict):
                continue
            for container_name in ("developer", "debug"):
                container = _as_dict(item.get(container_name))
                for key, found in container.items():
                    if _has_value(found) and not _has_value(result.get(key)):
                        result[key] = found
            for key in DEVELOPER_PAYLOAD_KEYS:
                found = item.get(key)
                if _has_value(found) and not _has_value(result.get(key)):
                    result[key] = found
    return result


def _query_pandas_developer(*sources: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source in sources:
        for item in _walk(source):
            item = _parse_json_dict(item) if isinstance(item, str) else item
            if not isinstance(item, dict):
                continue
            trace = _as_dict(item.get("trace"))
            inspection = _as_dict(trace.get("inspection"))
            pandas_trace = _as_dict(inspection.get("pandas_execution"))
            intent_trace = _as_dict(inspection.get("intent"))
            analysis = _as_dict(item.get("analysis") or item.get("analysis_result"))
            intent_plan = _as_dict(item.get("intent_plan") or item.get("intent"))
            pandas_plan = _as_list(intent_plan.get("pandas_execution_plan"))

            if pandas_plan:
                _set_missing(result, "analysis_plan", pandas_plan)
            if intent_trace.get("decision_reason"):
                _set_missing(result, "reasoning_steps", _as_list(intent_trace.get("decision_reason")))
            if pandas_trace:
                _set_missing(result, "pandas_execution_status", _pandas_execution_status_for_developer(pandas_trace, analysis))
                _set_missing(result, "filter_notes", pandas_trace.get("pandas_filter_plan"))
                _set_missing(result, "data_preparation_code", pandas_trace.get("pandas_filter_preamble") or analysis.get("pandas_filter_preamble"))
                _set_missing(
                    result,
                    "analysis_code",
                    pandas_trace.get("effective_code_with_helpers")
                    or analysis.get("effective_code_with_helpers")
                    or pandas_trace.get("generated_code")
                    or analysis.get("analysis_code"),
                )
                if pandas_trace.get("status") == "error":
                    _set_missing(result, "failed_analysis_code", pandas_trace.get("llm_generated_code") or analysis.get("llm_generated_code"))
                llm_code = pandas_trace.get("llm_generated_code") or analysis.get("llm_generated_code")
                if llm_code:
                    _set_missing(result, "pandas_code_json", {"code": llm_code})
            elif analysis:
                _set_missing(result, "analysis_status", analysis.get("status"))
                _set_missing(result, "analysis_code", analysis.get("analysis_code"))
                _set_missing(result, "data_preparation_code", analysis.get("pandas_filter_preamble"))
    return result


def _pandas_execution_status_for_developer(pandas_trace: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    excluded = {"generated_code", "llm_generated_code", "pandas_filter_preamble", "effective_code_with_helpers", "helper_sources"}
    status = {key: value for key, value in pandas_trace.items() if key not in excluded and _has_value(value)}
    if not status.get("execution_result") and analysis:
        status["execution_result"] = {
            "row_count": analysis.get("row_count"),
            "columns": analysis.get("columns"),
        }
    return {key: value for key, value in status.items() if _has_value(value)}


def _merge_missing(primary: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    for key, value in fallback.items():
        _set_missing(merged, key, value)
    return merged


def _set_missing(target: dict[str, Any], key: str, value: Any) -> None:
    if _has_value(value) and not _has_value(target.get(key)):
        target[key] = value


def _collect_data_refs(
    payload: dict[str, Any],
    data: dict[str, Any],
    developer: dict[str, Any] | None = None,
    source_payload: Any | None = None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for ref in _as_list(payload.get("data_refs")):
        _append_data_ref(refs, ref)
    _append_data_ref(refs, data.get("data_ref"))
    developer = _as_dict(developer) or _as_dict(payload.get("developer") or payload.get("debug"))
    for ref in _as_list(developer.get("data_refs")):
        _append_data_ref(refs, ref)
    answer_sections = _as_dict(payload.get("answer_sections"))
    for ref in _as_list(answer_sections.get("downloads")):
        _append_data_ref(refs, ref)

    if refs:
        return refs

    state = _as_dict(payload.get("state"))
    current_data = _as_dict(state.get("current_data"))
    _append_data_ref(refs, current_data.get("data_ref"))
    for source in _as_list(state.get("followup_source_results")):
        if isinstance(source, dict):
            _append_data_ref(refs, source.get("data_ref"))
    runtime_source_refs = _as_dict(state.get("runtime_source_refs"))
    for ref in runtime_source_refs.values():
        _append_data_ref(refs, ref)

    if refs:
        return refs

    for item in _walk(source_payload):
        item = _parse_json_dict(item) if isinstance(item, str) else item
        if not isinstance(item, dict):
            continue
        for ref in _as_list(item.get("data_refs")):
            _append_data_ref(refs, ref)
        item_data = _as_dict(item.get("data"))
        _append_data_ref(refs, item_data.get("data_ref"))
        item_answer_sections = _as_dict(item.get("answer_sections"))
        for ref in _as_list(item_answer_sections.get("downloads")):
            _append_data_ref(refs, ref)
        for container_name in ("developer", "debug"):
            container = _as_dict(item.get(container_name))
            for ref in _as_list(container.get("data_refs")):
                _append_data_ref(refs, ref)
    return refs


def _normalize_authoring_trace(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("trace")
    if isinstance(trace, dict):
        return dict(trace)
    stages = [dict(item) for item in trace if isinstance(item, dict)] if isinstance(trace, list) else []
    return {
        "raw_text": payload.get("raw_text") or payload.get("user_input") or _stage_text(stages, "input", "raw_text"),
        "duplicate_decision": _as_dict(payload.get("duplicate_decision")),
        "stages": stages,
    }


def _authoring_ui_status(
    payload: dict[str, Any],
    review: dict[str, Any],
    write_result: dict[str, Any],
    trace: dict[str, Any],
    existing_matches: list[Any],
    conflict_warnings: list[Any],
    errors: list[Any],
) -> str:
    status = str(write_result.get("status") or payload.get("status") or "").lower()
    duplicate_decision = _as_dict(trace.get("duplicate_decision"))
    supplement = _as_list(review.get("supplement_requests"))
    if errors or status == "error":
        return "error"
    if supplement or review.get("needs_supplement") or payload.get("needs_supplement"):
        return "needs_more_input"
    if status == "dry_run" or write_result.get("dry_run"):
        return "dry_run"
    saved_count = int(write_result.get("saved_count") or 0)
    skipped_count = int(write_result.get("skipped_count") or 0)
    if status == "skipped" or write_result.get("skipped") or (skipped_count > 0 and saved_count == 0):
        return "skipped"
    if status in {"ok", "saved"} or saved_count > 0:
        return "saved"
    if conflict_warnings:
        return "warning"
    return str(payload.get("status") or "processed")


def _authoring_message(ui_status: str, write_result: dict[str, Any], review: dict[str, Any]) -> str:
    if ui_status == "saved":
        return f"{int(write_result.get('saved_count') or 0)}개 metadata item을 저장했습니다."
    if ui_status == "needs_more_input":
        return "저장 전에 추가 정보가 필요합니다."
    if ui_status == "dry_run":
        return f"Dry Run 검토를 완료했습니다. 저장 예정 {int(write_result.get('would_save_count') or 0)}건입니다."
    if ui_status == "skipped":
        return f"기존 metadata를 유지하고 중복 쓰기 {int(write_result.get('skipped_count') or 0)}건을 건너뛰었습니다."
    if ui_status == "error":
        return "처리 중 오류가 발생했습니다."
    if review:
        return "검토 결과를 확인하세요."
    return "처리 결과를 확인하세요."


def _looks_like_query(value: dict[str, Any]) -> bool:
    if _looks_like_route_decision(value):
        return False
    if _looks_like_authoring(value):
        return False
    response_type = str(value.get("response_type") or "")
    data_value = value.get("data")
    if response_type in {"data_analysis", "analysis"} and isinstance(data_value, dict):
        return True
    return any(
        key in value
        for key in (
            "answer_message",
            "applied_scope",
            "intent_plan",
            "analysis",
            "data_ref",
            "data_refs",
            "direct_response_ready",
            "metadata_qa",
            "metadata_route",
        )
    ) or (
        "response" in value and any(key in value for key in ("data", "columns", "row_count"))
    )


def _looks_like_route_decision(value: dict[str, Any]) -> bool:
    if any(key in value for key in ("answer_message", "state", "analysis", "data_ref", "data_refs", "direct_response_ready")):
        return False
    data_value = value.get("data")
    if isinstance(data_value, dict) and any(key in data_value for key in ("rows", "columns", "row_count", "data_ref")):
        return False
    if isinstance(data_value, list):
        return False
    return bool(
        value.get("response_type") == "route_decision"
        or value.get("response_type") == "routed_flow_execution"
        or value.get("route_decision")
        or value.get("selected_flow")
        or (value.get("flow_inputs") and value.get("route"))
    )


def _looks_like_selected_flow_execution(value: dict[str, Any]) -> bool:
    return bool(
        value.get("response_type") == "routed_flow_execution"
        or (value.get("raw_response") and ("message" in value or "status" in value) and ("selected_flow" in value or "route" in value))
    )


def _route_from_selected_flow(selected_flow: str) -> str:
    return {
        "metadata_qa_flow": "metadata_qa",
        "data_analysis_flow": "data_analysis",
        "domain_saving_flow": "domain_saving",
        "domain_authoring_flow": "domain_saving",
        "table_catalog_saving_flow": "table_catalog_saving",
        "table_catalog_authoring_flow": "table_catalog_saving",
        "main_flow_filters_saving_flow": "main_flow_filter_saving",
        "main_flow_filters_authoring_flow": "main_flow_filter_saving",
    }.get(str(selected_flow or ""), "")


def _looks_like_authoring(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("metadata_type", "review", "review_result", "write_result", "existing_matches")) and any(
        key in value for key in ("items", "trace", "status", "message")
    )


def _walk(value: Any) -> Iterable[Any]:
    parsed = _parse_json_dict(value) if isinstance(value, str) else None
    if parsed:
        yield parsed
        for child in parsed.values():
            yield from _walk(child)
        yield value
        return
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _parse_json_dict(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    if text.lower().startswith("json"):
        candidates.append(text[4:].lstrip(" \t\r\n:"))
    if text.startswith("```"):
        lines = text.splitlines()
        body = "\n".join(lines[1:])
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3].strip()
        candidates.append(body)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _append_data_ref(refs: list[dict[str, Any]], ref: Any) -> None:
    normalized = _normalize_data_ref(ref)
    if not normalized:
        return
    signature = _data_ref_signature(normalized)
    for index, existing in enumerate(refs):
        if _data_ref_signature(existing) == signature:
            refs[index] = _merge_data_ref(existing, normalized)
            return
    refs.append(normalized)


def _normalize_data_ref(ref: Any) -> dict[str, Any]:
    if isinstance(ref, dict) and ref:
        normalized = dict(ref)
        if not str(normalized.get("ref_id") or "").strip() and str(normalized.get("data_ref") or "").strip():
            normalized["ref_id"] = str(normalized.get("data_ref") or "").strip()
        return normalized if _is_downloadable_data_ref(normalized) else {}
    return {}


def _is_downloadable_data_ref(ref: dict[str, Any]) -> bool:
    ref_id = str(ref.get("ref_id") or "").strip()
    if not ref_id:
        return False
    return any(str(ref.get(key) or "").strip() for key in ("path", "row_path", "role", "store", "collection_name", "collection", "database"))


def _data_ref_signature(ref: dict[str, Any]) -> str:
    ref_id = str(ref.get("ref_id") or "").strip()
    path = str(ref.get("path") or ref.get("row_path") or "").strip()
    role = str(ref.get("role") or "").strip()
    source_alias = str(ref.get("source_alias") or ref.get("dataset_key") or "").strip()
    if ref_id and path:
        return f"{ref_id}|{path}"
    if ref_id and role:
        return f"{ref_id}|{role}|{source_alias}"
    return ref_id


def _merge_data_ref(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if not _has_value(merged.get(key)) and _has_value(value):
            merged[key] = value
    return merged


def _stage_text(stages: list[dict[str, Any]], stage_name: str, key: str) -> str:
    for stage in stages:
        if stage.get("stage") == stage_name and isinstance(stage.get(key), str):
            return str(stage.get(key) or "")
    return ""


def _trace_values(trace: dict[str, Any], key: str) -> list[Any]:
    values: list[Any] = []
    for stage in _as_list(trace.get("stages")):
        if isinstance(stage, dict):
            values.extend(_as_list(stage.get(key)))
    return values


def _collection_name(metadata_type: str) -> str:
    kind = normalize_metadata_type(metadata_type)
    env_names = {
        "domain": ["MONGODB_DOMAIN_COLLECTION", "DOMAIN_COLLECTION_NAME"],
        "table_catalog": ["MONGODB_TABLE_CATALOG_COLLECTION", "TABLE_CATALOG_COLLECTION_NAME"],
        "main_flow_filter": ["MONGODB_MAIN_FLOW_FILTER_COLLECTION", "MAIN_FLOW_FILTER_COLLECTION_NAME"],
    }[kind]
    for name in env_names:
        value = _env(name)
        if value:
            return value
    return DEFAULT_COLLECTIONS[kind]


def _env(name: str, local_env: dict[str, str] | None = None) -> str:
    if name in os.environ:
        return str(os.getenv(name, "") or "").strip()
    values = local_env if local_env is not None else _load_local_env()
    return str(values.get(name, "") or "").strip()


def _load_local_env() -> dict[str, str]:
    cwd = Path.cwd().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [cwd / ".env"]
    if cwd == repo_root or repo_root in cwd.parents:
        candidates.append(repo_root / ".env")
    seen: set[Path] = set()
    values: dict[str, str] = {}
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        for line in resolved.read_text(encoding="utf-8").splitlines():
            key, value = _parse_env_line(line)
            if key and key not in values:
                values[key] = value
    return values


def _parse_env_line(line: str) -> tuple[str, str]:
    text = str(line or "").strip()
    if not text or text.startswith("#") or "=" not in text:
        return "", ""
    key, value = text.split("=", 1)
    key = key.strip()
    if not key or key.startswith("export "):
        key = key.removeprefix("export ").strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def _flow_run_url(base_url: str, flow_id: str) -> str:
    if not base_url or not flow_id:
        return ""
    return f"{base_url.rstrip('/')}/api/v1/run/{flow_id.strip()}"


def _int_env(name: str, default: int, local_env: dict[str, str] | None = None) -> int:
    try:
        return int(_env(name, local_env) or default)
    except Exception:
        return default


def _first_text(payload: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_text_anywhere(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    for attr in ("text", "content", "message"):
        text = getattr(value, attr, None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    if isinstance(value, dict):
        for key in ("answer_message", "message", "response", "answer", "text", "content", "output"):
            nested = value.get(key)
            text = _extract_text_anywhere(nested)
            if text:
                return text
        for key in ("api_response", "data", "result", "results", "outputs", "artifacts"):
            nested = value.get(key)
            text = _extract_text_anywhere(nested)
            if text:
                return text
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                text = _extract_text_anywhere(nested)
                if text:
                    return text
    if isinstance(value, list):
        for item in value:
            text = _extract_text_anywhere(item)
            if text:
                return text
    return ""


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _row_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if str(item or "").strip()]


def _columns_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    return columns


def _int_value(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value:
        return int(value)
    try:
        return int(str(value))
    except Exception:
        return fallback


def _unique_values(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    signatures: set[str] = set()
    for value in values:
        if value in (None, "", [], {}):
            continue
        try:
            signature = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            signature = str(value)
        if signature in signatures:
            continue
        signatures.add(signature)
        result.append(value)
    return result
