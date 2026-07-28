from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOW_ROOT = ROOT / "langflow_components" / "realtime_production_report_flow"
GENERATOR_PATH = FLOW_ROOT / "00_dummy_production_judgement_data.py"
CATALOG_PATH = FLOW_ROOT / "00a_process_group_catalog_loader.py"
PROMPT_PATH = FLOW_ROOT / "00b_process_group_selection_prompt.py"
GATE_PATH = FLOW_ROOT / "00c_process_group_selection_gate.py"
BUILDER_PATH = FLOW_ROOT / "01_realtime_production_report_builder.py"
TERMINAL_PATH = FLOW_ROOT / "02_realtime_production_report_api_terminal.py"


def _install_lfx_stubs() -> None:
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
builder = _load("realtime_production_report_builder_test", BUILDER_PATH)
terminal = _load("realtime_production_report_terminal_test", TERMINAL_PATH)


class Question:
    def __init__(self, text: str = "오늘 W/B 실시간 생산 분석 Report를 만들어줘"):
        self.text = text
        self.session_id = "session-report"
        self.data = {"text": text, "session_id": self.session_id}


class FakeStorage:
    def __init__(self):
        self.calls = []

    async def save_file(self, **kwargs):
        self.calls.append(kwargs)


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
    input_names = [item.kwargs["name"] for item in catalog.RealtimeProductionProcessGroupCatalogLoader.inputs]
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
    storage = FakeStorage()
    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=selection,
            question_value=Question("오늘 실시간 생산 분석을 해줘"),
            max_html_rows=1_000,
            report_api_url="https://reports.example.internal",
            report_ttl_hours=4,
            flow_id="flow-report",
            storage_service=storage,
            report_publisher_fn=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not publish")),
            file_token="fixed",
        )
    )

    assert result["contract_version"] == "realtime.production.report.v1"
    assert result["response_type"] == "realtime_production_process_group_clarification"
    assert result["status"] == "clarification_required"
    assert result["artifacts"] == []
    assert "공정그룹을 선택해 주세요" in result["message"]
    assert storage.calls == []


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


def test_report_builds_compact_api_contract_and_saves_once():
    dataset = generator.build_dummy_production_dataset(
        row_count=500,
        seed=20260727,
        work_date="2026-07-27",
        snapshot_at="2026-07-27T14:30:00+09:00",
    )
    storage = FakeStorage()

    def publish(**kwargs):
        assert "실시간 생산 분석 Report" in kwargs["html_document"]
        return {
            "report_id": "report-1",
            "view_url": "https://reports.example.internal/reports/view/report-1",
            "download_url": "https://reports.example.internal/reports/download/report-1",
            "expires_at": "2026-07-27T18:30:00+09:00",
            "ttl_hours": 4,
        }

    result = asyncio.run(
        builder.build_realtime_production_report(
            dataset_value=dataset,
            question_value=Question(),
            max_html_rows=1_000,
            report_api_url="https://reports.example.internal",
            report_ttl_hours=4,
            flow_id="flow-report",
            storage_service=storage,
            report_publisher_fn=publish,
            file_token="fixed",
        )
    )

    assert result["contract_version"] == "realtime.production.report.v1"
    assert result["status"] == "ok"
    assert result["success"] is True
    assert result["report_scope"]["case_count"] == 500
    assert result["artifacts"][0]["artifact_type"] == "html_report"
    assert result["artifacts"][0]["view_url"].startswith("https://")
    assert "상세 HTML Report 보기" in result["message"]
    assert "rows" not in result
    assert "html" not in result
    assert len(storage.calls) == 1
    assert storage.calls[0]["file_name"] == "realtime-production-report-fixed.html"


def test_schema_failure_and_terminal_contract_are_deterministic():
    rows, warnings, error = builder._validate_dataset({"rows": [{"WORK_DATE": "2026-07-27"}]})
    assert rows == []
    assert warnings == []
    assert error["type"] == "missing_required_columns"

    invalid = terminal.normalize_realtime_production_report_result({"status": "ok"})
    assert invalid["contract_version"] == "realtime.production.report.v1"
    assert invalid["status"] == "error"
    assert invalid["errors"][0]["type"] == "invalid_realtime_production_report_contract"
