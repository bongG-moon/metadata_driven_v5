from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PATH = ROOT / "langflow_components" / "visualization_flow" / "00_html_visualization_builder.py"
API_TERMINAL_PATH = ROOT / "langflow_components" / "visualization_flow" / "01_html_visualization_api_terminal.py"


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
    component_module = sys.modules["lfx.custom.custom_component.component"]
    component_module.Component = getattr(component_module, "Component", Component)
    io_module = sys.modules["lfx.io"]
    for name in (
        "BoolInput",
        "DataInput",
        "DropdownInput",
        "HandleInput",
        "MessageTextInput",
        "Output",
        "StrInput",
    ):
        setattr(io_module, name, getattr(io_module, name, InputBase))
    data_module = sys.modules["lfx.schema.data"]
    data_module.Data = getattr(data_module, "Data", Data)
    message_module = sys.modules["lfx.schema.message"]
    message_module.Message = getattr(message_module, "Message", Message)
    deps_module = sys.modules["lfx.services.deps"]
    deps_module.get_storage_service = getattr(deps_module, "get_storage_service", lambda: None)


_install_lfx_stubs()


def _load_component():
    spec = importlib.util.spec_from_file_location("html_visualization_builder_test", COMPONENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


component = _load_component()


def _load_api_terminal_component():
    spec = importlib.util.spec_from_file_location("html_visualization_api_terminal_test", API_TERMINAL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


api_terminal_component = _load_api_terminal_component()


class Question:
    def __init__(self, text: str, session_id: str = "session-1"):
        self.text = text
        self.session_id = session_id
        self.data = {"text": text, "session_id": session_id}


class FakeCollection:
    def __init__(self, document, metrics):
        self.document = document
        self.metrics = metrics

    def find_one(self, query, projection):
        self.metrics["queries"].append((query, projection))
        return self.document


class FakeDatabase:
    def __init__(self, document, metrics):
        self.document = document
        self.metrics = metrics

    def __getitem__(self, name):
        self.metrics["collections"].append(name)
        return FakeCollection(self.document, self.metrics)


def _mongo_client_factory(document):
    metrics = {"kwargs": [], "databases": [], "collections": [], "queries": [], "closed": 0}

    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            metrics["uri"] = uri
            metrics["kwargs"].append(kwargs)

        def __getitem__(self, name):
            metrics["databases"].append(name)
            return FakeDatabase(document, metrics)

        def close(self):
            metrics["closed"] += 1

    return FakeMongoClient, metrics


class FakeStorage:
    def __init__(self):
        self.calls = []

    async def save_file(self, **kwargs):
        self.calls.append(kwargs)


def _published_report(**kwargs):
    assert kwargs["html_document"].startswith("<!doctype html>")
    assert kwargs["report_api_url"] == "http://127.0.0.1:8010"
    return {
        "report_id": "20260719010101_0123456789abcdef0123456789abcdef",
        "view_url": "http://127.0.0.1:8010/reports/view/20260719010101_0123456789abcdef0123456789abcdef",
        "download_url": "http://127.0.0.1:8010/reports/download/20260719010101_0123456789abcdef0123456789abcdef",
        "expires_at": "2026-07-20T01:01:01+00:00",
        "ttl_hours": 24,
    }


def _document(rows, *, session_id="session-1", complete=True):
    return {
        "session_id": session_id,
        "payload": {
            "result_rows": rows,
            "storage_manifest": {"result_rows": {"complete": complete, "stored_count": len(rows)}},
        },
    }


def test_chart_plan_prefers_date_and_production_metric() -> None:
    rows = [
        {"WORK_DT": "20260716", "LEAD": 78, "PRODUCTION": 100},
        {"WORK_DT": "20260717", "LEAD": 78, "PRODUCTION": 120},
        {"WORK_DT": "20260718", "LEAD": 78, "PRODUCTION": 140},
    ]
    plan, error = component.select_chart_plan(rows, "최근 3일 생산량을 선 그래프로 보여줘")
    assert error is None
    assert plan["chart_type"] == "line"
    assert plan["x_column"] == "WORK_DT"
    assert plan["y_columns"] == ["PRODUCTION"]


def test_all_empty_named_metric_is_kept_as_zero_series() -> None:
    rows = [
        {"WORK_DT": "20260717", "PRODUCTION": None},
        {"WORK_DT": "20260718", "PRODUCTION": ""},
    ]
    plan, error = component.select_chart_plan(rows, "일자별 생산량을 선 그래프로 보여줘")
    assert error is None
    assert plan["x_column"] == "WORK_DT"
    assert plan["y_columns"] == ["PRODUCTION"]
    html_text = component.render_html_document(rows, plan)
    assert html_text.count("PRODUCTION: 0") == 2


def test_html_is_self_contained_escaped_and_null_metric_is_zero() -> None:
    rows = [{"WORK_DT": "20260717", "PRODUCTION": None}, {"WORK_DT": "20260718", "PRODUCTION": 12}]
    plan = {
        "title": "<script>alert(1)</script>",
        "chart_type": "line",
        "x_column": "WORK_DT",
        "y_columns": ["PRODUCTION"],
    }
    text = component.render_html_document(rows, plan)
    lowered = text.lower()
    assert text.startswith("<!doctype html>")
    assert "<svg" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script" not in lowered
    assert "src=" not in lowered
    assert "http://" not in lowered and "https://" not in lowered
    assert "PRODUCTION: 0" in text
    assert "<td>0</td>" in text


def test_loader_rejects_other_session_and_closes_client_with_all_timeouts() -> None:
    client_cls, metrics = _mongo_client_factory(_document([{"WORK_DT": "20260718", "PRODUCTION": 1}], session_id="other"))
    source, error = component.load_result_rows(
        Question("생산량을 그려줘"),
        "result:session-1:one",
        "mongodb://example",
        mongo_client_cls=client_cls,
    )
    assert source == {}
    assert error["type"] == "upstream_session_mismatch"
    assert metrics["closed"] == 1
    assert metrics["kwargs"] == [
        {"serverSelectionTimeoutMS": 5000, "connectTimeoutMS": 5000, "socketTimeoutMS": 5000}
    ]

    runtime_client_cls, runtime_metrics = _mongo_client_factory(
        _document([{"WORK_DT": "20260718", "PRODUCTION": 1}], session_id="runtime-session")
    )
    source, error = component.load_result_rows(
        Question("생산량을 그려줘", session_id="message-session"),
        "result:runtime-session:one",
        "mongodb://example",
        mongo_client_cls=runtime_client_cls,
        runtime_session_id="runtime-session",
    )
    assert error is None
    assert source["session_id"] == "runtime-session"
    assert runtime_metrics["closed"] == 1


def test_loader_blocks_incomplete_result() -> None:
    client_cls, metrics = _mongo_client_factory(_document([{"WORK_DT": "20260718", "PRODUCTION": 1}], complete=False))
    source, error = component.load_result_rows(
        Question("생산량을 그려줘"),
        "result:session-1:one",
        "mongodb://example",
        mongo_client_cls=client_cls,
    )
    assert source == {}
    assert error["type"] == "upstream_result_incomplete"
    assert metrics["closed"] == 1


def test_success_saves_one_html_and_returns_descriptor_without_rows_or_html() -> None:
    rows = [
        {"WORK_DT": "20260718", "PRODUCTION": 140},
        {"WORK_DT": "20260716", "PRODUCTION": 100},
        {"WORK_DT": "20260717", "PRODUCTION": None},
    ]
    client_cls, metrics = _mongo_client_factory(_document(rows))
    storage = FakeStorage()
    result = asyncio.run(
        component.build_html_visualization(
            question_value=Question("최근 3일 생산량을 선 그래프로 그려줘"),
            upstream_result_ref="result:session-1:production",
            mongo_uri="mongodb://example",
            flow_id="visualization-flow-id",
            storage_service=storage,
            mongo_client_cls=client_cls,
            report_publisher_fn=_published_report,
            file_token="fixed-token",
        )
    )
    assert result["contract_version"] == "visualization.result.v1"
    assert result["status"] == "ok"
    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert set(artifact) == {
        "artifact_type",
        "path",
        "report_id",
        "view_url",
        "download_url",
        "expires_at",
        "ttl_hours",
        "mime_type",
        "title",
        "download_name",
        "chart_type",
        "x_column",
        "y_columns",
        "row_count",
        "plotted_row_count",
        "size_bytes",
    }
    assert artifact["path"] == "visualization-flow-id/html-chart-fixedtoken.html"
    assert artifact["view_url"].startswith("http://127.0.0.1:8010/reports/view/")
    assert artifact["download_url"].startswith("http://127.0.0.1:8010/reports/download/")
    assert f"[HTML 차트 보기]({artifact['view_url']})" in result["message"]
    assert f"[HTML 다운로드]({artifact['download_url']})" in result["message"]
    assert "http" not in result["summary"]
    assert "WORK_DT" in result["summary"]
    assert artifact["x_column"] == "WORK_DT"
    assert artifact["y_columns"] == ["PRODUCTION"]
    assert len(storage.calls) == 1
    call = storage.calls[0]
    assert call["flow_id"] == "visualization-flow-id"
    assert call["file_name"] == "html-chart-fixedtoken.html"
    assert call["append"] is False
    assert call["data"].startswith(b"<!doctype html>")
    html_text = call["data"].decode("utf-8")
    assert html_text.index("20260716") < html_text.index("20260717") < html_text.index("20260718")
    serialized = json.dumps(result, ensure_ascii=False)
    assert "result_rows" not in serialized
    assert "html_content" not in serialized
    assert "20260716" not in serialized
    assert metrics["closed"] == 1


def test_row_limit_adds_sampling_warning() -> None:
    rows = [{"WORK_DT": f"2026-07-{index + 1:02d}", "PRODUCTION": index} for index in range(20)]
    client_cls, _ = _mongo_client_factory(_document(rows))
    storage = FakeStorage()
    result = asyncio.run(
        component.build_html_visualization(
            question_value=Question("생산량을 선 그래프로 그려줘"),
            upstream_result_ref="result:session-1:production",
            mongo_uri="mongodb://example",
            max_chart_rows=10,
            flow_id="visualization-flow-id",
            storage_service=storage,
            mongo_client_cls=client_cls,
            report_publisher_fn=_published_report,
            file_token="sampled",
        )
    )
    assert result["artifacts"][0]["row_count"] == 20
    assert result["artifacts"][0]["plotted_row_count"] == 10
    assert [item["type"] for item in result["warnings"]] == ["chart_rows_sampled"]


def test_report_api_failure_keeps_local_artifact_but_does_not_emit_broken_tauri_link() -> None:
    rows = [{"WORK_DT": "20260718", "PRODUCTION": 140}]
    client_cls, _ = _mongo_client_factory(_document(rows))
    storage = FakeStorage()

    def fail_publish(**_kwargs):
        raise RuntimeError("connection refused")

    result = asyncio.run(
        component.build_html_visualization(
            question_value=Question("오늘 생산량을 그래프로 그려줘"),
            upstream_result_ref="result:session-1:production",
            mongo_uri="mongodb://example",
            flow_id="visualization-flow-id",
            storage_service=storage,
            mongo_client_cls=client_cls,
            report_publisher_fn=fail_publish,
            file_token="publish-failed",
        )
    )

    assert result["status"] == "partial"
    assert result["success"] is True
    assert result["artifacts"][0]["path"].endswith(".html")
    assert "view_url" not in result["artifacts"][0]
    assert "download_url" not in result["artifacts"][0]
    assert [item["type"] for item in result["warnings"]] == ["report_api_publish_error"]
    assert "tauri.localhost" not in result["message"]
    assert "/api/v1/files/download" not in result["message"]


def test_public_report_urls_require_absolute_http_without_credentials_or_fragment() -> None:
    assert component._safe_public_url("https://reports.example.com/view/one?token=abc")
    assert component._safe_public_url("/api/v1/files/download/flow/file.html") == ""
    assert component._safe_public_url("javascript:alert(1)") == ""
    assert component._safe_public_url("https://user:secret@reports.example.com/view/one") == ""
    assert component._safe_public_url("https://reports.example.com/view/one#fragment") == ""


def test_component_message_and_api_outputs_share_one_execution(monkeypatch) -> None:
    rows = [{"WORK_DT": "20260718", "PRODUCTION": 140}]
    client_cls, metrics = _mongo_client_factory(_document(rows))
    pymongo = types.ModuleType("pymongo")
    pymongo.MongoClient = client_cls
    monkeypatch.setitem(sys.modules, "pymongo", pymongo)
    storage = FakeStorage()
    monkeypatch.setattr(component, "_runtime_storage_service", lambda: storage)
    monkeypatch.setattr(component, "publish_html_report", _published_report)

    instance = component.HTMLVisualizationBuilder()
    instance.question = Question("오늘 생산량을 그래프로 그려줘")
    instance.upstream_result_ref = "result:session-1:production"
    instance.mongo_uri = "mongodb://example"
    instance.mongo_database = "datagov"
    instance.collection_name = "agent_v4_result_store"
    instance.report_api_url = "http://127.0.0.1:8010"
    instance.report_ttl_hours = "24"
    instance.max_chart_rows = "500"
    instance.flow_id = "visualization-flow-id"
    instance.graph = SimpleNamespace(session_id="session-1")

    async def run_outputs():
        return await asyncio.gather(instance.build_message(), instance.build_api_response())

    message, data = asyncio.run(run_outputs())
    assert len(storage.calls) == 1
    assert metrics["closed"] == 1
    assert message.files == [data.data["artifacts"][0]["path"]]
    assert data.data["artifacts"][0]["download_url"] in message.text
    assert data.data["status"] == "ok"


def test_missing_result_ref_returns_structured_error_without_storage_call() -> None:
    storage = FakeStorage()
    result = asyncio.run(
        component.build_html_visualization(
            question_value=Question("생산량을 그려줘"),
            upstream_result_ref="",
            mongo_uri="mongodb://example",
            flow_id="visualization-flow-id",
            storage_service=storage,
        )
    )
    assert result["status"] == "error"
    assert result["artifacts"] == []
    assert result["errors"][0]["type"] == "missing_upstream_result_ref"
    assert storage.calls == []


def test_component_inputs_keep_standalone_mongodb_defaults_visible() -> None:
    inputs = {item.kwargs["name"]: item.kwargs for item in component.HTMLVisualizationBuilder.inputs}
    assert inputs["upstream_result_ref"]["advanced"] is False
    assert inputs["mongo_uri"]["advanced"] is False
    assert inputs["mongo_uri"]["load_from_db"] is True
    assert inputs["mongo_uri"]["value"] == "MONGO_URL"
    assert inputs["mongo_database"]["value"] == "datagov"
    assert inputs["collection_name"]["value"] == "agent_v4_result_store"
    assert inputs["report_api_url"]["advanced"] is False
    assert inputs["report_api_url"]["value"] == "http://127.0.0.1:8010"
    assert inputs["report_ttl_hours"]["advanced"] is False
    assert inputs["report_ttl_hours"]["value"] == "24"
    outputs = {item.kwargs["name"] for item in component.HTMLVisualizationBuilder.outputs}
    assert outputs == {"message", "api_response"}


def test_api_terminal_passes_only_visualization_contract_and_fails_closed() -> None:
    Data = sys.modules["lfx.schema.data"].Data
    valid = {
        "contract_version": "visualization.result.v1",
        "response_type": "html_visualization",
        "status": "ok",
        "success": True,
        "message": "차트를 생성했습니다.",
        "artifacts": [],
        "warnings": [],
        "errors": [],
    }
    assert api_terminal_component.normalize_visualization_api_result(Data(data=valid)) is valid

    invalid = api_terminal_component.normalize_visualization_api_result(Data(data={"message": "plain"}))
    assert invalid["contract_version"] == "visualization.result.v1"
    assert invalid["status"] == "error"
    assert invalid["artifacts"] == []
    assert invalid["errors"] == [
        {
            "type": "invalid_visualization_result_contract",
            "message": "시각화 생성기는 visualization.result.v1 Data 계약을 반환해야 합니다.",
        }
    ]

    instance = api_terminal_component.HTMLVisualizationApiTerminal()
    instance.visualization_result = Data(data=valid)
    assert instance.build_api_response().data is valid
