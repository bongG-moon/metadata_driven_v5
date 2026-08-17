from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from importlib.machinery import ModuleSpec
import sys
import types

import pytest

from component_test_support import ROOT, load_module


@contextmanager
def _isolated_lfx_stubs():
    """Load standalone sources against local stubs without replacing real LFX globally."""

    class Component:
        pass

    class InputBase:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Data:
        def __init__(self, data=None):
            self.data = data or {}

    class Message:
        def __init__(self, text="", files=None):
            self.text = text
            self.files = list(files or [])
            self.error = False
            self.category = "message"

    module_names = (
        "lfx",
        "lfx.custom",
        "lfx.custom.custom_component",
        "lfx.custom.custom_component.component",
        "lfx.io",
        "lfx.schema",
        "lfx.schema.data",
        "lfx.schema.message",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    modules = {name: types.ModuleType(name) for name in module_names}
    for name, module in modules.items():
        is_package = name in {"lfx", "lfx.custom", "lfx.custom.custom_component", "lfx.schema"}
        module.__spec__ = ModuleSpec(name, loader=None, is_package=is_package)
        if is_package:
            module.__path__ = []
        sys.modules[name] = module
    modules["lfx.custom.custom_component.component"].Component = Component
    for name in (
        "BoolInput",
        "DataInput",
        "DropdownInput",
        "HandleInput",
        "IntInput",
        "MessageTextInput",
        "ModelInput",
        "MultilineInput",
        "Output",
        "SecretStrInput",
        "SliderInput",
        "StrInput",
    ):
        setattr(modules["lfx.io"], name, InputBase)
    modules["lfx.schema.data"].Data = Data
    modules["lfx.schema.message"].Message = Message
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


FLOW_ROOT = ROOT / "langflow_components" / "report_followup_flow"
with _isolated_lfx_stubs():
    context_publisher = load_module(ROOT / "langflow_components" / "realtime_production_report_flow" / "00e_report_context_publisher.py")
    prompt_builder = load_module(FLOW_ROOT / "00_report_followup_prompt_builder.py")
    guarded_plan_router = load_module(FLOW_ROOT / "00b_report_followup_guarded_plan_router.py")
    plan_normalizer = load_module(FLOW_ROOT / "01_report_followup_plan_normalizer.py")
    executor = load_module(FLOW_ROOT / "02_report_snapshot_executor.py")
    response_builder = load_module(FLOW_ROOT / "03_report_followup_response_builder.py")
    terminal = load_module(FLOW_ROOT / "04_report_followup_api_terminal.py")
    result_loader = load_module(ROOT / "langflow_components" / "data_analysis_flow" / "05_mongodb_result_loader.py")
    session_writer = load_module(ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py")


PRODUCT_COLUMNS = [
    "MODE",
    "DENSITY",
    "TECH",
    "ORG",
    "PKG1",
    "PKG2",
    "LEAD",
    "MCP_NO",
    "PRODUCTION",
    "OUT_PLAN",
    "생산실적달성율",
    "달성율*판정",
]


def _query_source(
    *,
    alias: str,
    purpose: str,
    aliases: list[str],
    columns: list[str],
    grain_kind: str,
    grain_columns: list[str],
    predicates: list[dict] | None = None,
) -> dict:
    return {
        "contract_version": "report.query_source.v1",
        "source_alias": alias,
        "dataset_key": f"dataset_{alias}",
        "purpose": purpose,
        "aliases": aliases,
        "authoritative": True,
        "columns": columns,
        "grain": {"kind": grain_kind, "columns": grain_columns},
        "metrics": [
            {"key": "achievement_rate", "column": "생산실적달성율", "method": "derived"}
        ]
        if "생산실적달성율" in columns
        else [],
        "predicates": predicates or [],
        "allowed_operations": ["filter", "sort", "top_n", "select"],
        "default_display_columns": columns[:10],
    }


def _state(query_sources: list[dict] | None = None, *, revision: int | None = 7, context_ref: str = "result:report-session:context-A") -> dict:
    raw = _query_source(
        alias="report_snapshot",
        purpose="case_detail",
        aliases=["Report 원본", "Report 상세", "원본 판정 데이터", "케이스 상세"],
        columns=[*PRODUCT_COLUMNS, "현재작업재공"],
        grain_kind="case",
        grain_columns=["MODE", "MCP_NO"],
    )
    shortage = _query_source(
        alias="report_shortage_products",
        purpose="production_shortage_products",
        aliases=["생산부족 제품", "생산 부족 제품", "부족 제품"],
        columns=PRODUCT_COLUMNS,
        grain_kind="product",
        grain_columns=["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"],
        predicates=[{"column": "달성율*판정", "operator": "eq", "value": "생산부족"}],
    )
    sources = query_sources or [raw, shortage]
    state = {
        "session_id": "report-session",
        "last_question": "D/A 공정그룹 실시간 생산 분석을 해줘",
        "current_data": {
            "row_count": 40,
            "columns": raw["columns"],
            "source_aliases": [item["source_alias"] for item in sources],
            "source_dataset_keys": [item["dataset_key"] for item in sources],
            "source_columns_by_alias": {item["source_alias"]: item["columns"] for item in sources},
            "data_ref": {"ref_id": context_ref},
            "query_sources": deepcopy(sources),
            "report_context": {
                "context_version": "report.context.v1",
                "context_ref": context_ref,
                "report_type": "realtime_production",
                "as_of": "2026-08-16T09:00:00+09:00",
                "expires_at": "2099-01-01T00:00:00+00:00",
                "query_sources": deepcopy(sources),
            },
        },
        "followup_source_results": [
            {
                "source_alias": item["source_alias"],
                "dataset_key": item["dataset_key"],
                "columns": item["columns"],
                "data_ref": {"ref_id": context_ref, "source_alias": item["source_alias"]},
            }
            for item in sources
        ],
        "runtime_source_refs": {
            item["source_alias"]: {"ref_id": context_ref, "source_alias": item["source_alias"]}
            for item in sources
        },
    }
    if revision is not None:
        state["_session_state_revision"] = revision
    return state


def _loaded_state(state: dict | None = None) -> dict:
    loaded = deepcopy(state or _state())
    return {
        "state": loaded,
        "session_state_load": {
            "loaded": True,
            "source": "mongodb",
            "turn_count": loaded.get("_session_state_revision", 7),
        },
    }


def _product_rows() -> list[dict]:
    rates = [70.0, 55.0, None, 80.0, 65.0, 60.0]
    return [
        {
            "MODE": "MASS",
            "DENSITY": "16G",
            "TECH": "1A",
            "ORG": "X4",
            "PKG1": "FBGA",
            "PKG2": "STD",
            "LEAD": "A-663",
            "MCP_NO": f"MCP-{index + 1}",
            "PRODUCTION": 500 + index,
            "OUT_PLAN": 1000,
            "생산실적달성율": rate,
            "달성율*판정": "생산부족",
        }
        for index, rate in enumerate(rates)
    ]


def _loaded_source_payload(payload: dict, source_alias: str, rows: list[dict], contract: dict | None = None) -> dict:
    result = deepcopy(payload)
    source = next(item for item in result["report_followup"]["query_sources"] if item["source_alias"] == source_alias)
    stored_contract = deepcopy(contract or source)
    result["runtime_sources"] = {source_alias: deepcopy(rows)}
    result["source_results"] = [
        {
            "source_alias": source_alias,
            "dataset_key": source["dataset_key"],
            "source_type": "mongodb_result_store",
            "status": "ok",
            "success": True,
            "row_count": len(rows),
            "columns": source["columns"],
            "query_source_contract": stored_contract,
        }
    ]
    return result


def _exact_plan_response() -> dict:
    return {
        "status": "ready",
        "source_alias": "report_shortage_products",
        "operations": [
            {"operation": "sort", "column": "생산실적달성율", "direction": "asc", "nulls": "last"},
            {"operation": "top_n", "limit": 5},
            {
                "operation": "select",
                "columns": [
                    "MODE",
                    "DENSITY",
                    "TECH",
                    "ORG",
                    "PKG1",
                    "PKG2",
                    "LEAD",
                    "MCP_NO",
                    "생산실적달성율",
                ],
            },
        ],
        "reason": "Report가 제공한 생산부족 제품 View를 달성율 오름차순으로 정렬합니다.",
    }


def _state_with_current_shortage_view() -> dict:
    state = _state()
    exact = _exact_plan_response()
    state["current_data"]["current_view_plan"] = {
        "contract_version": "report.followup.plan.v1",
        "source_alias": "report_shortage_products",
        "dataset_key": "dataset_report_shortage_products",
        "source_view_key": "production_shortage_products",
        "operations": deepcopy(exact["operations"]),
        "output_columns": deepcopy(exact["operations"][-1]["columns"]),
    }
    return state


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        ("blocked", "report_context_missing"),
        ("blocked", "report_context_expired"),
        ("handoff_required", "live_retrieval_required"),
        ("handoff_required", "cross_source_retrieval_required"),
        ("clarification_required", "report_query_source_ambiguous"),
    ],
)
def test_guarded_plan_router_skips_model_for_every_non_ready_upstream_state(status: str, error_type: str):
    payload = {
        "report_followup": {"status": status},
        "trace": {"errors": [{"type": error_type, "message": f"blocked by {error_type}"}]},
    }
    calls: list[str] = []

    def invoke(prompt: str):
        calls.append(prompt)
        raise AssertionError("non-ready report follow-up must not invoke the model")

    text, trace = guarded_plan_router.route_report_followup_plan_response(payload, "", invoke)
    routed = guarded_plan_router.json.loads(text)

    assert calls == []
    assert trace["model_called"] is False
    assert trace["plan_llm_skipped"] is True
    assert routed == {
        "status": status,
        "source_alias": "",
        "operations": [],
        "reason": f"blocked by {error_type}",
    }
    normalized = plan_normalizer.normalize_report_followup_plan(payload, text)
    assert normalized["execution_gate"]["status"] == status


def test_guarded_plan_router_calls_model_exactly_once_for_ready_payload():
    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 생산부족 제품을 생산실적달성율이 낮은 순으로 5개 보여줘",
        _loaded_state(),
    )
    prompt = prompt_builder.build_report_followup_prompt(request)
    calls: list[str] = []

    def invoke(value: str):
        calls.append(value)
        return _exact_plan_response()

    text, trace = guarded_plan_router.route_report_followup_plan_response(request, prompt, invoke)

    assert calls == [prompt.strip()]
    assert trace["model_called"] is True
    assert trace["plan_llm_skipped"] is False
    normalized = plan_normalizer.normalize_report_followup_plan(request, text)
    assert normalized["execution_gate"]["status"] == "ready"
    assert normalized["intent_plan"]["retrieval_jobs"] == []


def test_exact_failed_question_uses_materialized_product_view_without_retrieval_or_global_aliases():
    question = "방금 Report에서 생산부족 제품을 생산실적달성율이 낮은 순으로 5개 보여줘"
    request = prompt_builder.build_report_followup_request(question, _loaded_state())
    prompt = prompt_builder.build_report_followup_prompt(request)

    assert request["report_followup"]["status"] == "ready"
    assert request["report_followup"]["candidate_source_aliases"] == ["report_shortage_products"]
    assert "report_shortage_products" in prompt
    assert "report_snapshot" not in prompt
    assert '"DEN"' not in prompt
    assert '"PKG_TYPE1"' not in prompt

    normalized = plan_normalizer.normalize_report_followup_plan(request, _exact_plan_response())
    plan = normalized["intent_plan"]
    assert plan["retrieval_jobs"] == []
    assert plan["reuse_strategy"] == "previous_source"
    assert plan["resolved_execution_graph"]["external_source_requirements"] == [
        {
            "kind": "external_source",
            "provider": "previous_source",
            "source_alias": "report_shortage_products",
            "dataset_key": "dataset_report_shortage_products",
        }
    ]
    assert result_loader._requested_source_aliases(normalized) == ["report_shortage_products"]
    assert result_loader._report_context_ref(normalized) == "result:report-session:context-A"

    executed = executor.execute_report_snapshot(
        _loaded_source_payload(normalized, "report_shortage_products", _product_rows())
    )
    rows = executed["_full_result_rows"]
    assert executed["analysis"]["status"] == "ok"
    assert executed["analysis"]["matched_row_count"] == 6
    assert len(rows) == 5
    assert [row["생산실적달성율"] for row in rows] == [55.0, 60.0, 65.0, 70.0, 80.0]
    assert len({row["MCP_NO"] for row in rows}) == 5
    assert all(row.get("달성율*판정") is None for row in rows)  # select contract intentionally omits it

    response = response_builder.build_report_followup_response(executed, 10)
    assert response["status"] == "ok"
    assert response["session_state_guard"] == {
        "expected_turn_count": 7,
        "expected_report_context_ref": "result:report-session:context-A",
    }
    assert response["state"]["current_data"]["report_context"]["context_ref"] == "result:report-session:context-A"
    assert response["state"]["current_data"]["current_view_plan"]["source_alias"] == "report_shortage_products"
    assert "총 6건" in response["message"]
    assert "5건을 표시" in response["message"]


def test_second_turn_inherits_the_previous_top_five_subset_before_applying_top_three():
    first_request = prompt_builder.build_report_followup_request(
        "방금 Report에서 생산부족 제품을 생산실적달성율이 낮은 순으로 5개 보여줘",
        _loaded_state(),
    )
    first_plan = plan_normalizer.normalize_report_followup_plan(first_request, _exact_plan_response())
    first_execution = executor.execute_report_snapshot(
        _loaded_source_payload(first_plan, "report_shortage_products", _product_rows())
    )
    first_response = response_builder.build_report_followup_response(first_execution)
    second_state = deepcopy(first_response["state"])
    second_state["_session_state_revision"] = 8

    second_request = prompt_builder.build_report_followup_request(
        "그중 3개만 보여줘",
        _loaded_state(second_state),
    )
    assert second_request["report_followup"]["candidate_source_aliases"] == ["report_shortage_products"]
    assert second_request["report_followup"]["inherit_current_view"] is True
    second_plan = plan_normalizer.normalize_report_followup_plan(
        second_request,
        {
            "status": "ready",
            "operations": [{"operation": "top_n", "limit": 3}],
            "reason": "직전 결과 중 3개만 유지합니다.",
        },
    )

    operations = second_plan["intent_plan"]["report_execution_plan"]["operations"]
    assert [item["operation"] for item in operations] == ["sort", "top_n", "top_n", "select"]
    assert [item["limit"] for item in operations if item["operation"] == "top_n"] == [5, 3]
    assert second_plan["intent_plan"]["output_contract"]["limit"] == 3
    assert second_plan["intent_plan"]["retrieval_jobs"] == []
    assert result_loader._requested_source_aliases(second_plan) == ["report_shortage_products"]
    assert result_loader._report_context_ref(second_plan) == "result:report-session:context-A"

    second_execution = executor.execute_report_snapshot(
        _loaded_source_payload(second_plan, "report_shortage_products", _product_rows())
    )
    assert [row["생산실적달성율"] for row in second_execution["_full_result_rows"]] == [55.0, 60.0, 65.0]
    assert second_execution["analysis"]["matched_row_count"] == 5
    second_response = response_builder.build_report_followup_response(second_execution)
    assert second_response["session_state_guard"] == {
        "expected_turn_count": 8,
        "expected_report_context_ref": "result:report-session:context-A",
    }
    assert second_response["state"]["current_data"]["report_context"]["context_ref"] == "result:report-session:context-A"
    assert second_response["state"]["current_data"]["current_view_plan"]["source_alias"] == "report_shortage_products"


@pytest.mark.parametrize("question", ["방금 Report에서 3개만 보여줘", "이 Report에서 3개만 보여줘"])
def test_report_anchor_reference_without_a_view_alias_resets_to_case_detail(question: str):
    request = prompt_builder.build_report_followup_request(question, _loaded_state(_state_with_current_shortage_view()))

    assert request["report_followup"]["candidate_source_aliases"] == ["report_snapshot"]
    assert request["report_followup"]["inherit_current_view"] is False


@pytest.mark.parametrize("question", ["위 결과에서 3개만 보여줘", "해당 결과에서 3개만 보여줘"])
def test_result_reference_markers_select_the_previous_view_for_inheritance(question: str):
    request = prompt_builder.build_report_followup_request(question, _loaded_state(_state_with_current_shortage_view()))

    assert request["report_followup"]["candidate_source_aliases"] == ["report_shortage_products"]
    assert request["report_followup"]["inherit_current_view"] is True


def test_inherited_and_current_operations_cannot_exceed_the_combined_limit():
    state = _state_with_current_shortage_view()
    projection = deepcopy(state["current_data"]["current_view_plan"]["operations"][-1])
    state["current_data"]["current_view_plan"]["operations"] = [
        {"operation": "sort", "column": "생산실적달성율", "direction": "asc", "nulls": "last"}
        for _ in range(15)
    ] + [projection]
    request = prompt_builder.build_report_followup_request("그중 3개만 보여줘", _loaded_state(state))

    normalized = plan_normalizer.normalize_report_followup_plan(
        request,
        {"status": "ready", "operations": [{"operation": "top_n", "limit": 3}]},
    )

    assert normalized["execution_gate"]["status"] == "blocked"
    assert "report_followup_operation_limit_exceeded" in {
        item["type"] for item in normalized["trace"]["errors"]
    }


def test_arbitrary_report_physical_columns_work_without_table_catalog_mapping():
    custom = _query_source(
        alias="custom_risk_items",
        purpose="risk_items",
        aliases=["위험 항목"],
        columns=["품목코드", "보고서점수", "현장판정"],
        grain_kind="item",
        grain_columns=["품목코드"],
    )
    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 위험 항목을 보고서점수가 높은 순으로 2개 보여줘",
        _loaded_state(_state([custom])),
    )
    normalized = plan_normalizer.normalize_report_followup_plan(
        request,
        {
            "status": "ready",
            "source_alias": "custom_risk_items",
            "operations": [
                {"operation": "sort", "column": "보고서점수", "direction": "desc"},
                {"operation": "top_n", "limit": 2},
                {"operation": "select", "columns": ["품목코드", "보고서점수"]},
            ],
        },
    )
    rows = [
        {"품목코드": "P1", "보고서점수": 12.5, "현장판정": "주의"},
        {"품목코드": "P2", "보고서점수": None, "현장판정": "정상"},
        {"품목코드": "P3", "보고서점수": 99.1, "현장판정": "위험"},
    ]
    executed = executor.execute_report_snapshot(_loaded_source_payload(normalized, "custom_risk_items", rows))

    assert executed["analysis"]["status"] == "ok"
    assert executed["data"]["columns"] == ["품목코드", "보고서점수"]
    assert [row["품목코드"] for row in executed["_full_result_rows"]] == ["P3", "P1"]


def test_prompt_builder_uses_generic_publisher_default_view_and_display_name():
    risk_view = _query_source(
        alias="shortage_equipment_risk_products",
        purpose="shortage_equipment_risk_products",
        aliases=["장비 위험 제품"],
        columns=["제품", "생산실적달성율", "장비교체판단", "필요장비대수"],
        grain_kind="product",
        grain_columns=["제품"],
    )
    risk_view.update(
        {
            "display_name": "생산부족 장비위험 제품",
            "default_view": True,
            "lineage": ["production_cases", "equipment_assign"],
        }
    )

    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 달성율이 낮은 순으로 5개만 보여줘",
        _loaded_state(_state([risk_view])),
    )
    prompt = prompt_builder.build_report_followup_prompt(request)

    assert request["report_followup"]["status"] == "ready"
    assert request["report_followup"]["candidate_source_aliases"] == ["shortage_equipment_risk_products"]
    assert '"display_name":"생산부족 장비위험 제품"' in prompt
    assert '"default_view":true' in prompt
    assert '"lineage":["production_cases","equipment_assign"]' in prompt


def test_flow10_accepts_a_query_contract_auto_generated_from_simple_report_data():
    question = types.SimpleNamespace(
        text="방금 Report에서 위험점수가 높은 순으로 1개 보여줘",
        session_id="report-session",
        data={"text": "방금 Report에서 위험점수가 높은 순으로 1개 보여줘", "session_id": "report-session"},
    )
    published = context_publisher.build_report_context_payload(
        question,
        report_data_value={
            "rows": [
                {"제품": "P-01", "위험점수": 91, "내부관리값": "secret"},
                {"제품": "P-02", "위험점수": 14, "내부관리값": "secret"},
            ],
            "report_columns": ["제품", "위험점수"],
        },
        report_title="장비 위험 Report",
        report_type="equipment_risk",
        view_label="장비 위험 제품",
    )
    source = published["source_results"][0]["query_source_contract"]
    request = prompt_builder.build_report_followup_request(question, _loaded_state(_state([source])))

    assert request["report_followup"]["status"] == "ready"
    assert request["report_followup"]["candidate_source_aliases"] == ["report_snapshot"]
    assert '"display_name":"장비 위험 제품"' in prompt_builder.build_report_followup_prompt(request)

    normalized = plan_normalizer.normalize_report_followup_plan(
        request,
        {
            "status": "ready",
            "source_alias": "report_snapshot",
            "operations": [
                {"operation": "sort", "column": "위험점수", "direction": "desc"},
                {"operation": "top_n", "limit": 1},
                {"operation": "select", "columns": ["제품", "위험점수"]},
            ],
        },
    )
    executed = executor.execute_report_snapshot(
        _loaded_source_payload(normalized, "report_snapshot", published["runtime_sources"]["report_snapshot"])
    )

    assert executed["analysis"]["status"] == "ok"
    assert executed["_full_result_rows"] == [{"제품": "P-01", "위험점수": 91}]


@pytest.mark.parametrize(
    "question",
    [
        "방금 Report를 현재 기준으로 다시 보여줘",
        "방금 Report를 현재 데이터로 계산해줘",
        "방금 Report를 지금 시점으로 보여줘",
        "방금 Report를 최신 데이터로 보여줘",
        "방금 Report를 다시 조회해줘",
        "방금 Report를 새로 조회해줘",
        "방금 Report를 다른 데이터와 비교해줘",
    ],
)
def test_explicit_freshness_or_cross_source_request_fails_closed(question: str):
    request = prompt_builder.build_report_followup_request(question, _loaded_state())

    assert request["report_followup"]["status"] == "handoff_required"
    assert request["execution_gate"]["status"] == "handoff_required"
    assert "live_retrieval_required" in {item["type"] for item in request["trace"]["errors"]}
    assert prompt_builder.build_report_followup_prompt(request) == ""


def test_current_work_wip_column_is_not_mistaken_for_a_freshness_cue():
    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 현재작업재공이 0인 제품을 보여줘",
        _loaded_state(),
    )

    assert request["report_followup"]["status"] == "ready"
    assert request["report_followup"]["candidate_source_aliases"] == ["report_snapshot"]
    normalized = plan_normalizer.normalize_report_followup_plan(
        request,
        {
            "status": "ready",
            "source_alias": "report_snapshot",
            "operations": [
                {
                    "operation": "filter",
                    "conditions": [{"column": "현재작업재공", "operator": "eq", "value": 0}],
                },
                {"operation": "select", "columns": ["MCP_NO", "현재작업재공"]},
            ],
        },
    )
    assert normalized["execution_gate"]["status"] == "ready"


def test_prompt_builder_requires_session_revision_for_optimistic_locking():
    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 생산부족 제품을 보여줘",
        _loaded_state(_state(revision=None)),
    )
    assert request["execution_gate"]["status"] == "blocked"
    assert "report_session_revision_missing" in {item["type"] for item in request["trace"]["errors"]}


def test_normalizer_blocks_undeclared_column_and_unsupported_groupby():
    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 생산부족 제품을 보여줘",
        _loaded_state(),
    )
    bad_column = plan_normalizer.normalize_report_followup_plan(
        request,
        {
            "status": "ready",
            "source_alias": "report_shortage_products",
            "operations": [{"operation": "sort", "column": "DEN", "direction": "asc"}],
        },
    )
    assert bad_column["execution_gate"]["status"] == "blocked"
    assert "report_followup_column_not_declared" in {item["type"] for item in bad_column["trace"]["errors"]}

    bad_groupby = plan_normalizer.normalize_report_followup_plan(
        request,
        {
            "status": "ready",
            "source_alias": "report_shortage_products",
            "operations": [{"operation": "groupby", "columns": ["MCP_NO"]}],
        },
    )
    assert bad_groupby["execution_gate"]["status"] == "blocked"
    assert "report_followup_operation_not_allowed" in {item["type"] for item in bad_groupby["trace"]["errors"]}


def test_executor_revalidates_authoritative_loaded_source_contract():
    request = prompt_builder.build_report_followup_request(
        "방금 Report에서 생산부족 제품을 생산실적달성율이 낮은 순으로 5개 보여줘",
        _loaded_state(),
    )
    normalized = plan_normalizer.normalize_report_followup_plan(request, _exact_plan_response())
    untrusted_contract = deepcopy(
        next(item for item in normalized["report_followup"]["query_sources"] if item["source_alias"] == "report_shortage_products")
    )
    untrusted_contract["authoritative"] = False
    executed = executor.execute_report_snapshot(
        _loaded_source_payload(normalized, "report_shortage_products", _product_rows(), untrusted_contract)
    )

    assert executed["analysis"]["status"] == "error"
    assert executed["execution_gate"]["reason"] == "report_followup_loaded_contract_invalid"


class _Client:
    def close(self):
        return None


class _Collection:
    def __init__(self, document: dict):
        self.document = deepcopy(document)
        self.replace_called = False

    def find_one(self, _query):
        return deepcopy(self.document)

    def replace_one(self, *_args, **_kwargs):
        self.replace_called = True
        raise AssertionError("stale guard must reject before replace")


def test_flow10_guard_prevents_a_newer_report_from_being_overwritten(monkeypatch):
    question = "방금 Report에서 생산부족 제품을 생산실적달성율이 낮은 순으로 5개 보여줘"
    request = prompt_builder.build_report_followup_request(question, _loaded_state())
    normalized = plan_normalizer.normalize_report_followup_plan(request, _exact_plan_response())
    executed = executor.execute_report_snapshot(
        _loaded_source_payload(normalized, "report_shortage_products", _product_rows())
    )
    response = response_builder.build_report_followup_response(executed)
    collection = _Collection(
        {
            "_id": "session_state:report-session",
            "session_id": "report-session",
            "turn_count": 8,
            "state": {
                "current_data": {
                    "report_context": {"context_ref": "result:report-session:newer-context"}
                }
            },
        }
    )
    monkeypatch.setattr(session_writer, "_connect_collection", lambda *_args: (_Client(), collection))

    written = session_writer.write_session_state(
        response,
        mongo_uri="mongodb://validation.invalid",
        mongo_database="datagov",
        session_collection_name="agent_v4_session_states",
        enabled="true",
    )

    assert written["session_state_write"]["guarded"] is True
    assert written["session_state_write"]["saved"] is False
    assert written["session_state_write"]["reason"] == "stale_session_state"
    assert collection.replace_called is False


def test_terminal_returns_compact_public_contract_without_state_or_sources():
    response = {
        "contract_version": "report.followup.response.v1",
        "response_type": "report_followup",
        "status": "ok",
        "success": True,
        "summary": "완료",
        "message": "### 답변\n완료",
        "answer_message": "### 답변\n완료",
        "data": {"row_count": 1, "columns": ["품목"], "rows": [{"품목": "P1"}]},
        "analysis": {"status": "ok"},
        "state": _state(),
        "runtime_sources": {"report_snapshot": [{"secret": "raw"}]},
        "session_state_guard": {"expected_turn_count": 7, "expected_report_context_ref": "context-A"},
        "session_state_write": {"saved": True, "guarded": True},
        "warnings": [],
        "errors": [],
    }
    public = terminal.public_report_followup_response(response)

    assert public["status"] == "ok"
    assert "state" not in public
    assert "runtime_sources" not in public
    assert "session_state_guard" not in public
    assert public["session_state_write"] == {"saved": True, "guarded": True}
