from __future__ import annotations

import base64
import gc
import html
import json
import sys
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

try:
    from .data_ref_store import DEFAULT_RESULT_COLLECTION, load_data_ref_rows
    from .langflow_client import LangflowApiClient, LangflowSettings
    from .metadata_store import collection_name_for, load_metadata_items, mark_metadata_deleted
    from .ui_helpers import chat_dataframe_height, compact_json_html, display_table_frame, format_answer_markdown_text, json_text, safe_markdown_text
except ImportError:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from web_app.data_ref_store import DEFAULT_RESULT_COLLECTION, load_data_ref_rows
    from web_app.langflow_client import LangflowApiClient, LangflowSettings
    from web_app.metadata_store import collection_name_for, load_metadata_items, mark_metadata_deleted
    from web_app.ui_helpers import chat_dataframe_height, compact_json_html, display_table_frame, format_answer_markdown_text, json_text, safe_markdown_text


APP_TITLE = "PTMORE PKG"
PAGE_CHAT = "Langflow 채팅"
PAGE_METADATA = "메타데이터 등록"
PAGE_LOOKUP = "조회/내보내기"
NAV_PAGES = [PAGE_CHAT, PAGE_METADATA, PAGE_LOOKUP]
NAV_PAGE_KEY = "registration_nav_page"
CHAT_RESET_QUERY_KEY = "chat_reset"
DEVELOPER_MODE_QUERY_KEY = "developer_mode"
DEVELOPER_MODE_SESSION_KEY = "langflow_developer_mode"
QUERY_TRUE_VALUES = {"1", "true", "yes", "y"}
CHAT_AVATARS = {
    "user": ":material/person:",
    "assistant": ":material/smart_toy:",
}


def collect_unused_memory() -> None:
    try:
        gc.collect()
    except Exception:
        pass

AUTHORING_TYPE_OPTIONS = {
    "domain": {
        "label": "Domain",
        "title": "Domain 지식 등록",
        "description": "업무 용어, 공정 그룹, 제품 조건, metric, join rule을 Langflow domain saving flow로 변환/검수/저장합니다.",
        "settings_attr": "domain_saving_api_url",
    },
    "table_catalog": {
        "label": "Data Catalog",
        "title": "Data Catalog 등록",
        "description": "dataset, source, tool, 컬럼, filter mapping 정보를 Langflow table catalog saving flow로 등록합니다.",
        "settings_attr": "table_catalog_saving_api_url",
    },
    "main_flow_filter": {
        "label": "Main Flow Filter",
        "title": "Main Flow Filter 등록",
        "description": "날짜, 공정, MODE, 제품 속성처럼 여러 dataset에서 공통으로 쓰는 표준 의미 필터를 등록합니다.",
        "settings_attr": "main_flow_filter_saving_api_url",
    },
}
AUTHORING_EXAMPLE_PATHS = {
    "domain": REPO_ROOT / "langflow_components" / "domain_saving_flow" / "raw_text_input_example.md",
    "table_catalog": REPO_ROOT / "langflow_components" / "table_catalog_saving_flow" / "raw_text_input_example.md",
    "main_flow_filter": REPO_ROOT / "langflow_components" / "main_flow_filters_saving_flow" / "raw_text_input_example.md",
}
AUTHORING_EXAMPLES = {
    "domain": "W/B공정은 W/B1부터 W/B6까지야. 재공 수량은 WIP 컬럼을 합산해.",
    "table_catalog": "wip_today는 Oracle PNT_RPT에서 SELECT WORK_DT, OPER_NAME, WIP FROM PKG_WIP_TODAY WHERE WORK_DT = {DATE}로 조회해. DATE는 WORK_DT에 매핑해.",
    "main_flow_filter": "날짜 조건은 DATE라는 기준 필터로 사용해줘. 오늘, 금일, 작업일은 WORK_DT 후보 컬럼과 연결해.",
}
DOMAIN_SECTION_DESCRIPTIONS = {
    "process_groups": ("공정 그룹", "사용자가 DA, WB, SG처럼 부르는 공정 묶음을 실제 공정명/OPER 값으로 연결합니다."),
    "product_terms": ("제품 조건", "POP, MOBILE, HBM 같은 제품군 표현을 실제 데이터 필터 조건으로 연결합니다."),
    "quantity_terms": ("수량 기준", "생산량, 재공, Lot 수 같은 업무 수량을 데이터 컬럼과 집계 방식으로 연결합니다."),
    "metric_terms": ("계산 지표", "달성률, 차이, 비율처럼 여러 수량을 조합해 계산하는 지표입니다."),
    "status_terms": ("상태 조건", "Hold, 작업대기 같은 상태 표현을 실제 상태 컬럼/값으로 연결합니다."),
    "analysis_recipes": ("분석 레시피", "여러 데이터셋 조회, rank, join, 집계처럼 단계가 필요한 질문의 처리 계획입니다."),
    "pandas_function_cases": ("Pandas 함수 케이스", "일반 도메인 조건만으로 어려운 질문에서 어떤 helper 함수를 써야 하는지 알려주는 선택 규칙입니다."),
    "product_key_columns": ("제품 키", "데이터셋 사이에서 같은 제품을 식별하고 join할 때 쓰는 기준 컬럼 목록입니다."),
}
CHAT_RESULT_TABLE_MAX_HEIGHT = 640
DATA_REF_TABLE_MAX_HEIGHT = 620
def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    inject_style()
    download_ref = download_ref_from_query()
    if download_ref:
        render_data_ref_download_page(download_ref)
        return
    ensure_state()
    settings = settings_sidebar()
    page = settings["page"]
    if page == PAGE_CHAT:
        render_langflow_chat(settings)
    elif page == PAGE_METADATA:
        render_metadata_registration(settings)
    else:
        render_lookup(settings)


def ensure_state() -> None:
    api_settings = LangflowSettings.from_env()
    if "langflow_api" not in st.session_state or getattr(st.session_state.langflow_api, "settings", None) != api_settings:
        st.session_state.langflow_api = LangflowApiClient(api_settings)
    if "session_id" not in st.session_state:
        st.session_state.session_id = new_session_id()
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []
    if "latest_state" not in st.session_state:
        st.session_state.latest_state = {}
    if DEVELOPER_MODE_SESSION_KEY not in st.session_state:
        st.session_state[DEVELOPER_MODE_SESSION_KEY] = query_flag_enabled(DEVELOPER_MODE_QUERY_KEY)


def new_session_id() -> str:
    return f"web-{uuid.uuid4().hex[:8]}"


def reset_langflow_chat_state() -> None:
    st.session_state.session_id = new_session_id()
    st.session_state.chat_messages = []
    st.session_state.latest_state = {}


def query_flag_enabled(key: str) -> bool:
    try:
        raw_value = st.query_params.get(key)
    except Exception:
        return False
    if isinstance(raw_value, list):
        raw_value = raw_value[0] if raw_value else ""
    return str(raw_value or "").strip().lower() in QUERY_TRUE_VALUES


def query_param_value(key: str) -> str:
    try:
        raw_value = st.query_params.get(key)
    except Exception:
        return ""
    if isinstance(raw_value, list):
        raw_value = raw_value[0] if raw_value else ""
    return str(raw_value or "").strip()


def download_ref_from_query() -> dict[str, Any]:
    token = query_param_value("download_ref")
    if not token:
        return {}
    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parsed = json.loads(decoded)
    except Exception:
        return {"_decode_error": "download_ref 토큰을 해석하지 못했습니다."}
    return parsed if isinstance(parsed, dict) else {"_decode_error": "download_ref 토큰이 올바른 객체가 아닙니다."}


def sync_query_flag(key: str, enabled: bool) -> None:
    try:
        current_enabled = query_flag_enabled(key)
        if enabled and not current_enabled:
            st.query_params[key] = "1"
        elif not enabled and current_enabled:
            del st.query_params[key]
    except Exception:
        pass


def consume_chat_reset_query() -> bool:
    if not query_flag_enabled(CHAT_RESET_QUERY_KEY):
        return False
    reset_langflow_chat_state()
    try:
        del st.query_params[CHAT_RESET_QUERY_KEY]
    except Exception:
        pass
    return True


def config_value(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "(missing)"


def config_status_label(value: Any) -> str:
    return "설정됨" if str(value or "").strip() else "미설정"


def config_status_class(value: Any) -> str:
    return "ok" if str(value or "").strip() else "missing"


def sidebar_config_rows(rows: list[dict[str, Any]]) -> None:
    row_html = []
    for row in rows:
        label = html.escape(str(row.get("label") or ""))
        env_name = html.escape(str(row.get("env") or ""))
        value = html.escape(str(row.get("value") or ""))
        status = row.get("status")
        if status is not None:
            status_label = html.escape(config_status_label(status))
            status_class = config_status_class(status)
            value_markup = f'<span class="config-badge {status_class}">{status_label}</span>'
        else:
            value_markup = f'<span class="config-value">{value}</span>'
        row_html.append(
            '<div class="config-row">'
            '<div class="config-meta">'
            f'<div class="config-label">{label}</div>'
            f'<div class="config-env">{env_name}</div>'
            "</div>"
            f'<div class="config-data">{value_markup}</div>'
            "</div>"
        )
    st.markdown('<div class="config-list">' + "".join(row_html) + "</div>", unsafe_allow_html=True)


def chat_avatar_for(role: Any) -> str:
    return CHAT_AVATARS["user"] if str(role or "").lower() in {"user", "human"} else CHAT_AVATARS["assistant"]


def session_badge_html(session_id: Any) -> str:
    safe_session_id = html.escape(str(session_id or ""))
    return (
        '<div class="session-strip">'
        '<div class="session-strip-label">Session ID</div>'
        f'<div class="session-strip-value">{safe_session_id}</div>'
        "</div>"
    )


def chat_topbar_html(session_id: Any, developer_mode: bool = False) -> str:
    reset_query = f"{CHAT_RESET_QUERY_KEY}=1"
    if developer_mode:
        reset_query = f"{reset_query}&{DEVELOPER_MODE_QUERY_KEY}=1"
    reset_href = html.escape(f"?{reset_query}", quote=True)
    return (
        '<div class="chat-topbar">'
        '<div class="chat-topbar-title">PTMORE PKG AGENT</div>'
        f"{session_badge_html(session_id)}"
        f'<a class="chat-topbar-reset" href="{reset_href}" target="_self">대화 초기화</a>'
        "</div>"
    )


def chat_topbar_spacer_html() -> str:
    return '<div class="chat-topbar-spacer"></div>'


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def latest_chat_applied_scope() -> dict[str, Any]:
    messages = st.session_state.get("chat_messages")
    if not isinstance(messages, list):
        return {}
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        result = message.get("result") if isinstance(message.get("result"), dict) else {}
        scope = result.get("applied_scope") if isinstance(result.get("applied_scope"), dict) else {}
        if scope:
            return scope
    return {}


def state_summary_for_sidebar(state: dict[str, Any] | None = None, applied_scope: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if isinstance(state, dict) else st.session_state.get("latest_state", {})
    applied_scope = applied_scope if isinstance(applied_scope, dict) else latest_chat_applied_scope()
    current_data = state.get("current_data") if isinstance(state.get("current_data"), dict) else {}
    datasets = current_data.get("source_dataset_keys") or applied_scope.get("datasets") or []
    if not isinstance(datasets, list):
        datasets = [datasets]
    source_aliases = current_data.get("source_aliases") or applied_scope.get("source_aliases") or []
    if not isinstance(source_aliases, list):
        source_aliases = [source_aliases]
    columns = current_data.get("columns") if isinstance(current_data.get("columns"), list) else []
    preview_rows = current_data.get("preview_rows") or current_data.get("rows") or []
    if not isinstance(preview_rows, list):
        preview_rows = []
    product_summary = current_data.get("product_key_summary") if isinstance(current_data.get("product_key_summary"), dict) else {}
    row_count_value = int_or_zero(current_data.get("row_count")) or len(preview_rows)
    return {
        "datasets": [str(item) for item in datasets if str(item or "").strip()],
        "source_aliases": [str(item) for item in source_aliases if str(item or "").strip()],
        "row_count": row_count_value,
        "preview_count": len(preview_rows),
        "columns": [str(column) for column in columns[:8]],
        "product_key_count": int_or_zero(product_summary.get("count") or product_summary.get("product_count")),
        "has_state": bool(current_data or applied_scope),
    }


def active_scope_sidebar_html(state: dict[str, Any] | None = None, applied_scope: dict[str, Any] | None = None) -> str:
    summary = state_summary_for_sidebar(state, applied_scope)
    state_label = "활성" if summary["has_state"] else "대기"
    panel_class = "active-scope-panel" if summary["has_state"] else "active-scope-panel empty"
    if summary["has_state"]:
        dataset_text = ", ".join(summary["datasets"][:3]) or "dataset 정보 없음"
        if len(summary["datasets"]) > 3:
            dataset_text += f" 외 {len(summary['datasets']) - 3}개"
        alias_text = ", ".join(summary["source_aliases"][:3])
        columns_text = ", ".join(summary["columns"][:6])
        body_html = (
            f'<div class="active-scope-datasets">{html.escape(dataset_text)}</div>'
            '<div class="active-scope-chip-list">'
            f'<div class="active-scope-chip"><div class="active-scope-chip-label">Rows</div><div class="active-scope-chip-value">{summary["row_count"]:,}</div></div>'
            f'<div class="active-scope-chip"><div class="active-scope-chip-label">Preview</div><div class="active-scope-chip-value">{summary["preview_count"]:,}</div></div>'
            f'<div class="active-scope-chip"><div class="active-scope-chip-label">Product keys</div><div class="active-scope-chip-value">{summary["product_key_count"]:,}</div></div>'
            "</div>"
        )
        if alias_text:
            body_html += f'<div class="active-scope-footer">Aliases: {html.escape(alias_text)}</div>'
        if columns_text:
            body_html += f'<div class="active-scope-footer">Columns: {html.escape(columns_text)}</div>'
    else:
        body_html = '<div class="active-scope-empty-text">첫 질문이 실행되면 다음 질문에 이어질 기준일, 공정, 제품 조건이 여기에 표시됩니다.</div>'
    return (
        f'<div class="{panel_class}">'
        '<div class="active-scope-header">'
        '<div><div class="active-scope-kicker">Follow-up scope</div><div class="active-scope-title">후속 질문 기준</div></div>'
        f'<span class="active-scope-state">{html.escape(state_label)}</span>'
        "</div>"
        f"{body_html}"
        "</div>"
    )


def render_sidebar_active_scope(slot: Any | None = None, state: dict[str, Any] | None = None, applied_scope: dict[str, Any] | None = None) -> None:
    target = slot if slot is not None else st.sidebar
    target.markdown(active_scope_sidebar_html(state, applied_scope), unsafe_allow_html=True)


def settings_sidebar() -> dict[str, Any]:
    api_settings = LangflowSettings.from_env()
    api_client = st.session_state.get("langflow_api")
    if getattr(api_client, "settings", None) != api_settings:
        st.session_state.langflow_api = LangflowApiClient(api_settings)
    configured = api_settings.configured_summary()
    st.sidebar.markdown(
        """
        <div class="sidebar-brand">
          <div class="sidebar-brand-row">
            <div class="sidebar-brand-mark">PT</div>
            <div>
              <div class="sidebar-brand-title">PTMORE PKG</div>
              <div class="sidebar-brand-subtitle">Manufacturing analysis console</div>
            </div>
          </div>
        </div>
        <div class="sidebar-section-label">Navigation</div>
        """,
        unsafe_allow_html=True,
    )
    if st.session_state.get(NAV_PAGE_KEY) not in NAV_PAGES:
        st.session_state[NAV_PAGE_KEY] = PAGE_CHAT
    page = st.sidebar.radio("Menu", NAV_PAGES, key=NAV_PAGE_KEY, label_visibility="collapsed")
    active_scope_slot = st.sidebar.empty()
    if page == PAGE_CHAT:
        render_sidebar_active_scope(active_scope_slot)

    with st.sidebar.expander("MongoDB 설정", expanded=False):
        st.markdown(
            '<div class="small-note">MongoDB 접속 정보는 환경변수에서만 읽습니다. 웹 화면에는 URI나 계정 정보를 입력하지 않습니다.</div>',
            unsafe_allow_html=True,
        )
        sidebar_config_rows(
            [
                {"label": "연결 URI", "env": "MONGODB_URI / MONGO_URI", "status": api_settings.mongo_uri},
                {"label": "데이터베이스", "env": "MONGODB_DATABASE / MONGO_DB_NAME", "value": config_value(api_settings.mongo_database)},
                {"label": "세션 상태 컬렉션", "env": "MONGODB_SESSION_STATE_COLLECTION", "value": config_value(api_settings.session_state_collection)},
            ]
        )

    with st.sidebar.expander("Langflow API 설정", expanded=False):
        st.markdown(
            '<div class="small-note">채팅 화면은 router flow가 있으면 router를 호출하고, v5 standalone 환경에서는 data analysis flow를 직접 호출합니다.</div>',
            unsafe_allow_html=True,
        )
        sidebar_config_rows(
            [
                {"label": "Router Flow", "env": "LANGFLOW_ROUTER_API_URL / LANGFLOW_ROUTER_FLOW_ID", "status": api_settings.router_api_url},
                {"label": "Data Analysis Flow", "env": "LANGFLOW_DATA_ANALYSIS_API_URL / LANGFLOW_DATA_ANALYSIS_FLOW_ID", "status": api_settings.data_analysis_api_url},
                {"label": "API Key", "env": "LANGFLOW_API_KEY", "status": api_settings.api_key},
                {"label": "Input Type", "env": "LANGFLOW_INPUT_TYPE", "value": config_value(api_settings.input_type)},
                {"label": "Output Type", "env": "LANGFLOW_OUTPUT_TYPE", "value": config_value(api_settings.output_type)},
            ]
        )

    with st.sidebar.expander("Saving Flow API 설정", expanded=False):
        st.markdown(
            '<div class="small-note">메타데이터 등록 화면은 각 Langflow saving flow의 Run API를 호출합니다.</div>',
            unsafe_allow_html=True,
        )
        sidebar_config_rows(
            [
                {"label": "Domain Flow", "env": "LANGFLOW_DOMAIN_SAVING_API_URL / LANGFLOW_DOMAIN_SAVING_FLOW_ID", "status": api_settings.domain_saving_api_url},
                {"label": "Data Catalog Flow", "env": "LANGFLOW_TABLE_CATALOG_SAVING_API_URL / LANGFLOW_TABLE_CATALOG_SAVING_FLOW_ID", "status": api_settings.table_catalog_saving_api_url},
                {"label": "Main Filter Flow", "env": "LANGFLOW_MAIN_FILTER_SAVING_API_URL / LANGFLOW_MAIN_FILTER_SAVING_FLOW_ID", "status": api_settings.main_flow_filter_saving_api_url},
            ]
        )

    developer_mode = st.sidebar.toggle(
        "개발자 모드",
        key=DEVELOPER_MODE_SESSION_KEY,
        help="Raw response, 적용 scope, pandas 전처리/분석 코드를 표시합니다.",
    )
    sync_query_flag(DEVELOPER_MODE_QUERY_KEY, developer_mode)
    number_format_label = st.sidebar.selectbox(
        "표 수량 표시",
        ["자동(K 기준)", "전체 숫자"],
        index=0,
        key="table_number_format_label",
    )
    st.sidebar.markdown(
        """
        <div class="small-note">
        채팅과 메타데이터 등록은 Langflow Run API를 통해 실행됩니다.
        조회/내보내기는 MongoDB metadata 컬렉션 기준으로 표시됩니다.
        </div>
        """,
        unsafe_allow_html=True,
    )
    return {
        "page": page,
        "developer_mode": developer_mode,
        "number_mode": "auto_k" if number_format_label == "자동(K 기준)" else "comma",
        "api_ready": configured["query"],
        "api_settings": api_settings,
        "active_scope_slot": active_scope_slot,
    }


def render_langflow_chat(settings: dict[str, Any]) -> None:
    if consume_chat_reset_query():
        st.rerun()
    st.markdown(chat_topbar_html(st.session_state.session_id, bool(settings.get("developer_mode"))), unsafe_allow_html=True)
    st.markdown(chat_topbar_spacer_html(), unsafe_allow_html=True)
    st.caption("Langflow run API를 호출해 현재 세션 ID로 대화를 이어가고, 표 형태 결과는 화면과 다운로드로 확인합니다.")
    if not settings.get("api_ready"):
        render_inline_status("", "LANGFLOW_ROUTER_API_URL 또는 LANGFLOW_DATA_ANALYSIS_API_URL 환경변수를 설정하면 채팅 화면을 사용할 수 있습니다.", tone="warning")
        return

    for index, message in enumerate(st.session_state.chat_messages):
        role = message.get("role", "assistant")
        with st.chat_message(role, avatar=chat_avatar_for(role)):
            if role == "assistant":
                render_assistant_chat_message(message, index, settings)
            else:
                st.markdown(safe_markdown_text(message.get("content") or ""))

    user_message = st.chat_input("질문을 입력하세요")
    if not user_message:
        return

    st.session_state.chat_messages.append({"role": "user", "content": user_message})
    with st.chat_message("user", avatar=chat_avatar_for("user")):
        st.markdown(safe_markdown_text(user_message))
    with st.chat_message("assistant", avatar=chat_avatar_for("assistant")):
        with render_loading_indicator():
            try:
                result = st.session_state.langflow_api.run_query(
                    user_message,
                    session_id=st.session_state.session_id,
                    state=st.session_state.latest_state or None,
                )
                st.session_state.latest_state = result.get("state", {})
                render_sidebar_active_scope(settings.get("active_scope_slot"), st.session_state.latest_state, result.get("applied_scope"))
                assistant_message = {"role": "assistant", "content": result.get("answer_message", ""), "result": result}
            except Exception as exc:
                assistant_message = {
                    "role": "assistant",
                    "content": f"Langflow API 호출 중 오류가 발생했습니다: {exc}",
                    "result": {"status": "error", "answer_message": f"Langflow API 호출 중 오류가 발생했습니다: {exc}", "errors": [str(exc)]},
                }
        render_assistant_chat_message(assistant_message, len(st.session_state.chat_messages), settings)
    st.session_state.chat_messages.append(assistant_message)
    collect_unused_memory()


@contextmanager
def render_loading_indicator():
    placeholder = st.empty()
    placeholder.markdown('<div class="mdv5-inline-loader" aria-label="처리 중"></div>', unsafe_allow_html=True)
    try:
        yield
    finally:
        placeholder.empty()


def render_data_ref_download_page(ref: dict[str, Any]) -> None:
    st.markdown(
        """
        <div class="chat-topbar">
          <div class="chat-topbar-title">PTMORE PKG DATA DOWNLOAD</div>
        </div>
        <div class="chat-topbar-spacer"></div>
        """,
        unsafe_allow_html=True,
    )
    if ref.get("_decode_error"):
        render_inline_status("다운로드 링크 오류", ref["_decode_error"], tone="error")
        st.markdown("[채팅 화면으로 돌아가기](./)")
        return

    label = data_ref_display_label(ref, 1)
    st.markdown(f"### {safe_markdown_text(label)}")
    render_summary_lines(data_ref_summary_lines(ref))
    with st.expander("data_ref JSON 보기", expanded=False):
        render_compact_json(ref, max_height=320)

    api_settings = LangflowSettings.from_env()
    loaded = load_web_data_ref_rows(ref, {"api_settings": api_settings, "number_mode": "auto_k"})
    rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
    if not loaded.get("ok") or not rows:
        render_inline_status("조회 불가", loaded.get("message") or "저장된 row가 없습니다.", tone="warning")
        st.markdown("[채팅 화면으로 돌아가기](./)")
        del rows, loaded
        collect_unused_memory()
        return

    frame = dataframe_with_columns(rows, loaded.get("columns"))
    row_count = int_or_zero(loaded.get("row_count")) or len(frame)
    st.caption(f"데이터 {row_count:,}행 · {len(frame.columns):,}컬럼")
    st.dataframe(
        display_table_frame(frame, "auto_k"),
        hide_index=True,
        width="stretch",
        height=chat_dataframe_height(len(frame), DATA_REF_TABLE_MAX_HEIGHT),
    )
    csv_data = dataframe_csv_bytes(frame)
    st.download_button(
        "CSV 다운로드",
        data=csv_data,
        file_name=data_ref_download_name(ref, "csv"),
        mime="text/csv",
        key="download_ref_csv",
        width="stretch",
    )
    st.download_button(
        "data_ref JSON 다운로드",
        data=json_text(ref),
        file_name=data_ref_download_name(ref, "json"),
        mime="application/json",
        key="download_ref_json",
        width="stretch",
    )
    del csv_data, frame, rows, loaded
    collect_unused_memory()
    st.markdown("[채팅 화면으로 돌아가기](./)")


def render_assistant_chat_message(message: dict[str, Any], message_index: int, settings: dict[str, Any]) -> None:
    result = message.get("result") if isinstance(message.get("result"), dict) else {}
    answer_text = result.get("display_message") or result.get("answer_message") or message.get("content") or "응답 메시지가 없습니다."
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    columns = data.get("columns") if isinstance(data.get("columns"), list) else []
    row_count = int_or_zero(data.get("row_count")) or len(rows)
    has_structured_data = bool(rows or data.get("data_ref"))
    display_answer_text = strip_result_table_section(answer_text) if has_structured_data else answer_text
    st.markdown(format_answer_markdown_text(display_answer_text))
    if result.get("message_only"):
        if settings.get("developer_mode"):
            render_chat_developer_details(result, message_index, settings)
        del data, rows, columns, answer_text, display_answer_text
        collect_unused_memory()
        return

    loaded = load_result_rows_for_display(data, settings)
    if loaded.get("ok") and loaded.get("rows"):
        rows = loaded["rows"]
        columns = loaded.get("columns") or columns
        row_count = int_or_zero(loaded.get("row_count")) or len(rows)
    if rows:
        frame = pd.DataFrame(rows)
        if columns:
            ordered = [column for column in columns if column in frame.columns]
            frame = frame[ordered + [column for column in frame.columns if column not in ordered]]
        table_options = result_table_display_options(result)
        caption = f"결과 {row_count:,}행 · {len(frame.columns):,}열"
        if loaded.get("loaded_from_ref"):
            caption += " · data_ref에서 전체 행을 불러왔습니다."
        elif result_rows_are_preview(data):
            caption += f" · 화면 표시 {len(frame):,}행"
        st.caption(caption)
        st.dataframe(
            display_table_frame(frame, settings.get("number_mode", "comma"), **table_options),
            hide_index=True,
            width="stretch",
            height=chat_dataframe_height(len(frame), CHAT_RESULT_TABLE_MAX_HEIGHT),
        )
        csv_data = dataframe_csv_bytes(frame)
        st.download_button(
            "결과 데이터 CSV 다운로드",
            data=csv_data,
            file_name=f"langflow_result_{message_index}.csv",
            mime="text/csv",
            key=f"chat_{message_index}_result_csv",
            width="stretch",
        )
        del csv_data, frame
    elif loaded.get("message") and result_rows_are_preview(data):
        render_inline_status("", f"전체 결과 data_ref를 불러오지 못했습니다: {loaded['message']}", tone="warning")

    render_chat_metadata(result)
    if settings.get("developer_mode"):
        render_chat_developer_details(result, message_index, settings)
    del data, rows, columns, loaded, answer_text, display_answer_text
    collect_unused_memory()


def strip_result_table_section(text: Any) -> str:
    value = str(text or "")
    if "### 결과 테이블" not in value:
        return value
    kept: list[str] = []
    skipping = False
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("### 결과 테이블"):
            skipping = True
            continue
        if skipping and stripped.startswith("### "):
            skipping = False
        if not skipping:
            kept.append(line)
    stripped_value = "\n".join(kept).strip()
    return stripped_value or value


def result_table_display_options(result: dict[str, Any]) -> dict[str, Any]:
    answer_sections = result.get("answer_sections") if isinstance(result.get("answer_sections"), dict) else {}
    result_table = answer_sections.get("result_table") if isinstance(answer_sections.get("result_table"), dict) else {}
    options: dict[str, Any] = {}
    column_labels = result_table.get("column_labels")
    display_columns = result_table.get("display_columns")
    if isinstance(column_labels, dict):
        options["column_labels"] = column_labels
    if isinstance(display_columns, list):
        options["display_columns"] = display_columns
    return options


def render_chat_metadata(result: dict[str, Any]) -> None:
    scope = result.get("applied_scope") if isinstance(result.get("applied_scope"), dict) else {}
    intent = result.get("intent_plan") or result.get("intent") or {}
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    if scope:
        with st.expander("적용 조건 / 도메인 정보", expanded=False):
            render_summary_lines(applied_scope_summary_lines(scope))
            with st.expander("원본 JSON 보기", expanded=False):
                render_compact_json(scope, max_height=360)
    if intent:
        with st.expander("의도 분석 / 실행 계획", expanded=False):
            render_summary_lines(intent_plan_summary_lines(intent))
            with st.expander("원본 JSON 보기", expanded=False):
                render_compact_json(intent, max_height=420)
    code = analysis.get("analysis_code") or (analysis.get("pandas_code_json") or {}).get("code") if isinstance(analysis.get("pandas_code_json"), dict) else analysis.get("analysis_code")
    if code or analysis.get("errors"):
        with st.expander("Pandas 처리", expanded=False):
            render_summary_lines(pandas_analysis_summary_lines(analysis))
            if code:
                render_detail_title("실행 코드")
                st.code(str(code), language="python")
            compact = {key: value for key, value in analysis.items() if key not in {"rows", "analysis_code", "pandas_code_json"}}
            if compact:
                with st.expander("원본 JSON 보기", expanded=False):
                    render_compact_json(compact, max_height=320)


def render_chat_developer_details(result: dict[str, Any], message_index: int, settings: dict[str, Any] | None = None) -> None:
    with st.expander("개발자 정보", expanded=False):
        tabs = st.tabs(["MongoDB / data_ref", "Pandas 진단", "Raw response"])
        with tabs[0]:
            render_developer_data_refs(result, message_index, settings or {})
        with tabs[1]:
            render_developer_pandas_info(result, settings)
        with tabs[2]:
            raw_payload = result.get("raw_response") or result
            render_compact_json(raw_payload, max_height=560)
            st.download_button(
                "Raw JSON 다운로드",
                data=json_text(raw_payload),
                file_name=f"langflow_response_{message_index}.json",
                mime="application/json",
                key=f"raw_download_{message_index}",
                width="stretch",
            )


def data_ref_download_name(ref: dict[str, Any], suffix: str) -> str:
    ref_id = str(ref.get("ref_id") or "data_ref").strip() or "data_ref"
    safe_ref_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in ref_id)
    return f"{safe_ref_id}.{suffix}"


def data_ref_display_label(ref: dict[str, Any], index: int) -> str:
    direct_label = str(ref.get("label") or "").strip()
    if direct_label:
        return direct_label
    source_label = str(ref.get("source_label") or "").strip()
    dataset_key = str(ref.get("dataset_key") or ref.get("source_alias") or ref.get("job_key") or "").strip()
    dataset_label = str(ref.get("dataset_label") or "").strip()
    tool_name = str(ref.get("tool_name") or "").strip()
    path = str(ref.get("path") or "").strip()
    ref_id = str(ref.get("ref_id") or "").strip()
    if source_label:
        return source_label
    label = dataset_label or dataset_key
    if label and tool_name:
        return f"{label} ({tool_name})"
    if label:
        return label
    if path:
        return path
    return ref_id or f"data_ref_{index}"


def dataframe_csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")


def dataframe_with_columns(rows: list[dict[str, Any]], columns: Any = None) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    ordered_columns = [str(column) for column in columns if str(column) in frame.columns] if isinstance(columns, list) else []
    if ordered_columns:
        frame = frame[ordered_columns + [column for column in frame.columns if column not in ordered_columns]]
    return frame


def result_rows_are_preview(data: dict[str, Any]) -> bool:
    return bool(data.get("data_is_preview") or data.get("rows_are_preview") or data.get("data_is_reference"))


def load_result_rows_for_display(data: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    data_ref = data.get("data_ref") if isinstance(data.get("data_ref"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    row_count = int_or_zero(data.get("row_count")) or len(rows)
    if not data_ref or (rows and not result_rows_are_preview(data) and row_count <= len(rows)):
        return {"ok": False, "rows": [], "message": ""}
    loaded = load_web_data_ref_rows(data_ref, settings)
    if loaded.get("ok") and loaded.get("rows"):
        loaded["loaded_from_ref"] = True
    return loaded


def load_web_data_ref_rows(ref: dict[str, Any], settings: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    if not isinstance(ref, dict) or not str(ref.get("ref_id") or "").strip():
        return {"ok": False, "rows": [], "columns": [], "row_count": 0, "message": "data_ref 정보가 비어 있습니다."}

    api_settings = settings.get("api_settings")
    mongo_uri = str(getattr(api_settings, "mongo_uri", "") or "").strip()
    mongo_database = str(getattr(api_settings, "mongo_database", "") or "").strip() or "datagov"
    if not mongo_uri:
        return {"ok": False, "rows": [], "columns": [], "row_count": 0, "message": "MONGODB_URI가 설정되어 있지 않습니다."}
    try:
        return load_data_ref_rows(
            ref,
            mongo_uri=mongo_uri,
            default_database=mongo_database,
            default_collection=DEFAULT_RESULT_COLLECTION,
            limit=limit,
        )
    except Exception as exc:
        return {"ok": False, "rows": [], "columns": [], "row_count": 0, "message": str(exc)}


def rows_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            text = str(key)
            if text not in columns:
                columns.append(text)
    return columns


def render_developer_data_refs(result: dict[str, Any], message_index: int, settings: dict[str, Any]) -> None:
    data_refs = result.get("data_refs") if isinstance(result.get("data_refs"), list) else []
    if not data_refs:
        render_inline_status("", "Langflow 응답에 MongoDB data_ref가 포함되어 있지 않습니다.")
        return
    for index, ref in enumerate(data_refs, start=1):
        if not isinstance(ref, dict):
            continue
        label = data_ref_display_label(ref, index)
        with st.expander(f"data_ref {index}: {label}", expanded=False):
            render_summary_lines(data_ref_summary_lines(ref))
            with st.expander("원본 JSON 보기", expanded=False):
                render_compact_json(ref, max_height=320)
            st.download_button(
                "data_ref 메타데이터 다운로드",
                data=json_text(ref),
                file_name=data_ref_download_name(ref, "json"),
                mime="application/json",
                key=f"chat_{message_index}_ref_meta_{index}",
                width="stretch",
            )
            loaded = load_web_data_ref_rows(ref, settings)
            rows = loaded.get("rows") if isinstance(loaded.get("rows"), list) else []
            if not rows:
                render_inline_status("", loaded.get("message") or "저장된 원본 row가 없습니다.", tone="warning" if loaded.get("message") else "info")
                del rows, loaded
                collect_unused_memory()
                continue
            frame = dataframe_with_columns(rows, loaded.get("columns"))
            st.caption(f"원본 데이터 {int_or_zero(loaded.get('row_count')) or len(frame):,}행 · {len(frame.columns):,}열")
            st.dataframe(
                display_table_frame(frame, settings.get("number_mode", "comma")),
                hide_index=True,
                width="stretch",
                height=chat_dataframe_height(len(frame), DATA_REF_TABLE_MAX_HEIGHT),
            )
            csv_data = dataframe_csv_bytes(frame)
            st.download_button(
                "원본 데이터 CSV 다운로드",
                data=csv_data,
                file_name=data_ref_download_name(ref, "csv"),
                mime="text/csv",
                key=f"chat_{message_index}_ref_rows_{index}",
                width="stretch",
            )
            del csv_data, frame, rows, loaded
            collect_unused_memory()


def render_developer_pandas_info(result: dict[str, Any], settings: dict[str, Any] | None = None) -> None:
    developer = result.get("developer") if isinstance(result.get("developer"), dict) else {}
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    if not developer and not analysis:
        render_inline_status("", "개발자용 pandas 진단 정보가 응답에 포함되어 있지 않습니다.")
        return

    status = developer.get("pandas_execution_status") or developer.get("analysis_status") or analysis.get("status")
    if status:
        render_detail_title("Pandas 실행 상태")
        render_summary_lines(status_summary_lines(status))
        with st.expander("상태 원본 JSON 보기", expanded=False):
            render_compact_json(status, max_height=260)

    for key, title in (
        ("source_summaries", "Source 요약"),
        ("filter_notes", "Filter notes"),
        ("merge_notes", "Merge notes"),
        ("analysis_plan", "Analysis plan"),
        ("reasoning_steps", "Reasoning steps"),
    ):
        value = developer.get(key) or analysis.get(key)
        if value not in (None, "", [], {}):
            render_detail_title(title)
            render_summary_lines(generic_summary_lines(value))
            with st.expander(f"{title} 원본 JSON 보기", expanded=False):
                render_compact_json(value, max_height=300)

    prepared = developer.get("prepared_dataframe") if isinstance(developer.get("prepared_dataframe"), dict) else {}
    if prepared:
        render_detail_title("준비된 DataFrame")
        render_summary_lines(prepared_dataframe_summary_lines(prepared))
        with st.expander("DataFrame 메타데이터 원본 JSON 보기", expanded=False):
            render_compact_json({key: value for key, value in prepared.items() if key != "preview_rows"}, max_height=260)
        preview_rows = prepared.get("preview_rows") if isinstance(prepared.get("preview_rows"), list) else []
        if preview_rows:
            frame = pd.DataFrame(preview_rows)
            st.dataframe(
                display_table_frame(frame, (settings or {}).get("number_mode", "comma")),
                hide_index=True,
                height=chat_dataframe_height(len(frame), 260),
                width="stretch",
            )

    for key, title in (
        ("data_preparation_code", "Pandas 전처리 코드"),
        ("failed_analysis_code", "오류가 발생한 LLM 분석 코드"),
        ("analysis_code", "실행된 최종 분석 코드"),
    ):
        code = str(developer.get(key) or analysis.get(key) or "").strip()
        if code:
            render_detail_title(title)
            st.code(code, language="python")


def render_summary_lines(lines: list[str]) -> None:
    clean_lines = [line for line in lines if str(line or "").strip()]
    if clean_lines:
        st.markdown(safe_markdown_text("\n".join(clean_lines)))


def applied_scope_summary_lines(scope: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    intent_type = _clean_text(scope.get("intent_type"))
    analysis_kind = _clean_text(scope.get("analysis_kind"))
    if intent_type or analysis_kind:
        lines.append(f"- 처리 의도는 `{intent_type or '-'}`이고 분석 유형은 `{analysis_kind or '-'}`입니다.")
    datasets = _string_values(scope.get("datasets") or scope.get("source_dataset_keys"))
    source_aliases = _string_values(scope.get("source_aliases"))
    if datasets:
        lines.append(f"- 사용 데이터셋은 {_inline_list(datasets)}입니다.")
    if source_aliases:
        lines.append(f"- flow 내부 source alias는 {_inline_list(source_aliases)}입니다.")
    params_by_source = _as_dict(scope.get("params_by_source") or scope.get("applied_params"))
    if params_by_source:
        lines.append("- 조회 파라미터는 " + "; ".join(_source_mapping_sentence(source, params) for source, params in params_by_source.items()) + "입니다.")
    filters_by_source = _as_dict(scope.get("filters_by_source") or scope.get("applied_filters"))
    for source, filters in filters_by_source.items():
        filter_lines = [_filter_sentence(item) for item in _as_list(filters)]
        filter_lines = [line for line in filter_lines if line]
        if filter_lines:
            lines.append(f"- `{source}`에는 " + ", ".join(filter_lines) + " 조건이 적용됩니다.")
    if not lines:
        lines.append("- 적용 조건 정보가 포함되어 있지만 요약할 수 있는 표준 항목은 없습니다. 원본 JSON을 확인하세요.")
    return lines


def intent_plan_summary_lines(intent: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    route = _clean_text(intent.get("route"))
    intent_type = _clean_text(intent.get("intent_type"))
    analysis_kind = _clean_text(intent.get("analysis_kind"))
    if route or intent_type or analysis_kind:
        lines.append(f"- route는 `{route or '-'}`, 의도 유형은 `{intent_type or '-'}`, 분석 유형은 `{analysis_kind or '-'}`입니다.")
    datasets = _string_values(intent.get("datasets"))
    if datasets:
        lines.append(f"- 계획에서 사용하는 데이터셋은 {_inline_list(datasets)}입니다.")
    function_cases = _intent_function_cases(intent)
    for function_case in function_cases:
        case_key = _clean_text(function_case.get("key"))
        function_name = _clean_text(function_case.get("function_name"))
        input_text = _clean_text(function_case.get("input_text"))
        detail = f" 함수 `{function_name}`" if function_name else ""
        if input_text:
            detail += f", 입력 `{input_text}`"
        lines.append(f"- pandas 함수 케이스 `{case_key or '-'}`를 사용합니다.{detail}")
    reasoning_steps = _string_values(intent.get("reasoning_steps"))
    for step in reasoning_steps:
        lines.append(f"- 판단 근거: {step}")
    retrieval_jobs = [item for item in _as_list(intent.get("retrieval_jobs")) if isinstance(item, dict)]
    for index, job in enumerate(retrieval_jobs, start=1):
        dataset = _clean_text(job.get("dataset_key") or job.get("dataset"))
        alias = _clean_text(job.get("source_alias"))
        purpose = _clean_text(job.get("purpose") or job.get("retrieval_purpose"))
        line = f"- 조회 작업 {index}: `{dataset or '-'}`"
        if alias:
            line += f"를 `{alias}` alias로 불러옵니다"
        if purpose:
            line += f". 목적은 {purpose}"
        lines.append(line + ".")
    step_plan = [item for item in _as_list(intent.get("step_plan")) if isinstance(item, dict)]
    for index, step in enumerate(step_plan, start=1):
        step_id = _clean_text(step.get("step_id") or step.get("id"))
        operation = _clean_text(step.get("operation") or step.get("task"))
        source = _clean_text(step.get("source_alias") or step.get("source"))
        details = []
        if step.get("group_by"):
            details.append(f"그룹 기준 {_inline_list(_string_values(step.get('group_by')))}")
        metric = _clean_text(step.get("metric") or step.get("aggregate_metric") or step.get("sort_by"))
        if metric:
            details.append(f"기준 지표 `{metric}`")
        if step.get("top_n"):
            details.append(f"상위 {step.get('top_n')}개")
        function_name = _clean_text(step.get("function_name"))
        if function_name:
            details.append(f"함수 `{function_name}`")
        function_case_key = _clean_text(step.get("function_case_key"))
        if function_case_key:
            details.append(f"함수 케이스 `{function_case_key}`")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- 분석 단계 {index}: `{step_id or '-'}`에서 `{operation or '-'}` 작업을 수행합니다. source는 `{source or '-'}`입니다{suffix}.")
    if not lines:
        lines.append("- 실행 계획 정보가 포함되어 있지만 요약할 수 있는 표준 항목은 없습니다. 원본 JSON을 확인하세요.")
    return lines


def _intent_function_cases(intent: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    multiple = intent.get("pandas_function_cases")
    if isinstance(multiple, list):
        cases.extend(deepcopy(item) for item in multiple if isinstance(item, dict))
    single = intent.get("pandas_function_case")
    if isinstance(single, dict):
        cases.append(deepcopy(single))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in cases:
        marker = (
            _clean_text(item.get("function_name")),
            _clean_text(item.get("key") or item.get("function_case_key")),
            _clean_text(item.get("input_text")),
            _clean_text(item.get("source_alias")),
        )
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


def pandas_analysis_summary_lines(analysis: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    status = _clean_text(analysis.get("status"))
    if status:
        lines.append(f"- Pandas 처리 상태는 `{status}`입니다.")
    if analysis.get("safety_passed") not in (None, "", [], {}):
        lines.append(f"- 안전성 검사는 `{analysis.get('safety_passed')}`입니다.")
    if analysis.get("executed") not in (None, "", [], {}):
        lines.append(f"- 코드 실행 여부는 `{analysis.get('executed')}`입니다.")
    row_count = analysis.get("row_count")
    if row_count not in (None, "", [], {}):
        lines.append(f"- 결과 행 수는 `{row_count}`건입니다.")
    columns = _string_values(analysis.get("columns"))
    if columns:
        lines.append(f"- 출력 컬럼은 {_inline_list(columns)}입니다.")
    for step in _string_values(analysis.get("reasoning_steps")):
        lines.append(f"- 처리 근거: {step}")
    for warning in _string_values(analysis.get("warnings")):
        lines.append(f"- 경고: {warning}")
    for error in _string_values(analysis.get("errors")):
        lines.append(f"- 오류: {error}")
    if not lines:
        lines.append("- Pandas 처리 정보가 포함되어 있지만 요약할 수 있는 표준 항목은 없습니다. 원본 JSON을 확인하세요.")
    return lines


def data_ref_summary_lines(ref: dict[str, Any]) -> list[str]:
    lines = []
    ref_id = _clean_text(ref.get("ref_id"))
    if ref_id:
        lines.append(f"- 참조 ID는 `{ref_id}`입니다.")
    dataset = _clean_text(ref.get("dataset_key") or ref.get("dataset_label"))
    alias = _clean_text(ref.get("source_alias"))
    if dataset or alias:
        lines.append(f"- 연결된 데이터셋은 `{dataset or '-'}`이고 source alias는 `{alias or '-'}`입니다.")
    store = _clean_text(ref.get("store"))
    collection = _clean_text(ref.get("collection_name"))
    db_name = _clean_text(ref.get("db_name"))
    if store or collection or db_name:
        lines.append(f"- 저장 위치는 `{store or '-'}` / `{db_name or '-'}` / `{collection or '-'}`입니다.")
    row_count = ref.get("row_count")
    if row_count not in (None, "", [], {}):
        lines.append(f"- 참조 데이터 행 수는 `{row_count}`건입니다.")
    return lines or ["- data_ref 메타데이터가 포함되어 있습니다. 원본 JSON에서 세부 값을 확인하세요."]


def status_summary_lines(status: Any) -> list[str]:
    if not isinstance(status, dict):
        return [f"- 상태 값은 `{_clean_text(status)}`입니다."]
    lines = []
    for key in ("status", "executed", "safety_passed", "used_fallback", "error"):
        value = status.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"- `{key}` 값은 `{value}`입니다.")
    return lines or generic_summary_lines(status)


def prepared_dataframe_summary_lines(prepared: dict[str, Any]) -> list[str]:
    lines = []
    row_count = prepared.get("row_count")
    if row_count not in (None, "", [], {}):
        lines.append(f"- 준비된 DataFrame 행 수는 `{row_count}`건입니다.")
    columns = _string_values(prepared.get("columns"))
    if columns:
        lines.append(f"- 준비된 컬럼은 {_inline_list(columns)}입니다.")
    preview_rows = _as_list(prepared.get("preview_rows"))
    if preview_rows:
        lines.append(f"- 화면에는 preview row `{len(preview_rows)}`건을 표시합니다.")
    return lines or ["- 준비된 DataFrame 메타데이터가 포함되어 있습니다."]


def generic_summary_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            return [f"- {str(item)}" for item in value[:12]]
        lines = []
        for index, item in enumerate(value[:8], start=1):
            if isinstance(item, dict):
                lines.append(f"- 항목 {index}: {_dict_brief(item)}")
            else:
                lines.append(f"- 항목 {index}: {_clean_text(item)}")
        return lines
    if isinstance(value, dict):
        return [f"- {_dict_brief(value)}"]
    text = _clean_text(value)
    return [f"- {text}"] if text else []


def _source_mapping_sentence(source: Any, mapping: Any) -> str:
    if isinstance(mapping, dict):
        values = ", ".join(f"{key}={_brief_value(value)}" for key, value in mapping.items())
    else:
        values = _brief_value(mapping)
    return f"`{source}`: {values}"


def _filter_sentence(item: Any) -> str:
    if not isinstance(item, dict):
        return _clean_text(item)
    field = _clean_text(item.get("field") or item.get("column") or item.get("filter_key"))
    op = _clean_text(item.get("op") or item.get("operator") or "eq")
    value = item.get("values") if item.get("values") not in (None, "", [], {}) else item.get("value")
    op_label = {
        "eq": "=",
        "in": "포함",
        "not_in": "제외",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "contains": "포함",
    }.get(op, op)
    if field:
        return f"`{field}` {op_label} {_brief_value(value)}"
    return _dict_brief(item)


def _dict_brief(value: dict[str, Any]) -> str:
    parts = []
    for key, item in value.items():
        if item in (None, "", [], {}):
            continue
        parts.append(f"`{key}`={_brief_value(item)}")
        if len(parts) >= 6:
            break
    return ", ".join(parts) if parts else "세부 값 없음"


def _brief_value(value: Any) -> str:
    if isinstance(value, list):
        return _inline_list(_string_values(value), limit=6) if value else "[]"
    if isinstance(value, dict):
        return "{" + _dict_brief(value) + "}"
    return f"`{_clean_text(value)}`"


def _inline_list(values: list[str], limit: int = 10) -> str:
    shown = [f"`{value}`" for value in values[:limit]]
    suffix = f" 외 {len(values) - limit}개" if len(values) > limit else ""
    return ", ".join(shown) + suffix


def _string_values(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    if isinstance(value, (tuple, set)):
        return [_clean_text(item) for item in value if _clean_text(item)]
    return [_clean_text(value)]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def authoring_type_from_label(label: str) -> str:
    for key, meta in AUTHORING_TYPE_OPTIONS.items():
        if meta["label"] == label:
            return key
    return "domain"


def authoring_example_text(flow_type: str) -> str:
    path = AUTHORING_EXAMPLE_PATHS.get(flow_type)
    if path and path.exists():
        return path.read_text(encoding="utf-8").strip()
    return AUTHORING_EXAMPLES.get(flow_type, "")


def example_placeholder(text: str) -> str:
    return f"예시)\n{str(text or '').strip()}"


def authoring_input_payload(raw_text: str, review_notes: str) -> str:
    raw = str(raw_text or "").strip()
    notes = str(review_notes or "").strip()
    if not notes:
        return raw
    return f"{raw}\n\n[추가 검수 지시]\n{notes}"


def render_metadata_registration(settings: dict[str, Any]) -> None:
    st.title(PAGE_METADATA)
    st.caption("Langflow saving flow API를 호출해 원문 입력부터 변환, 검증, MongoDB 저장 결과까지 한 화면에서 확인합니다.")
    labels = [meta["label"] for meta in AUTHORING_TYPE_OPTIONS.values()]
    selected_label = st.radio("등록 유형", labels, horizontal=True, label_visibility="collapsed", key="authoring_type_label")
    flow_type = authoring_type_from_label(selected_label)
    meta = AUTHORING_TYPE_OPTIONS[flow_type]
    api_url = str(getattr(settings["api_settings"], meta["settings_attr"]) or "").strip()

    st.subheader(meta["title"])
    st.caption(meta["description"])
    with st.expander("입력 예시 보기", expanded=False):
        st.code(authoring_example_text(flow_type), language="text")

    if not api_url:
        render_inline_status("", f"{meta['settings_attr']} 환경변수에 Langflow Run API URL을 설정해 주세요.", tone="warning")
    raw_text = st.text_area(
        "등록 설명",
        key=f"authoring_raw_text_{flow_type}",
        height=360,
        placeholder=example_placeholder(authoring_example_text(flow_type)),
    )
    review_notes = st.text_area(
        "추가 검수 지시",
        key=f"authoring_review_notes_{flow_type}",
        height=96,
        placeholder="예: 기존 항목과 충돌하면 보완 요청으로 돌려줘.",
    )
    execute_clicked = st.button("Langflow Saving Flow 실행", type="primary", width="stretch", disabled=not api_url)
    render_inline_status("", "저장 여부와 update mode는 현재 Langflow canvas의 Writer 설정을 따릅니다.")

    if execute_clicked:
        try:
            with render_loading_indicator():
                result = st.session_state.langflow_api.run_authoring(
                    flow_type,
                    authoring_input_payload(raw_text, review_notes),
                    session_id=st.session_state.session_id,
                )
                result["flow_type"] = result.get("flow_type") or flow_type
                st.session_state[f"authoring_result_{flow_type}"] = result
        except Exception as exc:
            st.session_state[f"authoring_result_{flow_type}"] = {
                "status": "error",
                "ui_status": "error",
                "message": f"실행 중 오류가 발생했습니다: {exc}",
                "items": [],
                "review": {},
                "write_result": {"status": "error", "errors": [str(exc)]},
                "trace": {"raw_text": raw_text},
                "errors": [str(exc)],
            }

    st.divider()
    st.subheader("실행 결과")
    result = st.session_state.get(f"authoring_result_{flow_type}")
    if isinstance(result, dict):
        render_authoring_result(result, flow_type)
    else:
        render_inline_status("", "실행하면 사용자 입력 값, 변환 텍스트, 검증 내용, 저장 결과가 단계별로 표시됩니다.")


def authoring_status_label(status: Any) -> str:
    labels = {
        "saved": "저장 완료",
        "ok": "완료",
        "processed": "처리 완료",
        "needs_more_input": "추가 정보 필요",
        "dry_run": "저장 전 검토",
        "warning": "확인 필요",
        "skipped": "저장 안 함",
        "error": "오류",
        "success": "완료",
        "ready_to_save": "저장 가능",
        "needs_supplement": "보완 필요",
    }
    return labels.get(str(status or "").strip(), str(status or "상태 없음"))


def authoring_status_tone(status: Any) -> str:
    text = str(status or "").strip()
    if text in {"saved", "ok", "success", "ready_to_save", "processed"}:
        return "success"
    if text in {"error", "failed"}:
        return "error"
    return "warning"


def authoring_saved(result: dict[str, Any]) -> bool:
    write_result = result.get("write_result") if isinstance(result.get("write_result"), dict) else {}
    if write_result.get("dry_run") or str(write_result.get("status") or result.get("ui_status") or "") == "skipped":
        return False
    return bool(
        result.get("ui_status") == "saved"
        or write_result.get("saved")
        or int_or_zero(write_result.get("saved_count")) > 0
    )


def authoring_ready_to_save(result: dict[str, Any]) -> bool:
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    if "ready_to_save" in review:
        return bool(review.get("ready_to_save"))
    return str(result.get("ui_status") or result.get("status") or "").strip() in {"saved", "ok", "ready_to_save"}


def authoring_needs_supplement(result: dict[str, Any]) -> bool:
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    return bool(review.get("needs_supplement") or review.get("supplement_requests") or result.get("ui_status") == "needs_more_input")


def authoring_item_summary_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in items:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        rows.append(
            {
                "유형": item.get("section") or item.get("metadata_type") or item.get("type") or "",
                "키": item.get("key") or item.get("dataset_key") or item.get("filter_key") or "",
                "상태": item.get("status", ""),
                "표시명": payload.get("display_name") or item.get("display_name") or "",
                "source": (payload.get("source_config") or {}).get("source_type") if isinstance(payload.get("source_config"), dict) else payload.get("source_type", ""),
            }
        )
    return pd.DataFrame(rows)


def authoring_trace_stages(result: dict[str, Any]) -> list[dict[str, Any]]:
    trace = result.get("trace")
    if isinstance(trace, list):
        return [dict(stage) for stage in trace if isinstance(stage, dict)]
    trace_dict = trace if isinstance(trace, dict) else {}
    stages = [dict(stage) for stage in trace_dict.get("stages", []) if isinstance(stage, dict)]
    if stages:
        return stages
    raw_text = trace_dict.get("raw_text") or result.get("raw_text")
    duplicate_decision = trace_dict.get("duplicate_decision") if isinstance(trace_dict.get("duplicate_decision"), dict) else {}
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    write_result = result.get("write_result") if isinstance(result.get("write_result"), dict) else {}
    items = result.get("items") if isinstance(result.get("items"), list) else []
    built: list[dict[str, Any]] = []
    if raw_text:
        built.append({"stage": "input", "label": "사용자 입력", "status": "success", "raw_text": raw_text})
    if items:
        built.append({"stage": "normalization", "label": "Metadata item 생성", "status": "success", "items": items})
    if duplicate_decision:
        built.append({"stage": "duplicate", "label": "중복 처리 정책", "status": "success", "duplicate_decision": duplicate_decision})
    if review:
        status = "success" if review.get("ready_to_save") else "warning"
        built.append(
            {
                "stage": "review",
                "label": "검토",
                "status": status,
                "supplement_requests": review.get("supplement_requests"),
                "item_reviews": review.get("item_reviews"),
                "review": review,
            }
        )
    if write_result:
        status = "success" if authoring_saved(result) else str(write_result.get("status") or "warning")
        built.append({"stage": "write", "label": "저장", "status": status, "write_result": write_result})
    return built


def render_authoring_stage(stage: dict[str, Any], index: int, key_prefix: str) -> None:
    label = str(stage.get("label") or stage.get("stage") or f"Step {index}").strip()
    status = str(stage.get("status") or "").strip()
    expanded = status in {"warning", "error"} or stage.get("stage") in {"review", "write"}
    with st.expander(f"{index}. {label} · {authoring_status_label(status)}", expanded=expanded):
        if stage.get("summary"):
            st.markdown(safe_markdown_text(stage.get("summary")))
        if stage.get("raw_text"):
            st.text_area("사용자 입력 값", value=str(stage.get("raw_text") or ""), height=150, disabled=True, key=f"{key_prefix}_stage_{index}_raw")
        for key, title in (
            ("items", "생성 항목"),
            ("supplement_requests", "보완 요청"),
            ("item_reviews", "항목별 검토"),
            ("duplicate_decision", "중복 처리 판단"),
            ("write_result", "저장 결과"),
            ("review", "검토 원본"),
            ("errors", "오류"),
            ("warnings", "경고"),
        ):
            value = stage.get(key)
            if not value:
                continue
            st.markdown(f"#### {title}")
            if key == "items" and isinstance(value, list):
                st.dataframe(authoring_item_summary_frame([item for item in value if isinstance(item, dict)]), hide_index=True, width="stretch")
            render_compact_json(value, max_height=240)


def render_authoring_result(result: dict[str, Any], key_prefix: str) -> None:
    ui_status = result.get("ui_status") or result.get("status")
    render_inline_status(authoring_status_label(ui_status), result.get("message", ""), authoring_status_tone(ui_status))
    items = result.get("items") if isinstance(result.get("items"), list) else []
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    write_result = result.get("write_result") if isinstance(result.get("write_result"), dict) else {}
    summary_cols = st.columns(4)
    summary_cols[0].metric("Items", f"{len(items):,}")
    summary_cols[1].metric("Ready", "Yes" if authoring_ready_to_save(result) else "No")
    summary_cols[2].metric("Supplement", "Yes" if authoring_needs_supplement(result) else "No")
    summary_cols[3].metric("Saved", "Yes" if authoring_saved(result) else "No")
    tabs = st.tabs(["처리 과정", "생성 항목", "보완 요청", "저장 결과", "Raw JSON"])
    with tabs[0]:
        stages = authoring_trace_stages(result)
        if not stages:
            render_inline_status("", "처리 과정 trace가 응답에 포함되어 있지 않습니다.", tone="warning")
        for index, stage in enumerate(stages, start=1):
            render_authoring_stage(stage, index, key_prefix)
    with tabs[1]:
        if items:
            st.dataframe(authoring_item_summary_frame([item for item in items if isinstance(item, dict)]), hide_index=True, width="stretch")
            render_compact_json(items, max_height=380)
        else:
            render_inline_status("", "생성된 item이 없습니다.", tone="warning")
    with tabs[2]:
        supplement_requests = review.get("supplement_requests") if isinstance(review.get("supplement_requests"), list) else []
        if supplement_requests:
            st.dataframe(pd.DataFrame(supplement_requests), hide_index=True, width="stretch")
        elif authoring_ready_to_save(result):
            render_inline_status("", "검증을 통과해 저장 가능한 상태입니다.", tone="success")
        else:
            render_inline_status("", "보완 요청이 응답에 포함되어 있지 않습니다.")
        if review:
            render_detail_title("검증 원본")
            render_compact_json(review, max_height=320)
        if result.get("existing_matches"):
            render_detail_title("비슷한 기존 정보")
            render_compact_json(result.get("existing_matches"), max_height=260)
        if result.get("conflict_warnings"):
            render_detail_title("경고")
            render_compact_json(result.get("conflict_warnings"), max_height=260)
    with tabs[3]:
        if write_result:
            render_compact_json(write_result, max_height=360)
        else:
            render_inline_status("", "MongoDB writer 결과가 응답에 포함되어 있지 않습니다.", tone="warning")
    with tabs[4]:
        render_compact_json(result.get("api_response") or result.get("raw_response") or result, max_height=520)
        st.download_button(
            "Saving 결과 JSON 다운로드",
            data=json_text(result.get("api_response") or result.get("raw_response") or result),
            file_name=f"langflow_saving_{key_prefix}.json",
            mime="application/json",
            key=f"{key_prefix}_download_saving_result",
            width="stretch",
        )


def value_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json_text(value)
    if value is None:
        return ""
    return str(value)


def key_value_frame(value: dict[str, Any]) -> pd.DataFrame:
    rows = [{"항목": key, "값": value_text(item)} for key, item in value.items()]
    return pd.DataFrame(rows, columns=["항목", "값"])


def domain_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in items:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        rows.append(
            {
                "gbn": item.get("gbn") or item.get("section", ""),
                "key": item.get("key", ""),
                "status": item.get("status", ""),
                "display_name": payload.get("display_name") or item.get("display_name", ""),
                "aliases": ", ".join(str(alias) for alias in (item.get("aliases") or payload.get("aliases") or [])[:5]),
            }
        )
    return pd.DataFrame(rows)


def table_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in items:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        rows.append(
            {
                "dataset_key": item.get("dataset_key", ""),
                "status": item.get("status", ""),
                "display_name": item.get("display_name") or payload.get("display_name", ""),
                "dataset_family": item.get("dataset_family") or payload.get("dataset_family", ""),
                "source_type": item.get("source_type") or payload.get("source_type", ""),
            }
        )
    return pd.DataFrame(rows)


def main_filter_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in items:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        rows.append(
            {
                "filter_key": item.get("filter_key") or item.get("key", ""),
                "status": item.get("status", ""),
                "display_name": item.get("display_name") or payload.get("display_name", ""),
                "semantic_role": item.get("semantic_role") or payload.get("semantic_role", ""),
                "column_candidates": ", ".join(str(column) for column in (item.get("column_candidates") or payload.get("column_candidates") or [])[:5]),
            }
        )
    return pd.DataFrame(rows)


def domain_item_label(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    gbn = item.get("gbn") or item.get("section", "")
    display_name = str(payload.get("display_name") or item.get("display_name") or "").strip()
    label = f"{gbn}:{item.get('key', '')}"
    return f"{label} | {display_name}" if display_name else label


def domain_section_label(section: Any) -> str:
    key = str(section or "").strip()
    return DOMAIN_SECTION_DESCRIPTIONS.get(key, (key or "도메인", ""))[0]


def domain_section_description(section: Any) -> str:
    key = str(section or "").strip()
    return DOMAIN_SECTION_DESCRIPTIONS.get(key, ("", ""))[1]


def short_value_text(value: Any, max_length: int = 240) -> str:
    text = value_text(value).strip()
    if len(text) <= max_length:
        return text
    return text[: max(0, max_length - 1)].rstrip() + "…"


def as_display_list(value: Any, limit: int = 8) -> str:
    if isinstance(value, list):
        values = [str(item) for item in value if str(item or "").strip()]
    elif isinstance(value, dict):
        values = [f"{key}={short_value_text(item, 60)}" for key, item in value.items()]
    elif str(value or "").strip():
        values = [str(value)]
    else:
        values = []
    if not values:
        return ""
    clipped = values[:limit]
    suffix = f" 외 {len(values) - limit}개" if len(values) > limit else ""
    return ", ".join(clipped) + suffix


def metadata_registration_trace(item: dict[str, Any]) -> dict[str, str]:
    trace = item.get("registration_trace") if isinstance(item.get("registration_trace"), dict) else {}
    if not trace and isinstance(item.get("authoring_trace"), dict):
        trace = item["authoring_trace"]
    if not trace and isinstance(item.get("trace"), dict):
        trace = item["trace"]
    result = {
        "raw_text": trace.get("raw_text") or item.get("raw_text") or item.get("source_text") or "",
        "refined_text": trace.get("refined_text") or item.get("refined_text") or "",
        "reviewed_at": trace.get("reviewed_at") or item.get("reviewed_at") or "",
    }
    return {key: str(value) for key, value in result.items() if str(value or "").strip()}


domain_saving_trace = metadata_registration_trace


def render_metadata_registration_trace(item: dict[str, Any], key_prefix: str) -> None:
    trace = metadata_registration_trace(item)
    if trace.get("raw_text") or trace.get("refined_text"):
        render_detail_title("생성할 때 입력한 문장")
        if trace.get("raw_text"):
            st.text_area("사용자 입력 원문", value=trace["raw_text"], height=130, disabled=True, key=f"{key_prefix}_raw_text")
        if trace.get("refined_text") and trace.get("refined_text") != trace.get("raw_text"):
            st.text_area("정제된 입력 문장", value=trace["refined_text"], height=130, disabled=True, key=f"{key_prefix}_refined_text")
    else:
        render_inline_status("생성 입력 없음", "이 문서에는 생성 당시 raw_text/refined_text가 저장되어 있지 않습니다. 앞으로 저장되는 메타데이터는 registration_trace에 입력 문장이 함께 남습니다.", tone="warning")


render_metadata_authoring_trace = render_metadata_registration_trace


def domain_item_summary(item: dict[str, Any]) -> dict[str, str]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    section = str(item.get("gbn") or item.get("section") or "").strip()
    summary = {
        "도메인 유형": f"{domain_section_label(section)} ({section})" if section else domain_section_label(section),
        "키": str(item.get("key") or ""),
        "상태": str(item.get("status") or "active"),
        "표시명": str(payload.get("display_name") or item.get("display_name") or ""),
    }
    aliases = as_display_list(item.get("aliases") or payload.get("aliases"))
    if aliases:
        summary["사용자 표현/별칭"] = aliases
    description = domain_section_description(section)
    use_when = payload.get("use_when") or payload.get("description") or payload.get("summary")
    if use_when or description:
        summary["무엇을 의미하나"] = str(use_when or description)
    cues = as_display_list(payload.get("required_question_cues") or payload.get("question_cues"))
    if cues:
        summary["질문에서 찾는 표현"] = cues
    if section == "process_groups":
        summary["포함 공정"] = as_display_list(payload.get("processes") or payload.get("OPER_NAME"), limit=12)
    elif section == "product_terms":
        summary["적용 조건"] = short_value_text(payload.get("condition_by_family") or payload.get("condition") or payload.get("filters"))
    elif section == "quantity_terms":
        summary["수량 컬럼"] = as_display_list(payload.get("quantity_column"))
        summary["집계 방식"] = str(payload.get("aggregation") or "")
        summary["출력 컬럼"] = str(payload.get("output_column") or "")
        summary["대상 데이터"] = as_display_list(payload.get("dataset_key") or payload.get("dataset_family") or payload.get("required_dataset_families"))
    elif section == "metric_terms":
        summary["계산식"] = short_value_text(payload.get("formula") or payload.get("calculation"))
        summary["필요 수량"] = as_display_list(payload.get("required_quantity_terms"))
        summary["출력 컬럼"] = str(payload.get("output_column") or "")
    elif section == "status_terms":
        summary["상태 조건"] = short_value_text(payload.get("condition") or payload.get("filters"))
        summary["대상 데이터"] = as_display_list(payload.get("dataset_key") or payload.get("dataset_family") or payload.get("required_dataset_families"))
    elif section == "analysis_recipes":
        summary["분석 유형"] = str(payload.get("default_analysis_kind") or "")
        summary["사용 데이터"] = as_display_list(payload.get("required_dataset_families") or payload.get("required_quantity_terms"))
        steps = payload.get("step_plan_template") if isinstance(payload.get("step_plan_template"), list) else []
        if steps:
            summary["처리 단계"] = " → ".join(str(step.get("step_id") or step.get("operation") or index + 1) for index, step in enumerate(steps[:6]) if isinstance(step, dict))
    elif section == "pandas_function_cases":
        summary["사용 함수"] = str(payload.get("function_name") or "")
        summary["입력 설명"] = str(payload.get("input_text") or "")
        summary["필수 컬럼"] = as_display_list(payload.get("required_source_columns"), limit=12)
    elif section == "product_key_columns":
        summary["제품 키 컬럼"] = as_display_list(item.get("columns") or payload.get("columns") or payload.get("product_key_columns"), limit=16)
    return {key: value for key, value in summary.items() if str(value or "").strip()}


def render_domain_human_summary(item: dict[str, Any], key_prefix: str) -> None:
    section = item.get("gbn") or item.get("section") or ""
    render_metadata_registration_trace(item, f"{key_prefix}_domain")
    description = domain_section_description(section)
    if description:
        render_inline_status(domain_section_label(section), description, tone="success")
    st.dataframe(key_value_frame(domain_item_summary(item)), width="stretch", hide_index=True)


def table_item_label(item: dict[str, Any]) -> str:
    label = str(item.get("dataset_key") or "")
    display_name = str(item.get("display_name") or "").strip()
    return f"{label} | {display_name}" if display_name else label


def main_filter_item_label(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    label = str(item.get("filter_key") or item.get("key") or "")
    role = str(item.get("semantic_role") or payload.get("semantic_role") or payload.get("value_type") or "").strip()
    return f"{label} | {role}" if role else label


def metadata_item_key_prefix(key_prefix: str, item: dict[str, Any]) -> str:
    section = item.get("gbn") or item.get("section") or ""
    item_key = item.get("key") or item.get("dataset_key") or item.get("filter_key") or ""
    identity = item.get("_id") or item.get("id") or f"{section}:{item_key}"
    raw_key = f"{key_prefix}_{identity}"
    safe_key = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in raw_key)
    return safe_key or key_prefix


def render_domain_item_detail(item: dict[str, Any], settings: dict[str, Any], key_prefix: str) -> None:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    gbn = item.get("gbn") or item.get("section", "")
    item_key_prefix = metadata_item_key_prefix(key_prefix, item)
    tab_summary, tab_payload = st.tabs(["요약", "Payload"])
    with tab_summary:
        render_domain_human_summary(item, item_key_prefix)
        render_detail_title("저장 위치")
        st.dataframe(
            key_value_frame(
                {
                    "collection": collection_name_for("domain", settings.get("api_settings") if isinstance(settings, dict) else None),
                    "section": gbn,
                    "key": item.get("key", ""),
                }
            ),
            width="stretch",
            hide_index=True,
        )
        render_metadata_delete_action("domain", item, settings, f"{item_key_prefix}_domain")
    with tab_payload:
        st.dataframe(key_value_frame(payload), width="stretch", hide_index=True)


def render_table_item_detail(item: dict[str, Any], settings: dict[str, Any], key_prefix: str) -> None:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    metadata = {key: value for key, value in {**payload, **{k: v for k, v in item.items() if k != "payload"}}.items() if key not in {"columns", "source_text"}}
    item_key_prefix = metadata_item_key_prefix(key_prefix, item)
    summary = {
        "dataset_key": item.get("dataset_key", ""),
        "status": item.get("status", ""),
        "display_name": item.get("display_name") or payload.get("display_name", ""),
        "source_type": item.get("source_type") or payload.get("source_type", ""),
        "tool_name": payload.get("tool_name", ""),
        "collection": collection_name_for("table_catalog", settings.get("api_settings") if isinstance(settings, dict) else None),
    }
    tab_summary, tab_metadata, tab_columns = st.tabs(["요약", "Metadata", "Columns"])
    with tab_summary:
        render_metadata_registration_trace(item, f"{item_key_prefix}_table")
        st.dataframe(key_value_frame(summary), width="stretch", hide_index=True)
        if payload.get("description") or item.get("description"):
            st.text_area("Description", value=str(payload.get("description") or item.get("description") or ""), height=100, disabled=True, key=f"{item_key_prefix}_table_description")
        render_metadata_delete_action("table_catalog", item, settings, f"{item_key_prefix}_table")
    with tab_metadata:
        st.dataframe(key_value_frame(metadata), width="stretch", hide_index=True)
    with tab_columns:
        columns = payload.get("columns") if isinstance(payload.get("columns"), list) else []
        st.dataframe(pd.DataFrame(columns), width="stretch", hide_index=True)


def render_main_filter_item_detail(item: dict[str, Any], settings: dict[str, Any], key_prefix: str) -> None:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    item_key_prefix = metadata_item_key_prefix(key_prefix, item)
    summary = {
        "filter_key": item.get("filter_key") or item.get("key", ""),
        "status": item.get("status", ""),
        "value_type": payload.get("value_type", ""),
        "semantic_role": item.get("semantic_role") or payload.get("semantic_role", ""),
        "collection": collection_name_for("main_flow_filter", settings.get("api_settings") if isinstance(settings, dict) else None),
    }
    tab_summary, tab_payload = st.tabs(["요약", "Payload"])
    with tab_summary:
        render_metadata_registration_trace(item, f"{item_key_prefix}_main_filter")
        st.dataframe(key_value_frame(summary), width="stretch", hide_index=True)
        render_metadata_delete_action("main_flow_filter", item, settings, f"{item_key_prefix}_main_filter")
    with tab_payload:
        st.dataframe(key_value_frame(payload), width="stretch", hide_index=True)


def filter_metadata_status(rows: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    if status == "all":
        return rows
    return [row for row in rows if str(row.get("status") or "active") == status]


def load_lookup_metadata(metadata_type: str, status: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    api_settings = settings.get("api_settings")
    mongo_uri = str(getattr(api_settings, "mongo_uri", "") or "")
    mongo_database = str(getattr(api_settings, "mongo_database", "") or "datagov")
    collection_name = collection_name_for(metadata_type, api_settings)
    loaded = load_metadata_items(
        metadata_type,
        mongo_uri=mongo_uri,
        mongo_database=mongo_database,
        collection_name=collection_name,
        status=status,
    )
    if not loaded.get("ok"):
        render_inline_status(
            "조회 불가",
            f"{loaded.get('collection_name') or collection_name}: {loaded.get('message') or 'metadata를 불러오지 못했습니다.'}",
            tone="warning",
        )
        return []
    st.caption(f"MongoDB `{loaded.get('database')}` / `{loaded.get('collection_name')}`에서 {len(loaded.get('items', [])):,}건을 불러왔습니다.")
    return [item for item in loaded.get("items", []) if isinstance(item, dict)]


def render_metadata_delete_action(metadata_type: str, item: dict[str, Any], settings: dict[str, Any], key_prefix: str) -> None:
    if str(item.get("status") or "active").strip() == "deleted":
        render_inline_status("삭제됨", "이 항목은 이미 deleted 상태입니다.")
        return
    api_settings = settings.get("api_settings") if isinstance(settings, dict) else None
    mongo_uri = str(getattr(api_settings, "mongo_uri", "") or "").strip()
    if not mongo_uri:
        render_inline_status("", "MONGODB_URI가 설정되어 있지 않아 웹에서 삭제할 수 없습니다.", tone="warning")
        return
    if st.button("선택 항목 삭제", key=f"{key_prefix}_delete", width="stretch"):
        result = mark_metadata_deleted(
            metadata_type,
            mongo_uri=mongo_uri,
            mongo_database=str(getattr(api_settings, "mongo_database", "") or "datagov"),
            collection_name=collection_name_for(metadata_type, api_settings),
            item=item,
        )
        if result.get("ok"):
            st.success("선택한 metadata 항목을 deleted 상태로 변경했습니다.")
            st.rerun()
        else:
            render_inline_status("삭제 실패", result.get("message") or "metadata 상태를 변경하지 못했습니다.", tone="error")


def domain_export_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    domain: dict[str, Any] = {}
    for row in rows:
        section = str(row.get("section") or "").strip()
        key = str(row.get("key") or "").strip()
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if not section:
            continue
        if isinstance(payload.get("values"), list) and key == section:
            domain[section] = payload.get("values")
        else:
            domain.setdefault(section, {})
            if isinstance(domain[section], dict) and key:
                domain[section][key] = payload
    return {"domain": domain}


def table_catalog_export_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "datasets": {
            str(row.get("dataset_key")): row.get("payload")
            for row in rows
            if str(row.get("dataset_key") or "").strip() and isinstance(row.get("payload"), dict)
        }
    }


def main_filter_export_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "main_flow_filters": {
            str(row.get("filter_key")): row.get("payload")
            for row in rows
            if str(row.get("filter_key") or "").strip() and isinstance(row.get("payload"), dict)
        }
    }


def render_lookup(settings: dict[str, Any]) -> None:
    st.title(PAGE_LOOKUP)
    st.caption("MongoDB에 저장된 현재 metadata item을 확인하고 Langflow 입력 JSON으로 내보냅니다.")
    tab_domain, tab_table, tab_filters = st.tabs(["도메인", "테이블 카탈로그", "Main Flow Filters"])

    with tab_domain:
        filter_status, filter_type = st.columns(2)
        with filter_status:
            status = st.selectbox("Domain Status", ["active", "review_required", "deleted", "all"], index=0, key="domain_status")
        all_domain_items = load_lookup_metadata("domain", status, settings)
        domain_types = sorted({str(item.get("gbn") or item.get("section") or "").strip() for item in all_domain_items if str(item.get("gbn") or item.get("section") or "").strip()})
        with filter_type:
            gbn_filter = st.selectbox("Domain Type", ["all", *domain_types], index=0, key="domain_gbn_filter")
        items = [item for item in all_domain_items if gbn_filter == "all" or str(item.get("gbn") or item.get("section") or "") == gbn_filter]
        st.dataframe(domain_frame(items), width="stretch", hide_index=True)
        domain_json = domain_export_payload(items)
        st.download_button("Domain JSON 다운로드", data=json_text(domain_json), file_name="langflow_main_domain_export.json", mime="application/json", width="stretch")
        with st.expander("Domain JSON 보기"):
            render_compact_json(domain_json, max_height=420)
        if items:
            st.markdown("#### 선택 항목 상세")
            labels = [domain_item_label(item) for item in items]
            selected = st.selectbox("상세 조회할 domain item", labels, key="domain_detail_select")
            selected_item = items[labels.index(selected)]
            render_domain_item_detail(selected_item, settings, "lookup")

    with tab_table:
        filter_status, _filter_spacer = st.columns([1, 1])
        with filter_status:
            status = st.selectbox("Table Status", ["active", "review_required", "deleted", "all"], index=0, key="table_status")
        items = load_lookup_metadata("table_catalog", status, settings)
        st.dataframe(table_frame(items), width="stretch", hide_index=True)
        catalog_json = table_catalog_export_payload(items)
        st.download_button("Table Catalog JSON 다운로드", data=json_text(catalog_json), file_name="langflow_main_table_catalog_export.json", mime="application/json", width="stretch")
        with st.expander("Table Catalog JSON 보기"):
            render_compact_json(catalog_json, max_height=420)
        if items:
            st.markdown("#### 선택 항목 상세")
            labels = [table_item_label(item) for item in items]
            selected = st.selectbox("상세 조회할 dataset", labels, key="table_detail_select")
            selected_item = items[labels.index(selected)]
            render_table_item_detail(selected_item, settings, "lookup")

    with tab_filters:
        filter_status, _filter_spacer = st.columns([1, 1])
        with filter_status:
            status = st.selectbox("Filter Status", ["active", "review_required", "deleted", "all"], index=0, key="main_filter_status")
        items = load_lookup_metadata("main_flow_filter", status, settings)
        st.dataframe(main_filter_frame(items), width="stretch", hide_index=True)
        filters_json = {"main_flow_filters": {"items": items}}
        st.download_button("Main Flow Filters JSON 다운로드", data=json_text(filters_json), file_name="langflow_main_flow_filters_export.json", mime="application/json", width="stretch")
        with st.expander("Main Flow Filters JSON 보기"):
            render_compact_json(filters_json, max_height=420)
        if items:
            st.markdown("#### 선택 항목 상세")
            labels = [main_filter_item_label(item) for item in items]
            selected = st.selectbox("상세 조회할 filter", labels, key="main_filter_detail_select")
            selected_item = items[labels.index(selected)]
            render_main_filter_item_detail(selected_item, settings, "lookup")


def render_detail_title(value: str) -> None:
    st.markdown(f'<div class="detail-section-title">{html.escape(str(value))}</div>', unsafe_allow_html=True)


def render_inline_status(label: str, value: Any, tone: str = "info") -> None:
    label_text = html.escape(str(label or "").strip())
    value_text = html.escape(str(value or "").strip())
    tone_class = {
        "warning": " inline-status-warning",
        "error": " inline-status-error",
        "success": " inline-status-success",
    }.get(str(tone or "info").strip().lower(), "")
    label_html = f"<strong>{label_text}</strong>" if label_text else ""
    st.markdown(f'<div class="inline-status{tone_class}">{label_html}<span>{value_text}</span></div>', unsafe_allow_html=True)


def render_compact_json(value: Any, max_height: int | None = None) -> None:
    style = f' style="max-height:{max(120, int(max_height))}px; overflow:auto;"' if max_height else ""
    st.html(f'<pre class="compact-json-block"{style}>{compact_json_html(value)}</pre>')


def inject_style() -> None:
    st.markdown(
        """
        <style>
        html, body, [class*="css"] {
            font-family: Inter, Pretendard, "Segoe UI", "Noto Sans KR", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            color: #182230;
        }
        .stApp { background: #fbfcfe; }
        .block-container { padding-top: 1.05rem; padding-bottom: 3rem; max-width: 1280px; }
        body:not(:has(.chat-topbar)) .block-container {
            padding-top: 3.35rem;
        }
        [data-testid="stHeader"] {
            background: rgba(251, 252, 254, 0.96);
            border-bottom: 1px solid #e4e7ec;
            backdrop-filter: blur(10px);
        }
        [data-testid="stHeader"]::before {
            content: "PTMORE PKG AGENT";
            position: absolute;
            left: 1.1rem;
            top: 50%;
            transform: translateY(-50%);
            color: #111827;
            font-size: 0.96rem;
            font-weight: 780;
            line-height: 1;
            letter-spacing: 0;
            pointer-events: none;
            white-space: nowrap;
        }
        [data-testid="stSpinner"] svg,
        [data-testid="stSpinner"] > div:first-child {
            animation: mdv5-spinner-rotate 0.82s linear infinite !important;
            transform-origin: center !important;
            transform-box: fill-box !important;
        }
        .mdv5-inline-loader {
            width: 1.2rem;
            height: 1.2rem;
            margin: 0.15rem 0 0.35rem 0.1rem;
            border-radius: 50%;
            border: 2px solid #d0d5dd;
            border-top-color: #2563eb;
            animation: mdv5-spinner-rotate 0.82s linear infinite;
        }
        @keyframes mdv5-spinner-rotate {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        [data-testid="stSidebar"] { background: #f7f8fb; border-right: 1px solid #e4e7ec; }
        h1, h2, h3 {
            letter-spacing: 0;
            color: #111827;
            font-weight: 720 !important;
        }
        h1 {
            font-size: 1.46rem !important;
            line-height: 1.36 !important;
            margin-bottom: 0.12rem !important;
            padding-top: 0.04rem !important;
            overflow: visible !important;
        }
        h2 { font-size: 1.16rem !important; line-height: 1.28 !important; }
        h3 { font-size: 1.02rem !important; line-height: 1.34 !important; }
        [data-testid="stMain"] [data-testid="stCaptionContainer"] {
            color: #667085;
            font-size: 0.84rem;
            line-height: 1.48;
            max-width: 860px;
            margin-bottom: 0.45rem;
        }
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li {
            color: #344054;
            font-size: 0.88rem;
            line-height: 1.54;
            margin-bottom: 0.24rem;
        }
        [data-testid="stMarkdownContainer"] ul,
        [data-testid="stMarkdownContainer"] ol {
            margin-top: 0.18rem !important;
            margin-bottom: 0.58rem !important;
            padding-left: 1.05rem !important;
        }
        [data-testid="stMarkdownContainer"] li p {
            margin: 0 !important;
        }
        .small-note { color: #667085; font-size: 0.84rem; line-height: 1.5; }
        .detail-text {
            color: #475467;
            font-size: 0.88rem;
            line-height: 1.54;
            margin: 0.08rem 0 0.58rem;
        }
        div[data-testid="stTextArea"] textarea {
            color: #182230 !important;
            font-size: 0.88rem !important;
            line-height: 1.5 !important;
            border-radius: 8px !important;
        }
        div[data-testid="stTextArea"] textarea::placeholder { color: #98a2b3 !important; opacity: 1 !important; }
        div[data-testid="stTextInput"] input { font-size: 0.9rem !important; border-radius: 8px !important; }
        div[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        div[data-testid="stTextInput"] input,
        div[data-testid="stTextArea"] textarea {
            border-color: #d0d5dd !important;
            box-shadow: none !important;
        }
        [data-testid="stMain"] div[data-testid="stRadio"] div[role="radiogroup"] {
            display: flex !important;
            align-items: center !important;
            gap: 0.78rem !important;
            flex-wrap: wrap !important;
            margin: 0.16rem 0 1.28rem !important;
        }
        [data-testid="stMain"] div[data-testid="stRadio"] label,
        [data-testid="stMain"] div[data-testid="stRadio"] label p,
        [data-testid="stMain"] div[data-testid="stRadio"] label span {
            color: #344054 !important;
            font-size: 0.88rem !important;
            font-weight: 650 !important;
            line-height: 1.2 !important;
            margin: 0 !important;
        }
        [data-testid="stMain"] div[data-testid="stRadio"] label {
            display: inline-flex !important;
            align-items: center !important;
            width: auto !important;
            min-height: 1.52rem !important;
            gap: 0.38rem !important;
            padding: 0 !important;
        }
        [data-testid="stMain"] div[data-testid="stRadio"] label > div:first-child {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            margin: 0 !important;
            transform: scale(0.92);
            transform-origin: center;
        }
        [data-testid="stMain"] div[data-testid="stRadio"] label div[data-testid="stMarkdownContainer"] {
            display: inline-flex !important;
            align-items: center !important;
            margin: 0 !important;
        }
        [data-testid="stBottom"] {
            min-height: 3.75rem !important;
        }
        [data-testid="stBottomBlockContainer"] {
            padding-top: 0.4rem !important;
            padding-bottom: 0.42rem !important;
        }
        [data-testid="stChatInput"] {
            min-height: 2.55rem !important;
        }
        [data-testid="stChatInput"] > div,
        [data-testid="stChatInput"] > div > div:first-child,
        [data-testid="stChatInput"] > div > div:first-child > div {
            min-height: 2.55rem !important;
        }
        [data-testid="stChatInput"] textarea,
        [data-testid="stChatInputTextArea"] {
            height: 2.55rem !important;
            min-height: 2.55rem !important;
            padding-top: 0.62rem !important;
            padding-bottom: 0.45rem !important;
            font-size: 0.86rem !important;
            line-height: 1.2 !important;
        }
        [data-testid="stChatInputTextArea"]::placeholder {
            color: #8b95a5 !important;
            opacity: 1 !important;
        }
        [data-testid="stMain"] div[data-testid="stChatMessage"] {
            gap: 0.62rem !important;
            padding: 0.48rem 0 !important;
        }
        [data-testid="stMain"] div[data-testid="stChatMessage"] [data-testid*="ChatMessageAvatar"] {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            width: 1.95rem !important;
            height: 1.95rem !important;
            min-width: 1.95rem !important;
            min-height: 1.95rem !important;
            margin-top: 0.12rem !important;
            border-radius: 9px !important;
            border: 1px solid #d8dee8 !important;
            background: #ffffff !important;
            color: #475467 !important;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06) !important;
        }
        [data-testid="stMain"] div[data-testid="stChatMessage"] [data-testid*="ChatMessageAvatar"] svg,
        [data-testid="stMain"] div[data-testid="stChatMessage"] [data-testid*="ChatMessageAvatar"] span {
            width: 1.05rem !important;
            height: 1.05rem !important;
            font-size: 1.05rem !important;
            line-height: 1 !important;
        }
        [data-testid="stMain"] div[data-testid="stChatMessage"] [data-testid="stChatMessageAvatarUser"] {
            border-color: #f1c6c6 !important;
            background: #fff8f8 !important;
            color: #b42318 !important;
        }
        [data-testid="stMain"] div[data-testid="stChatMessage"] [data-testid="stChatMessageAvatarAssistant"] {
            border-color: #efd7a6 !important;
            background: #fffaf0 !important;
            color: #a16207 !important;
        }
        code {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace !important;
            font-size: 0.76rem !important;
        }
        pre,
        pre code,
        div[data-testid="stCode"] code,
        div[data-testid="stCodeBlock"] code {
            font-size: 0.72rem !important;
            line-height: 1.38 !important;
        }
        div[data-testid="stCode"] pre,
        div[data-testid="stCodeBlock"] pre {
            padding: 0.65rem 0.75rem !important;
            border-radius: 8px !important;
        }
        [data-testid="stMain"] h4,
        [data-testid="stMain"] div[data-testid="stHeading"] h4,
        [data-testid="stMain"] div[data-testid="stMarkdownContainer"] h4 {
            color: #344054 !important;
            font-size: 0.88rem !important;
            font-weight: 720 !important;
            line-height: 1.24 !important;
            margin: 0.72rem 0 0.42rem !important;
            padding: 0 !important;
        }
        .detail-section-title {
            color: #344054;
            font-size: 0.88rem;
            font-weight: 720;
            line-height: 1.24;
            margin: 0.72rem 0 0.34rem;
        }
        .inline-status {
            display: flex;
            align-items: center;
            gap: 0.22rem;
            min-height: 2.15rem;
            margin: 0.18rem 0 0.86rem;
            padding: 0.48rem 0.72rem;
            border-radius: 8px;
            background: #e8efff;
            color: #344054;
            font-size: 0.84rem;
            line-height: 1.2;
            box-sizing: border-box;
        }
        .inline-status * {
            line-height: 1.2 !important;
        }
        .inline-status strong {
            color: #263244;
            font-weight: 720;
        }
        .inline-status-warning {
            background: #fffbea;
            color: #344054;
        }
        .inline-status-error {
            background: #fff1f3;
            color: #912018;
        }
        .inline-status-success {
            background: #ecfdf3;
            color: #027a48;
        }
        .compact-json-block {
            margin: 0.2rem 0 0.72rem;
            padding: 0.5rem 0.62rem;
            border: 1px solid #eef2f7 !important;
            border-radius: 8px !important;
            background: #ffffff !important;
            overflow: auto;
            box-shadow: none !important;
        }
        .compact-json-block,
        .compact-json-block * {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace !important;
            font-size: 0.62rem !important;
            line-height: 1.32 !important;
            color: #344054 !important;
            white-space: pre !important;
        }
        .compact-json-key { color: #344054 !important; font-weight: 680; }
        .compact-json-string { color: #b45309 !important; }
        .compact-json-number { color: #2563eb !important; }
        .compact-json-boolean { color: #0f766e !important; }
        .compact-json-null { color: #7c3aed !important; }
        .compact-json-punctuation { color: #667085 !important; }
        [data-testid="stMain"] div[data-testid="stJson"],
        [data-testid="stMain"] .stJson,
        [data-testid="stMain"] .react-json-view {
            padding: 0.46rem 0.56rem !important;
            border: 1px solid #eef2f7 !important;
            border-radius: 8px !important;
            background: #ffffff !important;
        }
        [data-testid="stMain"] div[data-testid="stJson"],
        [data-testid="stMain"] div[data-testid="stJson"] *,
        [data-testid="stMain"] .stJson,
        [data-testid="stMain"] .stJson *,
        [data-testid="stMain"] .react-json-view,
        [data-testid="stMain"] .react-json-view * {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace !important;
            font-size: 0.62rem !important;
            line-height: 1.3 !important;
        }
        [data-testid="stMain"] details div[data-testid="stCode"] code,
        [data-testid="stMain"] details div[data-testid="stCodeBlock"] code,
        [data-testid="stMain"] details pre,
        [data-testid="stMain"] details pre code {
            font-size: 0.62rem !important;
            line-height: 1.32 !important;
        }
        [data-testid="stMain"] details div[data-testid="stCode"] pre,
        [data-testid="stMain"] details div[data-testid="stCodeBlock"] pre {
            padding: 0.54rem 0.62rem !important;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid #d8dee8;
            border-radius: 10px;
            overflow: visible;
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05), 0 8px 24px rgba(16, 24, 40, 0.04);
        }
        div[data-testid="stDataFrame"] > div {
            border-radius: 10px;
        }
        div[data-testid="stDataFrame"] button {
            border-radius: 8px !important;
        }
        [data-testid="stMain"] div[data-testid="stButton"] button,
        [data-testid="stMain"] div[data-testid="stDownloadButton"] button {
            min-height: 2.25rem !important;
            border-radius: 7px !important;
            font-size: 0.84rem !important;
            font-weight: 650 !important;
            line-height: 1.1 !important;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04) !important;
        }
        [data-testid="stMain"] div[data-testid="stButton"] button p,
        [data-testid="stMain"] div[data-testid="stDownloadButton"] button p {
            font-size: inherit !important;
            font-weight: inherit !important;
            line-height: 1.1 !important;
            margin: 0 !important;
        }
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"] {
            border-color: #2563eb !important;
            background: #2563eb !important;
            color: #ffffff !important;
            box-shadow: 0 6px 14px rgba(37, 99, 235, 0.24) !important;
        }
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"]:hover {
            border-color: #1d4ed8 !important;
            background: #1d4ed8 !important;
            color: #ffffff !important;
        }
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"]:active {
            border-color: #1e40af !important;
            background: #1e40af !important;
            color: #ffffff !important;
        }
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"]:disabled,
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"][disabled] {
            border-color: #93c5fd !important;
            background: #93c5fd !important;
            color: rgba(255, 255, 255, 0.82) !important;
            box-shadow: none !important;
        }
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"] p,
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"] span,
        [data-testid="stMain"] button[data-testid="stBaseButton-primary"] div[data-testid="stMarkdownContainer"] p {
            color: #ffffff !important;
        }
        [data-testid="stMain"] details {
            border-color: #d8dee8 !important;
            border-radius: 8px !important;
            background: #ffffff !important;
        }
        [data-testid="stMain"] details summary {
            min-height: 2.25rem !important;
            padding: 0.5rem 0.7rem !important;
        }
        [data-testid="stMain"] details summary p {
            color: #344054 !important;
            font-size: 0.84rem !important;
            font-weight: 650 !important;
            line-height: 1.18 !important;
            margin: 0 !important;
        }
        [data-testid="stMain"] div[data-testid="stTabs"] button[role="tab"] {
            min-height: 2.15rem !important;
            padding: 0.38rem 0.7rem !important;
            font-size: 0.84rem !important;
            font-weight: 650 !important;
        }
        [data-testid="stMain"] div[data-testid="stTabs"] button[role="tab"] p,
        [data-testid="stMain"] div[data-testid="stTabs"] button[role="tab"] span {
            font-size: 0.82rem !important;
            font-weight: 650 !important;
            line-height: 1.15 !important;
            margin: 0 !important;
        }
        [data-testid="stMain"] div[data-testid="stAlert"] {
            border-radius: 8px !important;
            padding: 0.48rem 0.68rem !important;
        }
        [data-testid="stMain"] div[data-testid="stAlert"] [role="alert"],
        [data-testid="stMain"] div[data-testid="stAlert"] div[data-baseweb="notification"] {
            display: flex !important;
            align-items: center !important;
            min-height: 2rem !important;
            padding-top: 0.36rem !important;
            padding-bottom: 0.36rem !important;
            box-sizing: border-box !important;
        }
        [data-testid="stMain"] div[data-testid="stAlert"] > div {
            display: flex !important;
            align-items: center !important;
            min-height: 2rem !important;
        }
        [data-testid="stMain"] div[data-testid="stAlert"] div[data-testid="stMarkdownContainer"] {
            display: flex !important;
            align-items: center !important;
            min-height: 1.12rem !important;
            padding: 0 !important;
        }
        [data-testid="stMain"] div[data-testid="stAlert"] div[data-testid="stMarkdownContainer"] > div {
            display: flex !important;
            align-items: center !important;
            min-height: 1.12rem !important;
        }
        [data-testid="stMain"] div[data-testid="stAlert"] p {
            display: flex !important;
            align-items: center !important;
            font-size: 0.84rem !important;
            line-height: 1.12 !important;
            margin: 0 !important;
            padding: 0 !important;
        }
        .session-strip {
            display: grid;
            grid-template-columns: auto minmax(0, 1fr);
            align-items: center;
            gap: 0.42rem;
            box-sizing: border-box;
            height: 2.14rem;
            min-height: 2.14rem;
            padding: 0.26rem 0.52rem;
            border: 1px solid #d8dee8;
            border-radius: 8px;
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        .session-strip-label {
            color: #667085;
            font-size: 0.62rem;
            font-weight: 760;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .session-strip-value {
            min-width: 0;
            color: #111827;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
            font-size: 0.66rem;
            line-height: 1.2;
            overflow-x: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        body:has(.chat-topbar) [data-testid="stHeader"]::before {
            content: "" !important;
            display: none !important;
        }
        .chat-topbar {
            position: fixed;
            inset: 0 5.25rem auto calc(clamp(20rem, 20vw, 25.5rem) + 1rem);
            height: 3.75rem;
            z-index: 2147483000;
            display: flex;
            align-items: center;
            gap: 0.62rem;
            box-sizing: border-box;
            padding: 0 0 0 1.1rem;
            pointer-events: none;
        }
        .chat-topbar-title {
            flex: 0 0 auto;
            color: #111827;
            font-size: 0.96rem;
            font-weight: 780;
            line-height: 1;
            letter-spacing: 0;
            white-space: nowrap;
        }
        .chat-topbar .session-strip {
            flex: 1 1 18rem;
            max-width: 38rem;
            pointer-events: auto;
        }
        .chat-topbar-reset {
            pointer-events: auto;
            box-sizing: border-box;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            height: 2.14rem;
            min-height: 2.14rem;
            padding: 0 0.92rem;
            border: 1px solid #d8dee8;
            border-radius: 8px;
            background: #ffffff;
            color: #1f2937 !important;
            font-size: 0.73rem;
            font-weight: 650;
            line-height: 1;
            text-decoration: none !important;
            white-space: nowrap;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
        }
        .chat-topbar-reset:hover {
            border-color: #b9c3d4;
            background: #f8fafc;
            color: #111827 !important;
            text-decoration: none !important;
        }
        .chat-topbar-spacer {
            height: 2.85rem;
        }
        @media (max-width: 980px) {
            .chat-topbar {
                inset: 3.05rem 1rem auto 1rem;
                height: 3.2rem;
                padding-left: 0;
            }
            .chat-topbar-title { display: none; }
            .chat-topbar .session-strip { max-width: none; }
            .chat-topbar-spacer { height: 3.4rem; }
        }
        .chat-table-meta {
            color: #5b667a;
            font-size: 0.76rem;
            font-weight: 680;
            line-height: 1.35;
            margin: 0.75rem 0 0.28rem;
            letter-spacing: 0;
        }
        [data-testid="stSidebar"] .small-note {
            color: #667085;
            font-size: 0.7rem;
            line-height: 1.42;
            margin: 0.2rem 0 0.52rem;
        }
        [data-testid="stSidebar"] div[data-testid="stMarkdownContainer"] p {
            color: #667085;
            font-size: 0.72rem;
            line-height: 1.45;
            margin-bottom: 0.35rem;
        }
        .config-list { display: flex; flex-direction: column; gap: 0.36rem; margin: 0.38rem 0 0.55rem; }
        .config-row {
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            align-items: center;
            justify-content: space-between;
            gap: 0.42rem;
            min-height: 2.35rem;
            padding: 0.44rem 0.5rem;
            border: 1px solid #e4e7ec;
            border-radius: 7px;
            background: #ffffff;
            overflow-x: hidden;
        }
        .config-meta { min-width: 0; }
        .config-label { color: #344054; font-size: 0.72rem; font-weight: 700; line-height: 1.2; }
        .config-env {
            display: block;
            max-width: 100%;
            color: #98a2b3;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
            font-size: 0.56rem;
            line-height: 1.2;
            margin-top: 0.1rem;
            overflow-x: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .config-data {
            display: flex;
            align-items: center;
            justify-content: flex-end;
            min-width: 0;
            max-width: 100%;
            overflow-x: hidden;
            text-align: right;
        }
        .config-value {
            display: block;
            width: 100%;
            max-width: 100%;
            color: #101828;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
            font-size: 0.66rem;
            line-height: 1.25;
            overflow-x: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .config-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 1.28rem;
            box-sizing: border-box;
            border-radius: 999px;
            padding: 0.14rem 0.42rem;
            font-size: 0.62rem;
            font-weight: 700;
            line-height: 1;
            white-space: nowrap;
        }
        .config-badge.ok { color: #067647; background: #ecfdf3; border: 1px solid #abefc6; }
        .config-badge.missing { color: #b42318; background: #fef3f2; border: 1px solid #fecdca; }
        .active-scope-panel {
            margin: 0.18rem 0 0.58rem;
            padding: 0.62rem 0.66rem;
            border: 1px solid #d9e0ea;
            border-radius: 8px;
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
        }
        .active-scope-panel.empty {
            background: #f9fafb;
            border-style: dashed;
            box-shadow: none;
        }
        .active-scope-header {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 0.5rem;
            margin-bottom: 0.46rem;
        }
        .active-scope-kicker {
            color: #667085;
            font-size: 0.58rem;
            font-weight: 760;
            letter-spacing: 0.06em;
            line-height: 1.1;
            text-transform: uppercase;
        }
        .active-scope-title {
            color: #101828;
            font-size: 0.82rem;
            font-weight: 760;
            line-height: 1.18;
            margin-top: 0.1rem;
        }
        .active-scope-state {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 1.24rem;
            padding: 0.12rem 0.4rem;
            border-radius: 999px;
            color: #175cd3;
            background: #eff8ff;
            border: 1px solid #b2ddff;
            font-size: 0.6rem;
            font-weight: 760;
            line-height: 1;
            white-space: nowrap;
        }
        .active-scope-panel.empty .active-scope-state {
            color: #667085;
            background: #f2f4f7;
            border-color: #e4e7ec;
        }
        .active-scope-datasets {
            color: #344054;
            font-size: 0.68rem;
            font-weight: 680;
            line-height: 1.28;
            margin-bottom: 0.42rem;
            word-break: keep-all;
        }
        .active-scope-empty-text {
            color: #667085;
            font-size: 0.68rem;
            line-height: 1.42;
            margin-top: 0.08rem;
        }
        .active-scope-chip-list {
            display: flex;
            flex-direction: column;
            gap: 0.28rem;
        }
        .active-scope-chip {
            display: grid;
            grid-template-columns: minmax(3.7rem, 0.6fr) minmax(0, 1fr);
            gap: 0.38rem;
            align-items: start;
            min-height: 1.72rem;
            padding: 0.32rem 0.42rem;
            border: 1px solid #e4e7ec;
            border-radius: 7px;
            background: #f8fafc;
        }
        .active-scope-chip-label {
            color: #475467;
            font-size: 0.62rem;
            font-weight: 740;
            line-height: 1.18;
            overflow-x: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .active-scope-chip-value {
            color: #101828;
            font-size: 0.64rem;
            font-weight: 680;
            line-height: 1.22;
            text-align: right;
            overflow-wrap: anywhere;
        }
        .active-scope-chip-context {
            grid-column: 1 / -1;
            color: #98a2b3;
            font-size: 0.56rem;
            line-height: 1.12;
            margin-top: -0.1rem;
        }
        .active-scope-footer {
            margin-top: 0.42rem;
            padding-top: 0.4rem;
            border-top: 1px solid #edf1f7;
            color: #667085;
            font-size: 0.62rem;
            font-weight: 650;
            line-height: 1.3;
        }
        .sidebar-brand {
            margin: 0 0 0.65rem;
            padding: 0.62rem 0.66rem;
            border: 1px solid #e4e7ec;
            border-radius: 9px;
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
        }
        .sidebar-brand-row {
            display: flex;
            align-items: center;
            gap: 0.52rem;
        }
        .sidebar-brand-mark {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
            width: 1.72rem;
            height: 1.72rem;
            border-radius: 7px;
            color: #ffffff;
            background: #1f2a44;
            font-size: 0.68rem;
            font-weight: 800;
            letter-spacing: 0;
        }
        .sidebar-brand-title {
            color: #111827;
            font-size: 0.88rem;
            font-weight: 760;
            line-height: 1.14;
        }
        .sidebar-brand-subtitle {
            color: #667085;
            font-size: 0.66rem;
            line-height: 1.25;
            margin-top: 0.1rem;
            white-space: nowrap;
        }
        .sidebar-section-label {
            color: #667085;
            font-size: 0.64rem;
            font-weight: 760;
            letter-spacing: 0.06em;
            margin: 0.45rem 0 0.24rem;
            text-transform: uppercase;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] {
            display: flex;
            flex-direction: column;
            gap: 0.16rem;
            margin-bottom: 0.7rem;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label {
            display: flex !important;
            align-items: center !important;
            width: 100%;
            min-height: 2rem;
            margin: 0 !important;
            padding: 0.34rem 0.5rem !important;
            border: 1px solid transparent;
            border-radius: 7px;
            background: transparent;
            transition: background 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label:hover {
            background: #ffffff;
            border-color: #e4e7ec;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
            background: #ffffff;
            border-color: #cfd8e6;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label div[data-testid="stMarkdownContainer"] {
            margin: 0 !important;
            width: 100%;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label div[data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] div[role="radiogroup"] label p {
            display: flex !important;
            align-items: center !important;
            gap: 0.42rem !important;
            color: #475467;
            font-size: 0.78rem !important;
            font-weight: 650 !important;
            line-height: 1.12 !important;
            margin: 0 !important;
            white-space: nowrap;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label div[data-testid="stMarkdownContainer"] p::before {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 1rem;
            height: 1rem;
            border-radius: 5px;
            color: #475467;
            background: #eef2f7;
            border: 1px solid #d9e0ea;
            font-size: 0.58rem;
            font-weight: 800;
            line-height: 1;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(1) div[data-testid="stMarkdownContainer"] p::before { content: "◔"; }
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(2) div[data-testid="stMarkdownContainer"] p::before { content: "◇"; }
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(3) div[data-testid="stMarkdownContainer"] p::before { content: "↧"; }
        [data-testid="stSidebar"]:has(.developer-nav-enabled) div[role="radiogroup"] label:nth-of-type(4) div[data-testid="stMarkdownContainer"] p::before { content: "J"; }
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {
            color: #101828;
            font-weight: 740 !important;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) div[data-testid="stMarkdownContainer"] p::before {
            color: #ffffff;
            background: #1f2a44;
            border-color: #1f2a44;
        }
        [data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {
            display: none;
        }
        [data-testid="stSidebar"] details {
            border-radius: 8px !important;
            margin-bottom: 0.44rem !important;
        }
        [data-testid="stSidebar"] details summary {
            min-height: 2.15rem !important;
            padding: 0.42rem 0.58rem !important;
            font-size: 0.82rem !important;
        }
        [data-testid="stSidebar"] details summary p {
            font-size: 0.82rem !important;
            line-height: 1.18 !important;
            margin: 0 !important;
        }
        [data-testid="stSidebar"] details [data-testid="stExpanderDetails"],
        [data-testid="stSidebar"] details > div:not(summary) {
            padding: 0.58rem 0.62rem 0.64rem !important;
        }
        [data-testid="stSidebar"] div[data-testid="stButton"] button {
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            min-height: 1.88rem !important;
            padding: 0.3rem 0.52rem !important;
            border-radius: 7px !important;
            font-size: 0.7rem !important;
            font-weight: 620 !important;
            line-height: 1 !important;
            border-color: #cfd8e6 !important;
            color: #344054 !important;
            background: #ffffff !important;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04) !important;
        }
        [data-testid="stSidebar"] div[data-testid="stButton"] button p {
            color: #344054 !important;
            font-size: 0.7rem !important;
            font-weight: 620 !important;
            line-height: 1 !important;
            margin: 0 !important;
        }
        [data-testid="stSidebar"] button[data-testid="stBaseButton-secondary"] div[data-testid="stMarkdownContainer"] p {
            color: #344054 !important;
            font-size: 0.7rem !important;
            font-weight: 620 !important;
            line-height: 1 !important;
            margin: 0 !important;
        }
        [data-testid="stSidebar"] div[data-testid="stButton"] button:hover {
            border-color: #b9c5d6 !important;
            color: #111827 !important;
            background: #f9fafb !important;
        }
        [data-testid="stSidebar"] div[data-testid="stAlert"] {
            display: flex !important;
            align-items: center !important;
            min-height: 2.25rem !important;
            padding: 0.42rem 0.58rem !important;
            border-radius: 8px !important;
        }
        [data-testid="stSidebar"] div[data-testid="stAlert"] [data-testid="stMarkdownContainer"],
        [data-testid="stSidebar"] div[data-testid="stAlert"] p {
            margin: 0 !important;
            line-height: 1.15 !important;
            font-size: 0.74rem !important;
        }
        [data-testid="stSidebar"] div[data-testid="stToggle"],
        [data-testid="stSidebar"] div[data-testid="stCheckbox"] {
            margin: 0.34rem 0 0.48rem !important;
        }
        [data-testid="stSidebar"] div[data-testid="stToggle"] label,
        [data-testid="stSidebar"] div[data-testid="stCheckbox"] label {
            min-height: 1.7rem !important;
            gap: 0.42rem !important;
        }
        [data-testid="stSidebar"] div[data-testid="stCheckbox"] label > div:first-child {
            transform: scale(0.78);
            transform-origin: left center;
            margin-right: -0.18rem;
        }
        [data-testid="stSidebar"] div[data-testid="stToggle"] label p,
        [data-testid="stSidebar"] div[data-testid="stCheckbox"] label p,
        [data-testid="stSidebar"] div[data-testid="stWidgetLabel"] p {
            color: #344054 !important;
            font-size: 0.78rem !important;
            font-weight: 650 !important;
            line-height: 1.18 !important;
            margin: 0 !important;
        }
        [data-testid="stSidebar"] div[data-testid="stWidgetLabel"] {
            min-height: 1.2rem !important;
            margin-bottom: 0.28rem !important;
        }
        [data-testid="stSidebar"] div[data-testid="stWidgetLabel"] button {
            width: 1rem !important;
            height: 1rem !important;
            padding: 0 !important;
        }
        [data-testid="stSidebar"] div[data-testid="stSelectbox"] {
            margin-bottom: 0.62rem !important;
        }
        [data-testid="stSidebar"] div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
            height: 2.05rem !important;
            min-height: 2.05rem !important;
            border-radius: 7px !important;
            border-color: #cfd8e6 !important;
            background: #ffffff !important;
            box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04) !important;
        }
        [data-testid="stSidebar"] div[data-testid="stSelectbox"] div[data-baseweb="select"] * {
            font-size: 0.78rem !important;
            line-height: 1.15 !important;
        }
        div[data-baseweb="popover"] ul[role="listbox"],
        div[data-baseweb="popover"] div[role="listbox"] {
            padding: 0.28rem !important;
            border-radius: 8px !important;
        }
        div[data-baseweb="popover"] li[role="option"],
        div[data-baseweb="popover"] div[role="option"] {
            height: 1.9rem !important;
            min-height: 1.9rem !important;
            padding: 0.28rem 0.5rem !important;
            border-radius: 6px !important;
            box-sizing: border-box !important;
            font-size: 0.7rem !important;
            line-height: 1.12 !important;
        }
        div[data-baseweb="popover"] li[role="option"] *,
        div[data-baseweb="popover"] div[role="option"] * {
            font-size: 0.7rem !important;
            line-height: 1.12 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
