from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOW_ROOT = ROOT / "langflow_components" / "realtime_production_report_flow"
LEGACY_FLOW_ROOT = ROOT / "langflow_components" / "realtime_production_report_legacy_flow"
GENERATOR_PATH = FLOW_ROOT / "00_dummy_production_judgement_data.py"
CATALOG_PATH = FLOW_ROOT / "00a_process_group_catalog_loader.py"
PROMPT_PATH = FLOW_ROOT / "00b_process_group_selection_prompt.py"
GATE_PATH = FLOW_ROOT / "00c_process_group_selection_gate.py"
DETERMINISTIC_GATE_PATH = FLOW_ROOT / "00c_deterministic_process_group_selection_gate.py"
VIEW_BUNDLE_PATH = FLOW_ROOT / "00d_report_context_payload_builder.py"
CONTEXT_PUBLISHER_PATH = FLOW_ROOT / "00e_report_context_publisher.py"
BUILDER_PATH = FLOW_ROOT / "01_realtime_production_report_builder.py"
TERMINAL_PATH = FLOW_ROOT / "02_realtime_production_report_api_terminal.py"
LEGACY_BUILDER_PATH = LEGACY_FLOW_ROOT / "01_realtime_production_report_builder.py"
LEGACY_TERMINAL_PATH = LEGACY_FLOW_ROOT / "02_realtime_production_report_api_terminal.py"
RESULT_STORE_PATH = ROOT / "langflow_components" / "data_analysis_flow" / "23_mongodb_result_store.py"
SESSION_WRITER_PATH = ROOT / "langflow_components" / "session_state_flow" / "01_mongodb_session_state_writer.py"
SESSION_LOADER_PATH = ROOT / "langflow_components" / "session_state_flow" / "00_mongodb_session_state_loader.py"


def _install_lfx_stubs() -> None:
    # Use the real Langflow 1.11.0/LFX runtime when it is installed. Installing
    # module-level stubs during collection otherwise contaminates later router
    # tests in the same pytest process.
    if importlib.util.find_spec("lfx") is not None:
        return

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
        "lfx.services",
        "lfx.services.deps",
    )
    for name in module_names:
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["lfx.custom.custom_component.component"].Component = Component
    io_module = sys.modules["lfx.io"]
    for name in (
        "DataInput",
        "DropdownInput",
        "HandleInput",
        "IntInput",
        "MessageTextInput",
        "MultilineInput",
        "Output",
        "StrInput",
    ):
        setattr(io_module, name, InputBase)
    sys.modules["lfx.schema.data"].Data = Data
    sys.modules["lfx.schema.message"].Message = Message
    sys.modules["lfx.services.deps"].get_storage_service = lambda: None


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_lfx_stubs()
generator = _load("realtime_production_dummy_test", GENERATOR_PATH)
catalog = _load("realtime_production_process_group_catalog_test", CATALOG_PATH)
prompt = _load("realtime_production_process_group_prompt_test", PROMPT_PATH)
gate = _load("realtime_production_process_group_gate_test", GATE_PATH)
deterministic_gate = _load(
    "realtime_production_deterministic_process_group_gate_test",
    DETERMINISTIC_GATE_PATH,
)
view_bundle_builder = _load("realtime_production_report_view_bundle_test", VIEW_BUNDLE_PATH)
context_publisher = _load("realtime_production_report_context_publisher_test", CONTEXT_PUBLISHER_PATH)
builder = _load("realtime_production_report_builder_test", BUILDER_PATH)
terminal = _load("realtime_production_report_terminal_test", TERMINAL_PATH)
legacy_builder = _load("realtime_production_report_legacy_builder_test", LEGACY_BUILDER_PATH)
legacy_terminal = _load("realtime_production_report_legacy_terminal_test", LEGACY_TERMINAL_PATH)
result_store = _load("realtime_production_report_result_store_test", RESULT_STORE_PATH)
session_writer = _load("realtime_production_report_session_writer_test", SESSION_WRITER_PATH)
session_loader = _load("realtime_production_report_session_loader_test", SESSION_LOADER_PATH)


class Question:
    def __init__(self, text: str = "오늘 W/B 실시간 생산 분석 Report를 만들어줘"):
        self.text = text
        self.session_id = "session-report"
        self.data = {"text": text, "session_id": self.session_id}


# 함수 설명: Realtime Report Recipe Bundle과 공용 Publisher를 연결해 실제 Flow 07-1 Context payload를 만듭니다.
def _context_payload(dataset, question=None):
    request = question or Question()
    bundle = view_bundle_builder.build_realtime_report_view_bundle(dataset, request)
    return context_publisher.build_report_context_payload(request, bundle)


def _stored_report_context(dataset, *, session_id="session-report"):
    columns = list(dataset["columns"])
    question = Question()
    question.session_id = session_id
    question.data = {"text": question.text, "session_id": session_id}
    context_payload = _context_payload(dataset, question)
    result_ref = {
        "store": "mongodb",
        "ref_id": "result:session-report:context-1",
        "database": "datagov",
        "collection_name": "agent_v4_result_store",
        "path": "payload.result_rows",
        "role": "analysis_result",
        "label": "분석 결과 데이터",
        "row_count": dataset["row_count"],
        "columns": columns,
        "expires_at": "2099-07-27T18:30:00+09:00",
    }
    source_refs = []
    for source in context_payload["source_results"]:
        alias = source["source_alias"]
        source_refs.append(
            {
                "store": "mongodb",
                "ref_id": "result:session-report:context-1",
                "database": "datagov",
                "collection_name": "agent_v4_result_store",
                "path": f"payload.runtime_sources.{alias}",
                "role": "source_rows",
                "label": f"사용 원본 데이터: {alias}",
                "source_alias": alias,
                "dataset_key": source["dataset_key"],
                "source_type": source["source_type"],
                "row_count": source["row_count"],
                "columns": list(source["columns"]),
            }
        )
    return {
        "request": {"question": Question().text, "session_id": session_id},
        "data": {"row_count": dataset["row_count"], "columns": columns, "data_ref": result_ref},
        "source_results": deepcopy(context_payload["source_results"]),
        "data_refs": [result_ref, *source_refs],
        "trace": {
            "inspection": {
                "result_store": {
                    "status": "ok",
                    "data_ref": result_ref["ref_id"],
                    "errors": [],
                }
            }
        },
    }


def _multi_group_dataset():
    return generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        process_names=generator.DEFAULT_PROCESSES,
        snapshot_at="2026-07-27T14:30:00+09:00",
    )


PROCESS_GROUP_ITEMS = [
    {
        "_id": "domain:process_groups:WB",
        "section": "process_groups",
        "key": "WB",
        "status": "active",
        "payload": {
            "display_name": "W/B 공정 그룹",
            "aliases": ["WB", "W/B", "W/B 공정", "W/B 공정 그룹"],
            "field": "OPER_NAME",
            "processes": ["W/B1", "W/B2", "W/B3", "W/B4"],
        },
    },
    {
        "_id": "domain:process_groups:BG",
        "section": "process_groups",
        "key": "BG",
        "status": "active",
        "payload": {
            "display_name": "B/G 공정 그룹",
            "aliases": ["BG", "B/G", "B/G 공정", "B/G 공정 그룹"],
            "field": "OPER_NAME",
            "processes": ["B/G1", "B/G2", "B/G3"],
        },
    },
    {
        "_id": "domain:process_groups:DA",
        "section": "process_groups",
        "key": "DA",
        "status": "active",
        "payload": {
            "display_name": "D/A 공정 그룹",
            "aliases": ["DA", "D/A", "D/A 공정", "D/A 공정 그룹"],
            "field": "OPER_NAME",
            "processes": ["D/A1", "D/A2", "D/A3"],
        },
    },
]


def _process_group_catalog():
    class FakeCursor(list):
        def limit(self, value):
            assert value == 200
            return self

    class FakeCollection:
        def find(self, query):
            assert query == {"section": "process_groups", "status": "active"}
            return FakeCursor(PROCESS_GROUP_ITEMS)

    class FakeDatabase:
        def __getitem__(self, collection_name):
            assert collection_name == "agent_v4_domain_items"
            return FakeCollection()

    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            assert uri == "mongodb://fake"
            assert kwargs["serverSelectionTimeoutMS"] == 5000

        def __getitem__(self, database_name):
            assert database_name == "datagov"
            return FakeDatabase()

        def close(self):
            return None

    original_import_module = catalog.import_module
    catalog.import_module = lambda name: types.SimpleNamespace(MongoClient=FakeMongoClient)
    try:
        return catalog.load_process_group_catalog(mongo_uri="mongodb://fake")
    finally:
        catalog.import_module = original_import_module


def test_process_group_catalog_and_prompt_are_domain_grounded():
    result = _process_group_catalog()
    assert result["contract_version"] == "domain.process_group.catalog.v1"
    assert result["status"] == "ok"
    assert result["source_type"] == "mongodb"
    assert [item["key"] for item in result["process_groups"]] == ["BG", "DA", "WB"]
    assert next(item for item in result["process_groups"] if item["key"] == "WB")["processes"] == [
        "W/B1",
        "W/B2",
        "W/B3",
        "W/B4",
    ]

    text = prompt.build_process_group_selection_prompt(
        Question("W/B2 공정의 실시간 생산 분석을 해줘"),
        result,
    )
    assert "W/B2 공정의 실시간 생산 분석을 해줘" in text
    assert '"key": "WB"' in text
    assert "질문에 없는 그룹을 추천하거나 기본값으로 선택하지 않는다" in text


def test_process_group_catalog_component_exposes_only_mongodb_configuration():
    input_names = [
        item.kwargs["name"] if hasattr(item, "kwargs") else item.name
        for item in catalog.RealtimeProductionProcessGroupCatalogLoader.inputs
    ]
    assert input_names == [
        "mongo_uri",
        "mongo_database",
        "collection_name",
        "status_filter",
        "limit",
    ]
    assert "source_mode" not in input_names
    assert "inline_catalog_json" not in input_names


def test_process_group_gate_filters_selected_group_and_accepts_detail_process_evidence():
    dataset = _multi_group_dataset()
    selected = gate.select_process_group_dataset(
        question_value=Question("W/B2 공정의 실시간 생산 분석 Report를 만들어줘"),
        catalog_value=_process_group_catalog(),
        llm_response_value={
            "status": "selected",
            "process_group_key": "WB",
            "reason": "W/B2는 WB 그룹의 세부 공정입니다.",
            "evidence": ["W/B2"],
        },
        dataset_value=dataset,
    )

    assert selected["contract_version"] == "production.judgement.dataset.v1"
    assert selected["selected_process_group"]["key"] == "WB"
    assert selected["unfiltered_row_count"] == 500
    assert 0 < selected["row_count"] < 500
    assert {row["OPER_NAME"] for row in selected["rows"]} <= {"W/B1", "W/B2", "W/B3", "W/B4"}
    rows, warnings, error = builder._validate_dataset(selected)
    assert error is None
    analysis = builder.analyze_production_rows(rows, selected)
    document = builder.render_production_report_html(rows, analysis, warnings=warnings)
    assert analysis["scope"]["process_group"]["key"] == "WB"
    assert "공정그룹 · W/B 공정 그룹" in document


def test_process_group_gate_requires_explicit_single_group_even_if_llm_guesses():
    dataset = _multi_group_dataset()
    missing = gate.select_process_group_dataset(
        question_value=Question("실시간 생산 분석 Report를 만들어줘"),
        catalog_value=_process_group_catalog(),
        llm_response_value={
            "status": "selected",
            "process_group_key": "WB",
            "reason": "기본 그룹으로 추정",
            "evidence": [],
        },
        dataset_value=dataset,
    )
    assert missing["contract_version"] == "production.process_group.selection.v1"
    assert missing["status"] == "clarification_required"
    assert missing["success"] is True
    assert "공정그룹을 선택해 주세요" in missing["message"]
    assert "Report는 아직 생성하지 않았습니다" in missing["message"]
    assert "rows" not in missing

    ambiguous = gate.select_process_group_dataset(
        question_value=Question("W/B와 B/G 공정그룹을 분석해줘"),
        catalog_value=_process_group_catalog(),
        llm_response_value={
            "status": "ambiguous",
            "process_group_key": "",
            "reason": "두 그룹이 함께 언급됨",
            "evidence": ["W/B", "B/G"],
        },
        dataset_value=dataset,
    )
    assert ambiguous["status"] == "clarification_required"
    assert {item["key"] for item in ambiguous["matched_process_groups"]} == {"WB", "BG"}


def test_deterministic_process_group_gate_matches_legacy_selected_rows_without_llm():
    dataset = _multi_group_dataset()
    question = Question("W/B2 공정의 실시간 생산 분석 Report를 만들어줘")
    catalog_value = _process_group_catalog()
    legacy = gate.select_process_group_dataset(
        question_value=question,
        catalog_value=catalog_value,
        llm_response_value={
            "status": "selected",
            "process_group_key": "WB",
            "reason": "W/B2는 WB 그룹의 세부 공정입니다.",
            "evidence": ["W/B2"],
        },
        dataset_value=dataset,
    )
    selected = deterministic_gate.select_process_group_dataset(
        question_value=question,
        catalog_value=catalog_value,
        dataset_value=dataset,
    )

    assert selected["contract_version"] == "production.judgement.dataset.v1"
    assert selected["rows"] == legacy["rows"]
    assert selected["row_count"] == legacy["row_count"]
    assert selected["processes"] == legacy["processes"]
    assert selected["unfiltered_row_count"] == legacy["unfiltered_row_count"]
    assert selected["selected_process_group"]["key"] == "WB"
    assert selected["selected_process_group"]["question_evidence"] == ["W/B2"]
    assert set(selected["selected_process_group"]["question_evidence"]).issubset(
        set(legacy["selected_process_group"]["question_evidence"])
    )
    assert selected["selected_process_group"]["llm_reason"] == ""
    assert selected["selection_provenance"]["method"] == "deterministic_explicit_match"
    assert selected["selection_provenance"]["selected_key"] == "WB"


def test_deterministic_process_group_gate_clarifies_missing_ambiguous_and_colliding_aliases():
    dataset = _multi_group_dataset()
    catalog_value = _process_group_catalog()

    missing = deterministic_gate.select_process_group_dataset(
        question_value=Question("오늘 실시간 생산 분석을 해줘"),
        catalog_value=catalog_value,
        dataset_value=dataset,
    )
    assert missing["contract_version"] == "production.process_group.selection.v1"
    assert missing["status"] == "clarification_required"
    assert missing["success"] is True
    assert missing["llm_decision"] == {
        "status": "",
        "process_group_key": "",
        "reason": "",
        "evidence": [],
    }
    assert missing["selection_provenance"]["method"] == "deterministic_explicit_match"
    assert missing["selection_provenance"]["matched_group_count"] == 0
    assert "rows" not in missing

    ambiguous = deterministic_gate.select_process_group_dataset(
        question_value=Question("W/B와 B/G 공정그룹을 분석해줘"),
        catalog_value=catalog_value,
        dataset_value=dataset,
    )
    assert ambiguous["status"] == "clarification_required"
    assert {item["key"] for item in ambiguous["matched_process_groups"]} == {"WB", "BG"}
    assert ambiguous["selection_provenance"]["matched_group_count"] == 2

    collision_catalog = deepcopy(catalog_value)
    next(
        item
        for item in collision_catalog["process_groups"]
        if item["key"] == "BG"
    )["aliases"].append("W/B")
    collision = deterministic_gate.select_process_group_dataset(
        question_value=Question("W/B 공정그룹 실시간 생산 분석을 해줘"),
        catalog_value=collision_catalog,
        dataset_value=dataset,
    )
    assert collision["status"] == "clarification_required"
    assert {item["key"] for item in collision["matched_process_groups"]} == {"WB", "BG"}


def test_deterministic_process_group_gate_preserves_short_key_boundaries():
    groups = _process_group_catalog()["process_groups"]

    assert deterministic_gate.find_explicit_process_group_matches(
        "D/A1공정 실시간 생산 분석을 해줘",
        groups,
    ) == {"DA": ["D/A1"]}
    assert deterministic_gate.find_explicit_process_group_matches(
        "D/A10공정 실시간 생산 분석을 해줘",
        groups,
    ) == {}
    assert deterministic_gate.find_explicit_process_group_matches(
        "DAILY 생산 분석을 해줘",
        groups,
    ) == {}


def test_deterministic_process_group_gate_fails_closed_for_contract_schema_and_empty_group():
    catalog_value = _process_group_catalog()
    dataset = _multi_group_dataset()

    invalid_contract = deepcopy(dataset)
    invalid_contract["contract_version"] = "production.judgement.dataset.unknown"
    contract_error = deterministic_gate.select_process_group_dataset(
        question_value=Question("W/B 공정그룹 실시간 생산 분석을 해줘"),
        catalog_value=catalog_value,
        dataset_value=invalid_contract,
    )
    assert contract_error["status"] == "error"
    assert contract_error["errors"][0]["type"] == "invalid_production_judgement_dataset_contract"

    missing_field = deepcopy(dataset)
    missing_field["columns"] = [column for column in missing_field["columns"] if column != "OPER_NAME"]
    schema_error = deterministic_gate.select_process_group_dataset(
        question_value=Question("W/B 공정그룹 실시간 생산 분석을 해줘"),
        catalog_value=catalog_value,
        dataset_value=missing_field,
    )
    assert schema_error["status"] == "error"
    assert schema_error["errors"][0]["type"] == "missing_process_group_field"

    invalid_rows = deepcopy(dataset)
    invalid_rows["rows"] = [*invalid_rows["rows"], "invalid-row"]
    row_error = deterministic_gate.select_process_group_dataset(
        question_value=Question("W/B 공정그룹 실시간 생산 분석을 해줘"),
        catalog_value=catalog_value,
        dataset_value=invalid_rows,
    )
    assert row_error["status"] == "error"
    assert row_error["errors"][0]["type"] == "invalid_production_judgement_dataset_schema"

    empty_group = deepcopy(dataset)
    empty_group["rows"] = [row for row in empty_group["rows"] if row["OPER_NAME"].startswith("B/G")]
    empty_group["row_count"] = len(empty_group["rows"])
    empty_error = deterministic_gate.select_process_group_dataset(
        question_value=Question("W/B 공정그룹 실시간 생산 분석을 해줘"),
        catalog_value=catalog_value,
        dataset_value=empty_group,
    )
    assert empty_error["status"] == "error"
    assert empty_error["errors"][0]["type"] == "empty_selected_process_group_dataset"


def test_missing_process_group_returns_message_without_creating_html():
    selection = gate.select_process_group_dataset(
        question_value=Question("오늘 실시간 생산 분석을 해줘"),
        catalog_value=_process_group_catalog(),
        llm_response_value={
            "status": "missing",
            "process_group_key": "",
            "reason": "그룹 표현 없음",
            "evidence": [],
        },
        dataset_value=_multi_group_dataset(),
    )
    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=selection,
            question_value=Question("오늘 실시간 생산 분석을 해줘"),
            max_html_rows=1_000,
            report_api_url="https://reports.example.internal",
            report_ttl_hours=4,
            report_publisher_fn=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not publish")),
            file_token="fixed",
        )
    )

    assert result["contract_version"] == "realtime.production.report.v1"
    assert result["response_type"] == "realtime_production_process_group_clarification"
    assert result["status"] == "clarification_required"
    assert result["artifacts"] == []
    assert "공정그룹을 선택해 주세요" in result["message"]
    assert result["state"]["session_id"] == "session-report"
    assert "report_context" not in result["state"]["current_data"]
    invalidated = result["state"]["current_data"]["report_context_status"]
    assert invalidated["status"] == "invalidated"
    assert invalidated["reason"] == "process_group_not_selected"


def test_dummy_dataset_has_500_rows_and_all_expected_judgements():
    dataset = generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        process_names="W/B1,W/B2,W/B3,W/B4",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )

    assert dataset["contract_version"] == "production.judgement.dataset.v1"
    assert dataset["row_count"] == 500
    assert len(dataset["rows"]) == 500
    assert dataset["columns"] == generator.COLUMNS
    assert {row["달성율*판정"] for row in dataset["rows"]} == {
        "정상",
        "정상(초과생산)",
        "생산부족",
        "Abnormal",
    }
    assert {"정상", "Abnormal", "생산부진1", "생산부진2", "CAPA부족"} <= {
        row["CAPA이상판단"] for row in dataset["rows"]
    }
    assert {"정상", "교체불필요", "장비필요", "교체필요"} <= {
        row["장비교체판단"] for row in dataset["rows"]
    }


def test_analysis_counts_reconcile_and_preserve_multi_cause_flags():
    dataset = generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        process_names="W/B1,W/B2,W/B3,W/B4",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    rows, warnings, error = builder._validate_dataset(dataset)
    analysis = builder.analyze_production_rows(rows, dataset)

    assert error is None
    assert not [item for item in warnings if item["type"] == "unknown_judgement_value"]
    assert analysis["scope"]["case_count"] == 500
    assert analysis["scope"]["distinct_case_count"] == 500
    assert analysis["scope"]["process_count"] == 4
    assert sum(analysis["production"].values()) == 500
    assert sum(analysis["shortage"]["primary"].values()) == analysis["shortage"]["case_count"]
    assert analysis["shortage"]["multi_cause_count"] > 0
    assert analysis["shortage"]["flags"]["wip"] >= analysis["shortage"]["primary"]["wip"]
    assert analysis["capa"]["anomaly"] == sum(analysis["capa"]["detail"].values())
    assert (
        analysis["equipment"]["normal_no_change"]
        + analysis["equipment"]["장비필요"]
        + analysis["equipment"]["교체필요"]
        + analysis["equipment"]["unclassified"]
        == analysis["equipment"]["case_count"]
    )


def test_html_contains_four_sections_radio_filters_and_csv_download():
    dataset = generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    rows, warnings, error = builder._validate_dataset(dataset)
    assert error is None
    analysis = builder.analyze_production_rows(rows, dataset)
    document = builder.render_production_report_html(rows, analysis, warnings=warnings)

    assert "생산실적 분석" in document
    assert "생산부족 Case 세부 분석" in document
    assert "CAPA실적 분석" in document
    assert "장비Assign 조정 세부 분석" in document
    assert "생산실적 분석 PIE CHART" in document
    assert "생산부족 원인 PIE CHART" in document
    assert "CAPA실적 PIE CHART" in document
    assert "CAPA실적 이상 PIE CHART" in document
    assert "장비Assign 조정 PIE CHART" in document
    assert document.count('class="chart-card"') == 6
    assert document.count("<table>") == 4
    assert 'name="filter-production"' in document
    assert 'name="filter-shortage"' in document
    assert 'name="filter-capa"' in document
    assert 'name="filter-equipment"' in document
    assert "엑셀용 CSV 다운로드" in document
    assert "\ufeff" not in document
    assert '"\\ufeff"+lines.join' in document
    assert 'lines.join("\\r\\n")' in document
    assert 'lines.join("\r\n")' not in document
    assert "https://" not in document
    assert len(document.encode("utf-8")) < builder.MAX_HTML_BYTES


def test_report_builds_compact_mongodb_collection_api_contract_without_langflow_file_copy():
    dataset = generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    def publish(**kwargs):
        assert "실시간 생산 분석 Report" in kwargs["html_document"]
        return {
            "report_id": "report-1",
            "view_url": "https://reports.example.internal/reports/view/report-1",
            "download_url": "https://reports.example.internal/reports/download/report-1",
            "expires_at": "2026-07-27T18:30:00+09:00",
            "ttl_hours": 4,
            "storage_backend": "mongodb_collection",
        }

    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            max_html_rows=1_000,
            report_api_url="https://reports.example.internal",
            report_ttl_hours=4,
            report_publisher_fn=publish,
            file_token="fixed",
        )
    )

    assert result["contract_version"] == "realtime.production.report.v1"
    assert result["status"] == "ok"
    assert result["success"] is True
    assert result["report_scope"]["case_count"] == 500
    assert result["artifacts"][0]["artifact_type"] == "html_report"
    assert result["artifacts"][0]["storage_backend"] == "mongodb_collection"
    assert "path" not in result["artifacts"][0]
    assert result["artifacts"][0]["view_url"].startswith("https://")
    assert "상세 HTML Report 보기" in result["message"]
    assert "rows" not in result
    assert "html" not in result
    assert result["followup"]["available"] is False


def test_report_context_payload_is_result_store_compatible_and_session_bound():
    dataset = generator.build_dummy_production_dataset(
        row_count=20,
        seed=20260727,
        work_date="2026-07-27",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    bundle = view_bundle_builder.build_realtime_report_view_bundle(dataset, Question())
    payload = _context_payload(dataset)

    assert bundle["contract_version"] == "report.view.bundle.v1"
    assert bundle["views"][0]["view_key"] == "report_snapshot"
    assert "query_source_contract" not in bundle["views"][0]
    assert payload["contract_version"] == "report.context.payload.v1"
    assert payload["execution_gate"]["status"] == "ready"
    assert payload["request"]["session_id"] == "session-report"
    assert payload["analysis"]["row_count"] == 20
    assert payload["data"]["columns"] == dataset["columns"]
    assert "rows" not in payload["data"]
    assert payload["runtime_sources"]["report_snapshot"] == dataset["rows"]
    assert payload["_full_result_rows"] == dataset["rows"]
    assert payload["source_results"][0]["dataset_key"] == "production_judgement_snapshot"
    assert payload["source_results"][0]["query_source_contract"]["purpose"] == "case_detail"
    assert payload["source_results"][0]["query_source_contract"]["allowed_operations"] == [
        "filter",
        "sort_and_top_n",
        "select_columns",
    ]
    shortage_source = payload["source_results"][1]
    assert shortage_source["source_alias"] == "report_shortage_products"
    assert shortage_source["dataset_key"] == "report_shortage_products"
    assert shortage_source["query_source_contract"]["purpose"] == "production_shortage_products"
    assert shortage_source["query_source_contract"]["allowed_operations"] == [
        "filter",
        "sort_and_top_n",
        "select_columns",
    ]
    assert shortage_source["query_source_contract"]["grain"] == {
        "kind": "product",
        "columns": ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"],
        "unique": True,
    }
    assert payload["intent_plan"]["resolved_execution_graph"]["external_source_requirements"] == [
        {"source_alias": "report_snapshot", "dataset_key": "production_judgement_snapshot"},
        {"source_alias": "report_shortage_products", "dataset_key": "report_shortage_products"},
    ]
    stored_payload, storage_manifest = result_store._compact_store_payload(
        payload,
        max_result_rows=20_000,
        max_source_rows_per_alias=10_000,
        max_document_bytes=8 * 1024 * 1024,
    )
    assert storage_manifest["compacted"] is False
    assert storage_manifest["result_rows"] == {"original_count": 20, "stored_count": 20, "complete": True}
    assert storage_manifest["runtime_sources"]["report_snapshot"] == {
        "original_count": 20,
        "stored_count": 20,
        "complete": True,
    }
    shortage_count = len(payload["runtime_sources"]["report_shortage_products"])
    assert storage_manifest["runtime_sources"]["report_shortage_products"] == {
        "original_count": shortage_count,
        "stored_count": shortage_count,
        "complete": True,
    }
    assert stored_payload["result_rows"] == dataset["rows"]
    assert stored_payload["runtime_sources"]["report_snapshot"] == dataset["rows"]
    assert stored_payload["runtime_sources"]["report_shortage_products"] == payload["runtime_sources"][
        "report_shortage_products"
    ]
    assert stored_payload["source_results"][1]["query_source_contract"] == shortage_source[
        "query_source_contract"
    ]

    missing_session = Question()
    missing_session.session_id = ""
    missing_session.data = {"text": missing_session.text}
    blocked = _context_payload(dataset, missing_session)
    assert blocked["execution_gate"] == {"status": "blocked", "reason": "missing_session_id"}
    assert blocked["runtime_sources"] == {}
    assert "_full_result_rows" not in blocked


def test_report_context_materializes_one_shortage_row_per_physical_product_and_recomputes_rate():
    dataset = generator.build_dummy_production_dataset(row_count=100, seed=20260727)
    base = deepcopy(next(row for row in dataset["rows"] if row["달성율*판정"] == "생산부족"))
    first = deepcopy(base)
    first.update({"OPER": "OP-A", "PRODUCTION": 10, "OUT_PLAN": 100, "생산실적달성율": 10.0})
    second = deepcopy(base)
    second.update({"OPER": "OP-B", "PRODUCTION": 30, "OUT_PLAN": 100, "생산실적달성율": 30.0})
    missing_quantity = deepcopy(base)
    missing_quantity.update({"OPER": "OP-C", "PRODUCTION": None, "OUT_PLAN": "", "생산실적달성율": None})
    excluded = deepcopy(base)
    excluded.update(
        {
            "OPER": "OP-D",
            "PRODUCTION": 1_000,
            "OUT_PLAN": 1_000,
            "생산실적달성율": 100.0,
            "달성율*판정": "정상",
        }
    )
    dataset["rows"] = [first, second, missing_quantity, excluded]
    dataset["row_count"] = len(dataset["rows"])

    payload = _context_payload(dataset)

    assert payload["runtime_sources"]["report_snapshot"] == dataset["rows"]
    shortage_rows = payload["runtime_sources"]["report_shortage_products"]
    assert len(shortage_rows) == 1
    assert {column: shortage_rows[0][column] for column in view_bundle_builder.PRODUCT_KEY_COLUMNS} == {
        column: base[column] for column in view_bundle_builder.PRODUCT_KEY_COLUMNS
    }
    assert shortage_rows[0]["PRODUCTION"] == 40
    assert shortage_rows[0]["OUT_PLAN"] == 200
    assert shortage_rows[0]["생산실적달성율"] == 20.0
    assert shortage_rows[0]["달성율*판정"] == "생산부족"
    assert len(
        {
            tuple(row[column] for column in view_bundle_builder.PRODUCT_KEY_COLUMNS)
            for row in shortage_rows
        }
    ) == len(shortage_rows)


def test_generic_context_publisher_derives_safe_query_contract_without_manual_json():
    report_data = {
        "rows": [
            {"제품": "P-01", "위험등급": "높음", "위험점수": 91, "내부비고": "공개 금지"},
            {"제품": "P-02", "위험등급": "낮음", "위험점수": 14, "내부비고": "공개 금지"},
        ],
        "report_columns": ["제품", "위험등급", "위험점수"],
        "source_type": "report_recipe",
    }
    payload = context_publisher.build_report_context_payload(
        Question("생산부족 장비 위험 Report를 만들어줘"),
        report_data_value=report_data,
        report_title="생산부족 장비 위험 Report",
        report_type="shortage_equipment_risk",
        view_label="장비 위험 제품",
    )

    assert payload["execution_gate"]["status"] == "ready"
    assert payload["trace"]["inspection"]["report_context_publisher"]["input_mode"] == "direct_data"
    source = payload["source_results"][0]
    contract = source["query_source_contract"]
    assert source["source_alias"] == "report_snapshot"
    assert contract["display_name"] == "장비 위험 제품"
    assert contract["default_view"] is True
    assert contract["columns"] == ["제품", "위험등급", "위험점수"]
    assert contract["grain"] == {"kind": "row", "columns": [], "unique": False}
    assert contract["allowed_operations"] == ["filter", "sort_and_top_n", "select_columns"]
    assert "내부비고" not in payload["runtime_sources"]["report_snapshot"][0]


def test_generic_context_publisher_keeps_multiple_evidence_sources_but_exposes_only_declared_views():
    bundle = {
        "contract_version": "report.view.bundle.v1",
        "report": {"report_type": "shortage_equipment_risk", "title": "생산부족 장비 위험 Report"},
        "views": [
            {
                "view_key": "production_cases",
                "display_name": "생산 Case 원본",
                "rows": [{"제품": "P-01", "생산량": 20}],
                "columns": ["제품", "생산량"],
            },
            {
                "view_key": "equipment_assign",
                "display_name": "장비 Assign 원본",
                "rows": [{"제품": "P-01", "필요장비대수": 2}],
                "columns": ["제품", "필요장비대수"],
            },
            {
                "view_key": "shortage_equipment_risk_products",
                "display_name": "생산부족 장비위험 제품",
                "aliases": ["장비 위험 제품"],
                "purpose": "shortage_equipment_risk_products",
                "rows": [{"제품": "P-01", "생산실적달성율": 67.0, "장비교체판단": "장비필요", "필요장비대수": 2}],
                "columns": ["제품", "생산실적달성율", "장비교체판단", "필요장비대수"],
                "identity_columns": ["제품"],
                "grain": {"kind": "product", "unique": True},
                "lineage": ["production_cases", "equipment_assign"],
                "default_view": True,
            },
        ],
    }
    payload = context_publisher.build_report_context_payload(Question(), bundle)

    assert [item["source_alias"] for item in payload["source_results"]] == [
        "production_cases",
        "equipment_assign",
        "shortage_equipment_risk_products",
    ]
    assert "query_source_contract" not in payload["source_results"][0]
    assert "query_source_contract" not in payload["source_results"][1]
    contract = payload["source_results"][2]["query_source_contract"]
    assert contract["lineage"] == ["production_cases", "equipment_assign"]
    assert contract["default_view"] is True


def test_report_projects_stored_context_to_compact_followup_contract_and_state():
    dataset = generator.build_dummy_production_dataset(
        row_count=20,
        seed=20260727,
        work_date="2026-07-27",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    stored_context = _stored_report_context(dataset)
    published = {}

    def publish(**kwargs):
        published.update(kwargs)
        return {
            "report_id": "report-context-1",
            "view_url": "https://reports.example.internal/reports/view/report-context-1",
            "download_url": "https://reports.example.internal/reports/download/report-context-1",
            "expires_at": "2026-07-27T18:30:00+09:00",
            "ttl_hours": 4,
            "storage_backend": "mongodb_collection",
        }

    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            context_payload_value=stored_context,
            report_api_url="https://reports.example.internal",
            report_publisher_fn=publish,
            file_token="fixed-context",
        )
    )

    assert result["status"] == "ok"
    assert result["context_ref"] == "result:session-report:context-1"
    assert result["result_ref"]["role"] == "analysis_result"
    assert result["source_data_refs"][0]["role"] == "source_rows"
    assert result["available_datasets"][0]["source_alias"] == "report_snapshot"
    assert result["available_datasets"][0]["data_ref"]["path"] == "payload.runtime_sources.report_snapshot"
    assert result["available_datasets"][1]["source_alias"] == "report_shortage_products"
    assert result["available_datasets"][1]["data_ref"]["path"] == (
        "payload.runtime_sources.report_shortage_products"
    )
    assert result["available_datasets"][1]["query_source_contract"]["purpose"] == (
        "production_shortage_products"
    )
    assert result["followup"] == {
        "available": True,
        "context_ref": "result:session-report:context-1",
        "reason": "",
    }
    assert published["context_ref"] == result["context_ref"]
    assert published["available_datasets"] == result["available_datasets"]

    state = result["state"]
    assert state["session_id"] == "session-report"
    assert state["current_data"]["source_aliases"] == ["report_snapshot", "report_shortage_products"]
    assert state["current_data"]["source_dataset_keys"] == [
        "production_judgement_snapshot",
        "report_shortage_products",
    ]
    assert state["current_data"]["data_ref"] == result["result_ref"]
    assert state["followup_source_results"][0]["data_ref"] == result["source_data_refs"][0]
    assert state["runtime_source_refs"]["report_snapshot"] == result["source_data_refs"][0]
    assert state["runtime_source_refs"]["report_shortage_products"] == result["source_data_refs"][1]
    assert [item["purpose"] for item in state["current_data"]["query_sources"]] == [
        "case_detail",
        "production_shortage_products",
    ]
    report_context = state["current_data"]["report_context"]
    assert set(report_context) == {
        "context_version",
        "context_ref",
        "report_type",
        "snapshot_id",
        "as_of",
        "expires_at",
        "report_scope",
        "kpi_facts",
        "rules",
        "allowed_operations",
        "semantic_filters",
        "value_domains",
        "query_sources",
    }
    assert report_context["context_version"] == "report.context.v1"
    assert report_context["report_type"] == "realtime_production"
    assert report_context["context_ref"] == result["context_ref"]
    assert report_context["expires_at"] == "2099-07-27T18:30:00+09:00"
    assert report_context["report_scope"] == result["report_scope"]
    assert report_context["kpi_facts"] == result["kpis"]
    assert report_context["rules"] == {"rules_version": "realtime.production.report.rules.v1"}
    assert report_context["allowed_operations"] == ["filter", "sort_and_top_n", "select_columns"]
    query_sources = {item["purpose"]: item for item in report_context["query_sources"]}
    assert query_sources["case_detail"]["source_alias"] == "report_snapshot"
    assert query_sources["production_shortage_products"]["source_alias"] == "report_shortage_products"
    assert query_sources["production_shortage_products"]["grain"] == {
        "kind": "product",
        "columns": ["MODE", "DENSITY", "TECH", "ORG", "PKG1", "PKG2", "LEAD", "MCP_NO"],
        "unique": True,
    }
    semantic_filters = {item["key"]: item for item in report_context["semantic_filters"]}
    assert semantic_filters["production_shortage"] == {
        "key": "production_shortage",
        "aliases": ["생산부족", "생산 부족", "생산 부족 제품", "생산부족 제품"],
        "source_alias": "report_snapshot",
        "column": "달성율*판정",
        "operator": "eq",
        "value": "생산부족",
    }
    assert semantic_filters["equipment_needed"]["aliases"] == [
        "장비필요",
        "장비 필요",
        "장비필요 제품",
        "장비 필요 제품",
    ]
    value_domains = {item["column"]: item for item in report_context["value_domains"]}
    assert value_domains["달성율*판정"]["source_alias"] == "report_snapshot"
    assert set(value_domains["달성율*판정"]["values"]) == {
        "정상",
        "정상(초과생산)",
        "Abnormal",
        "생산부족",
    }
    for semantic_filter in report_context["semantic_filters"]:
        assert semantic_filter["value"] in value_domains[semantic_filter["column"]]["values"] or (
            isinstance(semantic_filter["value"], list)
            and set(semantic_filter["value"]).issubset(value_domains[semantic_filter["column"]]["values"])
        )
    compact_context_text = json.dumps(report_context, ensure_ascii=False)
    assert "rows" not in report_context
    assert "html" not in compact_context_text.lower()
    assert "http://" not in compact_context_text
    assert "https://" not in compact_context_text


def test_report_context_store_failure_does_not_fail_report_generation():
    dataset = generator.build_dummy_production_dataset(row_count=20, seed=20260727)
    failed_context = {
        "request": {"question": Question().text, "session_id": "session-report"},
        "trace": {
            "inspection": {
                "result_store": {"status": "error", "errors": [{"type": "mongo_write_error"}]}
            }
        },
    }

    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            context_payload_value=failed_context,
            report_api_url="https://reports.example.internal",
            report_publisher_fn=lambda **_kwargs: {
                "report_id": "report-without-context",
                "view_url": "https://reports.example.internal/reports/view/report-without-context",
                "download_url": "https://reports.example.internal/reports/download/report-without-context",
                "expires_at": "2026-07-27T18:30:00+09:00",
                "ttl_hours": 4,
                "storage_backend": "mongodb_collection",
            },
        )
    )

    assert result["status"] == "ok"
    assert result["artifacts"]
    assert result["followup"] == {"available": False, "context_ref": "", "reason": "error"}
    assert "report_context" not in result["state"]["current_data"]
    invalidated = result["state"]["current_data"]["report_context_status"]
    assert invalidated["status"] == "invalidated"
    assert invalidated["reason"] == "error"
    assert any(item["type"] == "report_context_unavailable" for item in result["warnings"])


def test_report_api_failure_returns_no_unpublished_local_artifact():
    dataset = generator.build_dummy_production_dataset(
        row_count=20,
        seed=20260727,
        work_date="2026-07-27",
    )
    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            report_api_url="https://reports.example.internal",
            report_publisher_fn=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            file_token="fixed",
        )
    )

    assert result["status"] == "error"
    assert result["artifacts"] == []
    assert result["errors"][0]["type"] == "report_api_publish_error"
    assert "report_context" not in result["state"]["current_data"]
    invalidated = result["state"]["current_data"]["report_context_status"]
    assert invalidated["status"] == "invalidated"
    assert invalidated["reason"] == "report_api_publish_error"


def test_new_failed_report_replaces_older_followup_context_in_same_session():
    documents = {}

    class FakeCollection:
        def find_one(self, query):
            return deepcopy(documents.get(query["_id"], {}))

        def replace_one(self, query, document, upsert=False):
            assert upsert is True
            assert query == {"_id": document["_id"]}
            documents[document["_id"]] = deepcopy(document)

    class FakeDatabase:
        def __getitem__(self, _collection_name):
            return FakeCollection()

    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            assert uri == "mongodb://fake"
            assert kwargs["serverSelectionTimeoutMS"] == 5000

        def __getitem__(self, database_name):
            assert database_name == "datagov"
            return FakeDatabase()

        def close(self):
            return None

    dataset = generator.build_dummy_production_dataset(row_count=20, seed=20260727)
    successful = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question("오늘 W/B Report를 만들어줘"),
            context_payload_value=_stored_report_context(dataset),
            report_api_url="https://reports.example.internal",
            report_publisher_fn=lambda **_kwargs: {
                "report_id": "report-a",
                "view_url": "https://reports.example.internal/reports/view/report-a",
                "download_url": "https://reports.example.internal/reports/download/report-a",
                "expires_at": "2026-07-27T18:30:00+09:00",
                "ttl_hours": 4,
                "storage_backend": "mongodb_collection",
            },
        )
    )
    clarification = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value={
                "contract_version": builder.SELECTION_CONTRACT_VERSION,
                "status": "clarification_required",
                "message": "공정그룹을 선택해 주세요.",
            },
            question_value=Question("새 Report를 만들어줘"),
            report_api_url="https://reports.example.internal",
        )
    )

    original_import_module = session_writer.import_module
    session_writer.import_module = lambda _name: types.SimpleNamespace(MongoClient=FakeMongoClient)
    try:
        first = session_writer.write_session_state(
            successful,
            "mongodb://fake",
            "datagov",
            "agent_v4_session_states",
        )
        assert first["session_state_write"]["saved"] is True
        stored_document = documents["session_state:session-report"]
        stored_first = stored_document["state"]
        assert stored_first["current_data"]["report_context"]["context_ref"]
        assert stored_document["updated_at"].endswith("+09:00")

        second = session_writer.write_session_state(
            clarification,
            "mongodb://fake",
            "datagov",
            "agent_v4_session_states",
        )
        assert second["session_state_write"]["saved"] is True
    finally:
        session_writer.import_module = original_import_module

    stored_second = documents["session_state:session-report"]["state"]
    assert "report_context" not in stored_second["current_data"]
    assert stored_second["current_data"]["report_context_status"] == {
        "context_version": "report.context.v1",
        "report_type": "realtime_production",
        "status": "invalidated",
        "reason": "process_group_not_selected",
    }
    assert stored_second["followup_source_results"] == []
    assert "runtime_source_refs" not in stored_second


def test_expired_report_context_is_removed_before_followup_planning():
    dataset = generator.build_dummy_production_dataset(row_count=20, seed=20260727)
    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            context_payload_value=_stored_report_context(dataset),
            report_api_url="https://reports.example.internal",
            report_publisher_fn=lambda **_kwargs: {
                "report_id": "report-expiry",
                "view_url": "https://reports.example.internal/reports/view/report-expiry",
                "download_url": "https://reports.example.internal/reports/download/report-expiry",
                "expires_at": "2099-07-27T18:30:00+09:00",
                "ttl_hours": 4,
                "storage_backend": "mongodb_collection",
            },
        )
    )
    valid_loaded = session_loader.load_session_state(
        question=Question("그중 생산부족 제품 알려줘"),
        fallback_state_value={"state": result["state"]},
    )
    assert valid_loaded["state"]["current_data"]["report_context"]["context_ref"]

    expired_state = deepcopy(result["state"])
    expired_state["current_data"]["report_context"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    expired_loaded = session_loader.load_session_state(
        question=Question("그중 생산부족 제품 알려줘"),
        fallback_state_value={"state": expired_state},
    )
    assert expired_loaded["session_state_load"]["report_context_status"] == {
        "status": "invalidated",
        "reason": "report_context_expired",
    }
    expired_current = expired_loaded["state"]["current_data"]
    assert "report_context" not in expired_current
    assert "data_ref" not in expired_current
    assert expired_current["report_context_status"]["reason"] == "report_context_expired"
    assert expired_loaded["state"]["followup_source_results"] == []
    assert "runtime_source_refs" not in expired_loaded["state"]


def test_schema_failure_and_terminal_contract_are_deterministic():
    rows, warnings, error = builder._validate_dataset({"rows": [{"WORK_DATE": "2026-07-27"}]})
    assert rows == []
    assert warnings == []
    assert error["type"] == "missing_required_columns"

    invalid = terminal.normalize_realtime_production_report_result({"status": "ok"})
    assert invalid["contract_version"] == "realtime.production.report.v1"
    assert invalid["status"] == "error"
    assert invalid["errors"][0]["type"] == "invalid_realtime_production_report_contract"

    valid_payload = {
        "contract_version": "realtime.production.report.v1",
        "status": "ok",
        "message": "fallback",
        "followup": {"available": True, "context_ref": "result:session-report:context-1"},
        "state": {"session_id": "session-report"},
        "session_state_write": {"saved": True, "errors": []},
    }
    original = terminal.Message(text="### Report 답변")
    forwarded = terminal.report_message_after_session_write(valid_payload, original)
    assert forwarded is original
    assert forwarded.text == "### Report 답변"

    failed_write = {
        **valid_payload,
        "message": "### Report 답변",
        "session_state_write": {"saved": False, "reason": "mongo_error", "errors": ["offline"]},
    }
    finalized = terminal.finalize_report_after_session_write(failed_write)
    assert finalized["followup"]["available"] is False
    assert finalized["followup"]["reason"] == "report_session_state_unavailable"
    assert finalized["state"] == {}
    assert finalized["warnings"][-1]["type"] == "report_session_state_unavailable"
    assert "후속 질문용 상태를 저장하지 못했습니다" in finalized["message"]
    failed_message = terminal.report_message_after_session_write(failed_write, original)
    assert failed_message is not original
    assert "후속 질문용 상태를 저장하지 못했습니다" in failed_message.text

    failed_invalidation = {
        **valid_payload,
        "message": "공정그룹을 선택해 주세요.",
        "followup": {"available": False, "context_ref": "", "reason": "process_group_not_selected"},
        "state": {"session_id": "session-report", "current_data": {"report_context_status": {"status": "invalidated"}}},
        "session_state_write": {"saved": False, "reason": "mongo_error", "errors": ["offline"]},
    }
    invalidation_result = terminal.finalize_report_after_session_write(failed_invalidation)
    assert invalidation_result["followup"] == {
        "available": False,
        "context_ref": "",
        "reason": "report_context_invalidation_failed",
    }
    assert invalidation_result["warnings"][-1]["type"] == "report_context_invalidation_failed"
    assert "이전 Report 문맥을 무효화하지 못했습니다" in invalidation_result["message"]


def test_legacy_report_preserves_prefollowup_response_and_warning_contract():
    dataset = generator.build_dummy_production_dataset(
        row_count=20,
        seed=20260727,
        work_date="2026-07-27",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    published = {}

    def publish(**kwargs):
        published.update(kwargs)
        return {
            "report_id": "legacy-report-1",
            "view_url": "https://reports.example.internal/reports/view/legacy-report-1",
            "download_url": "https://reports.example.internal/reports/download/legacy-report-1",
            "expires_at": "2026-07-27T18:30:00+09:00",
            "ttl_hours": 4,
            "storage_backend": "mongodb_collection",
        }

    result = asyncio.run(
        legacy_builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            max_html_rows=1_000,
            report_api_url="https://reports.example.internal",
            report_ttl_hours=4,
            report_publisher_fn=publish,
            file_token="legacy-fixed",
        )
    )

    assert set(result) == {
        "contract_version",
        "response_type",
        "status",
        "success",
        "summary",
        "message",
        "report_scope",
        "rules_version",
        "kpis",
        "artifacts",
        "warnings",
        "errors",
    }
    assert result["status"] == "ok"
    assert result["success"] is True
    assert result["warnings"] == []
    assert not any(item.get("type") == "report_context_unavailable" for item in result["warnings"])
    assert "context_ref" not in result
    assert "followup" not in result
    assert "state" not in result
    assert set(published) == {
        "html_document",
        "question",
        "download_name",
        "analysis",
        "report_api_url",
        "report_ttl_hours",
    }


def test_legacy_report_clarification_and_terminal_keep_single_stage_contract():
    selection = gate.select_process_group_dataset(
        question_value=Question("오늘 실시간 생산 분석을 해줘"),
        catalog_value=_process_group_catalog(),
        llm_response_value={
            "status": "missing",
            "process_group_key": "",
            "reason": "그룹 표현 없음",
            "evidence": [],
        },
        dataset_value=_multi_group_dataset(),
    )
    result = asyncio.run(
        legacy_builder.build_realtime_production_report(
            dataset_value=selection,
            question_value=Question("오늘 실시간 생산 분석을 해줘"),
            report_api_url="https://reports.example.internal",
            report_publisher_fn=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy clarification must not publish")
            ),
        )
    )

    assert result["status"] == "clarification_required"
    assert result["artifacts"] == []
    assert result["warnings"] == []
    assert "context_ref" not in result
    assert "followup" not in result
    assert "state" not in result

    input_names = {
        getattr(item, "name", None) or getattr(item, "kwargs", {}).get("name")
        for item in legacy_terminal.RealtimeProductionReportApiTerminal.inputs
    }
    output_names = {
        getattr(item, "name", None) or getattr(item, "kwargs", {}).get("name")
        for item in legacy_terminal.RealtimeProductionReportApiTerminal.outputs
    }
    assert input_names == {"report_result"}
    assert output_names == {"api_response"}
